"""Structured logging with the mandatory correlation fields.

Every log record carries run_id / strategy_id / task_id / event_sequence /
simulated_time / event_type so Loki can correlate engine activity with runs.
"""
from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("quant_log_context", default=None)


def _current() -> dict[str, Any]:
    return _CONTEXT.get() or {}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_current())
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if json_output else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    current = dict(_current())
    current.update({k: v for k, v in fields.items() if v is not None})
    token = _CONTEXT.set(current)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def set_context(**fields: Any) -> None:
    current = dict(_current())
    current.update({k: v for k, v in fields.items() if v is not None})
    _CONTEXT.set(current)
