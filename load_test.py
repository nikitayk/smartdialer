"""Load test for the one claim the decision log makes about scale: that SQLite's
single-writer lock is the first thing to break, not CPU or the Python loop.

We don't try to place 10,000 real calls. We isolate the hot write path — the
atomic reserve — and hammer it with W concurrent workers against N agent rows on
one shared SQLite file, then read off:

  - committed reserve+release throughput (writes/sec)
  - CAS UPDATE latency p50/p95/p99
  - how often SQLite actually raised 'database is locked' (vs. absorbing the
    contention into busy_timeout waits, which show up as latency instead)

The interesting result is what happens to throughput and tail latency as W grows:
if the single writer is the ceiling, throughput stops rising and p99 climbs.

Usage:
    python3 load_test.py --sweep                 # the standard comparison matrix
    python3 load_test.py --agents 1000 --workers 8 --duration 3
"""

import argparse
import sqlite3
import threading
import time

from smartdialer import db as _db
from smartdialer import agent as A
from smartdialer.clock import now


def _worker(path, n_agents, duration, out, idx, start_evt):
    conn = _db.connect(path)
    latencies = []
    ok = conflict = locked = attempts = 0
    rng = idx * 7919
    start_evt.wait()
    deadline = time.perf_counter() + duration
    while time.perf_counter() < deadline:
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        aid = f"a{rng % n_agents}"
        attempts += 1
        t0 = time.perf_counter()
        try:
            reserved = A.try_reserve(conn, aid, f"w{idx}")
            latencies.append(time.perf_counter() - t0)
            if reserved:
                ok += 1
                # release immediately to keep the pool churning (a 2nd write)
                conn.execute(
                    "UPDATE agents SET state='AVAILABLE', reserved_by=NULL, "
                    "reserved_at=NULL, updated_at=? WHERE id=? AND state='RESERVED'",
                    (now(), aid),
                )
            else:
                conflict += 1
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                locked += 1
            else:
                raise
    conn.close()
    out[idx] = dict(attempts=attempts, ok=ok, conflict=conflict,
                    locked=locked, latencies=latencies)


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = min(int(len(sorted_vals) * p), len(sorted_vals) - 1)
    return sorted_vals[k]


def run_once(n_agents, n_workers, duration):
    import os, tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _db.init_db(path)
    setup = _db.connect(path)
    for i in range(n_agents):
        A.create_agent(setup, f"a{i}")
    setup.close()

    out = [None] * n_workers
    start_evt = threading.Event()
    threads = [threading.Thread(target=_worker,
                                args=(path, n_agents, duration, out, i, start_evt))
               for i in range(n_workers)]
    for t in threads:
        t.start()
    wall0 = time.perf_counter()
    start_evt.set()
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall0

    all_lat = []
    ok = conflict = locked = attempts = 0
    for r in out:
        ok += r["ok"]; conflict += r["conflict"]; locked += r["locked"]
        attempts += r["attempts"]; all_lat += r["latencies"]
    all_lat.sort()
    committed_writes = ok * 2 + conflict          # reserve+release for ok, one UPDATE for conflict
    for s in ("", "-wal", "-shm"):
        try:
            os.remove(path + s)
        except OSError:
            pass
    return dict(
        agents=n_agents, workers=n_workers,
        writes_per_s=round(committed_writes / wall),
        reserves_per_s=round(ok / wall),
        attempts=attempts, locked=locked,
        locked_rate=round(locked / attempts, 4) if attempts else 0.0,
        p50_ms=round(_pct(all_lat, 0.50) * 1000, 3),
        p95_ms=round(_pct(all_lat, 0.95) * 1000, 3),
        p99_ms=round(_pct(all_lat, 0.99) * 1000, 3),
    )


def sweep(duration):
    cols = ["agents", "workers", "writes_per_s", "reserves_per_s",
            "locked_rate", "p50_ms", "p95_ms", "p99_ms"]
    print("  ".join(f"{c:>13}" for c in cols))
    for n_agents in (100, 1000):
        for w in (1, 2, 4, 8, 16):
            r = run_once(n_agents, w, duration)
            print("  ".join(f"{r[c]:>13}" for c in cols))


def main():
    ap = argparse.ArgumentParser(description="SmartDialer reserve-path load test")
    ap.add_argument("--agents", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--duration", type=float, default=3.0)
    ap.add_argument("--sweep", action="store_true", help="run the standard comparison matrix")
    args = ap.parse_args()
    if args.sweep:
        sweep(args.duration)
    else:
        r = run_once(args.agents, args.workers, args.duration)
        for k, v in r.items():
            print(f"{k:>16}: {v}")


if __name__ == "__main__":
    main()
