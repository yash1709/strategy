import numpy as np
import pandas as pd

from nse_monitor.indicators import rsi_wilder, sma


def _reference_rsi(closes, period=14):
    """Straightforward textbook Wilder RSI, written independently."""
    deltas = np.diff(closes)
    out = [np.nan] * len(closes)
    gains = [max(x, 0) for x in deltas]
    losses = [max(-x, 0) for x in deltas]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    out[period] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(period + 1, len(closes)):
        ag = (ag * (period - 1) + gains[i - 1]) / period
        al = (al * (period - 1) + losses[i - 1]) / period
        out[i] = 100 - 100 / (1 + ag / al) if al else 100.0
    return np.array(out)


def test_rsi_matches_reference():
    rng = np.random.default_rng(1)
    closes = 100 + np.cumsum(rng.normal(0, 1, 300))
    ours = rsi_wilder(pd.Series(closes)).to_numpy()
    np.testing.assert_allclose(ours, _reference_rsi(closes), equal_nan=True)


def test_rsi_bounds_and_warmup():
    s = pd.Series(np.linspace(100, 50, 40))  # straight decline
    r = rsi_wilder(s)
    assert r.iloc[:14].isna().all()
    assert r.iloc[14:].eq(0).all()
    assert rsi_wilder(pd.Series([10.0] * 5)).isna().all()


def test_indicators_are_causal():
    """Value at bar k must equal the value computed on data truncated at k (no look-ahead)."""
    rng = np.random.default_rng(7)
    closes = pd.Series(200 + np.cumsum(rng.normal(0, 2, 180)))
    full_rsi, full_sma = rsi_wilder(closes), sma(closes, 50)
    for k in (14, 15, 49, 50, 51, 120, 179):
        assert np.isclose(rsi_wilder(closes.iloc[:k + 1]).iloc[-1], full_rsi.iloc[k])
        trunc = sma(closes.iloc[:k + 1], 50).iloc[-1]
        assert (np.isnan(trunc) and np.isnan(full_sma.iloc[k])) or np.isclose(trunc, full_sma.iloc[k])


def test_sma_needs_full_window():
    s = pd.Series(range(1, 61), dtype=float)
    out = sma(s, 50)
    assert out.iloc[:49].isna().all()
    assert out.iloc[49] == np.mean(range(1, 51))
