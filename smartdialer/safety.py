"""The Safety Controller sits between pacing and the telecom provider. Two
properties matter:

  1. It is the ONLY object that holds a reference to the Call Allocator. The
     pacing engine is just a function of a snapshot -> int; it has no handle to
     the allocator and therefore physically cannot place a call or turn safety
     off. This is enforced by wiring, not by a rule someone could forget.

  2. Its rules are ordered and first-match-wins, so every decision is
     deterministic and explainable. Each decision is written to
     safety_decisions, which is the direct answer to "why did you initiate N
     calls and not M?" — you read the row.
"""

from typing import NamedTuple

from .clock import now

MAX_OVERDIAL_RATIO = 1.5      # named, tunable; not calibrated from data in V1
SUDDEN_DROP_RATIO = 0.25      # >25% drop in available agents vs last cycle
ANOMALY_WINDOW_LIMIT = 20     # anomalies in the last 5 min


class SafetyDecision(NamedTuple):
    action: str            # APPROVE | REDUCE | REJECT | FALLBACK_PROGRESSIVE
    approved_n: int
    rule_triggered: str
    reason: str


def decide(proposed: int, snap) -> SafetyDecision:
    """Pure decision function — no side effects, trivially unit-testable."""
    avail = snap.agents_available

    # 1. Circuit breaker: provider degraded -> collapse to one dial per agent.
    if snap.provider_degraded:
        n = max(min(proposed, avail), 0)
        return SafetyDecision("FALLBACK_PROGRESSIVE", n, "circuit_breaker",
                              "provider degraded; falling back to one dial per free agent")

    # 2. Sudden agent-drop guard: let the system stabilise for a cycle.
    if snap.prev_available > 0 and avail < snap.prev_available * (1 - SUDDEN_DROP_RATIO):
        return SafetyDecision("REJECT", 0, "sudden_agent_drop",
                              f"available dropped {snap.prev_available}->{avail} (>25%)")

    # 3. Anomaly spike guard: something's off (dupes/out-of-order rising) -> half.
    if snap.anomaly_count_5min > ANOMALY_WINDOW_LIMIT:
        return SafetyDecision("REDUCE", max(proposed // 2, 0), "anomaly_spike",
                              f"{snap.anomaly_count_5min} anomalies in 5m; approving 50%")

    # 4. Hard overdial cap: never let dialing+ringing exceed avail * ratio.
    current = snap.calls_dialing + snap.calls_ringing
    cap = int(avail * MAX_OVERDIAL_RATIO)
    if current + proposed > cap:
        allowed = max(cap - current, 0)
        if allowed < proposed:
            return SafetyDecision("REDUCE", allowed, "hard_overdial_cap",
                                  f"cap={cap}, in-flight={current}; approving {allowed}")

    # 5. Nothing to do.
    if proposed <= 0:
        return SafetyDecision("REJECT", 0, "nothing_proposed", "pacer proposed <= 0")

    # 6. Default.
    return SafetyDecision("APPROVE", proposed, "default_approve", "no guard triggered")


class SafetyController:
    def __init__(self, conn, allocator):
        self._conn = conn
        self._allocator = allocator    # the ONLY reference to the allocator

    def _log(self, proposed, d: SafetyDecision):
        self._conn.execute(
            "INSERT INTO safety_decisions (proposed_n, action, approved_n, "
            "rule_triggered, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (proposed, d.action, d.approved_n, d.rule_triggered, d.reason, now()),
        )

    def admit(self, pacer, snap) -> SafetyDecision:
        """Run one pacing cycle. The pacer only proposes; we decide, log, and
        are the only code path that can actually place calls."""
        proposed = pacer.propose(snap)
        d = decide(proposed, snap)
        self._log(proposed, d)
        if d.approved_n > 0:
            reserve_agents = (
                getattr(pacer, "mode", "progressive") == "progressive"
                or d.action == "FALLBACK_PROGRESSIVE"
            )
            self._allocator.place(d.approved_n, reserve_agents=reserve_agents)
        return d
