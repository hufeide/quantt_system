"""End-to-end backtest runner tests."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from core.backtest.runner import BacktestRunner
from core.config import BacktestConfig
from core.market.store import ParquetMarketDataStore
from core.replay.feed import MemoryBarSource
from tests.golden_backtest.golden import build_dataset, golden_config


def test_artifacts_are_written(tmp_path, bars, config):
    cfg = BacktestConfig.from_dict(config.to_dict())
    cfg.run_id = "artifact_run"
    runner = BacktestRunner(cfg, source=MemoryBarSource.from_bars(bars), output_root=tmp_path / "out")
    result = runner.run()

    run_dir = Path(tmp_path / "out" / "run_id=artifact_run")
    for name in ("manifest.json", "events.parquet", "orders.parquet", "trades.parquet",
                 "positions.parquet", "equity.parquet", "metrics.json"):
        assert (run_dir / name).exists(), name

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["strategy"] == "sma_cross"
    assert manifest["dataset_version"] == cfg.dataset_version
    assert manifest["matching"]["slippage_bps"] == cfg.matching.slippage_bps
    assert manifest["fees"]["commission_rate"] == cfg.fees.commission_rate
    assert manifest["seed"] == cfg.seed
    assert manifest["environment"]["python"]

    equity = pd.read_parquet(run_dir / "equity.parquet")
    assert len(equity) > 100
    assert equity["equity"].iloc[-1] == pytest.approx(result.metrics["final_equity"], rel=1e-6)


def test_same_inputs_produce_identical_results(tmp_path, bars, config):
    cfg1 = BacktestConfig.from_dict(config.to_dict())
    cfg1.run_id = "r1"
    cfg2 = BacktestConfig.from_dict(config.to_dict())
    cfg2.run_id = "r2"
    a = BacktestRunner(cfg1, source=MemoryBarSource.from_bars(bars), output_root=tmp_path / "a").run()
    b = BacktestRunner(cfg2, source=MemoryBarSource.from_bars(bars), output_root=tmp_path / "b").run()
    assert a.fingerprint == b.fingerprint
    assert a.metrics["final_equity"] == pytest.approx(b.metrics["final_equity"])
    assert a.trades["price"].tolist() == pytest.approx(b.trades["price"].tolist())


def test_all_builtin_strategies_run(tmp_path, bars):
    from core.strategy import registry_names

    for name in registry_names():
        cfg = BacktestConfig(
            strategy=name,
            symbols=["600000.SH"],
            initial_cash=300_000.0,
            checkpoint_every=0,
        )
        cfg.risk.max_position_weight = 0.9
        cfg.risk.max_order_value = 1_000_000.0
        result = BacktestRunner(
            cfg, source=MemoryBarSource.from_bars(bars), output_root=tmp_path / name
        ).run()
        assert result.errors == [], (name, result.errors)
        assert result.metrics["final_equity"] > 0


def test_parquet_dataset_end_to_end(tmp_path):
    root = tmp_path / "data"
    build_dataset(root)
    cfg = golden_config(root)
    cfg.run_id = "parquet_run"
    cfg.output_root = str(tmp_path / "out")
    result = BacktestRunner(cfg, output_root=tmp_path / "out", run_id="parquet_run").run()
    assert result.metrics["trade_count"] > 0
    assert result.manifest.dataset_version == "golden_v1"
    store = ParquetMarketDataStore(root, "golden_v1", "1m")
    assert store.stats()["rows"] > 0
    assert store.stats()["duplicate_bars"] == 0


def test_no_lookahead_in_full_backtest(tmp_path, bars, config):
    """Every fill must be priced from a bar that had not closed when the order was sent."""
    cfg = BacktestConfig.from_dict(config.to_dict())
    cfg.run_id = "lookahead_run"
    result = BacktestRunner(cfg, source=MemoryBarSource.from_bars(bars), output_root=tmp_path / "la").run()
    orders = result.orders.set_index("order_id")
    for trade in result.trades.itertuples():
        order = orders.loc[trade.order_id]
        assert pd.Timestamp(trade.ts) > pd.Timestamp(order.created_at)
