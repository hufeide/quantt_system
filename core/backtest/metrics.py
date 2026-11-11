"""Performance metrics for backtest results."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

PERIODS_PER_YEAR = {
    "1m": 240 * 244,
    "5m": 48 * 244,
    "15m": 16 * 244,
    "30m": 8 * 244,
    "60m": 4 * 244,
    "1d": 244,
}


def round_trips(trades: pd.DataFrame) -> pd.DataFrame:
    """FIFO-match buys and sells into closed round trips with realised P&L."""
    if trades is None or trades.empty:
        return pd.DataFrame(columns=["symbol", "entry_ts", "exit_ts", "quantity", "entry_price", "exit_price", "pnl", "return"])
    df = trades.sort_values(["ts", "trade_id"]).reset_index(drop=True)
    lots: dict[str, list[list[float]]] = {}
    out: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        symbol = row.symbol
        qty = float(row.quantity)
        price = float(row.price)
        fee = float(getattr(row, "commission", 0.0) or 0.0)
        if row.side == "buy":
            lots.setdefault(symbol, []).append([qty, price + (fee / qty if qty else 0.0), row.ts])
        else:
            remaining = qty
            sell_price = price - (fee / qty if qty else 0.0)
            queue = lots.setdefault(symbol, [])
            while remaining > 1e-9 and queue:
                lot = queue[0]
                take = min(remaining, lot[0])
                pnl = (sell_price - lot[1]) * take
                out.append(
                    {
                        "symbol": symbol,
                        "entry_ts": lot[2],
                        "exit_ts": row.ts,
                        "quantity": take,
                        "entry_price": lot[1],
                        "exit_price": sell_price,
                        "pnl": pnl,
                        "return": (sell_price / lot[1] - 1.0) if lot[1] else 0.0,
                    }
                )
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    queue.pop(0)
    return pd.DataFrame(out)


def compute_metrics(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    initial_cash: float,
    frequency: str = "1m",
    risk_free_rate: float = 0.0,
) -> dict[str, Any]:
    if equity is None or equity.empty:
        return {"total_return": 0.0, "final_equity": initial_cash, "trade_count": 0, "error": "no equity data"}

    eq = equity.copy()
    eq["ts"] = pd.to_datetime(eq["ts"])
    eq = eq.sort_values("ts").drop_duplicates(subset=["ts"], keep="last")
    values = eq["equity"].astype(float).to_numpy()
    periods_per_year = PERIODS_PER_YEAR.get(frequency, 240 * 244)
    final_equity = float(values[-1])
    total_return = final_equity / initial_cash - 1.0

    rets = np.diff(values) / values[:-1] if len(values) > 1 else np.array([])
    rets = rets[np.isfinite(rets)]
    n = len(rets)
    ann_return: float | None
    if total_return <= -1.0:
        ann_return = -1.0
    elif n >= 2:
        exponent = periods_per_year / n
        magnitude = math.log1p(total_return) * exponent
        # A handful of observations cannot be annualised sensibly; clamp instead of overflowing.
        if magnitude > 50:
            ann_return = None
        elif magnitude < -50:
            ann_return = -1.0
        else:
            ann_return = math.expm1(magnitude)
    else:
        ann_return = None
    vol = float(rets.std(ddof=0) * math.sqrt(periods_per_year)) if n > 1 else 0.0
    rf_period = risk_free_rate / periods_per_year
    excess = rets - rf_period
    sharpe = float(excess.mean() / rets.std(ddof=0) * math.sqrt(periods_per_year)) if n > 1 and rets.std(ddof=0) > 0 else 0.0
    downside = rets[rets < 0]
    sortino = (
        float(excess.mean() / downside.std(ddof=0) * math.sqrt(periods_per_year))
        if len(downside) > 1 and downside.std(ddof=0) > 0
        else 0.0
    )

    peak = np.maximum.accumulate(values)
    dd = 1.0 - values / peak
    max_dd = float(dd.max()) if len(dd) else 0.0
    calmar = float(ann_return / max_dd) if (ann_return is not None and max_dd > 0) else 0.0

    trips = round_trips(trades)
    wins = trips[trips["pnl"] > 0] if not trips.empty else trips
    losses = trips[trips["pnl"] <= 0] if not trips.empty else trips
    win_rate = float(len(wins) / len(trips)) if len(trips) else 0.0
    gross_profit = float(wins["pnl"].sum()) if len(wins) else 0.0
    gross_loss = float(-losses["pnl"].sum()) if len(losses) else 0.0
    profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    trade_count = int(len(trades)) if trades is not None and not trades.empty else 0
    notional = float((trades["price"] * trades["quantity"]).sum()) if trade_count else 0.0
    fees = float(trades["commission"].sum()) if trade_count and "commission" in trades else 0.0
    avg_equity = float(values.mean()) if len(values) else initial_cash

    return {
        "initial_cash": float(initial_cash),
        "final_equity": round(final_equity, 6),
        "total_return": round(total_return, 8),
        "annualized_return": round(float(ann_return), 8) if ann_return is not None else None,
        "volatility": round(vol, 8),
        "sharpe": round(sharpe, 6),
        "sortino": round(sortino, 6),
        "max_drawdown": round(max_dd, 8),
        "calmar": round(calmar, 6),
        "trade_count": trade_count,
        "round_trips": int(len(trips)),
        "win_rate": round(win_rate, 6),
        "profit_factor": round(profit_factor, 6) if profit_factor != float("inf") else None,
        "avg_trade_pnl": round(float(trips["pnl"].mean()), 6) if len(trips) else 0.0,
        "turnover": round(notional / avg_equity, 6) if avg_equity > 0 else 0.0,
        "total_fees": round(fees, 6),
        "realized_pnl": round(float(trips["pnl"].sum()), 6) if len(trips) else 0.0,
        "avg_exposure": round(float(eq["exposure"].mean()), 6) if "exposure" in eq else 0.0,
        "periods": int(len(values)),
        "start": eq["ts"].iloc[0].isoformat() if len(eq) else None,
        "end": eq["ts"].iloc[-1].isoformat() if len(eq) else None,
    }


def metrics_fingerprint(metrics: dict[str, Any], trades: pd.DataFrame) -> str:
    """Stable hash used by golden-backtest regression tests."""
    import hashlib
    import json

    payload = {
        "metrics": {k: v for k, v in sorted(metrics.items()) if k not in {"start", "end", "run_id"}},
        "trades": [],
    }
    if trades is not None and not trades.empty:
        cols = [c for c in ("ts", "symbol", "side", "quantity", "price", "commission") if c in trades.columns]
        payload["trades"] = trades[cols].astype(str).values.tolist()
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
