"""Data quality checks for minute bars.

Every check returns a list of structured issues:

    {"code": "DUPLICATE_BAR", "severity": "error", "message": "...",
     "count": 12, "examples": [...]}

validate_bars() aggregates them into a JSON-serializable report with an overall
ok flag, per-check status and descriptive statistics. Errors fail the report;
warnings (e.g. unusual volume) do not.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from core.market.calendar import TradingCalendar

ERROR = "error"
WARNING = "warning"

REQUIRED_COLUMNS = ("symbol", "ts", "open", "high", "low", "close", "volume")


class DataValidationError(ValueError):
    """Raised when a validation report contains blocking issues."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        codes = sorted({issue["code"] for issue in report.get("issues", [])})
        super().__init__(f"data validation failed: {', '.join(codes) or 'unknown'}")


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if math.isnan(float(value)) else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _examples(df: pd.DataFrame, mask: pd.Series, max_examples: int) -> list[dict[str, Any]]:
    if max_examples <= 0:
        return []
    cols = [c for c in ("symbol", "ts", "open", "high", "low", "close", "volume") if c in df.columns]
    subset = df.loc[mask, cols].head(max_examples)
    return [{k: _jsonable(v) for k, v in record.items()} for record in subset.to_dict("records")]


def _issue(
    code: str,
    severity: str,
    message: str,
    count: int,
    examples: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "count": int(count),
        "examples": list(examples or []),
    }


def check_timestamps(df: pd.DataFrame, max_examples: int = 5) -> list[dict[str, Any]]:
    ts = pd.to_datetime(df["ts"], errors="coerce")
    mask = ts.isna()
    count = int(mask.sum())
    if not count:
        return []
    return [_issue("TIMESTAMP_INVALID", ERROR, "rows without a parseable timestamp", count,
                   _examples(df, mask, max_examples))]


def check_duplicates(df: pd.DataFrame, max_examples: int = 5) -> list[dict[str, Any]]:
    mask = df.duplicated(subset=["symbol", "ts"], keep=False)
    count = int(mask.sum())
    if not count:
        return []
    return [_issue("DUPLICATE_BAR", ERROR, "duplicate (symbol, ts) bars", count,
                   _examples(df, mask, max_examples))]


def check_ohlc(df: pd.DataFrame, max_examples: int = 5) -> list[dict[str, Any]]:
    o = pd.to_numeric(df["open"], errors="coerce")
    h = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")
    mask = (
        o.isna() | h.isna() | low.isna() | c.isna()
        | (h < low)
        | (h < o) | (h < c)
        | (low > o) | (low > c)
    )
    count = int(mask.sum())
    if not count:
        return []
    return [_issue("OHLC_INVALID", ERROR, "high/low do not bracket open/close", count,
                   _examples(df, mask, max_examples))]


def check_prices(df: pd.DataFrame, max_examples: int = 5) -> list[dict[str, Any]]:
    prices = df[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    mask = (prices <= 0).any(axis=1) | prices.isna().any(axis=1)
    count = int(mask.sum())
    if not count:
        return []
    return [_issue("INVALID_PRICE", ERROR, "non-positive or missing price", count,
                   _examples(df, mask, max_examples))]


def check_volume(
    df: pd.DataFrame,
    max_examples: int = 5,
    outlier_ratio: float = 20.0,
) -> list[dict[str, Any]]:
    volume = pd.to_numeric(df["volume"], errors="coerce")
    issues: list[dict[str, Any]] = []

    invalid = volume.isna() | (volume < 0)
    if int(invalid.sum()):
        issues.append(_issue("VOLUME_INVALID", ERROR, "negative or missing volume", int(invalid.sum()),
                             _examples(df, invalid, max_examples)))

    valid = volume[~invalid]
    median = float(valid.median()) if len(valid) else 0.0
    threshold = median * outlier_ratio if median > 0 else np.inf
    anomaly = (~invalid) & ((volume == 0) | (volume > threshold))
    if int(anomaly.sum()):
        issues.append(
            _issue(
                "VOLUME_ANOMALY",
                WARNING,
                f"zero volume or volume above {outlier_ratio:g}x the median ({median:,.0f})",
                int(anomaly.sum()),
                _examples(df, anomaly, max_examples),
            )
        )
    return issues


def check_sessions(
    df: pd.DataFrame,
    calendar: TradingCalendar | None = None,
    max_examples: int = 5,
) -> list[dict[str, Any]]:
    calendar = calendar or TradingCalendar()
    ts = pd.to_datetime(df["ts"], errors="coerce")
    minutes = ts.dt.hour * 60 + ts.dt.minute
    in_session = ((minutes >= 9 * 60 + 30) & (minutes < 11 * 60 + 30)) | (
        (minutes >= 13 * 60) & (minutes < 15 * 60)
    )
    days = ts.dt.normalize().dt.date
    non_trading = {day for day in days.dropna().unique() if not calendar.is_trading_day(day)}
    off_day = days.isin(non_trading) if non_trading else pd.Series(False, index=df.index)
    mask = (~in_session) | off_day
    count = int(mask.sum())
    if not count:
        return []
    return [_issue("OFF_SESSION", ERROR, "bar outside trading sessions or on a non-trading day",
                   count, _examples(df, mask, max_examples))]


def check_missing_minutes(
    df: pd.DataFrame,
    calendar: TradingCalendar | None = None,
    frequency: str = "1m",
    max_examples: int = 5,
) -> list[dict[str, Any]]:
    if frequency != "1m":
        return []
    calendar = calendar or TradingCalendar()
    work = pd.DataFrame(
        {
            "symbol": df["symbol"].astype(str),
            "day": pd.to_datetime(df["ts"], errors="coerce").dt.date,
        }
    ).dropna()
    counts = work.groupby(["symbol", "day"], sort=True).size()

    missing = 0
    examples: list[dict[str, Any]] = []
    for (symbol, day), actual in counts.items():
        expected = calendar.expected_minutes(day)
        if expected and actual < expected:
            missing += expected - int(actual)
            if len(examples) < max_examples:
                examples.append(
                    {
                        "symbol": symbol,
                        "date": day.isoformat(),
                        "expected": expected,
                        "actual": int(actual),
                        "missing": expected - int(actual),
                    }
                )
    if not missing:
        return []
    return [_issue("MISSING_MINUTES", ERROR, "missing expected minute bars", missing, examples)]


def _frame_stats(df: pd.DataFrame, calendar: TradingCalendar) -> dict[str, Any]:
    ts = pd.to_datetime(df["ts"], errors="coerce")
    prices = df[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")
    amount = pd.to_numeric(df["amount"], errors="coerce") if "amount" in df.columns else pd.Series(dtype=float)
    symbols = sorted(df["symbol"].astype(str).unique().tolist()) if "symbol" in df.columns else []
    days = ts.dt.date.dropna().unique()
    return {
        "rows": int(len(df)),
        "symbols": symbols,
        "trading_days": int(len(days)),
        "start": ts.min().isoformat() if ts.notna().any() else None,
        "end": ts.max().isoformat() if ts.notna().any() else None,
        "bars_per_symbol": round(len(df) / len(symbols), 2) if symbols else 0.0,
        "price_min": float(prices.min().min()) if len(prices) else None,
        "price_max": float(prices.max().max()) if len(prices) else None,
        "volume_total": float(volume.sum()) if len(volume) else 0.0,
        "amount_total": float(amount.sum()) if len(amount) else 0.0,
        "duplicate_rows": int(df.duplicated(subset=["symbol", "ts"]).sum()),
        "trading_day_count_expected": len(calendar.trading_days(min(days), max(days))) if len(days) else 0,
    }


def validate_bars(
    df: pd.DataFrame,
    calendar: TradingCalendar | None = None,
    frequency: str = "1m",
    raise_on_error: bool = False,
    max_examples: int = 5,
    volume_outlier_ratio: float = 20.0,
) -> dict[str, Any]:
    """Run every check and return a structured, JSON-serializable report.

    The report contains ok, issues, per-check status and statistics. When
    raise_on_error is true and any error-severity issue is found, a
    DataValidationError carrying the report is raised.
    """
    calendar = calendar or TradingCalendar()
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        raise ValueError(f"cannot validate: missing columns {missing_cols}")

    work = df.copy()
    work["ts"] = pd.to_datetime(work["ts"], errors="coerce")

    if work.empty:
        report = {
            "ok": False,
            "frequency": frequency,
            "rows": 0,
            "symbols": [],
            "start": None,
            "end": None,
            "checks": {"empty": {"ok": False, "severity": ERROR, "count": 1,
                                 "message": "dataset contains no rows"}},
            "issues": [_issue("EMPTY_DATASET", ERROR, "dataset contains no rows", 1)],
            "stats": {"rows": 0, "symbols": [], "trading_days": 0},
        }
        if raise_on_error:
            raise DataValidationError(report)
        return report

    check_runs: list[tuple[str, list[dict[str, Any]]]] = [
        ("timestamps", check_timestamps(work, max_examples)),
        ("duplicates", check_duplicates(work, max_examples)),
        ("ohlc", check_ohlc(work, max_examples)),
        ("prices", check_prices(work, max_examples)),
        ("volume", check_volume(work, max_examples, volume_outlier_ratio)),
        ("sessions", check_sessions(work, calendar, max_examples)),
        ("missing_minutes", check_missing_minutes(work, calendar, frequency, max_examples)),
    ]

    issues: list[dict[str, Any]] = []
    checks: dict[str, dict[str, Any]] = {}
    for name, found in check_runs:
        issues.extend(found)
        if found:
            checks[name] = {
                "ok": False,
                "severity": ERROR if any(i["severity"] == ERROR for i in found) else WARNING,
                "count": int(sum(i["count"] for i in found)),
                "message": "; ".join(i["message"] for i in found),
            }
        else:
            skipped = name == "missing_minutes" and frequency != "1m"
            checks[name] = {
                "ok": True,
                "severity": None,
                "count": 0,
                "message": "skipped (only checked for 1m)" if skipped else "ok",
            }

    stats = _frame_stats(work, calendar)
    report = {
        "ok": not any(i["severity"] == ERROR for i in issues),
        "frequency": frequency,
        "rows": int(len(work)),
        "symbols": stats["symbols"],
        "start": stats["start"],
        "end": stats["end"],
        "checks": checks,
        "issues": issues,
        "stats": stats,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    if raise_on_error and not report["ok"]:
        raise DataValidationError(report)
    return report


def write_report(report: dict[str, Any], path: str | Path) -> Path:
    """Persist a validation report as UTF-8 JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=_jsonable), encoding="utf-8")
    return path
