"""Account snapshots derived from completed backtest artifacts."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from apps.api.artifacts import equity_last_row, latest_positions
from apps.api.deps import SettingsDep, StoreDep
from apps.api.models import AccountListResponse, AccountSnapshot, PositionResponse
from apps.api.settings import Settings
from core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _snapshot(settings: Settings, run: dict) -> AccountSnapshot:
    metrics = run.get("metrics") or {}
    config = run.get("config") or {}
    initial_cash = float(metrics.get("initial_cash") or config.get("initial_cash") or 0.0)
    snapshot = AccountSnapshot(
        run_id=run["run_id"],
        strategy=run.get("strategy", ""),
        status=run.get("status", ""),
        initial_cash=initial_cash,
        final_equity=metrics.get("final_equity"),
        total_return=metrics.get("total_return"),
        as_of=metrics.get("end"),
    )
    if run.get("status") != "succeeded":
        return snapshot
    row = equity_last_row(settings, run["run_id"])
    if row:
        snapshot.cash = row.get("cash")
        snapshot.market_value = row.get("market_value")
        snapshot.as_of = row.get("ts") or snapshot.as_of
    try:
        positions, as_of, _ = latest_positions(settings, run["run_id"], limit=1000)
    except FileNotFoundError:
        return snapshot
    snapshot.position_count = sum(1 for item in positions if (item.get("quantity") or 0) > 0)
    snapshot.as_of = as_of or snapshot.as_of
    return snapshot


@router.get("", response_model=AccountListResponse, summary="回测账户快照")
def list_accounts(
    store: StoreDep,
    settings: SettingsDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> AccountListResponse:
    runs, total = store.list_runs(limit=limit, offset=offset)
    return AccountListResponse(items=[_snapshot(settings, run) for run in runs], total=total)


@router.get("/{run_id}/positions", response_model=PositionResponse, summary="最新持仓快照")
def get_positions(
    run_id: str,
    store: StoreDep,
    settings: SettingsDep,
    limit: int = Query(1000, ge=1, le=10_000),
) -> PositionResponse:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"回测 {run_id} 不存在")
    try:
        items, as_of, count = latest_positions(settings, run_id, limit=limit)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"回测 {run_id} 的持仓数据尚未生成（当前状态: {run.get('status')}）",
        ) from exc
    return PositionResponse(run_id=run_id, as_of=as_of, count=count, items=items)
