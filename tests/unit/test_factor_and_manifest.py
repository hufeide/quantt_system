"""Factor engine and manifest tests."""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from core.config import BacktestConfig, FeeConfig, MatchingConfig, RiskConfig
from core.factor.engine import FactorEngine, registry
from core.storage.manifest import RunManifest
from tests.conftest import make_bars


def bars_frame() -> pd.DataFrame:
    bars = make_bars(symbols=["600000.SH", "000001.SZ"], days=[date(2025, 1, 2), date(2025, 1, 3)])
    df = pd.DataFrame([b.to_dict() for b in bars])
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def test_registry_has_expected_factors():
    names = registry.names()
    for expected in ("momentum", "volatility", "ma_ratio", "volume_ratio", "rsi"):
        assert expected in names


def test_factor_computation_has_no_lookahead():
    df = bars_frame()
    factors = FactorEngine().compute(df, ["momentum"], {"momentum": {"window": 10}})
    first_valid = factors.dropna(subset=["value"]).groupby("symbol")["ts"].min()
    for symbol, ts in first_valid.items():
        symbol_bars = df[df["symbol"] == symbol].sort_values("ts").reset_index(drop=True)
        index = symbol_bars.index[symbol_bars["ts"] == ts][0]
        assert index >= 10


def test_factor_store_and_load(tmp_path):
    df = bars_frame()
    engine = FactorEngine(tmp_path, "v001")
    summary = engine.compute_and_store(df, ["momentum", "volatility"])
    assert summary["factors"]["momentum"]["rows"] > 0
    loaded = engine.load(["momentum"])
    assert not loaded.empty
    assert set(loaded["factor"]) == {"momentum"}
    pivot = engine.pivot("momentum", ["600000.SH"])
    assert not pivot.empty


def test_config_roundtrip(tmp_path):
    cfg = BacktestConfig(
        strategy="momentum",
        strategy_params={"lookback": 15},
        symbols=["600000.SH"],
        start="2025-01-02",
        end="2025-01-10",
        initial_cash=500_000,
        fees=FeeConfig(commission_rate=0.0002),
        matching=MatchingConfig(slippage_bps=2.0, lot_size=100),
        risk=RiskConfig(max_position_weight=0.5),
    )
    path = tmp_path / "cfg.yaml"
    cfg.dump(path)
    loaded = BacktestConfig.load(path)
    assert loaded.strategy == "momentum"
    assert loaded.symbols == ["600000.SH"]
    assert loaded.start == date(2025, 1, 2)
    assert loaded.fees.commission_rate == 0.0002
    assert loaded.matching.slippage_bps == 2.0
    assert loaded.risk.max_position_weight == 0.5

    json_path = tmp_path / "cfg.json"
    cfg.dump(json_path)
    assert BacktestConfig.load(json_path).to_dict() == cfg.to_dict()


def test_manifest_captures_reproducibility_inputs(tmp_path):
    cfg = BacktestConfig(
        strategy="sma_cross",
        strategy_params={"fast": 5},
        symbols=["600000.SH"],
        initial_cash=100_000,
        dataset_version="v001",
    )
    manifest = RunManifest.build(cfg, run_id="r1", strategy_source="class X: pass", dataset_checksum="abc")
    assert manifest.strategy == "sma_cross"
    assert manifest.strategy_code_hash
    assert manifest.dataset_checksum == "abc"
    assert manifest.environment["python"]
    saved = manifest.save(tmp_path / "manifest.json")
    reloaded = RunManifest.load(saved)
    assert reloaded.fingerprint() == manifest.fingerprint()
    assert json.loads(saved.read_text())["run_id"] == "r1"

    other = RunManifest.build(cfg, run_id="r2", strategy_source="class X: pass", dataset_checksum="abc")
    assert other.fingerprint() == manifest.fingerprint()
    changed = RunManifest.build(cfg, run_id="r3", strategy_source="class Y: pass")
    assert changed.fingerprint() != manifest.fingerprint()
