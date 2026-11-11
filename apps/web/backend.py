"""Data access for the Streamlit UI.

Every read goes through the FastAPI service when it is reachable (QR_API_URL)
and falls back to calling core directly otherwise, so the UI is usable on a
single node with no API process running. Artifact files (equity/trades/...)
are always read from the local output directory: in the single-node deployment
the API and the UI share the same disk.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from core.backtest.runner import BacktestRunner, run_backtest
from core.config import BacktestConfig
from core.market.store import ParquetMarketDataStore
from core.replay.session import ReplaySession

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_API_PREFIX = "/api"
DEFAULT_DATA_ROOT = "data_files"
DEFAULT_OUTPUT_ROOT = "data_files/backtests"

ARTIFACT_FILES = {
    "equity": "equity.parquet",
    "trades": "trades.parquet",
    "positions": "positions.parquet",
    "orders": "orders.parquet",
}


def _env_first(*names: str, default: str = "") -> str:
    """Return the first non-empty environment variable among names."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from QUANT_* / QR_* environment variables.

    QUANT_API_BASE_URL is the deployment contract (K8s service DNS); QR_API_URL
    stays as the local-development alias.
    """

    api_url: str = DEFAULT_API_URL
    api_prefix: str = DEFAULT_API_PREFIX
    api_timeout: float = 2.0
    data_root: str = DEFAULT_DATA_ROOT
    output_root: str = DEFAULT_OUTPUT_ROOT

    @classmethod
    def from_env(cls) -> "Settings":
        data_root = _env_first("QUANT_DATA_ROOT", "QR_DATA_ROOT", default=DEFAULT_DATA_ROOT)
        output_root = _env_first("QUANT_OUTPUT_ROOT", "QR_OUTPUT_ROOT")
        if not output_root:
            # In a container the relative default would be lost: keep artifacts
            # next to the mounted dataset instead.
            output_root = (
                str(Path(data_root) / "backtests")
                if os.getenv("QUANT_DATA_ROOT")
                else DEFAULT_OUTPUT_ROOT
            )
        return cls(
            api_url=_env_first("QUANT_API_BASE_URL", "QR_API_URL", default=DEFAULT_API_URL).rstrip("/"),
            api_prefix=os.getenv("QR_API_PREFIX", DEFAULT_API_PREFIX),
            api_timeout=_env_float("QR_API_TIMEOUT", 2.0),
            data_root=data_root,
            output_root=output_root,
        )


# --------------------------------------------------------------------------- API client
class ApiClient:
    """Minimal JSON client for the FastAPI service; never raises."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error = ""
        self._online: bool | None = None
        self._base_hint: str | None = None

    def _candidates(self, path: str) -> list[str]:
        """Candidate URLs: configured prefix, then /api, then bare path.

        QR_API_PREFIX may be empty (K8s deployment, /health is unprefixed while
        the routers live under /api), so every combination is attempted once.
        """
        bases: list[str] = []
        if self._base_hint:
            bases.append(self._base_hint)
        for prefix in (self.settings.api_prefix, DEFAULT_API_PREFIX):
            if prefix:
                base = f"{self.settings.api_url}{prefix}"
                if base not in bases:
                    bases.append(base)
        if self.settings.api_url not in bases:
            bases.append(self.settings.api_url)
        return [f"{base}{path}" for base in bases]

    def _request(self, method: str, *paths: str, payload: Any = None) -> Any:
        """Try every candidate URL and return the first JSON body (None on failure)."""
        import httpx

        urls: list[str] = []
        for path in paths:
            for url in self._candidates(path):
                if url not in urls:
                    urls.append(url)
        for url in urls:
            try:
                response = httpx.request(method, url, json=payload, timeout=self.settings.api_timeout)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                for path in paths:
                    if url.endswith(path):
                        self._base_hint = url[: len(url) - len(path)]
                        break
                return response.json()
            except Exception as exc:  # connection refused, timeout, bad json ...
                self.last_error = f"{method} {url}: {exc}"
        return None

    def health(self) -> bool:
        if self._online is None:
            self._online = self._request("GET", "/health") is not None
        return self._online

    def list_runs(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/backtests")
        return _items(data)

    def submit_backtest(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("POST", "/backtests", payload)
        return data if isinstance(data, dict) else None

    def list_datasets(self) -> list[dict[str, Any]]:
        return _items(self._request("GET", "/data/datasets", "/datasets"))


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("items", "results", "runs", "datasets"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


# --------------------------------------------------------------------------- local artifacts
def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_parquet(path: Path) -> pd.DataFrame:
    try:
        if path.exists():
            return pd.read_parquet(path)
    except Exception:
        pass
    return pd.DataFrame()


def list_runs(output_root: str | Path) -> list[dict[str, Any]]:
    """Scan <output_root>/run_id=*/metrics.json, newest first."""
    root = Path(output_root)
    if not root.exists():
        return []
    runs: list[dict[str, Any]] = []
    for run_dir in sorted(root.glob("run_id=*"), reverse=True):
        if not run_dir.is_dir():
            continue
        metrics = _read_json(run_dir / "metrics.json")
        summary = _read_json(run_dir / "summary.json")
        manifest = _read_json(run_dir / "manifest.json")
        runs.append(
            {
                "run_id": run_dir.name.split("=", 1)[-1],
                "path": str(run_dir),
                "metrics": metrics or (summary.get("metrics") or {}),
                "summary": summary,
                "manifest": manifest,
                "strategy": metrics.get("strategy") or manifest.get("strategy") or summary.get("strategy", ""),
                "dataset_version": metrics.get("dataset_version") or manifest.get("dataset_version", ""),
                "created_at": manifest.get("created_at") or metrics.get("start"),
            }
        )
    return runs


def load_run(output_root: str | Path, run_id: str) -> dict[str, Any]:
    """Load one run's metrics, manifest and tabular artifacts from disk."""
    run_dir = Path(output_root) / f"run_id={run_id}"
    metrics = _read_json(run_dir / "metrics.json")
    manifest = _read_json(run_dir / "manifest.json")
    detail: dict[str, Any] = {
        "run_id": run_id,
        "path": str(run_dir),
        "exists": run_dir.exists(),
        "metrics": metrics,
        "manifest": manifest,
        "summary": _read_json(run_dir / "summary.json"),
        "checkpoints": sorted(p.name for p in (run_dir / "checkpoints").glob("ckpt_*.json")),
    }
    for key, filename in ARTIFACT_FILES.items():
        detail[key] = _read_parquet(run_dir / filename)
    return detail


def load_risk_rejections(
    output_root: str | Path, run_id: str, limit: int = 200
) -> pd.DataFrame:
    """Risk-rejected orders recorded in events.parquet (empty if not recorded).

    A run with zero trades is usually caused by the risk limits, but the reason
    never shows up in metrics.json - it only exists as RiskRejected events.
    """
    columns = ["simulated_time", "symbol", "order_id", "rule", "reason"]
    path = Path(output_root) / f"run_id={run_id}" / "events.parquet"
    if not path.exists():
        return pd.DataFrame(columns=columns)
    try:
        frame = pd.read_parquet(
            path,
            columns=["simulated_time", "symbol", "order_id", "payload"],
            filters=[("event_type", "==", "RiskRejected")],
        )
    except Exception:
        return pd.DataFrame(columns=columns)
    if frame.empty:
        return pd.DataFrame(columns=columns)

    def _rule(raw: Any) -> str:
        try:
            return str(json.loads(raw).get("rule", "")) if isinstance(raw, str) and raw else ""
        except (TypeError, ValueError):
            return ""

    def _reason(raw: Any) -> str:
        try:
            return str(json.loads(raw).get("reason", "")) if isinstance(raw, str) and raw else ""
        except (TypeError, ValueError):
            return ""

    frame = frame.assign(rule=frame["payload"].map(_rule), reason=frame["payload"].map(_reason))
    return frame[columns].head(limit)


def recent_runs(settings: Settings, client: ApiClient | None = None) -> tuple[list[dict[str, Any]], str]:
    """Local runs merged with API-known runs. Returns (runs, source)."""
    local = list_runs(settings.output_root)
    if client is None or not client.health():
        return local, "local"
    api_runs = client.list_runs()
    known = {run["run_id"] for run in local}
    for item in api_runs:
        run_id = item.get("run_id")
        if not run_id or run_id in known:
            continue
        local.append(
            {
                "run_id": run_id,
                "path": "",
                "metrics": item.get("metrics") or {},
                "summary": {},
                "manifest": item.get("manifest") or {},
                "strategy": item.get("strategy", ""),
                "dataset_version": item.get("dataset_version", ""),
                "created_at": item.get("created_at"),
                "status": item.get("status", ""),
            }
        )
    return local, "api+local"


# --------------------------------------------------------------------------- datasets
def list_datasets(data_root: str | Path, frequency: str = "1m") -> list[dict[str, Any]]:
    """Enumerate <data_root>/normalized/dataset=* versions."""
    root = Path(data_root) / "normalized"
    if not root.exists():
        return []
    datasets: list[dict[str, Any]] = []
    for dataset_dir in sorted(root.glob("dataset=*")):
        if not dataset_dir.is_dir():
            continue
        version = dataset_dir.name.split("=", 1)[-1]
        manifest = _read_json(dataset_dir / "manifest.json")
        frequencies = sorted(
            path.name.split("=", 1)[-1] for path in dataset_dir.glob("frequency=*") if path.is_dir()
        )
        symbols = list(manifest.get("symbols") or [])
        if not symbols:
            symbols = dataset_symbols(data_root, version, frequencies[0] if frequencies else frequency)
        datasets.append(
            {
                "version": version,
                "frequencies": frequencies or [frequency],
                "frequency": frequencies[0] if frequencies else frequency,
                "manifest": manifest,
                "symbols": symbols,
                "rows": int(manifest.get("row_count") or 0),
                "start": manifest.get("start"),
                "end": manifest.get("end"),
            }
        )
    return datasets


def _store(data_root: str | Path, version: str, frequency: str) -> ParquetMarketDataStore:
    return ParquetMarketDataStore(data_root, version, frequency)


def dataset_symbols(data_root: str | Path, version: str, frequency: str = "1m") -> list[str]:
    try:
        return list(_store(data_root, version, frequency).symbols())
    except Exception:
        return []


def dataset_frame(
    data_root: str | Path,
    version: str,
    frequency: str = "1m",
    symbols: Sequence[str] | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    try:
        frame = _store(data_root, version, frequency).load_frame(symbols=symbols or None)
    except Exception:
        return pd.DataFrame()
    return frame.head(limit) if limit else frame


def dataset_range(
    data_root: str | Path, version: str, frequency: str = "1m"
) -> tuple[date | None, date | None]:
    """First/last trading date of a dataset (manifest first, parquet fallback)."""
    manifest_path = Path(data_root) / "normalized" / f"dataset={version}" / "manifest.json"
    manifest = _read_json(manifest_path)
    try:
        if manifest.get("start") and manifest.get("end"):
            return (
                pd.Timestamp(manifest["start"]).date(),
                pd.Timestamp(manifest["end"]).date(),
            )
    except Exception:
        pass
    frame = dataset_frame(data_root, version, frequency, limit=None)
    if frame.empty:
        return None, None
    ts = pd.to_datetime(frame["ts"])
    return ts.min().date(), ts.max().date()


def dataset_stats(data_root: str | Path, version: str, frequency: str = "1m") -> dict[str, Any]:
    frame = dataset_frame(data_root, version, frequency)
    stats: dict[str, Any] = {
        "version": version,
        "frequency": frequency,
        "exists": not frame.empty,
        "rows": int(len(frame)),
        "symbols": [],
        "start": None,
        "end": None,
        "duplicates": 0,
        "bars_per_symbol": {},
    }
    if frame.empty:
        return stats
    frame = frame.copy()
    frame["ts"] = pd.to_datetime(frame["ts"])
    stats["symbols"] = sorted(frame["symbol"].unique().tolist())
    stats["start"] = frame["ts"].min().isoformat()
    stats["end"] = frame["ts"].max().isoformat()
    stats["duplicates"] = int(frame.duplicated(subset=["symbol", "ts"]).sum())
    stats["bars_per_symbol"] = frame.groupby("symbol").size().astype(int).to_dict()
    return stats


def validate_dataset(data_root: str | Path, version: str, frequency: str = "1m") -> dict[str, Any]:
    """Boundary-level dataset checks shown on the data page."""
    frame = dataset_frame(data_root, version, frequency)
    report: dict[str, Any] = {"version": version, "ok": False, "errors": [], "warnings": [], "rows": int(len(frame))}
    if frame.empty:
        report["errors"].append("数据集为空或不存在")
        return report

    frame = frame.copy()
    frame["ts"] = pd.to_datetime(frame["ts"])
    required = ["symbol", "ts", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        report["errors"].append(f"缺少字段: {missing}")
        return report

    nulls = frame[required].isna().sum()
    if int(nulls.sum()):
        report["errors"].append(f"存在空值: {nulls[nulls > 0].to_dict()}")
    if bool((frame[["open", "high", "low", "close"]] <= 0).any().any()):
        report["errors"].append("存在非正价格")
    if bool((frame["high"] < frame["low"]).any()):
        report["errors"].append("存在 high < low 的记录")
    if bool((frame["volume"] < 0).any()):
        report["errors"].append("存在负成交量")
    duplicates = int(frame.duplicated(subset=["symbol", "ts"]).sum())
    if duplicates:
        report["errors"].append(f"存在 {duplicates} 条重复 (symbol, ts)")

    manifest_path = Path(data_root) / "normalized" / f"dataset={version}" / "manifest.json"
    manifest = _read_json(manifest_path)
    if not manifest:
        report["warnings"].append("缺少 manifest.json")
    elif manifest.get("row_count") and int(manifest["row_count"]) != int(len(frame)):
        report["warnings"].append(
            f"manifest row_count={manifest['row_count']} 与实际行数 {len(frame)} 不一致"
        )
    report["ok"] = not report["errors"]
    return report


# --------------------------------------------------------------------------- backtests
def run_backtest_local(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Run a backtest in-process (used when the API is offline)."""
    data = dict(payload)
    data.setdefault("data_root", settings.data_root)
    data.setdefault("output_root", settings.output_root)
    config = BacktestConfig.from_dict(data)
    result = run_backtest(config)
    return result.to_dict()


def submit_backtest(settings: Settings, payload: dict[str, Any], client: ApiClient | None = None) -> dict[str, Any]:
    """Submit through the API when possible, otherwise execute locally."""
    client = client or ApiClient(settings)
    if client.health():
        response = client.submit_backtest(payload)
        if response is not None:
            return {"source": "api", **response}
    result = run_backtest_local(payload, settings)
    return {"source": "local", **result}


def build_replay_session(
    settings: Settings,
    dataset_version: str,
    symbols: Sequence[str],
    strategy: str,
    params: dict[str, Any] | None = None,
    start: date | str | None = None,
    end: date | str | None = None,
    frequency: str = "1m",
    initial_cash: float = 1_000_000.0,
    max_events: int = 500,
) -> tuple[ReplaySession, list[dict[str, Any]]]:
    """Build an interactive replay session backed by a BacktestRunner."""
    config = BacktestConfig(
        strategy=strategy,
        strategy_params=dict(params or {}),
        symbols=list(symbols),
        start=start,
        end=end,
        initial_cash=initial_cash,
        frequency=frequency,
        dataset_version=dataset_version,
        data_root=settings.data_root,
        output_root=settings.output_root,
        checkpoint_every=240,
    )
    runner = BacktestRunner(config)
    runner.build()

    events: list[dict[str, Any]] = []

    def _collect(event) -> None:
        if len(events) >= max_events:
            return
        record = event.to_record()
        events.append(
            {
                "seq": record.get("seq"),
                "time": str(record.get("simulated_time")),
                "type": record.get("event_type"),
                "symbol": record.get("symbol") or "",
            }
        )

    runner.engine.on_event(_collect)
    session = ReplaySession(runner.engine, runner.checkpoints, runner.run_id)
    return session, events


__all__ = [
    "ApiClient",
    "Settings",
    "build_replay_session",
    "dataset_frame",
    "dataset_range",
    "dataset_stats",
    "dataset_symbols",
    "list_datasets",
    "list_runs",
    "load_risk_rejections",
    "load_run",
    "recent_runs",
    "run_backtest_local",
    "submit_backtest",
    "validate_dataset",
]
