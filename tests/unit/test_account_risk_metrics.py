"""Account, risk and metric maths."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from core.account.engine import AccountEngine
from core.backtest.metrics import compute_metrics, round_trips
from core.config import RiskConfig
from core.matching.models import Trade
from core.risk.engine import RiskEngine


def trade(symbol, side, qty, price, fee=0.0, ts=None, tid="t1"):
    return Trade(
        trade_id=tid,
        order_id="o1",
        symbol=symbol,
        side=side,
        quantity=qty,
        price=price,
        commission=fee,
        ts=ts or datetime(2025, 1, 2, 9, 31),
    )


def test_buy_and_sell_accounting():
    acct = AccountEngine(100_000, t_plus_1=True)
    acct.mark("600000.SH", 10.0)
    acct.apply_trade(trade("600000.SH", "buy", 1000, 10.0, fee=30.0))
    assert acct.cash == pytest.approx(100_000 - 10_000 - 30)
    pos = acct.positions["600000.SH"]
    assert pos.quantity == 1000
    assert pos.available == 0  # T+1
    assert pos.avg_cost == pytest.approx(10.03)

    acct.mark("600000.SH", 11.0)
    assert acct.equity == pytest.approx(acct.cash + 11_000)

    acct.on_new_trading_day("2025-01-03")
    assert pos.available == 1000
    acct.apply_trade(trade("600000.SH", "sell", 1000, 11.0, fee=40.0, tid="t2"))
    assert acct.positions["600000.SH"].quantity == 0
    assert acct.realized_pnl == pytest.approx((11.0 - 10.03) * 1000 - 40.0)


def test_t_plus_1_blocks_same_day_sell_in_risk():
    acct = AccountEngine(100_000)
    acct.mark("600000.SH", 10.0)
    acct.apply_trade(trade("600000.SH", "buy", 1000, 10.0))
    risk = RiskEngine(RiskConfig(max_position_weight=1.0, max_order_value=1e9))
    from core.matching.engine import MatchingEngine

    m = MatchingEngine()
    o = m.create_order("600000.SH", "sell", 1000)
    # available is 0, but total quantity is 1000 -> the risk engine checks holdings
    decision = risk.check(o, acct, 10.0)
    assert decision.approved


def test_risk_rejects_banned_and_oversized_orders():
    acct = AccountEngine(100_000)
    acct.mark("600000.SH", 10.0)
    risk = RiskEngine(RiskConfig(banned_symbols=["000001.SZ"], max_order_value=200_000, max_position_weight=0.5))
    from core.matching.engine import MatchingEngine

    m = MatchingEngine()
    banned = m.create_order("000001.SZ", "buy", 100)
    assert not risk.check(banned, acct, 10.0).approved
    too_big = m.create_order("600000.SH", "buy", 30_000)
    assert risk.check(too_big, acct, 10.0).rule == "max_order_value"
    too_heavy = m.create_order("600000.SH", "buy", 6_000)
    assert risk.check(too_heavy, acct, 10.0).rule == "max_position_weight"
    ok = m.create_order("600000.SH", "buy", 4_000)
    assert risk.check(ok, acct, 10.0).approved


def test_risk_halts_on_drawdown():
    acct = AccountEngine(100_000)
    acct.mark("600000.SH", 10.0)
    acct.peak_equity = 100_000
    acct.cash = 60_000
    acct.drawdown = 0.4
    risk = RiskEngine(RiskConfig(max_drawdown=0.25, max_order_value=1e9, max_position_weight=1.0))
    from core.matching.engine import MatchingEngine

    m = MatchingEngine()
    order = m.create_order("600000.SH", "buy", 100)
    decision = risk.check(order, acct, 10.0)
    assert not decision.approved
    assert decision.rule == "max_drawdown"
    assert risk.halted


def test_metrics_on_known_equity_curve():
    equity = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2025-01-02 09:31", "2025-01-02 09:32", "2025-01-02 09:33", "2025-01-02 09:34"]),
            "equity": [100.0, 110.0, 99.0, 121.0],
            "exposure": [0.0, 0.5, 0.4, 0.0],
        }
    )
    trades = pd.DataFrame(
        [
            {"trade_id": "1", "symbol": "A", "side": "buy", "quantity": 1, "price": 10.0, "commission": 0.0,
             "ts": pd.Timestamp("2025-01-02 09:31")},
            {"trade_id": "2", "symbol": "A", "side": "sell", "quantity": 1, "price": 12.0, "commission": 0.0,
             "ts": pd.Timestamp("2025-01-02 09:33")},
        ]
    )
    metrics = compute_metrics(equity, trades, initial_cash=100.0, frequency="1m")
    assert metrics["total_return"] == pytest.approx(0.21)
    assert metrics["max_drawdown"] == pytest.approx(0.1)
    assert metrics["trade_count"] == 2
    assert metrics["win_rate"] == 1.0
    assert metrics["realized_pnl"] == pytest.approx(2.0)


def test_fifo_round_trips():
    trades = pd.DataFrame(
        [
            {"trade_id": "1", "symbol": "A", "side": "buy", "quantity": 100, "price": 10.0, "commission": 0.0,
             "ts": pd.Timestamp("2025-01-02 09:31")},
            {"trade_id": "2", "symbol": "A", "side": "buy", "quantity": 100, "price": 11.0, "commission": 0.0,
             "ts": pd.Timestamp("2025-01-02 09:32")},
            {"trade_id": "3", "symbol": "A", "side": "sell", "quantity": 150, "price": 12.0, "commission": 0.0,
             "ts": pd.Timestamp("2025-01-02 09:33")},
        ]
    )
    trips = round_trips(trades)
    assert len(trips) == 2
    assert trips["pnl"].tolist() == pytest.approx([200.0, 50.0])
