"""Data layer unit tests: generator, loader, checks, normalization, DuckDB."""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from data.ingestion.rqdata_loader import load_rqdata_csv, load_rqdata_directory, standardize_frame
from data.ingestion.synthetic import SyntheticConfig, generate_bars, generate_raw
from data.normalization.normalize import build_dataset, normalize_frame, read_raw
from data.pipeline import build_pipeline
from data.storage.duckdb_store import DuckDBStore
from data.validation.checks import (
    DataValidationError,
    check_duplicates,
    check_missing_minutes,
    check_ohlc,
    check_prices,
    check_sessions,
    check_volume,
    validate_bars,
)


def small_config(seed: int = 11, symbols: tuple[str, ...] = ("600000.SH", "000001.SZ")) -> SyntheticConfig:
    return SyntheticConfig(
        symbols=symbols,
        start=date(2025, 1, 2),
        end=date(2025, 1, 6),
        seed=seed,
        base_volume=20_000.0,
    )


# -- synthetic generator -------------------------------------------------
def test_synthetic_is_deterministic():
    a = generate_bars(small_config(seed=5))
    b = generate_bars(small_config(seed=5))
    c = generate_bars(small_config(seed=6))
    assert a.equals(b)
    assert not a.equals(c)


def test_synthetic_covers_only_trading_sessions():
    frame = generate_bars(small_config())
    assert len(frame) == 2 * 3 * 240  # 2 symbols x 3 trading days x 240 minutes
    assert sorted(frame["symbol"].unique()) == ["000001.SZ", "600000.SH"]
    days = frame["ts"].dt.date.unique()
    assert all(pd.Timestamp(day).weekday() < 5 for day in days)
    minutes = frame["ts"].dt.hour * 60 + frame["ts"].dt.minute
    in_session = ((minutes >= 570) & (minutes < 690)) | ((minutes >= 780) & (minutes < 900))
    assert in_session.all()
    assert frame[["ts", "symbol"]].equals(
        frame.sort_values(["ts", "symbol"], kind="mergesort")[["ts", "symbol"]]
    )


def test_synthetic_has_overnight_gaps_and_valid_ohlc():
    frame = generate_bars(small_config())
    gaps = 0
    for _, group in frame.groupby("symbol"):
        daily = group.groupby(group["ts"].dt.date)
        previous_close = None
        for _, day_bars in daily:
            day_open = float(day_bars.iloc[0]["open"])
            if previous_close is not None and abs(day_open - previous_close) > 1e-9:
                gaps += 1
            previous_close = float(day_bars.iloc[-1]["close"])
    assert gaps > 0
    assert (frame["high"] >= frame[["open", "close"]].max(axis=1)).all()
    assert (frame["low"] <= frame[["open", "close"]].min(axis=1)).all()
    assert (frame[["open", "high", "low", "close"]] > 0).all().all()
    assert (frame["volume"] > 0).all()
    assert frame["volume"].mod(100).eq(0).all()


def test_raw_partitions_are_written_and_read_back(tmp_path):
    build = generate_raw(small_config(symbols=("600000.SH",)), raw_root=tmp_path)
    assert len(build.files) == 3
    assert all(p.parent.name.startswith("date=2025-01-") for p in build.files)
    assert all(p.name == "part.csv" for p in build.files)

    frame = read_raw(tmp_path, start=date(2025, 1, 3), end=date(2025, 1, 3))
    assert len(frame) == 240
    assert frame["ts"].dt.date.unique().tolist() == [date(2025, 1, 3)]


# -- rqdata loader -------------------------------------------------------
def test_rqdata_loader_handles_date_time_and_order_book_id(tmp_path):
    path = tmp_path / "bars.csv"
    pd.DataFrame(
        {
            "order_book_id": ["600000.sh", "600000.SH"],
            "date": ["2025-01-02", "2025-01-02"],
            "time": ["09:30:00", "09:31:00"],
            "open": [10.0, 10.1],
            "high": [10.2, 10.3],
            "low": [9.9, 10.0],
            "close": [10.1, 10.2],
            "volume": [1000, 1200],
            "amount": [10_050.0, 12_180.0],
        }
    ).to_csv(path, index=False)

    frame = load_rqdata_csv(path)
    assert frame.columns.tolist() == [
        "symbol", "ts", "open", "high", "low", "close", "volume", "amount", "frequency"
    ]
    assert frame["symbol"].tolist() == ["600000.SH", "600000.SH"]
    assert frame["ts"].tolist() == [pd.Timestamp("2025-01-02 09:30"), pd.Timestamp("2025-01-02 09:31")]
    assert frame["ts"].dt.tz is None
    assert frame["frequency"].tolist() == ["1m", "1m"]
    assert frame["volume"].dtype == "float64"


def test_rqdata_loader_strips_timezone_and_fills_missing_amount():
    raw = pd.DataFrame(
        {
            "datetime": ["2025-01-02T09:30:00+08:00"],
            "symbol": ["000001.sz"],
            "open": ["8.0"],
            "high": ["8.1"],
            "low": ["7.9"],
            "close": ["8.05"],
            "volume": ["500"],
        }
    )
    frame = standardize_frame(raw)
    assert frame.loc[0, "ts"] == pd.Timestamp("2025-01-02 09:30")
    assert frame.loc[0, "symbol"] == "000001.SZ"
    assert frame.loc[0, "amount"] == 0.0


def test_rqdata_directory_loader_filters_symbols(tmp_path):
    for name, symbol in (("a.csv", "600000.SH"), ("b.csv", "000001.SZ")):
        pd.DataFrame(
            {
                "datetime": ["2025-01-02 09:30"],
                "symbol": [symbol],
                "open": [10.0],
                "high": [10.1],
                "low": [9.9],
                "close": [10.05],
                "volume": [1000],
            }
        ).to_csv(tmp_path / name, index=False)

    frame = load_rqdata_directory(tmp_path, symbols=["600000.SH"])
    assert frame["symbol"].tolist() == ["600000.SH"]
    assert len(load_rqdata_directory(tmp_path)) == 2


def test_rqdata_loader_rejects_frames_without_prices_or_symbol():
    base = {"datetime": ["2025-01-02 09:30"], "symbol": ["600000.SH"], "open": [1.0]}
    with pytest.raises(ValueError, match="missing required price columns"):
        standardize_frame(pd.DataFrame(base))

    no_symbol = {"datetime": ["2025-01-02 09:30"], "open": [1.0], "high": [1.0],
                 "low": [1.0], "close": [1.0]}
    with pytest.raises(ValueError, match="no symbol column"):
        standardize_frame(pd.DataFrame(no_symbol))


# -- validation ----------------------------------------------------------
def test_clean_synthetic_frame_passes_validation():
    report = validate_bars(generate_bars(small_config()))
    assert report["ok"] is True
    assert report["issues"] == []
    assert report["stats"]["duplicate_rows"] == 0
    assert report["checks"]["missing_minutes"]["ok"] is True


def test_duplicate_bars_are_detected():
    frame = generate_bars(small_config(symbols=("600000.SH",)))
    dirty = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    issues = check_duplicates(dirty)
    assert [i["code"] for i in issues] == ["DUPLICATE_BAR"]
    assert issues[0]["count"] == 2
    assert issues[0]["examples"][0]["symbol"] == "600000.SH"


def test_invalid_ohlc_and_prices_are_detected():
    frame = generate_bars(small_config(symbols=("600000.SH",))).copy()
    frame.loc[10, "high"] = frame.loc[10, "low"] - 1.0
    frame.loc[20, "close"] = 0.0
    assert [i["code"] for i in check_ohlc(frame)] == ["OHLC_INVALID"]
    assert [i["code"] for i in check_prices(frame)] == ["INVALID_PRICE"]


def test_negative_volume_is_an_error_and_extreme_volume_is_a_warning():
    frame = generate_bars(small_config(symbols=("600000.SH",))).copy()
    frame.loc[5, "volume"] = -1.0
    frame.loc[6, "volume"] = 1_000_000_000.0

    codes = {issue["code"]: issue for issue in check_volume(frame)}
    assert codes["VOLUME_INVALID"]["severity"] == "error"
    assert codes["VOLUME_ANOMALY"]["severity"] == "warning"

    report = validate_bars(frame)
    assert report["ok"] is False
    assert {i["code"] for i in report["issues"]} == {"VOLUME_INVALID", "VOLUME_ANOMALY"}


def test_missing_minutes_and_off_session_bars_are_detected():
    frame = generate_bars(small_config(symbols=("600000.SH",)))
    missing = frame.drop(index=100).reset_index(drop=True)
    issues = check_missing_minutes(missing)
    assert issues[0]["code"] == "MISSING_MINUTES"
    assert issues[0]["count"] == 1

    off = frame.copy()
    off.loc[0, "ts"] = pd.Timestamp("2025-01-02 12:00")
    assert [i["code"] for i in check_sessions(off)] == ["OFF_SESSION"]


def test_validate_bars_report_shape_and_raise_on_error():
    frame = generate_bars(small_config(symbols=("600000.SH",)))
    dirty = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)

    report = validate_bars(dirty, raise_on_error=False)
    assert report["ok"] is False
    assert report["rows"] == len(dirty)
    assert report["symbols"] == ["600000.SH"]
    assert set(report["checks"]) >= {"duplicates", "ohlc", "prices", "volume", "sessions"}
    assert json.dumps(report)  # JSON serializable

    with pytest.raises(DataValidationError) as excinfo:
        validate_bars(dirty, raise_on_error=True)
    assert excinfo.value.report["ok"] is False


# -- normalization -------------------------------------------------------
def test_normalize_frame_uppercases_dedupes_sorts_and_versions():
    frame = generate_bars(small_config(symbols=("600000.SH",))).copy()
    frame.loc[0, "symbol"] = "600000.sh"
    duplicate = frame.iloc[[1]].copy()
    duplicate["close"] = duplicate["close"] + 0.01
    frame = pd.concat([frame, duplicate], ignore_index=True)
    frame = frame.sample(frac=1.0, random_state=3).reset_index(drop=True)  # shuffle

    out = normalize_frame(frame, version="v009")
    assert out["symbol"].unique().tolist() == ["600000.SH"]
    assert out["dataset_version"].unique().tolist() == ["v009"]
    assert out["year"].unique().tolist() == ["2025"]
    assert not out.duplicated(subset=["symbol", "ts"]).any()
    assert out[["ts", "symbol"]].equals(out.sort_values(["symbol", "ts"])[["ts", "symbol"]].reset_index(drop=True))


def test_build_dataset_writes_manifest_and_quality_report(tmp_path):
    generate_raw(small_config(), raw_root=tmp_path)
    build = build_dataset(tmp_path, version="v001")
    assert build.rows == 2 * 3 * 240
    assert build.ok is True
    assert build.dataset_dir.exists()
    assert (build.dataset_dir / "year=2025" / "part-0.parquet").exists()
    assert build.manifest_path.exists()
    assert build.report_path.exists()

    manifest = json.loads(build.manifest_path.read_text(encoding="utf-8"))
    assert manifest["dataset_version"] == "v001"
    assert manifest["row_count"] == 1440
    assert manifest["symbols"] == ["000001.SZ", "600000.SH"]
    assert manifest["start"].startswith("2025-01-02T09:30")
    assert len(manifest["checksum"]) == 64
    assert {f["name"] for f in manifest["schema"]} >= {"symbol", "ts", "open", "close", "dataset_version"}


# -- DuckDB --------------------------------------------------------------
def test_duckdb_store_bar_queries(tmp_path):
    generate_raw(small_config(), raw_root=tmp_path)
    build_dataset(tmp_path, version="v001")

    with DuckDBStore(tmp_path, "v001") as db:
        stats = db.stats()
        assert stats["rows"] == 1440
        assert stats["symbols"] == 2
        assert stats["duplicate_bars"] == 0

        bars = db.bars(symbols=["600000.SH"], start=date(2025, 1, 3), end=date(2025, 1, 3))
        assert len(bars) == 240
        assert bars["symbol"].unique().tolist() == ["600000.SH"]
        assert bars["ts"].is_monotonic_increasing

        daily = db.daily_bars()
        assert len(daily) == 6
        assert daily["bars"].eq(240).all()
        assert (daily["high"] >= daily["close"]).all()

        grouped = db.query("SELECT symbol, count(*) AS n FROM bars GROUP BY symbol ORDER BY symbol")
        assert grouped["n"].tolist() == [720, 720]
        assert db.symbols() == ["000001.SZ", "600000.SH"]
        assert db.date_range()[0] == pd.Timestamp("2025-01-02 09:30")


def test_duckdb_store_reads_backtest_artifacts(tmp_path):
    run_dir = tmp_path / "backtests" / "run_id=r1"
    run_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "ts": pd.to_datetime(["2025-01-02 09:31", "2025-01-02 09:32"]),
            "equity": [1_000_000.0, 1_001_000.0],
        }
    ).to_parquet(run_dir / "equity.parquet", index=False)
    pd.DataFrame(
        {
            "ts": pd.to_datetime(["2025-01-02 09:31"]),
            "symbol": ["600000.SH"],
            "side": ["buy"],
            "quantity": [100.0],
            "price": [10.0],
            "commission": [5.0],
        }
    ).to_parquet(run_dir / "trades.parquet", index=False)
    (run_dir / "metrics.json").write_text(
        json.dumps({"final_equity": 1_001_000.0, "total_return": 0.001}), encoding="utf-8"
    )

    with DuckDBStore(tmp_path, "v001") as db:
        assert db.run_ids() == ["r1"]
        equity = db.load_equity("r1")
        assert len(equity) == 2
        trades = db.load_trades("r1")
        assert trades["commission"].sum() == pytest.approx(5.0)
        summary = db.run_summary("r1")
        assert summary["final_equity"] == pytest.approx(1_001_000.0)
        assert summary["trade_rows"] == 1
        assert summary["total_fees"] == pytest.approx(5.0)


# -- pipeline ------------------------------------------------------------
def test_pipeline_reexports_the_canonical_build_dataset():
    from data.normalization.normalize import build_dataset as canonical
    from data.pipeline import build_dataset

    assert build_dataset is canonical


def test_pipeline_builds_dataset_and_factors(tmp_path):
    result = build_pipeline(
        root=tmp_path,
        version="v001",
        symbols=("600000.SH", "000001.SZ"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 6),
        seed=13,
        with_factors=True,
    )
    assert result.ok is True
    assert result.rows == 1440
    assert len(result.raw_files) == 3
    assert result.factor_summary is not None
    assert "momentum" in result.factor_summary["factors"]
    factor_dir = tmp_path / "factors" / "version=v001"
    assert factor_dir.exists()
    assert any(factor_dir.rglob("*.parquet"))

    manifest = result.manifest
    assert manifest.row_count == result.rows
    assert manifest.checksum
    assert result.dataset_dir.name == "frequency=1m"


def test_cli_build_validate_and_stats(tmp_path, capsys):
    from data.cli import main

    assert main([
        "build", "--root", str(tmp_path), "--version", "v001",
        "--symbols", "600000.SH", "--start", "2025-01-02", "--end", "2025-01-03", "--seed", "3",
    ]) == 0
    assert "数据集构建完成" in capsys.readouterr().out

    assert main(["validate", "--root", str(tmp_path), "--version", "v001"]) == 0
    assert "质量校验: 通过" in capsys.readouterr().out

    assert main(["stats", "--root", str(tmp_path), "--version", "v001"]) == 0
    assert "行数=480" in capsys.readouterr().out

    assert main(["validate", "--root", str(tmp_path), "--version", "missing"]) == 2


def test_pipeline_rejects_empty_date_range(tmp_path):
    with pytest.raises(ValueError, match="no bars"):
        build_pipeline(
            root=tmp_path,
            symbols=("600000.SH",),
            start=date(2025, 1, 4),  # Saturday
            end=date(2025, 1, 5),    # Sunday
        )
