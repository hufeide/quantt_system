"""Checkpoint / restore for deterministic replay and Seek support."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.event.model import BarReceived, Event
from core.market.models import Bar
from core.replay.engine import ReplayEngine

CHECKPOINT_VERSION = 1


@dataclass
class Checkpoint:
    checkpoint_id: str
    clock: str
    seq: int
    cursor: int
    pending: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    source_signature: str = ""
    version: int = CHECKPOINT_VERSION
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Checkpoint":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("version") != CHECKPOINT_VERSION:
            raise ValueError(f"unsupported checkpoint version: {data.get('version')}")
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def clock_dt(self) -> datetime:
        return datetime.fromisoformat(self.clock)


class CheckpointStore:
    """File-backed checkpoint store: <root>/<run_id>/checkpoints/*.json"""

    def __init__(self, root: str | Path, run_id: str, keep: int | None = None) -> None:
        self.root = Path(root) / f"run_id={run_id}" / "checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.keep = keep
        self._cache: list[Checkpoint] | None = None

    def path_for(self, checkpoint_id: str) -> Path:
        return self.root / f"{checkpoint_id}.json"

    def save(
        self,
        engine: ReplayEngine,
        label: str = "",
        source_signature: str = "",
        extra_state: dict[str, Any] | None = None,
    ) -> Checkpoint:
        if engine.clock is None:
            raise RuntimeError("cannot checkpoint before the replay clock has advanced")
        state = {}
        if engine.state_provider is not None and hasattr(engine.state_provider, "snapshot"):
            state = engine.state_provider.snapshot()
        if extra_state:
            state.update(extra_state)
        cid = f"ckpt_{engine.processed_events:010d}_{engine.clock.strftime('%Y%m%dT%H%M%S')}"
        ckpt = Checkpoint(
            checkpoint_id=cid,
            clock=engine.clock.isoformat(),
            seq=engine.seq,
            cursor=engine.source.cursor,
            pending=engine.snapshot_pending(),
            state=state,
            source_signature=source_signature,
            label=label,
        )
        ckpt.save(self.path_for(cid))
        self._cache = None
        self._prune()
        return ckpt

    def _prune(self) -> None:
        if not self.keep:
            return
        items = sorted(self.root.glob("ckpt_*.json"))
        for old in items[: max(0, len(items) - self.keep)]:
            old.unlink(missing_ok=True)
        self._cache = None

    def list(self) -> list[Checkpoint]:
        if self._cache is not None:
            return self._cache
        items: list[Checkpoint] = []
        for path in sorted(self.root.glob("ckpt_*.json")):
            try:
                items.append(Checkpoint.load(path))
            except Exception:
                continue
        items.sort(key=lambda c: (c.clock, c.seq))
        self._cache = items
        return items

    def latest(self) -> Checkpoint | None:
        items = self.list()
        return items[-1] if items else None

    def latest_at_or_before(self, when: datetime) -> Checkpoint | None:
        best: Checkpoint | None = None
        for ckpt in self.list():
            if ckpt.clock_dt <= when:
                best = ckpt
            else:
                break
        return best

    def load(self, checkpoint_id: str) -> Checkpoint:
        return Checkpoint.load(self.path_for(checkpoint_id))

    def restore(self, engine: ReplayEngine, checkpoint: Checkpoint) -> None:
        engine.restore_state(
            clock=checkpoint.clock_dt,
            seq=checkpoint.seq,
            cursor=checkpoint.cursor,
            pending=checkpoint.pending,
        )
        if engine.state_provider is not None and hasattr(engine.state_provider, "restore"):
            engine.state_provider.restore(checkpoint.state)


def restore_from_file(engine: ReplayEngine, path: str | Path) -> Checkpoint:
    ckpt = Checkpoint.load(path)
    CheckpointStore(Path(path).parent.parent, ckpt.checkpoint_id).restore(engine, ckpt)
    return ckpt


def engine_snapshot_pending(engine: ReplayEngine) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for time, priority, seq, event in sorted(engine._heap):  # noqa: SLF001 - intentional
        if not isinstance(event, BarReceived):
            raise RuntimeError(f"cannot checkpoint with pending non-bar event: {event.event_type}")
        out.append(
            {
                "time": time.isoformat(),
                "priority": priority,
                "seq": seq,
                "bar": event.bar.to_dict() if event.bar else None,
            }
        )
    return out


def engine_restore_state(
    engine: ReplayEngine,
    clock: datetime,
    seq: int,
    cursor: int,
    pending: list[dict[str, Any]],
) -> None:
    engine.source.seek(cursor)
    engine._heap.clear()  # noqa: SLF001 - intentional
    engine.clock = clock
    engine.seq = seq
    engine.processed_events = 0
    engine.processed_bars = 0
    engine.state = "paused"
    engine._finished = False  # noqa: SLF001
    engine._last_day = clock.date()  # noqa: SLF001
    for item in pending:
        bar = Bar.from_dict(item["bar"]) if item.get("bar") else None
        event: Event = BarReceived(time=datetime.fromisoformat(item["time"]), bar=bar)
        event.seq = item["seq"]
        engine._heap.append(  # noqa: SLF001
            (event.time, item["priority"], event.seq, event)
        )
    import heapq

    heapq.heapify(engine._heap)  # noqa: SLF001
