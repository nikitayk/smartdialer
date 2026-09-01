from smartdialer import providers as P
from smartdialer import db as _db
from smartdialer import agent as A
from smartdialer import borrower as B
from smartdialer import call as C
from smartdialer.allocator import CallAllocator


def _seq(provider, n_calls=30, horizon=60.0):
    """Place n calls at t=0 and drain all events up to horizon; return the
    ordered list of (call, type) for comparison."""
    for i in range(n_calls):
        provider.place_call(f"c{i}", "555", now=0.0)
    out = []
    t = 0.0
    while t <= horizon:
        for ev in provider.poll_events(now=t):
            out.append((ev.call_id, ev.event_type, ev.event_id))
        t += 0.5
    return out


def test_provider_is_deterministic_with_seed():
    a1 = _seq(P.provider_a(seed=7))
    a2 = _seq(P.provider_a(seed=7))
    assert a1 == a2                       # same seed -> identical stream


def test_providers_behave_differently():
    a = _seq(P.provider_a(seed=7), n_calls=60)
    b = _seq(P.provider_b(seed=7), n_calls=60)
    # B must produce duplicate event ids (dup_rate>0); A must not
    def has_dupes(seq):
        ids = [e[2] for e in seq]
        return len(ids) != len(set(ids))
    assert has_dupes(b)
    assert not has_dupes(a)


def test_force_outage_degrades_provider():
    p = P.provider_a(seed=1)
    p.force_outage = True
    for i in range(6):
        p.place_call(f"c{i}", "555", now=0.0)
    # all failures land shortly after; drain them
    p.poll_events(now=5.0)
    assert p.is_degraded(now=5.0) is True


def test_allocator_progressive_reserves_and_dials(dbpath):
    c = _db.connect(dbpath)
    for i in range(3):
        A.create_agent(c, f"a{i}")
    for i in range(5):
        B.add_borrower(c, f"b{i}", "555")
    alloc = CallAllocator(c, P.provider_a(seed=3))
    placed = alloc.place(5, reserve_agents=True)
    # only 3 agents -> progressive stops at 3 even though 5 requested
    assert placed == 3
    dialing = c.execute("SELECT COUNT(*) n FROM agents WHERE state=?",
                        (A.DIALING,)).fetchone()["n"]
    assert dialing == 3
    c.close()


def test_allocator_predictive_dials_without_agents(dbpath):
    c = _db.connect(dbpath)
    A.create_agent(c, "a0")               # only 1 agent
    for i in range(5):
        B.add_borrower(c, f"b{i}", "555")
    alloc = CallAllocator(c, P.provider_a(seed=3))
    placed = alloc.place(5, reserve_agents=False)
    assert placed == 5                    # dials all 5 despite 1 agent
    initiated = c.execute("SELECT COUNT(*) n FROM calls WHERE state=?",
                         (C.INITIATED,)).fetchone()["n"]
    assert initiated == 5
    c.close()


def test_bridge_answered_and_abandonment(dbpath):
    c = _db.connect(dbpath)
    A.create_agent(c, "a0")               # one agent for two answered calls
    for i in range(2):
        B.add_borrower(c, f"b{i}", "555")
    alloc = CallAllocator(c, P.provider_a(seed=3))
    alloc.place(2, reserve_agents=False)  # predictive: 2 calls, 0 agents held
    calls = [r["id"] for r in c.execute("SELECT id FROM calls ORDER BY created_at").fetchall()]
    for cid in calls:                     # drive both to ANSWERED
        C.internal_transition(c, cid, C.INITIATED, C.RINGING)
        C.internal_transition(c, cid, C.RINGING, C.ANSWERED)

    r1 = alloc.bridge_answered(calls[0])
    r2 = alloc.bridge_answered(calls[1])
    assert r1 == "bridged"                # first gets the only agent
    assert r2 == "abandoned"              # second has none -> forced abandonment
    m = c.execute("SELECT value FROM metrics WHERE name=?",
                  ("forced_abandonment",)).fetchone()
    assert m["value"] == 1
    c.close()
