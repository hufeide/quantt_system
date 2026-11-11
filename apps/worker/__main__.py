"""Start a Celery worker: python -m apps.worker --queues backtest,factor

The process stays in the foreground (systemd / K3s manage its lifecycle); this
module only translates CLI flags into worker options.

Logging: every line is single-line JSON on stdout with the core correlation
fields (run_id / task_id / strategy_id / event_type) so Loki can parse it.
"""
from __future__ import annotations

import argparse
import os
from typing import Sequence

from apps.worker.celery_app import create_celery, default_queues, env_flag
from apps.worker.metrics import maybe_start_metrics_server
from core.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.worker",
        description="Start a Celery worker for the quant replay system.",
    )
    parser.add_argument(
        "-Q",
        "--queues",
        default=default_queues(),
        help="Comma separated queues to consume (default: QUANT_WORKER_QUEUES or all).",
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=None,
        help="Worker concurrency (default: CELERY_CONCURRENCY or 2).",
    )
    parser.add_argument(
        "-l",
        "--loglevel",
        default=os.getenv("QUANT_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO",
        help="Log level (default: QUANT_LOG_LEVEL / LOG_LEVEL or INFO).",
    )
    parser.add_argument(
        "-P",
        "--pool",
        default=None,
        choices=["solo", "prefork", "threads", "gevent", "eventlet"],
        help="Execution pool (default: Celery default for the platform).",
    )
    parser.add_argument("--hostname", default=None, help="Optional worker hostname.")
    parser.add_argument(
        "--broker",
        default=None,
        help="Override the broker url for this process only.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.broker:
        os.environ["CELERY_BROKER_URL"] = args.broker
    if args.concurrency is not None:
        os.environ["CELERY_CONCURRENCY"] = str(args.concurrency)

    configure_logging(level=args.loglevel, json_output=env_flag("QUANT_LOG_JSON", True))
    app = create_celery()
    maybe_start_metrics_server()

    worker_argv = ["worker", f"--loglevel={args.loglevel}", "--queues", args.queues]
    if args.concurrency is not None:
        worker_argv += ["--concurrency", str(args.concurrency)]
    if args.pool:
        worker_argv += ["--pool", args.pool]
    if args.hostname:
        worker_argv += ["--hostname", args.hostname]
    app.worker_main(worker_argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
