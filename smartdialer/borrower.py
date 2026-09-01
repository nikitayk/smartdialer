"""Borrower queue.

'Which borrower to call' gets the same atomic-claim treatment as agents, so
two workers can't grab the same borrower. The claim is one guarded UPDATE:
QUEUED -> IN_FLIGHT, conditional on still being QUEUED. Requeue on failure is
also a guarded UPDATE, which is what makes the sweeper safe (see sweeper.py).
"""

from .clock import now

QUEUED = "QUEUED"
IN_FLIGHT = "IN_FLIGHT"
DONE = "DONE"


def add_borrower(conn, borrower_id: str, phone: str) -> None:
    conn.execute(
        "INSERT INTO borrowers (id, phone, state, updated_at) VALUES (?, ?, ?, ?)",
        (borrower_id, phone, QUEUED, now()),
    )


def claim_next(conn, worker_id: str):
    """Claim one QUEUED borrower atomically. Returns its id, or None.

    Two-step but still race-free: we read a candidate id, then CAS-claim it.
    If another worker claimed it between the read and the UPDATE, our UPDATE
    matches 0 rows and we simply try the next candidate. We never assume the
    read is still valid — the UPDATE's WHERE state='QUEUED' is the real guard.
    """
    while True:
        row = conn.execute(
            "SELECT id FROM borrowers WHERE state=? ORDER BY updated_at LIMIT 1",
            (QUEUED,),
        ).fetchone()
        if row is None:
            return None
        bid = row["id"]
        cur = conn.execute(
            "UPDATE borrowers SET state=?, locked_by=?, locked_at=?, "
            "attempts=attempts+1, updated_at=? WHERE id=? AND state=?",
            (IN_FLIGHT, worker_id, now(), now(), bid, QUEUED),
        )
        if cur.rowcount == 1:
            return bid
        # lost the race for this one; loop and pick another


def requeue(conn, borrower_id: str) -> bool:
    """IN_FLIGHT -> QUEUED, guarded. Only the worker/sweeper that actually
    flips it (rowcount==1) is responsible for the follow-up, so a borrower is
    never requeued twice even if two sweepers see the same stale row."""
    cur = conn.execute(
        "UPDATE borrowers SET state=?, locked_by=NULL, locked_at=NULL, updated_at=? "
        "WHERE id=? AND state=?",
        (QUEUED, now(), borrower_id, IN_FLIGHT),
    )
    return cur.rowcount == 1


def mark_done(conn, borrower_id: str) -> None:
    conn.execute(
        "UPDATE borrowers SET state=?, updated_at=? WHERE id=?",
        (DONE, now(), borrower_id),
    )
