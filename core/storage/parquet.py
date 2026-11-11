"""Parquet helpers shared by the data layer and the backtest artifact writer."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

BAR_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.string()),
        pa.field("ts", pa.timestamp("ms")),
        pa.field("open", pa.float64()),
        pa.field("high", pa.float64()),
        pa.field("low", pa.float64()),
        pa.field("close", pa.float64()),
        pa.field("volume", pa.float64()),
        pa.field("amount", pa.float64()),
        pa.field("frequency", pa.string()),
        pa.field("dataset_version", pa.string()),
    ]
)

EVENT_SCHEMA = pa.schema(
    [
        pa.field("seq", pa.int64()),
        pa.field("run_id", pa.string()),
        pa.field("simulated_time", pa.timestamp("ms")),
        pa.field("event_type", pa.string()),
        pa.field("symbol", pa.string()),
        pa.field("order_id", pa.string()),
        pa.field("payload", pa.string()),
    ]
)


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_table(table: pa.Table, path: str | Path) -> Path:
    path = Path(path)
    ensure_dir(path.parent)
    pq.write_table(table, path, compression="snappy")
    return path


def write_dataframe(df: pd.DataFrame, path: str | Path, schema: pa.Schema | None = None) -> Path:
    path = Path(path)
    ensure_dir(path.parent)
    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    pq.write_table(table, path, compression="snappy")
    return path


def write_partitioned(
    df: pd.DataFrame,
    root: str | Path,
    partition_cols: Sequence[str] = ("year",),
    schema: pa.Schema | None = None,
) -> Path:
    root = Path(root)
    ensure_dir(root)
    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    partitioning = ds.partitioning(pa.schema([(c, pa.string()) for c in partition_cols]), flavor="hive")
    ds.write_dataset(
        table,
        base_dir=str(root),
        format="parquet",
        partitioning=partitioning,
        existing_data_behavior="overwrite_or_ignore",
        file_options=ds.ParquetFileFormat().make_write_options(compression="snappy"),
    )
    return root


def read_dataset(
    root: str | Path,
    columns: Iterable[str] | None = None,
    filters: Any = None,
) -> pd.DataFrame:
    root = Path(root)
    if not root.exists():
        return pd.DataFrame()
    dataset = ds.dataset(str(root), format="parquet", partitioning="hive")
    if isinstance(filters, (list, tuple)):
        expr = None
        for item in filters:
            expr = item if expr is None else expr & item
        filters = expr
    table = dataset.to_table(columns=list(columns) if columns else None, filter=filters)
    return table.to_pandas()


def file_checksum(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def directory_checksum(path: str | Path) -> str:
    """Stable checksum over a directory tree (paths + contents)."""
    root = Path(path)
    h = hashlib.sha256()
    if not root.exists():
        return ""
    for file in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = str(file.relative_to(root))
        h.update(rel.encode())
        h.update(file_checksum(file).encode())
    return h.hexdigest()
