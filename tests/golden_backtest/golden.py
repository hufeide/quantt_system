"""Golden backtest: fixed dataset + fixed strategy + fixed params.

Any change to the engine that alters trades, fees or the equity curve shows up
as a mismatch against expected_metrics.json. This is the regression net for the
quant core described in section 9 of the plan.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from core.backtest.runner import BacktestResult, BacktestRunner
from core.config import BacktestConfig
from core.market.calendar import TradingCalendar
from core.market.store import ParquetMarketDataStore
from core.storage.parquet import BAR_SCHEMA, write_partitioned
from tests.conftest import make_bars

GOLDEN_VERSION = "golden_v1"
GOLDEN_SYMBOLS = ["600000.SH", "000001.SZ", "300750.SZ"]
GOLDEN_SEED = 20250102
GOLDEN_START = date(2025, 1, 2)
GOLDEN_END = date(2025, 1, 8)
GOLDEN_STRATEGY = "sma_cross"
GOLDEN_PARAMS = {"fast": 8, "slow": 30, "weight": 0.3}
GOLDEN_INITIAL_CASH = 1_000_000.0

EXPECTED_PATH = Path(__file__).parent / "expected_metrics.json"

FLOAT_FIELDS = (
    "final_equity",
    "total_return",
    "max_drawdown",
    "sharpe",
    "total_fees",
    "realized_pnl",
    "turnover",
)
INT_FIELDS = ("trade_count", "round_trips")


def golden_days() -> list[date]:
    return TradingCalendar().trading_days(GOLDEN_START, GOLDEN_END)


def build_dataset(root: str | Path) -> Path:
    root = Path(root)
    target = root / "normalized" / f"dataset={GOLDEN_VERSION}" / "frequency=1m"
    marker = target / "_ready"
    if marker.exists():
        return target
    bars = make_bars(symbols=GOLDEN_SYMBOLS, days=golden_days(), seed=GOLDEN_SEED, vol=0.0022)
    df = pd.DataFrame([b.to_dict() for b in bars])
    df["ts"] = pd.to_datetime(df["ts"])
    df["dataset_version"] = GOLDEN_VERSION
    df["year"] = df["ts"].dt.year.astype(str)
    write_partitioned(df, target, partition_cols=("year",), schema=BAR_SCHEMA)
    marker.write_text("ok", encoding="utf-8")
    return target


def golden_config(root: str | Path) -> BacktestConfig:
    root = Path(root)
    cfg = BacktestConfig(
        strategy=GOLDEN_STRATEGY,
        strategy_params=dict(GOLDEN_PARAMS),
        symbols=list(GOLDEN_SYMBOLS),
        start=GOLDEN_START,
        end=GOLDEN_END,
        initial_cash=GOLDEN_INITIAL_CASH,
        frequency="1m",
        dataset_version=GOLDEN_VERSION,
        data_root=str(root),
        output_root=str(root / "backtests"),
        checkpoint_every=2_000,
        seed=GOLDEN_SEED,
        run_id="golden",
    )
    cfg.risk.max_position_weight = 0.35
    cfg.risk.max_order_value = 500_000.0
    cfg.risk.max_orders_per_day = 50
    return cfg


def run_golden(root: str | Path = "data_files/golden", run_id: str = "golden") -> BacktestResult:
    root = Path(root)
    build_dataset(root)
    cfg = golden_config(root)
    cfg.run_id = run_id
    store = ParquetMarketDataStore(root, GOLDEN_VERSION, "1m")
    assert store.exists(), "golden dataset missing"
    runner = BacktestRunner(cfg, output_root=root / "backtests", run_id=run_id)
    return runner.run()


def snapshot(result: BacktestResult) -> dict:
    metrics = result.metrics
    return {
        "fingerprint": result.fingerprint,
        "trade_count": metrics["trade_count"],
        "round_trips": metrics["round_trips"],
        "final_equity": metrics["final_equity"],
        "total_return": metrics["total_return"],
        "max_drawdown": metrics["max_drawdown"],
        "sharpe": metrics["sharpe"],
        "total_fees": metrics["total_fees"],
        "realized_pnl": metrics["realized_pnl"],
        "turnover": metrics["turnover"],
        "trades_digest": trades_digest(result.trades),
    }


def trades_digest(trades: pd.DataFrame) -> str:
    import hashlib

    if trades is None or trades.empty:
        return "empty"
    cols = [c for c in ("ts", "symbol", "side", "quantity", "price", "commission") if c in trades.columns]
    payload = trades[cols].astype(str).to_csv(index=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def load_expected() -> dict | None:
    if EXPECTED_PATH.exists():
        return json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    return None


def save_expected(snapshot_dict: dict) -> Path:
    EXPECTED_PATH.write_text(json.dumps(snapshot_dict, indent=2, ensure_ascii=False), encoding="utf-8")
    return EXPECTED_PATH


def compare(actual: dict, expected: dict, tolerance: float = 1e-6) -> list[str]:
    problems: list[str] = []
    for field in INT_FIELDS:
        if field in expected and actual.get(field) != expected[field]:
            problems.append(f"{field}: expected {expected[field]}, got {actual.get(field)}")
    for field in FLOAT_FIELDS:
        if field not in expected:
            continue
        want = expected[field]
        got = actual.get(field)
        if got is None:
            problems.append(f"{field}: expected {want}, got None")
            continue
        if abs(got - want) > tolerance * max(1.0, abs(want)):
            problems.append(f"{field}: expected {want}, got {got}")
    if expected.get("trades_digest") and actual.get("trades_digest") != expected["trades_digest"]:
        problems.append("trades_digest changed (execution path differs)")
    return problems
