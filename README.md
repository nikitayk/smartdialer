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
| Pacing engines (progressive live, predictive in Phase 4) | `smartdialer/pacing.py` |
| State snapshot         | `smartdialer/snapshot.py` |
| Mock providers A & B   | `smartdialer/providers/__init__.py` |
| Five failure-case tests | `tests/test_failure_cases.py` |
| Multi-worker stress test | `tests/test_stress.py` |
| Tests                  | `tests/`, run via `run_tests.py` |

(Predictive tuning, simulator, load test and the architecture doc land in
later phases.)

## Design notes so far

- **One source of truth.** All state lives in one SQLite file; there is no cache,
  so there is no cache-vs-DB disagreement to resolve.
- **Compare-and-swap everywhere.** Every state change is `UPDATE ... WHERE
  state = <expected>`. We never read a state and then write it in a separate
  step, so two workers can't both act on the same row.
- **One reconciler.** The sweeper handles both timeouts and worker crashes;
  there is no separate crash-recovery path.
