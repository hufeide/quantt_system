"""ParquetBarSource + dataset filtering."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from core.market.store import DatasetNotFound, ParquetMarketDataStore
from core.replay.feed import ParquetBarSource
from tests.golden_backtest.golden import GOLDEN_SYMBOLS, GOLDEN_VERSION, build_dataset


def test_source_groups_and_cursor(tmp_path):
    build_dataset(tmp_path)
    src = ParquetBarSource(
        root=str(tmp_path), version=GOLDEN_VERSION, frequency="1m", symbols=[GOLDEN_SYMBOLS[0]]
    )
    total = len(src)
    assert total == 5 * 240
    first = src.next_group()
    assert first is not None
    assert len(first[1]) == 1
    assert src.cursor == 1
    src.seek(total - 1)
    assert src.next_group() is not None
    assert src.next_group() is None
    src.reset()
    assert src.cursor == 0


def test_date_range_filter(tmp_path):
    build_dataset(tmp_path)
    store = ParquetMarketDataStore(tmp_path, GOLDEN_VERSION, "1m")
    df = store.load_frame(symbols=GOLDEN_SYMBOLS, start=date(2025, 1, 3), end=date(2025, 1, 3))
    assert not df.empty
    assert df["ts"].min() >= datetime(2025, 1, 3, 9, 30)
    assert df["ts"].max() < datetime(2025, 1, 4)
    assert set(df["symbol"].unique()) == set(GOLDEN_SYMBOLS)


def test_missing_dataset_raises(tmp_path):
    store = ParquetMarketDataStore(tmp_path, "nope", "1m")
    assert not store.exists()
    with pytest.raises(DatasetNotFound):
        store.load_frame()
