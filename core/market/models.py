"""Market data models."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True, slots=True)
class Bar:
    """A completed OHLCV bar.

    ts is the *start* of the bar interval. The bar only becomes visible to a
    strategy at ts + interval (i.e. after it has closed).
    """

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0
    frequency: str = "1m"

    def close_time(self, seconds: int = 60) -> datetime:
        return self.ts + timedelta(seconds=seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ts": self.ts.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "frequency": self.frequency,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Bar":
        ts = data["ts"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return cls(
            symbol=data["symbol"],
            ts=ts,
            open=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]),
            close=float(data["close"]),
            volume=float(data.get("volume", 0.0)),
            amount=float(data.get("amount", 0.0)),
            frequency=data.get("frequency", "1m"),
        )

    @property
    def vwap(self) -> float:
        if self.volume > 0 and self.amount > 0:
            return self.amount / self.volume
        return (self.high + self.low + self.close) / 3.0

    def is_valid(self) -> bool:
        if self.volume < 0:
            return False
        if min(self.open, self.high, self.low, self.close) <= 0:
            return False
        if self.high < self.low:
            return False
        if not (self.low <= self.open <= self.high):
            return False
        if not (self.low <= self.close <= self.high):
            return False
        return True


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    symbol: str
    name: str = ""
    lot_size: int = 100
    price_tick: float = 0.01
    limit_pct: float = 0.10
