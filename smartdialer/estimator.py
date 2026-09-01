"""The predictive pacer needs an estimate of the answer rate. This is where the
'why so many calls?' story starts: the pacer dials against the *estimated* rate,
and when reality diverges from the estimate, the safety controller is what
catches the gap. That divergence is the whole point of the drop-to-10% test.

Rolling window over the last N outcomes (the decision log's "last 50 calls",
widened to keep it stable at high call volume). A plain window is unbiased and
reacts within N calls; at hundreds of calls per tick that's a second or two,
fast enough for the sudden-drop scenario while not being noisy tick to tick.
"""

from collections import deque


class RollingEstimator:
    def __init__(self, prior_rate: float = 0.3, window: int = 200):
        self._window = deque(maxlen=window)
        self._prior = prior_rate
        self.attempts = 0
        self.answers = 0

    def record_outcome(self, answered: bool) -> None:
        self.attempts += 1
        if answered:
            self.answers += 1
        self._window.append(1 if answered else 0)

    @property
    def predicted_answer_rate(self) -> float:
        if len(self._window) < 10:            # too little data: lean on the prior
            rate = self._prior
        else:
            rate = sum(self._window) / len(self._window)
        return min(max(rate, 0.02), 0.98)     # keep the pacer from dividing by ~0
