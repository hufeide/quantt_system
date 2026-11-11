"""Bar sources: deterministic, seekable feeds consumed by the replay engine."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from core.market.models import Bar


class BarSource(Protocol):
    def reset(self) -> None: ...

    def next_group(self) -> tuple[datetime, list[Bar]] | None: ...

    @property
    def cursor(self) -> int: ...

    def seek(self, index: int) -> None: ...

    def __len__(self) -> int: ...


class MemoryBarSource:
    """In-memory, indexable group feed (also used by tests and checkpoints)."""

    def __init__(self, groups: Sequence[tuple[datetime, list[Bar]]]) -> None:
        self._groups = list(groups)
        self._cursor = 0

    @classmethod
    def from_bars(cls, bars: Sequence[Bar]) -> "MemoryBarSource":
        groups: list[tuple[datetime, list[Bar]]] = []
        for bar in sorted(bars, key=lambda b: (b.ts, b.symbol)):
            if groups and groups[-1][0] == bar.ts:
                groups[-1][1].append(bar)
            else:
                groups.append((bar.ts, [bar]))
        return cls(groups)

    def reset(self) -> None:
        self._cursor = 0

    def next_group(self) -> tuple[datetime, list[Bar]] | None:
        if self._cursor >= len(self._groups):
            return None
        group = self._groups[self._cursor]
        self._cursor += 1
        return group

    def peek_group(self) -> tuple[datetime, list[Bar]] | None:
        if self._cursor >= len(self._groups):
            return None
        return self._groups[self._cursor]

    @property
    def cursor(self) -> int:
        return self._cursor

    def seek(self, index: int) -> None:
        if index < 0 or index > len(self._groups):
            raise IndexError(f"cursor {index} outside [0, {len(self._groups)}]")
        self._cursor = index

    def __len__(self) -> int:
        return len(self._groups)

    @property
    def groups(self) -> Sequence[tuple[datetime, list[Bar]]]:
        return self._groups


class ParquetBarSource(MemoryBarSource):
    """Loads a versioned Parquet dataset into memory as a seekable group feed."""

    def __init__(
        self,
        root: str = "data_files",
        version: str = "v001",
        frequency: str = "1m",
        symbols: Sequence[str] | None = None,
        start=None,
        end=None,
    ) -> None:
        from core.market.store import ParquetMarketDataStore

        store = ParquetMarketDataStore(root, version, frequency)
        groups = store.load_groups(symbols, start, end)
        if not groups:
            raise FileNotFoundError(
                f"no bars in dataset {version} ({frequency}) for symbols={symbols} range={start}..{end}"
            )
        super().__init__(groups)
