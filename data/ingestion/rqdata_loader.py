"""Loader for RiceQuant (rqdata) style CSV exports.

Column names in real exports vary between "datetime" / "date" + "time" and
"order_book_id" / "symbol". This module maps the common variants onto the
canonical bar columns, coerces dtypes and strips timezones so the output can go
straight into normalization.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

STANDARD_COLUMNS = [
    "symbol",
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "frequency",
]

_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("order_book_id", "symbol", "code", "ticker", "instrument", "sec_code"),
    "datetime": ("datetime", "date_time", "timestamp", "ts", "dt"),
    "date": ("date", "trading_date", "trade_date"),
    "time": ("time", "bar_time"),
    "open": ("open", "open_price", "o"),
    "high": ("high", "high_price", "h"),
    "low": ("low", "low_price", "l"),
    "close": ("close", "close_price", "c", "last"),
    "volume": ("volume", "vol", "v"),
    "amount": ("amount", "turnover", "value", "amt"),
    "frequency": ("frequency", "freq", "interval"),
}

_PRICE_COLUMNS = ("open", "high", "low", "close")


def _canonical_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename columns to canonical names, tolerating case/space/underscore noise."""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for col in df.columns:
        key = str(col).strip().lower().replace(" ", "_").replace("-", "_")
        target = key
        for canonical, aliases in _ALIASES.items():
            if key in aliases and canonical not in used:
                target = canonical
                break
        used.add(target)
        mapping[col] = target
    return df.rename(columns=mapping)


def _parse_date_column(series: pd.Series) -> pd.Series:
    """Parse dates that may arrive as 2025-01-02 strings or 20250102 integers."""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series.astype("int64").astype(str), format="%Y%m%d", errors="coerce")
    return pd.to_datetime(series, errors="coerce", format="mixed")


def _extract_timestamps(df: pd.DataFrame) -> pd.Series:
    if "datetime" in df.columns:
        ts = pd.to_datetime(df["datetime"], errors="coerce", format="mixed")
    elif "date" in df.columns and "time" in df.columns:
        combined = df["date"].astype(str).str.strip() + " " + df["time"].astype(str).str.strip()
        ts = pd.to_datetime(combined, errors="coerce", format="mixed")
    elif "date" in df.columns:
        ts = _parse_date_column(df["date"])
    else:
        raise ValueError(
            "cannot find a timestamp column; expected one of datetime, date+time or date"
        )
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    return ts


def standardize_frame(
    df: pd.DataFrame,
    default_frequency: str = "1m",
    symbol: str | None = None,
    drop_invalid: bool = True,
) -> pd.DataFrame:
    """Map an arbitrary rqdata-like frame onto the canonical bar schema.

    Missing volume/amount columns are filled with zeros; rows without a valid
    timestamp or without all four prices are dropped (and logged) when
    drop_invalid is true.
    """
    if df.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    work = _canonical_columns(df.copy())
    work["ts"] = _extract_timestamps(work)

    if "symbol" in work.columns:
        work["symbol"] = work["symbol"].astype(str).str.strip().str.upper()
    elif symbol is not None:
        work["symbol"] = str(symbol).strip().upper()
    else:
        raise ValueError("no symbol column found and no default symbol given")

    missing = [c for c in ("open", "high", "low", "close") if c not in work.columns]
    if missing:
        raise ValueError(f"missing required price columns: {missing}")

    for col in (*_PRICE_COLUMNS, "volume", "amount"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce").astype("float64")
        else:
            work[col] = 0.0 if col in ("volume", "amount") else pd.NA

    if "frequency" in work.columns:
        work["frequency"] = work["frequency"].fillna(default_frequency).astype(str)
    else:
        work["frequency"] = default_frequency

    before = len(work)
    work = work.dropna(subset=["ts", *_PRICE_COLUMNS])
    work = work[work["symbol"] != ""]
    dropped = before - len(work)
    if dropped and drop_invalid:
        logger.warning("dropped %d row(s) without a valid timestamp or price", dropped)

    work = work.sort_values(["symbol", "ts"], kind="mergesort").reset_index(drop=True)
    return work[STANDARD_COLUMNS]


def load_rqdata_csv(
    path: str | Path,
    default_frequency: str = "1m",
    symbol: str | None = None,
    **read_csv_kwargs,
) -> pd.DataFrame:
    """Read a single rqdata CSV export and return standardized bars."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    kwargs = {"encoding": "utf-8-sig"}
    kwargs.update(read_csv_kwargs)
    raw = pd.read_csv(path, **kwargs)
    return standardize_frame(raw, default_frequency=default_frequency, symbol=symbol)


def load_rqdata_directory(
    directory: str | Path,
    pattern: str = "*.csv",
    symbols: list[str] | None = None,
    default_frequency: str = "1m",
) -> pd.DataFrame:
    """Read and concatenate every matching CSV under a directory."""
    directory = Path(directory)
    if not directory.exists():
        return pd.DataFrame(columns=STANDARD_COLUMNS)
    files = sorted(p for p in directory.rglob(pattern) if p.is_file())
    if not files:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    frames = [load_rqdata_csv(f, default_frequency=default_frequency) for f in files]
    frame = pd.concat(frames, ignore_index=True)
    if symbols:
        wanted = {str(s).strip().upper() for s in symbols}
        frame = frame[frame["symbol"].isin(wanted)]
    if frame.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)
    return frame.sort_values(["symbol", "ts"], kind="mergesort").reset_index(drop=True)
