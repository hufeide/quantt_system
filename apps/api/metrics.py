"""Prometheus business metrics for the backtest/replay API.

Metric names follow the deployment contract used by the Grafana dashboards
(see infrastructure/monitoring); every metric is prefixed with quant_.
"""
from __future__ import annotations

from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


def _safe(factory, *args, **kwargs):
    """Create a metric, tolerating duplicate registration in reloaded processes."""
    try:
        return factory(*args, **kwargs)
    except ValueError:  # already registered (e.g. module reloaded by a test runner)
        name = kwargs.get("name") or (args[0] if args else "")
        from prometheus_client import REGISTRY

        return REGISTRY._names_to_collectors.get(name)  # noqa: SLF001 - documented fallback


BACKTEST_RUNS = _safe(
    Counter,
    "quant_backtest_runs_total",
    "Backtest runs that reached a terminal state",
    ["status"],
)
BACKTEST_DURATION = _safe(
    Histogram,
    "quant_backtest_duration_seconds",
    "Wall-clock duration of a backtest run",
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)
BACKTEST_QUEUE_DEPTH = _safe(
    Gauge, "quant_backtest_queue_depth", "Backtest tasks waiting to start", ["queue"]
)
BARS_PROCESSED = _safe(Counter, "quant_bars_processed_total", "Bars processed by backtests")
BARS_PER_SECOND = _safe(Gauge, "quant_backtest_bars_per_second", "Bars per second of the last run")
BACKTEST_ACTIVE = _safe(Gauge, "quant_backtest_active", "Backtest runs currently executing")

DATA_LAG = _safe(Gauge, "quant_data_lag_seconds", "Seconds between dataset end and now")
DATA_MISSING_MINUTES = _safe(Gauge, "quant_data_missing_minutes", "Missing bars inside trading days")
DATA_DUPLICATE_BARS = _safe(Gauge, "quant_data_duplicate_bars", "Duplicate (symbol, ts) bars")

STRATEGY_TOTAL_RETURN = _safe(
    Gauge, "quant_strategy_total_return_pct", "Total return in percent", ["strategy_id"]
)
STRATEGY_MAX_DRAWDOWN = _safe(
    Gauge, "quant_strategy_max_drawdown_pct", "Max drawdown in percent", ["strategy_id"]
)
STRATEGY_WIN_RATE = _safe(Gauge, "quant_strategy_win_rate", "Round-trip win rate", ["strategy_id"])
STRATEGY_TURNOVER = _safe(Gauge, "quant_strategy_turnover", "Turnover ratio", ["strategy_id"])

REPLAY_SESSIONS_ACTIVE = _safe(Gauge, "quant_replay_sessions_active", "Live replay sessions")

_STATUS_LABELS = {"succeeded": "success", "failed": "failed", "cancelled": "cancelled"}


def on_submit() -> None:
    pass


def on_start() -> None:
    BACKTEST_ACTIVE.inc()


def on_progress(status: dict[str, Any]) -> None:
    speed = status.get("bars_per_second")
    if speed is not None:
        BARS_PER_SECOND.set(float(speed))


def on_finish(
    *,
    status: str,
    elapsed_seconds: float,
    bars_processed: int = 0,
    bars_per_second: float = 0.0,
    strategy: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> None:
    BACKTEST_ACTIVE.dec()
    BACKTEST_RUNS.labels(status=_STATUS_LABELS.get(status, status)).inc()
    BACKTEST_DURATION.observe(max(elapsed_seconds, 0.0))
    if bars_processed:
        BARS_PROCESSED.inc(bars_processed)
    BARS_PER_SECOND.set(float(bars_per_second))
    if strategy and metrics:
        _record_strategy(strategy, metrics)


def _record_strategy(strategy: str, metrics: dict[str, Any]) -> None:
    total_return = metrics.get("total_return")
    if total_return is not None:
        STRATEGY_TOTAL_RETURN.labels(strategy_id=strategy).set(float(total_return) * 100.0)
    max_drawdown = metrics.get("max_drawdown")
    if max_drawdown is not None:
        STRATEGY_MAX_DRAWDOWN.labels(strategy_id=strategy).set(float(max_drawdown) * 100.0)
    win_rate = metrics.get("win_rate")
    if win_rate is not None:
        STRATEGY_WIN_RATE.labels(strategy_id=strategy).set(float(win_rate) * 100.0)
    turnover = metrics.get("turnover")
    if turnover is not None:
        STRATEGY_TURNOVER.labels(strategy_id=strategy).set(float(turnover))


def set_queue_depth(depth: int) -> None:
    BACKTEST_QUEUE_DEPTH.labels(queue="backtest").set(float(max(0, depth)))


def set_data_quality(
    *,
    lag_seconds: float | None = None,
    missing_minutes: int | None = None,
    duplicate_bars: int | None = None,
) -> None:
    if lag_seconds is not None:
        DATA_LAG.set(max(0.0, float(lag_seconds)))
    if missing_minutes is not None:
        DATA_MISSING_MINUTES.set(float(missing_minutes))
    if duplicate_bars is not None:
        DATA_DUPLICATE_BARS.set(float(duplicate_bars))


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
