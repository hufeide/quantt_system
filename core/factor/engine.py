"""Factor engine: definitions, computation, versioned Parquet storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from core.storage.parquet import ensure_dir, write_partitioned


@dataclass
class FactorDefinition:
    name: str
    description: str
    func: Callable[[pd.DataFrame], pd.Series]
    params: dict[str, Any] = field(default_factory=dict)
    lookback: int = 1


class FactorRegistry:
    def __init__(self) -> None:
        self._factors: dict[str, FactorDefinition] = {}

    def register(self, definition: FactorDefinition) -> None:
        self._factors[definition.name] = definition

    def get(self, name: str) -> FactorDefinition:
        if name not in self._factors:
            raise KeyError(f"unknown factor '{name}'. available: {sorted(self._factors)}")
        return self._factors[name]

    def names(self) -> list[str]:
        return sorted(self._factors)

    def list(self) -> list[dict[str, Any]]:
        return [
            {"name": f.name, "description": f.description, "params": f.params, "lookback": f.lookback}
            for f in sorted(self._factors.values(), key=lambda d: d.name)
        ]


registry = FactorRegistry()


def factor(name: str, description: str = "", params: dict[str, Any] | None = None, lookback: int = 1):
    def decorator(func: Callable[[pd.DataFrame], pd.Series]) -> Callable[[pd.DataFrame], pd.Series]:
        registry.register(FactorDefinition(name, description, func, params or {}, lookback))
        return func

    return decorator


@factor("momentum", "N-bar return", {"window": 20}, lookback=20)
def momentum_factor(df: pd.DataFrame, window: int = 20) -> pd.Series:
    return df.groupby("symbol", group_keys=False)["close"].apply(lambda s: s / s.shift(window) - 1.0)


@factor("volatility", "Rolling return volatility", {"window": 20}, lookback=20)
def volatility_factor(df: pd.DataFrame, window: int = 20) -> pd.Series:
    def _vol(s: pd.Series) -> pd.Series:
        ret = s.pct_change()
        return ret.rolling(window).std(ddof=0)

    return df.groupby("symbol", group_keys=False)["close"].apply(_vol)


@factor("ma_ratio", "Close / SMA ratio", {"window": 20}, lookback=20)
def ma_ratio_factor(df: pd.DataFrame, window: int = 20) -> pd.Series:
    def _ratio(s: pd.Series) -> pd.Series:
        return s / s.rolling(window).mean() - 1.0

    return df.groupby("symbol", group_keys=False)["close"].apply(_ratio)


@factor("volume_ratio", "Volume / average volume", {"window": 20}, lookback=20)
def volume_ratio_factor(df: pd.DataFrame, window: int = 20) -> pd.Series:
    def _ratio(s: pd.Series) -> pd.Series:
        return s / s.rolling(window).mean()

    return df.groupby("symbol", group_keys=False)["volume"].apply(_ratio)


@factor("intraday_range", "High-low range relative to close", {}, lookback=1)
def intraday_range_factor(df: pd.DataFrame) -> pd.Series:
    return (df["high"] - df["low"]) / df["close"]


@factor("rsi", "Relative strength index", {"window": 14}, lookback=14)
def rsi_factor(df: pd.DataFrame, window: int = 14) -> pd.Series:
    def _rsi(s: pd.Series) -> pd.Series:
        delta = s.diff()
        gain = delta.clip(lower=0).rolling(window).mean()
        loss = (-delta.clip(upper=0)).rolling(window).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - 100 / (1 + rs)

    return df.groupby("symbol", group_keys=False)["close"].apply(_rsi)


class FactorEngine:
    """Computes and stores factor values for a dataset version."""

    def __init__(self, root: str | Path = "data_files", version: str = "v001") -> None:
        self.root = Path(root)
        self.version = version

    @property
    def factor_dir(self) -> Path:
        return self.root / "factors" / f"version={self.version}"

    def compute(
        self,
        bars: pd.DataFrame,
        factor_names: list[str] | None = None,
        params: dict[str, dict[str, Any]] | None = None,
    ) -> pd.DataFrame:
        if bars.empty:
            return pd.DataFrame(columns=["ts", "symbol", "factor", "value"])
        df = bars.sort_values(["symbol", "ts"]).reset_index(drop=True).copy()
        names = factor_names or registry.names()
        params = params or {}
        frames = []
        for name in names:
            definition = registry.get(name)
            kwargs = dict(definition.params)
            kwargs.update(params.get(name, {}))
            values = definition.func(df, **kwargs)
            if values is None:
                continue
            out = pd.DataFrame(
                {
                    "ts": df["ts"].values,
                    "symbol": df["symbol"].values,
                    "factor": name,
                    "value": np.asarray(values, dtype=float),
                }
            )
            out["year"] = pd.to_datetime(out["ts"]).dt.year.astype(str)
            frames.append(out)
        if not frames:
            return pd.DataFrame(columns=["ts", "symbol", "factor", "value"])
        return pd.concat(frames, ignore_index=True)

    def store(self, factors: pd.DataFrame) -> Path:
        ensure_dir(self.factor_dir)
        payload = factors.dropna(subset=["value"]).copy()
        if payload.empty:
            return self.factor_dir
        payload["ts"] = pd.to_datetime(payload["ts"])
        payload["year"] = payload["ts"].dt.year.astype(str)
        return write_partitioned(payload, self.factor_dir, partition_cols=("year",))

    def compute_and_store(
        self,
        bars: pd.DataFrame,
        factor_names: list[str] | None = None,
        params: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        factors = self.compute(bars, factor_names, params)
        path = self.store(factors)
        summary = {}
        if not factors.empty:
            for name, group in factors.groupby("factor"):
                valid = group["value"].dropna()
                summary[name] = {
                    "rows": int(len(group)),
                    "valid": int(len(valid)),
                    "mean": float(valid.mean()) if len(valid) else None,
                    "std": float(valid.std(ddof=0)) if len(valid) else None,
                }
        return {"path": str(path), "version": self.version, "factors": summary}

    def load(self, factor_names: list[str] | None = None, symbols: list[str] | None = None) -> pd.DataFrame:
        from core.storage.parquet import read_dataset

        if not self.factor_dir.exists():
            return pd.DataFrame(columns=["ts", "symbol", "factor", "value"])
        df = read_dataset(self.factor_dir)
        if df.empty:
            return df
        if factor_names:
            df = df[df["factor"].isin(factor_names)]
        if symbols:
            df = df[df["symbol"].isin(symbols)]
        return df.sort_values(["ts", "symbol", "factor"]).reset_index(drop=True)

    def pivot(self, factor_name: str, symbols: list[str] | None = None) -> pd.DataFrame:
        df = self.load([factor_name], symbols)
        if df.empty:
            return df
        return df.pivot_table(index="ts", columns="symbol", values="value")
