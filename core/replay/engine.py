"""Deterministic replay engine: simulated clock, event queue, controls."""
from __future__ import annotations

import heapq
import time as wallclock
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable

from core.config import FREQUENCY_SECONDS
from core.event.model import BarReceived, Event, EventType, SessionEvent
from core.logging import get_logger, set_context
from core.market.models import Bar
from core.replay.feed import BarSource

logger = get_logger(__name__)

Handler = Callable[[Event], None]


class EngineState(str):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"


class ReplayEngine:
    """Drives simulated time forward in a fully deterministic order.

    Ordering rule: events are processed by (simulated_time, priority, seq).
    Market data has priority 0, strategy/order events 5-7, session markers -1.
    Bars are only visible at their close time (bar.ts + interval), which is what
    makes look-ahead bias impossible.
    """

    def __init__(
        self,
        source: BarSource,
        frequency: str = "1m",
        state_provider: Any | None = None,
        speed: float = 0.0,
    ) -> None:
        self.source = source
        self.frequency = frequency
        self.bar_seconds = FREQUENCY_SECONDS[frequency]
        self.state_provider = state_provider
        self.state: str = EngineState.IDLE
        self.speed = speed  # 0 => as fast as possible
        self.clock: datetime | None = None
        self.seq = 0
        self.processed_events = 0
        self.processed_bars = 0
        self._heap: list[tuple[datetime, int, int, Event]] = []
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._global_handlers: list[Handler] = []
        self._time_end_handlers: list[Callable[[datetime], None]] = []
        self._last_day = None
        self._wall_start: float | None = None
        self._sim_start: datetime | None = None
        self._started_at: float | None = None
        self._finished = False

    # ------------------------------------------------------------------ wiring
    def on(self, event_type: str | EventType, handler: Handler) -> None:
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        self._handlers[key].append(handler)

    def on_event(self, handler: Handler) -> None:
        self._global_handlers.append(handler)

    def on_time_end(self, handler: Callable[[datetime], None]) -> None:
        """Called once all events at a given simulated timestamp are processed."""
        self._time_end_handlers.append(handler)

    # ------------------------------------------------------------------ queue
    def _push(self, event: Event) -> None:
        event.seq = self.seq
        self.seq += 1
        heapq.heappush(self._heap, (event.time, event.priority, event.seq, event))

    def pending_events(self) -> int:
        return len(self._heap)

    def emit(self, event: Event) -> None:
        """Queue a derived event at the current simulated time."""
        if event.time is None:
            event.time = self.clock
        self._push(event)

    def _fill_heap(self) -> None:
        """Pull the next bar group from the source when it is due."""
        while True:
            nxt = None
            if hasattr(self.source, "peek_group"):
                nxt = self.source.peek_group()  # type: ignore[attr-defined]
            else:
                nxt = self.source.next_group()
                if nxt is not None:
                    self._push_group(nxt)
                return
            if nxt is None:
                return
            ts, bars = nxt
            close_time = ts + timedelta(seconds=self.bar_seconds)
            if self._heap and close_time > self._heap[0][0]:
                return
            self.source.next_group()
            self._push_group((ts, bars))

    def _push_group(self, group: tuple[datetime, list[Bar]]) -> None:
        ts, bars = group
        close_time = ts + timedelta(seconds=self.bar_seconds)
        for bar in bars:
            self._push(BarReceived(time=close_time, bar=bar))

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self.state == EngineState.FINISHED:
            raise RuntimeError("replay already finished; reset() first")
        self.state = EngineState.RUNNING
        self._started_at = wallclock.time()
        if self._wall_start is None:
            self._wall_start = wallclock.time()
            self._sim_start = self.clock

    def pause(self) -> None:
        if self.state == EngineState.RUNNING:
            self.state = EngineState.PAUSED

    def resume(self) -> None:
        if self.state in (EngineState.PAUSED, EngineState.IDLE):
            self.state = EngineState.RUNNING
            if self._wall_start is None:
                self._wall_start = wallclock.time()
                self._sim_start = self.clock

    def set_speed(self, speed: float) -> None:
        if speed < 0:
            raise ValueError("speed must be >= 0 (0 = unlimited)")
        self.speed = float(speed)
        self._wall_start = wallclock.time()
        self._sim_start = self.clock

    def reset(self) -> None:
        self.source.reset()
        self._heap.clear()
        self.clock = None
        self.seq = 0
        self.processed_events = 0
        self.processed_bars = 0
        self.state = EngineState.IDLE
        self._last_day = None
        self._finished = False
        self._wall_start = None
        self._sim_start = None
        if self.state_provider is not None and hasattr(self.state_provider, "reset_state"):
            self.state_provider.reset_state()

    def finish(self) -> None:
        self.state = EngineState.FINISHED
        self._finished = True

    # ------------------------------------------------------------------ stepping
    def step(self, count: int = 1) -> int:
        """Process at most count events. Returns the number actually processed."""
        if self.state == EngineState.FINISHED:
            return 0
        if self.state == EngineState.IDLE:
            self.state = EngineState.PAUSED
        done = 0
        for _ in range(max(0, count)):
            if not self._step_once():
                break
            done += 1
        return done

    def step_bar(self) -> int:
        """Advance until one full bar timestamp has been processed."""
        if self.state == EngineState.FINISHED:
            return 0
        if self.state == EngineState.IDLE:
            self.state = EngineState.PAUSED
        done = 0
        start_clock = self.clock
        saw_new_time = False
        while True:
            before = self.clock
            if not self._step_once():
                break
            done += 1
            if self.clock != before:
                saw_new_time = True
            if saw_new_time and self._at_time_boundary():
                break
            if start_clock is None and saw_new_time and self._at_time_boundary():
                break
        return done

    def _at_time_boundary(self) -> bool:
        if not self._heap:
            return True
        return self.clock is not None and self._heap[0][0] > self.clock

    def run(self, max_events: int | None = None) -> int:
        """Run to completion (or until paused / max_events reached)."""
        self.start()
        processed = 0
        while self.state == EngineState.RUNNING:
            if not self._step_once():
                break
            processed += 1
            if max_events is not None and processed >= max_events:
                self.state = EngineState.PAUSED
                break
            self._pace()
        return processed

    def _pace(self) -> None:
        if not self.speed or self.clock is None or self._sim_start is None or self._wall_start is None:
            return
        sim_elapsed = (self.clock - self._sim_start).total_seconds()
        target = self._wall_start + sim_elapsed / self.speed
        delay = target - wallclock.time()
        if delay > 0:
            wallclock.sleep(min(delay, 0.25))

    def _step_once(self) -> bool:
        if not self._heap:
            self._fill_heap()
            if not self._heap:
                self.finish()
                self._emit_time_end()
                return False
        _, _, _, event = heapq.heappop(self._heap)
        self.clock = event.time
        self.processed_events += 1
        if isinstance(event, BarReceived):
            self.processed_bars += 1
        self._dispatch(event)
        if self._at_time_boundary():
            self._emit_time_end()
        return True

    def _dispatch(self, event: Event) -> None:
        set_context(event_sequence=event.seq, simulated_time=event.time.isoformat(), event_type=event.event_type)
        handlers = self._handlers.get(event.event_type, ())
        for handler in handlers:
            handler(event)
        for handler in self._global_handlers:
            handler(event)

    def _emit_time_end(self) -> None:
        if self.clock is None:
            return
        day = self.clock.date()
        if self._last_day is not None and day != self._last_day:
            close_evt = SessionEvent(
                time=self.clock, kind=EventType.SESSION_CLOSE, trading_day=self._last_day.isoformat()
            )
            close_evt.seq = self.seq
            self.seq += 1
            self._dispatch(close_evt)
            open_evt = SessionEvent(time=self.clock, kind=EventType.SESSION_OPEN, trading_day=day.isoformat())
            open_evt.seq = self.seq
            self.seq += 1
            self._dispatch(open_evt)
        elif self._last_day is None:
            open_evt = SessionEvent(time=self.clock, kind=EventType.SESSION_OPEN, trading_day=day.isoformat())
            open_evt.seq = self.seq
            self.seq += 1
            self._dispatch(open_evt)
        self._last_day = day
        for handler in self._time_end_handlers:
            handler(self.clock)

    # ------------------------------------------------------------------ snapshot
    def snapshot_pending(self) -> list[dict]:
        """Serialize queued (not yet processed) events for checkpointing."""
        from core.replay.checkpoint import engine_snapshot_pending

        return engine_snapshot_pending(self)

    def restore_state(self, clock, seq: int, cursor: int, pending: list[dict]) -> None:
        """Rebuild engine internals from a checkpoint (see CheckpointStore)."""
        from core.replay.checkpoint import engine_restore_state

        engine_restore_state(self, clock, seq, cursor, pending)

    # ------------------------------------------------------------------ status
    @property
    def finished(self) -> bool:
        return self._finished or self.state == EngineState.FINISHED

    def elapsed_wall(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(wallclock.time() - self._started_at, 1e-9)

    def status(self) -> dict[str, Any]:
        elapsed = self.elapsed_wall()
        return {
            "state": self.state,
            "clock": self.clock.isoformat() if self.clock else None,
            "seq": self.seq,
            "processed_events": self.processed_events,
            "processed_bars": self.processed_bars,
            "pending_events": len(self._heap),
            "cursor": self.source.cursor,
            "total_groups": len(self.source),
            "speed": self.speed,
            "bars_per_second": round(self.processed_bars / elapsed, 2) if elapsed > 0 else 0.0,
        }
