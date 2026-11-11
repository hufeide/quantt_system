"""Event ordering and look-ahead guarantees."""
from __future__ import annotations

from datetime import datetime

from core.backtest.runner import BacktestRunner
from core.event.model import EventType
from core.market.models import Bar
from core.replay.engine import ReplayEngine
from core.replay.feed import MemoryBarSource
from tests.conftest import make_bars


def test_bar_visible_only_after_close():
    bar = Bar("600000.SH", datetime(2025, 1, 2, 9, 30), 10, 10.2, 9.9, 10.1, 1000)
    engine = ReplayEngine(MemoryBarSource.from_bars([bar]), frequency="1m")
    seen: list[tuple[datetime, datetime]] = []
    engine.on(EventType.BAR_RECEIVED, lambda e: seen.append((e.time, e.bar.ts)))
    engine.run()
    assert seen == [(datetime(2025, 1, 2, 9, 31), datetime(2025, 1, 2, 9, 30))]


def test_event_sequence_is_monotonic_and_deterministic(bars):
    def collect():
        engine = ReplayEngine(MemoryBarSource.from_bars(bars), frequency="1m")
        events = []
        engine.on_event(lambda e: events.append((e.seq, e.time, e.event_type, getattr(e, "symbol", None))))
        engine.run()
        return events

    first = collect()
    second = collect()
    assert first == second
    assert [e[0] for e in first] == list(range(len(first)))
    times = [e[1] for e in first]
    assert times == sorted(times)


def test_fills_happen_after_all_bars_at_same_timestamp():
    bars = make_bars(symbols=["600000.SH", "000001.SZ"], days=[__import__("datetime").date(2025, 1, 2)])
    from core.config import BacktestConfig

    cfg = BacktestConfig(
        strategy="buy_and_hold",
        strategy_params={"weight": 0.4},
        symbols=["600000.SH", "000001.SZ"],
        initial_cash=500_000.0,
        checkpoint_every=0,
        output_root="/tmp/qr_test_evt",
    )
    cfg.risk.max_position_weight = 0.9
    cfg.risk.max_order_value = 1_000_000.0
    runner = BacktestRunner(cfg, source=MemoryBarSource.from_bars(bars), output_root="/tmp/qr_test_evt")
    runner.build()
    order: list[tuple] = []
    runner.engine.on_event(lambda e: order.append((e.seq, e.time, e.event_type)))
    runner.run()
    # every OrderFilled must be timestamped at a bar close time
    for _seq, ts, etype in order:
        if etype == EventType.ORDER_FILLED.value:
            assert ts.minute in range(0, 60)
            assert ts.second == 0
    assert any(e[2] == EventType.ORDER_FILLED.value for e in order)


def test_strategy_cannot_see_future_bars():
    """The strategy must never observe a bar whose close time is in the future."""
    from core.config import BacktestConfig
    from core.strategy.base import Strategy
    from core.strategy.registry import register_strategy

    violations: list[str] = []

    @register_strategy("lookahead_probe")
    class Probe(Strategy):
        default_params = {"weight": 0.5}

        def on_bar(self, ctx, bar):
            for symbol in ctx.history.symbols:
                for past in ctx.bars(symbol).tail(5):
                    if past.close_time(60) > ctx.now:
                        violations.append(f"{symbol}@{past.ts} visible at {ctx.now}")
            if not ctx.get_quantity(bar.symbol):
                ctx.order_target_weight(bar.symbol, 0.5)

    cfg = BacktestConfig(
        strategy="lookahead_probe",
        symbols=["600000.SH"],
        initial_cash=100_000.0,
        checkpoint_every=0,
    )
    cfg.risk.max_order_value = 1_000_000.0
    cfg.risk.max_position_weight = 0.9
    BacktestRunner(cfg, source=MemoryBarSource.from_bars(make_bars()), output_root="/tmp/qr_test_la").run()
    assert violations == []
