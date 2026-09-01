from smartdialer import call as C
from smartdialer import borrower as B


def _setup_call(conn, cid="c1", up_to=None):
    B.add_borrower(conn, "b1", "555")
    C.create_call(conn, cid, "b1")
    # drive to RESERVED->INITIATED via internal transitions
    C.internal_transition(conn, cid, C.QUEUED, C.RESERVED)
    C.internal_transition(conn, cid, C.RESERVED, C.INITIATED)
    return cid


def test_happy_path(conn):
    cid = _setup_call(conn)
    assert C.apply_event(conn, cid, "RINGING", "e1") == "applied"
    assert C.apply_event(conn, cid, "ANSWERED", "e2") == "applied"
    assert C.internal_transition(conn, cid, C.ANSWERED, C.CONNECTED) is True
    assert C.apply_event(conn, cid, "COMPLETED", "e3") == "applied"
    assert C.get_state(conn, cid) == C.COMPLETED


def test_duplicate_event_id_is_idempotent(conn):
    cid = _setup_call(conn)
    assert C.apply_event(conn, cid, "RINGING", "dup") == "applied"
    # exact same event id again -> duplicate, no second transition
    assert C.apply_event(conn, cid, "RINGING", "dup") == "duplicate"
    assert C.get_state(conn, cid) == C.RINGING
    n = conn.execute("SELECT COUNT(*) n FROM anomaly_log WHERE reason=?",
                     ("duplicate_event_id",)).fetchone()["n"]
    assert n == 1


def test_answered_x3_then_completed(conn):
    """Assignment's literal example: ANSWERED, ANSWERED, ANSWERED, COMPLETED."""
    cid = _setup_call(conn)
    C.apply_event(conn, cid, "RINGING", "e1")
    assert C.apply_event(conn, cid, "ANSWERED", "e2") == "applied"
    # different event ids, same type: not duplicates, but wrong state now
    assert C.apply_event(conn, cid, "ANSWERED", "e3") == "rejected"
    assert C.apply_event(conn, cid, "ANSWERED", "e4") == "rejected"
    C.internal_transition(conn, cid, C.ANSWERED, C.CONNECTED)
    assert C.apply_event(conn, cid, "COMPLETED", "e5") == "applied"
    assert C.get_state(conn, cid) == C.COMPLETED


def test_completed_answered_ringing_out_of_order(conn):
    """Assignment's literal example: COMPLETED, ANSWERED, RINGING."""
    cid = _setup_call(conn)
    # drive to CONNECTED so COMPLETED can legitimately land first
    C.apply_event(conn, cid, "RINGING", "e0")
    C.apply_event(conn, cid, "ANSWERED", "e0b")
    C.internal_transition(conn, cid, C.ANSWERED, C.CONNECTED)
    assert C.apply_event(conn, cid, "COMPLETED", "e1") == "applied"
    # terminal now; later out-of-order events must not move it back
    assert C.apply_event(conn, cid, "ANSWERED", "e2") == "rejected"
    assert C.apply_event(conn, cid, "RINGING", "e3") == "rejected"
    assert C.get_state(conn, cid) == C.COMPLETED


def test_terminal_is_immutable(conn):
    cid = _setup_call(conn)
    C.force_fail(conn, cid)
    assert C.get_state(conn, cid) == C.FAILED
    assert C.apply_event(conn, cid, "RINGING", "z1") == "rejected"
    r = conn.execute("SELECT reason FROM anomaly_log ORDER BY id DESC LIMIT 1").fetchone()
    assert r["reason"] == "event_after_terminal"
