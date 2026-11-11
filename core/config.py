"""Typed configuration objects for the quant replay core.

The core is deliberately free of FastAPI / Celery / MongoDB imports; configuration
is expressed with plain dataclasses and can be loaded from YAML or JSON.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

try:  # optional dependency, but part of the standard install
    import yaml
except Exception:  # pragma: no cover - yaml is a hard dependency in pyproject
    yaml = None


FREQUENCY_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "60m": 3600,
    "1d": 86400,
}


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


@dataclass
class FeeConfig:
    """A-share style fee model (configurable for other markets)."""

    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001

    def compute(self, side: str, price: float, quantity: float) -> dict[str, float]:
        notional = abs(price * quantity)
        commission = max(notional * self.commission_rate, self.min_commission) if notional > 0 else 0.0
        stamp_duty = notional * self.stamp_duty_rate if side == "sell" else 0.0
        transfer_fee = notional * self.transfer_fee_rate
        total = commission + stamp_duty + transfer_fee
        return {
            "commission": round(commission, 6),
            "stamp_duty": round(stamp_duty, 6),
            "transfer_fee": round(transfer_fee, 6),
            "total": round(total, 6),
        }


@dataclass
class MatchingConfig:
    """Rules that decide whether and at what price an order is filled."""

    slippage_bps: float = 1.0
    participation_rate: float = 0.1  # max share of a bar's volume we may take
    lot_size: int = 100
    price_limit_pct: float = 0.10  # A-share daily limit; 0 disables the check
    allow_partial_fill: bool = True
    t_plus_1: bool = True
    fill_on_same_bar: bool = False  # False => fill on the next bar after submission
    allow_short: bool = False
    max_price_deviation_pct: float = 0.02  # reject limit prices far from the market


@dataclass
class RiskConfig:
    max_position_weight: float = 0.30
    max_order_value: float = 200_000.0
    max_total_exposure: float = 1.0
    max_drawdown: float = 0.25
    min_cash_reserve: float = 0.0
    max_orders_per_day: int = 200
    cash_buffer: float = 0.01  # extra cash held back for slippage / fees
    banned_symbols: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.banned_symbols = list(self.banned_symbols or [])


@dataclass
class BacktestConfig:
    strategy: str = "sma_cross"
    strategy_params: dict[str, Any] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    initial_cash: float = 1_000_000.0
    frequency: str = "1m"
    dataset_version: str = "v001"
    data_root: str = "data_files"
    output_root: str = "data_files/backtests"
    run_id: str | None = None
    seed: int = 42
    benchmark: str | None = None
    checkpoint_every: int = 5_000  # events between automatic checkpoints
    record_events: bool = True
    warmup_bars: int = 30
    fees: FeeConfig = field(default_factory=FeeConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.start = _as_date(self.start)
        self.end = _as_date(self.end)
        self.symbols = [str(s).upper() for s in (self.symbols or [])]
        self.frequency = str(self.frequency)
        if isinstance(self.fees, Mapping):
            self.fees = FeeConfig(**self.fees)
        if isinstance(self.matching, Mapping):
            self.matching = MatchingConfig(**self.matching)
        if isinstance(self.risk, Mapping):
            self.risk = RiskConfig(**self.risk)
        if self.frequency not in FREQUENCY_SECONDS:
            raise ValueError(f"unsupported frequency: {self.frequency}")

    @property
    def bar_seconds(self) -> int:
        return FREQUENCY_SECONDS[self.frequency]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("start", "end"):
            if data.get(key) is not None:
                data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BacktestConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in dict(data).items() if k in known}
        return cls(**payload)

    @classmethod
    def load(cls, path: str | Path) -> "BacktestConfig":
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:  # pragma: no cover
                raise RuntimeError("pyyaml is required to load YAML configs")
            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text)
        return cls.from_dict(data)

    def dump(self, path: str | Path) -> None:
        path = Path(path)
        payload = self.to_dict()
        if path.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:  # pragma: no cover
                raise RuntimeError("pyyaml is required to dump YAML configs")
            path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
