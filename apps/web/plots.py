"""Plotly figures for the Streamlit pages.

Plotly renders from the JavaScript bundled with the plotly Python package, so
the pages never reach out to a CDN and work on an air-gapped node.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def equity_figure(equity: pd.DataFrame) -> go.Figure:
    """Equity curve with a drawdown panel underneath (shared x axis)."""
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.68, 0.32],
        vertical_spacing=0.08,
        subplot_titles=("权益曲线", "回撤"),
    )
    if equity is None or equity.empty or "equity" not in equity.columns:
        fig.update_layout(height=420, margin=dict(l=10, r=10, t=40, b=10))
        return fig

    frame = equity.copy()
    frame["ts"] = pd.to_datetime(frame["ts"])
    frame = frame.sort_values("ts")

    fig.add_trace(
        go.Scatter(
            x=frame["ts"],
            y=frame["equity"].astype(float),
            name="权益",
            mode="lines",
            line=dict(color="#1f77b4", width=1.6),
        ),
        row=1,
        col=1,
    )

    if "drawdown" in frame.columns:
        drawdown = frame["drawdown"].astype(float)
    else:
        values = frame["equity"].astype(float)
        drawdown = 1.0 - values / values.cummax()

    fig.add_trace(
        go.Scatter(
            x=frame["ts"],
            y=-drawdown,
            name="回撤",
            mode="lines",
            fill="tozeroy",
            line=dict(color="#d62728", width=1.0),
        ),
        row=2,
        col=1,
    )

    fig.update_yaxes(title_text="权益", row=1, col=1)
    fig.update_yaxes(title_text="回撤", tickformat=".1%", row=2, col=1)
    fig.update_layout(
        height=520,
        margin=dict(l=10, r=10, t=40, b=10),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
    )
    return fig


def exposure_figure(equity: pd.DataFrame) -> go.Figure:
    """Position exposure over time (0..1)."""
    fig = go.Figure()
    if equity is None or equity.empty or "exposure" not in equity.columns:
        fig.update_layout(height=240, margin=dict(l=10, r=10, t=20, b=10))
        return fig
    frame = equity.copy()
    frame["ts"] = pd.to_datetime(frame["ts"])
    fig.add_trace(
        go.Scatter(
            x=frame["ts"],
            y=frame["exposure"].astype(float),
            name="仓位",
            mode="lines",
            fill="tozeroy",
            line=dict(color="#2ca02c", width=1.2),
        )
    )
    fig.update_yaxes(tickformat=".0%")
    fig.update_layout(height=240, margin=dict(l=10, r=10, t=20, b=10), hovermode="x unified")
    return fig
