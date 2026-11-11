"""In-memory registry of interactive replay sessions."""
from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from apps.api import metrics
from apps.api.settings import Settings
from core.backtest.runner import BacktestRunner
from core.config import BacktestConfig
from core.logging import get_logger
from core.replay.checkpoint import Checkpoint
from core.replay.session import ReplaySession

logger = get_logger(__name__)


class ReplaySessionManager:
    """Owns live replay sessions; they are process-local by design."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ creation
    def create(self, config: BacktestConfig) -> tuple[str, ReplaySession]:
        session_id = "replay_" + datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
        runner = BacktestRunner(config, output_root=self._settings.replay_root, run_id=session_id)
        runner.build()
        session = ReplaySession(runner.engine, runner.checkpoints, session_id)
        buffer = max(100, int(self._settings.replay_event_buffer))

        def _record(event) -> None:
            session.events.append(event.to_record())
            overflow = len(session.events) - buffer
            if overflow > 0:
                del session.events[:overflow]

        runner.engine.on_event(_record)
        with self._lock:
            self._sessions[session_id] = {
                "session": session,
                "runner": runner,
                "config": config,
                "created_at": datetime.now(UTC).isoformat(),
            }
            metrics.REPLAY_SESSIONS_ACTIVE.set(len(self._sessions))
        logger.info("replay session %s created for dataset %s", session_id, config.dataset_version)
        return session_id, session

    # ------------------------------------------------------------------ lookup
    def _entry(self, session_id: str) -> dict[str, Any]:
        entry = self._sessions.get(session_id)
        if entry is None:
            raise KeyError(session_id)
        return entry

    def _session(self, session_id: str) -> ReplaySession:
        return self._entry(session_id)["session"]

    def exists(self, session_id: str) -> bool:
        return session_id in self._sessions

    def status(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._session(session_id).status()

    # ------------------------------------------------------------------ controls
    def start(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._session(session_id).start()

    def pause(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._session(session_id).pause()

    def resume(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._session(session_id).resume()

    def step(self, session_id: str, events: int = 1) -> dict[str, Any]:
        with self._lock:
            return self._session(session_id).step(events)

    def set_speed(self, session_id: str, speed: float) -> dict[str, Any]:
        with self._lock:
            return self._session(session_id).set_speed(speed)

    def seek(self, session_id: str, target: datetime) -> dict[str, Any]:
        with self._lock:
            return self._session(session_id).seek(target)

    def checkpoint(self, session_id: str, label: str = "") -> Checkpoint:
        with self._lock:
            session = self._session(session_id)
            self._settle(session)
            return session.checkpoint(label=label)

    @staticmethod
    def _settle(session: ReplaySession) -> None:
        """Finish all events of the current simulated timestamp.

        A checkpoint must not contain pending non-bar events (the core rejects
        that), so an explicit checkpoint is always taken at a clean boundary.
        """
        engine = session.engine
        if engine.clock is None:
            return
        guard = 0
        while engine._heap and engine._heap[0][0] <= engine.clock:  # noqa: SLF001 - no public API
            if engine.step(1) == 0:
                break
            guard += 1
            if guard >= 1_000_000:  # pragma: no cover - runaway protection
                raise RuntimeError("无法在保存检查点前处理完当前时间片内的事件")

    def restore(self, session_id: str, checkpoint_id: str) -> dict[str, Any]:
        with self._lock:
            return self._session(session_id).restore(checkpoint_id)

    # ------------------------------------------------------------------ views
    def events(self, session_id: str, limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            session = self._session(session_id)
            total = len(session.events)
            return list(session.events[-limit:]), total

    def checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            entry = self._entry(session_id)
            return [ckpt.to_dict() for ckpt in entry["session"].checkpoints.list()]

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def close(self, session_id: str) -> bool:
        with self._lock:
            removed = self._sessions.pop(session_id, None)
            metrics.REPLAY_SESSIONS_ACTIVE.set(len(self._sessions))
            return removed is not None
