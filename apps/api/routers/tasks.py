"""Task status endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from apps.api.deps import StoreDep
from apps.api.models import TaskInfo, TaskListResponse

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=TaskListResponse, summary="任务列表")
def list_tasks(
    store: StoreDep,
    status: str | None = Query(None, description="按状态过滤: pending/running/succeeded/failed/cancelled"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> TaskListResponse:
    items, total = store.list_tasks(status=status, limit=limit, offset=offset)
    return TaskListResponse(items=[TaskInfo(**item) for item in items], total=total, limit=limit, offset=offset)


@router.get("/{task_id}", response_model=TaskInfo, summary="任务详情")
def get_task(task_id: str, store: StoreDep) -> TaskInfo:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    return TaskInfo(**task)
