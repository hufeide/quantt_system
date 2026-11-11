"""DuckDB query layer over normalized Parquet bars and backtest artifacts.

DuckDB is a local analytical engine only: Parquet stays the authoritative store.
This module registers the normalized dataset as a view and offers a small set of
convenience queries (bar filters, daily aggregation, run artifacts) plus a
generic SQL escape hatch that returns a DataFrame.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from core.market.store import DatasetNotFound

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def _quote_path(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def _end_exclusive(end: date | datetime | str | None) -> pd.Timestamp | None:
    if end is None:
        return None
    ts = pd.Timestamp(end)
    if not isinstance(end, datetime) and ts.time() == pd.Timestamp(0).time():
        ts = ts + pd.Timedelta(days=1)
    return ts


class DuckDBStore:
    """Analytical queries over a normalized dataset and its backtest runs.

    Example::

        store = DuckDBStore("data_files", "v001")
        store.bars(["600000.SH"], start="2025-01-02")
        store.daily_bars()
        store.query("SELECT symbol, count(*) FROM bars GROUP BY 1")
    """

    def __init__(
        self,
        root: str | Path = "data_files",
        version: str = "v001",
        frequency: str = "1m",
        database: str | Path = ":memory:",
    ) -> None:
        self.root = Path(root)
        self.version = version
        self.frequency = frequency
        self.database = str(database)
        self._con: Any = None

    # -- connection -------------------------------------------------------
    @property
    def dataset_dir(self) -> Path:
        return self.root / "normalized" / f"dataset={self.version}" / f"frequency={self.frequency}"

    @property
    def backtest_root(self) -> Path:
        return self.root / "backtests"

    @property
    def connection(self):
        if self._con is None:
            import duckdb

            self._con = duckdb.connect(self.database)
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def __enter__(self) -> "DuckDBStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- dataset views ----------------------------------------------------
    def register_bars(self, view: str = "bars") -> str:
        """Create (or replace) a view over the dataset's Parquet files."""
        if not _IDENTIFIER.match(view):
            raise ValueError(f"invalid view name: {view!r}")
        if not self.dataset_dir.exists():
            raise DatasetNotFound(f"dataset {self.version} not found at {self.dataset_dir}")
        pattern = _quote_path(self.dataset_dir / "**" / "*.parquet")
        self.connection.execute(
            f"CREATE OR REPLACE VIEW {view} AS "
            f"SELECT * FROM read_parquet('{pattern}', hive_partitioning=true)"
        )
        return view

    def query(self, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
        """Run arbitrary SQL and return a DataFrame."""
        cursor = self.connection.execute(sql, list(params)) if params else self.connection.execute(sql)
        return cursor.df()

    # -- market data ------------------------------------------------------
    def _where(
        self,
        symbols: Sequence[str] | None,
        start: date | datetime | str | None,
        end: date | datetime | str | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if symbols:
            placeholders = ", ".join("?" for _ in symbols)
            clauses.append(f"symbol IN ({placeholders})")
            params.extend(str(s).upper() for s in symbols)
        if start is not None:
            clauses.append("ts >= CAST(? AS TIMESTAMP)")
            params.append(pd.Timestamp(start).strftime("%Y-%m-%d %H:%M:%S"))
        end_ts = _end_exclusive(end)
        if end_ts is not None:
            clauses.append("ts < CAST(? AS TIMESTAMP)")
            params.append(end_ts.strftime("%Y-%m-%d %H:%M:%S"))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def bars(
        self,
        symbols: Sequence[str] | None = None,
        start: date | datetime | str | None = None,
        end: date | datetime | str | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Return minute bars ordered by (ts, symbol)."""
        self.register_bars()
        where, params = self._where(symbols, start, end)
        sql = (
            "SELECT symbol, ts, open, high, low, close, volume, amount, frequency "
            f"FROM bars{where} ORDER BY ts, symbol"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, params)

    def daily_bars(
        self,
        symbols: Sequence[str] | None = None,
        start: date | datetime | str | None = None,
        end: date | datetime | str | None = None,
    ) -> pd.DataFrame:
        """Aggregate minute bars into daily OHLCV rows."""
        self.register_bars()
        where, params = self._where(symbols, start, end)
        sql = f"""
            SELECT
                symbol,
                CAST(ts AS DATE) AS trade_date,
                arg_min(open, ts) AS open,
                max(high) AS high,
                min(low) AS low,
                arg_max(close, ts) AS close,
                sum(volume) AS volume,
                sum(amount) AS amount,
                count(*) AS bars
            FROM bars{where}
            GROUP BY symbol, CAST(ts AS DATE)
            ORDER BY trade_date, symbol
        """
        return self.query(sql, params)

    def symbols(self) -> list[str]:
        self.register_bars()
        rows = self.query("SELECT DISTINCT symbol FROM bars ORDER BY symbol")
        return rows["symbol"].tolist()

    def date_range(self) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
        self.register_bars()
        row = self.query("SELECT min(ts) AS start, max(ts) AS end FROM bars").iloc[0]
        start = pd.Timestamp(row["start"]) if pd.notna(row["start"]) else None
        end = pd.Timestamp(row["end"]) if pd.notna(row["end"]) else None
        return start, end

    def stats(self) -> dict[str, Any]:
        self.register_bars()
        row = self.query(
            """
            SELECT count(*) AS rows,
                   count(DISTINCT symbol) AS symbols,
                   min(ts) AS start,
                   max(ts) AS end,
                   sum(volume) AS volume,
                   sum(amount) AS amount
            FROM bars
            """
        ).iloc[0]
        duplicates = self.query(
            "SELECT count(*) AS n FROM (SELECT symbol, ts FROM bars GROUP BY 1, 2 HAVING count(*) > 1)"
        ).iloc[0]["n"]
        return {
            "rows": int(row["rows"]),
            "symbols": int(row["symbols"]),
            "start": pd.Timestamp(row["start"]).isoformat() if pd.notna(row["start"]) else None,
            "end": pd.Timestamp(row["end"]).isoformat() if pd.notna(row["end"]) else None,
            "volume": float(row["volume"] or 0.0),
            "amount": float(row["amount"] or 0.0),
            "duplicate_bars": int(duplicates),
        }

    # -- backtest artifacts ----------------------------------------------
    def run_ids(self) -> list[str]:
        if not self.backtest_root.exists():
            return []
        return sorted(
            p.name.split("=", 1)[1]
            for p in self.backtest_root.iterdir()
            if p.is_dir() and p.name.startswith("run_id=")
        )

    def _run_dir(self, run_id: str) -> Path:
        if not _RUN_ID.match(str(run_id)):
            raise ValueError(f"invalid run_id: {run_id!r}")
        run_dir = self.backtest_root / f"run_id={run_id}"
        if not run_dir.exists():
            raise FileNotFoundError(run_dir)
        return run_dir

    def load_artifact(self, run_id: str, name: str = "equity") -> pd.DataFrame:
        """Read one artifact Parquet file from a backtest run."""
        path = self._run_dir(run_id) / f"{name}.parquet"
        if not path.exists():
            raise FileNotFoundError(path)
        df = self.query(f"SELECT * FROM read_parquet('{_quote_path(path)}')")
        if "ts" in df.columns:
            df = df.sort_values("ts").reset_index(drop=True)
        return df

    def load_equity(self, run_id: str) -> pd.DataFrame:
        return self.load_artifact(run_id, "equity")

    def load_trades(self, run_id: str) -> pd.DataFrame:
        return self.load_artifact(run_id, "trades")

    def run_summary(self, run_id: str) -> dict[str, Any]:
        """Summarize a backtest run from its metrics.json and artifacts."""
        run_dir = self._run_dir(run_id)
        metrics: dict[str, Any] = {}
        metrics_path = run_dir / "metrics.json"
        if metrics_path.exists():
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))

        summary: dict[str, Any] = {"run_id": run_id, "metrics": metrics}
        equity_path = run_dir / "equity.parquet"
        if equity_path.exists():
            equity = self.load_equity(run_id)
            summary["equity_rows"] = int(len(equity))
            if "equity" in equity.columns and len(equity):
                summary["final_equity"] = float(equity["equity"].iloc[-1])
        trades_path = run_dir / "trades.parquet"
        if trades_path.exists():
            trades = self.load_trades(run_id)
            summary["trade_rows"] = int(len(trades))
            if "commission" in trades.columns and len(trades):
                summary["total_fees"] = float(trades["commission"].sum())
        for key in ("final_equity", "total_return", "max_drawdown", "trade_count", "total_fees"):
            if key in metrics and key not in summary:
                summary[key] = metrics[key]
        return summary
