from smartdialer import agent as A
from smartdialer import db as _db


def test_valid_transitions(conn):
    A.create_agent(conn, "a1")
    assert A.try_reserve(conn, "a1", "w1") is True
    assert A.get_state(conn, "a1") == A.RESERVED
    assert A.transition(conn, "a1", A.RESERVED, A.DIALING) is True
    assert A.transition(conn, "a1", A.DIALING, A.CONNECTED) is True
    assert A.transition(conn, "a1", A.CONNECTED, A.WRAP_UP) is True
    assert A.transition(conn, "a1", A.WRAP_UP, A.AVAILABLE) is True
    assert A.get_state(conn, "a1") == A.AVAILABLE


def test_reserve_only_from_available(conn):
    A.create_agent(conn, "a1")
    assert A.try_reserve(conn, "a1", "w1") is True
    # second reserve on an already-reserved agent must fail
    assert A.try_reserve(conn, "a1", "w2") is False


def test_illegal_transition_raises(conn):
    A.create_agent(conn, "a1")
    raised = False
    try:
        A.transition(conn, "a1", A.AVAILABLE, A.CONNECTED)
    except ValueError:
        raised = True
    assert raised, "illegal transition should raise ValueError"


def test_reserve_clears_on_release(conn):
    A.create_agent(conn, "a1")
    A.try_reserve(conn, "a1", "w1")
    A.transition(conn, "a1", A.RESERVED, A.AVAILABLE)
    row = conn.execute("SELECT reserved_by, reserved_at FROM agents WHERE id=?",
                       ("a1",)).fetchone()
    assert row["reserved_by"] is None and row["reserved_at"] is None


def test_offline_wildcard_side_effects(conn):
    cases = {
        A.AVAILABLE: "no_resource_held",
        A.PAUSED: "no_resource_held",
        A.WRAP_UP: "no_resource_held",
        A.RESERVED: "release_reservation",
        A.DIALING: "cancel_dial",
        A.CONNECTED: "forced_abandonment",
    }
    for i, (state, expected) in enumerate(cases.items()):
        aid = f"a{i}"
        A.create_agent(conn, aid, state=state)
        assert A.go_offline(conn, aid) == expected
        assert A.get_state(conn, aid) == A.OFFLINE


def test_offline_then_reconnect(conn):
    A.create_agent(conn, "a1", state=A.AVAILABLE)
    A.go_offline(conn, "a1")
    assert A.reconnect(conn, "a1") is True
    assert A.get_state(conn, "a1") == A.AVAILABLE
