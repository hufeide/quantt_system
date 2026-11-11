"""Metadata store for tasks and backtest runs.

The store prefers MongoDB but must never make the API unusable when Mongo is not
available. Connection attempts are lazy (never at import time) and fall back to an
in-process dictionary that is mirrored to JSON files on disk.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging import get_logger

logger = get_logger(__name__)

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
KNOWN_STATUSES = frozenset({"pending", "running", "succeeded", "failed", "cancelled"})


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class MetadataStore:
    """Thread-safe task/run metadata store with Mongo or JSON persistence."""

    def __init__(
        self,
        mongo_url: str = "",
        mongo_db: str = "quant_replay",
        meta_dir: str | Path = "data_files/backtests/_meta",
        timeout_ms: int = 500,
    ) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._mongo_url = mongo_url
        self._mongo_db_name = mongo_db
        self._timeout_ms = max(int(timeout_ms), 1)
        self._meta_dir = Path(meta_dir)
        self._db = None
        self._checked = False
        self.backend = "pending"
        self._load_json()

    # ------------------------------------------------------------------ backend
    @property
    def mongo_connected(self) -> bool:
        return self._db is not None

    def _ensure_backend(self) -> None:
        if self._checked:
            return
        with self._lock:
            if self._checked:
                return
            self._checked = True
            if not self._mongo_url:
                self.backend = "json"
                return
            try:
                from pymongo import MongoClient

                client = MongoClient(
                    self._mongo_url,
                    serverSelectionTimeoutMS=self._timeout_ms,
                    connectTimeoutMS=self._timeout_ms,
                    socketTimeoutMS=self._timeout_ms * 4,
                    connect=False,
                )
                client.admin.command("ping")
                self._db = client[self._mongo_db_name]
                self.backend = "mongo"
                logger.info("metadata store connected to MongoDB (%s)", self._mongo_db_name)
                self._load_from_mongo()
            except Exception as exc:  # pymongo errors, DNS failures, timeouts
                logger.warning("MongoDB unavailable (%s); using JSON metadata store", exc)
                self._db = None
                self.backend = "json"

    def _load_from_mongo(self) -> None:
        assert self._db is not None
        try:
            for collection, target in (("tasks", self._tasks), ("runs", self._runs)):
                for doc in self._db[collection].find({}):
                    doc.pop("_id", None)
                    key = doc.get("task_id") if collection == "tasks" else doc.get("run_id")
                    if key and key not in target:
                        target[key] = doc
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("failed to preload metadata from MongoDB: %s", exc)

    def _persist(self, collection: str, doc: dict[str, Any]) -> None:
        if self._db is not None:
            key = "task_id" if collection == "tasks" else "run_id"
            try:
                self._db[collection].replace_one({"_id": doc[key]}, {"_id": doc[key], **doc}, upsert=True)
                return
            except Exception as exc:  # pragma: no cover - fall back to JSON
                logger.warning("MongoDB write failed (%s); persisting to JSON instead", exc)
                self._db = None
                self.backend = "json"
        self._write_json()

    # ------------------------------------------------------------------ JSON files
    @property
    def _tasks_path(self) -> Path:
        return self._meta_dir / "tasks.json"

    @property
    def _runs_path(self) -> Path:
        return self._meta_dir / "runs.json"

    def _load_json(self) -> None:
        for path, target in ((self._tasks_path, self._tasks), (self._runs_path, self._runs)):
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    target.update({str(k): v for k, v in payload.items() if isinstance(v, dict)})
            except Exception as exc:  # pragma: no cover - corrupt file
                logger.warning("failed to read metadata file %s: %s", path, exc)

    def _write_json(self) -> None:
        try:
            self._meta_dir.mkdir(parents=True, exist_ok=True)
            for path, payload in ((self._tasks_path, self._tasks), (self._runs_path, self._runs)):
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                tmp.replace(path)
        except Exception as exc:  # pragma: no cover - disk issues must not break the API
            logger.warning("failed to persist metadata to JSON: %s", exc)

    # ------------------------------------------------------------------ tasks
    def create_task(self, doc: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_backend()
            record = dict(doc)
            record.setdefault("created_at", utcnow())
            self._tasks[record["task_id"]] = record
            self._persist("tasks", record)
            return dict(record)

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_backend()
            record = self._tasks.get(task_id)
            if record is None:
                return None
            record.update(fields)
            self._persist("tasks", record)
            return dict(record)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_backend()
            record = self._tasks.get(task_id)
            return dict(record) if record else None

    def list_tasks(
        self, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            self._ensure_backend()
            items = list(self._tasks.values())
            if status:
                items = [d for d in items if d.get("status") == status]
            items.sort(key=lambda d: d.get("created_at") or "", reverse=True)
            return [dict(d) for d in items[offset : offset + limit]], len(items)

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            self._ensure_backend()
            removed = self._tasks.pop(task_id, None)
            if removed is None:
                return False
            if self._db is not None:
                try:
                    self._db["tasks"].delete_one({"_id": task_id})
                except Exception:  # pragma: no cover
                    pass
            self._write_json()
            return True

    # ------------------------------------------------------------------ runs
    def create_run(self, doc: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_backend()
            record = dict(doc)
            record.setdefault("created_at", utcnow())
            self._runs[record["run_id"]] = record
            self._persist("runs", record)
            return dict(record)

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_backend()
            record = self._runs.get(run_id)
            if record is None:
                return None
            record.update(fields)
            self._persist("runs", record)
            return dict(record)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_backend()
            record = self._runs.get(run_id)
            return dict(record) if record else None

    def list_runs(
        self, status: str | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            self._ensure_backend()
            items = list(self._runs.values())
            if status:
                items = [d for d in items if d.get("status") == status]
            items.sort(key=lambda d: d.get("created_at") or "", reverse=True)
            return [dict(d) for d in items[offset : offset + limit]], len(items)

    def delete_run(self, run_id: str) -> bool:
        with self._lock:
            self._ensure_backend()
            removed = self._runs.pop(run_id, None)
            if removed is None:
                return False
            task_id = removed.get("task_id")
            if task_id:
                self._tasks.pop(task_id, None)
                if self._db is not None:
                    try:
                        self._db["tasks"].delete_one({"_id": task_id})
                    except Exception:  # pragma: no cover
                        pass
            if self._db is not None:
                try:
                    self._db["runs"].delete_one({"_id": run_id})
                except Exception:  # pragma: no cover
                    pass
            self._write_json()
            return True
