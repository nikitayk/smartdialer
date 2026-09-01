"""Telecom provider abstraction.

The dialer only ever calls two methods — place_call() and poll_events() — so it
never depends on a provider's internals. Two mock providers implement the same
interface with deliberately different behaviour, seeded so runs are reproducible.

Providers also track their own recent-failure history and expose is_degraded().
For the single-process simulator this in-provider health is the source of truth;
in a real multi-worker deployment this counter would move into the DB so every
worker sees the same health. That trade-off is called out in the defense notes
rather than hidden.
"""

import random
from dataclasses import dataclass

from ..clock import now as _now


@dataclass
class ProviderEvent:
    call_id: str
    event_type: str      # RINGING | ANSWERED | COMPLETED | FAILED
    event_id: str        # globally unique per emission; dedup key downstream
    ts: float


# Health thresholds (decision log, failure case #2): the breaker trips on
# consecutive setup failures/timeouts, so a real outage trips it in a handful
# of calls while normal high-volume traffic with the occasional failure does
# not (any success resets the counter). Stale counters expire after the window.
DEGRADE_FAILS = 5
DEGRADE_WINDOW = 10.0


class MockProvider:
    def __init__(self, name, seed, answer_rate=0.4, *, setup_min, setup_max,
                 timeout_prob, timeout_latency, failure_rate, dup_rate, ooo_rate):
        self.name = name
        self.answer_rate = answer_rate
        self.force_outage = False
        self._rng = random.Random(seed)
        self._cfg = dict(setup_min=setup_min, setup_max=setup_max,
                         timeout_prob=timeout_prob, timeout_latency=timeout_latency,
                         failure_rate=failure_rate, dup_rate=dup_rate, ooo_rate=ooo_rate)
        self._pending = []          # scheduled (not yet matured) events, in emit order
        self._eid = 0
        self._consec_fails = 0
        self._last_fail_ts = None

    # -- helpers ----------------------------------------------------------
    def _next_eid(self):
        self._eid += 1
        return f"{self.name}-e{self._eid}"

    def _latency(self):
        c = self._cfg
        if self._rng.random() < c["timeout_prob"]:
            return c["timeout_latency"]
        return self._rng.uniform(c["setup_min"], c["setup_max"])

    def _emit(self, ev):
        self._pending.append(ev)
        # duplicate delivery: same event_id re-enqueued 1-2 more times
        if self._rng.random() < self._cfg["dup_rate"]:
            for _ in range(self._rng.randint(1, 2)):
                self._pending.append(ProviderEvent(ev.call_id, ev.event_type,
                                                   ev.event_id, ev.ts))

    def _record_setup_failure(self, ts):
        self._consec_fails += 1
        self._last_fail_ts = ts

    def _record_setup_success(self):
        self._consec_fails = 0

    # -- interface --------------------------------------------------------
    def place_call(self, call_id, number, now=None):
        t = _now() if now is None else now
        if self.force_outage or self._rng.random() < self._cfg["failure_rate"]:
            fail_ts = t + self._latency()
            self._emit(ProviderEvent(call_id, "FAILED", self._next_eid(), fail_ts))
            self._record_setup_failure(fail_ts)
            return

        # the call reached the network (a no-answer is still a healthy setup)
        self._record_setup_success()
        ring = ProviderEvent(call_id, "RINGING", self._next_eid(), t + self._latency() * 0.4)
        ans_ts = t + self._latency()
        if self._rng.random() < self.answer_rate:
            ans = ProviderEvent(call_id, "ANSWERED", self._next_eid(), ans_ts)
        else:
            ans = ProviderEvent(call_id, "FAILED", self._next_eid(), ans_ts)  # no answer

        # out-of-order: sometimes emit ANSWERED before RINGING
        if self._rng.random() < self._cfg["ooo_rate"]:
            self._emit(ans)
            self._emit(ring)
        else:
            self._emit(ring)
            self._emit(ans)

    def complete_call(self, call_id, now=None):
        """Called by the worker when talk time is up; provider confirms hangup."""
        t = _now() if now is None else now
        self._emit(ProviderEvent(call_id, "COMPLETED", self._next_eid(), t))

    def poll_events(self, now=None):
        """Return matured events and drop them from the queue. Returned in emit
        order (NOT re-sorted by ts), so out-of-order stays out-of-order — the
        ingestion layer is what makes that safe, not the provider."""
        t = _now() if now is None else now
        matured, keep = [], []
        for ev in self._pending:
            (matured if ev.ts <= t else keep).append(ev)
        self._pending = keep
        return matured

    def is_degraded(self, now=None):
        t = _now() if now is None else now
        if self._last_fail_ts is not None and t - self._last_fail_ts > DEGRADE_WINDOW:
            self._consec_fails = 0          # stale failures; treat as recovered
        return self._consec_fails >= DEGRADE_FAILS


def provider_a(seed=1, answer_rate=0.4):
    """Fast, reliable, low failure, no duplicates, in-order."""
    return MockProvider("A", seed, answer_rate, setup_min=0.5, setup_max=1.5,
                        timeout_prob=0.0, timeout_latency=0.0, failure_rate=0.02,
                        dup_rate=0.0, ooo_rate=0.0)


def provider_b(seed=2, answer_rate=0.4):
    """Slow, occasional timeouts, duplicate + out-of-order events."""
    return MockProvider("B", seed, answer_rate, setup_min=2.0, setup_max=6.0,
                        timeout_prob=0.10, timeout_latency=10.0, failure_rate=0.12,
                        dup_rate=0.05, ooo_rate=0.08)
