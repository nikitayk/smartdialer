# SmartDialer

A collections auto-dialer prototype: progressive and predictive pacing behind a
safety controller that can never be bypassed by the pacing logic.

Runs on Python 3.10+ and SQLite only — no external services, no extra packages.

## Run the tests

```bash
python3 run_tests.py     # stdlib only, no dependencies
# or, if you have pytest:
pytest -q
```

## Run the simulator

```bash
python3 simulate.py --scenario B --provider A --duration 180 --agents 100 --out run.csv
```

Scenarios: `A` (20% answer / 120s talk), `B` (50% / 90s), `C` (70% / 180s),
`D` (rate changes across the run). Providers: `A` (fast/reliable) or `B`
(slow/timeouts/duplicates/out-of-order). Inject failures with
`--outage-start/--outage-end` and `--drop-at/--drop-count`. The run prints a
summary (utilization, calls initiated/connected, forced_abandonment,
safety-controller interventions) and writes a per-tick CSV with the proposed vs
approved call counts and the safety action for every tick.

## Where each required deliverable lives

| Deliverable            | Location |
|------------------------|----------|
| Agent state machine    | `smartdialer/agent.py` |
| Call state machine     | `smartdialer/call.py` |
| Borrower queue         | `smartdialer/borrower.py` |
| Sweeper / reconciler   | `smartdialer/sweeper.py` |
| DB schema (source of truth) | `smartdialer/db.py` |
| Safety Controller (6 ordered rules) | `smartdialer/safety.py` |
| Call Allocator         | `smartdialer/allocator.py` |
| Pacing engines (progressive + predictive) | `smartdialer/pacing.py` |
| Answer-rate estimator  | `smartdialer/estimator.py` |
| Agent-offline reconciliation | `smartdialer/reconcile.py` |
| State snapshot         | `smartdialer/snapshot.py` |
| Mock providers A & B   | `smartdialer/providers/__init__.py` |
| Simulator (scenarios A-D, injectors) | `simulate.py` |
| Five failure-case tests | `tests/test_failure_cases.py` |
| Multi-worker stress test | `tests/test_stress.py` |
| Tests                  | `tests/`, run via `run_tests.py` |

(Load test and the architecture doc land in the last phases.)

## Design notes so far

- **One source of truth.** All state lives in one SQLite file; there is no cache,
  so there is no cache-vs-DB disagreement to resolve.
- **Compare-and-swap everywhere.** Every state change is `UPDATE ... WHERE
  state = <expected>`. We never read a state and then write it in a separate
  step, so two workers can't both act on the same row.
- **One reconciler.** The sweeper handles both timeouts and worker crashes;
  there is no separate crash-recovery path.
