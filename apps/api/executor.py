"""Backtest execution: Celery when available, otherwise a local thread pool."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from apps.api import metrics
from apps.api.settings import Settings
from apps.api.store import MetadataStore, utcnow
from core.backtest.runner import run_backtest
from core.config import BacktestConfig
from core.logging import get_logger

logger = get_logger(__name__)


class JobCancelled(Exception):
    """Raised inside the progress callback to cooperatively stop a local run."""


class BacktestJobManager:
    """Submits backtest jobs and keeps task/run metadata in sync."""

    def __init__(self, settings: Settings, store: MetadataStore) -> None:
        self._settings = settings
        self._store = store
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(settings.max_workers)), thread_name_prefix="qr-backtest"
        )
        self._lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._celery_checked = False
        self._celery_ok = False
        self._celery_task: Callable[..., Any] | None = None

    # ------------------------------------------------------------------ backend selection
    def _celery_available(self) -> bool:
        if not self._settings.celery_broker_url:
            return False
        if self._celery_checked:
            return self._celery_ok
        with self._lock:
            if self._celery_checked:
                return self._celery_ok
            self._celery_checked = True
            try:
                from kombu import Connection

                with Connection(self._settings.celery_broker_url, connect_timeout=0.5) as connection:
                    connection.ensure_connection(max_retries=0, timeout=0.5)
                from apps.worker.tasks import run_backtest_task  # noqa: PLC0415 - optional dependency

                self._celery_task = run_backtest_task
                self._celery_ok = True
                logger.info("backtests will be dispatched through Celery")
            except Exception as exc:
                logger.warning("Celery unavailable (%s); using the local thread pool", exc)
                self._celery_ok = False
        return self._celery_ok

    def submit(self, run_id: str, task_id: str, config: BacktestConfig) -> str:
        """Queue a backtest and return the backend actually used."""
        self._refresh_queue_depth()
        if self._celery_available():
            try:
                assert self._celery_task is not None
                async_result = self._celery_task.delay(config=config.to_dict(), run_id=run_id)
                celery_id = getattr(async_result, "id", None)
                self._store.update_task(task_id, backend="celery", celery_task_id=celery_id)
                self._store.update_run(run_id, backend="celery", celery_task_id=celery_id)
                self._pool.submit(self._monitor_celery, run_id, task_id, async_result)
                self._refresh_queue_depth()
                return "celery"
            except Exception as exc:  # dispatch failure must not lose the job
                logger.warning("Celery dispatch failed (%s); falling back to local execution", exc)
        self._store.update_task(task_id, backend="local")
        self._store.update_run(run_id, backend="local")
        self._pool.submit(self._run_local, run_id, task_id, config)
        self._refresh_queue_depth()
        return "local"

    # ------------------------------------------------------------------ local execution
    def _run_local(self, run_id: str, task_id: str, config: BacktestConfig) -> None:
        cancel = threading.Event()
        with self._lock:
            self._cancel_events[run_id] = cancel
        task = self._store.get_task(task_id) or {}
        if task.get("cancel_requested"):
            self._mark_cancelled(run_id, task_id, "任务在开始前已被取消")
            self._forget(run_id)
            return

        started = time.perf_counter()
        self._store.update_task(task_id, status="running", started_at=utcnow())
        self._store.update_run(run_id, status="running", started_at=utcnow())
        if (self._store.get_task(task_id) or {}).get("cancel_requested"):
            # Cancelled between the queue check and the status flip.
            self._mark_cancelled(run_id, task_id, "任务在开始前已被取消")
            self._forget(run_id)
            return
        metrics.on_start()
        self._refresh_queue_depth()
        last: dict[str, Any] = {"processed_bars": 0, "processed_events": 0, "bars_per_second": 0.0}

        def progress(status: dict[str, Any]) -> None:
            if cancel.is_set():
                raise JobCancelled()
            last.update(
                {
                    "processed_bars": int(status.get("processed_bars", 0) or 0),
                    "processed_events": int(status.get("processed_events", 0) or 0),
                    "bars_per_second": float(status.get("bars_per_second", 0.0) or 0.0),
                }
            )
            self._store.update_task(task_id, progress=dict(status))
            self._store.update_run(run_id, progress=dict(status))
            metrics.on_progress(status)

        try:
            result = run_backtest(config, output_root=self._settings.output_root, progress=progress)
            elapsed = time.perf_counter() - started
            self._store.update_run(
                run_id,
                status="succeeded",
                metrics=result.metrics,
                manifest=result.manifest.to_dict(),
                artifacts=result.artifacts,
                events_recorded=result.events_recorded,
                fingerprint=result.fingerprint,
                elapsed_seconds=round(elapsed, 4),
                finished_at=utcnow(),
                error="; ".join(result.errors)[:2000] if result.errors else None,
            )
            self._store.update_task(
                task_id,
                status="succeeded",
                progress={
                    "processed_bars": last["processed_bars"],
                    "processed_events": last["processed_events"],
                    "bars_per_second": last["bars_per_second"],
                },
                elapsed_seconds=round(elapsed, 4),
                finished_at=utcnow(),
                error=None,
            )
            metrics.on_finish(
                status="succeeded",
                elapsed_seconds=elapsed,
                bars_processed=last["processed_bars"],
                bars_per_second=last["bars_per_second"],
                strategy=config.strategy,
                metrics=result.metrics,
            )
            logger.info("backtest %s finished in %.2fs", run_id, elapsed)
        except JobCancelled:
            self._mark_cancelled(run_id, task_id, "任务已按请求取消")
            metrics.on_finish(
                status="cancelled",
                elapsed_seconds=time.perf_counter() - started,
                bars_processed=last["processed_bars"],
                bars_per_second=last["bars_per_second"],
            )
        except Exception as exc:
            elapsed = time.perf_counter() - started
            logger.exception("backtest %s failed", run_id)
            message = f"{type(exc).__name__}: {exc}"[:2000]
            self._store.update_run(
                run_id, status="failed", error=message, finished_at=utcnow(), elapsed_seconds=round(elapsed, 4)
            )
            self._store.update_task(
                task_id, status="failed", error=message, finished_at=utcnow(), elapsed_seconds=round(elapsed, 4)
            )
            metrics.on_finish(
                status="failed",
                elapsed_seconds=elapsed,
                bars_processed=last["processed_bars"],
                bars_per_second=last["bars_per_second"],
            )
        finally:
            self._forget(run_id)

    def _mark_cancelled(self, run_id: str, task_id: str, message: str) -> None:
        self._store.update_run(run_id, status="cancelled", error=message, finished_at=utcnow())
        self._store.update_task(task_id, status="cancelled", error=message, finished_at=utcnow())
        logger.info("backtest %s cancelled: %s", run_id, message)

    def _forget(self, run_id: str) -> None:
        with self._lock:
            self._cancel_events.pop(run_id, None)
        self._refresh_queue_depth()

    def _refresh_queue_depth(self) -> None:
        try:
            _, pending = self._store.list_tasks(status="pending", limit=1)
            metrics.set_queue_depth(pending)
        except Exception:  # pragma: no cover - metrics must never break a job
            pass

    # ------------------------------------------------------------------ celery monitoring
    def _monitor_celery(self, run_id: str, task_id: str, async_result: Any) -> None:
        """Mirror Celery task state into the metadata store.

        The worker process has its own memory, so the API polls the result
        backend instead of expecting the worker to write our store.
        """
        started = time.perf_counter()
        metrics.on_start()
        state = "PENDING"
        try:
            deadline = time.time() + 24 * 3600
            while time.time() < deadline:
                state = async_result.state
                if state not in {"PENDING", "RECEIVED", "STARTED", "RETRY", "PROGRESS"}:
                    break
                info = async_result.info if isinstance(async_result.info, dict) else {}
                if info:
                    progress = {
                        "processed_events": info.get("processed_events", 0),
                        "processed_bars": info.get("processed_bars", 0),
                        "clock": info.get("clock"),
                        "percent": info.get("percent", 0.0),
                    }
                    self._store.update_task(task_id, progress=progress)
                    self._store.update_run(run_id, progress=progress)
                time.sleep(0.5)
            elapsed = time.perf_counter() - started
            if state == "SUCCESS":
                payload = async_result.result or {}
                self._store.update_run(
                    run_id,
                    status="succeeded",
                    metrics=payload.get("metrics") or {},
                    manifest=payload.get("manifest"),
                    artifacts=payload.get("artifacts") or {},
                    events_recorded=int(payload.get("events_recorded", 0) or 0),
                    fingerprint=payload.get("fingerprint", ""),
                    elapsed_seconds=round(elapsed, 4),
                    finished_at=utcnow(),
                    error="; ".join(payload.get("errors") or [])[:2000] or None,
                )
                self._store.update_task(
                    task_id, status="succeeded", finished_at=utcnow(), elapsed_seconds=round(elapsed, 4), error=None
                )
                run = self._store.get_run(run_id) or {}
                metrics.on_finish(
                    status="succeeded",
                    elapsed_seconds=elapsed,
                    strategy=run.get("strategy"),
                    metrics=payload.get("metrics") or {},
                )
            elif state == "REVOKED":
                self._mark_cancelled(run_id, task_id, "Celery 任务已撤销")
                metrics.on_finish(status="cancelled", elapsed_seconds=elapsed)
            else:
                message = f"Celery 任务失败（{state}）：{async_result.result}"[:2000]
                self._store.update_run(
                    run_id, status="failed", error=message, finished_at=utcnow(), elapsed_seconds=round(elapsed, 4)
                )
                self._store.update_task(
                    task_id, status="failed", error=message, finished_at=utcnow(), elapsed_seconds=round(elapsed, 4)
                )
                metrics.on_finish(status="failed", elapsed_seconds=elapsed)
        except Exception as exc:  # pragma: no cover - broker/backend dependent
            logger.exception("lost track of Celery task for run %s", run_id)
            self._store.update_run(run_id, status="failed", error=f"Celery 监控失败：{exc}"[:2000], finished_at=utcnow())
            self._store.update_task(task_id, status="failed", error=f"Celery 监控失败：{exc}"[:2000], finished_at=utcnow())
            metrics.on_finish(status="failed", elapsed_seconds=time.perf_counter() - started)

    # ------------------------------------------------------------------ cancellation
    def cancel(self, run_id: str, task_id: str | None) -> dict[str, Any]:
        """Request cancellation and return the refreshed run document."""
        run = self._store.get_run(run_id) or {}
        if run.get("status") in {"succeeded", "failed", "cancelled"}:
            return run
        if task_id:
            self._store.update_task(task_id, cancel_requested=True)
        run = self._store.update_run(run_id, cancel_requested=True) or run
        if run.get("status") == "pending":
            # Never started: no worker will pick it up locally.
            if task_id:
                self._store.update_task(task_id, status="cancelled", cancel_requested=True, finished_at=utcnow())
            run = self._store.update_run(
                run_id, status="cancelled", cancel_requested=True, finished_at=utcnow(), error="任务在开始前被取消"
            ) or run
            return run
        with self._lock:
            event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        elif self._celery_ok:
            celery_id = run.get("celery_task_id") or (self._store.get_task(task_id) or {}).get("celery_task_id")
            try:
                from celery.result import AsyncResult

                if celery_id:
                    AsyncResult(celery_id).revoke(terminate=False)
            except Exception as exc:  # pragma: no cover - broker dependent
                logger.warning("failed to revoke Celery task %s: %s", task_id, exc)
        return run

    # ------------------------------------------------------------------ lifecycle
    def shutdown(self) -> None:
        with self._lock:
            for event in self._cancel_events.values():
                event.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
