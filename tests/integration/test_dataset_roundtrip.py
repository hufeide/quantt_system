"""Integration: synthetic raw -> normalized Parquet -> ParquetMarketDataStore."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from core.market.store import ParquetMarketDataStore
from data.ingestion.synthetic import SyntheticConfig, generate_bars
from data.normalization.normalize import build_dataset
from data.pipeline import build_pipeline
from data.validation.checks import validate_bars

SYMBOLS = ("600000.SH", "000001.SZ")
START = date(2025, 1, 2)
END = date(2025, 1, 6)
EXPECTED_DAYS = 3  # 2025-01-02, 01-03, 01-06


def _pipeline(root: Path, seed: int = 21, version: str = "v001"):
    return build_pipeline(
        root=root,
        version=version,
        symbols=SYMBOLS,
        start=START,
        end=END,
        seed=seed,
    )


def test_normalized_dataset_roundtrips_through_market_store(tmp_path):
    result = _pipeline(tmp_path)
    assert result.ok is True
    assert result.rows == len(SYMBOLS) * EXPECTED_DAYS * 240

    store = ParquetMarketDataStore(tmp_path, "v001", "1m")
    assert store.exists()
    assert store.symbols() == ["000001.SZ", "600000.SH"]

    frame = store.load_frame()
    assert len(frame) == result.rows
    assert frame.duplicated(subset=["symbol", "ts"]).sum() == 0
    ordered = frame.sort_values(["ts", "symbol"], kind="mergesort").reset_index(drop=True)
    assert frame[["ts", "symbol"]].equals(ordered[["ts", "symbol"]])
    assert frame["dataset_version"].unique().tolist() == ["v001"]
    assert frame["frequency"].unique().tolist() == ["1m"]
    assert frame["ts"].dt.tz is None
    assert frame["ts"].dt.date.nunique() == EXPECTED_DAYS
    assert frame.groupby("symbol").size().eq(EXPECTED_DAYS * 240).all()

    # hive year partition on disk and visible to the reader
    partition_dirs = sorted(p.name for p in result.dataset_dir.iterdir() if p.is_dir())
    assert partition_dirs == ["year=2025"]
    assert (result.dataset_dir / "year=2025" / "part-0.parquet").exists()
    assert frame["year"].unique().tolist() == [2025]

    # manifest matches the data that was written
    manifest = store.manifest()
    assert manifest is not None
    assert manifest.row_count == result.rows
    assert manifest.symbols == ["000001.SZ", "600000.SH"]
    assert manifest.start.startswith("2025-01-02T09:30")
    assert manifest.end.startswith("2025-01-06T14:59")
    assert len(manifest.checksum) == 64
    assert manifest.checksum == result.manifest.checksum

    report = json.loads((tmp_path / "normalized" / "dataset=v001" / "quality_report.json").read_text())
    assert report["ok"] is True
    assert report["issues"] == []
    assert validate_bars(frame)["ok"] is True

    # filtered reads keep bar ordering and boundaries
    bars = store.load_bars(symbols=["600000.SH"], start=date(2025, 1, 3), end=date(2025, 1, 3))
    assert len(bars) == 240
    assert {bar.symbol for bar in bars} == {"600000.SH"}
    assert bars[0].ts == pd.Timestamp("2025-01-03 09:30").to_pydatetime()
    assert bars[-1].ts == pd.Timestamp("2025-01-03 14:59").to_pydatetime()

    groups = store.load_groups(symbols=["600000.SH"], start=date(2025, 1, 2), end=date(2025, 1, 2))
    assert len(groups) == 240
    assert all(len(bucket) == 1 for _, bucket in groups)


def test_rebuild_is_byte_identical_and_leaves_no_staging(tmp_path):
    first = _pipeline(tmp_path / "a")
    second = _pipeline(tmp_path / "b")
    assert first.manifest.checksum == second.manifest.checksum

    files_a = sorted(str(p.relative_to(first.dataset_dir)) for p in first.dataset_dir.rglob("*") if p.is_file())
    files_b = sorted(str(p.relative_to(second.dataset_dir)) for p in second.dataset_dir.rglob("*") if p.is_file())
    assert files_a == files_b == ["year=2025/part-0.parquet"]

    # rebuilding in place must be idempotent, not append stale partitions
    again = _pipeline(tmp_path / "a")
    assert again.manifest.checksum == first.manifest.checksum
    assert not list((tmp_path / "a" / "normalized").glob("*.building"))
    assert ParquetMarketDataStore(tmp_path / "a", "v001").stats()["rows"] == first.rows


def test_rqdata_raw_source_roundtrip(tmp_path):
    frame = generate_bars(SyntheticConfig(symbols=("600000.SH",), start=START, end=date(2025, 1, 3), seed=33))
    raw_dir = tmp_path / "raw" / "source=rqdata" / "frequency=1m"
    for day, group in frame.groupby(frame["ts"].dt.strftime("%Y-%m-%d")):
        target = raw_dir / f"date={day}"
        target.mkdir(parents=True, exist_ok=True)
        export = pd.DataFrame(
            {
                "order_book_id": group["symbol"].str.lower(),
                "datetime": group["ts"].dt.strftime("%Y-%m-%d %H:%M:%S"),
                "open": group["open"],
                "high": group["high"],
                "low": group["low"],
                "close": group["close"],
                "volume": group["volume"],
                "amount": group["amount"],
            }
        )
        export.to_csv(target / "part.csv", index=False)

    build = build_dataset(tmp_path, version="rq001", source="rqdata")
    assert build.rows == 2 * 240
    assert build.ok is True
    assert build.manifest.source == "rqdata"
    assert build.frame["symbol"].unique().tolist() == ["600000.SH"]
    assert build.frame["close"].tolist() == pytest.approx(frame.sort_values(["symbol", "ts"])["close"].tolist())
    assert ParquetMarketDataStore(tmp_path, "rq001").stats()["rows"] == 480


def test_reusable_sample_dataset_in_data_files():
    root = Path("data_files")
    store = ParquetMarketDataStore(root, "v001", "1m")
    if not store.exists():
        pytest.skip("sample dataset data_files/normalized/dataset=v001 not generated")

    stats = store.stats()
    assert stats["rows"] == 2 * 7 * 240  # 2025-01-02 .. 2025-01-10, 7 trading days
    assert stats["duplicate_bars"] == 0
    assert store.manifest() is not None
    report_path = root / "normalized" / "dataset=v001" / "quality_report.json"
    assert json.loads(report_path.read_text(encoding="utf-8"))["ok"] is True
