from datetime import date

import pandas as pd
import pytest

from nse_monitor.data.bhavcopy import parse_bhavcopy
from test_strategy import market  # noqa: F401  (fixture)

SAMPLE = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
RELIANCE, EQ, 23-Sep-2026, 1240.40, 1241.00, 1252.00, 1238.10, 1248.30, 1248.00, 1246.12, 8352048, 104077.61, 245120, 4235680, 50.71
NIFTYBEES, EQ, 23-Sep-2026, 266.64, 266.90, 268.20, 266.50, 267.60, 267.59, 267.35, 3169237, 8472.97, 20115, 2810044, 88.67
SOMEBOND, N1, 23-Sep-2026, 1000.00, 1000.00, 1000.00, 1000.00, 1000.00, 1000.00, 1000.00, 10, 0.10, 1, 10, 100.00
"""


def test_parse_bhavcopy_keeps_requested_series_and_checks_date():
    bars = parse_bhavcopy(SAMPLE, {"EQ"}, expected=date(2026, 9, 23))
    assert list(bars.index) == ["RELIANCE", "NIFTYBEES"]
    assert bars.loc["RELIANCE", "close"] == 1248.0 and bars.loc["NIFTYBEES", "volume"] == 3169237
    with pytest.raises(ValueError):
        parse_bhavcopy(SAMPLE, {"EQ"}, expected=date(2026, 9, 22))  # wrong file for the day


def test_day_yahoo_has_not_completed_is_filled_from_official_file(market, make_engine):  # noqa: F811
    source, cal, _ = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[130], start=cal[125])
    d = cal[131]
    official = pd.DataFrame({s: {"close": df.loc[d, "close"], "volume": df.loc[d, "volume"]}
                             for s, df in source.prices.items()}).T
    for s in list(source.prices)[:9]:  # Yahoo has only 3 of 12 symbols for the day so far
        source.prices[s] = source.prices[s].drop(index=[d])
    kept = next(s for s in list(source.prices)[9:])
    official.loc[kept, "close"] = 1.0  # official file must never overwrite a bar Yahoo already has
    source.official = {d: official}

    res = engine.run(as_of=d)
    assert res.processed_days == [d]
    assert [a for a in repo.audit_log() if a["event"] == "OFFICIAL_EOD_FILL"]
    assert repo.get_prices(kept, d, d)["close"].iloc[0] != 1.0


def test_without_official_file_incomplete_day_still_waits(market, make_engine):  # noqa: F811
    source, cal, _ = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[130], start=cal[125])
    for s in list(source.prices)[:9]:
        source.prices[s] = source.prices[s].drop(index=[cal[131]])
    source.official = {}
    assert engine.run(as_of=cal[131]).processed_days == []
