"""Golden backtest regression test."""
from __future__ import annotations

from tests.golden_backtest.golden import compare, load_expected, run_golden, snapshot


def test_golden_backtest_matches_baseline(tmp_path):
    result = run_golden(tmp_path / "golden", run_id="golden_test")
    current = snapshot(result)
    expected = load_expected()
    assert expected is not None, "expected_metrics.json missing"
    problems = compare(current, expected)
    assert problems == [], problems
    assert result.metrics["trade_count"] > 0
    assert result.metrics["total_fees"] > 0


def test_golden_backtest_is_reproducible(tmp_path):
    first = snapshot(run_golden(tmp_path / "a", run_id="golden_a"))
    second = snapshot(run_golden(tmp_path / "b", run_id="golden_b"))
    assert first["fingerprint"] == second["fingerprint"]
    assert first["trades_digest"] == second["trades_digest"]
