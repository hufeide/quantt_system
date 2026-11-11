"""Deterministic synthetic minute-bar generator.

Bars are produced with a geometric Brownian motion price path, an overnight gap
between sessions and a U-shaped intraday volume profile (heavy at the open and
close, light around lunch). The generator is fully deterministic for a given
seed: the per-symbol random stream is derived from a SHA-256 digest of
(seed, symbol), so symbol order or adding symbols never changes existing series.

Raw output layout (CSV, one file per trading day):

    <raw_root>/raw/source=synthetic/frequency=1m/date=YYYY-MM-DD/part.csv
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from core.market.calendar import TradingCalendar
from core.storage.parquet import ensure_dir

RAW_COLUMNS = [
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

# A-share price tick and lot size keep the output realistic.
PRICE_TICK = 0.01
LOT_SIZE = 100.0


@dataclass(frozen=True)
class SyntheticConfig:
    """Parameters of a synthetic dataset generation run."""

    symbols: tuple[str, ...] = ("600000.SH", "000001.SZ")
    start: date = date(2025, 1, 2)
    end: date = date(2025, 1, 10)
    seed: int = 42
    frequency: str = "1m"
    start_price: float = 10.0
    daily_drift: float = 0.0005
    minute_vol: float = 0.0012
    gap_vol: float = 0.006
    base_volume: float = 40_000.0
    holidays: tuple[date, ...] = ()

    def __post_init__(self) -> None:
        symbols = tuple(str(s).strip().upper() for s in self.symbols if str(s).strip())
        if not symbols:
            raise ValueError("SyntheticConfig.symbols must not be empty")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "start", _as_date(self.start))
        object.__setattr__(self, "end", _as_date(self.end))
        object.__setattr__(self, "holidays", tuple(_as_date(h) for h in self.holidays))
        if self.start > self.end:
            raise ValueError("start must not be after end")
        if self.minute_vol <= 0 or self.base_volume <= 0:
            raise ValueError("minute_vol and base_volume must be positive")

    @property
    def calendar(self) -> TradingCalendar:
        return TradingCalendar(holidays=set(self.holidays))


@dataclass
class RawBuild:
    """Result of writing synthetic raw data."""

    frame: pd.DataFrame
    files: list[Path]
    config: SyntheticConfig


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _symbol_rng(seed: int, symbol: str) -> np.random.Generator:
    """Stable per-symbol random stream (independent of Python's hash salt)."""
    digest = hashlib.sha256(f"{seed}:{symbol}".encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def _u_shape(n: int) -> np.ndarray:
    """U-shaped intraday liquidity profile, high at both ends of the day."""
    if n <= 1:
        return np.ones(max(n, 1), dtype=float)
    index = np.arange(n, dtype=float)
    return 1.55 - 1.25 * np.sin(np.pi * (index + 0.5) / n)


def _generate_symbol(config: SyntheticConfig, symbol: str, days: list[date]) -> pd.DataFrame:
    rng = _symbol_rng(config.seed, symbol)
    calendar = config.calendar
    price = max(round(config.start_price * float(np.exp(rng.normal(0.0, 0.35))), 2), 2.0)

    frames: list[pd.DataFrame] = []
    for day in days:
        minutes = list(calendar.iter_minutes(day, day))
        n = len(minutes)
        if n == 0:
            continue

        # Overnight gap, then a GBM path inside the session.
        day_open = max(price * float(np.exp(rng.normal(0.0, config.gap_vol))), PRICE_TICK)
        sigma = config.minute_vol
        shocks = rng.normal(config.daily_drift / n - 0.5 * sigma**2, sigma, n)
        closes = day_open * np.exp(np.cumsum(shocks))
        opens = np.concatenate(([day_open], closes[:-1]))

        wick_up = np.abs(rng.normal(0.0, sigma * 0.7, n))
        wick_down = np.abs(rng.normal(0.0, sigma * 0.7, n))
        highs = np.maximum(opens, closes) * np.exp(wick_up)
        lows = np.minimum(opens, closes) * np.exp(-wick_down)

        volume = config.base_volume * _u_shape(n) * rng.lognormal(0.0, 0.30, n)
        volume = np.maximum(np.round(volume / LOT_SIZE) * LOT_SIZE, LOT_SIZE)

        o = np.round(opens, 2)
        c = np.round(closes, 2)
        h = np.maximum(np.maximum(o, c), np.round(highs, 2))
        low = np.minimum(np.minimum(o, c), np.round(lows, 2))
        low = np.maximum(low, PRICE_TICK)
        amount = np.round(volume * (o + h + low + c) / 4.0, 2)

        frames.append(
            pd.DataFrame(
                {
                    "symbol": symbol,
                    "ts": pd.to_datetime(minutes),
                    "open": o,
                    "high": h,
                    "low": low,
                    "close": c,
                    "volume": volume,
                    "amount": amount,
                    "frequency": config.frequency,
                }
            )
        )
        price = float(closes[-1])

    if not frames:
        return pd.DataFrame(columns=RAW_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def generate_bars(config: SyntheticConfig | None = None) -> pd.DataFrame:
    """Generate minute bars for every symbol and trading day in the config.

    Only trading days and in-session minutes are produced. Rows are sorted by
    (ts, symbol) so downstream code sees a canonical order.
    """
    config = config or SyntheticConfig()
    days = config.calendar.trading_days(config.start, config.end)
    if not days:
        return pd.DataFrame(columns=RAW_COLUMNS)

    frames = [_generate_symbol(config, symbol, days) for symbol in config.symbols]
    frame = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    if frame.empty:
        return pd.DataFrame(columns=RAW_COLUMNS)
    frame["ts"] = pd.to_datetime(frame["ts"])
    frame = frame.sort_values(["ts", "symbol"], kind="mergesort").reset_index(drop=True)
    return frame[RAW_COLUMNS]


def write_raw(
    frame: pd.DataFrame,
    raw_root: str | Path = "data_files",
    source: str = "synthetic",
    frequency: str = "1m",
) -> list[Path]:
    """Write a raw frame as one CSV per trading day, hive partitioned by date."""
    if frame.empty:
        return []
    base = Path(raw_root) / "raw" / f"source={source}" / f"frequency={frequency}"
    work = frame.copy()
    work["ts"] = pd.to_datetime(work["ts"])
    work["_date"] = work["ts"].dt.strftime("%Y-%m-%d")

    files: list[Path] = []
    for day, group in work.groupby("_date", sort=True):
        target = ensure_dir(base / f"date={day}") / "part.csv"
        group.drop(columns=["_date"]).to_csv(
            target,
            index=False,
            date_format="%Y-%m-%d %H:%M:%S",
            float_format="%.6f",
            encoding="utf-8",
        )
        files.append(target)
    return files


def generate_raw(config: SyntheticConfig | None = None, raw_root: str | Path = "data_files") -> RawBuild:
    """Generate bars and persist them under <raw_root>/raw/source=synthetic."""
    config = config or SyntheticConfig()
    frame = generate_bars(config)
    files = write_raw(frame, raw_root, source="synthetic", frequency=config.frequency)
    return RawBuild(frame=frame, files=files, config=config)
