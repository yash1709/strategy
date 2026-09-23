import numpy as np
import pandas as pd
import pytest

from conftest import InMemorySource, dip_then_rally, frame, make_calendar, sideways
from nse_monitor.cleaning import adjust_discontinuities
from nse_monitor.data.base import Instrument
from nse_monitor.indicators import rsi_wilder
from test_strategy import START, expected_cycle


def test_unadjusted_split_is_snapped_and_rsi_matches_properly_adjusted_series():
    rng = np.random.default_rng(3)
    true = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 200)))
    raw = true.copy()
    raw.iloc[:120] *= 10  # provider forgot to adjust a 1:10 split effective at bar 120
    raw.iloc[120] *= 1.012  # plus a real +1.2% move on the split day
    adjusted, scale, events = adjust_discontinuities(raw)
    [ev] = events
    assert ev["kind"] == "SPLIT/BONUS" and ev["factor"] == pytest.approx(0.1)
    assert scale[119] == pytest.approx(0.1) and scale[120] == 1
    fixed = true.copy()
    fixed.iloc[120] *= 1.012
    np.testing.assert_allclose(rsi_wilder(pd.Series(adjusted)), rsi_wilder(fixed), equal_nan=True)


def test_one_day_bad_print_is_neutralised():
    s = pd.Series([100.0, 101, 102, 700, 103, 104])  # 6.9x: not a split ratio
    adjusted, _, events = adjust_discontinuities(s)
    assert [e["kind"] for e in events] == ["GAP", "GAP"]
    returns = np.diff(adjusted) / adjusted[:-1]
    assert abs(returns[2]) < 1e-9 and abs(returns[3]) < 1e-9  # spike and revert both zeroed


def test_normal_moves_are_untouched():
    s = pd.Series([100.0, 120, 96, 115, 92])  # 20% band moves
    adjusted, scale, events = adjust_discontinuities(s)
    assert events == [] and (scale == 1).all()


def _market_with_split(split_at: int):
    closes = dip_then_rally()
    cal = make_calendar(START, len(closes))
    flat = pd.Series(sideways(len(cal), base=80.0))
    flat.iloc[:split_at] *= 10  # unadjusted 1:10 split
    instruments = [Instrument("DIPCO", "Dip Co"), Instrument("SPLITCO", "Split Co")]
    prices = {"DIPCO": frame(cal, closes), "SPLITCO": frame(cal, flat)}
    for k in range(10):
        instruments.append(Instrument(f"FILL{k}", f"Filler {k}"))
        prices[f"FILL{k}"] = frame(cal, sideways(len(cal), base=40.0 + k))
    return InMemorySource(instruments, prices, cal), cal, closes, flat


def test_unadjusted_split_does_not_create_false_rsi_entry(make_engine):
    source, cal, _, flat = _market_with_split(split_at=110)
    assert rsi_wilder(flat).iloc[125] < 30  # without the repair this would be a (false) entry
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    symbols = {r["symbol"] for r in repo.active()} | {h["symbol"] for h in repo.history()}
    assert "SPLITCO" not in symbols
    [ev] = [a for a in repo.audit_log("SPLITCO") if a["event"] == "PRICE_DISCONTINUITY_ADJUSTED"]
    assert ev["trade_date"] == cal[110].isoformat()


def test_live_matches_replay_with_split_after_crossover(make_engine, tmp_path):
    """A split occurring after DIPCO's crossover must not change what the replay records
    (CMP/SMA are shown in the price scale of their own day, exactly as a live run saw them)."""
    closes = dip_then_rally()
    cal = make_calendar(START, len(closes))
    exp = expected_cycle(closes, cal)
    j = cal.index(exp["cross"]) + 3
    split = pd.Series(closes)
    split.iloc[:j] *= 2  # 1:2 split three sessions after the crossover, never adjusted by provider
    instruments = [Instrument("DIPCO", "Dip Co")] + [Instrument(f"FILL{k}", "F") for k in range(10)]
    prices = {"DIPCO": frame(cal, split)} | {f"FILL{k}": frame(cal, sideways(len(cal), 40.0 + k)) for k in range(10)}
    source = InMemorySource(instruments, prices, cal)

    live, live_repo, _ = make_engine(source, db=tmp_path / "live.db")
    for d in cal[120:]:
        live.run(as_of=d)
    replay, replay_repo, _ = make_engine(source, db=tmp_path / "replay.db")
    replay.run(as_of=cal[-1], start=cal[120])

    strip = lambda rows: [{k: v for k, v in r.items() if k not in ("id", "closed_at")} for r in rows]
    assert strip(live_repo.history()) == strip(replay_repo.history())
    [h] = replay_repo.history()
    assert h["crossover_date"] == exp["cross"].isoformat()
    assert h["cmp_on_crossover"] == pytest.approx(2 * exp["cmp"])  # real traded (pre-split) price
    assert h["sma50_on_crossover"] == pytest.approx(2 * exp["sma"])
