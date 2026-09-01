# SmartDialer

A collections auto-dialer prototype: progressive and predictive pacing behind a
safety controller that the pacing logic can never bypass. Python 3.10+ and
SQLite only — no external services, no third-party packages.

## Run everything in one command

```bash
python3 run_tests.py && python3 simulate.py --scenario B --provider A --duration 120 --agents 100
```

The first runs the full test suite (stdlib only); the second runs an end-to-end
simulation and prints a utilization / pacing / safety summary.

## Deliverable checklist

| Deliverable | Location |
|---|---|
| Working source code | `smartdialer/` |
| README + setup | this file |
| Architecture diagram | `docs/ARCHITECTURE.md` (mermaid) |
| Agent state machine | `docs/ARCHITECTURE.md` + `smartdialer/agent.py` |
| Call state machine | `docs/ARCHITECTURE.md` + `smartdialer/call.py` |
| Progressive dialer | `smartdialer/pacing.py` (ProgressivePacer) + `smartdialer/allocator.py` |
| Predictive pacing engine | `smartdialer/pacing.py` (PredictivePacer) + `smartdialer/estimator.py` |
| Safety controller | `smartdialer/safety.py` (six ordered rules, bypass-proof) |
| Mock telecom providers | `smartdialer/providers/` (A and B, different behaviour) |
| Tests | `tests/`, run via `run_tests.py` |
| Basic simulation | `simulate.py` |
| Basic load test | `load_test.py` |
| Architecture decision doc | `docs/ARCHITECTURE.md` (ADR) + `docs/SCALE.md` |
| Defense / discussion prep | `DEFENSE_NOTES.md` |

## Run the tests

```bash
python3 run_tests.py     # stdlib only, no dependencies
# or, if you have pytest installed:
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
summary and writes a per-tick CSV (proposed vs approved counts and the safety
action for every tick).

## Run the load test

```bash
python3 load_test.py --sweep --duration 3
```

Measures reserve throughput, CAS latency percentiles, and lock-contention as
concurrency grows. See `docs/SCALE.md` for the measured results and what they
mean for scaling to 10,000 agents.

## Design in three lines

- **One source of truth.** All state is in one SQLite file; no cache, so no
  cache-vs-DB disagreement.
- **Compare-and-swap everywhere.** Every state change is
  `UPDATE ... WHERE state = <expected>`; two workers can't act on the same row.
- **One reconciler.** The sweeper handles timeouts, crashes, and (on a tight 2s
  path) the abandoned-call compliance case — no separate crash-recovery code.

See `docs/ARCHITECTURE.md` for the full pipeline, both state machines, the ADR,
and the answer to the utilization-vs-safety question.
