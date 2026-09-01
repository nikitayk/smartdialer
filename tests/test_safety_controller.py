from smartdialer import safety
from smartdialer.safety import decide, SafetyController
from smartdialer.pacing import ProgressivePacer, PredictivePacer
from smartdialer.snapshot import Snapshot


def _snap(**kw):
    base = dict(
        agents_available=10, prev_available=10, calls_dialing=0, calls_ringing=0,
        calls_connected=0, calls_in_flight=0, anomaly_count_5min=0,
        provider_degraded=False, predicted_answer_rate=0.4, agents_expected_free_soon=0,
    )
    base.update(kw)
    return Snapshot(**base)


def test_rule1_circuit_breaker_fallback():
    d = decide(30, _snap(provider_degraded=True, agents_available=8))
    assert d.action == "FALLBACK_PROGRESSIVE"
    assert d.approved_n == 8              # min(30, available)
    assert d.rule_triggered == "circuit_breaker"


def test_rule2_sudden_drop_reject():
    d = decide(20, _snap(prev_available=100, agents_available=60))
    assert d.action == "REJECT"
    assert d.rule_triggered == "sudden_agent_drop"


def test_rule2_moderate_drop_not_triggered():
    # 100 -> 80 is only 20%, under the 25% threshold
    d = decide(5, _snap(prev_available=100, agents_available=80,
                        calls_dialing=0, calls_ringing=0))
    assert d.rule_triggered != "sudden_agent_drop"


def test_rule3_anomaly_spike_reduce_half():
    d = decide(10, _snap(anomaly_count_5min=21))
    assert d.action == "REDUCE"
    assert d.approved_n == 5
    assert d.rule_triggered == "anomaly_spike"


def test_rule4_hard_overdial_cap_reduce():
    # avail=10 -> cap=15; already 12 in setup; proposing 10 -> only 3 allowed
    d = decide(10, _snap(agents_available=10, calls_dialing=8, calls_ringing=4))
    assert d.action == "REDUCE"
    assert d.approved_n == 3
    assert d.rule_triggered == "hard_overdial_cap"


def test_rule5_nothing_proposed_reject():
    d = decide(0, _snap())
    assert d.action == "REJECT"
    assert d.rule_triggered == "nothing_proposed"


def test_rule6_default_approve():
    d = decide(4, _snap(agents_available=10))
    assert d.action == "APPROVE"
    assert d.approved_n == 4
    assert d.rule_triggered == "default_approve"


def test_rule_order_breaker_beats_everything():
    # degraded AND a big drop AND anomalies: circuit breaker must win (rule 1)
    d = decide(30, _snap(provider_degraded=True, prev_available=100,
                        agents_available=5, anomaly_count_5min=99))
    assert d.rule_triggered == "circuit_breaker"


def test_predictive_worked_example_35_to_12(conn):
    """Decision log worked example: pacer proposes 35, safety cap approves 12."""
    snap = _snap(agents_available=10, agents_expected_free_soon=5,
                 predicted_answer_rate=0.4, calls_in_flight=3,
                 calls_dialing=3, calls_ringing=0)
    proposed = PredictivePacer().propose(snap)
    assert proposed == 35                       # ceil(15/0.4) - 3
    d = decide(proposed, snap)
    # cap=15, already 3 in setup -> allowed 12
    assert d.action == "REDUCE"
    assert d.approved_n == 12


class _SpyAllocator:
    def __init__(self):
        self.calls = []

    def place(self, n, reserve_agents=True):
        self.calls.append((n, reserve_agents))
        return n


def test_pacer_cannot_reach_allocator_structural():
    """Structural proof: a pacer has no allocator handle and propose() takes
    only a snapshot. The controller is the sole holder of the allocator."""
    pacer = ProgressivePacer()
    assert not hasattr(pacer, "_allocator")
    assert not hasattr(pacer, "allocator")
    # propose takes exactly one arg besides self
    argcount = pacer.propose.__func__.__code__.co_argcount
    assert argcount == 2  # self, snap


def test_controller_admits_and_logs(conn):
    spy = _SpyAllocator()
    ctrl = SafetyController(conn, spy)
    snap = _snap(agents_available=6, prev_available=6, calls_dialing=0, calls_ringing=0)
    d = ctrl.admit(ProgressivePacer(), snap)
    assert d.action == "APPROVE"
    assert spy.calls == [(6, True)]            # progressive -> reserve_agents=True
    logged = conn.execute("SELECT action, approved_n FROM safety_decisions").fetchone()
    assert logged["action"] == "APPROVE" and logged["approved_n"] == 6
