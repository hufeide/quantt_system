"""Shared fixtures: deterministic synthetic minute bars."""
from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from core.config import BacktestConfig
from core.market.calendar import TradingCalendar
from core.market.models import Bar
from core.replay.feed import MemoryBarSource


def make_bars(
    symbols: list[str] | None = None,
    days: list[date] | None = None,
    seed: int = 7,
    start_price: float = 10.0,
    drift: float = 0.0002,
    vol: float = 0.0015,
    volume: float = 120_000.0,
) -> list[Bar]:
    symbols = symbols or ["600000.SH"]
    days = days or [date(2025, 1, 2), date(2025, 1, 3)]
    calendar = TradingCalendar()
    rng = random.Random(seed)
    bars: list[Bar] = []
    for idx, symbol in enumerate(symbols):
        price = start_price * (1 + 0.1 * idx)
        for day in days:
            for begin, finish in calendar.sessions(day):
                ts = begin
                while ts < finish:
                    price *= 1 + drift + rng.gauss(0, vol)
                    open_ = price * (1 + rng.gauss(0, vol / 3))
                    close = price
                    high = max(open_, close) * (1 + abs(rng.gauss(0, vol / 2)))
                    low = min(open_, close) * (1 - abs(rng.gauss(0, vol / 2)))
                    vol_ = volume * (1 + rng.uniform(-0.3, 0.3))
                    bars.append(
                        Bar(
                            symbol=symbol,
                            ts=ts,
                            open=round(open_, 4),
                            high=round(high, 4),
                            low=round(low, 4),
                            close=round(close, 4),
                            volume=round(vol_, 2),
                            amount=round(vol_ * close, 2),
                        )
                    )
                    ts += timedelta(minutes=1)
    return bars


@pytest.fixture()
def bars() -> list[Bar]:
    return make_bars()


@pytest.fixture()
def source(bars) -> MemoryBarSource:
    return MemoryBarSource.from_bars(bars)


@pytest.fixture()
def config(tmp_path) -> BacktestConfig:
    cfg = BacktestConfig(
        strategy="sma_cross",
        strategy_params={"fast": 5, "slow": 20, "weight": 0.8},
        symbols=["600000.SH"],
        initial_cash=200_000.0,
        checkpoint_every=0,
        output_root=str(tmp_path / "backtests"),
        data_root=str(tmp_path / "data"),
    )
    cfg.risk.max_position_weight = 0.95
    cfg.risk.max_order_value = 1_000_000.0
    return cfg
