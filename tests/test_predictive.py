from smartdialer.estimator import RollingEstimator
from smartdialer.pacing import PredictivePacer, ProgressivePacer
from smartdialer.snapshot import Snapshot


def _snap(**kw):
    base = dict(agents_available=10, prev_available=10, calls_dialing=0, calls_ringing=0,
                calls_connected=0, calls_in_flight=0, anomaly_count_5min=0,
                provider_degraded=False, predicted_answer_rate=0.4,
                agents_expected_free_soon=0, observed_attempts=10 ** 9)
    base.update(kw)
    return Snapshot(**base)


def test_estimator_is_unbiased():
    est = RollingEstimator(prior_rate=0.3, window=200)
    # feed a stable 20% answer rate; estimate should track it, not the 0.3 prior
    pattern = [True, False, False, False, False]      # exactly 20%
    for i in range(1000):
        est.record_outcome(pattern[i % 5])
    assert abs(est.predicted_answer_rate - 0.20) < 0.03


def test_estimator_reacts_to_a_drop():
    est = RollingEstimator(prior_rate=0.3, window=200)
    for _ in range(300):                              # settle at 70%
        est.record_outcome(True)
    for i in range(300):
        est.record_outcome(i % 10 < 7)
    high = est.predicted_answer_rate
    for _ in range(300):                              # rate collapses to 10%
        est.record_outcome(False)
    for i in range(300):
        est.record_outcome(i % 10 < 1)
    low = est.predicted_answer_rate
    assert high > 0.6 and low < 0.2                   # it followed the drop down


def test_predictive_warmup_is_progressive():
    p = PredictivePacer()
    # not enough data yet -> paces like progressive (one per free agent)
    snap = _snap(agents_available=10, observed_attempts=5, calls_dialing=0, calls_ringing=0)
    assert p.propose(snap) == 10


def test_predictive_worked_example_after_warmup():
    p = PredictivePacer()
    snap = _snap(agents_available=10, agents_expected_free_soon=5,
                 predicted_answer_rate=0.4, calls_in_flight=3,
                 observed_attempts=1000)
    assert p.propose(snap) == 35                      # ceil(15/0.4) - 3


def test_progressive_never_exceeds_available():
    p = ProgressivePacer()
    snap = _snap(agents_available=7, calls_dialing=2, calls_ringing=1)
    assert p.propose(snap) == 4                       # 7 - 3 in setup
