"""Multi-worker stress: many workers hammering one shared SQLite DB. The
guarantees under test are the ones that actually matter for correctness — no
agent is double-booked, no borrower is double-called — under real thread
contention, not a mock."""

import threading

from smartdialer import db as _db, agent as A, borrower as B
from smartdialer import providers as P
from smartdialer.allocator import CallAllocator


def test_multi_worker_no_double_booking(dbpath):
    N_AGENTS, M_BORROWERS, W_WORKERS = 20, 100, 8

    setup = _db.connect(dbpath)
    for i in range(N_AGENTS):
        A.create_agent(setup, f"a{i}")
    for i in range(M_BORROWERS):
        B.add_borrower(setup, f"b{i}", "555")
    setup.close()

    barrier = threading.Barrier(W_WORKERS)

    def worker(wid):
        c = _db.connect(dbpath)
        alloc = CallAllocator(c, P.provider_a(seed=wid), worker_id=f"w{wid}")
        barrier.wait()
        # keep grabbing agent+borrower pairs until none can be placed
        while alloc.place(1, reserve_agents=True) == 1:
            pass
        c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(W_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    c = _db.connect(dbpath)
    calls = c.execute("SELECT agent_id, borrower_id FROM calls").fetchall()

    # progressive: exactly N calls (one per agent), since agents < borrowers
    assert len(calls) == N_AGENTS

    agent_ids = [r["agent_id"] for r in calls]
    borrower_ids = [r["borrower_id"] for r in calls]
    assert len(set(agent_ids)) == len(agent_ids), "an agent was double-booked"
    assert len(set(borrower_ids)) == len(borrower_ids), "a borrower was double-called"

    # every agent ended up committed to a call (DIALING), none left dangling wrong
    dialing = c.execute("SELECT COUNT(*) n FROM agents WHERE state=?",
                       (A.DIALING,)).fetchone()["n"]
    assert dialing == N_AGENTS

    # borrowers: N consumed, the rest still queued
    queued = c.execute("SELECT COUNT(*) n FROM borrowers WHERE state=?",
                      (B.QUEUED,)).fetchone()["n"]
    assert queued == M_BORROWERS - N_AGENTS
    c.close()
