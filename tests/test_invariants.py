"""Direct property tests for the safety invariants documented in
docs/ARCHITECTURE.md. These are additive: they pin invariants that the rest of
the suite exercises only indirectly, so a future change that quietly breaks one
fails here with a clear name.

Numbered comments map to the "Safety Invariants" section of the architecture doc.
"""

import threading

from smartdialer import db as _db
from smartdialer import agent as A
from smartdialer import borrower as B
from smartdialer import call as C
from smartdialer import providers as P
from smartdialer.allocator import CallAllocator
from smartdialer.safety import decide, MAX_OVERDIAL_RATIO
from smartdialer.pacing import PredictivePacer
from smartdialer.snapshot import Snapshot


def _snap(**kw):
    base = dict(
        agents_available=10, prev_available=10, calls_dialing=0, calls_ringing=0,
        calls_connected=0, calls_in_flight=0, anomaly_count_5min=0,
        provider_degraded=False, predicted_answer_rate=0.4,
        agents_expected_free_soon=0, observed_attempts=10 ** 9,
    )
    base.update(kw)
    return Snapshot(**base)


# --- Invariant 2: one borrower cannot have multiple active claims -----------

def test_two_workers_one_borrower_exactly_one_claims(dbpath):
    """Many workers race to claim a single QUEUED borrower; exactly one wins."""
    setup = _db.connect(dbpath)
    B.add_borrower(setup, "only", "555")
    setup.close()

    claimers = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker(wid):
        c = _db.connect(dbpath)
        barrier.wait()
        bid = B.claim_next(c, wid)
        if bid is not None:
            with lock:
                claimers.append((wid, bid))
        c.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimers) == 1, f"borrower claimed by {len(claimers)} workers"


# --- Invariant 8 + out-of-order: events cannot move a call backward ----------

def test_answered_before_ringing_is_rejected_not_applied(conn):
    """A provider that emits ANSWERED before RINGING must NOT make the call jump
    illegally to ANSWERED. The out-of-order event is rejected and logged; the
    call stays INITIATED (recoverable by the stale-call sweep). This is the
    safety half of out-of-order handling: no backward/forward illegal jump."""
    B.add_borrower(conn, "b0", "555")
    C.create_call(conn, "c0", "b0")
    C.internal_transition(conn, "c0", C.QUEUED, C.RESERVED)
    C.internal_transition(conn, "c0", C.RESERVED, C.INITIATED)

    assert C.apply_event(conn, "c0", "ANSWERED", "eA") == "rejected"
    assert C.get_state(conn, "c0") == C.INITIATED           # no illegal jump
    r = conn.execute("SELECT reason FROM anomaly_log ORDER BY id DESC LIMIT 1").fetchone()
    assert r["reason"] == "out_of_order_or_wrong_state"


# --- Invariant 3: terminal calls cannot transition back ----------------------

def test_no_terminal_state_can_be_left(conn):
    """From each terminal state, every provider event is rejected and the state
    is unchanged."""
    for i, terminal in enumerate((C.COMPLETED, C.FAILED, C.CANCELLED)):
        cid = f"c{i}"
        B.add_borrower(conn, f"b{i}", "555")
        C.create_call(conn, cid, f"b{i}")
        conn.execute("UPDATE calls SET state=? WHERE id=?", (terminal, cid))
        for ev in ("RINGING", "ANSWERED", "COMPLETED", "FAILED"):
            assert C.apply_event(conn, cid, ev, f"{cid}-{ev}") == "rejected"
        assert C.get_state(conn, cid) == terminal


# --- Invariant 10: predictive pacing cannot expand the safety envelope -------

def test_predictive_never_exceeds_overdial_cap_across_rates():
    """Sweep the predicted answer rate from near-zero upward. The raw predictive
    proposal can be enormous when the rate is low, but the approved count can
    never push dialing+ringing past the hard overdial cap, in ANY safety branch
    (including the anomaly-spike branch, which is the one this pins)."""
    pacer = PredictivePacer()
    for rate_milli in range(20, 500, 10):          # 0.02 .. 0.49
        rate = rate_milli / 1000.0
        for dialing in (0, 4, 8):
            for anomalies in (0, 25):              # 25 -> anomaly-spike branch
                snap = _snap(agents_available=10, agents_expected_free_soon=6,
                             predicted_answer_rate=rate, calls_in_flight=dialing,
                             calls_dialing=dialing, calls_ringing=0,
                             anomaly_count_5min=anomalies)
                proposed = pacer.propose(snap)
                d = decide(proposed, snap)
                cap = int(snap.agents_available * MAX_OVERDIAL_RATIO)
                in_flight_after = snap.calls_dialing + snap.calls_ringing + d.approved_n
                assert in_flight_after <= cap, (
                    f"envelope breached: rate={rate} anomalies={anomalies} "
                    f"proposed={proposed} approved={d.approved_n} cap={cap}")


def test_anomaly_spike_clamped_to_cap_regression():
    """Regression: a huge predictive proposal during an anomaly spike is clamped
    to the overdial cap, not merely halved. avail=10 -> cap=15; nothing in setup
    -> at most 15 approved even though half of the proposal is far larger."""
    snap = _snap(agents_available=10, agents_expected_free_soon=10,
                 predicted_answer_rate=0.02, calls_in_flight=0,
                 calls_dialing=0, calls_ringing=0, anomaly_count_5min=50)
    proposed = PredictivePacer().propose(snap)
    assert proposed // 2 > 15                       # halving alone would breach
    d = decide(proposed, snap)
    assert d.rule_triggered == "anomaly_spike"
    assert d.approved_n == 15                       # clamped to cap-current


# --- Predictive binding bound: answered calls bind at most `agents` agents ---

def test_predictive_binds_at_most_free_agents_rest_abandon(dbpath):
    """5 predictive calls, only 2 agents. All answer. Exactly 2 bridge to an
    agent; the other 3 are forced abandonments. No agent is bound to two calls."""
    c = _db.connect(dbpath)
    for i in range(2):
        A.create_agent(c, f"a{i}")
    for i in range(5):
        B.add_borrower(c, f"b{i}", "555")
    alloc = CallAllocator(c, P.provider_a(seed=3))
    placed = alloc.place(5, reserve_agents=False)   # predictive: no agents held
    assert placed == 5

    calls = [r["id"] for r in c.execute("SELECT id FROM calls ORDER BY created_at")]
    for cid in calls:
        C.internal_transition(c, cid, C.INITIATED, C.RINGING)
        C.internal_transition(c, cid, C.RINGING, C.ANSWERED)

    outcomes = [alloc.bridge_answered(cid) for cid in calls]
    assert outcomes.count("bridged") == 2
    assert outcomes.count("abandoned") == 3

    bound = c.execute(
        "SELECT agent_id, COUNT(*) n FROM calls WHERE state=? GROUP BY agent_id",
        (C.CONNECTED,)).fetchall()
    for row in bound:
        assert row["n"] == 1, "an agent was bound to more than one connected call"

    m = c.execute("SELECT value FROM metrics WHERE name=?",
                  ("forced_abandonment",)).fetchone()
    assert m["value"] == 3
    c.close()
