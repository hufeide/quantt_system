"""Interactive replay session endpoints (start/pause/step/seek/checkpoint...)."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query

from apps.api.deps import ReplayDep, SettingsDep
from apps.api.models import (
    BacktestCreateRequest,
    CheckpointInfo,
    ReplayActionResponse,
    ReplayCheckpointRequest,
    ReplayCreateRequest,
    ReplayCreateResponse,
    ReplayEventsResponse,
    ReplayRestoreRequest,
    ReplaySeekRequest,
    ReplaySpeedRequest,
    ReplayStatus,
    ReplayStepRequest,
)
from apps.api.replay_manager import ReplaySessionManager
from apps.api.routers.backtests import build_config

router = APIRouter(prefix="/replay", tags=["replay"])


def _call(session_id: str, action: Callable[[], Any]) -> Any:
    try:
        return action()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"回放会话 {session_id} 不存在") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"检查点不存在：{exc}") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=f"回放操作失败：{exc}") from exc


def _action(session_id: str, status: dict[str, Any], checkpoint: CheckpointInfo | None = None) -> ReplayActionResponse:
    return ReplayActionResponse(session_id=session_id, status=ReplayStatus(**status), checkpoint=checkpoint)


@router.post("/sessions", response_model=ReplayCreateResponse, status_code=201, summary="创建回放会话")
def create_session(payload: ReplayCreateRequest, settings: SettingsDep, manager: ReplayDep) -> ReplayCreateResponse:
    base = BacktestCreateRequest(**payload.model_dump(exclude={"speed"}))
    config = build_config(base, settings, replay=True)
    try:
        session_id, session = manager.create(config)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"无法创建回放会话：{exc}") from exc
    if payload.speed > 0:
        manager.set_speed(session_id, payload.speed)
    return ReplayCreateResponse(session_id=session_id, status=ReplayStatus(**session.status()))


@router.get("/sessions/{session_id}", response_model=ReplayStatus, summary="回放会话状态")
def get_session(session_id: str, manager: ReplayDep) -> ReplayStatus:
    return ReplayStatus(**_call(session_id, lambda: manager.status(session_id)))


@router.get("/sessions/{session_id}/events", response_model=ReplayEventsResponse, summary="最近回放事件")
def get_events(
    session_id: str,
    manager: ReplayDep,
    limit: int = Query(100, ge=1, le=5000),
) -> ReplayEventsResponse:
    items, total = _call(session_id, lambda: _events(manager, session_id, limit))
    return ReplayEventsResponse(session_id=session_id, count=len(items), total=total, items=items)


def _events(manager: ReplaySessionManager, session_id: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    return manager.events(session_id, limit)


@router.post("/sessions/{session_id}/start", response_model=ReplayActionResponse, summary="开始回放")
def start_session(session_id: str, manager: ReplayDep) -> ReplayActionResponse:
    return _action(session_id, _call(session_id, lambda: manager.start(session_id)))


@router.post("/sessions/{session_id}/pause", response_model=ReplayActionResponse, summary="暂停回放")
def pause_session(session_id: str, manager: ReplayDep) -> ReplayActionResponse:
    return _action(session_id, _call(session_id, lambda: manager.pause(session_id)))


@router.post("/sessions/{session_id}/resume", response_model=ReplayActionResponse, summary="继续回放")
def resume_session(session_id: str, manager: ReplayDep) -> ReplayActionResponse:
    return _action(session_id, _call(session_id, lambda: manager.resume(session_id)))


@router.post("/sessions/{session_id}/step", response_model=ReplayActionResponse, summary="推进若干事件")
def step_session(
    session_id: str, manager: ReplayDep, payload: ReplayStepRequest | None = None
) -> ReplayActionResponse:
    events = payload.events if payload else 1
    return _action(session_id, _call(session_id, lambda: manager.step(session_id, events)))


@router.post("/sessions/{session_id}/speed", response_model=ReplayActionResponse, summary="设置回放倍速")
def set_speed(session_id: str, payload: ReplaySpeedRequest, manager: ReplayDep) -> ReplayActionResponse:
    return _action(session_id, _call(session_id, lambda: manager.set_speed(session_id, payload.speed)))


@router.post("/sessions/{session_id}/seek", response_model=ReplayActionResponse, summary="跳转到指定模拟时间")
def seek_session(session_id: str, payload: ReplaySeekRequest, manager: ReplayDep) -> ReplayActionResponse:
    return _action(session_id, _call(session_id, lambda: manager.seek(session_id, payload.target)))


@router.post("/sessions/{session_id}/checkpoint", response_model=ReplayActionResponse, summary="保存检查点")
def checkpoint_session(
    session_id: str, manager: ReplayDep, payload: ReplayCheckpointRequest | None = None
) -> ReplayActionResponse:
    label = payload.label if payload else ""
    ckpt = _call(session_id, lambda: manager.checkpoint(session_id, label))
    info = CheckpointInfo(**ckpt.to_dict())
    return _action(session_id, _call(session_id, lambda: manager.status(session_id)), checkpoint=info)


@router.post("/sessions/{session_id}/restore", response_model=ReplayActionResponse, summary="从检查点恢复")
def restore_session(session_id: str, payload: ReplayRestoreRequest, manager: ReplayDep) -> ReplayActionResponse:
    return _action(
        session_id, _call(session_id, lambda: manager.restore(session_id, payload.checkpoint_id))
    )
