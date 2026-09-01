"""Pacing engines propose a number and nothing else: propose(snapshot) -> int.

They are given no allocator, no provider, no DB handle — so a pacer physically
cannot place a call or bypass the safety controller. That is the whole point of
keeping this a plain function of a snapshot.

ProgressivePacer is fully live here (Phase 2). It is also the real fallback the
safety controller collapses to, so it is not a stub. PredictivePacer is filled
in in Phase 4.
"""

import math


class ProgressivePacer:
    """One dial per free agent: never propose more calls than agents you have.
    Subtract calls already dialing/ringing so we don't double-count."""
    mode = "progressive"

    def propose(self, snap) -> int:
        in_setup = snap.calls_dialing + snap.calls_ringing
        return max(snap.agents_available - in_setup, 0)


class PredictivePacer:
    """Dial ahead of free agents based on expected answer rate. Filled in Phase 4;
    the formula and worked example are tested there."""
    mode = "predictive"

    def propose(self, snap) -> int:
        rate = max(snap.predicted_answer_rate, 0.01)
        target = snap.agents_available + snap.agents_expected_free_soon
        raw = math.ceil(target / rate) - snap.calls_in_flight
        return max(raw, 0)
