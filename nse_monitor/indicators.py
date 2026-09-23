"""Technical indicators computed on daily closing prices.

All indicators here are *causal*: the value at bar ``i`` depends only on bars
``0..i``. That property is what lets the engine compute each series once and
read the value for any day without look-ahead bias (tests assert it).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI: seed with the simple mean of the first ``period`` moves,
    then smooth with ``avg = (prev * (period - 1) + current) / period``."""
    values = close.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) <= period:
        return pd.Series(out, index=close.index)

    delta = np.diff(values)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return pd.Series(out, index=close.index)


def _rsi(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def sma(close: pd.Series, period: int = 50) -> pd.Series:
    return close.rolling(window=period, min_periods=period).mean()
