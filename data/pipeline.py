"""End-to-end data pipeline: generate -> validate -> normalize -> factors.

build_pipeline() is the single entry point used by the CLI and by tests. It
writes raw CSV partitions, builds the versioned normalized Parquet dataset
(with manifest and quality report) and optionally computes factors through
core.factor.FactorEngine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from core.factor.engine import FactorEngine, registry
from core.market.calendar import TradingCalendar
from data.ingestion.synthetic import SyntheticConfig, generate_raw
from data.normalization.normalize import DatasetBuild, build_dataset

# build_dataset is re-exported on purpose: API/worker code imports it from
# data.pipeline, and the canonical definition lives in data.normalization.
__all__ = ["PipelineResult", "build_dataset", "build_pipeline", "default_factor_names"]

# The core registry declares a "window" param for intraday_range but the
# function does not accept it, so computing every registered factor raises
# TypeError. Until core is fixed we compute the factors that are callable.
DEFAULT_FACTORS = ("momentum", "volatility", "ma_ratio", "volume_ratio", "rsi")


def default_factor_names() -> list[str]:
    """Registered factors that can be computed with their declared params."""
    return [name for name in registry.names() if name in DEFAULT_FACTORS]


@dataclass
class PipelineResult:
    """Artifacts produced by build_pipeline."""

    root: Path
    build: DatasetBuild
    raw_files: list[Path]
    factor_summary: dict[str, Any] | None = None

    @property
    def dataset_dir(self) -> Path:
        return self.build.dataset_dir

    @property
    def manifest(self):
        return self.build.manifest

    @property
    def report(self) -> dict[str, Any]:
        return self.build.report

    @property
    def frame(self) -> pd.DataFrame:
        return self.build.frame

    @property
    def rows(self) -> int:
        return self.build.rows

    @property
    def ok(self) -> bool:
        return self.build.ok


def build_pipeline(
    root: str | Path = "data_files",
    version: str = "v001",
    symbols: tuple[str, ...] | list[str] = ("600000.SH", "000001.SZ"),
    start: date | str = date(2025, 1, 2),
    end: date | str = date(2025, 1, 10),
    seed: int = 42,
    source: str = "synthetic",
    frequency: str = "1m",
    with_factors: bool = False,
    factor_names: list[str] | None = None,
    calendar: TradingCalendar | None = None,
    raise_on_error: bool = True,
    synthetic_config: SyntheticConfig | None = None,
    notes: str = "",
) -> PipelineResult:
    """Build a complete dataset from a source and optionally compute factors.

    For source="synthetic" the raw bars are regenerated deterministically from
    the seed before normalization; for other sources existing raw files under
    <root>/raw/source=<source> are normalized as-is.
    """
    root = Path(root)
    symbols = tuple(str(s).strip().upper() for s in symbols)
    raw_files: list[Path] = []

    if source == "synthetic":
        config = synthetic_config or SyntheticConfig(
            symbols=symbols,
            start=start,
            end=end,
            seed=seed,
            frequency=frequency,
        )
        raw = generate_raw(config, raw_root=root)
        if raw.frame.empty:
            raise ValueError("synthetic generator produced no bars; check the date range")
        raw_files = raw.files
        symbols = config.symbols

    build = build_dataset(
        root=root,
        version=version,
        frequency=frequency,
        source=source,
        symbols=list(symbols) or None,
        start=start,
        end=end,
        calendar=calendar,
        raise_on_error=raise_on_error,
        notes=notes,
    )

    factor_summary: dict[str, Any] | None = None
    if with_factors:
        engine = FactorEngine(root=root, version=version)
        names = factor_names if factor_names is not None else default_factor_names()
        factor_summary = engine.compute_and_store(build.frame, factor_names=names)

    return PipelineResult(root=root, build=build, raw_files=raw_files, factor_summary=factor_summary)
