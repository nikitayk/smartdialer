# Scale: what the load test actually shows

The design assumption going in was the usual one for a single-file SQLite system:
"the single-writer lock will be the first thing to break, somewhere around 1,000
agents." The load test (`load_test.py --sweep`) was written to prove that with
numbers rather than assert it. The numbers refined the story, so this is written
from what was measured, not from the original assumption.

## Method

`load_test.py` isolates the hottest write path — the atomic reserve — and runs W
worker threads doing reserve+release cycles against N agent rows on one shared
SQLite file (WAL, `busy_timeout=5000`). It records committed writes/sec, the CAS
`UPDATE` latency percentiles, and how often SQLite actually raised
`database is locked`.

## Representative result

Numbers vary by hardware and run; the shape is what matters and it is stable.

```
 agents  workers  writes/s  reserves/s  locked_rate  p50_ms  p95_ms  p99_ms
    100        1    ~19,900      ~9,900          0.0   0.02    0.05    0.13
    100        4    ~28,000     ~13,900          0.0   0.02    0.08    1.5
    100       16    ~27,600     ~13,300          0.0   0.02    0.08    6.5
   1000        1    ~12,400      ~6,200          0.0   0.03    0.07    0.16
   1000        8    ~21,000     ~10,600          0.0   0.03    0.09    2.1
   1000       16    ~18,000      ~9,200          0.0   0.03    0.21    8.6
```

## What the data says

1. **The single-writer ceiling is real.** Throughput stops rising with more
   workers — going from 1 to 16 workers buys roughly 1.4x, not 16x. All writes
   serialize through one writer; adding workers past ~4 mostly adds waiting.

2. **The bottleneck shows up as tail latency, not errors.** `locked_rate` is
   0.0 everywhere. WAL plus `busy_timeout` absorbs contention into waiting, so
   the pressure appears as p99 latency climbing ~50x (0.13ms -> ~6-12ms at 16
   workers), not as `database is locked` exceptions. The original plan to
   measure a lock-error retry rate was measuring the wrong signal; p99 is the
   signal.

3. **Agent count is a secondary effect.** N=1000 is modestly slower than N=100
   (larger table and index per write), not the cliff the assumption predicted at
   1,000 agents.

## So where does it actually break first?

Do the arithmetic. A reservation happens per placed call. At a ~3-minute average
handle time an agent completes ~20 calls/hour; 10,000 agents is ~55 calls/sec,
say ~300 reservations/sec even allowing generous predictive over-dial. The
measured reserve ceiling is ~10,000/sec — roughly 30x headroom. **The reserve
path is not the first wall at the target scale.** In rough order, the real
pressure points are:

1. **Hot-row metric contention.** Every call bumps `calls_initiated` /
   `calls_connected` / `forced_abandonment` — single rows that every worker
   writes. On SQLite this just joins the serialized write stream; on any
   client-server DB it becomes a genuine hot-row lock. Fix: per-worker counters
   summed periodically, or sharded counter rows. This is the first thing to
   change, and it's cheap.

2. **Total write rate across the whole lifecycle, not just reserves.** Each call
   is ~6-8 writes (reserve, initiate, ring, answer, connect, complete, plus
   event-ingest and metric bumps). At 10k agents that is a few thousand writes/
   sec — still under the ceiling, but now the single writer is the shared
   resource for *everything*, and tail latency is what suffers.

3. **Sweeper scan cost.** The 5s sweep scans agents and calls. Indexes on
   `state`/`reserved_at`/`updated_at` (already present) keep it cheap into the
   thousands, but at 10k+ agents with high call volume the scan and its writes
   add up. Fix: bound the scan (only rows past TTL via the index), then partition.

4. **Single host.** SQLite is one file on one machine. The moment you need
   workers on more than one host — for availability or beyond one box's write
   throughput — SQLite cannot do it at all. This is a hard limit independent of
   any benchmark and is the real reason to move.

## Migration path (and what each step costs)

- **Cheap, now:** move metric counters off the single hot row (per-worker
  aggregation). Buys headroom, adds a little reporting complexity.
- **When one host isn't enough:** move to Postgres. The CAS pattern
  (`UPDATE ... WHERE state = ...`) ports directly — the whole design was built
  on it precisely so this move is mechanical. Postgres gives real multi-client
  parallel writes and multi-host workers. What it makes harder: setup and
  operational cost go up (a server to run, connection pooling, no more
  zero-config single file), and you inherit transaction-isolation subtleties
  SQLite hid.
- **Beyond a single Postgres writer:** partition by campaign. Each campaign's
  agents/calls are independent, so campaign is a natural shard key with no
  cross-shard transactions on the hot path. What it makes harder: cross-campaign
  reporting and agent sharing across campaigns.

"Add more servers" is not the answer, because the reserve throughput was never
the wall — the single writer serializing the *whole* workload, hot-row metric
contention, and the single-host limit are, and each has a specific, measured fix.
