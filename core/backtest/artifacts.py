"""Writes every backtest artifact described by the plan (manifest + parquet + metrics)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from core.storage.manifest import RunManifest
from core.storage.parquet import EVENT_SCHEMA, ensure_dir, write_dataframe


class ArtifactWriter:
    def __init__(self, root: str | Path, run_id: str) -> None:
        self.root = Path(root) / f"run_id={run_id}"
        ensure_dir(self.root)
        ensure_dir(self.root / "checkpoints")

    # -- paths ---------------------------------------------------------------
    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def events_path(self) -> Path:
        return self.root / "events.parquet"

    @property
    def orders_path(self) -> Path:
        return self.root / "orders.parquet"

    @property
    def trades_path(self) -> Path:
        return self.root / "trades.parquet"

    @property
    def positions_path(self) -> Path:
        return self.root / "positions.parquet"

    @property
    def equity_path(self) -> Path:
        return self.root / "equity.parquet"

    @property
    def metrics_path(self) -> Path:
        return self.root / "metrics.json"

    @property
    def checkpoints_dir(self) -> Path:
        return self.root / "checkpoints"

    # -- writers -------------------------------------------------------------
    def write_manifest(self, manifest: RunManifest) -> Path:
        return manifest.save(self.manifest_path)

    def write_events(self, events: Sequence[dict[str, Any]], run_id: str) -> Path:
        if not events:
            df = pd.DataFrame(columns=[f.name for f in EVENT_SCHEMA])
        else:
            df = pd.DataFrame(
                [
                    {
                        "seq": int(e.get("seq", -1)),
                        "run_id": run_id,
                        "simulated_time": pd.Timestamp(e.get("simulated_time")),
                        "event_type": e.get("event_type"),
                        "symbol": e.get("symbol"),
                        "order_id": e.get("order_id"),
                        "payload": json.dumps(e.get("payload", {}), default=str),
                    }
                    for e in events
                ]
            )
        return write_dataframe(df, self.events_path, schema=EVENT_SCHEMA)

    def write_orders(self, orders: Sequence[dict[str, Any]]) -> Path:
        df = pd.DataFrame(list(orders)) if orders else pd.DataFrame(
            columns=[
                "order_id",
                "symbol",
                "side",
                "quantity",
                "order_type",
                "limit_price",
                "created_at",
                "status",
                "filled_quantity",
                "avg_fill_price",
                "commission",
                "strategy_id",
                "tag",
                "reject_reason",
            ]
        )
        if not df.empty and "created_at" in df:
            df["created_at"] = pd.to_datetime(df["created_at"])
        return write_dataframe(df, self.orders_path)

    def write_trades(self, trades: Sequence[dict[str, Any]]) -> Path:
        df = pd.DataFrame(list(trades)) if trades else pd.DataFrame(
            columns=[
                "trade_id",
                "order_id",
                "symbol",
                "side",
                "quantity",
                "price",
                "commission",
                "slippage_cost",
                "ts",
                "strategy_id",
                "tag",
            ]
        )
        if not df.empty and "ts" in df:
            df["ts"] = pd.to_datetime(df["ts"])
        return write_dataframe(df, self.trades_path)

    def write_positions(self, rows: Sequence[dict[str, Any]]) -> Path:
        df = pd.DataFrame(list(rows)) if rows else pd.DataFrame(
            columns=[
                "ts",
                "symbol",
                "quantity",
                "available",
                "avg_cost",
                "last_price",
                "market_value",
                "unrealized_pnl",
                "realized_pnl",
            ]
        )
        if not df.empty and "ts" in df:
            df["ts"] = pd.to_datetime(df["ts"])
        return write_dataframe(df, self.positions_path)

    def write_equity(self, rows: Sequence[dict[str, Any]]) -> Path:
        df = pd.DataFrame(list(rows)) if rows else pd.DataFrame(
            columns=["ts", "cash", "market_value", "equity", "drawdown", "exposure", "fees_paid", "realized_pnl"]
        )
        if not df.empty and "ts" in df:
            df["ts"] = pd.to_datetime(df["ts"])
        return write_dataframe(df, self.equity_path)

    def write_metrics(self, metrics: dict[str, Any]) -> Path:
        self.metrics_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return self.metrics_path

    def write_extra(self, name: str, payload: Any) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return path

    def list_files(self) -> dict[str, str]:
        return {
            str(p.relative_to(self.root)): str(p)
            for p in sorted(self.root.rglob("*"))
            if p.is_file()
        }
