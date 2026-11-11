"""Interactive replay session: Start/Pause/Resume/Step/SetSpeed/Seek/Checkpoint/Restore."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.logging import get_logger
from core.replay.checkpoint import Checkpoint, CheckpointStore
from core.replay.engine import EngineState, ReplayEngine

logger = get_logger(__name__)


class ReplaySession:
    """Thin control layer over ReplayEngine used by the API and the web UI."""

    def __init__(self, engine: ReplayEngine, checkpoint_store: CheckpointStore, run_id: str) -> None:
        self.engine = engine
        self.checkpoints = checkpoint_store
        self.run_id = run_id
        self.events: list[dict[str, Any]] = []

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> dict[str, Any]:
        self.engine.start()
        return self.status()

    def pause(self) -> dict[str, Any]:
        self.engine.pause()
        return self.status()

    def resume(self) -> dict[str, Any]:
        self.engine.resume()
        return self.status()

    def step(self, events: int = 1) -> dict[str, Any]:
        self.engine.step(events)
        return self.status()

    def step_bar(self) -> dict[str, Any]:
        self.engine.step_bar()
        return self.status()

    def set_speed(self, speed: float) -> dict[str, Any]:
        self.engine.set_speed(speed)
        return self.status()

    def run(self, max_events: int | None = None) -> dict[str, Any]:
        self.engine.run(max_events=max_events)
        return self.status()

    # -- checkpointing -------------------------------------------------------
    def checkpoint(self, label: str = "") -> Checkpoint:
        return self.checkpoints.save(self.engine, label=label)

    def restore(self, checkpoint_id: str) -> dict[str, Any]:
        ckpt = self.checkpoints.load(checkpoint_id)
        self.checkpoints.restore(self.engine, ckpt)
        return self.status()

    def seek(self, target: datetime | str) -> dict[str, Any]:
        """Restore the newest checkpoint at/before target, then replay forward.

        This is the only correct way to jump in time: the engine never simply
        assigns the clock, because account/strategy state must be rebuilt by
        replaying the deterministic event stream.
        """
        if isinstance(target, str):
            target = datetime.fromisoformat(target)
        if self.engine.state == EngineState.RUNNING:
            self.engine.pause()

        ckpt = self.checkpoints.latest_at_or_before(target)
        if ckpt is not None:
            logger.info("seek: restoring checkpoint %s at %s", ckpt.checkpoint_id, ckpt.clock)
            self.checkpoints.restore(self.engine, ckpt)
        else:
            logger.info("seek: no checkpoint before %s, replaying from the beginning", target)
            self.engine.reset()

        self.engine.state = EngineState.RUNNING
        while self.engine.clock is None or self.engine.clock < target:
            if not self.engine._step_once():  # noqa: SLF001 - controlled access
                break
        self.engine.pause()
        return self.status()

    def status(self) -> dict[str, Any]:
        status = self.engine.status()
        status["run_id"] = self.run_id
        status["checkpoints"] = len(self.checkpoints.list())
        return status
