"""Event store journal tests."""
from __future__ import annotations

import json

from core.event.store import EventStore


def records():
    return [
        {
            "seq": i,
            "simulated_time": f"2025-01-02T09:{30 + i:02d}:00",
            "event_type": "BarReceived" if i % 2 == 0 else "OrderFilled",
            "symbol": "600000.SH",
            "order_id": None if i % 2 == 0 else f"ord_{i}",
            "payload": {"close": 10 + i},
        }
        for i in range(5)
    ]


def test_append_flush_and_read(tmp_path):
    store = EventStore(tmp_path / "events.jsonl", run_id="r1", buffer_size=2)
    store.extend(records())
    store.close()
    assert store.count == 5
    lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 5
    assert json.loads(lines[0])["run_id"] == "r1"

    reopened = EventStore(tmp_path / "events.jsonl", run_id="r1")
    assert [r["seq"] for r in reopened.read_all()] == [0, 1, 2, 3, 4]
    assert len(list(reopened.iter_records(event_type="OrderFilled"))) == 2
    assert len(list(reopened.iter_records(limit=3))) == 3


def test_audit_summary_and_parquet_export(tmp_path):
    store = EventStore(tmp_path / "events.jsonl", run_id="r1", buffer_size=100)
    store.extend(records())
    store.close()
    summary = store.audit_summary()
    assert summary["count"] == 5
    assert summary["by_type"]["BarReceived"] == 3
    path = store.to_parquet()
    import pandas as pd

    df = pd.read_parquet(path)
    assert len(df) == 5
    assert set(df["event_type"]) == {"BarReceived", "OrderFilled"}
