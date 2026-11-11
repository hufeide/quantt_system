"""Versioned Parquet market data store (the authoritative normalized dataset)."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

from core.market.models import Bar
from core.storage.dataset_manifest import DatasetManifest
from core.storage.parquet import read_dataset


class DatasetNotFound(FileNotFoundError):
    pass


class ParquetMarketDataStore:
    """Reads normalized, versioned minute bars from a hive-partitioned dataset.

    Layout::

        <root>/normalized/dataset=<version>/frequency=<freq>/year=<yyyy>/part-*.parquet
        <root>/normalized/dataset=<version>/manifest.json
    """

    def __init__(self, root: str | Path = "data_files", version: str = "v001", frequency: str = "1m") -> None:
        self.root = Path(root)
        self.version = version
        self.frequency = frequency

    @property
    def dataset_dir(self) -> Path:
        return self.root / "normalized" / f"dataset={self.version}" / f"frequency={self.frequency}"

    @property
    def manifest_path(self) -> Path:
        return self.root / "normalized" / f"dataset={self.version}" / "manifest.json"

    def exists(self) -> bool:
        return self.dataset_dir.exists()

    def manifest(self) -> DatasetManifest | None:
        if self.manifest_path.exists():
            return DatasetManifest.load(self.manifest_path)
        return None

    def _filters(
        self,
        symbols: Sequence[str] | None = None,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
    ) -> list:
        filters = []
        if symbols:
            filters.append(ds.field("symbol").isin(list(symbols)))
        if start is not None:
            filters.append(ds.field("ts") >= pa.scalar(pd.Timestamp(start).to_pydatetime(), pa.timestamp("ms")))
        if end is not None:
            end_ts = pd.Timestamp(end)
            if end_ts.time() == pd.Timestamp(0).time() and not isinstance(end, datetime):
                end_ts = end_ts + pd.Timedelta(days=1)
            filters.append(ds.field("ts") < pa.scalar(end_ts.to_pydatetime(), pa.timestamp("ms")))
        return filters

    def load_frame(
        self,
        symbols: Sequence[str] | None = None,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        columns: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        if not self.exists():
            raise DatasetNotFound(f"dataset {self.version} not found at {self.dataset_dir}")
        filters = self._filters(symbols, start, end)
        df = read_dataset(self.dataset_dir, columns=columns, filters=filters)
        if df.empty:
            return df
        df = df.sort_values(["ts", "symbol"], kind="mergesort").reset_index(drop=True)
        return df

    def load_bars(
        self,
        symbols: Sequence[str] | None = None,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
    ) -> list[Bar]:
        df = self.load_frame(symbols, start, end)
        if df.empty:
            return []
        return [
            Bar(
                symbol=str(row.symbol),
                ts=row.ts.to_pydatetime() if hasattr(row.ts, "to_pydatetime") else row.ts,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
                amount=float(row.amount) if not pd.isna(row.amount) else 0.0,
                frequency=str(row.frequency) if getattr(row, "frequency", None) else self.frequency,
            )
            for row in df.itertuples(index=False)
        ]

    def group_bars(self, bars: Iterable[Bar]) -> list[tuple[datetime, list[Bar]]]:
        groups: list[tuple[datetime, list[Bar]]] = []
        current_ts: datetime | None = None
        bucket: list[Bar] = []
        for bar in bars:
            if current_ts is None or bar.ts == current_ts:
                current_ts = bar.ts
                bucket.append(bar)
            else:
                groups.append((current_ts, bucket))
                current_ts = bar.ts
                bucket = [bar]
        if bucket and current_ts is not None:
            groups.append((current_ts, bucket))
        return groups

    def load_groups(
        self,
        symbols: Sequence[str] | None = None,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
    ) -> list[tuple[datetime, list[Bar]]]:
        return self.group_bars(self.load_bars(symbols, start, end))

    def symbols(self) -> list[str]:
        if not self.exists():
            return []
        dataset = ds.dataset(str(self.dataset_dir), format="parquet", partitioning="hive")
        values = dataset.to_table(columns=["symbol"]).column("symbol").unique().to_pylist()
        return sorted(v for v in values if v)

    def date_range(self) -> tuple[datetime | None, datetime | None]:
        if not self.exists():
            return None, None
        dataset = ds.dataset(str(self.dataset_dir), format="parquet", partitioning="hive")
        table = dataset.to_table(columns=["ts"])
        if table.num_rows == 0:
            return None, None
        col = table.column("ts")
        return col.to_pandas().min().to_pydatetime(), col.to_pandas().max().to_pydatetime()

    def stats(self) -> dict:
        if not self.exists():
            return {"exists": False}
        df = self.load_frame(columns=["symbol", "ts", "volume"])
        if df.empty:
            return {"exists": True, "rows": 0}
        return {
            "exists": True,
            "rows": int(len(df)),
            "symbols": int(df["symbol"].nunique()),
            "start": df["ts"].min().isoformat(),
            "end": df["ts"].max().isoformat(),
            "duplicate_bars": int(df.duplicated(subset=["symbol", "ts"]).sum()),
        }

    def to_duckdb(self, con=None, view: str = "bars"):
        """Register the dataset as a DuckDB view for analytical queries."""
        import duckdb

        con = con or duckdb.connect()
        pattern = str(self.dataset_dir / "**" / "*.parquet")
        con.execute(
            f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{pattern}', hive_partitioning=true)"
        )
        return con
