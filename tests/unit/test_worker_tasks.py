"""Worker task tests, run in Celery eager mode (no broker required)."""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Must be set before apps.worker.celery_app is imported.
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "1"
os.environ["CELERY_TASK_EAGER_PROPAGATES"] = "1"

from apps.worker.celery_app import celery_app  # noqa: E402
from apps.worker.tasks import (  # noqa: E402
    compute_factors_task,
    maintenance_task,
    optimize_task,
    run_backtest_task,
)
from core.storage.parquet import BAR_SCHEMA, write_dataframe  # noqa: E402

VERSION = "vtest"


def write_synthetic_dataset(
    root: Path,
    version: str = VERSION,
    symbols: tuple[str, ...] = ("AAA", "BBB"),
    days: int = 2,
    bars_per_day: int = 120,
    seed: int = 7,
) -> Path:
    """Write a small hive-partitioned minute dataset plus its manifest."""
    rng = np.random.default_rng(seed)
    rows = []
    day = date(2024, 1, 2)
    for _ in range(days):
        start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
        for i in range(bars_per_day):
            ts = start + timedelta(minutes=i)
            for symbol in symbols:
                base = 10.0 if symbol == "AAA" else 20.0
                drift = 0.0005 * np.sin(i / 7.0) + rng.normal(0, 0.0015)
                close = round(base * (1.0 + drift * (i + 1) / 10.0), 4)
                open_ = round(close * (1.0 + rng.normal(0, 0.0005)), 4)
                high = round(max(open_, close) * 1.0008, 4)
                low = round(min(open_, close) * 0.9992, 4)
                volume = float(rng.integers(50_000, 200_000))
                rows.append(
                    {
                        "symbol": symbol,
                        "ts": ts,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                        "amount": close * volume,
                        "frequency": "1m",
                        "dataset_version": version,
                    }
                )
        day = day + timedelta(days=1)

    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"]).astype("datetime64[ms]")
    path = root / "normalized" / f"dataset={version}" / "frequency=1m" / "year=2024" / "part-0.parquet"
    write_dataframe(df, path, schema=BAR_SCHEMA)

    manifest = {
        "dataset_version": version,
        "frequency": "1m",
        "source": "synthetic",
        "symbols": list(symbols),
        "start": df["ts"].min().isoformat(),
        "end": df["ts"].max().isoformat(),
        "row_count": int(len(df)),
        "checksum": "",
        "schema": [],
        "timezone": "Asia/Shanghai",
        "notes": "synthetic fixture",
        "stats": {},
    }
    (root / "normalized" / f"dataset={version}" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return path


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    write_synthetic_dataset(tmp_path)
    return tmp_path


@pytest.fixture()
def output_root(workspace: Path) -> Path:
    return workspace / "backtests"


@pytest.fixture(autouse=True)
def _eager_mode():
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield
    celery_app.conf.task_always_eager = False


def backtest_config(workspace: Path, output_root: Path, **overrides) -> dict:
    config = {
        "strategy": "sma_cross",
        "strategy_params": {"fast": 3, "slow": 8, "weight": 0.5},
        "symbols": ["AAA"],
        "start": "2024-01-02",
        "end": "2024-01-03",
        "initial_cash": 500_000.0,
        "frequency": "1m",
        "dataset_version": VERSION,
        "data_root": str(workspace),
        "output_root": str(output_root),
        "warmup_bars": 10,
        "record_events": True,
        "checkpoint_every": 100,
    }
    config.update(overrides)
    return config


def test_task_registration_and_routes():
    assert run_backtest_task.name == "quant.run_backtest"
    assert optimize_task.name == "quant.optimize"
    assert compute_factors_task.name == "quant.compute_factors"
    assert maintenance_task.name == "quant.maintenance"
    assert celery_app.conf.task_routes["quant.run_backtest"]["queue"] == "backtest"
    assert celery_app.conf.task_routes["quant.optimize"]["queue"] == "optimization"
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.timezone == "Asia/Shanghai"


def test_deployment_env_contract(monkeypatch: pytest.MonkeyPatch):
    from apps.worker.celery_app import (
        create_celery,
        default_queues,
        resolve_broker_url,
        resolve_result_backend,
    )

    monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
    monkeypatch.delenv("CELERY_RESULT_BACKEND", raising=False)
    monkeypatch.setenv("QUANT_CELERY_BROKER_URL", "amqp://quant:secret@rabbitmq:5672//")
    monkeypatch.setenv("QUANT_CELERY_RESULT_BACKEND", "redis://redis:6379/1")
    monkeypatch.setenv("QUANT_WORKER_QUEUES", "backtest,factor")

    assert resolve_broker_url() == "amqp://quant:secret@rabbitmq:5672//"
    assert resolve_result_backend() == "redis://redis:6379/1"
    assert default_queues() == "backtest,factor"

    # QUANT_RABBITMQ_URL is the fallback when no dedicated broker var exists.
    monkeypatch.delenv("QUANT_CELERY_BROKER_URL")
    monkeypatch.setenv("QUANT_RABBITMQ_URL", "amqp://rabbitmq:5672//")
    assert resolve_broker_url() == "amqp://rabbitmq:5672//"

    app = create_celery("contract_test")
    assert app.conf.broker_url == "amqp://rabbitmq:5672//"
    assert {queue.name for queue in app.conf.task_queues} == {
        "replay",
        "backtest",
        "factor",
        "optimization",
        "maintenance",
    }


def test_worker_metrics_are_exported():
    from apps.worker import metrics

    metrics.record_backtest("sma_cross", bars=42, duration=0.25)
    metrics.record_factor_rows(10)
    metrics.record_optimization_runs(3)

    registry = metrics.build_registry()
    names = {sample.name for metric in registry.collect() for sample in metric.samples}
    assert "quant_worker_backtest_total" in names
    assert "quant_worker_backtest_duration_seconds_count" in names
    assert "quant_worker_bars_processed_total" in names
    assert "quant_factor_rows_total" in names
    assert "quant_optimization_runs_total" in names


def test_run_backtest_task_returns_metrics_and_artifacts(workspace: Path, output_root: Path):
    config = backtest_config(workspace, output_root)
    result = run_backtest_task.apply(args=(config, "run_test_001")).get()

    assert result["run_id"] == "run_test_001"
    assert isinstance(result["metrics"], dict)
    assert "sharpe" in result["metrics"]
    assert result["metrics"]["run_id"] == "run_test_001"
    assert result["metrics"]["symbols"] == ["AAA"]

    artifacts = result["artifacts"]
    for name in ("metrics.json", "manifest.json", "equity.parquet", "trades.parquet", "orders.parquet"):
        assert name in artifacts, name
        assert Path(artifacts[name]).exists(), name

    run_dir = output_root / "run_id=run_test_001"
    assert run_dir.is_dir()
    equity = pd.read_parquet(run_dir / "equity.parquet")
    assert not equity.empty and {"ts", "equity"}.issubset(equity.columns)
    assert result["artifact_count"] == len(artifacts)


def test_task_defaults_roots_from_env(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The API dispatches configs that already carry roots; env is the fallback."""
    env_out = tmp_path / "env_out"
    monkeypatch.setenv("QR_DATA_ROOT", str(workspace))
    monkeypatch.setenv("QR_OUTPUT_ROOT", str(env_out))

    config = backtest_config(workspace, tmp_path / "unused")
    config.pop("data_root")
    config.pop("output_root")
    result = run_backtest_task.apply(args=(config, "run_env_roots")).get()

    assert Path(result["artifacts"]["metrics.json"]).exists()
    assert str(env_out) in result["output_dir"]


def test_run_backtest_task_generates_trades(workspace: Path, output_root: Path):
    config = backtest_config(
        workspace,
        output_root,
        strategy="buy_and_hold",
        # weight must stay under the default 30% per-position risk limit
        strategy_params={"weight": 0.25},
        initial_cash=200_000.0,
    )
    result = run_backtest_task.apply(args=(config, "run_test_bh")).get()
    assert result["metrics"]["trade_count"] >= 1
    trades = pd.read_parquet(output_root / "run_id=run_test_bh" / "trades.parquet")
    assert not trades.empty
    assert set(trades["symbol"]) == {"AAA"}


def test_optimize_task_ranks_by_sharpe(workspace: Path, output_root: Path):
    config = backtest_config(workspace, output_root)
    grid = {"fast": [2, 4], "slow": [8, 12]}
    result = optimize_task.apply(args=(config, grid), kwargs={"metric": "sharpe"}).get()

    assert result["count"] == 4
    assert result["metric"] == "sharpe"
    assert len(result["results"]) == 4
    sharpes = [item["metrics"]["sharpe"] for item in result["results"]]
    assert sharpes == sorted(sharpes, reverse=True)
    assert result["best"]["run_id"] == result["results"][0]["run_id"]

    for item in result["results"]:
        assert item["params"]["fast"] in {2, 4}
        assert item["params"]["slow"] in {8, 12}
        run_dir = output_root / f"run_id={item['run_id']}"
        assert (run_dir / "metrics.json").exists()
        assert item["strategy"] == "sma_cross"


def test_optimize_task_rejects_empty_grid(workspace: Path, output_root: Path):
    config = backtest_config(workspace, output_root)
    with pytest.raises(ValueError):
        optimize_task.apply(args=(config, {"fast": []})).get()


def test_compute_factors_task(workspace: Path):
    result = compute_factors_task.apply(
        args=(VERSION, ["momentum", "volatility"]),
        kwargs={"data_root": str(workspace)},
    ).get()

    assert result["dataset_version"] == VERSION
    assert result["rows_processed"] > 0
    assert set(result["symbols"]) == {"AAA", "BBB"}
    assert set(result["factors"]) == {"momentum", "volatility"}
    assert result["factors"]["momentum"]["valid"] > 0
    factor_dir = Path(result["path"])
    assert factor_dir.exists()
    assert list(factor_dir.rglob("*.parquet"))


def test_maintenance_validate_dataset(workspace: Path):
    result = maintenance_task.apply(
        args=("validate",), kwargs={"data_root": str(workspace), "dataset_version": VERSION}
    ).get()
    assert result["kind"] == "validate"
    assert result["checked"] == 1
    report = result["reports"][0]
    assert report["ok"] is True
    assert report["rows"] > 0
    assert report["manifest"]["dataset_version"] == VERSION


def test_maintenance_cleanup_removes_old_runs(workspace: Path, output_root: Path):
    old_run = output_root / "run_id=old_run"
    old_run.mkdir(parents=True)
    (old_run / "metrics.json").write_text("{}", encoding="utf-8")
    stale = time.time() - 10 * 86_400
    os.utime(old_run, (stale, stale))

    fresh_run = output_root / "run_id=fresh_run"
    fresh_run.mkdir(parents=True)

    preview = maintenance_task.apply(
        args=("cleanup",), kwargs={"output_root": str(output_root), "older_than_days": 5}
    ).get()
    assert preview["dry_run"] is True
    assert preview["candidates"] == ["run_id=old_run"]
    assert old_run.exists()

    applied = maintenance_task.apply(
        args=("cleanup",),
        kwargs={"output_root": str(output_root), "older_than_days": 5, "dry_run": False},
    ).get()
    assert applied["deleted"] == ["run_id=old_run"]
    assert not old_run.exists()
    assert fresh_run.exists()


def test_maintenance_prune_checkpoints(workspace: Path, output_root: Path):
    run_dir = output_root / "run_id=ckpt_run" / "checkpoints"
    run_dir.mkdir(parents=True)
    for i in range(5):
        (run_dir / f"ckpt_{i:010d}_20240102T093000.json").write_text("{}", encoding="utf-8")

    result = maintenance_task.apply(
        args=("prune_checkpoints",),
        kwargs={"output_root": str(output_root), "keep_checkpoints": 2, "dry_run": False},
    ).get()
    assert result["removed_count"] == 3
    assert len(list(run_dir.glob("ckpt_*.json"))) == 2


def test_maintenance_rejects_unknown_kind(workspace: Path):
    with pytest.raises(ValueError):
        maintenance_task.apply(args=("nonsense",)).get()
