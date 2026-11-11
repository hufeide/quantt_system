"""Prometheus metrics exported by the worker processes.

Metric names use the quant_ prefix shared with the API so one Grafana board can
cover the whole system. The HTTP endpoint is opt-in: set QUANT_METRICS_PORT to
expose /metrics from the worker (the API already exposes its own).

Prefork workers need PROMETHEUS_MULTIPROC_DIR for the parent process to see the
counters written by child processes; when that variable is set the registry uses
the multiprocess collector automatically.
"""
from __future__ import annotations

import os
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram, start_http_server

# NOTE: the worker names are prefixed with quant_worker_ so they never collide
# with the API's own collectors when both modules are imported in one process
# (tests, or a single-container all-in-one deployment).
BARS_PROCESSED = Counter(
    "quant_worker_bars_processed",
    "Bars processed by the replay engine",
    ["strategy"],
)
BACKTEST_TOTAL = Counter(
    "quant_worker_backtest",
    "Backtest runs finished by the worker",
    ["strategy", "status"],
)
BACKTEST_DURATION = Histogram(
    "quant_worker_backtest_duration_seconds",
    "Wall clock duration of one backtest run",
    ["strategy"],
    # Must match apps.api.metrics buckets: panel 2 subtracts the API P95 from
    # the worker P95 to estimate queueing time, which needs identical buckets.
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
FACTOR_ROWS = Counter(
    "quant_factor_rows",
    "Market data rows processed by factor tasks",
)
OPTIMIZATION_RUNS = Counter(
    "quant_optimization_runs",
    "Backtest runs executed inside optimization tasks",
)

_METRICS = (BARS_PROCESSED, BACKTEST_TOTAL, BACKTEST_DURATION, FACTOR_ROWS, OPTIMIZATION_RUNS)


def record_backtest(strategy: str, bars: int, duration: float, status: str = "success") -> None:
    """Record one finished backtest (status is "success" or "error")."""
    label = strategy or "unknown"
    BACKTEST_TOTAL.labels(strategy=label, status=status).inc()
    BACKTEST_DURATION.labels(strategy=label).observe(max(float(duration), 0.0))
    if bars > 0:
        BARS_PROCESSED.labels(strategy=label).inc(bars)


def record_factor_rows(rows: int) -> None:
    if rows > 0:
        FACTOR_ROWS.inc(rows)


def record_optimization_runs(count: int) -> None:
    if count > 0:
        OPTIMIZATION_RUNS.inc(count)


def build_registry() -> CollectorRegistry:
    """Registry for the worker /metrics endpoint (multiprocess aware)."""
    registry = CollectorRegistry()
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.MultiProcessCollector(registry)
        return registry
    for metric in _METRICS:
        registry.register(metric)
    return registry


def maybe_start_metrics_server() -> Any | None:
    """Start the /metrics HTTP server when QUANT_METRICS_PORT is configured."""
    port = os.getenv("QUANT_METRICS_PORT")
    if not port:
        return None
    try:
        _, server = start_http_server(int(port), registry=build_registry())
        return server
    except (OSError, ValueError):
        return None


__all__ = [
    "build_registry",
    "maybe_start_metrics_server",
    "record_backtest",
    "record_factor_rows",
    "record_optimization_runs",
]
