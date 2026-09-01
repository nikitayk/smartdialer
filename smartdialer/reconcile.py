"""When an agent goes OFFLINE (heartbeat miss, disconnect, or a simulated drop),
the state flip is the easy part — the side effect is the real work, and it
differs by what the agent was doing. This centralises that so the heartbeat
handler and the simulator's drop injector share one correct implementation
instead of each reimplementing it.

The CONNECTED case is the compliance one the whole assignment is about: an agent
vanishing mid-call means a live human is suddenly talking to no one, so the call
is force-failed and counted as forced_abandonment, and the borrower becomes a
callback candidate rather than a plain requeue (they already had a conversation).
"""

from .clock import now
from . import agent as A
from . import borrower as B
from . import call as C


def _bump(conn, name, delta=1.0):
    conn.execute(
        "INSERT INTO metrics (name, value) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value = value + ?",
        (name, delta, delta),
    )


def _active_call_for_agent(conn, agent_id):
    return conn.execute(
        "SELECT id, borrower_id, state FROM calls WHERE agent_id=? "
        "AND state NOT IN (?,?,?) ORDER BY created_at DESC LIMIT 1",
        (agent_id, C.COMPLETED, C.FAILED, C.CANCELLED),
    ).fetchone()


def agent_offline(conn, agent_id: str) -> str:
    """Take an agent OFFLINE and apply the side effect its previous state demands.
    Returns the tag from agent.go_offline for observability."""
    tag = A.go_offline(conn, agent_id)
    call = _active_call_for_agent(conn, agent_id)

    if tag == "release_reservation":
        # reserved, not yet a live conversation -> cancel + requeue borrower
        if call:
            C.force_fail(conn, call["id"])
            B.requeue(conn, call["borrower_id"])

    elif tag == "cancel_dial":
        # dial in progress, nobody connected yet -> fail call, requeue borrower
        if call:
            C.force_fail(conn, call["id"])
            B.requeue(conn, call["borrower_id"])

    elif tag == "forced_abandonment":
        # live connected call abandoned -> the compliance case
        if call:
            C.force_fail(conn, call["id"])
            _bump(conn, "forced_abandonment", 1)
            # borrower is a callback candidate, not a plain requeue

    return tag
