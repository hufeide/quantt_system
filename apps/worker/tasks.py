"""Celery tasks: thin, JSON-serialisable wrappers around the core engines.

Design rules:

* Every task receives plain dicts / primitives so the payload survives JSON
  serialisation over RabbitMQ.
* All heavy lifting happens in core.*; the worker only orchestrates, reports
  progress and normalises the result.
* Every task binds the run_id / task_id correlation fields through
  core.logging.log_context so the logs of one run can be filtered as a unit.
"""
from __future__ import annotations

import itertools
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from apps.worker import metrics as worker_metrics
from apps.worker.celery_app import celery_app
from core.backtest.runner import BacktestResult, new_run_id, run_backtest
from core.config import BacktestConfig
from core.factor import FactorEngine
from core.logging import get_logger, log_context
from core.market.store import ParquetMarketDataStore

logger = get_logger(__name__)

DEFAULT_DATA_ROOT = "data_files"
DEFAULT_OUTPUT_ROOT = "data_files/backtests"


# --------------------------------------------------------------------------- helpers
def _is_eager(task) -> bool:
    return bool(getattr(task.request, "is_eager", False))


def _progress_reporter(task, run_id: str):
    """Build a progress callback that mirrors engine status into task state."""

    def report(status: dict[str, Any]) -> None:
        if _is_eager(task):
            return
        total = int(status.get("total_groups") or 0)
        cursor = int(status.get("cursor") or 0)
        task.update_state(
            state="PROGRESS",
            meta={
                "run_id": run_id,
                "percent": round(100.0 * cursor / total, 2) if total else 0.0,
                "clock": status.get("clock"),
                "processed_events": status.get("processed_events"),
                "processed_bars": status.get("processed_bars"),
            },
        )

    return report


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _metric_value(result: dict[str, Any], metric: str) -> float:
    metrics = result.get("metrics") or {}
    value = metrics.get(metric)
    return _as_float(value, float("-inf"))


def _expand_grid(grid: dict[str, Sequence[Any]]) -> list[dict[str, Any]]:
    """Expand a {param: [values]} grid into a list of parameter combinations."""
    if not grid:
        return [{}]
    keys = list(grid)
    values = [list(grid[key]) for key in keys]
    for key, options in zip(keys, values, strict=False):
        if not options:
            raise ValueError(f"grid parameter '{key}' has no candidate values")
    return [dict(zip(keys, combo, strict=False)) for combo in itertools.product(*values)]


def _writable_root(path: str | None, fallback: str) -> str:
    """Use the incoming root only when this worker can actually write there.

    The API and the worker may run in different mount namespaces (a host path in
    the payload does not necessarily exist inside the worker container). When the
    payload path is unusable we fall back to this worker's own configured root,
    which is the one shared with the API in a real deployment.
    """
    candidate = path or fallback
    try:
        Path(candidate).mkdir(parents=True, exist_ok=True)
        probe = Path(candidate) / ".write_probe"
        probe.touch()
        probe.unlink()
        return candidate
    except OSError:
        logger.warning("root %s not usable in this worker, falling back to %s", candidate, fallback)
        Path(fallback).mkdir(parents=True, exist_ok=True)
        return fallback


def _prepare_payload(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Resolve the data / output roots inside the worker's own namespace."""
    payload = dict(config)
    data_root = _writable_root(payload.get("data_root"), os.getenv("QR_DATA_ROOT") or DEFAULT_DATA_ROOT)
    output_root = _writable_root(payload.get("output_root"), os.getenv("QR_OUTPUT_ROOT") or DEFAULT_OUTPUT_ROOT)
    payload["data_root"] = data_root
    payload["output_root"] = output_root
    payload["run_id"] = run_id
    return payload


def _run_backtest_sync(config: BacktestConfig, run_id: str, progress=None) -> BacktestResult:
    config.run_id = run_id
    return run_backtest(config, progress=progress)


def _serialise(result: BacktestResult) -> dict[str, Any]:
    payload = result.to_dict()
    payload["artifact_count"] = len(result.artifacts)
    payload["output_dir"] = str(Path(result.artifacts.get("metrics.json", "")).parent) if result.artifacts else ""
    return payload


# --------------------------------------------------------------------------- backtest
@celery_app.task(
    bind=True,
    name="quant.run_backtest",
    autoretry_for=(Exception,),
    max_retries=2,
    retry_backoff=True,
    retry_jitter=True,
)
def run_backtest_task(self, config: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    """Run one backtest and return its metrics plus artifact paths."""
    run_id = run_id or new_run_id()
    with log_context(run_id=run_id, task_id=self.request.id, event_type="backtest"):
        logger.info("starting backtest %s strategy=%s", run_id, config.get("strategy"))
        cfg = BacktestConfig.from_dict(_prepare_payload(config, run_id))
        reporter = _progress_reporter(self, run_id)
        last_status: dict[str, Any] = {}

        def progress(status: dict[str, Any]) -> None:
            last_status.update(status)
            reporter(status)

        try:
            result = _run_backtest_sync(cfg, run_id, progress=progress)
        except Exception:
            worker_metrics.record_backtest(cfg.strategy, bars=0, duration=0.0, status="error")
            raise
        worker_metrics.record_backtest(
            cfg.strategy,
            bars=int(last_status.get("processed_bars") or 0),
            duration=result.elapsed_seconds,
        )
        logger.info(
            "finished backtest %s trades=%s sharpe=%s",
            run_id,
            result.metrics.get("trade_count"),
            result.metrics.get("sharpe"),
        )
        return _serialise(result)


# --------------------------------------------------------------------------- factors
@celery_app.task(
    bind=True,
    name="quant.compute_factors",
    autoretry_for=(Exception,),
    dont_autoretry_for=(ValueError, KeyError),
    max_retries=2,
)
def compute_factors_task(
    self,
    dataset_version: str,
    factor_names: list[str] | None = None,
    symbols: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    data_root: str = DEFAULT_DATA_ROOT,
    frequency: str = "1m",
    params: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute factor values for a dataset version and persist them as Parquet."""
    with log_context(task_id=self.request.id, event_type="factor"):
        store = ParquetMarketDataStore(data_root, dataset_version, frequency)
        if not store.exists():
            raise FileNotFoundError(f"dataset {dataset_version} not found under {data_root}")
        bars = store.load_frame(symbols=symbols or None, start=start, end=end)
        engine = FactorEngine(root=data_root, version=dataset_version)
        summary = engine.compute_and_store(bars, factor_names=factor_names, params=params)
        summary.update(
            {
                "dataset_version": dataset_version,
                "frequency": frequency,
                "rows_processed": int(len(bars)),
                "symbols": sorted(set(bars["symbol"])) if not bars.empty else [],
            }
        )
        worker_metrics.record_factor_rows(int(summary["rows_processed"]))
        logger.info("computed factors %s rows=%s", summary.get("factors"), summary["rows_processed"])
        return summary


# --------------------------------------------------------------------------- optimization
@celery_app.task(
    bind=True,
    name="quant.optimize",
    autoretry_for=(Exception,),
    dont_autoretry_for=(ValueError, KeyError, TypeError),
    max_retries=1,
)
def optimize_task(
    self,
    config: dict[str, Any],
    grid: dict[str, Sequence[Any]],
    metric: str = "sharpe",
    max_workers: int = 1,
) -> dict[str, Any]:
    """Grid-search strategy parameters, ranking runs by a metric (default sharpe)."""
    combos = _expand_grid(grid or {})
    base_run_id = config.get("run_id") or new_run_id()
    with log_context(run_id=base_run_id, task_id=self.request.id, event_type="optimization"):
        logger.info("optimization over %s combinations, metric=%s", len(combos), metric)

        def _one(index: int, combo: dict[str, Any]) -> dict[str, Any]:
            params = {k: v for k, v in combo.items() if k != "strategy"}
            payload = dict(config)
            if combo.get("strategy"):
                payload["strategy"] = combo["strategy"]
            payload["strategy_params"] = {**(payload.get("strategy_params") or {}), **params}
            run_id = f"opt_{base_run_id}_{index:03d}"
            cfg = BacktestConfig.from_dict(_prepare_payload(payload, run_id))
            result = _run_backtest_sync(cfg, run_id)
            return {
                "run_id": result.run_id,
                "params": {k: v for k, v in combo.items()},
                "strategy": cfg.strategy,
                "metrics": result.metrics,
                "artifacts": result.artifacts,
                "elapsed_seconds": round(result.elapsed_seconds, 4),
            }

        results: list[dict[str, Any]] = []
        if max_workers and max_workers > 1:
            with ThreadPoolExecutor(max_workers=int(max_workers)) as pool:
                futures = [pool.submit(_one, i, combo) for i, combo in enumerate(combos)]
                for done, future in enumerate(futures, start=1):
                    results.append(future.result())
                    if not _is_eager(self):
                        self.update_state(
                            state="PROGRESS",
                            meta={"run_id": base_run_id, "completed": done, "total": len(combos)},
                        )
        else:
            for i, combo in enumerate(combos):
                results.append(_one(i, combo))
                if not _is_eager(self):
                    self.update_state(
                        state="PROGRESS",
                        meta={"run_id": base_run_id, "completed": i + 1, "total": len(combos)},
                    )

        results.sort(key=lambda item: _metric_value(item, metric), reverse=True)
        worker_metrics.record_optimization_runs(len(results))
        best = results[0] if results else None
        return {
            "base_run_id": base_run_id,
            "metric": metric,
            "grid": {k: list(v) for k, v in (grid or {}).items()},
            "count": len(results),
            "best": best,
            "results": results,
        }


# --------------------------------------------------------------------------- maintenance
def _validate_dataset(data_root: str, version: str, frequency: str = "1m") -> dict[str, Any]:
    """Structural validation of one dataset version (manifest + OHLC sanity)."""
    store = ParquetMarketDataStore(data_root, version, frequency)
    report: dict[str, Any] = {
        "dataset_version": version,
        "frequency": frequency,
        "ok": False,
        "rows": 0,
        "symbols": [],
        "start": None,
        "end": None,
        "errors": [],
        "warnings": [],
        "manifest": None,
    }
    if not store.exists():
        report["errors"].append(f"dataset {version} not found at {store.dataset_dir}")
        return report

    manifest = store.manifest()
    if manifest is not None:
        report["manifest"] = manifest.to_dict()
    else:
        report["warnings"].append("manifest.json missing")

    try:
        frame = store.load_frame()
    except Exception as exc:  # corrupted parquet etc.
        report["errors"].append(f"failed to read dataset: {exc}")
        return report

    report["rows"] = int(len(frame))
    if frame.empty:
        report["errors"].append("dataset is empty")
        return report

    frame = frame.copy()
    frame["ts"] = pd.to_datetime(frame["ts"])
    report["symbols"] = sorted(frame["symbol"].unique().tolist())
    report["start"] = frame["ts"].min().isoformat()
    report["end"] = frame["ts"].max().isoformat()

    required = ["symbol", "ts", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        report["errors"].append(f"missing columns: {missing}")
        return report

    if frame[required].isna().any().any():
        nulls = frame[required].isna().sum()
        report["errors"].append(f"null values: {nulls[nulls > 0].to_dict()}")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        report["errors"].append("non-positive prices found")
    if (frame["high"] < frame["low"]).any():
        report["errors"].append("high < low rows found")
    if (frame["volume"] < 0).any():
        report["errors"].append("negative volume rows found")
    duplicates = int(frame.duplicated(subset=["symbol", "ts"]).sum())
    if duplicates:
        report["errors"].append(f"{duplicates} duplicated (symbol, ts) rows")

    if manifest is not None and manifest.row_count and int(manifest.row_count) != int(len(frame)):
        report["warnings"].append(
            f"manifest row_count={manifest.row_count} differs from parquet rows={len(frame)}"
        )

    report["ok"] = not report["errors"]
    return report


def _cleanup_runs(output_root: str, older_than_days: int, dry_run: bool = True) -> dict[str, Any]:
    """Delete run directories older than N days (dry run by default)."""
    root = Path(output_root)
    cutoff = time.time() - max(0, int(older_than_days)) * 86_400
    candidates: list[str] = []
    deleted: list[str] = []
    if root.exists():
        for run_dir in sorted(root.glob("run_id=*")):
            if not run_dir.is_dir():
                continue
            if run_dir.stat().st_mtime >= cutoff:
                continue
            candidates.append(run_dir.name)
            if not dry_run:
                shutil.rmtree(run_dir, ignore_errors=True)
                deleted.append(run_dir.name)
    return {
        "kind": "cleanup",
        "output_root": str(root),
        "older_than_days": int(older_than_days),
        "dry_run": bool(dry_run),
        "candidates": candidates,
        "deleted": deleted,
        "freed_runs": len(deleted),
    }


def _prune_checkpoints(output_root: str, keep: int = 10, dry_run: bool = True) -> dict[str, Any]:
    """Keep only the newest N checkpoints per run directory."""
    root = Path(output_root)
    removed: list[str] = []
    if root.exists():
        for run_dir in sorted(root.glob("run_id=*")):
            store_dir = run_dir / "checkpoints"
            if not store_dir.is_dir():
                continue
            files = sorted(store_dir.glob("ckpt_*.json"))
            for old in files[: max(0, len(files) - int(keep))]:
                removed.append(str(old))
                if not dry_run:
                    old.unlink(missing_ok=True)
    return {
        "kind": "prune_checkpoints",
        "output_root": str(root),
        "keep": int(keep),
        "dry_run": bool(dry_run),
        "removed": removed,
        "removed_count": len(removed),
    }


@celery_app.task(
    bind=True,
    name="quant.maintenance",
    autoretry_for=(Exception,),
    dont_autoretry_for=(ValueError, KeyError, TypeError),
    max_retries=1,
)
def maintenance_task(
    self,
    kind: str = "validate",
    dataset_version: str | None = None,
    data_root: str = DEFAULT_DATA_ROOT,
    output_root: str = DEFAULT_OUTPUT_ROOT,
    frequency: str = "1m",
    older_than_days: int = 30,
    keep_checkpoints: int = 10,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Housekeeping: dataset validation, run cleanup and checkpoint pruning."""
    with log_context(task_id=self.request.id, event_type="maintenance"):
        kind = (kind or "validate").lower()
        if kind == "validate":
            versions = [dataset_version] if dataset_version else _dataset_versions(data_root)
            reports = [_validate_dataset(data_root, version, frequency) for version in versions]
            return {
                "kind": "validate",
                "data_root": data_root,
                "ok": all(report["ok"] for report in reports) if reports else False,
                "checked": len(reports),
                "reports": reports,
            }
        if kind == "cleanup":
            return _cleanup_runs(output_root, older_than_days, dry_run=dry_run)
        if kind in {"prune_checkpoints", "prune"}:
            return _prune_checkpoints(output_root, keep=keep_checkpoints, dry_run=dry_run)
        raise ValueError(f"unknown maintenance kind '{kind}' (validate|cleanup|prune_checkpoints)")


def _dataset_versions(data_root: str) -> list[str]:
    root = Path(data_root) / "normalized"
    if not root.exists():
        return []
    return sorted(path.name.split("=", 1)[-1] for path in root.glob("dataset=*") if path.is_dir())


__all__ = [
    "compute_factors_task",
    "maintenance_task",
    "optimize_task",
    "run_backtest_task",
]
