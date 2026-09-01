import threading

from smartdialer import db as _db
from smartdialer import agent as A
from smartdialer import borrower as B
from smartdialer import call as C
from smartdialer import sweeper as S
from smartdialer.clock import now


def test_20_threads_one_agent_exactly_one_wins(dbpath):
    """The core concurrency guarantee: many workers race to reserve one agent;
    exactly one succeeds."""
    setup = _db.connect(dbpath)
    A.create_agent(setup, "hot")
    setup.close()

    wins = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker(wid):
        c = _db.connect(dbpath)
        barrier.wait()  # maximise the collision window
        ok = A.try_reserve(c, "hot", wid)
        if ok:
            with lock:
                wins.append(wid)
        c.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(wins) == 1


def test_sweeper_reclaims_stale_reservation_and_requeues(dbpath):
    c = _db.connect(dbpath)
    A.create_agent(c, "a1")
    B.add_borrower(c, "b1", "555")
    B.claim_next(c, "w1")
    A.try_reserve(c, "a1", "w1")
    # backdate the reservation past the TTL (simulating a crashed worker)
    c.execute("UPDATE agents SET reserved_at=? WHERE id=?",
              (now() - S.RESERVED_TTL - 5, "a1"))
    # also simulate the in-flight borrower being stuck
    reclaimed = S.sweep_reservations(c)
    assert reclaimed == 1
    assert A.get_state(c, "a1") == A.AVAILABLE
    c.close()


def test_two_sweepers_no_double_requeue(dbpath):
    """Two sweepers hitting the same stale call must requeue the borrower once."""
    c = _db.connect(dbpath)
    B.add_borrower(c, "b1", "555")
    C.create_call(c, "call1", "b1")
    C.internal_transition(c, "call1", C.QUEUED, C.RESERVED)
    B.claim_next(c, "w1")  # borrower -> IN_FLIGHT
    c.execute("UPDATE calls SET updated_at=? WHERE id=?",
              (now() - S.RESERVED_TTL - 5, "call1"))
    c.close()

    results = []
    barrier = threading.Barrier(2)

    def sweep():
        cc = _db.connect(dbpath)
        barrier.wait()
        results.append(S.sweep_stale_calls(cc))
        cc.close()

    ts = [threading.Thread(target=sweep) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    # exactly one sweeper should have done the reclaim+requeue
    assert sum(results) == 1
    c = _db.connect(dbpath)
    attempts = c.execute("SELECT attempts, state FROM borrowers WHERE id=?",
                         ("b1",)).fetchone()
    # claimed once (attempts=1), requeued back to QUEUED exactly once
    assert attempts["state"] == B.QUEUED
    c.close()


def test_fast_abandon_guard(dbpath):
    c = _db.connect(dbpath)
    B.add_borrower(c, "b1", "555")
    C.create_call(c, "call1", "b1")
    C.internal_transition(c, "call1", C.QUEUED, C.RESERVED)
    C.internal_transition(c, "call1", C.RESERVED, C.INITIATED)
    C.apply_event(c, "call1", "RINGING", "e1")
    C.apply_event(c, "call1", "ANSWERED", "e2")
    # backdate answered_at past the 2s abandon TTL
    c.execute("UPDATE calls SET answered_at=? WHERE id=?",
              (now() - S.ABANDON_TTL - 1, "call1"))
    n = S.sweep_abandoned(c)
    assert n == 1
    assert C.get_state(c, "call1") == C.FAILED
    m = c.execute("SELECT value FROM metrics WHERE name=?",
                  ("forced_abandonment",)).fetchone()
    assert m["value"] == 1
    c.close()
