"""Vectorised technical indicators computed on completed-bar arrays only."""
from __future__ import annotations

import numpy as np


def sma(values: np.ndarray, window: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if window <= 0 or len(values) < window:
        return np.array([], dtype=float)
    kernel = np.ones(window) / window
    return np.convolve(values, kernel, mode="valid")


def ema(values: np.ndarray, window: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if window <= 0 or len(values) == 0:
        return np.array([], dtype=float)
    alpha = 2.0 / (window + 1.0)
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(values: np.ndarray, window: int = 14) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if len(values) <= window:
        return np.array([], dtype=float)
    diff = np.diff(values)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    out = np.empty(len(diff) - window + 1)
    avg_gain = gains[:window].mean()
    avg_loss = losses[:window].mean()
    for i in range(window, len(diff) + 1):
        if i > window:
            avg_gain = (avg_gain * (window - 1) + gains[i - 1]) / window
            avg_loss = (avg_loss * (window - 1) + losses[i - 1]) / window
        rs = avg_gain / avg_loss if avg_loss > 0 else np.inf
        out[i - window] = 100 - 100 / (1 + rs)
    return out


def zscore(values: np.ndarray, window: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if len(values) < window or window <= 1:
        return np.array([], dtype=float)
    out = np.empty(len(values) - window + 1)
    for i in range(window, len(values) + 1):
        chunk = values[i - window : i]
        std = chunk.std(ddof=0)
        out[i - window] = (chunk[-1] - chunk.mean()) / std if std > 0 else 0.0
    return out


def momentum(values: np.ndarray, window: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if len(values) <= window:
        return np.array([], dtype=float)
    return values[window:] / values[:-window] - 1.0


def rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if len(values) < window:
        return np.array([], dtype=float)
    out = np.empty(len(values) - window + 1)
    for i in range(window, len(values) + 1):
        out[i - window] = values[i - window : i].std(ddof=0)
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int = 14) -> np.ndarray:
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    if len(close) < 2:
        return np.array([], dtype=float)
    prev_close = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return sma(tr, window)


def bollinger(values: np.ndarray, window: int = 20, num_std: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mid = sma(values, window)
    std = rolling_std(values, window)
    n = min(len(mid), len(std))
    if n == 0:
        return np.array([]), np.array([]), np.array([])
    mid, std = mid[-n:], std[-n:]
    return mid - num_std * std, mid, mid + num_std * std


def macd(values: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    values = np.asarray(values, dtype=float)
    if len(values) < slow:
        return np.array([]), np.array([]), np.array([])
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    line = fast_ema - slow_ema
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist
