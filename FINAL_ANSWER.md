# Final Answer: Predictive Utilization, Progressive Safety

**Question:** How would you build a SmartDialer that gets as much of the
utilization benefit of predictive dialing as possible, while retaining the
deterministic safety characteristics of progressive dialing?

## The core idea

Predictive pacing can decide how aggressively to *propose* work. It can
never decide how much work is *safe*. Those are two different components in
this system, and the second one doesn't trust the first.

## Why progressive is safe

Reserve-then-dial: an agent is CAS-reserved (`state='AVAILABLE'` guard)
*before* a call is placed, so every dial already has a landing spot. Live
agent-bound calls can never exceed available agents — an arithmetic
guarantee, no estimate involved.

## Why predictive is inherently more aggressive

`PredictivePacer.propose` computes `ceil(target / rate) - in_flight`. As the
estimated answer rate falls, the proposal grows without bound — it's
*designed* to over-dial relative to free agents, because that's where the
utilization gain comes from.

## The one thing that makes it safe anyway

Both pacers feed the same `SafetyController`, which is the *only* object
holding a reference to the Call Allocator — the pacer returns an int and
physically cannot reach the provider. `decide()` runs six ordered,
first-match-wins rules on every proposal:

| # | Rule | Fires when | Result |
|---|------|-----------|--------|
| 1 | `circuit_breaker` | provider degraded | fall back to 1 dial/free agent |
| 2 | `sudden_agent_drop` | avail drops >25% vs last cycle | reject, 0 |
| 3 | `anomaly_spike` | >20 anomalies in 5 min | halve, clamped to cap |
| 4 | `hard_overdial_cap` | in-setup + proposed > 1.5×avail | reduce to fill the cap |
| 5 | `nothing_proposed` | proposed ≤ 0 | reject |
| 6 | `default_approve` | otherwise | approve as proposed |

Every decision is written to `safety_decisions` with the rule that fired —
"why 12 calls and not 35" is a row you read, not a re-derivation.

## A real example

With `available=10`, `expected_free_soon=5`, `rate=0.4`, `in_flight=3`, the
predictive pacer proposes **35** calls. The overdial cap is `1.5×10=15`;
with 3 already in setup, the controller approves **12** and logs
`hard_overdial_cap` as the reason. The pacer's number and the controller's
number are both correct — they're answering different questions.

## What happens when the prediction is wrong

Overestimate → clipped by the overdial cap; any answers beyond the agent
pool hit the per-agent binding gate at answer time and force-fail as a
*counted* abandonment, backstopped by a 2-second sweep. Underestimate →
lower utilization, never a safety breach. Error costs utilization, not
safety — and this isn't just a design claim: tightening the anomaly-spike
rule to respect the same cap dropped forced abandonments from 163 to 38 on
a flaky-provider stress run (`--scenario D --provider B`).

## In one line

The safety guarantee doesn't come from the predictor being right — it comes
from the predictor being structurally unable to place a call regardless of
whether it's right.

Full 12-point walkthrough (cold start, provider degradation, agent
disconnects mid-call, etc.): `docs/ARCHITECTURE.md`, §27–28.
