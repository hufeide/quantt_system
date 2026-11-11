"""Backtest lifecycle endpoints: create, inspect, read artifacts, cancel, delete."""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from apps.api.artifacts import read_artifact, run_dir
from apps.api.deps import ExecutorDep, SettingsDep, StoreDep
from apps.api.models import (
    ArtifactResponse,
    BacktestCreateRequest,
    BacktestCreateResponse,
    BacktestListResponse,
    BacktestRunDetail,
    BacktestRunSummary,
    CancelResponse,
    DeleteResponse,
)
from apps.api.settings import Settings
from apps.api.store import KNOWN_STATUSES, TERMINAL_STATUSES, utcnow
from core.backtest.runner import new_run_id
from core.config import BacktestConfig
from core.logging import get_logger
from core.market.store import ParquetMarketDataStore
from core.strategy import registry_names

logger = get_logger(__name__)

router = APIRouter(prefix="/backtests", tags=["backtests"])

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def build_config(payload: BacktestCreateRequest, settings: Settings, *, replay: bool = False) -> BacktestConfig:
    """Translate a validated request into a core BacktestConfig."""
    available = registry_names()
    if payload.strategy not in available:
        raise HTTPException(
            status_code=400,
            detail=f"未知策略 '{payload.strategy}'，可用策略: {', '.join(available)}",
        )
    data: dict[str, Any] = payload.model_dump()
    data["data_root"] = settings.data_root
    data["output_root"] = settings.output_root
    data["dataset_version"] = payload.dataset_version or settings.dataset_version
    if replay:
        data["record_events"] = False
        data["checkpoint_every"] = 0
    try:
        config = BacktestConfig.from_dict(data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"回测参数无效：{exc}") from exc
    if config.start and config.end and config.start > config.end:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")

    store = ParquetMarketDataStore(settings.data_root, config.dataset_version, config.frequency)
    if not store.exists():
        raise HTTPException(
            status_code=400,
            detail=f"数据集 {config.dataset_version}（{config.frequency}）不存在，请先构建行情数据",
        )
    return config


def _require_run(store, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"回测 {run_id} 不存在")
    return run


@router.post("", response_model=BacktestCreateResponse, status_code=202, summary="创建回测任务")
def create_backtest(
    payload: BacktestCreateRequest,
    store: StoreDep,
    settings: SettingsDep,
    executor: ExecutorDep,
) -> BacktestCreateResponse:
    config = build_config(payload, settings)
    run_id = new_run_id()
    task_id = uuid.uuid4().hex
    # The runner must write artifacts under the run_id exposed by the API.
    config.run_id = run_id

    store.create_run(
        {
            "run_id": run_id,
            "task_id": task_id,
            "kind": "backtest",
            "status": "pending",
            "strategy": config.strategy,
            "dataset_version": config.dataset_version,
            "symbols": list(config.symbols),
            "config": config.to_dict(),
            "progress": {},
            "metrics": {},
            "manifest": None,
            "artifacts": {},
            "events_recorded": 0,
            "fingerprint": "",
            "error": None,
            "elapsed_seconds": None,
            "cancel_requested": False,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
        }
    )
    store.create_task(
        {
            "task_id": task_id,
            "kind": "backtest",
            "run_id": run_id,
            "status": "pending",
            "backend": "local",
            "progress": {},
            "error": None,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "elapsed_seconds": None,
        }
    )
    backend = executor.submit(run_id, task_id, config)
    run = store.get_run(run_id) or {}
    logger.info("backtest %s queued (backend=%s, strategy=%s)", run_id, backend, config.strategy)
    return BacktestCreateResponse(run_id=run_id, task_id=task_id, status=run.get("status", "pending"), backend=backend)


@router.get("", response_model=BacktestListResponse, summary="回测列表（分页 / 状态过滤）")
def list_backtests(
    store: StoreDep,
    status: str | None = Query(None, description="按状态过滤"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> BacktestListResponse:
    if status and status not in KNOWN_STATUSES:
        raise HTTPException(status_code=400, detail=f"未知状态 '{status}'，可用: {', '.join(sorted(KNOWN_STATUSES))}")
    items, total = store.list_runs(status=status, limit=limit, offset=offset)
    return BacktestListResponse(
        items=[BacktestRunSummary(**item) for item in items], total=total, limit=limit, offset=offset
    )


@router.get("/{run_id}", response_model=BacktestRunDetail, summary="回测状态、指标与 manifest")
def get_backtest(run_id: str, store: StoreDep) -> BacktestRunDetail:
    run = _require_run(store, run_id)
    return BacktestRunDetail(**run)


@router.get("/{run_id}/{kind}", response_model=ArtifactResponse, summary="读取回测产物（equity/trades/orders/positions/events）")
def get_artifact(
    run_id: str,
    kind: Literal["equity", "trades", "orders", "positions", "events"],
    store: StoreDep,
    settings: SettingsDep,
    limit: int = Query(1000, ge=1, le=200_000),
) -> ArtifactResponse:
    run = _require_run(store, run_id)
    try:
        items, total = read_artifact(settings, run_id, kind, limit=limit)
    except FileNotFoundError as exc:
        status = run.get("status", "unknown")
        raise HTTPException(
            status_code=404,
            detail=f"回测 {run_id} 的 {kind} 数据尚未生成（当前状态: {status}）",
        ) from exc
    return ArtifactResponse(run_id=run_id, kind=kind, count=len(items), total=total, limit=limit, items=items)


@router.post("/{run_id}/cancel", response_model=CancelResponse, summary="取消回测")
def cancel_backtest(run_id: str, store: StoreDep, executor: ExecutorDep) -> CancelResponse:
    run = _require_run(store, run_id)
    if run.get("status") in TERMINAL_STATUSES:
        return CancelResponse(
            run_id=run_id,
            status=run["status"],
            cancel_requested=bool(run.get("cancel_requested")),
            message="任务已结束，无需取消",
        )
    updated = executor.cancel(run_id, run.get("task_id"))
    return CancelResponse(
        run_id=run_id,
        status=updated.get("status", run.get("status", "unknown")),
        cancel_requested=bool(updated.get("cancel_requested")),
        message="已发送取消请求",
    )


@router.delete("/{run_id}", response_model=DeleteResponse, summary="删除回测及其产物")
def delete_backtest(run_id: str, store: StoreDep, settings: SettingsDep, executor: ExecutorDep) -> DeleteResponse:
    if not _RUN_ID_PATTERN.match(run_id):
        raise HTTPException(status_code=400, detail="非法的 run_id")
    run = _require_run(store, run_id)
    if run.get("status") == "running":
        raise HTTPException(status_code=409, detail="回测正在运行，请先取消再删除")
    if run.get("status") == "pending":
        executor.cancel(run_id, run.get("task_id"))

    store.delete_run(run_id)
    root = Path(settings.output_root).resolve()
    target = run_dir(settings, run_id).resolve()
    if target.is_relative_to(root) and target.exists():
        shutil.rmtree(target, ignore_errors=True)
    logger.info("backtest %s deleted", run_id)
    return DeleteResponse(run_id=run_id, deleted=True, message="回测及其产物已删除")
