"""Rolling, lookahead-safe bar history buffers."""
from __future__ import annotations

from collections import deque
from typing import Iterable, Sequence

import numpy as np

from core.market.models import Bar


class HistoryBuffer:
    """Fixed-size window of completed bars for one symbol.

    The buffer only ever contains bars that have already been delivered by the
    replay engine, which makes look-ahead bias structurally impossible.
    """

    __slots__ = ("symbol", "maxlen", "_bars", "_close", "_high", "_low", "_volume", "_open")

    def __init__(self, symbol: str, maxlen: int = 2000) -> None:
        self.symbol = symbol
        self.maxlen = maxlen
        self._bars: deque[Bar] = deque(maxlen=maxlen)
        self._close: deque[float] = deque(maxlen=maxlen)
        self._high: deque[float] = deque(maxlen=maxlen)
        self._low: deque[float] = deque(maxlen=maxlen)
        self._open: deque[float] = deque(maxlen=maxlen)
        self._volume: deque[float] = deque(maxlen=maxlen)

    def append(self, bar: Bar) -> None:
        self._bars.append(bar)
        self._close.append(bar.close)
        self._high.append(bar.high)
        self._low.append(bar.low)
        self._open.append(bar.open)
        self._volume.append(bar.volume)

    def __len__(self) -> int:
        return len(self._bars)

    @property
    def last(self) -> Bar | None:
        return self._bars[-1] if self._bars else None

    @property
    def closes(self) -> np.ndarray:
        return np.fromiter(self._close, dtype=float, count=len(self._close))

    @property
    def highs(self) -> np.ndarray:
        return np.fromiter(self._high, dtype=float, count=len(self._high))

    @property
    def lows(self) -> np.ndarray:
        return np.fromiter(self._low, dtype=float, count=len(self._low))

    @property
    def opens(self) -> np.ndarray:
        return np.fromiter(self._open, dtype=float, count=len(self._open))

    @property
    def volumes(self) -> np.ndarray:
        return np.fromiter(self._volume, dtype=float, count=len(self._volume))

    def tail(self, n: int) -> Sequence[Bar]:
        if n <= 0:
            return []
        return list(self._bars)[-n:]

    def close_at(self, offset: int) -> float:
        """Close price, where 0 is the most recent completed bar."""
        if offset < 0 or offset >= len(self._close):
            raise IndexError(f"history offset {offset} out of range for {self.symbol}")
        return self._close[-1 - offset]

    def sma(self, window: int) -> float | None:
        if window <= 0 or len(self._close) < window:
            return None
        arr = np.fromiter(self._close, dtype=float, count=len(self._close))
        return float(arr[-window:].mean())

    def std(self, window: int) -> float | None:
        if window <= 0 or len(self._close) < window:
            return None
        arr = np.fromiter(self._close, dtype=float, count=len(self._close))
        return float(arr[-window:].std(ddof=0))

    def snapshot(self, n: int = 64) -> list[dict]:
        return [b.to_dict() for b in self.tail(n)]

    def clear(self) -> None:
        for d in (self._bars, self._close, self._high, self._low, self._open, self._volume):
            d.clear()


class PanelHistory:
    """A collection of per-symbol history buffers."""

    def __init__(self, symbols: Iterable[str], maxlen: int = 2000) -> None:
        self.maxlen = maxlen
        self._buffers: dict[str, HistoryBuffer] = {s: HistoryBuffer(s, maxlen) for s in symbols}

    def get(self, symbol: str) -> HistoryBuffer:
        buf = self._buffers.get(symbol)
        if buf is None:
            buf = HistoryBuffer(symbol, self.maxlen)
            self._buffers[symbol] = buf
        return buf

    def __getitem__(self, symbol: str) -> HistoryBuffer:
        return self.get(symbol)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._buffers

    @property
    def symbols(self) -> list[str]:
        return list(self._buffers)

    def append(self, bar: Bar) -> None:
        self.get(bar.symbol).append(bar)

    def reset(self) -> None:
        for buf in self._buffers.values():
            buf.clear()
