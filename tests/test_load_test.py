import load_test


def test_load_test_runs_and_reports():
    r = load_test.run_once(n_agents=50, n_workers=4, duration=0.3)
    for key in ("writes_per_s", "reserves_per_s", "locked_rate",
                "p50_ms", "p95_ms", "p99_ms", "attempts"):
        assert key in r
    assert r["attempts"] > 0
    assert r["writes_per_s"] >= 0
    assert 0.0 <= r["locked_rate"] <= 1.0
