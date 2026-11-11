"""Built-in reference strategies."""
from __future__ import annotations

import numpy as np

from core.market.models import Bar
from core.strategy.base import Strategy, StrategyContext
from core.strategy.registry import register_strategy


@register_strategy("buy_and_hold")
class BuyAndHold(Strategy):
    description = "Buy once at the first tradable bar and hold until the end."
    default_params = {"weight": 0.95}
    param_schema = {"weight": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.95}}

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        if ctx.get_quantity(bar.symbol) > 0:
            return
        ctx.signal(bar.symbol, 1.0, target_weight=self.params["weight"], reason="initial_entry")
        ctx.order_target_weight(bar.symbol, self.params["weight"], tag="entry")


@register_strategy("sma_cross")
class SmaCross(Strategy):
    description = "Fast/slow SMA crossover with a fixed target weight per position."
    default_params = {"fast": 5, "slow": 20, "weight": 0.9, "allow_short": False}
    param_schema = {
        "fast": {"type": "integer", "min": 1, "max": 120, "default": 5},
        "slow": {"type": "integer", "min": 2, "max": 240, "default": 20},
        "weight": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.9},
    }

    def on_start(self, ctx: StrategyContext) -> None:
        super().on_start(ctx)
        self.state.setdefault("prev_diff", {})

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        fast = int(self.params["fast"])
        slow = int(self.params["slow"])
        buf = ctx.bars(bar.symbol)
        if len(buf) < slow + 1:
            return
        fast_now = buf.sma(fast)
        slow_now = buf.sma(slow)
        if fast_now is None or slow_now is None:
            return
        diff = fast_now - slow_now
        self.state.setdefault("prev_diff", {})
        prev = self.state["prev_diff"].get(bar.symbol)
        self.state["prev_diff"][bar.symbol] = diff
        if prev is None:
            return
        if prev <= 0 < diff:
            ctx.signal(bar.symbol, 1.0, target_weight=self.params["weight"], reason="golden_cross")
            ctx.order_target_weight(bar.symbol, self.params["weight"], tag="golden_cross")
        elif prev >= 0 > diff:
            ctx.signal(bar.symbol, -1.0, target_weight=0.0, reason="death_cross")
            ctx.order_target_weight(bar.symbol, 0.0, tag="death_cross")


@register_strategy("momentum")
class Momentum(Strategy):
    description = "Enter when the N-bar return exceeds a threshold, exit when it turns negative."
    default_params = {"lookback": 30, "threshold": 0.01, "weight": 0.8}
    param_schema = {
        "lookback": {"type": "integer", "min": 2, "max": 240, "default": 30},
        "threshold": {"type": "number", "min": 0.0, "max": 0.5, "default": 0.01},
    }

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        lookback = int(self.params["lookback"])
        buf = ctx.bars(bar.symbol)
        if len(buf) <= lookback:
            return
        closes = buf.closes
        ret = closes[-1] / closes[-lookback - 1] - 1.0
        holding = ctx.get_quantity(bar.symbol) > 0
        if not holding and ret > float(self.params["threshold"]):
            ctx.signal(bar.symbol, 1.0, target_weight=self.params["weight"], reason=f"momentum {ret:.3f}")
            ctx.order_target_weight(bar.symbol, self.params["weight"], tag="momentum_entry")
        elif holding and ret < 0:
            ctx.signal(bar.symbol, -1.0, target_weight=0.0, reason=f"momentum_exit {ret:.3f}")
            ctx.order_target_weight(bar.symbol, 0.0, tag="momentum_exit")


@register_strategy("mean_reversion")
class MeanReversion(Strategy):
    description = "Bollinger-style reversion: buy oversold dips, exit at the mean."
    default_params = {"window": 20, "entry_z": 1.5, "exit_z": 0.2, "weight": 0.7}
    param_schema = {
        "window": {"type": "integer", "min": 5, "max": 240, "default": 20},
        "entry_z": {"type": "number", "min": 0.5, "max": 5.0, "default": 1.5},
        "exit_z": {"type": "number", "min": 0.0, "max": 2.0, "default": 0.2},
    }

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        window = int(self.params["window"])
        buf = ctx.bars(bar.symbol)
        if len(buf) < window:
            return
        closes = buf.closes[-window:]
        mean = float(closes.mean())
        std = float(closes.std(ddof=0))
        if std <= 0:
            return
        z = (closes[-1] - mean) / std
        holding = ctx.get_quantity(bar.symbol) > 0
        if not holding and z <= -float(self.params["entry_z"]):
            ctx.signal(bar.symbol, 1.0, target_weight=self.params["weight"], reason=f"oversold z={z:.2f}")
            ctx.order_target_weight(bar.symbol, self.params["weight"], tag="mr_entry")
        elif holding and z >= -float(self.params["exit_z"]):
            ctx.signal(bar.symbol, -1.0, target_weight=0.0, reason=f"reverted z={z:.2f}")
            ctx.order_target_weight(bar.symbol, 0.0, tag="mr_exit")


@register_strategy("factor_score")
class FactorScore(Strategy):
    description = "Cross-sectional composite factor score (momentum + low volatility)."
    default_params = {"lookback": 60, "top_n": 1, "weight": 0.3, "vol_penalty": 0.5}
    param_schema = {
        "lookback": {"type": "integer", "min": 5, "max": 240, "default": 60},
        "top_n": {"type": "integer", "min": 1, "max": 20, "default": 1},
    }

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        lookback = int(self.params["lookback"])
        symbols = [s for s in ctx.history.symbols if len(ctx.bars(s)) > lookback]
        if len(symbols) < 1:
            return
        scores: dict[str, float] = {}
        for symbol in symbols:
            closes = ctx.bars(symbol).closes
            mom = closes[-1] / closes[-lookback] - 1.0
            vol = float(np.std(np.diff(closes[-lookback:]) / closes[-lookback:-1]))
            scores[symbol] = float(mom - float(self.params["vol_penalty"]) * vol)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top = {s for s, _ in ranked[: int(self.params["top_n"])]}
        for symbol, score in ranked:
            holding = ctx.get_quantity(symbol) > 0
            if symbol in top and not holding:
                ctx.signal(symbol, 1.0, target_weight=self.params["weight"], reason=f"factor {score:.4f}")
                ctx.order_target_weight(symbol, self.params["weight"], tag="factor_entry")
            elif symbol not in top and holding:
                ctx.signal(symbol, -1.0, target_weight=0.0, reason="factor_exit")
                ctx.order_target_weight(symbol, 0.0, tag="factor_exit")
