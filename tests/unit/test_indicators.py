"""Indicator maths and history buffer behaviour."""
from __future__ import annotations

import numpy as np
import pytest

from core.market.history import HistoryBuffer, PanelHistory
from core.market.models import Bar
from core.strategy import indicators as ind
from tests.conftest import make_bars


def test_sma_and_ema_are_causal():
    values = np.arange(1, 11, dtype=float)
    sma = ind.sma(values, 3)
    assert sma.tolist() == pytest.approx([2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0])
    ema = ind.ema(values, 3)
    assert len(ema) == len(values)
    # the first EMA value is the first observation, later values lag the raw series
    assert ema[0] == values[0]
    assert ema[-1] < values[-1]


def test_rsi_bounds():
    rng = np.random.default_rng(0)
    values = np.cumsum(rng.normal(0, 1, 200)) + 100
    rsi = ind.rsi(values, 14)
    assert len(rsi) == len(values) - 14
    assert np.all(rsi >= 0) and np.all(rsi <= 100)


def test_zscore_and_momentum():
    values = np.array([1, 2, 3, 4, 5], dtype=float)
    z = ind.zscore(values, 5)
    assert z[0] == pytest.approx(1.4142136, rel=1e-5)
    mom = ind.momentum(values, 2)
    assert mom.tolist() == pytest.approx([2.0, 1.0, 2 / 3])


def test_bollinger_bands_ordering():
    rng = np.random.default_rng(1)
    values = np.cumsum(rng.normal(0, 1, 100)) + 50
    lower, mid, upper = ind.bollinger(values, 20, 2.0)
    assert len(lower) == len(mid) == len(upper)
    assert np.all(lower <= mid) and np.all(mid <= upper)


def test_history_buffer_respects_maxlen_and_lookback():
    bars = make_bars(days=[__import__("datetime").date(2025, 1, 2)])
    buf = HistoryBuffer("600000.SH", maxlen=30)
    for bar in bars:
        buf.append(bar)
    assert len(buf) == 30
    assert buf.close_at(0) == bars[-1].close
    assert buf.close_at(5) == bars[-6].close
    with pytest.raises(IndexError):
        buf.close_at(100)


def test_panel_history_isolates_symbols():
    panel = PanelHistory(["A", "B"], maxlen=10)
    panel.append(Bar("A", __import__("datetime").datetime(2025, 1, 2, 9, 30), 1, 1, 1, 1, 10))
    panel.append(Bar("B", __import__("datetime").datetime(2025, 1, 2, 9, 30), 2, 2, 2, 2, 10))
    assert len(panel["A"]) == 1
    assert panel["B"].last.close == 2
    panel.reset()
    assert len(panel["A"]) == 0
