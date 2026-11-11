"""Account engine: cash, positions, fees, equity curve, P&L."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.matching.models import OrderSide, Trade


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    available: float = 0.0
    avg_cost: float = 0.0
    last_price: float = 0.0
    realized_pnl: float = 0.0
    frozen: float = 0.0
    opened_at: datetime | None = None

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price

    @property
    def cost_value(self) -> float:
        return self.quantity * self.avg_cost

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_value

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "available": self.available,
            "frozen": self.frozen,
            "avg_cost": self.avg_cost,
            "last_price": self.last_price,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
        }


class AccountEngine:
    """Tracks cash and positions; T+1 settlement for A-share style trading."""

    def __init__(self, initial_cash: float = 1_000_000.0, t_plus_1: bool = True) -> None:
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.t_plus_1 = t_plus_1
        self.positions: dict[str, Position] = {}
        self.equity_curve: list[dict[str, Any]] = []
        self.position_history: list[dict[str, Any]] = []
        self.fees_paid = 0.0
        self.realized_pnl = 0.0
        self.turnover = 0.0
        self.trade_count = 0
        self.peak_equity = float(initial_cash)
        self.drawdown = 0.0
        self.max_drawdown = 0.0
        self._last_day = None
        self._last_ts: datetime | None = None
        self._last_prices: dict[str, float] = {}

    # ------------------------------------------------------------------ prices
    def mark(self, symbol: str, price: float) -> None:
        self._last_prices[symbol] = price
        pos = self.positions.get(symbol)
        if pos is not None:
            pos.last_price = price

    def mark_many(self, prices: dict[str, float]) -> None:
        for symbol, price in prices.items():
            self.mark(symbol, price)

    @property
    def market_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def equity(self) -> float:
        return self.cash + self.market_value

    @property
    def exposure(self) -> float:
        equity = self.equity
        return self.market_value / equity if equity > 0 else 0.0

    def position(self, symbol: str) -> Position:
        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self.positions[symbol] = pos
        return pos

    def get_quantity(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else 0.0

    # ------------------------------------------------------------------ trades
    def apply_trade(self, trade: Trade) -> None:
        pos = self.position(trade.symbol)
        qty = trade.quantity
        price = trade.price
        fee = trade.commission
        self.fees_paid += fee
        self.turnover += price * qty
        self.trade_count += 1
        if trade.ts is not None:
            self._last_ts = trade.ts

        if trade.side == OrderSide.BUY.value:
            cost = price * qty + fee
            if self.cash < cost - 1e-6:
                raise ValueError(
                    f"insufficient cash for {trade.symbol}: need {cost:.2f}, have {self.cash:.2f}"
                )
            self.cash -= cost
            new_qty = pos.quantity + qty
            pos.avg_cost = (pos.avg_cost * pos.quantity + price * qty + fee) / new_qty if new_qty else 0.0
            pos.quantity = new_qty
            if self.t_plus_1:
                pos.frozen += qty
            else:
                pos.available = pos.quantity
            pos.last_price = price
            if pos.opened_at is None:
                pos.opened_at = trade.ts
        else:
            if pos.quantity < qty - 1e-9:
                raise ValueError(
                    f"cannot sell {qty} of {trade.symbol}: holding {pos.quantity}"
                )
            proceeds = price * qty - fee
            self.cash += proceeds
            pnl = (price - pos.avg_cost) * qty - fee
            pos.realized_pnl += pnl
            self.realized_pnl += pnl
            pos.quantity -= qty
            pos.available = max(pos.available - qty, 0.0)
            pos.last_price = price
            if pos.quantity <= 1e-9:
                pos.quantity = 0.0
                pos.available = 0.0
                pos.frozen = 0.0
                pos.avg_cost = 0.0
                pos.opened_at = None

    # ------------------------------------------------------------------ days
    def on_new_trading_day(self, day: str | None = None) -> None:
        for pos in self.positions.values():
            pos.available = pos.quantity
            pos.frozen = 0.0
        self._last_day = day

    # ------------------------------------------------------------------ equity
    def record(self, ts: datetime) -> dict[str, Any]:
        equity = self.equity
        self.peak_equity = max(self.peak_equity, equity)
        self.drawdown = 1 - equity / self.peak_equity if self.peak_equity > 0 else 0.0
        self.max_drawdown = max(self.max_drawdown, self.drawdown)
        row = {
            "ts": ts,
            "cash": round(self.cash, 6),
            "market_value": round(self.market_value, 6),
            "equity": round(equity, 6),
            "drawdown": round(self.drawdown, 8),
            "exposure": round(self.exposure, 8),
            "fees_paid": round(self.fees_paid, 6),
            "realized_pnl": round(self.realized_pnl, 6),
        }
        self.equity_curve.append(row)
        return row

    def record_positions(self, ts: datetime) -> None:
        for pos in self.positions.values():
            if pos.quantity <= 0:
                continue
            self.position_history.append(
                {
                    "ts": ts,
                    "symbol": pos.symbol,
                    "quantity": pos.quantity,
                    "available": pos.available,
                    "avg_cost": round(pos.avg_cost, 6),
                    "last_price": round(pos.last_price, 6),
                    "market_value": round(pos.market_value, 6),
                    "unrealized_pnl": round(pos.unrealized_pnl, 6),
                    "realized_pnl": round(pos.realized_pnl, 6),
                }
            )

    # ------------------------------------------------------------------ state
    def snapshot(self) -> dict[str, Any]:
        return {
            "cash": self.cash,
            "initial_cash": self.initial_cash,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "equity_curve_len": len(self.equity_curve),
            "position_history_len": len(self.position_history),
            "fees_paid": self.fees_paid,
            "realized_pnl": self.realized_pnl,
            "turnover": self.turnover,
            "trade_count": self.trade_count,
            "peak_equity": self.peak_equity,
            "drawdown": self.drawdown,
            "max_drawdown": self.max_drawdown,
            "last_day": self._last_day,
        }

    def restore(self, state: dict[str, Any]) -> None:
        self.cash = state.get("cash", self.initial_cash)
        self.initial_cash = state.get("initial_cash", self.initial_cash)
        self.positions = {}
        for symbol, data in (state.get("positions") or {}).items():
            self.positions[symbol] = Position(
                symbol=symbol,
                quantity=data.get("quantity", 0.0),
                available=data.get("available", 0.0),
                avg_cost=data.get("avg_cost", 0.0),
                last_price=data.get("last_price", 0.0),
                realized_pnl=data.get("realized_pnl", 0.0),
                frozen=data.get("frozen", 0.0),
            )
        self.fees_paid = state.get("fees_paid", 0.0)
        self.realized_pnl = state.get("realized_pnl", 0.0)
        self.turnover = state.get("turnover", 0.0)
        self.trade_count = state.get("trade_count", 0)
        self.peak_equity = state.get("peak_equity", self.initial_cash)
        self.drawdown = state.get("drawdown", 0.0)
        self.max_drawdown = state.get("max_drawdown", 0.0)
        self._last_day = state.get("last_day")
        # keep only the history prefix that existed at checkpoint time
        self.equity_curve = self.equity_curve[: state.get("equity_curve_len", 0)]
        self.position_history = self.position_history[: state.get("position_history_len", 0)]

    def reset_state(self) -> None:
        self.cash = self.initial_cash
        self.positions.clear()
        self.equity_curve.clear()
        self.position_history.clear()
        self.fees_paid = 0.0
        self.realized_pnl = 0.0
        self.turnover = 0.0
        self.trade_count = 0
        self.peak_equity = self.initial_cash
        self.drawdown = 0.0
        self.max_drawdown = 0.0
        self._last_day = None
