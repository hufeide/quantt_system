#!/usr/bin/env python
"""Run a backtest from a YAML/JSON config file.

    python scripts/run_backtest.py --config configs/backtest/default.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.backtest.runner import BacktestRunner  # noqa: E402
from core.config import BacktestConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="run a backtest")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    config = BacktestConfig.load(args.config)
    if args.run_id:
        config.run_id = args.run_id
    runner = BacktestRunner(config, output_root=args.output, run_id=args.run_id)
    result = runner.run()
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
