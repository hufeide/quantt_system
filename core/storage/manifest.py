"""Run manifests: everything required to reproduce a backtest exactly."""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import BacktestConfig


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def code_hash(source: str | None) -> str:
    if not source:
        return ""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in ("numpy", "pandas", "pyarrow", "duckdb"):
        try:
            mod = __import__(name)
            versions[name] = getattr(mod, "__version__", "unknown")
        except Exception:  # pragma: no cover
            versions[name] = "missing"
    return versions


@dataclass
class RunManifest:
    run_id: str
    created_at: str = field(default_factory=_utcnow)
    dataset_version: str = ""
    dataset_checksum: str = ""
    strategy: str = ""
    strategy_params: dict[str, Any] = field(default_factory=dict)
    strategy_code_hash: str = ""
    factor_version: str = ""
    symbols: list[str] = field(default_factory=list)
    start: str | None = None
    end: str | None = None
    frequency: str = "1m"
    initial_cash: float = 0.0
    fees: dict[str, Any] = field(default_factory=dict)
    matching: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    calendar: dict[str, Any] = field(default_factory=dict)
    seed: int = 0
    environment: dict[str, Any] = field(default_factory=dict)
    code_version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        config: BacktestConfig,
        run_id: str,
        strategy_source: str | None = None,
        dataset_checksum: str = "",
        calendar: dict[str, Any] | None = None,
        code_version: str = "",
        factor_version: str = "",
    ) -> "RunManifest":
        return cls(
            run_id=run_id,
            dataset_version=config.dataset_version,
            dataset_checksum=dataset_checksum,
            strategy=config.strategy,
            strategy_params=dict(config.strategy_params),
            strategy_code_hash=code_hash(strategy_source),
            factor_version=factor_version,
            symbols=list(config.symbols),
            start=config.start.isoformat() if config.start else None,
            end=config.end.isoformat() if config.end else None,
            frequency=config.frequency,
            initial_cash=config.initial_cash,
            fees=asdict(config.fees),
            matching=asdict(config.matching),
            risk=asdict(config.risk),
            calendar=calendar or {},
            seed=config.seed,
            environment={
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "packages": package_versions(),
            },
            code_version=code_version,
            extra=dict(config.extra),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "RunManifest":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def fingerprint(self) -> str:
        """Hash of every reproducibility-relevant field (run_id excluded)."""
        data = self.to_dict()
        data.pop("run_id", None)
        data.pop("created_at", None)
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
