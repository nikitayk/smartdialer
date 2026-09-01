"""The Call Allocator is the only component that talks to the provider. It turns
an approved count into real reservations and dials, using the same CAS discipline
as everything else.

Two placement modes:

  Progressive (reserve_agents=True): reserve an agent, THEN dial. The agent is
  held for the whole call, so we never dial without somewhere to land the call.

  Predictive (reserve_agents=False): dial WITHOUT reserving an agent, betting on
  the answer rate. The agent is grabbed when the call is answered (bridge_answered).
  If more calls answer than we have agents, the extra answered call has nowhere to
  go — that is the abandonment case, handled explicitly and counted, never ignored.
"""

import uuid

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


class CallAllocator:
    def __init__(self, conn, provider, worker_id="alloc"):
        self._conn = conn
        self._provider = provider
        self._worker_id = worker_id

    def _reserve_any_agent(self):
        row = self._conn.execute(
            "SELECT id FROM agents WHERE state=? LIMIT 1", (A.AVAILABLE,)
        ).fetchone()
        if row is None:
            return None
        return row["id"] if A.try_reserve(self._conn, row["id"], self._worker_id) else None

    def place(self, n: int, reserve_agents: bool = True) -> int:
        placed = 0
        for _ in range(n):
            agent_id = None
            if reserve_agents:
                agent_id = self._reserve_any_agent()
                if agent_id is None:
                    break   # progressive: no free agent -> stop, by design

            borrower_id = B.claim_next(self._conn, self._worker_id)
            if borrower_id is None:
                if agent_id:
                    A.transition(self._conn, agent_id, A.RESERVED, A.AVAILABLE)
                break

            phone = self._conn.execute(
                "SELECT phone FROM borrowers WHERE id=?", (borrower_id,)
            ).fetchone()["phone"]

            call_id = "call-" + uuid.uuid4().hex[:12]
            C.create_call(self._conn, call_id, borrower_id,
                          agent_id=agent_id, provider=self._provider.name)
            C.internal_transition(self._conn, call_id, C.QUEUED, C.RESERVED)
            C.internal_transition(self._conn, call_id, C.RESERVED, C.INITIATED)
            if agent_id:
                A.transition(self._conn, agent_id, A.RESERVED, A.DIALING)

            self._provider.place_call(call_id, phone, now=now())
            _bump(self._conn, "calls_initiated", 1)
            placed += 1
        return placed

    def bridge_answered(self, call_id: str) -> str:
        """Handle an ANSWERED call. Returns 'bridged' or 'abandoned'.

        Progressive: the agent is already bound and DIALING -> connect it.
        Predictive: grab a free agent now; if none, it's a forced abandonment.
        """
        row = self._conn.execute(
            "SELECT agent_id, borrower_id, state FROM calls WHERE id=?", (call_id,)
        ).fetchone()
        if row is None or row["state"] != C.ANSWERED:
            return "noop"

        agent_id = row["agent_id"]
        if agent_id is None:
            agent_id = self._reserve_any_agent()
            if agent_id is None:
                # No agent for a live answered call -> abandonment. Fail fast,
                # count it, and tell the provider to hang up safely.
                C.internal_transition(self._conn, call_id, C.ANSWERED, C.FAILED)
                _bump(self._conn, "forced_abandonment", 1)
                return "abandoned"
            self._conn.execute("UPDATE calls SET agent_id=? WHERE id=?", (agent_id, call_id))
            A.transition(self._conn, agent_id, A.RESERVED, A.DIALING)

        A.transition(self._conn, agent_id, A.DIALING, A.CONNECTED)
        C.internal_transition(self._conn, call_id, C.ANSWERED, C.CONNECTED)
        _bump(self._conn, "calls_connected", 1)
        return "bridged"
