"""Command line interface for the data layer.

Usage:
    python -m data.cli build    --root data_files --version v001 ...
    python -m data.cli validate --root data_files --version v001 [--raw]
    python -m data.cli stats    --root data_files --version v001
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

import pandas as pd

from core.market.store import ParquetMarketDataStore
from data.normalization.normalize import read_raw
from data.pipeline import build_pipeline
from data.storage.duckdb_store import DuckDBStore
from data.validation.checks import validate_bars


def _parse_symbols(value: str | None) -> list[str]:
    if not value:
        return []
    return [s.strip().upper() for s in value.replace(" ", ",").split(",") if s.strip()]


def _print_report(report: dict) -> None:
    status = "通过" if report.get("ok") else "未通过"
    print(f"质量校验: {status}  (行数={report.get('rows', 0)}, 标的数={len(report.get('symbols', []))})")
    print(f"  区间: {report.get('start')} ~ {report.get('end')}")
    for issue in report.get("issues", []):
        print(f"  [{issue['severity']}] {issue['code']} x{issue['count']}: {issue['message']}")
        for example in issue.get("examples", [])[:2]:
            print(f"      例: {example}")
    if not report.get("issues"):
        print("  未发现数据质量问题")


def _cmd_build(args: argparse.Namespace) -> int:
    symbols = _parse_symbols(args.symbols)
    result = build_pipeline(
        root=args.root,
        version=args.version,
        symbols=symbols or ("600000.SH", "000001.SZ"),
        start=args.start,
        end=args.end,
        seed=args.seed,
        source=args.source,
        frequency=args.frequency,
        with_factors=args.with_factors,
        raise_on_error=False,
    )
    manifest = result.manifest
    print(f"数据集构建完成: 版本={manifest.dataset_version} 频率={manifest.frequency}")
    print(f"  行数={manifest.row_count} 标的={len(manifest.symbols)} 区间={manifest.start} ~ {manifest.end}")
    print(f"  Parquet 目录: {result.dataset_dir}")
    print(f"  清单文件: {result.build.manifest_path}")
    print(f"  质量报告: {result.build.report_path}")
    print(f"  checksum: {manifest.checksum}")
    print(f"  原始文件: {len(result.raw_files)} 个")
    if result.factor_summary:
        names = ", ".join(sorted(result.factor_summary.get("factors", {}))) or "无"
        print(f"  因子: {names}")
    _print_report(result.report)
    return 0 if result.ok else 1


def _cmd_validate(args: argparse.Namespace) -> int:
    if args.raw:
        frame = read_raw(
            args.root,
            source=args.source,
            frequency=args.frequency,
            symbols=_parse_symbols(args.symbols) or None,
            start=args.start,
            end=args.end,
        )
        origin = f"raw/source={args.source}/frequency={args.frequency}"
    else:
        store = ParquetMarketDataStore(args.root, args.version, args.frequency)
        if not store.exists():
            print(f"数据集不存在: {store.dataset_dir}")
            return 2
        frame = store.load_frame(symbols=_parse_symbols(args.symbols) or None, start=args.start, end=args.end)
        origin = str(store.dataset_dir)

    if frame.empty:
        print(f"没有可校验的数据: {origin}")
        return 2

    report = validate_bars(frame, frequency=args.frequency)
    print(f"校验数据: {origin}")
    _print_report(report)
    return 0 if report["ok"] else 1


def _cmd_stats(args: argparse.Namespace) -> int:
    store = ParquetMarketDataStore(args.root, args.version, args.frequency)
    if not store.exists():
        print(f"数据集不存在: {store.dataset_dir}")
        return 2

    with DuckDBStore(args.root, args.version, args.frequency) as db:
        stats = db.stats()
        print(f"数据集 {args.version} ({args.frequency})")
        print(f"  行数={stats['rows']} 标的={stats['symbols']} 重复bar={stats['duplicate_bars']}")
        print(f"  区间: {stats['start']} ~ {stats['end']}")
        print(f"  成交量合计={stats['volume']:,.0f} 成交额合计={stats['amount']:,.2f}")
        per_symbol = db.query(
            "SELECT symbol, count(*) AS bars, min(ts) AS start, max(ts) AS end "
            "FROM bars GROUP BY symbol ORDER BY symbol"
        )
        print("  按标的:")
        for row in per_symbol.itertuples(index=False):
            print(f"    {row.symbol}: {row.bars} 行, {pd.Timestamp(row.start)} ~ {pd.Timestamp(row.end)}")
        daily = db.daily_bars()
        print(f"  日线条数: {len(daily)}")
        run_ids = db.run_ids()
        if run_ids:
            print(f"  回测记录: {', '.join(run_ids)}")

    manifest = store.manifest()
    if manifest is not None:
        print(f"  checksum: {manifest.checksum}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m data.cli", description="数据层命令行工具")
    sub = parser.add_subparsers(dest="command", required=True)

    default_root = os.environ.get("QUANT_DATA_ROOT", "data_files")
    default_version = os.environ.get("QUANT_DATASET_VERSION", "v001")

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default=default_root, help=f"数据根目录 (默认 {default_root})")
        p.add_argument("--version", default=default_version, help=f"数据集版本 (默认 {default_version})")
        p.add_argument("--frequency", default="1m", help="数据频率 (默认 1m)")
        p.add_argument("--symbols", default="", help="逗号分隔的标的列表")
        p.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD")
        p.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")

    build_p = sub.add_parser("build", help="生成原始数据并构建标准化数据集")
    add_common(build_p)
    build_p.add_argument("--seed", type=int, default=42, help="随机种子")
    build_p.add_argument("--source", default="synthetic", help="数据源 (默认 synthetic)")
    build_p.add_argument("--with-factors", action="store_true", help="同时计算因子")
    build_p.set_defaults(func=_cmd_build)

    validate_p = sub.add_parser("validate", help="校验数据质量")
    add_common(validate_p)
    validate_p.add_argument("--source", default="synthetic", help="raw 校验时使用的数据源")
    validate_p.add_argument("--raw", action="store_true", help="校验 raw CSV 而不是 normalized Parquet")
    validate_p.set_defaults(func=_cmd_validate)

    stats_p = sub.add_parser("stats", help="打印数据集统计")
    add_common(stats_p)
    stats_p.set_defaults(func=_cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for key in ("start", "end"):
        value = getattr(args, key, None)
        if isinstance(value, str):
            setattr(args, key, date.fromisoformat(value))
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
