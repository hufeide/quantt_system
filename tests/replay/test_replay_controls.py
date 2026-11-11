"""Replay controls: pause/resume/step/seek/checkpoint/restore."""
from __future__ import annotations

from datetime import timedelta

import pytest

from core.backtest.runner import BacktestRunner
from core.config import BacktestConfig
from core.replay.engine import ReplayEngine
from core.replay.feed import MemoryBarSource
from core.replay.session import ReplaySession


def build(cfg: BacktestConfig, bars, out):
    runner = BacktestRunner(cfg, source=MemoryBarSource.from_bars(bars), output_root=out)
    runner.build()
    runner.strategy.on_start(runner.ctx)
    return runner


def clone(cfg: BacktestConfig) -> BacktestConfig:
    return BacktestConfig.from_dict(cfg.to_dict())


def test_pause_resume_and_step(bars, config):
    cfg = clone(config)
    runner = build(cfg, bars, "/tmp/qr_replay_a")
    engine = runner.engine
    engine.start()
    engine.step(5)
    assert engine.processed_events == 5
    engine.pause()
    assert engine.state == "paused"
    processed = engine.processed_events
    engine.run(max_events=10)
    assert engine.processed_events == processed + 10


def test_step_bar_advances_one_timestamp(bars, config):
    cfg = clone(config)
    runner = build(cfg, bars, "/tmp/qr_replay_b")
    engine = runner.engine
    engine.step_bar()
    assert engine.clock is not None
    first = engine.clock
    engine.step_bar()
    assert engine.clock > first


def test_checkpoint_restore_reproduces_uninterrupted_run(bars, config):
    reference = build(clone(config), bars, "/tmp/qr_ckpt_ref").run()

    cfg = clone(config)
    cfg.run_id = "ckpt_run"
    runner = build(cfg, bars, "/tmp/qr_ckpt_mid")
    mid = runner.source.groups[len(runner.source.groups) // 2][0] + timedelta(minutes=1)
    runner.run_until(mid)
    ckpt = runner.checkpoints.save(runner.engine, label="manual")
    assert ckpt.cursor > 0
    runner.engine.run()
    continued = runner.finalize()

    assert [t.trade_id for t in continued.trades.itertuples()] == [
        t.trade_id for t in reference.trades.itertuples()
    ]
    assert continued.metrics["final_equity"] == pytest.approx(reference.metrics["final_equity"])

    # fresh process, same checkpoint
    cfg2 = clone(config)
    cfg2.run_id = "ckpt_run2"
    runner2 = build(cfg2, bars, "/tmp/qr_ckpt_restore")
    runner2.checkpoints.restore(runner2.engine, ckpt)
    runner2.engine.run()
    restored = runner2.finalize()

    assert restored.metrics["final_equity"] == pytest.approx(reference.metrics["final_equity"])
    assert restored.trades["price"].tolist() == pytest.approx(reference.trades["price"].tolist())
    assert restored.trades["quantity"].tolist() == pytest.approx(reference.trades["quantity"].tolist())


def test_seek_replays_from_checkpoint_and_matches_reference(bars, config):
    cfg = clone(config)
    cfg.checkpoint_every = 300
    cfg.run_id = "seek_run"
    runner = build(cfg, bars, "/tmp/qr_seek")
    runner.run()
    ref_curve = {row["ts"]: row["equity"] for row in runner.account.equity_curve}

    session = ReplaySession(runner.engine, runner.checkpoints, "seek_run")
    target = runner.source.groups[int(len(runner.source.groups) * 0.6)][0] + timedelta(minutes=1)
    status = session.seek(target)
    assert status["clock"] is not None
    assert runner.engine.clock >= target
    assert runner.account.equity == pytest.approx(ref_curve[runner.engine.clock])

    earlier = runner.source.groups[int(len(runner.source.groups) * 0.2)][0] + timedelta(minutes=1)
    status = session.seek(earlier)
    assert runner.engine.clock >= earlier
    assert runner.account.equity == pytest.approx(ref_curve[runner.engine.clock])
    assert len(runner.checkpoints.list()) >= 1


def test_seek_without_checkpoints_replays_from_start(bars, config):
    cfg = clone(config)
    cfg.checkpoint_every = 0
    cfg.run_id = "seek_plain"
    runner = build(cfg, bars, "/tmp/qr_seek_plain")
    runner.run()
    ref_curve = {row["ts"]: row["equity"] for row in runner.account.equity_curve}
    session = ReplaySession(runner.engine, runner.checkpoints, "seek_plain")
    target = runner.source.groups[40][0] + timedelta(minutes=1)
    session.seek(target)
    assert runner.engine.clock >= target
    assert runner.account.equity == pytest.approx(ref_curve[runner.engine.clock])


def test_engine_reset_returns_to_initial_state(bars):
    engine = ReplayEngine(MemoryBarSource.from_bars(bars), frequency="1m")
    engine.run()
    assert engine.finished
    engine.reset()
    assert engine.clock is None
    assert engine.processed_events == 0
    engine.run()
    assert engine.processed_events > 0
