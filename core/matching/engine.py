"""Bar-based matching engine with A-share style trading rules."""
from __future__ import annotations

import math
from typing import Callable

from core.config import FeeConfig, MatchingConfig
from core.logging import get_logger
from core.market.models import Bar
from core.matching.models import Order, OrderSide, OrderStatus, OrderType, Trade

logger = get_logger(__name__)


class MatchingEngine:
    """Fills orders against completed bars only.

    Execution model:
      * market orders fill at the open of the first eligible bar, with slippage;
      * limit orders fill only if the bar traded through the limit price;
      * fills are capped by bar volume * participation_rate;
      * quantities are rounded down to the lot size;
      * an order becomes eligible on the first bar that starts at or after the
        submission timestamp (so it can never fill on the bar that produced the
        signal).
    """

    def __init__(
        self,
        config: MatchingConfig | None = None,
        fees: FeeConfig | None = None,
        emit: Callable | None = None,
    ) -> None:
        self.config = config or MatchingConfig()
        self.fees = fees or FeeConfig()
        self.emit = emit or (lambda event: None)
        self.orders: dict[str, Order] = {}
        self.trades: list[Trade] = []
        self._pending_by_symbol: dict[str, list[str]] = {}
        self._trade_seq = 0
        self._order_seq = 0

    # ------------------------------------------------------------------ orders
    def next_order_id(self, prefix: str = "ord") -> str:
        self._order_seq += 1
        return f"{prefix}_{self._order_seq:08d}"

    def create_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        limit_price: float | None = None,
        created_at=None,
        strategy_id: str = "",
        tag: str = "",
        order_id: str | None = None,
    ) -> Order:
        quantity = self.round_quantity(quantity)
        order = Order(
            order_id=order_id or self.next_order_id(),
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            created_at=created_at,
            strategy_id=strategy_id,
            tag=tag,
        )
        self.orders[order.order_id] = order
        return order

    def register(self, order: Order) -> None:
        """Mark an order as accepted and eligible for matching."""
        order.status = OrderStatus.ACCEPTED.value
        self._pending_by_symbol.setdefault(order.symbol, []).append(order.order_id)

    def cancel(self, order_id: str, reason: str = "cancelled") -> Order | None:
        order = self.orders.get(order_id)
        if order is None or not order.is_active:
            return None
        order.status = OrderStatus.CANCELLED.value
        order.reject_reason = reason
        self._remove_pending(order)
        return order

    def cancel_all(self, symbol: str | None = None, reason: str = "end_of_run") -> list[Order]:
        cancelled = []
        for order in list(self.orders.values()):
            if not order.is_active:
                continue
            if symbol is not None and order.symbol != symbol:
                continue
            self.cancel(order.order_id, reason)
            cancelled.append(order)
        return cancelled

    def _remove_pending(self, order: Order) -> None:
        bucket = self._pending_by_symbol.get(order.symbol)
        if bucket and order.order_id in bucket:
            bucket.remove(order.order_id)

    def round_quantity(self, quantity: float) -> float:
        lot = max(int(self.config.lot_size), 1)
        if lot <= 1:
            return float(quantity)
        return float(math.floor(abs(quantity) / lot) * lot)

    # ------------------------------------------------------------------ fills
    def on_bar(self, bar: Bar) -> list[Trade]:
        order_ids = self._pending_by_symbol.get(bar.symbol, [])
        if not order_ids:
            return []
        created: list[Trade] = []
        for order_id in list(order_ids):
            order = self.orders.get(order_id)
            if order is None or not order.is_active:
                self._remove_pending(order) if order else None
                continue
            if order.created_at is not None and bar.ts < order.created_at:
                continue
            trade = self._try_fill(order, bar)
            if trade is not None:
                created.append(trade)
            if not order.is_active:
                self._remove_pending(order)
        return created

    def _try_fill(self, order: Order, bar: Bar) -> Trade | None:
        if bar.high <= 0 or bar.low <= 0 or bar.volume <= 0:
            return None
        # A locked limit-up / limit-down bar offers no liquidity.
        if abs(bar.high - bar.low) < 1e-12:
            return None

        price: float | None = None
        if order.order_type == OrderType.MARKET.value:
            price = bar.open
            if self.config.slippage_bps:
                adj = self.config.slippage_bps / 10_000.0
                price = price * (1 + adj) if order.side == OrderSide.BUY.value else price * (1 - adj)
        else:
            limit = float(order.limit_price or 0.0)
            if limit <= 0:
                self.cancel(order.order_id, "invalid_limit_price")
                return None
            if order.side == OrderSide.BUY.value:
                if bar.low <= limit:
                    price = min(bar.open, limit)
            else:
                if bar.high >= limit:
                    price = max(bar.open, limit)

        if price is None or price <= 0:
            return None

        if self.config.price_limit_pct > 0 and bar.open > 0:
            deviation = abs(price - bar.open) / bar.open
            if deviation > self.config.price_limit_pct:
                logger.warning("fill price deviation too large, skipping: %s", order.order_id)
                return None

        max_qty = self.config.participation_rate * bar.volume
        fill_qty = min(order.remaining, max_qty)
        fill_qty = self.round_quantity(fill_qty)
        if fill_qty <= 0:
            return None
        if fill_qty < order.remaining and not self.config.allow_partial_fill:
            return None

        fee = self.fees.compute(order.side, price, fill_qty)
        slippage_cost = 0.0
        if order.order_type == OrderType.MARKET.value and self.config.slippage_bps:
            slippage_cost = abs(price - bar.open) * fill_qty

        order.apply_fill(fill_qty, price, fee["total"])
        self._trade_seq += 1
        trade = Trade(
            trade_id=f"trd_{self._trade_seq:08d}",
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill_qty,
            price=round(price, 6),
            commission=fee["total"],
            slippage_cost=round(slippage_cost, 6),
            ts=bar.close_time(60),
            strategy_id=order.strategy_id,
            tag=order.tag,
        )
        self.trades.append(trade)
        return trade

    # ------------------------------------------------------------------ state
    def snapshot(self) -> dict:
        return {
            "orders": {oid: o.to_dict() for oid, o in self.orders.items()},
            "trades": [t.to_dict() for t in self.trades],
            "trade_seq": self._trade_seq,
            "order_seq": self._order_seq,
        }

    def restore(self, state: dict) -> None:
        from datetime import datetime

        self.orders = {}
        for oid, data in (state.get("orders") or {}).items():
            created = data.get("created_at")
            self.orders[oid] = Order(
                order_id=data["order_id"],
                symbol=data["symbol"],
                side=data["side"],
                quantity=data["quantity"],
                order_type=data.get("order_type", "market"),
                limit_price=data.get("limit_price"),
                created_at=datetime.fromisoformat(created) if created else None,
                status=data.get("status", OrderStatus.PENDING.value),
                filled_quantity=data.get("filled_quantity", 0.0),
                avg_fill_price=data.get("avg_fill_price", 0.0),
                commission=data.get("commission", 0.0),
                strategy_id=data.get("strategy_id", ""),
                tag=data.get("tag", ""),
                reject_reason=data.get("reject_reason", ""),
            )
        self._pending_by_symbol = {}
        for order in self.orders.values():
            if order.is_active:
                self._pending_by_symbol.setdefault(order.symbol, []).append(order.order_id)
        self.trades = []
        for t in state.get("trades") or []:
            ts = t.get("ts")
            self.trades.append(
                Trade(
                    trade_id=t["trade_id"],
                    order_id=t["order_id"],
                    symbol=t["symbol"],
                    side=t["side"],
                    quantity=t["quantity"],
                    price=t["price"],
                    commission=t.get("commission", 0.0),
                    slippage_cost=t.get("slippage_cost", 0.0),
                    ts=datetime.fromisoformat(ts) if ts else None,
                    strategy_id=t.get("strategy_id", ""),
                    tag=t.get("tag", ""),
                )
            )
        self._trade_seq = state.get("trade_seq", len(self.trades))
        self._order_seq = state.get("order_seq", len(self.orders))

    def reset_state(self) -> None:
        self.orders.clear()
        self.trades.clear()
        self._pending_by_symbol.clear()
        self._trade_seq = 0
        self._order_seq = 0
