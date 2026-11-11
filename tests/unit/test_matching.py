"""Matching engine rules."""
from __future__ import annotations

from datetime import datetime

from core.config import FeeConfig, MatchingConfig
from core.market.models import Bar
from core.matching.engine import MatchingEngine


def bar(ts, o, h, low, c, v=100_000.0):
    return Bar("600000.SH", ts, o, h, low, c, v)


def test_market_order_fills_at_next_bar_open_with_slippage():
    m = MatchingEngine(MatchingConfig(slippage_bps=10.0), FeeConfig())
    o = m.create_order("600000.SH", "buy", 1000, created_at=datetime(2025, 1, 2, 9, 31))
    m.register(o)
    assert m.on_bar(bar(datetime(2025, 1, 2, 9, 30), 10, 10.1, 9.9, 10.0)) == []  # too early
    trades = m.on_bar(bar(datetime(2025, 1, 2, 9, 31), 10.0, 10.2, 9.9, 10.1))
    assert len(trades) == 1
    assert trades[0].price == 10.01  # 10 * (1 + 10bps)
    assert trades[0].quantity == 1000
    assert o.status == "filled"


def test_limit_order_only_fills_when_touched():
    m = MatchingEngine(MatchingConfig(), FeeConfig())
    o = m.create_order(
        "600000.SH", "buy", 500, order_type="limit", limit_price=9.5, created_at=datetime(2025, 1, 2, 9, 31)
    )
    m.register(o)
    assert m.on_bar(bar(datetime(2025, 1, 2, 9, 31), 10, 10.2, 9.8, 10.0)) == []
    trades = m.on_bar(bar(datetime(2025, 1, 2, 9, 32), 9.9, 10.0, 9.4, 9.6))
    assert len(trades) == 1
    assert trades[0].price == 9.5


def test_participation_cap_and_lot_rounding():
    m = MatchingEngine(MatchingConfig(participation_rate=0.01, lot_size=100), FeeConfig())
    o = m.create_order("600000.SH", "buy", 5000, created_at=datetime(2025, 1, 2, 9, 31))
    m.register(o)
    trades = m.on_bar(bar(datetime(2025, 1, 2, 9, 31), 10, 10.2, 9.9, 10.0, v=10_000))
    assert trades[0].quantity == 100  # 1% of 10k volume
    assert o.status == "partially_filled"
    assert o.remaining == 4900


def test_locked_bar_offers_no_liquidity():
    m = MatchingEngine(MatchingConfig(), FeeConfig())
    o = m.create_order("600000.SH", "buy", 100, created_at=datetime(2025, 1, 2, 9, 31))
    m.register(o)
    assert m.on_bar(bar(datetime(2025, 1, 2, 9, 31), 11, 11, 11, 11)) == []


def test_fees_are_asymmetric():
    fees = FeeConfig(commission_rate=0.0003, min_commission=5.0, stamp_duty_rate=0.0005, transfer_fee_rate=0.00001)
    buy = fees.compute("buy", 10.0, 10_000)
    sell = fees.compute("sell", 10.0, 10_000)
    assert buy["stamp_duty"] == 0.0
    assert sell["stamp_duty"] == 50.0
    assert buy["total"] == 31.0  # 30 commission + 1 transfer fee
    assert sell["total"] == 81.0
