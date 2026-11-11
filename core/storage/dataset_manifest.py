"""Versioned dataset manifests for normalized market data."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.storage.parquet import directory_checksum


@dataclass
class DatasetManifest:
    dataset_version: str
    frequency: str = "1m"
    source: str = "rqdata"
    symbols: list[str] = field(default_factory=list)
    start: str | None = None
    end: str | None = None
    row_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    checksum: str = ""
    schema: list[dict[str, Any]] = field(default_factory=list)
    timezone: str = "Asia/Shanghai"
    notes: str = ""
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "DatasetManifest":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def compute_checksum(self, data_dir: str | Path) -> str:
        self.checksum = directory_checksum(data_dir)
        return self.checksum
