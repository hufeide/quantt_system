"""Event model for the replay/backtest pipeline.

Event ordering is fully deterministic: every event carries the simulated time and a
monotonically increasing sequence number assigned by the engine when it is queued.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from core.market.models import Bar


class EventType(str, Enum):
    SESSION_OPEN = "SessionOpen"
    SESSION_CLOSE = "SessionClose"
    BAR_RECEIVED = "BarReceived"
    SIGNAL_GENERATED = "SignalGenerated"
    ORDER_SUBMITTED = "OrderSubmitted"
    ORDER_ACCEPTED = "OrderAccepted"
    ORDER_REJECTED = "OrderRejected"
    ORDER_FILLED = "OrderFilled"
    ORDER_CANCELLED = "OrderCancelled"
    RISK_REJECTED = "RiskRejected"
    ACCOUNT_UPDATED = "AccountUpdated"
    CHECKPOINT_SAVED = "CheckpointSaved"


@dataclass
class Event:
    time: datetime
    seq: int = -1
    priority: int = 10

    @property
    def event_type(self) -> str:  # pragma: no cover - overridden
        return "Event"

    def to_record(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": getattr(self, "symbol", None),
            "order_id": getattr(self, "order_id", None),
            "payload": {},
        }


@dataclass
class BarReceived(Event):
    bar: Bar | None = None
    priority: int = 0

    @property
    def event_type(self) -> str:
        return EventType.BAR_RECEIVED.value

    @property
    def symbol(self) -> str:
        return self.bar.symbol if self.bar else ""

    def to_record(self) -> dict[str, Any]:
        bar = self.bar
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "order_id": None,
            "payload": bar.to_dict() if bar else {},
        }


@dataclass
class SignalGenerated(Event):
    strategy_id: str = ""
    symbol: str = ""
    signal: float = 0.0
    target_weight: float | None = None
    reason: str = ""

    @property
    def event_type(self) -> str:
        return EventType.SIGNAL_GENERATED.value

    def to_record(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "order_id": None,
            "payload": {
                "strategy_id": self.strategy_id,
                "signal": self.signal,
                "target_weight": self.target_weight,
                "reason": self.reason,
            },
        }


@dataclass
class OrderSubmitted(Event):
    order_id: str = ""
    symbol: str = ""
    side: str = "buy"
    quantity: float = 0.0
    order_type: str = "market"
    limit_price: float | None = None
    priority: int = 5

    @property
    def event_type(self) -> str:
        return EventType.ORDER_SUBMITTED.value

    def to_record(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "order_id": self.order_id,
            "payload": {
                "side": self.side,
                "quantity": self.quantity,
                "order_type": self.order_type,
                "limit_price": self.limit_price,
            },
        }


@dataclass
class OrderAccepted(Event):
    order_id: str = ""
    symbol: str = ""
    priority: int = 6

    @property
    def event_type(self) -> str:
        return EventType.ORDER_ACCEPTED.value

    def to_record(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "order_id": self.order_id,
            "payload": {},
        }


@dataclass
class OrderRejected(Event):
    order_id: str = ""
    symbol: str = ""
    reason: str = ""
    source: str = "matching"
    priority: int = 6

    @property
    def event_type(self) -> str:
        return EventType.ORDER_REJECTED.value

    def to_record(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "order_id": self.order_id,
            "payload": {"reason": self.reason, "source": self.source},
        }


@dataclass
class RiskRejected(Event):
    order_id: str = ""
    symbol: str = ""
    rule: str = ""
    reason: str = ""
    priority: int = 6

    @property
    def event_type(self) -> str:
        return EventType.RISK_REJECTED.value

    def to_record(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "order_id": self.order_id,
            "payload": {"rule": self.rule, "reason": self.reason},
        }


@dataclass
class OrderFilled(Event):
    order_id: str = ""
    trade_id: str = ""
    symbol: str = ""
    side: str = "buy"
    quantity: float = 0.0
    price: float = 0.0
    commission: float = 0.0
    slippage_cost: float = 0.0
    priority: int = 7

    @property
    def event_type(self) -> str:
        return EventType.ORDER_FILLED.value

    def to_record(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "order_id": self.order_id,
            "payload": {
                "trade_id": self.trade_id,
                "side": self.side,
                "quantity": self.quantity,
                "price": self.price,
                "commission": self.commission,
                "slippage_cost": self.slippage_cost,
            },
        }


@dataclass
class OrderCancelled(Event):
    order_id: str = ""
    symbol: str = ""
    reason: str = ""
    priority: int = 7

    @property
    def event_type(self) -> str:
        return EventType.ORDER_CANCELLED.value

    def to_record(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": self.symbol,
            "order_id": self.order_id,
            "payload": {"reason": self.reason},
        }


@dataclass
class AccountUpdated(Event):
    equity: float = 0.0
    cash: float = 0.0
    market_value: float = 0.0
    drawdown: float = 0.0

    @property
    def event_type(self) -> str:
        return EventType.ACCOUNT_UPDATED.value

    def to_record(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": None,
            "order_id": None,
            "payload": {
                "equity": self.equity,
                "cash": self.cash,
                "market_value": self.market_value,
                "drawdown": self.drawdown,
            },
        }


@dataclass
class SessionEvent(Event):
    kind: EventType = EventType.SESSION_OPEN
    trading_day: str = ""
    priority: int = -1

    @property
    def event_type(self) -> str:
        return self.kind.value

    def to_record(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": None,
            "order_id": None,
            "payload": {"trading_day": self.trading_day},
        }


@dataclass
class CheckpointEvent(Event):
    checkpoint_id: str = ""
    path: str = ""

    @property
    def event_type(self) -> str:
        return EventType.CHECKPOINT_SAVED.value

    def to_record(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "simulated_time": self.time,
            "event_type": self.event_type,
            "symbol": None,
            "order_id": None,
            "payload": {"checkpoint_id": self.checkpoint_id, "path": self.path},
        }
