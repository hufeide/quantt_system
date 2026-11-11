"""Order and trade models."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str
    quantity: float
    order_type: str = "market"
    limit_price: float | None = None
    created_at: datetime | None = None
    status: str = OrderStatus.PENDING.value
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    commission: float = 0.0
    strategy_id: str = ""
    tag: str = ""
    reject_reason: str = ""

    @property
    def remaining(self) -> float:
        return max(self.quantity - self.filled_quantity, 0.0)

    @property
    def is_active(self) -> bool:
        return self.status in (
            OrderStatus.PENDING.value,
            OrderStatus.ACCEPTED.value,
            OrderStatus.PARTIALLY_FILLED.value,
        )

    def apply_fill(self, qty: float, price: float, commission: float) -> None:
        total_value = self.avg_fill_price * self.filled_quantity + price * qty
        self.filled_quantity += qty
        self.avg_fill_price = total_value / self.filled_quantity if self.filled_quantity else 0.0
        self.commission += commission
        if self.remaining <= 1e-9:
            self.status = OrderStatus.FILLED.value
        else:
            self.status = OrderStatus.PARTIALLY_FILLED.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "status": self.status,
            "filled_quantity": self.filled_quantity,
            "avg_fill_price": self.avg_fill_price,
            "commission": self.commission,
            "strategy_id": self.strategy_id,
            "tag": self.tag,
            "reject_reason": self.reject_reason,
        }


@dataclass
class Trade:
    trade_id: str
    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    commission: float = 0.0
    slippage_cost: float = 0.0
    ts: datetime | None = None
    strategy_id: str = ""
    tag: str = ""

    @property
    def notional(self) -> float:
        return self.price * self.quantity

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "commission": self.commission,
            "slippage_cost": self.slippage_cost,
            "ts": self.ts.isoformat() if self.ts else None,
            "strategy_id": self.strategy_id,
            "tag": self.tag,
        }
