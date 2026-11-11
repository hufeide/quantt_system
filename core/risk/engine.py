"""Risk engine: pre-trade checks and portfolio guard rails."""
from __future__ import annotations

from dataclasses import dataclass

from core.account.engine import AccountEngine
from core.config import RiskConfig
from core.logging import get_logger
from core.matching.models import Order, OrderSide, OrderType

logger = get_logger(__name__)


@dataclass
class RiskDecision:
    approved: bool
    rule: str = ""
    reason: str = ""

    def __bool__(self) -> bool:
        return self.approved


class RiskEngine:
    def __init__(self, config: RiskConfig | None = None, allow_short: bool = False) -> None:
        self.config = config or RiskConfig()
        self.allow_short = allow_short
        self.orders_today = 0
        self.current_day: str | None = None
        self.halted = False
        self.halt_reason = ""
        self.rejections: list[dict] = []

    def on_new_trading_day(self, day: str) -> None:
        self.current_day = day
        self.orders_today = 0

    def check(
        self, order: Order, account: AccountEngine, price: float, pending_notional: float = 0.0
    ) -> RiskDecision:
        cfg = self.config
        symbol = order.symbol

        if self.halted:
            return self._reject(order, "halted", self.halt_reason or "trading halted")

        if symbol in cfg.banned_symbols:
            return self._reject(order, "banned_symbol", f"{symbol} is banned")

        if order.quantity <= 0:
            return self._reject(order, "invalid_quantity", "quantity must be positive")

        if order.order_type == OrderType.LIMIT.value and (order.limit_price or 0) <= 0:
            return self._reject(order, "invalid_limit_price", "limit price must be positive")

        if self.current_day is not None and self.orders_today >= cfg.max_orders_per_day:
            return self._reject(order, "max_orders_per_day", f"daily order cap {cfg.max_orders_per_day} hit")

        ref_price = price if price > 0 else 0.0
        if ref_price <= 0:
            return self._reject(order, "no_market_price", f"no reference price for {symbol}")

        notional = ref_price * order.quantity
        if notional > cfg.max_order_value + 1e-6:
            return self._reject(
                order, "max_order_value", f"order value {notional:.2f} exceeds {cfg.max_order_value:.2f}"
            )

        equity = account.equity
        if equity <= 0:
            return self._reject(order, "no_equity", "account equity is zero")

        if cfg.max_drawdown > 0 and account.drawdown >= cfg.max_drawdown:
            self.halted = True
            self.halt_reason = f"max drawdown {account.drawdown:.2%} reached"
            return self._reject(order, "max_drawdown", self.halt_reason)

        pos = account.positions.get(symbol)
        current_value = pos.market_value if pos else 0.0

        if order.side == OrderSide.BUY.value:
            projected = current_value + notional
            if projected / equity > cfg.max_position_weight + 1e-9:
                return self._reject(
                    order,
                    "max_position_weight",
                    f"projected weight {projected / equity:.2%} > {cfg.max_position_weight:.2%}",
                )
            projected_exposure = (account.market_value + notional) / equity
            if projected_exposure > cfg.max_total_exposure + 1e-9:
                return self._reject(
                    order,
                    "max_total_exposure",
                    f"projected exposure {projected_exposure:.2%} > {cfg.max_total_exposure:.2%}",
                )
            required_cash = (
                notional * (1.0 + max(cfg.cash_buffer, 0.0))
                + max(pending_notional, 0.0)
                + max(cfg.min_cash_reserve, 0.0)
            )
            if required_cash > account.cash + 1e-6:
                return self._reject(
                    order,
                    "insufficient_cash",
                    f"need {required_cash:.2f}, cash {account.cash:.2f}",
                )
        else:
            if not self.allow_short and account.get_quantity(symbol) < order.quantity - 1e-9:
                return self._reject(
                    order,
                    "insufficient_position",
                    f"hold {account.get_quantity(symbol)}, sell {order.quantity}",
                )

        return RiskDecision(True, "ok", "")

    def _reject(self, order: Order, rule: str, reason: str) -> RiskDecision:
        order.status = "rejected"
        order.reject_reason = reason
        self.rejections.append({"order_id": order.order_id, "symbol": order.symbol, "rule": rule, "reason": reason})
        logger.info("risk rejected %s (%s): %s", order.order_id, rule, reason)
        return RiskDecision(False, rule, reason)

    def register_order(self) -> None:
        self.orders_today += 1

    def snapshot(self) -> dict:
        return {
            "orders_today": self.orders_today,
            "current_day": self.current_day,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "rejections": list(self.rejections),
        }

    def restore(self, state: dict) -> None:
        self.orders_today = state.get("orders_today", 0)
        self.current_day = state.get("current_day")
        self.halted = state.get("halted", False)
        self.halt_reason = state.get("halt_reason", "")
        self.rejections = list(state.get("rejections") or [])

    def reset_state(self) -> None:
        self.orders_today = 0
        self.current_day = None
        self.halted = False
        self.halt_reason = ""
        self.rejections.clear()
