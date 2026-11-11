"""Strategy base class and the execution context strategies talk to."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.account.engine import AccountEngine
from core.event.model import OrderSubmitted, RiskRejected, SignalGenerated
from core.logging import get_logger
from core.market.history import HistoryBuffer, PanelHistory
from core.market.models import Bar
from core.matching.engine import MatchingEngine
from core.matching.models import Order, OrderSide
from core.risk.engine import RiskEngine

logger = get_logger(__name__)


class StrategyContext:
    """Everything a strategy may touch. Deliberately narrow: no engine internals."""

    def __init__(
        self,
        engine,
        history: PanelHistory,
        account: AccountEngine,
        matching: MatchingEngine,
        risk: RiskEngine,
        strategy_id: str = "",
        config: Any | None = None,
    ) -> None:
        self.engine = engine
        self.history = history
        self.account = account
        self.matching = matching
        self.risk = risk
        self.strategy_id = strategy_id
        self.config = config
        self.signals: list[dict] = []

    # -- market access -------------------------------------------------------
    @property
    def now(self) -> datetime | None:
        return self.engine.clock

    def bars(self, symbol: str) -> HistoryBuffer:
        return self.history.get(symbol)

    def close(self, symbol: str) -> float | None:
        buf = self.history.get(symbol)
        return buf.last.close if buf.last else None

    def position(self, symbol: str):
        return self.account.positions.get(symbol)

    def get_quantity(self, symbol: str) -> float:
        return self.account.get_quantity(symbol)

    @property
    def cash(self) -> float:
        return self.account.cash

    @property
    def equity(self) -> float:
        return self.account.equity

    # -- signals -------------------------------------------------------------
    def signal(self, symbol: str, value: float, target_weight: float | None = None, reason: str = "") -> None:
        event = SignalGenerated(
            time=self.now,
            strategy_id=self.strategy_id,
            symbol=symbol,
            signal=value,
            target_weight=target_weight,
            reason=reason,
        )
        self.signals.append(event.to_record())
        self.engine.emit(event)

    # -- order API -----------------------------------------------------------
    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        limit_price: float | None = None,
        tag: str = "",
    ) -> Order | None:
        if quantity <= 0:
            return None
        price = self.close(symbol) or 0.0
        pending = sum(
            o.remaining * (o.limit_price or self.close(o.symbol) or 0.0)
            for o in self.matching.orders.values()
            if o.is_active and o.side == OrderSide.BUY.value
        )
        order = self.matching.create_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            created_at=self.now,
            strategy_id=self.strategy_id,
            tag=tag,
        )
        decision = self.risk.check(order, self.account, price, pending_notional=pending)
        if not decision.approved:
            self.engine.emit(
                RiskRejected(
                    time=self.now,
                    order_id=order.order_id,
                    symbol=symbol,
                    rule=decision.rule,
                    reason=decision.reason,
                )
            )
            return None
        self.risk.register_order()
        self.matching.register(order)
        self.engine.emit(
            OrderSubmitted(
                time=self.now,
                order_id=order.order_id,
                symbol=symbol,
                side=side,
                quantity=order.quantity,
                order_type=order_type,
                limit_price=limit_price,
            )
        )
        return order

    def pending_quantity(self, symbol: str) -> float:
        """Signed quantity still working for this symbol (buys positive)."""
        total = 0.0
        for order in self.matching.orders.values():
            if order.symbol != symbol or not order.is_active:
                continue
            total += order.remaining if order.side == OrderSide.BUY.value else -order.remaining
        return total

    def cancel_open_orders(self, symbol: str, reason: str = "rebalance") -> int:
        cancelled = 0
        for order in list(self.matching.orders.values()):
            if order.symbol == symbol and order.is_active:
                self.matching.cancel(order.order_id, reason)
                cancelled += 1
        return cancelled

    def order_target_weight(self, symbol: str, weight: float, tag: str = "") -> Order | None:
        """Rebalance a symbol to a target portfolio weight.

        Working orders for the symbol are cancelled first so a rebalance always
        reflects the real position instead of stacking duplicate orders.
        """
        self.cancel_open_orders(symbol, reason="rebalance")
        price = self.close(symbol)
        if not price or price <= 0:
            return None
        equity = self.account.equity
        target_value = max(0.0, min(weight, 1.0)) * equity
        target_qty = int(target_value / price / 100) * 100
        current = self.account.get_quantity(symbol)
        delta = target_qty - current
        if abs(delta) < 100:
            return None
        side = OrderSide.BUY.value if delta > 0 else OrderSide.SELL.value
        return self.submit_order(symbol, side, abs(delta), tag=tag)

    def close_position(self, symbol: str, tag: str = "close") -> Order | None:
        self.cancel_open_orders(symbol, reason="close_position")
        qty = self.account.get_quantity(symbol)
        if qty <= 0:
            return None
        return self.submit_order(symbol, OrderSide.SELL.value, qty, tag=tag)

    def buy(self, symbol: str, quantity: float, **kwargs) -> Order | None:
        return self.submit_order(symbol, OrderSide.BUY.value, quantity, **kwargs)

    def sell(self, symbol: str, quantity: float, **kwargs) -> Order | None:
        return self.submit_order(symbol, OrderSide.SELL.value, quantity, **kwargs)


class Strategy:
    """Base class. Subclasses implement on_bar / on_start / on_finish."""

    name: str = "base"
    description: str = ""
    default_params: dict[str, Any] = {}
    param_schema: dict[str, dict[str, Any]] = {}

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        merged = dict(self.default_params)
        merged.update(params or {})
        unknown = set(merged) - set(self.default_params)
        if unknown and self.default_params:
            logger.warning("%s received unknown params: %s", self.name, sorted(unknown))
        self.params = merged
        self.ctx: StrategyContext | None = None
        self.state: dict[str, Any] = {}

    # -- lifecycle -----------------------------------------------------------
    def on_start(self, ctx: StrategyContext) -> None:
        self.ctx = ctx

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        raise NotImplementedError

    def on_order_filled(self, ctx: StrategyContext, event) -> None:
        pass

    def on_order_rejected(self, ctx: StrategyContext, event) -> None:
        pass

    def on_finish(self, ctx: StrategyContext) -> None:
        pass

    # -- state ---------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return dict(self.state)

    def restore(self, state: dict[str, Any]) -> None:
        self.state = dict(state or {})

    def reset_state(self) -> None:
        self.state = {}
