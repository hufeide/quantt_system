"""Shared fixtures for API integration tests."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.settings import Settings
from core.storage.dataset_manifest import DatasetManifest
from core.storage.parquet import write_partitioned


def build_minute_dataset(
    root: Path,
    version: str = "v001",
    symbols: tuple[str, ...] = ("000001", "600000"),
    days: int = 3,
    minutes: int = 60,
) -> dict:
    """Write a small, deterministic minute-bar dataset in the production layout."""
    rows: list[dict] = []
    first_day = datetime(2025, 1, 6, 9, 30)
    for day_index in range(days):
        day = first_day + timedelta(days=day_index)
        for symbol_index, symbol in enumerate(symbols):
            price = 10.0 + symbol_index * 2.0
            for minute in range(minutes):
                ts = day + timedelta(minutes=minute)
                wave = math.sin((minute + day_index * 9 + symbol_index * 3) / 4.0) * 0.06
                open_price = price
                close_price = max(1.0, price + wave)
                high = max(open_price, close_price) + 0.02
                low = min(open_price, close_price) - 0.02
                volume = float(10_000 + minute * 10)
                rows.append(
                    {
                        "symbol": symbol,
                        "ts": ts,
                        "open": round(open_price, 4),
                        "high": round(high, 4),
                        "low": round(low, 4),
                        "close": round(close_price, 4),
                        "volume": volume,
                        "amount": round(volume * close_price, 4),
                        "frequency": "1m",
                        "dataset_version": version,
                    }
                )
                price = close_price

    frame = pd.DataFrame(rows)
    frame["ts"] = pd.to_datetime(frame["ts"])
    frame["year"] = frame["ts"].dt.year.astype(str)
    target = root / "normalized" / f"dataset={version}" / "frequency=1m"
    write_partitioned(frame, target, partition_cols=("year",))

    manifest = DatasetManifest(
        dataset_version=version,
        frequency="1m",
        source="integration-test",
        symbols=list(symbols),
        start=frame["ts"].min().isoformat(),
        end=frame["ts"].max().isoformat(),
        row_count=len(frame),
    )
    manifest.save(root / "normalized" / f"dataset={version}" / "manifest.json")
    return {"rows": len(frame), "symbols": list(symbols)}


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    root = tmp_path_factory.mktemp("api_env")
    data_root = root / "data"
    data_root.mkdir(parents=True)
    dataset = build_minute_dataset(data_root)

    settings = Settings(
        data_root=str(data_root),
        output_root=str(root / "backtests"),
        mongo_uri="mongodb://127.0.0.1:9",
        mongo_timeout_ms=200,
        max_workers=1,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, settings, dataset
