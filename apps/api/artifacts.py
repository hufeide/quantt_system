"""Read backtest artifacts from Parquet and convert them to JSON-safe records."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from apps.api.settings import Settings

ARTIFACT_FILES: dict[str, str] = {
    "equity": "equity.parquet",
    "trades": "trades.parquet",
    "orders": "orders.parquet",
    "positions": "positions.parquet",
    "events": "events.parquet",
}


def run_dir(settings: Settings, run_id: str, *, replay: bool = False) -> Path:
    root = settings.replay_root if replay else Path(settings.output_root)
    return Path(root) / f"run_id={run_id}"


def artifact_path(settings: Settings, run_id: str, kind: str) -> Path:
    filename = ARTIFACT_FILES.get(kind)
    if filename is None:
        raise KeyError(kind)
    return run_dir(settings, run_id) / filename


def jsonify(value: Any) -> Any:
    """Convert numpy/pandas scalars and timestamps into plain JSON values."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonify(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):  # pragma: no cover - non-scalar fallback
        pass
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, bool)):
        return value
    return value


def _frame_records(frame: pd.DataFrame, limit: int) -> tuple[list[dict[str, Any]], int]:
    total = int(len(frame))
    subset = frame.head(max(0, limit))
    records = []
    for row in subset.to_dict(orient="records"):
        record = {str(k): jsonify(v) for k, v in row.items()}
        payload = record.get("payload")
        if isinstance(payload, str):
            try:
                record["payload"] = json.loads(payload)
            except json.JSONDecodeError:
                record["payload"] = {"raw": payload}
        records.append(record)
    return records, total


def read_artifact(
    settings: Settings, run_id: str, kind: str, limit: int = 1000
) -> tuple[list[dict[str, Any]], int]:
    """Return (records, total_rows). Raises FileNotFoundError when absent."""
    path = artifact_path(settings, run_id, kind)
    if not path.exists():
        raise FileNotFoundError(str(path))
    frame = pd.read_parquet(path)
    return _frame_records(frame, limit)


def equity_last_row(settings: Settings, run_id: str) -> dict[str, Any] | None:
    path = artifact_path(settings, run_id, "equity")
    if not path.exists():
        return None
    try:
        frame = pd.read_parquet(path)
    except Exception:
        return None
    if frame.empty:
        return None
    record = {str(k): jsonify(v) for k, v in frame.iloc[-1].to_dict().items()}
    return record


def latest_positions(settings: Settings, run_id: str, limit: int = 1000) -> tuple[list[dict[str, Any]], str | None, int]:
    path = artifact_path(settings, run_id, "positions")
    if not path.exists():
        raise FileNotFoundError(str(path))
    frame = pd.read_parquet(path)
    if frame.empty:
        return [], None, 0
    frame = frame.copy()
    frame["ts"] = pd.to_datetime(frame["ts"])
    as_of = frame["ts"].max()
    latest = frame[frame["ts"] == as_of]
    records, _ = _frame_records(latest, limit)
    return records, jsonify(as_of), int(len(latest))
