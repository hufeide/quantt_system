"""Backtest runner: wires the engines together and produces reproducible artifacts."""
from __future__ import annotations

import hashlib
import time as wallclock
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from core.account.engine import AccountEngine
from core.backtest.artifacts import ArtifactWriter
from core.backtest.metrics import compute_metrics, metrics_fingerprint
from core.config import BacktestConfig
from core.event.model import Event, EventType, OrderFilled, OrderRejected
from core.event.store import EventStore
from core.logging import get_logger, log_context
from core.market.history import PanelHistory
from core.market.models import Bar
from core.market.store import ParquetMarketDataStore
from core.matching.engine import MatchingEngine
from core.matching.models import OrderStatus
from core.replay.checkpoint import CheckpointStore
from core.replay.engine import ReplayEngine
from core.replay.feed import BarSource, ParquetBarSource
from core.risk.engine import RiskEngine
from core.storage.manifest import RunManifest
from core.strategy import Strategy, StrategyContext, get_strategy

logger = get_logger(__name__)


@dataclass
class BacktestResult:
    run_id: str
    metrics: dict[str, Any]
    manifest: RunManifest
    artifacts: dict[str, str]
    equity: pd.DataFrame
    trades: pd.DataFrame
    orders: pd.DataFrame
    positions: pd.DataFrame
    events_recorded: int
    elapsed_seconds: float
    fingerprint: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "metrics": self.metrics,
            "manifest": self.manifest.to_dict(),
            "artifacts": self.artifacts,
            "events_recorded": self.events_recorded,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "fingerprint": self.fingerprint,
            "errors": self.errors,
        }


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]


class BacktestRunner:
    """Single-process, deterministic backtest orchestration."""

    def __init__(
        self,
        config: BacktestConfig,
        source: BarSource | None = None,
        output_root: str | Path | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
        run_id: str | None = None,
        keep_checkpoints: int | None = None,
    ) -> None:
        self.config = config
        self.run_id = run_id or config.run_id or new_run_id()
        self.config.run_id = self.run_id
        self.source = source
        self.output_root = Path(output_root or config.output_root)
        self.progress = progress
        self.keep_checkpoints = keep_checkpoints
        self.writer = ArtifactWriter(self.output_root, self.run_id)
        self.checkpoints = CheckpointStore(self.output_root, self.run_id, keep=keep_checkpoints)

        self.engine: ReplayEngine | None = None
        self.account: AccountEngine | None = None
        self.matching: MatchingEngine | None = None
        self.risk: RiskEngine | None = None
        self.history: PanelHistory | None = None
        self.strategy: Strategy | None = None
        self.ctx: StrategyContext | None = None

        self._event_log: list[dict[str, Any]] = []
        self.event_store: EventStore | None = (
            EventStore(self.writer.root / "events.jsonl", run_id=self.run_id) if config.record_events else None
        )
        self._last_checkpoint_events = 0
        self._last_position_day = None
        self._last_progress = 0.0
        self._errors: list[str] = []
        self._history_maxlen = max(int(config.extra.get("history_maxlen", 2000)), config.warmup_bars * 2, 100)
        self._max_events_recorded = int(config.extra.get("max_events_recorded", 2_000_000))
        self._dataset_checksum = ""
        self._build_done = False

    # ------------------------------------------------------------------ build
    def build(self) -> "BacktestRunner":
        cfg = self.config
        if self.source is None:
            store = ParquetMarketDataStore(cfg.data_root, cfg.dataset_version, cfg.frequency)
            dataset_manifest = store.manifest()
            if dataset_manifest is not None:
                self._dataset_checksum = dataset_manifest.checksum
            self.source = ParquetBarSource(
                root=cfg.data_root,
                version=cfg.dataset_version,
                frequency=cfg.frequency,
                symbols=cfg.symbols or None,
                start=cfg.start,
                end=cfg.end,
            )
        symbols = cfg.symbols or self._symbols_from_source()
        if not symbols:
            raise ValueError("no symbols resolved from dataset")
        cfg.symbols = symbols

        self.account = AccountEngine(cfg.initial_cash, t_plus_1=cfg.matching.t_plus_1)
        self.matching = MatchingEngine(cfg.matching, cfg.fees)
        self.risk = RiskEngine(cfg.risk, allow_short=cfg.matching.allow_short)
        self.history = PanelHistory(symbols, maxlen=self._history_maxlen)
        self.strategy = get_strategy(cfg.strategy, cfg.strategy_params)
        self.engine = ReplayEngine(
            source=self.source,
            frequency=cfg.frequency,
            state_provider=self,
            speed=0.0,
        )
        self.ctx = StrategyContext(
            engine=self.engine,
            history=self.history,
            account=self.account,
            matching=self.matching,
            risk=self.risk,
            strategy_id=f"{cfg.strategy}:{self.run_id}",
            config=cfg,
        )
        self._wire()
        self._build_done = True
        return self

    def _symbols_from_source(self) -> list[str]:
        symbols: set[str] = set()
        src = self.source
        groups = getattr(src, "groups", None)
        if groups is not None:
            for _, bars in groups:
                for bar in bars:
                    symbols.add(bar.symbol)
        return sorted(symbols)

    def _wire(self) -> None:
        assert self.engine is not None
        self.engine.on(EventType.BAR_RECEIVED, self._on_bar)
        self.engine.on(EventType.ORDER_FILLED, self._on_fill)
        self.engine.on(EventType.RISK_REJECTED, self._on_risk_rejected)
        self.engine.on(EventType.ORDER_REJECTED, self._on_order_rejected)
        self.engine.on(EventType.SESSION_OPEN, self._on_session_open)
        self.engine.on_time_end(self._on_time_end)
        if self.config.record_events:
            self.engine.on_event(self._record_event)

    # ------------------------------------------------------------------ handlers
    def _on_bar(self, event: Event) -> None:
        bar: Bar = event.bar  # type: ignore[attr-defined]
        assert self.history and self.account and self.matching and self.strategy and self.ctx
        self.history.append(bar)
        self.account.mark(bar.symbol, bar.close)
        for trade in self.matching.on_bar(bar):
            if trade.side == "sell":
                held = self.account.get_quantity(trade.symbol)
                if trade.quantity > held + 1e-9:
                    # A broker would never fill more than the account holds.
                    if held <= 1e-9:
                        order = self.matching.orders.get(trade.order_id)
                        if order is not None:
                            order.status = OrderStatus.CANCELLED.value
                            order.reject_reason = "no_position"
                        continue
                    trade.quantity = held
            try:
                self.account.apply_trade(trade)
            except ValueError as exc:
                msg = f"trade rejected by account at {event.time}: {exc}"
                logger.error(msg)
                self._errors.append(msg)
                order = self.matching.orders.get(trade.order_id)
                if order is not None:
                    order.status = OrderStatus.REJECTED.value
                    order.reject_reason = str(exc)
                self.engine.emit(
                    OrderRejected(
                        time=event.time,
                        order_id=trade.order_id,
                        symbol=trade.symbol,
                        reason=str(exc),
                        source="account",
                    )
                )
                continue
            self.engine.emit(
                OrderFilled(
                    time=event.time,
                    order_id=trade.order_id,
                    trade_id=trade.trade_id,
                    symbol=trade.symbol,
                    side=trade.side,
                    quantity=trade.quantity,
                    price=trade.price,
                    commission=trade.commission,
                    slippage_cost=trade.slippage_cost,
                )
            )
        try:
            self.strategy.on_bar(self.ctx, bar)
        except Exception as exc:  # keep the run alive, but surface the error
            msg = f"strategy error at {event.time}: {exc}"
            logger.exception(msg)
            self._errors.append(msg)

    def _on_fill(self, event: Event) -> None:
        assert self.strategy and self.ctx
        try:
            self.strategy.on_order_filled(self.ctx, event)
        except Exception as exc:
            self._errors.append(f"on_order_filled error: {exc}")
            logger.exception("on_order_filled failed")

    def _on_risk_rejected(self, event: Event) -> None:
        assert self.strategy and self.ctx
        try:
            self.strategy.on_order_rejected(self.ctx, event)
        except Exception:
            logger.exception("on_order_rejected failed")

    def _on_order_rejected(self, event: Event) -> None:
        self._on_risk_rejected(event)

    def _on_session_open(self, event: Event) -> None:
        assert self.account and self.risk
        day = getattr(event, "trading_day", "") or event.time.date().isoformat()
        self.account.on_new_trading_day(day)
        self.risk.on_new_trading_day(day)

    def _on_time_end(self, ts: datetime) -> None:
        assert self.account and self.engine
        self.account.record(ts)
        if self._last_position_day != ts.date():
            self.account.record_positions(ts)
            self._last_position_day = ts.date()
        if self.config.checkpoint_every and (
            self.engine.processed_events - self._last_checkpoint_events >= self.config.checkpoint_every
        ):
            self._last_checkpoint_events = self.engine.processed_events
            self.checkpoints.save(self.engine, label="auto", source_signature=self._source_signature())
        if self.progress is not None:
            now = wallclock.time()
            if now - self._last_progress > 0.5:
                self._last_progress = now
                self.progress(self.engine.status())

    def _record_event(self, event: Event) -> None:
        if len(self._event_log) >= self._max_events_recording:
            return
        record = event.to_record()
        self._event_log.append(record)
        if self.event_store is not None:
            self.event_store.append(record)

    @property
    def _max_events_recording(self) -> int:
        return self._max_events_recorded

    def _source_signature(self) -> str:
        groups = getattr(self.source, "groups", None)
        if not groups:
            return ""
        first = groups[0][0].isoformat()
        last = groups[-1][0].isoformat()
        return hashlib.sha256(f"{len(groups)}|{first}|{last}".encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ run
    def run(self) -> BacktestResult:
        if not self._build_done:
            self.build()
        assert self.engine and self.strategy and self.ctx
        started = wallclock.perf_counter()
        with log_context(run_id=self.run_id, strategy_id=self.config.strategy):
            self.strategy.on_start(self.ctx)
            self.engine.run()
        return self.finalize(wallclock.perf_counter() - started)

    def run_until(self, target: datetime, max_events: int | None = None) -> dict[str, Any]:
        """Advance the replay to a simulated time (used by interactive replay)."""
        assert self.engine
        if not self._build_done:
            self.build()
        assert self.strategy and self.ctx
        if self.engine.clock is None:
            self.strategy.on_start(self.ctx)
        self.engine.state = "running"
        processed = 0
        while self.engine.clock is None or self.engine.clock < target:
            if not self.engine._step_once():  # noqa: SLF001
                break
            processed += 1
            if max_events is not None and processed >= max_events:
                break
        self.engine.pause()
        return self.engine.status()

    def finalize(self, elapsed: float | None = None) -> BacktestResult:
        assert self.engine and self.account and self.matching and self.strategy and self.ctx
        for order in self.matching.cancel_all(reason="end_of_run"):
            logger.debug("cancelled open order %s", order.order_id)
        try:
            self.strategy.on_finish(self.ctx)
        except Exception as exc:
            self._errors.append(f"on_finish error: {exc}")
        if self.engine.clock is not None:
            self.account.record(self.engine.clock)
            self.account.record_positions(self.engine.clock)

        equity_df = pd.DataFrame(self.account.equity_curve)
        trades_df = pd.DataFrame([t.to_dict() for t in self.matching.trades])
        orders_df = pd.DataFrame([o.to_dict() for o in self.matching.orders.values()])
        positions_df = pd.DataFrame(self.account.position_history)

        metrics = compute_metrics(equity_df, trades_df, self.config.initial_cash, self.config.frequency)
        metrics["run_id"] = self.run_id
        metrics["strategy"] = self.config.strategy
        metrics["strategy_params"] = dict(self.config.strategy_params)
        metrics["symbols"] = list(self.config.symbols)
        metrics["dataset_version"] = self.config.dataset_version
        metrics["errors"] = list(self._errors)

        manifest = RunManifest.build(
            self.config,
            run_id=self.run_id,
            strategy_source=self._strategy_source(),
            dataset_checksum=self._dataset_checksum,
            factor_version=str(self.config.extra.get("factor_version", "")),
            calendar={
                "frequency": self.config.frequency,
                "sessions": "09:30-11:30,13:00-15:00",
                "timezone": "Asia/Shanghai",
                "adjustment": str(self.config.extra.get("adjustment", "none")),
            },
        )

        if self.event_store is not None:
            self.event_store.close()
        self.writer.write_manifest(manifest)
        if self.config.record_events:
            self.writer.write_events(self._event_log, self.run_id)
        self.writer.write_orders([o.to_dict() for o in self.matching.orders.values()])
        self.writer.write_trades([t.to_dict() for t in self.matching.trades])
        self.writer.write_positions(self.account.position_history)
        self.writer.write_equity(self.account.equity_curve)
        self.writer.write_metrics(metrics)
        self.writer.write_extra(
            "summary.json",
            {
                "run_id": self.run_id,
                "strategy": self.config.strategy,
                "metrics": metrics,
                "status": self.engine.status(),
                "errors": self._errors,
            },
        )
        fingerprint = metrics_fingerprint(metrics, trades_df)
        (self.writer.root / "fingerprint.txt").write_text(fingerprint, encoding="utf-8")

        return BacktestResult(
            run_id=self.run_id,
            metrics=metrics,
            manifest=manifest,
            artifacts=self.writer.list_files(),
            equity=equity_df,
            trades=trades_df,
            orders=orders_df,
            positions=positions_df,
            events_recorded=len(self._event_log),
            elapsed_seconds=elapsed or 0.0,
            fingerprint=fingerprint,
            errors=list(self._errors),
        )

    def _strategy_source(self) -> str:
        import inspect

        try:
            return inspect.getsource(type(self.strategy))
        except Exception:  # pragma: no cover
            return ""

    # ------------------------------------------------------------------ state
    def snapshot(self) -> dict[str, Any]:
        assert self.account and self.matching and self.risk and self.strategy
        return {
            "account": self.account.snapshot(),
            "matching": self.matching.snapshot(),
            "risk": self.risk.snapshot(),
            "strategy": self.strategy.snapshot(),
            "strategy_params": dict(self.strategy.params),
            "last_position_day": self._last_position_day.isoformat() if self._last_position_day else None,
            "events_recorded": len(self._event_log),
        }

    def restore(self, state: dict[str, Any]) -> None:
        assert self.account and self.matching and self.risk and self.strategy and self.engine
        self.account.restore(state.get("account") or {})
        self.matching.restore(state.get("matching") or {})
        self.risk.restore(state.get("risk") or {})
        self.strategy.restore(state.get("strategy") or {})
        if state.get("strategy_params"):
            self.strategy.params.update(state["strategy_params"])
        day = state.get("last_position_day")
        self._last_position_day = datetime.fromisoformat(day).date() if day else None
        self._event_log = self._event_log[: int(state.get("events_recorded", 0))]
        self._rebuild_history()

    def _rebuild_history(self) -> None:
        """Restore the lookback window after a checkpoint restore (no stale bars)."""
        assert self.engine and self.history
        groups = getattr(self.engine.source, "groups", None)
        if not groups:
            return
        cursor = self.engine.source.cursor
        start = max(0, cursor - self._history_maxlen)
        self.history.reset()
        for _, bars in groups[start:cursor]:
            for bar in bars:
                self.history.append(bar)

    def reset_state(self) -> None:
        assert self.account and self.matching and self.risk and self.strategy and self.history
        self.account.reset_state()
        self.matching.reset_state()
        self.risk.reset_state()
        self.strategy.reset_state()
        self.history.reset()
        self._event_log.clear()
        self._errors.clear()
        self._last_position_day = None
        self._last_checkpoint_events = 0


def run_backtest(
    config: BacktestConfig,
    source: BarSource | None = None,
    output_root: str | Path | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> BacktestResult:
    return BacktestRunner(config, source=source, output_root=output_root, progress=progress).run()
