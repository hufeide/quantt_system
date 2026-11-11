"""Raw CSV -> versioned normalized Parquet dataset.

Normalization makes a dataset replay-ready: symbols are upper-cased, timestamps
are timezone-naive, rows are de-duplicated and sorted, dtypes are fixed to the
shared BAR_SCHEMA, and a dataset_version column is added. Output layout:

    <root>/normalized/dataset=<version>/frequency=1m/year=<yyyy>/part-0.parquet
    <root>/normalized/dataset=<version>/manifest.json
    <root>/normalized/dataset=<version>/quality_report.json

The build is atomic: partitions are written into a staging directory that then
replaces the previous version directory, so rebuilding a version never leaves
stale partitions behind.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from core.market.calendar import TradingCalendar
from core.storage.dataset_manifest import DatasetManifest
from core.storage.parquet import BAR_SCHEMA, ensure_dir, write_dataframe
from data.ingestion.rqdata_loader import standardize_frame
from data.validation.checks import DataValidationError, validate_bars, write_report

logger = logging.getLogger(__name__)

NORMALIZED_COLUMNS = [
    "symbol",
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "frequency",
    "dataset_version",
]


@dataclass
class DatasetBuild:
    """Outcome of a normalize / build_dataset call."""

    root: Path
    dataset_dir: Path
    manifest: DatasetManifest
    manifest_path: Path
    report: dict[str, Any]
    report_path: Path
    frame: pd.DataFrame = field(repr=False)

    @property
    def rows(self) -> int:
        return int(self.manifest.row_count)

    @property
    def ok(self) -> bool:
        return bool(self.report.get("ok", False))


def _as_date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def read_raw(
    root: str | Path = "data_files",
    source: str = "synthetic",
    frequency: str = "1m",
    symbols: list[str] | None = None,
    start: date | str | None = None,
    end: date | str | None = None,
    raw_format: str = "auto",
) -> pd.DataFrame:
    """Read raw CSV partitions for a source, optionally filtered by date/symbol."""
    base = Path(root) / "raw" / f"source={source}" / f"frequency={frequency}"
    if not base.exists():
        return pd.DataFrame(columns=NORMALIZED_COLUMNS)

    start, end = _as_date(start), _as_date(end)
    files: list[Path] = []
    for path in sorted(base.rglob("*.csv")):
        folder = path.parent.name
        if folder.startswith("date="):
            try:
                day = date.fromisoformat(folder.split("=", 1)[1])
            except ValueError:
                day = None
            if day is not None:
                if start is not None and day < start:
                    continue
                if end is not None and day > end:
                    continue
        files.append(path)
    if not files:
        return pd.DataFrame(columns=NORMALIZED_COLUMNS)

    frames = [pd.read_csv(path, encoding="utf-8-sig") for path in files]
    frame = pd.concat(frames, ignore_index=True)
    if frame.empty:
        return pd.DataFrame(columns=NORMALIZED_COLUMNS)

    if raw_format == "rqdata" or (raw_format == "auto" and "ts" not in frame.columns):
        frame = standardize_frame(frame, default_frequency=frequency)
    else:
        frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")

    if symbols:
        wanted = {str(s).strip().upper() for s in symbols}
        frame = frame[frame["symbol"].astype(str).str.upper().isin(wanted)]
    if start is not None:
        frame = frame[frame["ts"] >= pd.Timestamp(start)]
    if end is not None:
        frame = frame[frame["ts"] < pd.Timestamp(end) + pd.Timedelta(days=1)]
    if frame.empty:
        return pd.DataFrame(columns=NORMALIZED_COLUMNS)
    return frame.sort_values(["ts", "symbol"], kind="mergesort").reset_index(drop=True)


def normalize_frame(
    frame: pd.DataFrame,
    version: str = "v001",
    frequency: str = "1m",
) -> pd.DataFrame:
    """Return a canonical, de-duplicated, sorted frame ready for Parquet."""
    if frame.empty:
        return pd.DataFrame(columns=NORMALIZED_COLUMNS)

    work = standardize_frame(frame, default_frequency=frequency)
    if work.empty:
        return pd.DataFrame(columns=NORMALIZED_COLUMNS)

    work = work.drop_duplicates(subset=["symbol", "ts"], keep="last")
    work = work.sort_values(["symbol", "ts"], kind="mergesort").reset_index(drop=True)

    work["dataset_version"] = str(version)
    work["frequency"] = str(frequency)
    work["year"] = work["ts"].dt.year.astype(str)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        work[col] = work[col].astype("float64")

    return work[["year", *NORMALIZED_COLUMNS]]


def _write_year_partitions(normalized: pd.DataFrame, freq_dir: Path) -> Path:
    """Write one Parquet file per year under year=<yyyy>/ (hive layout).

    core.storage.parquet.write_partitioned currently passes a list containing a
    Partitioning object to pyarrow, which pyarrow ignores: no year= directories
    are created and the year column is silently dropped. Until that helper is
    fixed we write the partitions explicitly, using the same BAR_SCHEMA and a
    fixed file name so rebuilds are byte-for-byte reproducible.
    """
    ensure_dir(freq_dir)
    for year, group in normalized.groupby("year", sort=True):
        target = ensure_dir(freq_dir / f"year={year}") / "part-0.parquet"
        payload = group.drop(columns=["year"]).reset_index(drop=True)
        write_dataframe(payload, target, schema=BAR_SCHEMA)
    return freq_dir


def _schema_fields() -> list[dict[str, Any]]:
    return [
        {"name": field.name, "type": str(field.type), "nullable": bool(field.nullable)}
        for field in BAR_SCHEMA
    ]


def build_dataset(
    root: str | Path = "data_files",
    version: str = "v001",
    frequency: str = "1m",
    source: str = "synthetic",
    symbols: list[str] | None = None,
    start: date | str | None = None,
    end: date | str | None = None,
    frame: pd.DataFrame | None = None,
    raw_format: str = "auto",
    validate: bool = True,
    raise_on_error: bool = True,
    calendar: TradingCalendar | None = None,
    notes: str = "",
) -> DatasetBuild:
    """Normalize raw bars into a versioned Parquet dataset with manifest.

    Either pass a frame directly (useful in tests) or let the function read the
    raw partitions under <root>/raw/source=<source>/frequency=<frequency>.
    """
    root = Path(root)
    raw = frame if frame is not None else read_raw(root, source, frequency, symbols, start, end, raw_format)
    if raw.empty:
        raise ValueError(
            f"no raw bars found under {root / 'raw' / f'source={source}'} "
            f"for symbols={symbols} start={start} end={end}"
        )

    normalized = normalize_frame(raw, version=version, frequency=frequency)
    if normalized.empty:
        raise ValueError("normalization produced no rows")

    report: dict[str, Any]
    if validate:
        report = validate_bars(normalized, calendar=calendar, frequency=frequency, raise_on_error=False)
        if raise_on_error and not report["ok"]:
            raise DataValidationError(report)
    else:
        report = {"ok": True, "skipped": True, "rows": int(len(normalized)), "issues": [], "checks": {}}

    version_dir = root / "normalized" / f"dataset={version}"
    staging = version_dir.with_name(version_dir.name + ".building")
    if staging.exists():
        shutil.rmtree(staging)
    freq_dir = ensure_dir(staging / f"frequency={frequency}")

    try:
        _write_year_partitions(normalized, freq_dir)

        manifest = DatasetManifest(
            dataset_version=version,
            frequency=frequency,
            source=source,
            symbols=sorted(normalized["symbol"].unique().tolist()),
            start=normalized["ts"].min().isoformat(),
            end=normalized["ts"].max().isoformat(),
            row_count=int(len(normalized)),
            schema=_schema_fields(),
            notes=notes,
        )
        manifest.compute_checksum(freq_dir)
        manifest.stats = {
            "rows": int(len(normalized)),
            "symbols": len(manifest.symbols),
            "trading_days": int(normalized["ts"].dt.date.nunique()),
            "partitions": sorted(p.name for p in freq_dir.iterdir() if p.is_dir()),
            "quality_ok": bool(report.get("ok")),
            "issue_count": len(report.get("issues", [])),
        }

        if version_dir.exists():
            shutil.rmtree(version_dir)
        staging.rename(version_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    manifest_path = manifest.save(version_dir / "manifest.json")
    report_path = write_report(report, version_dir / "quality_report.json")

    logger.info(
        "dataset %s built: %d rows, %d symbols, checksum %s",
        version,
        manifest.row_count,
        len(manifest.symbols),
        manifest.checksum[:12],
    )
    return DatasetBuild(
        root=root,
        dataset_dir=version_dir / f"frequency={frequency}",
        manifest=manifest,
        manifest_path=manifest_path,
        report=report,
        report_path=report_path,
        frame=normalized,
    )
