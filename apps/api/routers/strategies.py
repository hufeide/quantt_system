"""Strategy catalogue endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from apps.api.models import StrategyInfo, StrategyListResponse
from core.strategy import list_strategies

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get("", response_model=StrategyListResponse, summary="列出内置策略与参数 schema")
def list_all_strategies() -> StrategyListResponse:
    items = [StrategyInfo(**item) for item in list_strategies()]
    return StrategyListResponse(items=items, total=len(items))
