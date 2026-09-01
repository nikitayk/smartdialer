"""A snapshot is a read-only view of system state at one instant. The pacing
engine and the safety controller both reason over a snapshot rather than
poking at the DB directly, which keeps their logic pure and unit-testable:
a test can hand-build a Snapshot and assert exactly what they decide.
"""

from dataclasses import dataclass

from .clock import now
from . import agent as A
from . import call as C


@dataclass
class Snapshot:
    agents_available: int
    prev_available: int
    calls_dialing: int
    calls_ringing: int
    calls_connected: int
    calls_in_flight: int          # reserved+initiated+ringing+answered (not connected/terminal)
    anomaly_count_5min: int
    provider_degraded: bool
    predicted_answer_rate: float
    agents_expected_free_soon: int


def _count_agents(conn, state):
    return conn.execute("SELECT COUNT(*) n FROM agents WHERE state=?", (state,)).fetchone()["n"]


def _count_calls(conn, states):
    q = ",".join("?" * len(states))
    return conn.execute(f"SELECT COUNT(*) n FROM calls WHERE state IN ({q})", states).fetchone()["n"]


def build_snapshot(conn, provider, prev_available, *,
                   predicted_answer_rate=0.4, agents_expected_free_soon=0,
                   now_ts=None):
    t = now() if now_ts is None else now_ts
    anomalies = conn.execute(
        "SELECT COUNT(*) n FROM anomaly_log WHERE created_at > ?", (t - 300,)
    ).fetchone()["n"]
    return Snapshot(
        agents_available=_count_agents(conn, A.AVAILABLE),
        prev_available=prev_available,
        calls_dialing=_count_calls(conn, [C.INITIATED]),
        calls_ringing=_count_calls(conn, [C.RINGING]),
        calls_connected=_count_calls(conn, [C.CONNECTED]),
        calls_in_flight=_count_calls(conn, [C.RESERVED, C.INITIATED, C.RINGING, C.ANSWERED]),
        anomaly_count_5min=anomalies,
        provider_degraded=provider.is_degraded(now=t) if provider else False,
        predicted_answer_rate=predicted_answer_rate,
        agents_expected_free_soon=agents_expected_free_soon,
    )
