"""Celery application for the quant replay workers.

Everything is configured from environment variables so the same code can run
against RabbitMQ in production and in eager (in-process) mode inside tests:

    QUANT_CELERY_BROKER_URL    preferred broker url (K8s configmap)
    QUANT_RABBITMQ_URL         broker fallback
    CELERY_BROKER_URL          legacy override, highest priority
    QUANT_CELERY_RESULT_BACKEND / CELERY_RESULT_BACKEND   default rpc://
    QUANT_CELERY_TASK_ALWAYS_EAGER / CELERY_TASK_ALWAYS_EAGER  "1" => sync mode
    CELERY_CONCURRENCY         default 2
    QUANT_LOG_LEVEL            default INFO
    QUANT_LOG_JSON             default "1" (single-line JSON on stdout for Loki)

The task catalog is split across five queues so a single node can decide which
kind of work it wants to consume (backtest vs. factor vs. maintenance).
"""
from __future__ import annotations

import logging
import os
import sys

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger
from kombu import Queue

from core.logging import JsonFormatter

DEFAULT_BROKER_URL = "amqp://guest:guest@localhost:5672//"
DEFAULT_RESULT_BACKEND = "rpc://"
DEFAULT_CONCURRENCY = 2

REPLAY_QUEUE = "replay"
BACKTEST_QUEUE = "backtest"
FACTOR_QUEUE = "factor"
OPTIMIZATION_QUEUE = "optimization"
MAINTENANCE_QUEUE = "maintenance"

QUEUES: tuple[str, ...] = (
    REPLAY_QUEUE,
    BACKTEST_QUEUE,
    FACTOR_QUEUE,
    OPTIMIZATION_QUEUE,
    MAINTENANCE_QUEUE,
)

TASK_ROUTES: dict[str, dict[str, str]] = {
    "quant.run_backtest": {"queue": BACKTEST_QUEUE},
    "quant.compute_factors": {"queue": FACTOR_QUEUE},
    "quant.optimize": {"queue": OPTIMIZATION_QUEUE},
    "quant.maintenance": {"queue": MAINTENANCE_QUEUE},
}


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment flag ("1", "true", "yes", "on")."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to a default."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_first(*names: str, default: str = "") -> str:
    """Return the first non-empty environment variable among names."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def resolve_broker_url() -> str:
    return env_first(
        "CELERY_BROKER_URL",
        "QUANT_CELERY_BROKER_URL",
        "QUANT_RABBITMQ_URL",
        default=DEFAULT_BROKER_URL,
    )


def resolve_result_backend() -> str:
    return env_first(
        "CELERY_RESULT_BACKEND",
        "QUANT_CELERY_RESULT_BACKEND",
        "QUANT_REDIS_URL",
        default=DEFAULT_RESULT_BACKEND,
    )


def default_queues() -> str:
    """Queues the worker consumes by default (QUANT_WORKER_QUEUES overrides)."""
    return os.getenv("QUANT_WORKER_QUEUES") or ",".join(QUEUES)


def _apply_json_handler(logger: logging.Logger | None) -> None:
    if logger is None:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.propagate = False


@after_setup_logger.connect
def _setup_logger(logger=None, **_: object) -> None:  # pragma: no cover - worker only
    if env_flag("QUANT_LOG_JSON", True):
        _apply_json_handler(logger)


@after_setup_task_logger.connect
def _setup_task_logger(logger=None, **_: object) -> None:  # pragma: no cover - worker only
    if env_flag("QUANT_LOG_JSON", True):
        _apply_json_handler(logger)


def create_celery(name: str = "quant_worker") -> Celery:
    """Build a fresh Celery app (also used by the __main__ entry point)."""
    app = Celery(
        name,
        broker=resolve_broker_url(),
        backend=resolve_result_backend(),
        # Register the task catalog on start-up (python -m apps.worker and
        # "celery -A apps.worker.celery_app worker" both rely on this).
        include=["apps.worker.tasks"],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="Asia/Shanghai",
        enable_utc=True,
        task_default_queue=BACKTEST_QUEUE,
        task_queues=tuple(Queue(queue, routing_key=queue) for queue in QUEUES),
        task_routes=TASK_ROUTES,
        # Backtests are long and memory hungry: never let one worker hoard tasks.
        worker_prefetch_multiplier=1,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # Keep the root logger ours so every log line is JSON on stdout.
        worker_hijack_root_logger=False,
        worker_concurrency=env_int("CELERY_CONCURRENCY", DEFAULT_CONCURRENCY),
        task_always_eager=env_flag("CELERY_TASK_ALWAYS_EAGER") or env_flag("QUANT_CELERY_TASK_ALWAYS_EAGER"),
        task_eager_propagates=env_flag("CELERY_TASK_EAGER_PROPAGATES", True),
        task_track_started=True,
        task_time_limit=env_int("CELERY_TASK_TIME_LIMIT", 3600),
        result_expires=86_400,
        broker_connection_retry_on_startup=True,
    )
    return app


celery_app = create_celery()

__all__ = [
    "BACKTEST_QUEUE",
    "FACTOR_QUEUE",
    "MAINTENANCE_QUEUE",
    "OPTIMIZATION_QUEUE",
    "QUEUES",
    "REPLAY_QUEUE",
    "celery_app",
    "create_celery",
    "default_queues",
    "env_flag",
    "env_first",
    "env_int",
    "resolve_broker_url",
    "resolve_result_backend",
]
