"""End-to-end simulator.

Each tick runs the real pipeline against a virtual clock:

    provider events -> ingest -> bridge/complete -> sweep -> build snapshot
    -> pacer proposes -> safety controller decides + places -> log row

Nothing here is a shortcut around the real code: the same allocator, safety
controller, providers and state machines from the package are used. The
simulator only supplies time, borrowers, talk-time samples and the injectors.

Usage:
    python3 simulate.py --scenario {A,B,C,D} --provider {A,B} --duration 180
    optional: --outage-start 60 --outage-end 90 --drop-at 100 --drop-count 40
              --agents 100 --seed 1 --out run.csv
"""

import argparse
import os
import random
import tempfile

from smartdialer import db as _db
from smartdialer import agent as A
from smartdialer import borrower as B
from smartdialer import call as C
from smartdialer import clock
from smartdialer import sweeper as S
from smartdialer import providers as P
from smartdialer.allocator import CallAllocator
from smartdialer.safety import SafetyController
from smartdialer.pacing import PredictivePacer
from smartdialer.snapshot import build_snapshot
from smartdialer.estimator import RollingEstimator
from smartdialer import reconcile

SCENARIOS = {
    "A": dict(answer_rate=0.20, talk=120),
    "B": dict(answer_rate=0.50, talk=90),
    "C": dict(answer_rate=0.70, talk=180),
    "D": dict(answer_rate=0.20, talk=120),   # changes across phases at runtime
}

FREE_SOON_HORIZON = 10.0   # a connected call finishing within 10s counts as "free soon"
WRAP_SECONDS = 3.0


class Simulator:
    def __init__(self, n_agents, provider, scenario, duration, seed,
                 outage=None, drop=None):
        self.n_agents = n_agents
        self.provider = provider
        self.scenario = scenario
        self.duration = duration
        self.rng = random.Random(seed)
        self.outage = outage          # (start, end) or None
        self.drop = drop              # (at, count) or None
        self.talk_mean = SCENARIOS[scenario]["talk"]
        self.provider.answer_rate = SCENARIOS[scenario]["answer_rate"]

        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        _db.init_db(self.path)
        self.conn = _db.connect(self.path)

        self.vc = 0.0
        clock.set_source(lambda: self.vc)

        for i in range(n_agents):
            A.create_agent(self.conn, f"a{i}")
        self._bid = 0
        self._topup()

        self.est = RollingEstimator(prior_rate=0.3)
        self.pacer = PredictivePacer()
        self.allocator = CallAllocator(self.conn, provider, worker_id="sim")
        self.controller = SafetyController(self.conn, self.allocator)

        self.connected_complete = {}   # call_id -> completion time
        self.wrap_until = {}           # agent_id -> time it returns to AVAILABLE
        self.answered_calls = set()
        self.counted_terminal = set()
        self.prev_available = n_agents
        self._dropped = False
        self._last_full_sweep = 0.0
        self.rows = []
        self.util_samples = []

    # -- borrower supply --------------------------------------------------
    def _topup(self):
        q = self.conn.execute("SELECT COUNT(*) n FROM borrowers WHERE state=?",
                              (B.QUEUED,)).fetchone()["n"]
        need = self.n_agents * 4 - q
        if need > 0:
            batch = []
            for _ in range(need):
                batch.append((f"b{self._bid}", "555", "QUEUED", self.vc))
                self._bid += 1
            self.conn.executemany(
                "INSERT INTO borrowers (id, phone, state, updated_at) VALUES (?,?,?,?)",
                batch,
            )

    # -- per-tick stages --------------------------------------------------
    def _drain_events(self):
        for ev in self.provider.poll_events(now=self.vc):
            res = C.apply_event(self.conn, ev.call_id, ev.event_type, ev.event_id)
            if res != "applied":
                continue
            state = C.get_state(self.conn, ev.call_id)
            if ev.event_type == "ANSWERED":
                # answer decided: count it now (a connected call won't hit a
                # terminal state until talk time ends, minutes later)
                if ev.call_id not in self.answered_calls:
                    self.answered_calls.add(ev.call_id)
                    self.est.record_outcome(True)
                outcome = self.allocator.bridge_answered(ev.call_id)
                if outcome == "bridged":
                    talk = max(self.rng.gauss(self.talk_mean, self.talk_mean * 0.25), 5.0)
                    self.connected_complete[ev.call_id] = self.vc + talk
                # if abandoned, call is already terminal FAILED
            if state == C.FAILED:
                # a failure that never answered is a no-answer / setup failure
                if ev.call_id not in self.answered_calls and ev.call_id not in self.counted_terminal:
                    self.counted_terminal.add(ev.call_id)
                    self.est.record_outcome(False)
                row = self.conn.execute("SELECT agent_id FROM calls WHERE id=?",
                                       (ev.call_id,)).fetchone()
                A.release_to_available(self.conn, row["agent_id"] if row else None)

    def _advance_connected(self):
        done = [cid for cid, t in self.connected_complete.items() if self.vc >= t]
        for cid in done:
            row = self.conn.execute("SELECT agent_id FROM calls WHERE id=?",
                                   (cid,)).fetchone()
            C.internal_transition(self.conn, cid, C.CONNECTED, C.COMPLETED)
            aid = row["agent_id"] if row else None
            if aid and A.get_state(self.conn, aid) == A.CONNECTED:
                A.transition(self.conn, aid, A.CONNECTED, A.WRAP_UP)
                self.wrap_until[aid] = self.vc + WRAP_SECONDS
            del self.connected_complete[cid]

    def _advance_wrap(self):
        done = [aid for aid, t in self.wrap_until.items() if self.vc >= t]
        for aid in done:
            if A.get_state(self.conn, aid) == A.WRAP_UP:
                A.transition(self.conn, aid, A.WRAP_UP, A.AVAILABLE)
            del self.wrap_until[aid]

    def _sweep(self):
        S.sweep_abandoned(self.conn)
        if self.vc - self._last_full_sweep >= 5.0:
            S.sweep_reservations(self.conn)
            S.sweep_stale_calls(self.conn)
            self._last_full_sweep = self.vc

    def _apply_injectors(self):
        if self.outage:
            start, end = self.outage
            self.provider.force_outage = (start <= self.vc < end)
        if self.drop and not self._dropped and self.vc >= self.drop[0]:
            count = self.drop[1]
            # a real disconnect hits agents in any state, not just idle ones
            victims = self.conn.execute(
                "SELECT id FROM agents WHERE state != ? LIMIT ?", (A.OFFLINE, count)
            ).fetchall()
            for v in victims:
                reconcile.agent_offline(self.conn, v["id"])
            self._dropped = True
        # Scenario D: shift the true answer rate over the run
        if self.scenario == "D":
            third = self.duration / 3.0
            if self.vc < third:
                self.provider.answer_rate = 0.20
            elif self.vc < 2 * third:
                self.provider.answer_rate = 0.70
            else:
                step = self.rng.uniform(-0.05, 0.05)
                self.provider.answer_rate = min(max(self.provider.answer_rate + step, 0.05), 0.90)

    def _expected_free_soon(self):
        return sum(1 for t in self.connected_complete.values()
                   if t - self.vc <= FREE_SOON_HORIZON)

    def _utilization(self, snap):
        busy = snap.calls_connected
        active = self.conn.execute(
            "SELECT COUNT(*) n FROM agents WHERE state != ?", (A.OFFLINE,)
        ).fetchone()["n"]
        return (busy / active) if active else 0.0

    # -- main loop --------------------------------------------------------
    def tick(self, dt=1.0):
        self.vc += dt
        self._apply_injectors()
        self._drain_events()
        self._advance_connected()
        self._advance_wrap()
        self._sweep()
        self._topup()

        snap = build_snapshot(
            self.conn, self.provider, self.prev_available,
            predicted_answer_rate=self.est.predicted_answer_rate,
            agents_expected_free_soon=self._expected_free_soon(),
            observed_attempts=self.est.attempts,
            now_ts=self.vc,
        )
        proposed = self.pacer.propose(snap)
        decision = self.controller.admit(self.pacer, snap)

        util = self._utilization(snap)
        self.util_samples.append(util)
        self.rows.append(dict(
            t=round(self.vc, 1),
            agents_available=snap.agents_available,
            calls_dialing=snap.calls_dialing,
            calls_ringing=snap.calls_ringing,
            calls_connected=snap.calls_connected,
            answer_rate_est=round(self.est.predicted_answer_rate, 3),
            proposed_n=proposed,
            safety_action=decision.action,
            approved_n=decision.approved_n,
            utilization=round(util, 3),
        ))
        self.prev_available = snap.agents_available

    def run(self):
        try:
            steps = int(self.duration)
            for _ in range(steps):
                self.tick(1.0)
        finally:
            clock.reset_source()
        return self.summary()

    def _metric(self, name):
        row = self.conn.execute("SELECT value FROM metrics WHERE name=?", (name,)).fetchone()
        return row["value"] if row else 0.0

    def summary(self):
        interventions = self.conn.execute(
            "SELECT COUNT(*) n FROM safety_decisions WHERE action != 'APPROVE'"
        ).fetchone()["n"]
        avg_util = sum(self.util_samples) / len(self.util_samples) if self.util_samples else 0.0
        return dict(
            scenario=self.scenario, provider=self.provider.name, duration=self.duration,
            avg_utilization=round(avg_util, 3),
            calls_initiated=int(self._metric("calls_initiated")),
            calls_connected=int(self._metric("calls_connected")),
            forced_abandonment=int(self._metric("forced_abandonment")),
            safety_interventions=interventions,
            final_answer_rate_est=round(self.est.predicted_answer_rate, 3),
        )

    def write_csv(self, path):
        if not self.rows:
            return
        import csv
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(self.rows[0].keys()))
            w.writeheader()
            w.writerows(self.rows)

    def close(self):
        self.conn.close()
        for s in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + s)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description="SmartDialer simulator")
    ap.add_argument("--scenario", choices=list(SCENARIOS), default="B")
    ap.add_argument("--provider", choices=["A", "B"], default="A")
    ap.add_argument("--duration", type=int, default=180)
    ap.add_argument("--agents", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--outage-start", type=float, default=None)
    ap.add_argument("--outage-end", type=float, default=None)
    ap.add_argument("--drop-at", type=float, default=None)
    ap.add_argument("--drop-count", type=int, default=None)
    ap.add_argument("--out", default=None, help="write time-series CSV to this path")
    args = ap.parse_args()

    provider = (P.provider_a if args.provider == "A" else P.provider_b)(seed=args.seed)
    outage = None
    if args.outage_start is not None and args.outage_end is not None:
        outage = (args.outage_start, args.outage_end)
    drop = None
    if args.drop_at is not None and args.drop_count is not None:
        drop = (args.drop_at, args.drop_count)

    sim = Simulator(args.agents, provider, args.scenario, args.duration, args.seed,
                    outage=outage, drop=drop)
    summary = sim.run()
    if args.out:
        sim.write_csv(args.out)
    sim.close()

    print("\n=== run summary ===")
    for k, v in summary.items():
        print(f"{k:>22}: {v}")
    if args.out:
        print(f"{'time_series_csv':>22}: {args.out}")


if __name__ == "__main__":
    main()
