"""Repair price discontinuities the data provider failed to adjust.

NSE price bands cap a normal session's move at 20% (2/5/10/20% for most stocks and
ETFs). A one-day move far beyond that is therefore a corporate action -- a split,
bonus, demerger, relisting -- or a bad print, never a real trading move. Yahoo
frequently leaves such events unadjusted (e.g. ETF unit splits 1:10 or 1:100), and a
single fake -90% "loss" holds Wilder's RSI near zero for months and poisons SMA 50.

Every such jump is removed by rescaling all earlier closes:
* ratio within ``snap_tol`` of a standard split/bonus factor (1:2, 1:10, 2:3...) ->
  that exact factor, so the day's genuine move survives;
* anything else (demerger, relisting, bad print) -> the observed ratio, i.e. that
  day's return is treated as 0. A one-day spike that reverts is neutralised both ways.

Rescaling earlier bars by a constant leaves RSI unchanged and preserves close-vs-SMA
comparisons at every earlier date, so this introduces no look-ahead.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

_SPLITS = [2, 3, 4, 5, 8, 10, 20, 25, 50, 100]
# price ratio new/old for splits (1/k), bonus issues (a:b bonus -> b/(a+b)) and consolidations (k)
FACTORS = sorted({1 / k for k in _SPLITS} | {2 / 3, 3 / 4, 3 / 5, 2 / 5} | {float(k) for k in _SPLITS[:6]})


def snap_factor(ratio: float, tol: float) -> float | None:
    best = min(FACTORS, key=lambda f: abs(ratio / f - 1))
    return best if abs(ratio / best - 1) <= tol else None


def adjust_discontinuities(close: pd.Series, max_move: float = 0.35, snap_tol: float = 0.03
                           ) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Returns (adjusted closes, scale factor per bar, events).

    ``adjusted[i] = close[i] * scale[i]``; ``scale`` is 1 from the last discontinuity onwards.
    """
    raw = close.to_numpy(dtype=float)
    n = len(raw)
    scale = np.ones(n)
    events: list[dict] = []
    cum = 1.0
    for i in range(n - 1, 0, -1):
        ratio = raw[i] / raw[i - 1]
        if abs(ratio - 1) > max_move:
            factor = snap_factor(ratio, snap_tol)
            cum *= factor if factor else ratio
            events.append({"date": close.index[i], "prev_close": round(raw[i - 1], 4), "close": round(raw[i], 4),
                           "ratio": round(ratio, 5), "kind": "SPLIT/BONUS" if factor else "GAP",
                           "factor": round(factor if factor else ratio, 6)})
        scale[i - 1] = cum
    return raw * scale, scale, events[::-1]
