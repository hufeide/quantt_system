#!/usr/bin/env python
"""Run the golden backtest and compare it with the recorded expectations.

Usage:
    python scripts/run_golden.py            # print the current snapshot
    python scripts/run_golden.py --check    # fail on any drift (used by CI)
    python scripts/run_golden.py --update   # re-record expectations
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.golden_backtest.golden import (  # noqa: E402
    compare,
    load_expected,
    run_golden,
    save_expected,
    snapshot,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="golden backtest")
    parser.add_argument("--check", action="store_true", help="fail if results drift")
    parser.add_argument("--update", action="store_true", help="rewrite expected_metrics.json")
    parser.add_argument("--root", default=str(ROOT / "data_files" / "golden"))
    parser.add_argument("--tolerance", type=float, default=1e-6)
    args = parser.parse_args()

    result = run_golden(args.root, run_id="golden")
    current = snapshot(result)
    print(json.dumps(current, indent=2, ensure_ascii=False))

    if args.update:
        path = save_expected(current)
        print(f"[golden] 已写入基线: {path}")
        return 0

    expected = load_expected()
    if expected is None:
        print("[golden] 没有基线文件，请先运行 --update", file=sys.stderr)
        return 2

    problems = compare(current, expected, tolerance=args.tolerance)
    if problems:
        print("[golden] 结果与基线不一致:", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        return 1
    print("[golden] 与基线一致 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
