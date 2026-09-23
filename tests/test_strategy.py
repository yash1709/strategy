from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from conftest import InMemorySource, RecordingNotifier, dip_then_rally, frame, make_calendar, sideways
from nse_monitor.data.base import Instrument
from nse_monitor.indicators import rsi_wilder, sma
from nse_monitor.storage import Repository
from nse_monitor.strategy import IndicatorBook, Strategy

START = date(2025, 11, 3)


def expected_cycle(closes, cal, min_bars=100):
    """Independent computation of the entry and crossover for one stock."""
    s = pd.Series(closes)
    r, m = rsi_wilder(s), sma(s, 50)
    i = next(k for k in range(min_bars - 1, len(s)) if r[k] < 30)
    j = next(k for k in range(i + 1, len(s)) if s[k] > m[k])
    return {"rsi_date": cal[i], "rsi": round(r[i], 2), "start": cal[i + 1], "cross": cal[j],
            "cmp": round(s[j], 4), "sma": round(m[j], 4), "days": j - i}


@pytest.fixture
def market():
    closes = dip_then_rally()
    cal = make_calendar(START, len(closes))
    instruments = [Instrument("DIPCO", "Dip Co Ltd"), Instrument("FLAT", "Flat Industries Ltd")]
    prices = {"DIPCO": frame(cal, closes), "FLAT": frame(cal, sideways(len(cal)))}
    for k in range(10):  # quiet fillers so one suspended stock is a small share of the market
        instruments.append(Instrument(f"FILL{k}", f"Filler {k} Ltd"))
        prices[f"FILL{k}"] = frame(cal, sideways(len(cal), base=40.0 + k))
    return InMemorySource(instruments, prices, cal), cal, closes


def test_replay_full_cycle(market, make_engine):
    source, cal, closes = market
    exp = expected_cycle(closes, cal)
    engine, repo, notifier = make_engine(source)
    res = engine.run(as_of=cal[-1], start=cal[120])

    assert len(res.processed_days) == len(cal) - 120
    assert repo.active() == []
    [h] = repo.history()
    assert h["symbol"] == "DIPCO" and h["status"] == "SMA50_CROSSED"
    assert h["rsi_date"] == exp["rsi_date"].isoformat()
    assert h["rsi_value"] == exp["rsi"]
    assert h["tracking_start_date"] == exp["start"].isoformat()
    assert h["crossover_date"] == exp["cross"].isoformat()
    assert h["cmp_on_crossover"] == pytest.approx(exp["cmp"])
    assert h["sma50_on_crossover"] == pytest.approx(exp["sma"])
    assert h["trading_days_taken"] == exp["days"]

    # exactly one entry although RSI stayed < 30 for several days
    assert [a["event"] for a in repo.audit_log("DIPCO")].count("ENTERED") == 1

    [(subject, body)] = notifier.messages
    assert "DIPCO" in subject
    assert f"RSI Entry Date: {exp['rsi_date']:%d-%b-%Y}" in body
    assert f"Crossover Date: {exp['cross']:%d-%b-%Y}" in body
    assert f"Trading Days Taken: {exp['days']}" in body
    assert "Status: SMA 50 Crossed" in body


def test_live_daily_runs_match_replay(market, make_engine, tmp_path):
    """Running every day with only data available up to that day must give the same
    result as a replay -- i.e. no look-ahead anywhere in the pipeline."""
    source, cal, _ = market
    live, live_repo, _ = make_engine(source, db=tmp_path / "live.db")
    for d in cal[120:]:
        live.run(as_of=d)
    replay, replay_repo, _ = make_engine(source, db=tmp_path / "replay.db")
    replay.run(as_of=cal[-1], start=cal[120])

    strip = lambda rows: [{k: v for k, v in r.items() if k not in ("id", "closed_at")} for r in rows]
    assert strip(live_repo.history()) == strip(replay_repo.history())
    assert len(live_repo.history()) == 1


def test_active_table_while_tracking(market, make_engine):
    source, cal, closes = market
    exp = expected_cycle(closes, cal)
    engine, repo, _ = make_engine(source)
    engine.run(as_of=exp["rsi_date"], start=cal[120])
    [rec] = repo.active()
    assert rec["status"] == "PENDING_START" and rec["tracking_start_date"] is None

    idx = cal.index(exp["start"])
    engine.run(as_of=cal[idx + 2])
    [rec] = repo.active()
    assert rec["tracking_start_date"] == exp["start"].isoformat()
    assert rec["days_tracked"] == 3
    assert rec["cmp"] == pytest.approx(closes[idx + 2], abs=1e-3)
    assert rec["cmp"] < rec["sma50"]
    assert rec["status"] == "ACTIVE"


def test_rerun_is_idempotent(market, make_engine):
    source, cal, _ = market
    engine, repo, notifier = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    res = engine.run(as_of=cal[-1])
    assert res.processed_days == []
    assert len(repo.history()) == 1 and len(notifier.messages) == 1


def test_tracking_starts_next_trading_day_after_holiday(tmp_path, cfg):
    """RSI date is the day before a holiday -> tracking starts on the day after the holiday."""
    closes = dip_then_rally()
    exp_plain = expected_cycle(closes, make_calendar(START, len(closes)))
    holiday = exp_plain["rsi_date"] + timedelta(days=1)
    while holiday.weekday() >= 5:
        holiday += timedelta(days=1)
    cal = make_calendar(START, len(closes), holidays=[holiday])
    exp = expected_cycle(closes, cal)
    assert exp["start"] > holiday

    repo = Repository(tmp_path / "h.db")
    repo.add_trading_days(cal)
    book = IndicatorBook({"DIPCO": frame(cal, closes)}, cfg.strategy)
    strat = Strategy(cfg.strategy)
    for d in cal[120:]:
        with repo.transaction():
            strat.process_day(d, book, repo, {"DIPCO": "Dip Co"})
    [h] = repo.history()
    assert h["tracking_start_date"] == exp["start"].isoformat()
    assert h["trading_days_taken"] == exp["days"]


def test_suspension_counts_trading_days_and_delisting_closes(market, make_engine):
    source, cal, closes = market
    exp = expected_cycle(closes, cal)
    i = cal.index(exp["rsi_date"])
    gap = cal[i + 2:i + 5]  # suspended for 3 sessions after tracking starts
    df = source.prices["DIPCO"]
    source.prices["DIPCO"] = df.drop(index=gap)

    engine, repo, _ = make_engine(source)
    engine.run(as_of=gap[-1], start=cal[120])
    [rec] = repo.active()
    assert rec["missing_days"] == 3
    assert rec["days_tracked"] == 4  # trading days, including the suspended ones
    assert rec["last_eval_date"] == cal[i + 1].isoformat()

    # delisted: vanishes from the NSE list and never trades again
    source.instruments = [x for x in source.instruments if x.symbol != "DIPCO"]
    source.prices["DIPCO"] = df[df.index <= gap[-1]].drop(index=gap)
    engine.run(as_of=cal[i + 6])
    [h] = repo.history()
    assert h["status"] == "DELISTED" and h["crossover_date"] is None
    assert repo.active() == []


def test_corporate_action_triggers_history_refresh(market, make_engine):
    source, cal, closes = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[140], start=cal[130])

    # 1:2 split effective cal[141]; provider back-adjusts all earlier prices
    df = source.prices["FLAT"].copy()
    df.loc[df.index < cal[141], "close"] /= 2
    df.loc[df.index >= cal[141], "close"] /= 2
    source.prices["FLAT"] = df
    engine.run(as_of=cal[142])

    events = [a["event"] for a in repo.audit_log("FLAT")]
    assert "PRICE_HISTORY_ADJUSTED" in events
    cached = repo.get_prices("FLAT", cal[0], cal[142])
    np.testing.assert_allclose(cached["close"].to_numpy(), df.loc[:cal[142], "close"].to_numpy())


def test_reentry_after_exit_creates_new_cycle(cfg, tmp_path):
    first = dip_then_rally()
    closes = first +[first[-1] * 0.975 ** k for k in range(1, 13)] + \
        [first[-1] * 0.975 ** 12 * 1.015 ** k for k in range(1, 60)]
    cal = make_calendar(START, len(closes))
    repo = Repository(tmp_path / "r.db")
    repo.add_trading_days(cal)
    book = IndicatorBook({"DIPCO": frame(cal, closes)}, cfg.strategy)
    strat = Strategy(cfg.strategy)
    for d in cal[120:]:
        with repo.transaction():
            strat.process_day(d, book, repo, {"DIPCO": "Dip Co"})
    hist = repo.history()
    assert len(hist) == 2
    assert all(h["status"] == "SMA50_CROSSED" for h in hist)
    assert len({h["rsi_date"] for h in hist}) == 2


def test_failed_alert_is_retried(market, make_engine):
    source, cal, _ = market
    notifier = RecordingNotifier()
    notifier.fail = True
    engine, repo, _ = make_engine(source, notifier)
    res = engine.run(as_of=cal[-1], start=cal[120])
    assert res.alerts_failed == 1 and notifier.messages == []
    notifier.fail = False
    res = engine.run(as_of=cal[-1])
    assert res.alerts_sent == 1 and len(notifier.messages) == 1


def test_incomplete_day_is_deferred(market, make_engine):
    source, cal, _ = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[130], start=cal[125])
    for s in ["DIPCO", "FILL0", "FILL1", "FILL2"]:  # provider has published only 8 of 12 stocks so far
        source.prices[s] = source.prices[s].drop(index=[cal[131]])
    res = engine.run(as_of=cal[131])
    assert res.processed_days == [] and "retried" in res.skipped_reason
    assert repo.last_processed_day() == cal[130]


def test_holiday_placeholder_bars_are_not_sessions(market, make_engine):
    """Yahoo publishes zero-volume, unchanged-close bars on NSE holidays and its index
    feed lists the day. Such a day must not start tracking or count as a trading day."""
    source, cal, closes = market
    exp = expected_cycle(closes, cal)
    holiday = exp["rsi_date"] + timedelta(days=1)
    while holiday.weekday() >= 5 or holiday in cal:
        holiday += timedelta(days=1)
    for s, df in source.prices.items():
        prev = df[df.index < holiday].iloc[-1]
        ph = pd.DataFrame({"close": [prev["close"]], "volume": [0.0]}, index=[holiday])
        source.prices[s] = pd.concat([df, ph]).sort_index()
    source.calendar = sorted(cal + [holiday])

    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    [h] = repo.history()
    assert holiday not in repo.trading_days()
    assert h["tracking_start_date"] == exp["start"].isoformat()
    assert h["trading_days_taken"] == exp["days"]


def test_permanent_gap_day_is_processed_when_later_days_are_complete(market, make_engine):
    source, cal, _ = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[130], start=cal[125])
    for s in ["FILL0", "FILL1", "FILL2"]:  # provider permanently lacks cal[131] for 3 of 12 stocks
        source.prices[s] = source.prices[s].drop(index=[cal[131]])
    res = engine.run(as_of=cal[133])
    assert res.processed_days == [cal[131], cal[132], cal[133]]
    assert "LOW_COVERAGE_DAY" in [a["event"] for a in repo.audit_log()]


def test_replay_without_notify_does_not_flush_old_alerts_later(market, cfg, monkeypatch, tmp_path):
    from nse_monitor import cli
    from conftest import RecordingNotifier
    source, cal, _ = market
    rec = RecordingNotifier()
    monkeypatch.setattr(cli, "create_source", lambda c: source)
    monkeypatch.setattr(cli, "build_notifiers", lambda c: [rec])
    db = str(tmp_path / "cli.db")
    conf = tmp_path / "config.toml"
    conf.write_text('reports_dir = "reports"\nlog_dir = "logs"\n', encoding="utf-8")
    assert cli.main(["--config", str(conf), "--db", db, "replay", "--start", cal[120].isoformat(), "--end", cal[-3].isoformat()]) == 0
    assert cli.main(["--config", str(conf), "--db", db, "run", "--as-of", cal[-1].isoformat()]) == 0
    assert all("SMA 50 crossed" not in subject for subject, _ in rec.messages)


def test_market_cap_volume_and_largest_first(market, make_engine):
    from nse_monitor.reports import active_table, history_table
    source, cal, closes = market
    exp = expected_cycle(closes, cal)
    source.shares = {"DIPCO": 5e8}
    engine, repo, notifier = make_engine(source)
    # stop mid-tracking: DIPCO active, then add a bigger company to the list
    idx = cal.index(exp["start"])
    engine.run(as_of=cal[idx + 1], start=cal[120])
    [rec] = repo.active()
    assert rec["volume"] == 1e6
    assert rec["market_cap"] == pytest.approx(5e8 * rec["cmp"])

    with repo.transaction():
        repo.add_active("BIGCO", "Big Co", cal[idx], 25.0, 1000.0, "ACTIVE")
        repo.set_shares({"BIGCO": 1e10}, cal[idx])
    repo.conn.execute("INSERT OR IGNORE INTO instruments(symbol, company, shares_outstanding, shares_updated, is_listed) "
                      "VALUES ('BIGCO','Big Co',1e10,?,0)", (cal[idx].isoformat(),))
    repo.refresh_market_caps()
    table = active_table(repo)
    assert list(table["Symbol"]) == ["BIGCO", "DIPCO"]  # largest market cap first
    assert table.loc[1, "Volume"] == 1_000_000 and table.loc[1, "CMP (Close)"] == rec["cmp"]
    assert table.loc[1, "Market Cap / AUM (₹ Cr)"] == pytest.approx(5e8 * rec["cmp"] / 1e7, abs=0.01)

    engine.run(as_of=cal[-1])  # BIGCO (unlisted, no prices) is closed as DELISTED; DIPCO crosses
    h = next(r for r in repo.history() if r["symbol"] == "DIPCO")
    assert h["market_cap_on_crossover"] == pytest.approx(5e8 * exp["cmp"], rel=1e-6)
    assert h["volume_on_crossover"] == 1e6
    hist = history_table(repo).set_index("NSE Symbol")
    assert hist.loc["DIPCO", "Volume on Crossover Date"] == 1_000_000
    body = next(b for s, b in notifier.messages if "DIPCO" in s)
    assert f"Market Cap: ₹{5e8 * exp['cmp'] / 1e7:,.0f} Cr" in body and "Volume: 1,000,000" in body


def test_shares_lookup_failure_does_not_break_run(market, make_engine):
    source, cal, _ = market
    source.shares_error = ConnectionError("yahoo down")
    engine, repo, _ = make_engine(source)
    res = engine.run(as_of=cal[-1], start=cal[120])
    assert len(repo.history()) == 1 and repo.history()[0]["market_cap_on_crossover"] is None
    assert "SHARES_FETCH_FAILED" in [a["event"] for a in repo.audit_log()]
