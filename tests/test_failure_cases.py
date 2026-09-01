"""The assignment's five named failure cases, each a scenario that fails if the
guarantee breaks. Framed end-to-end, not as unit tests."""

from smartdialer import db as _db, agent as A, borrower as B, call as C, sweeper as S
from smartdialer import providers as P
from smartdialer.allocator import CallAllocator
from smartdialer.safety import decide
from smartdialer.snapshot import Snapshot, build_snapshot
from smartdialer.pacing import PredictivePacer
from smartdialer.clock import now


def _snap(**kw):
    base = dict(agents_available=10, prev_available=10, calls_dialing=0, calls_ringing=0,
                calls_connected=0, calls_in_flight=0, anomaly_count_5min=0,
                provider_degraded=False, predicted_answer_rate=0.4, agents_expected_free_soon=0)
    base.update(kw)
    return Snapshot(**base)


def test_case1_worker_crash_mid_flow(conn):
    """Agent reserved -> borrower reserved -> call initiated -> worker crashes.
    The sweeper must reconcile BOTH the call (FAILED, borrower requeued) AND the
    stranded agent (back to AVAILABLE)."""
    A.create_agent(conn, "a0")
    B.add_borrower(conn, "b0", "555")
    alloc = CallAllocator(conn, P.provider_a(seed=1))
    alloc.place(1, reserve_agents=True)
    call_id = conn.execute("SELECT id FROM calls").fetchone()["id"]
    assert A.get_state(conn, "a0") == A.DIALING and C.get_state(conn, call_id) == C.INITIATED

    # crash: no further events; time passes past the TTL
    conn.execute("UPDATE calls SET updated_at=? WHERE id=?", (now() - 40, call_id))
    S.sweep_stale_calls(conn)

    assert C.get_state(conn, call_id) == C.FAILED
    assert A.get_state(conn, "a0") == A.AVAILABLE           # regression guard for the bug
    assert conn.execute("SELECT state FROM borrowers WHERE id=?", ("b0",)).fetchone()["state"] == B.QUEUED


def test_case2_provider_outage(conn):
    """Provider degraded: the circuit breaker throttles NEW calls via the safety
    controller (not the pacer), and does not touch calls already in flight."""
    # an existing in-flight call
    B.add_borrower(conn, "b0", "555")
    C.create_call(conn, "existing", "b0")
    C.internal_transition(conn, "existing", C.QUEUED, C.RESERVED)
    C.internal_transition(conn, "existing", C.RESERVED, C.INITIATED)

    snap = _snap(agents_available=8, provider_degraded=True)
    proposed = PredictivePacer().propose(snap)               # pacer still proposes a big number
    assert proposed > 8
    d = decide(proposed, snap)
    assert d.action == "FALLBACK_PROGRESSIVE"
    assert d.approved_n == 8                                  # throttled to one-per-agent
    # existing call untouched by the breaker
    assert C.get_state(conn, "existing") == C.INITIATED


def test_case2_outage_at_provider_level():
    """force_outage makes every setup fail and trips the provider health check."""
    p = P.provider_a(seed=1)
    p.force_outage = True
    for i in range(6):
        p.place_call(f"c{i}", "555", now=0.0)
    p.poll_events(now=5.0)
    assert p.is_degraded(now=5.0) is True


def test_case3_sudden_agent_drop_is_immediate(conn):
    """100 agents, 40 disappear (heartbeat miss -> OFFLINE). Available count must
    reflect the drop immediately, without waiting for a sweeper cycle, and the
    controller's sudden-drop guard rejects the next cycle."""
    for i in range(100):
        A.create_agent(conn, f"a{i}")
    for i in range(40):
        A.go_offline(conn, f"a{i}")                          # active OFFLINE, not a timeout
    avail = conn.execute("SELECT COUNT(*) n FROM agents WHERE state=?",
                         (A.AVAILABLE,)).fetchone()["n"]
    assert avail == 60                                        # immediate, not delayed

    d = decide(20, _snap(prev_available=100, agents_available=60))
    assert d.action == "REJECT" and d.rule_triggered == "sudden_agent_drop"


def test_case4_duplicate_events_single_transition(conn):
    """Same provider event id delivered three times -> one transition, two logged."""
    B.add_borrower(conn, "b0", "555")
    C.create_call(conn, "c0", "b0")
    C.internal_transition(conn, "c0", C.QUEUED, C.RESERVED)
    C.internal_transition(conn, "c0", C.RESERVED, C.INITIATED)
    results = [C.apply_event(conn, "c0", "RINGING", "same-id") for _ in range(3)]
    assert results == ["applied", "duplicate", "duplicate"]
    assert C.get_state(conn, "c0") == C.RINGING
    dupes = conn.execute("SELECT COUNT(*) n FROM anomaly_log WHERE reason=?",
                        ("duplicate_event_id",)).fetchone()["n"]
    assert dupes == 2


def test_case5_out_of_order_terminal_never_reverses(conn):
    """COMPLETED arrives first (out of order); later ANSWERED/RINGING must not
    move the terminal call backwards."""
    B.add_borrower(conn, "b0", "555")
    C.create_call(conn, "c0", "b0")
    C.internal_transition(conn, "c0", C.QUEUED, C.RESERVED)
    C.internal_transition(conn, "c0", C.RESERVED, C.INITIATED)
    C.apply_event(conn, "c0", "RINGING", "e0")
    C.apply_event(conn, "c0", "ANSWERED", "e1")
    C.internal_transition(conn, "c0", C.ANSWERED, C.CONNECTED)
    assert C.apply_event(conn, "c0", "COMPLETED", "e2") == "applied"
    assert C.apply_event(conn, "c0", "ANSWERED", "e3") == "rejected"
    assert C.apply_event(conn, "c0", "RINGING", "e4") == "rejected"
    assert C.get_state(conn, "c0") == C.COMPLETED
