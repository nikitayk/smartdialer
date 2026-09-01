"""The sweeper is the one reconciliation mechanism for both crashes and
timeouts — there is no separate crash-recovery code path. It runs on every
live worker; that's fine because every reclaim is a guarded CAS, so if two
sweepers see the same stale row, exactly one wins the UPDATE and only that one
does the follow-up (requeue / fail). No double requeue, no double call.

Two windows, deliberately different:

  RESERVED_TTL (30s) — a reserved-but-not-dialed agent or a stalled call is a
  resource leak. 30s bounded staleness is fine; nobody is on the line yet.

  ABANDON_TTL (2s)  — a call sitting in ANSWERED (a real human has picked up)
  but not yet CONNECTED is the compliance case, not a mere leak. The generic
  30s sweep is far too slow for it, so it gets its own tight guard that fails
  the call fast and counts it as forced_abandonment. This is the gap I called
  out against the decision log: the log reconciled this case for *state*
  consistency but left it on the 30s path, which blows the abandonment SLA.
"""

from .clock import now
from . import agent as A
from . import borrower as B
from . import call as C

RESERVED_TTL = 30.0
ABANDON_TTL = 2.0


def _bump_metric(conn, name, delta=1.0):
    conn.execute(
        "INSERT INTO metrics (name, value) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value = value + ?",
        (name, delta, delta),
    )


def sweep_reservations(conn) -> int:
    """Reclaim agents stuck in RESERVED past the TTL. Returns count reclaimed."""
    cutoff = now() - RESERVED_TTL
    rows = conn.execute(
        "SELECT id FROM agents WHERE state=? AND reserved_at < ?",
        (A.RESERVED, cutoff),
    ).fetchall()
    reclaimed = 0
    for r in rows:
        cur = conn.execute(
            "UPDATE agents SET state=?, reserved_by=NULL, reserved_at=NULL, "
            "updated_at=? WHERE id=? AND state=? AND reserved_at < ?",
            (A.AVAILABLE, now(), r["id"], A.RESERVED, cutoff),
        )
        if cur.rowcount == 1:            # only the winner does follow-up
            reclaimed += 1
    return reclaimed


def sweep_stale_calls(conn) -> int:
    """Fail calls that have made no progress within the TTL (worker crash
    mid-dial, provider went silent). Guarded so two sweepers don't both act."""
    cutoff = now() - RESERVED_TTL
    rows = conn.execute(
        "SELECT id, borrower_id FROM calls WHERE state IN (?,?,?) AND updated_at < ?",
        (C.RESERVED, C.INITIATED, C.RINGING, cutoff),
    ).fetchall()
    swept = 0
    for r in rows:
        cur = conn.execute(
            "UPDATE calls SET state=?, updated_at=? WHERE id=? AND state IN (?,?,?) "
            "AND updated_at < ?",
            (C.FAILED, now(), r["id"], C.RESERVED, C.INITIATED, C.RINGING, cutoff),
        )
        if cur.rowcount == 1:
            B.requeue(conn, r["borrower_id"])
            swept += 1
    return swept


def sweep_abandoned(conn) -> int:
    """Fast path: a call ANSWERED but not CONNECTED within ABANDON_TTL means a
    live human with no agent. Fail it immediately and count the compliance hit.
    Returns count. Intended to run on a much tighter cadence than the 30s sweep."""
    cutoff = now() - ABANDON_TTL
    rows = conn.execute(
        "SELECT id, borrower_id FROM calls WHERE state=? AND answered_at < ?",
        (C.ANSWERED, cutoff),
    ).fetchall()
    n = 0
    for r in rows:
        cur = conn.execute(
            "UPDATE calls SET state=?, updated_at=? WHERE id=? AND state=? "
            "AND answered_at < ?",
            (C.FAILED, now(), r["id"], C.ANSWERED, cutoff),
        )
        if cur.rowcount == 1:
            _bump_metric(conn, "forced_abandonment", 1)
            n += 1
    return n
