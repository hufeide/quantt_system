"""Append-only event store for audit and post-mortem analysis.

The Parquet artifact is the analysis copy; this JSONL journal is the durable
append-only audit log (crash-safe, line-oriented, replayable).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd

from core.storage.parquet import EVENT_SCHEMA, write_dataframe


class EventStore:
    def __init__(self, path: str | Path, run_id: str = "", buffer_size: int = 1000) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._buffer: list[dict[str, Any]] = []
        self._buffer_size = max(int(buffer_size), 1)
        self._count = 0
        self._fh = None

    def append(self, record: dict[str, Any]) -> None:
        payload = dict(record)
        payload.setdefault("run_id", self.run_id)
        self._buffer.append(payload)
        self._count += 1
        if len(self._buffer) >= self._buffer_size:
            self.flush()

    def extend(self, records: Iterable[dict[str, Any]]) -> None:
        for record in records:
            self.append(record)

    def flush(self) -> None:
        if not self._buffer:
            return
        if self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8")
        for record in self._buffer:
            self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        self._buffer.clear()

    def close(self) -> None:
        self.flush()
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def count(self) -> int:
        return self._count

    def iter_records(self, event_type: str | None = None, limit: int | None = None) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        emitted = 0
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if event_type and record.get("event_type") != event_type:
                    continue
                yield record
                emitted += 1
                if limit is not None and emitted >= limit:
                    return

    def read_all(self) -> list[dict[str, Any]]:
        return list(self.iter_records())

    def to_parquet(self, path: str | Path | None = None) -> Path:
        path = Path(path) if path else self.path.with_suffix(".parquet")
        records = self.read_all()
        rows = [
            {
                "seq": int(r.get("seq", -1)),
                "run_id": r.get("run_id", self.run_id),
                "simulated_time": pd.Timestamp(r.get("simulated_time")),
                "event_type": r.get("event_type"),
                "symbol": r.get("symbol"),
                "order_id": r.get("order_id"),
                "payload": json.dumps(r.get("payload", {}), ensure_ascii=False, default=str),
            }
            for r in records
        ]
        df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=[f.name for f in EVENT_SCHEMA])
        return write_dataframe(df, path, schema=EVENT_SCHEMA)

    def audit_summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        first_ts = last_ts = None
        for record in self.iter_records():
            key = record.get("event_type", "unknown")
            counts[key] = counts.get(key, 0) + 1
            ts = record.get("simulated_time")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
        return {"count": self._count, "by_type": counts, "first": first_ts, "last": last_ts, "path": str(self.path)}
