"""Agent lifecycle.

Every state change is a compare-and-swap: UPDATE ... WHERE state = <expected>.
We never SELECT the state and then UPDATE it in a separate statement — that
read-then-write gap is exactly where two workers would race. A single guarded
UPDATE is atomic inside SQLite, so the row can only move if it is still in the
state we expected. rowcount tells us whether we won.
"""

from .clock import now
from . import db as _db

# 7 states from the decision log.
OFFLINE = "OFFLINE"
AVAILABLE = "AVAILABLE"
RESERVED = "RESERVED"
DIALING = "DIALING"
CONNECTED = "CONNECTED"
WRAP_UP = "WRAP_UP"
PAUSED = "PAUSED"

STATES = {OFFLINE, AVAILABLE, RESERVED, DIALING, CONNECTED, WRAP_UP, PAUSED}

# Allowed (from -> to) transitions, excluding the OFFLINE wildcard which is
# handled separately (any state -> OFFLINE, and OFFLINE -> AVAILABLE).
ALLOWED = {
    (AVAILABLE, RESERVED),
    (RESERVED, DIALING),
    (RESERVED, AVAILABLE),   # reservation released (sweeper / dial cancelled)
    (DIALING, CONNECTED),
    (DIALING, AVAILABLE),    # dial failed
    (CONNECTED, WRAP_UP),
    (WRAP_UP, AVAILABLE),
    (AVAILABLE, PAUSED),
    (PAUSED, AVAILABLE),
    (OFFLINE, AVAILABLE),
}


def create_agent(conn, agent_id: str, state: str = AVAILABLE) -> None:
    conn.execute(
        "INSERT INTO agents (id, state, updated_at) VALUES (?, ?, ?)",
        (agent_id, state, now()),
    )


def get_state(conn, agent_id: str):
    row = conn.execute("SELECT state FROM agents WHERE id=?", (agent_id,)).fetchone()
    return row["state"] if row else None


def try_reserve(conn, agent_id: str, worker_id: str) -> bool:
    """Atomically move AVAILABLE -> RESERVED. Returns True iff this worker won.

    This is the whole concurrency story for agent allocation. Two workers can
    run this same statement on the same row at the same instant; SQLite
    serializes the two UPDATEs, the first flips the state, and the second's
    WHERE state='AVAILABLE' no longer matches -> 0 rows -> that worker learns
    it lost, from this same statement, with no extra read.
    """
    cur = conn.execute(
        "UPDATE agents SET state=?, reserved_by=?, reserved_at=?, updated_at=? "
        "WHERE id=? AND state=?",
        (RESERVED, worker_id, now(), now(), agent_id, AVAILABLE),
    )
    return cur.rowcount == 1


def transition(conn, agent_id: str, expected_from: str, to: str) -> bool:
    """Guarded CAS transition for the non-reserve edges. Returns True if applied."""
    if (expected_from, to) not in ALLOWED:
        raise ValueError(f"illegal agent transition {expected_from}->{to}")
    # Clear the reservation fields when the agent lands back on AVAILABLE.
    if to == AVAILABLE:
        cur = conn.execute(
            "UPDATE agents SET state=?, reserved_by=NULL, reserved_at=NULL, updated_at=? "
            "WHERE id=? AND state=?",
            (to, now(), agent_id, expected_from),
        )
    else:
        cur = conn.execute(
            "UPDATE agents SET state=?, updated_at=? WHERE id=? AND state=?",
            (to, now(), agent_id, expected_from),
        )
    return cur.rowcount == 1


def go_offline(conn, agent_id: str, worker_id: str = "system") -> str:
    """OFFLINE is a wildcard: reachable from ANY non-offline state.

    Returns a tag describing the side-effect that the caller must honour.
    The five distinct outcomes are the actual work here — just flipping the
    state would leave dangling reservations or, worse, a live abandoned call.
    """
    row = conn.execute("SELECT state FROM agents WHERE id=?", (agent_id,)).fetchone()
    if row is None:
        return "unknown"
    frm = row["state"]
    if frm == OFFLINE:
        return "already_offline"

    # Atomic flip guarded on the observed state; if it changed underneath us,
    # someone else transitioned first and we do nothing.
    cur = conn.execute(
        "UPDATE agents SET state=?, reserved_by=NULL, reserved_at=NULL, updated_at=? "
        "WHERE id=? AND state=?",
        (OFFLINE, now(), agent_id, frm),
    )
    if cur.rowcount != 1:
        return "raced"

    if frm in (AVAILABLE, PAUSED, WRAP_UP):
        return "no_resource_held"     # nothing was held; clean drop
    if frm == RESERVED:
        return "release_reservation"  # caller requeues borrower immediately
    if frm == DIALING:
        return "cancel_dial"          # caller cancels setup, call -> FAILED
    if frm == CONNECTED:
        return "forced_abandonment"   # THE compliance case; caller flags it
    return "no_resource_held"


def reconnect(conn, agent_id: str) -> bool:
    return transition(conn, agent_id, OFFLINE, AVAILABLE)


def release_to_available(conn, agent_id: str) -> bool:
    """Release an agent that's holding a call which just got reclaimed. The
    agent may be in RESERVED (reserved, not yet dialing) or DIALING (dial sent,
    call now failed). Guarded so we only move it from those two states."""
    if agent_id is None:
        return False
    for frm in (RESERVED, DIALING):
        cur = conn.execute(
            "UPDATE agents SET state=?, reserved_by=NULL, reserved_at=NULL, "
            "updated_at=? WHERE id=? AND state=?",
            (AVAILABLE, now(), agent_id, frm),
        )
        if cur.rowcount == 1:
            return True
    return False
