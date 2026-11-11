"""Stable request/response schemas for the public API."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from core.config import FREQUENCY_SECONDS


# --------------------------------------------------------------------------- strategies
class StrategyInfo(BaseModel):
    name: str
    description: str = ""
    default_params: dict[str, Any] = Field(default_factory=dict)
    param_schema: dict[str, Any] = Field(default_factory=dict)


class StrategyListResponse(BaseModel):
    items: list[StrategyInfo]
    total: int


# --------------------------------------------------------------------------- backtests
class BacktestCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    strategy: str = Field(..., min_length=1, description="内置策略名称")
    strategy_params: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("strategy_params", "params"),
        description="策略参数覆盖",
    )
    symbols: list[str] = Field(default_factory=list, description="标的列表，空表示数据集内全部标的")
    start: date | None = None
    end: date | None = None
    initial_cash: float = Field(1_000_000.0, gt=0)
    frequency: str = "1m"
    dataset_version: str | None = None
    seed: int = 42
    warmup_bars: int = Field(30, ge=0)
    checkpoint_every: int = Field(5000, ge=0)
    record_events: bool = True
    fees: dict[str, Any] = Field(default_factory=dict)
    matching: dict[str, Any] = Field(default_factory=dict)
    risk: dict[str, Any] = Field(default_factory=dict)

    @field_validator("frequency")
    @classmethod
    def _check_frequency(cls, value: str) -> str:
        if value not in FREQUENCY_SECONDS:
            raise ValueError(f"不支持的频率 {value}，可选: {sorted(FREQUENCY_SECONDS)}")
        return value

    @field_validator("symbols")
    @classmethod
    def _normalise_symbols(cls, value: list[str]) -> list[str]:
        return [str(s).strip().upper() for s in value if str(s).strip()]


class BacktestCreateResponse(BaseModel):
    run_id: str
    task_id: str
    status: str
    backend: str


class BacktestRunSummary(BaseModel):
    run_id: str
    task_id: str | None = None
    status: str
    strategy: str = ""
    dataset_version: str = ""
    symbols: list[str] = Field(default_factory=list)
    progress: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float | None = None
    error: str | None = None
    cancel_requested: bool = False


class BacktestRunDetail(BacktestRunSummary):
    config: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    manifest: dict[str, Any] | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    events_recorded: int = 0
    fingerprint: str = ""


class BacktestListResponse(BaseModel):
    items: list[BacktestRunSummary]
    total: int
    limit: int
    offset: int


class ArtifactResponse(BaseModel):
    run_id: str
    kind: str
    count: int
    total: int
    limit: int
    items: list[dict[str, Any]]


class CancelResponse(BaseModel):
    run_id: str
    status: str
    cancel_requested: bool
    message: str = ""


class DeleteResponse(BaseModel):
    run_id: str
    deleted: bool
    message: str = ""


# --------------------------------------------------------------------------- tasks
class TaskInfo(BaseModel):
    task_id: str
    kind: str = "backtest"
    run_id: str | None = None
    status: str
    backend: str = "local"
    progress: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float | None = None


class TaskListResponse(BaseModel):
    items: list[TaskInfo]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- data
class DatasetInfo(BaseModel):
    version: str
    frequency: str
    exists: bool = True
    rows: int = 0
    symbols: list[str] = Field(default_factory=list)
    symbol_count: int = 0
    start: str | None = None
    end: str | None = None
    manifest: dict[str, Any] | None = None


class DatasetListResponse(BaseModel):
    items: list[DatasetInfo]
    total: int
    data_root: str


class DatasetStatsResponse(BaseModel):
    version: str
    frequency: str
    exists: bool
    rows: int = 0
    symbols: list[str] = Field(default_factory=list)
    symbol_count: int = 0
    start: str | None = None
    end: str | None = None
    duplicate_bars: int = 0
    manifest: dict[str, Any] | None = None


class DataValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str | None = None
    frequency: str = "1m"
    symbols: list[str] = Field(default_factory=list)
    start: date | None = None
    end: date | None = None

    @field_validator("frequency")
    @classmethod
    def _check_frequency(cls, value: str) -> str:
        if value not in FREQUENCY_SECONDS:
            raise ValueError(f"不支持的频率 {value}")
        return value


class DataValidateResponse(BaseModel):
    version: str
    frequency: str
    valid: bool
    checked_rows: int
    symbols: int
    issues: list[str] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)


class DataBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str | None = None
    frequency: str = "1m"
    source: str = "rqdata"
    symbols: list[str] = Field(default_factory=list)
    start: date | None = None
    end: date | None = None
    seed: int = 42
    with_factors: bool = False
    overwrite: bool = False

    @field_validator("frequency")
    @classmethod
    def _check_frequency(cls, value: str) -> str:
        if value not in FREQUENCY_SECONDS:
            raise ValueError(f"不支持的频率 {value}")
        return value


class DataBuildResponse(BaseModel):
    status: str
    version: str
    result: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


# --------------------------------------------------------------------------- replay
class ReplayCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    strategy: str = Field(..., min_length=1)
    strategy_params: dict[str, Any] = Field(
        default_factory=dict, validation_alias=AliasChoices("strategy_params", "params")
    )
    symbols: list[str] = Field(default_factory=list)
    start: date | None = None
    end: date | None = None
    frequency: str = "1m"
    dataset_version: str | None = None
    initial_cash: float = Field(1_000_000.0, gt=0)
    speed: float = Field(0.0, ge=0)

    @field_validator("frequency")
    @classmethod
    def _check_frequency(cls, value: str) -> str:
        if value not in FREQUENCY_SECONDS:
            raise ValueError(f"不支持的频率 {value}")
        return value

    @field_validator("symbols")
    @classmethod
    def _normalise_symbols(cls, value: list[str]) -> list[str]:
        return [str(s).strip().upper() for s in value if str(s).strip()]


class ReplayStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    state: str
    clock: str | None = None
    seq: int = 0
    processed_events: int = 0
    processed_bars: int = 0
    pending_events: int = 0
    cursor: int = 0
    total_groups: int = 0
    speed: float = 0.0
    bars_per_second: float = 0.0
    run_id: str | None = None
    checkpoints: int = 0


class ReplayCreateResponse(BaseModel):
    session_id: str
    status: ReplayStatus


class ReplayStepRequest(BaseModel):
    events: int = Field(1, ge=1, le=100_000)


class ReplaySpeedRequest(BaseModel):
    speed: float = Field(..., ge=0)


class ReplaySeekRequest(BaseModel):
    target: datetime


class ReplayCheckpointRequest(BaseModel):
    label: str = ""


class ReplayRestoreRequest(BaseModel):
    checkpoint_id: str = Field(..., min_length=1)


class CheckpointInfo(BaseModel):
    checkpoint_id: str
    clock: str
    seq: int
    cursor: int
    label: str = ""
    created_at: str | None = None


class ReplayActionResponse(BaseModel):
    session_id: str
    status: ReplayStatus
    checkpoint: CheckpointInfo | None = None


class ReplayEventsResponse(BaseModel):
    session_id: str
    count: int
    total: int
    items: list[dict[str, Any]]


# --------------------------------------------------------------------------- accounts
class AccountSnapshot(BaseModel):
    run_id: str
    strategy: str = ""
    status: str = ""
    initial_cash: float = 0.0
    final_equity: float | None = None
    cash: float | None = None
    market_value: float | None = None
    total_return: float | None = None
    as_of: str | None = None
    position_count: int = 0


class AccountListResponse(BaseModel):
    items: list[AccountSnapshot]
    total: int


class PositionResponse(BaseModel):
    run_id: str
    as_of: str | None = None
    count: int
    items: list[dict[str, Any]]


class ErrorResponse(BaseModel):
    detail: str
