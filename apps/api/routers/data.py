"""Market data catalogue, statistics, validation and dataset building."""
from __future__ import annotations

import importlib
import inspect
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from apps.api import metrics as api_metrics
from apps.api.deps import SettingsDep
from apps.api.models import (
    DataBuildRequest,
    DataBuildResponse,
    DatasetInfo,
    DatasetListResponse,
    DatasetStatsResponse,
    DataValidateRequest,
    DataValidateResponse,
)
from apps.api.settings import Settings
from core.logging import get_logger
from core.market.store import ParquetMarketDataStore
from core.storage.dataset_manifest import DatasetManifest

logger = get_logger(__name__)

router = APIRouter(prefix="/data", tags=["data"])

_BUILDER_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("data.pipeline", "build_pipeline"),
    ("data.build", "build_dataset"),
    ("data.pipeline", "build_dataset"),
    ("data.storage.builder", "build_dataset"),
    ("data.ingestion.builder", "build_dataset"),
)


def _resolve_builder() -> Callable[..., Any] | None:
    for module_name, attr in _BUILDER_CANDIDATES:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        builder = getattr(module, attr, None)
        if callable(builder):
            return builder
    return None


def _dataset_quality_metrics(frame: pd.DataFrame, frequency: str) -> tuple[int | None, int | None]:
    """Read missing-minute / duplicate counts from the data layer validator.

    The validator understands A-share sessions, so lunch breaks are not counted
    as missing bars. Returns (None, None) when the data package is unavailable.
    """
    try:
        from data.validation.checks import validate_bars

        report = validate_bars(frame, frequency=frequency, raise_on_error=False)
    except Exception:
        return None, None
    checks = report.get("checks") or {}
    missing = checks.get("missing_minutes") or {}
    duplicates = checks.get("duplicates") or {}
    return int(missing.get("count") or 0), int(duplicates.get("count") or 0)


def _observe_lag(end: str | None) -> None:
    if not end:
        return
    try:
        lag = (datetime.now() - pd.Timestamp(end).to_pydatetime()).total_seconds()
        api_metrics.set_data_quality(lag_seconds=lag)
    except Exception:  # pragma: no cover - defensive
        pass


def _serialize_result(result: Any) -> dict[str, Any]:
    """Turn a builder return value into a compact, JSON-safe summary."""
    if isinstance(result, dict):
        return json.loads(json.dumps(result, default=str))
    summary: dict[str, Any] = {}
    for key in ("dataset_dir", "rows", "ok", "report", "manifest", "manifest_path", "report_path"):
        if not hasattr(result, key):
            continue
        value = getattr(result, key)
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        elif isinstance(value, Path):
            value = str(value)
        summary[key] = value
    if hasattr(result, "raw_files"):
        summary["raw_file_count"] = len(result.raw_files or [])
    if not summary:
        return {"result": str(result)}
    return json.loads(json.dumps(summary, default=str))


def _manifest_for(root: Path, version: str) -> DatasetManifest | None:
    path = root / "normalized" / f"dataset={version}" / "manifest.json"
    if not path.exists():
        return None
    try:
        return DatasetManifest.load(path)
    except Exception as exc:  # pragma: no cover - corrupt manifest
        logger.warning("failed to load dataset manifest %s: %s", path, exc)
        return None


def _dataset_info(settings: Settings, version: str, frequency: str, manifest: DatasetManifest | None) -> DatasetInfo:
    store = ParquetMarketDataStore(settings.data_root, version, frequency)
    if manifest is not None and manifest.frequency == frequency:
        return DatasetInfo(
            version=version,
            frequency=frequency,
            exists=store.exists(),
            rows=int(manifest.row_count or 0),
            symbols=list(manifest.symbols),
            symbol_count=len(manifest.symbols),
            start=manifest.start,
            end=manifest.end,
            manifest=manifest.to_dict(),
        )
    stats = store.stats()
    symbols = store.symbols()
    return DatasetInfo(
        version=version,
        frequency=frequency,
        exists=bool(stats.get("exists")),
        rows=int(stats.get("rows", 0) or 0),
        symbols=symbols,
        symbol_count=len(symbols),
        start=stats.get("start"),
        end=stats.get("end"),
        manifest=manifest.to_dict() if manifest else None,
    )


@router.get("/datasets", response_model=DatasetListResponse, summary="行情数据集列表")
def list_datasets(settings: SettingsDep) -> DatasetListResponse:
    normalized = Path(settings.data_root) / "normalized"
    items: list[DatasetInfo] = []
    if normalized.exists():
        for version_dir in sorted(p for p in normalized.glob("dataset=*") if p.is_dir()):
            version = version_dir.name.split("=", 1)[1]
            manifest = _manifest_for(Path(settings.data_root), version)
            for freq_dir in sorted(p for p in version_dir.glob("frequency=*") if p.is_dir()):
                frequency = freq_dir.name.split("=", 1)[1]
                items.append(_dataset_info(settings, version, frequency, manifest))
    return DatasetListResponse(items=items, total=len(items), data_root=settings.data_root)


@router.get("/datasets/{version}/stats", response_model=DatasetStatsResponse, summary="数据集统计")
def dataset_stats(
    version: str,
    settings: SettingsDep,
    frequency: str = Query("1m"),
) -> DatasetStatsResponse:
    store = ParquetMarketDataStore(settings.data_root, version, frequency)
    if not store.exists():
        raise HTTPException(status_code=404, detail=f"数据集 {version}（{frequency}）不存在")
    stats = store.stats()
    manifest = store.manifest()
    _observe_lag(stats.get("end"))
    symbols = list(manifest.symbols) if manifest and manifest.frequency == frequency else store.symbols()
    return DatasetStatsResponse(
        version=version,
        frequency=frequency,
        exists=True,
        rows=int(stats.get("rows", 0) or 0),
        symbols=symbols,
        symbol_count=len(symbols),
        start=stats.get("start"),
        end=stats.get("end"),
        duplicate_bars=int(stats.get("duplicate_bars", 0) or 0),
        manifest=manifest.to_dict() if manifest else None,
    )


@router.post("/validate", response_model=DataValidateResponse, summary="校验行情数据质量")
def validate_dataset(payload: DataValidateRequest, settings: SettingsDep) -> DataValidateResponse:
    version = payload.version or settings.dataset_version
    store = ParquetMarketDataStore(settings.data_root, version, payload.frequency)
    if not store.exists():
        raise HTTPException(status_code=404, detail=f"数据集 {version}（{payload.frequency}）不存在")
    try:
        frame = store.load_frame(
            symbols=payload.symbols or None, start=payload.start, end=payload.end
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"读取数据集失败：{exc}") from exc

    issues: list[str] = []
    stats: dict[str, Any] = {"rows": int(len(frame))}
    if frame.empty:
        issues.append("数据集为空，没有任何 Bar")
        return DataValidateResponse(
            version=version,
            frequency=payload.frequency,
            valid=False,
            checked_rows=0,
            symbols=0,
            issues=issues,
            stats=stats,
        )

    required = ["symbol", "ts", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"数据集缺少必要字段：{', '.join(missing)}")

    duplicates = int(frame.duplicated(subset=["symbol", "ts"]).sum())
    null_rows = int(frame[required].isna().any(axis=1).sum())
    negative_volume = int((frame["volume"] < 0).sum())
    invalid_ohlc = int(
        (
            (frame["high"] < frame["low"])
            | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
            | (frame["close"] > frame["high"])
            | (frame["close"] < frame["low"])
        ).sum()
    )
    stats.update(
        {
            "symbols": int(frame["symbol"].nunique()),
            "start": pd.to_datetime(frame["ts"]).min().isoformat(),
            "end": pd.to_datetime(frame["ts"]).max().isoformat(),
            "duplicate_bars": duplicates,
            "null_rows": null_rows,
            "negative_volume": negative_volume,
            "invalid_ohlc": invalid_ohlc,
        }
    )
    if duplicates:
        issues.append(f"存在 {duplicates} 条重复 Bar（symbol + ts）")
    if null_rows:
        issues.append(f"存在 {null_rows} 条包含空值的 Bar")
    if negative_volume:
        issues.append(f"存在 {negative_volume} 条成交量为负的 Bar")
    if invalid_ohlc:
        issues.append(f"存在 {invalid_ohlc} 条 OHLC 关系非法的 Bar")

    missing_minutes, validator_duplicates = _dataset_quality_metrics(frame, payload.frequency)
    api_metrics.set_data_quality(
        missing_minutes=missing_minutes if missing_minutes is not None else 0,
        duplicate_bars=validator_duplicates if validator_duplicates is not None else duplicates,
    )
    if missing_minutes is not None:
        stats["missing_minutes"] = missing_minutes

    return DataValidateResponse(
        version=version,
        frequency=payload.frequency,
        valid=not issues,
        checked_rows=int(len(frame)),
        symbols=int(frame["symbol"].nunique()),
        issues=issues,
        stats=stats,
    )


@router.post("/build", response_model=DataBuildResponse, summary="构建版本化行情数据集")
def build_dataset_endpoint(payload: DataBuildRequest, settings: SettingsDep) -> DataBuildResponse:
    builder = _resolve_builder()
    if builder is None:
        raise HTTPException(
            status_code=501,
            detail=(
                "数据构建功能尚未可用：data 包未提供 build_dataset 入口"
                "（期望 data.build.build_dataset 或 data.storage.builder.build_dataset）"
            ),
        )
    version = payload.version or settings.dataset_version
    kwargs: dict[str, Any] = payload.model_dump(mode="json")
    kwargs["version"] = version
    kwargs["data_root"] = settings.data_root
    # Let the builder's own defaults apply for unset optional fields.
    kwargs = {key: value for key, value in kwargs.items() if value is not None}
    if not kwargs.get("symbols"):
        kwargs.pop("symbols", None)
    try:
        signature = inspect.signature(builder)
        parameters = signature.parameters
        accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        if not accepts_kwargs:
            kwargs = {k: v for k, v in kwargs.items() if k in parameters}
        if "root" in parameters:
            kwargs["root"] = settings.data_root
        if "raise_on_error" in parameters or accepts_kwargs:
            # Surface quality problems in the response instead of raising.
            kwargs["raise_on_error"] = False
        result = builder(**kwargs)
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=f"数据构建参数不匹配：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"数据构建失败：{exc}") from exc
    except Exception as exc:
        if type(exc).__name__ == "DataValidationError":
            report = getattr(exc, "report", {}) or {}
            issues = report.get("issues") or []
            summary = "；".join(str(item.get("message", item)) for item in issues[:5])
            raise HTTPException(
                status_code=422, detail=f"数据质量校验未通过：{summary or '请查看质量报告'}"
            ) from exc
        logger.exception("dataset build failed")
        raise HTTPException(status_code=500, detail=f"数据构建失败：{exc}") from exc

    safe_result = _serialize_result(result)
    built_ok = bool(safe_result.get("ok", True))
    return DataBuildResponse(
        status="ok" if built_ok else "quality_failed",
        version=version,
        result=safe_result,
        message="数据集构建完成" if built_ok else "数据集已生成，但质量校验未通过",
    )
