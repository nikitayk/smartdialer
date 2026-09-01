# Defense notes

Prep for the technical discussion. Every answer points at real code. Where the
honest answer is "here is a limitation," it says so — that scores better than
pretending the edge is covered, and it is asked for directly ("what are you least
confident about", "what would you do differently").

---

## "Two workers try to reserve the same agent at exactly the same time."

`smartdialer/agent.py`, `try_reserve` (line 52). Both workers run the same
statement: `UPDATE agents SET state='RESERVED', ... WHERE id=? AND state='AVAILABLE'`.
SQLite serializes the two writes. The first flips AVAILABLE -> RESERVED and gets
`rowcount == 1`. The second's `WHERE state='AVAILABLE'` no longer matches, gets
`rowcount == 0`, and learns it lost from that same statement — no separate read,
no race window. It moves to the next candidate. Proven under real threads in
`tests/test_concurrency.py::test_20_threads_one_agent_exactly_one_wins` (exactly
one of 20 wins).

The discipline that makes this valid: nowhere do we SELECT a state and then
UPDATE it separately. Every write in the system is a guarded CAS.

## "Your DB says AVAILABLE, but your cache says RESERVED. Which one wins?"

There is no cache. SQLite is the single source of truth (`smartdialer/db.py`
header). This question has no answer to get wrong because there is no second copy
of the state to disagree. That is a deliberate V1 simplification: a cache would
buy read throughput but reintroduce exactly this consistency problem.

## "Provider sends ANSWERED, your worker crashes, then COMPLETED arrives."

Three mechanisms combine (`smartdialer/call.py`, `apply_event`, line 103):

1. ANSWERED applied, worker crashes before bridging. The call is now ANSWERED
   with no agent — a live human on the line. The 2s abandonment guard
   (`sweeper.py`, `sweep_abandoned`, line 78) fails it fast and counts
   `forced_abandonment`. This is the tight path; the generic 30s sweep would be
   a compliance violation here, which is why it is separate.
2. If instead COMPLETED arrives out of order: it is deduped by
   `provider_event_id` (line 110), and if it applies it makes the call terminal.
3. Once terminal, no later event moves it (line 127, terminal guard). A late
   COMPLETED after an abandon-fail is rejected and logged, not applied.

Either ordering ends in a consistent terminal state.
`tests/test_failure_cases.py::test_case1_worker_crash_mid_flow` and
`::test_case5_out_of_order_terminal_never_reverses` cover this.

## "Your model predicted 70%, it drops to 10%. How does the system protect itself?"

Three layers, fastest to slowest:

1. The estimate itself moves. `estimator.py` is a rolling window over recent
   outcomes, so a real drop pulls the predicted rate down within a window of
   calls (at high volume, a second or two). A lower rate directly lowers the
   pacer's proposal.
2. The hard overdial cap (`safety.py`, `hard_overdial_cap`, line 57) bounds
   `dialing + ringing` to `available * 1.5` regardless of what the pacer says,
   so even a stale high estimate cannot flood.
3. If the drop is because the provider is failing, the circuit breaker
   (line 38) collapses to one dial per free agent.

And when answers still beat the agent pool, the 2s abandonment guard catches the
overflow rather than leaving dead lines. The demo: `simulate.py --scenario D`
changes the rate mid-run; the CSV shows the estimate following it and the safety
action column reacting.

## "1,000 -> 100,000 agents. What breaks first?"

Not the reserve path — that was the assumed answer and the load test disproved
it. `docs/SCALE.md` has the measured numbers. Short version: the single-writer
ceiling (~10k reserves/s measured) is ~30x the rate even 10k agents demand, and
contention shows up as p99 latency, not lock errors. What actually breaks, in
order: (1) hot-row metric contention (every call bumps single counter rows),
(2) the whole-lifecycle write rate through one writer, (3) sweeper scan cost,
(4) the hard single-host limit. Fix order: shard the metric counters (cheap),
then Postgres for multi-host + real write concurrency (the CAS pattern ports
directly), then partition by campaign.

## "Why did your algorithm decide to initiate 17 calls instead of 10?"

Read the `safety_decisions` row. Every cycle logs `proposed_n`, `action`,
`approved_n`, `rule_triggered`, `reason` (`safety.py`, `admit`, line 80). The
pacer's raw math and the safety clip are two independent, separately defensible
numbers. Worked example (a passing test,
`tests/test_predictive.py::test_predictive_worked_example_after_warmup`):
available 10, expected-free-soon 5, rate 0.4, in-flight 3 -> pacer proposes
`ceil(15/0.4) - 3 = 35`; the cap at `1.5 * 10 = 15` with 3 already in setup
approves 12. The row shows exactly that.

## "What part are you least confident about?"

Provider health. `is_degraded()` is tracked inside each provider instance
(in-memory consecutive failures). That is correct for the single-process
simulator, but with workers on separate connections/hosts each would see only
its own failures, so the breaker could be slow to trip globally. The real fix is
a shared health record in the DB (or a small health table) that every worker
updates and reads. I scoped it to in-provider for V1 and am calling it out rather
than hiding it.

Second: the `1.5` overdial ratio is a named constant (`MAX_OVERDIAL_RATIO`), not
calibrated from data — a reasonable conservative starting point that a real
deployment would tune from historical abandonment rates.

## "What would you do differently with another week?"

- Move provider health into the DB so the breaker is correct across workers.
- Shard the metric counters off single hot rows (the first real scale fix).
- Add feed-forward to the pacer for fast rate changes: `scenario D` on the flaky
  provider shows abandonment spike when the true rate jumps faster than the
  rolling window adapts — a short-term derivative term would cut that.
- A real Plivo integration behind the existing provider interface (the interface
  was built so this is a drop-in, not a rewrite).

---

## Known divergences from the V1 decision log (own these before you're asked)

I did not implement the log blindly. Four deliberate, flagged changes:

1. **Event-id dedup.** The log described state-based dedup but also carried
   `provider_event_id` in its schema/tests; those didn't quite agree. I made the
   event id the real dedup gate (`call.py`, line 110) so a duplicate is
   idempotent independent of state.
2. **2s abandonment guard.** The log left answered-but-unconnected calls on the
   30s sweep, which blows the abandonment SLA the whole assignment is about. I
   added a dedicated 2s path (`sweeper.py`, line 78).
3. **Bug fix.** Worker-crash-mid-dial stranded the bound agent in DIALING
   forever. Fixed so the sweeper releases it (`agent.release_to_available`;
   commit "fix: release agent stranded in DIALING").
4. **Scale claim corrected by measurement.** The log (Section 12/19) asserts the
   SQLite writer bottlenecks around 1,000 agents and uses that to justify moving
   to Postgres. The load test does not support the 1,000-agent claim — the
   reserve ceiling is far higher. The Postgres migration is still right, but for
   the single-host limit and hot-row contention, not the reserve path. If a V2
   exists whose rationale is "V1's writer bottleneck at 1,000 agents," that
   rationale needs updating to match this measurement, or the two docs will
   contradict each other.

5. **Anomaly-spike guard clamped to the overdial cap (found and fixed).** The
   safety rules are first-match-wins, so a match at rule 3 (`anomaly_spike`)
   short-circuits rule 4 (`hard_overdial_cap`). The original rule 3 returned
   `proposed // 2`; but halving a *huge* predictive proposal (low estimated
   answer rate) is still huge, so during an anomaly spike the guard could approve
   more setups than `available * 1.5` — i.e. the guard meant to be conservative
   could breach the overdial envelope, and the claimed invariant "predictive
   pacing cannot expand the safety envelope" did not strictly hold in that branch.
   Fix (`safety.py`, rule 3): also clamp to `cap - in_setup`, so the anomaly guard
   can only ever be *more* conservative. Measured effect on a seeded flaky-provider
   stress run (`--scenario D --provider B`): forced abandonments dropped 163 → 38.
   Pinned by `tests/test_invariants.py::test_anomaly_spike_clamped_to_cap_regression`
   and the property test `::test_predictive_never_exceeds_overdial_cap_across_rates`.
   The existing `test_rule3_anomaly_spike_reduce_half` still passes unchanged (its
   proposal was already under the cap).
