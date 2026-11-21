"""Tests for the extra features: quality score, optional rules, market mood and signals, backtest outcomes,
chart data, alert types and filters, data gap filling, sectors, health checks, AI helpers and the new CLI commands."""
import json
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import requests

from conftest import InMemorySource, RecordingNotifier, dip_then_rally, frame, make_calendar, sideways
from nse_monitor import ai, analytics
from nse_monitor.config import AiConfig
from nse_monitor.data.base import Instrument
from nse_monitor.indicators import rsi_wilder
from nse_monitor.notifications import Notifier, dispatch_pending, safe_error
from nse_monitor.storage import Repository
from nse_monitor.strategy import Bar, IndicatorBook, Strategy, quality_score
from test_strategy import START, expected_cycle, market  # noqa: F401  (fixture)

NAN = float("nan")


def replay_one(cfg, closes, start_idx=120, stop_idx=None, universe_until=None):
    """Run the strategy directly on one stock (DIPCO) and return (repo, calendar)."""
    cal = make_calendar(START, len(closes) if stop_idx is None else max(len(closes), stop_idx + 1))
    repo = Repository(":memory:")
    repo.add_trading_days(cal)
    book = IndicatorBook({"DIPCO": frame(cal, closes)}, cfg.strategy)
    strat = Strategy(cfg.strategy)
    for d in cal[start_idx:stop_idx]:
        universe = {"DIPCO": "Dip Co"} if universe_until is None or d <= universe_until else {}
        with repo.transaction():
            strat.process_day(d, book, repo, universe)
    return repo, cal


def the_record(repo):
    return (repo.history() + repo.active())[0]


# ------------------------------------------------------------------ quality score
def bar(**kw):
    base = dict(close=100.0, volume=1e6, rsi=25.0, sma=110.0, avg_volume=1e6, bars=300, sma200=100.0, prev_rsi=24.0)
    base.update(kw)
    return Bar(**base)


def test_quality_score_full_and_empty_marks():
    best, notes = quality_score(bar(rsi=15, volume=2e6, sma200=100.0, prev_rsi=14.0))
    assert best == 100 and "RSI turning up" in notes and "stock-specific" in notes
    worst, _ = quality_score(bar(rsi=29.9, volume=1e6, sma200=150.0, prev_rsi=30.0))  # 33% under SMA 200, falling
    assert worst < 10


def test_quality_score_says_when_the_whole_market_is_oversold():
    _, notes = quality_score(bar(), market_oversold_pct=22.0)
    assert "market-wide selloff" in notes
    q, notes = quality_score(bar(sma200=NAN, avg_volume=NAN))
    assert "SMA200 n/a" in notes and "volume n/a" in notes and 0 <= q <= 100


def test_entries_record_quality_in_the_audit_log_and_on_the_active_row(cfg):
    closes = dip_then_rally()
    exp = expected_cycle(closes, make_calendar(START, len(closes)))
    repo, cal = replay_one(cfg, closes, stop_idx=cal_index(closes, exp["rsi_date"]) + 1)
    [rec] = repo.active()
    assert 0 <= rec["quality"] <= 100 and rec["quality_notes"].startswith("RSI ")
    entered = [json.loads(a["details"]) for a in repo.audit_log("DIPCO") if a["event"] == "ENTERED"]
    assert entered[0]["quality"] == rec["quality"]


def cal_index(closes, d):
    return make_calendar(START, len(closes)).index(d)


# ------------------------------------------------------------------ optional rules
def test_rsi_turn_confirmation_waits_for_the_first_uptick(cfg):
    closes = dip_then_rally()
    baseline, cal = replay_one(cfg, closes)
    cfg.strategy.entry_confirmation = "rsi_turn"
    confirmed, _ = replay_one(cfg, closes)
    first, second = the_record(baseline), the_record(confirmed)
    assert second["rsi_date"] > first["rsi_date"]
    i = cal.index(date.fromisoformat(second["rsi_date"]))
    rsi = rsi_wilder(pd.Series(closes))
    assert rsi[i] < 30 and rsi[i] > rsi[i - 1]


def first_closed(repo):
    """The earliest closed cycle. (After an early exit a stock that is still oversold re-enters, so there can be several.)"""
    return min(repo.history(), key=lambda h: h["exit_date"])


def test_time_limit_closes_a_cycle_that_has_not_crossed(cfg):
    cfg.strategy.max_tracking_days = 3
    repo, _ = replay_one(cfg, dip_then_rally())
    h = first_closed(repo)
    assert h["status"] == "TIME_LIMIT" and h["trading_days_taken"] == 3 and h["crossover_date"] is None
    assert "3 trading days" in h["notes"]


def test_stop_loss_closes_when_price_keeps_falling(cfg):
    cfg.strategy.stop_loss_pct = 1.0
    repo, _ = replay_one(cfg, dip_then_rally())
    h = first_closed(repo)
    assert h["status"] == "STOP_LOSS" and h["trading_days_taken"] == 1


def test_rsi_recovery_exit_fires_before_the_sma_crossover(cfg):
    closes = dip_then_rally()
    exp = expected_cycle(closes, make_calendar(START, len(closes)))
    cfg.strategy.exit_rsi = 50.0
    repo, _ = replay_one(cfg, closes)
    h = first_closed(repo)
    assert h["status"] == "RSI_RECOVERED" and h["trading_days_taken"] < exp["days"]


def test_optional_rules_are_off_by_default(cfg):
    repo, _ = replay_one(cfg, dip_then_rally())
    assert [h["status"] for h in repo.history()] == ["SMA50_CROSSED"]


def test_delisting_waits_for_the_grace_period(cfg):
    closes = dip_then_rally()
    exp = expected_cycle(closes, make_calendar(START, len(closes)))
    i = cal_index(closes, exp["rsi_date"])
    truncated = closes[:i + 2]                      # bars exist through the first tracked day only
    cal = make_calendar(START, len(closes))
    repo = Repository(":memory:")
    repo.add_trading_days(cal)
    book = IndicatorBook({"DIPCO": frame(cal, truncated)}, cfg.strategy)
    strat = Strategy(cfg.strategy)
    for n, d in enumerate(cal[120:i + 5], start=120):
        universe = {"DIPCO": "Dip Co"} if n <= i + 1 else {}   # then it vanishes from the NSE list
        with repo.transaction():
            strat.process_day(d, book, repo, universe)
        if n == i + 3:   # two sessions missing: a list glitch is still possible
            assert repo.history() == [] and repo.active()[0]["missing_days"] == 2
    [h] = repo.history()                            # third missing session: now it is a delisting
    assert h["status"] == "DELISTED"


# ------------------------------------------------------------------ breadth and signals
def test_breadth_is_recorded_every_day_and_counts_oversold_stocks(cfg):
    repo, cal = replay_one(cfg, dip_then_rally())
    rows = repo.breadth_history(500)
    assert len(rows) == len(cal) - 120
    assert max(r["oversold"] for r in rows) == 1 and all(r["stocks"] == 1 for r in rows)


def test_extra_signals_are_detected():
    cfg = SimpleNamespace(rsi_period=14, max_daily_move=0.35, sma_period=50, rsi_threshold=30.0)
    closes = np.concatenate([np.linspace(100, 200, 100), np.linspace(200, 100, 220), np.linspace(100, 260, 100)])
    cal = make_calendar(START, len(closes))
    repo = Repository(":memory:")
    book = IndicatorBook({"DIPCO": frame(cal, closes)}, cfg)
    strat = Strategy(__import__("nse_monitor.config", fromlist=["StrategyConfig"]).StrategyConfig())
    seen = set()
    for d in cal[200:]:  # the 200-day average exists from bar 200; 52-week signals need 252 bars
        with repo.transaction():
            strat._breadth_and_signals(d, book, repo, {"DIPCO": "Dip Co"})
        seen |= {r["kind"] for r in repo.signals_recent(0)}
    assert {"OVERBOUGHT", "LOW_52W", "HIGH_52W", "GOLDEN_CROSS", "DEATH_CROSS"} <= seen


def test_old_signals_are_pruned(cfg):
    repo = Repository(":memory:")
    repo.save_signals(date(2026, 1, 1), [("A", "LOW_52W", 1.0)])
    repo.save_signals(date(2026, 3, 1), [("B", "LOW_52W", 2.0)])
    assert [r["symbol"] for r in repo.signals_recent(1000)] == ["B"]


# ------------------------------------------------------------------ outcomes (backtest) and chart data
def test_forward_returns_and_backtest_summary(market, make_engine):  # noqa: F811
    source, cal, closes = market
    exp = expected_cycle(closes, cal)
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    i = cal.index(exp["rsi_date"])
    row = next(r for r in repo.conn.execute("SELECT * FROM outcomes WHERE symbol='DIPCO' AND base='ENTRY'"))
    assert row["ret_5"] == pytest.approx(closes[i + 5] / closes[i] - 1)
    assert row["ret_20"] == pytest.approx(closes[i + 20] / closes[i] - 1)
    assert row["bench_5"] is not None  # median of the other stocks over the same window

    summary = analytics.backtest_summary(repo)
    assert summary["signals"] == 1
    entry5 = summary["overall"][summary["overall"]["Horizon (days)"] == 5].iloc[0]
    assert entry5["Signals"] == 1 and entry5["Avg return %"] == pytest.approx(100 * (closes[i + 5] / closes[i] - 1), abs=0.01)
    assert not summary["by_size"].empty and not summary["by_speed"].empty


def test_outcomes_are_not_recomputed_once_complete(market, make_engine):  # noqa: F811
    source, cal, _ = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    from nse_monitor.strategy import IndicatorBook as Book
    book = Book(repo.load_all_prices(), engine.cfg.strategy)
    assert analytics.update_outcomes(repo, book) == 0


def test_chart_bars_hold_recent_bars_for_tracked_and_exited_stocks(market, make_engine):  # noqa: F811
    source, cal, closes = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    book = IndicatorBook(repo.load_all_prices(), engine.cfg.strategy)
    exited = date.fromisoformat(repo.history()[0]["exit_date"])

    # a stock that exited inside the window keeps its chart, trimmed to the last CHART_BARS sessions
    analytics.refresh_chart_bars(repo, book, exited + timedelta(days=analytics.CHART_HISTORY_DAYS - 1))
    bars = repo.chart_bars("DIPCO")
    assert 0 < len(bars) <= analytics.CHART_BARS == 90
    assert bars["date"].iloc[-1] == cal[-1].isoformat()
    assert bars["close"].iloc[-1] == pytest.approx(closes[-1])
    assert repo.chart_bars("FLAT").empty  # never tracked, so no chart data kept

    # once it exited longer ago than the window, its chart is dropped to keep the published file small
    analytics.refresh_chart_bars(repo, book, exited + timedelta(days=analytics.CHART_HISTORY_DAYS + 1))
    assert repo.chart_bars("DIPCO").empty


def test_empty_backtest_is_safe():
    summary = analytics.backtest_summary(Repository(":memory:"))
    assert summary["signals"] == 0 and summary["overall"].empty


# ------------------------------------------------------------------ one-time catch-up after an upgrade
def make_pre_upgrade_state(repo):
    """What a database written before these features looks like: no mood, signals, outcomes, charts, scores."""
    for table in ("market_breadth", "signals", "outcomes", "chart_bars", "ai_notes"):
        repo.conn.execute(f"DELETE FROM {table}")
    repo.conn.execute("UPDATE active_tracking SET quality=NULL, quality_notes=NULL")
    repo.conn.execute("DELETE FROM audit_log WHERE event='ANALYTICS_CATCH_UP'")


def test_catch_up_fills_the_new_panels_when_there_is_nothing_new_to_process(market, make_engine):  # noqa: F811
    source, cal, closes = market
    exp = expected_cycle(closes, cal)
    idx = cal.index(exp["start"])
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[idx + 2], start=cal[120])           # DIPCO is being tracked
    processed = len(repo.processed_days_table(500))
    make_pre_upgrade_state(repo)

    res = engine.run(as_of=cal[idx + 2])                      # nothing new: like the first run after deploying
    assert res.processed_days == [] and "up to date" in res.skipped_reason
    assert len(repo.breadth_history(500)) == processed        # market mood rebuilt from the price cache
    assert not repo.chart_bars("DIPCO").empty
    [rec] = repo.active()
    assert 0 <= rec["quality"] <= 100 and rec["quality_notes"].endswith("(backfilled)")
    assert "market-wide" not in rec["quality_notes"] and "stock-specific" not in rec["quality_notes"]  # context unknown
    assert repo.has_audit("ANALYTICS_CATCH_UP")


def test_catch_up_runs_only_once(market, make_engine):  # noqa: F811
    source, cal, _ = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    assert repo.has_audit("ANALYTICS_CATCH_UP")
    repo.conn.execute("DELETE FROM chart_bars")
    engine.run(as_of=cal[-1])
    assert repo.chart_bars("DIPCO").empty                      # not repeated on every run


def test_catch_up_never_touches_past_decisions(market, make_engine):  # noqa: F811
    source, cal, _ = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    before = ([dict(r) for r in repo.history()], [dict(r) for r in repo.active()])
    make_pre_upgrade_state(repo)
    engine.run(as_of=cal[-1])
    after = ([dict(r) for r in repo.history()], [dict(r) for r in repo.active()])
    assert before == after


def test_catch_up_waits_for_a_price_cache(tmp_path, cfg):
    from nse_monitor.engine import Engine
    repo = Repository(tmp_path / "e.db")
    repo.mark_processed(date(2026, 11, 20), {}, coverage=1.0)  # state exists, but the price cache was evicted
    Engine(cfg, repo, InMemorySource([], {}, []), [])._catch_up()
    assert not repo.has_audit("ANALYTICS_CATCH_UP")           # will try again once prices are back


# ------------------------------------------------------------------ alerts
def test_entry_digest_alert_lists_new_entries_with_quality(market, make_engine, cfg):  # noqa: F811
    source, cal, _ = market
    cfg.alerts.on_entry = True
    engine, repo, notifier = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    [(subject, body)] = [m for m in notifier.messages if m[0].startswith("NSE oversold entries")]
    assert "DIPCO" in body and "quality" in body and "RSI " in body


def test_near_cross_alert_is_sent_once_per_cycle(market, make_engine, cfg):  # noqa: F811
    source, cal, closes = market
    exp = expected_cycle(closes, cal)
    idx = cal.index(exp["start"])
    cfg.alerts.on_near_cross = True
    cfg.alerts.near_cross_pct = 50.0
    engine, repo, notifier = make_engine(source)
    engine.run(as_of=cal[idx + 2], start=cal[120])
    near = lambda: [s for s, _ in notifier.messages if s == "Near SMA 50: DIPCO"]
    assert len(near()) == 1
    engine.run(as_of=cal[idx + 3])
    assert len(near()) == 1  # not repeated on the next day


def test_alert_filters_limit_instrument_types_and_size(market, make_engine, cfg):  # noqa: F811
    source, cal, _ = market
    cfg.alerts.kinds = ["ETF"]
    engine, repo, notifier = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    assert len(repo.history()) == 1 and notifier.messages == []  # DIPCO is a stock: crossover alert filtered out


def test_min_size_filter_skips_small_names_but_keeps_unknown_size(market, make_engine, cfg):  # noqa: F811
    source, cal, _ = market
    cfg.alerts.min_market_cap_cr = 1000
    engine, repo, _ = make_engine(source)
    crore = 1e7
    assert not engine._alert_allowed("X", kind="EQUITY", size=500 * crore)     # 500 Cr < 1000 Cr
    assert engine._alert_allowed("X", kind="EQUITY", size=2000 * crore)
    assert engine._alert_allowed("X", kind="EQUITY", size=None)                # unknown size is not held against a name
    cfg.alerts.kinds = ["EQUITY"]
    assert not engine._alert_allowed("X", kind="ETF", size=2000 * crore)


def test_safe_error_never_contains_the_secret_url():
    exc = requests.ConnectionError("HTTPSConnectionPool: Max retries for url: /botSECRET-TOKEN/sendMessage")
    assert "SECRET" not in safe_error(exc) and safe_error(exc) == "ConnectionError"
    resp = SimpleNamespace(status_code=401)
    http = requests.HTTPError("401 Client Error for url: https://api.telegram.org/botSECRET-TOKEN/sendMessage")
    http.response = resp
    assert safe_error(http) == "HTTPError (HTTP 401)"


def test_failed_delivery_does_not_store_the_secret(tmp_path):
    class Leaky(Notifier):
        channel = "telegram"

        def send(self, subject, body):
            raise requests.ConnectionError("failed for url https://api.telegram.org/botSECRET-TOKEN/sendMessage")

    repo = Repository(tmp_path / "a.db")
    repo.queue_alert("CROSSOVER", "s", "b", ["telegram"])
    assert dispatch_pending(repo, [Leaky()], 5) == (0, 1)
    err = repo.conn.execute("SELECT last_error FROM alerts").fetchone()[0]
    assert "SECRET" not in err and err == "ConnectionError"


# ------------------------------------------------------------------ data gaps, mismatches, sectors
def official_for(source, d):
    return pd.DataFrame({s: {"close": df.loc[d, "close"], "volume": df.loc[d, "volume"]}
                         for s, df in source.prices.items()}).T


def test_gaps_in_a_mostly_complete_day_are_filled_before_it_is_processed(market, make_engine):  # noqa: F811
    source, cal, _ = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[130], start=cal[125])
    d = cal[131]
    official = official_for(source, d)
    source.prices["FILL0"] = source.prices["FILL0"].drop(index=[d])  # 11 of 12 stocks: above the 90% gate
    source.official = {d: official}
    res = engine.run(as_of=d)
    assert res.processed_days == [d]
    assert repo.get_prices("FILL0", d, d)["close"].iloc[0] == pytest.approx(official.loc["FILL0", "close"])


def test_price_disagreement_with_the_official_file_is_flagged_not_overwritten(market, make_engine):  # noqa: F811
    source, cal, _ = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[130], start=cal[125])
    d = cal[131]
    official = official_for(source, d)
    yahoo = official.loc["FILL1", "close"]
    official.loc["FILL1", "close"] = yahoo * 1.2
    source.prices["FILL0"] = source.prices["FILL0"].drop(index=[d])  # a gap, as on any real day, so the file is fetched
    source.official = {d: official}
    engine.run(as_of=d)
    [row] = [a for a in repo.audit_log("FILL1") if a["event"] == "PRICE_SOURCE_MISMATCH"]
    assert json.loads(row["details"])["diff_pct"] == pytest.approx(16.67, abs=0.01)
    assert repo.get_prices("FILL1", d, d)["close"].iloc[0] == pytest.approx(yahoo)  # Yahoo's bar is kept


def test_sectors_are_filled_for_tracked_stocks_and_etfs(make_engine):
    closes = dip_then_rally()
    cal = make_calendar(START, len(closes))
    instruments = [Instrument("DIPCO", "Dip Co"), Instrument("DIPETF", "Dip Gold ETF [GOLD]", kind="ETF")]
    prices = {"DIPCO": frame(cal, closes), "DIPETF": frame(cal, closes)}
    for k in range(10):
        instruments.append(Instrument(f"FILL{k}", f"Filler {k}"))
        prices[f"FILL{k}"] = frame(cal, sideways(len(cal), base=40.0 + k))
    source = InMemorySource(instruments, prices, cal)
    source.fetch_sectors = lambda syms: {s: ("Technology", "Software") for s in syms}
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    assert repo.instrument("DIPCO")["sector"] == "Technology"
    assert repo.instrument("DIPETF")["sector"] == "GOLD"
    assert repo.instrument("FILL0")["sector"] is None  # never tracked: no look-up spent on it
    from nse_monitor.reports import history_table
    assert set(history_table(repo)["Sector"]) == {"Technology", "GOLD"}


def test_a_failing_post_step_does_not_fail_the_run(market, make_engine):  # noqa: F811
    source, cal, _ = market
    source.fetch_sectors = lambda syms: (_ for _ in ()).throw(RuntimeError("yahoo down"))
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    assert len(repo.history()) == 1
    assert "SECTOR_FETCH_FAILED" in [a["event"] for a in repo.audit_log()]


def test_coverage_is_recorded_for_each_processed_day(market, make_engine):  # noqa: F811
    source, cal, _ = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[125], start=cal[120])
    assert all(p["coverage"] == 1.0 for p in repo.processed_days_table())
    table = analytics.health_table(repo)
    assert list(table["Price coverage %"].unique()) == [100.0]


# ------------------------------------------------------------------ health checks
def test_health_check_flags_stale_data_and_low_coverage():
    repo = Repository(":memory:")
    assert analytics.health_check(repo, date(2026, 11, 20)) == ["No trading day has been processed yet."]
    repo.mark_processed(date(2026, 11, 18), {}, coverage=0.99)
    assert analytics.health_check(repo, date(2026, 11, 20)) == []
    assert "stale" in analytics.health_check(repo, date(2026, 11, 30))[0]
    repo.mark_processed(date(2026, 11, 19), {}, coverage=0.7)
    assert "Low price coverage" in analytics.health_check(repo, date(2026, 11, 20))[0]
    repo.audit("PRICE_FETCH_FAILED", trade_date=date(2026, 11, 19))
    assert any("Price Fetch Failed" in p for p in analytics.health_check(repo, date(2026, 11, 20)))


# ------------------------------------------------------------------ AI helpers
class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Messages:
    def __init__(self, text, stop="end_turn", boom=None):
        self.text, self.stop, self.boom, self.calls = text, stop, boom, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.boom:
            raise self.boom
        return SimpleNamespace(content=[_Block(self.text)], stop_reason=self.stop, stop_details=None)


def fake_client(text, stop="end_turn", beta_boom=None):
    return SimpleNamespace(beta=SimpleNamespace(messages=_Messages(text, stop, beta_boom)),
                           messages=_Messages(text, stop))


AI_CFG = AiConfig(enabled=True)


def test_ai_is_off_without_a_key_or_when_disabled(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not ai.available(AI_CFG)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert ai.available(AI_CFG) and not ai.available(AiConfig(enabled=False))


def test_entry_note_parses_json_and_sends_a_sane_request():
    c = fake_client('```json\n{"risk": "high", "summary": "Weak results were reported."}\n```')
    note = ai.entry_note(AI_CFG, "ABC", "Abc Ltd", {"RSI(14)": 24.1}, ["2026-11-01: Abc misses estimates"], client=c)
    assert note == {"risk": "HIGH", "summary": "Weak results were reported."}
    [call] = c.beta.messages.calls
    assert call["model"] == "claude-opus-5-5" and call["output_config"] == {"effort": "low"}
    assert call["fallbacks"] == "default" and "temperature" not in call
    assert "Abc misses estimates" in call["messages"][0]["content"]


def test_entry_note_falls_back_to_the_plain_endpoint_and_coerces_bad_risk():
    c = fake_client('{"risk": "scary", "summary": "Normal market weakness."}', beta_boom=TypeError("no fallbacks"))
    note = ai.entry_note(AI_CFG, "ABC", "Abc Ltd", {}, [], client=c)
    assert note["risk"] == "MEDIUM" and len(c.messages.calls) == 1


@pytest.mark.parametrize("text,stop", [("not json", "end_turn"), ('{"risk": "LOW"}', "end_turn"),
                                       ('{"risk": "LOW", "summary": "x"}', "refusal")])
def test_entry_note_returns_none_when_unusable(text, stop):
    assert ai.entry_note(AI_CFG, "ABC", "Abc", {}, [], client=fake_client(text, stop)) is None


def test_entry_note_swallows_api_errors():
    c = fake_client("{}")
    c.beta.messages.boom = RuntimeError("network down")
    assert ai.entry_note(AI_CFG, "ABC", "Abc", {}, [], client=c) is None


def test_engine_adds_ai_notes_to_the_entry_digest_once(market, make_engine, cfg, monkeypatch):  # noqa: F811
    source, cal, _ = market
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg.ai.enabled = True
    cfg.alerts.on_entry = True
    calls = []
    monkeypatch.setattr(ai, "entry_note", lambda c, sym, company, facts, headlines, **k:
                        calls.append((sym, headlines)) or {"risk": "HIGH", "summary": "Weak results reported."})
    source.fetch_news = lambda sym, limit=8: ["2026-01-01: headline"]
    engine, repo, notifier = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    assert calls == [("DIPCO", ["2026-01-01: headline"])]
    [(subject, body)] = [m for m in notifier.messages if m[0].startswith("NSE oversold entries")]
    assert "News risk HIGH: Weak results reported." in body
    [note] = repo.ai_notes()
    assert note["symbol"] == "DIPCO" and note["risk"] == "HIGH"


def test_ai_failures_never_break_the_run(market, make_engine, cfg, monkeypatch):  # noqa: F811
    source, cal, _ = market
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg.ai.enabled = True
    cfg.alerts.on_entry = True
    monkeypatch.setattr(ai, "entry_note", lambda *a, **k: None)
    engine, repo, notifier = make_engine(source)
    engine.run(as_of=cal[-1], start=cal[120])
    assert len(repo.history()) == 1 and repo.ai_notes() == []
    assert any(m[0].startswith("NSE oversold entries") for m in notifier.messages)


TABLES = {
    "active": pd.DataFrame({
        "Symbol": ["AAA", "BBB", "CCC", "DDD"], "Type": ["EQUITY", "ETF", "EQUITY", "EQUITY"],
        "Company": ["Alpha Ltd", "Gold Fund", "Gamma Steel", "Delta Pharma"], "Quality": [80, 60, 90, 40],
        "Market Cap / AUM (₹ Cr)": [25000.0, 300.0, None, 8000.0],
        "RSI Date": pd.to_datetime(["2026-09-01", "2026-10-01", "2026-10-15", "2026-08-01"])}),
}


def spec(**kw):
    return {"table": "active", **kw}


def test_apply_spec_filters_sorts_and_limits():
    name, df = ai.apply_spec(spec(filters=[{"column": "Quality", "op": ">", "value": 70}], sort_by="Quality",
                                  ascending=False), TABLES)
    assert name == "active" and list(df["Symbol"]) == ["CCC", "AAA"]
    _, df = ai.apply_spec(spec(filters=[{"column": "Type", "op": "==", "value": "etf"}]), TABLES)  # case-insensitive
    assert list(df["Symbol"]) == ["BBB"]
    _, df = ai.apply_spec(spec(filters=[{"column": "Company", "op": "contains", "value": "STEEL"}]), TABLES)
    assert list(df["Symbol"]) == ["CCC"]
    _, df = ai.apply_spec(spec(filters=[{"column": "Symbol", "op": "in", "value": ["aaa", "DDD"]}]), TABLES)
    assert list(df["Symbol"]) == ["AAA", "DDD"]
    _, df = ai.apply_spec(spec(filters=[{"column": "RSI Date", "op": ">=", "value": "2026-09-15"}]), TABLES)
    assert list(df["Symbol"]) == ["BBB", "CCC"]
    _, df = ai.apply_spec(spec(filters=[{"column": "Market Cap / AUM (₹ Cr)", "op": ">=", "value": 20000}]), TABLES)
    assert list(df["Symbol"]) == ["AAA"]  # missing sizes never match
    _, df = ai.apply_spec(spec(limit=2), TABLES)
    assert len(df) == 2
    _, df = ai.apply_spec(spec(limit=999999, sort_by="Symbol"), TABLES)
    assert len(df) == 4


@pytest.mark.parametrize("bad", [
    {"table": "secrets"},
    {"table": "active", "filters": [{"column": "Password", "op": "==", "value": 1}]},
    {"table": "active", "filters": [{"column": "Symbol", "op": "; DROP TABLE x", "value": 1}]},
    {"table": "active", "filters": [{"column": "Quality", "op": ">", "value": "lots"}]},
    {"table": "active", "filters": [{"column": "Quality", "op": "contains", "value": "8"}]},
    {"table": "active", "sort_by": "nope"},
    {"error": "cannot answer"},
])
def test_apply_spec_rejects_anything_invalid(bad):
    with pytest.raises(ValueError):
        ai.apply_spec(bad, TABLES)


def test_question_to_spec_describes_tables_to_the_model():
    c = fake_client('{"table": "active", "filters": [], "limit": 5, "explanation": "all"}')
    out = ai.question_to_spec("show everything", TABLES, AI_CFG, client=c)
    assert out["table"] == "active"
    prompt = c.beta.messages.calls[0]["messages"][0]["content"]
    assert "Table 'active'" in prompt and "Quality (number)" in prompt and "RSI Date (date)" in prompt


# ------------------------------------------------------------------ shipped config files
@pytest.mark.parametrize("name", ["config.example.toml", "config.cloud.toml"])
def test_shipped_config_files_load(name):
    from pathlib import Path
    from nse_monitor.config import load_config
    cfg = load_config(Path(__file__).resolve().parents[1] / name)  # unknown keys raise
    assert cfg.alerts.on_entry and cfg.ai.enabled is False and cfg.ai.model == "claude-opus-5-5"
    assert cfg.strategy.entry_confirmation == "none" and cfg.strategy.max_tracking_days == 0
    assert cfg.data.include_bse is True and cfg.data.bse_groups == ["A", "B", "X", "XT"]


# ------------------------------------------------------------------ cloud state and CLI
def test_cloud_pack_keeps_the_new_tables_and_drops_only_prices(tmp_path):
    from nse_monitor import cloud_state
    repo = Repository(tmp_path / "w.db")
    repo.save_breadth(date(2026, 11, 2), 100, 10, 5, 40, 50, 90)
    repo.save_ai_note("AAA", date(2026, 11, 2), "LOW", "fine")
    repo.replace_chart_bars("AAA", pd.DataFrame({"date": [date(2026, 11, 2)], "close": [1.0], "sma50": [1.0], "rsi": [30.0]}))
    repo.close()
    cloud_state.pack(tmp_path / "w.db", tmp_path / "state.db", tmp_path / "prices.db")
    con = sqlite3.connect(tmp_path / "state.db")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"market_breadth", "ai_notes", "chart_bars", "outcomes", "signals"} <= tables
    assert "prices" not in tables
    assert con.execute("SELECT COUNT(*) FROM market_breadth").fetchone()[0] == 1


def test_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE active_tracking (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL UNIQUE,
        company TEXT, rsi_date TEXT NOT NULL, rsi_value REAL NOT NULL, rsi_close REAL, tracking_start_date TEXT,
        last_eval_date TEXT, cmp REAL, sma50 REAL, days_tracked INTEGER NOT NULL DEFAULT 0,
        missing_days INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE processed_days (date TEXT PRIMARY KEY, processed_at TEXT NOT NULL, screened INTEGER, added INTEGER,
        crossed INTEGER, removed INTEGER);
        INSERT INTO active_tracking(symbol, company, rsi_date, rsi_value, status, created_at, updated_at)
        VALUES ('OLD', 'Old Co', '2026-08-03', 25, 'ACTIVE', 'x', 'x');""")
    con.commit()
    con.close()
    repo = Repository(path)
    [row] = repo.active()
    assert row["symbol"] == "OLD" and row["quality"] is None and row["near_alerted"] == 0
    repo.mark_processed(date(2026, 8, 4), {}, coverage=0.95)
    assert repo.processed_days_table()[0]["coverage"] == 0.95


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from nse_monitor import cli
    rec = RecordingNotifier()
    monkeypatch.setattr(cli, "build_notifiers", lambda c: [rec])
    conf = tmp_path / "config.toml"
    conf.write_text('reports_dir = "reports"\nlog_dir = "logs"\n', encoding="utf-8")
    base = ["--config", str(conf), "--db", str(tmp_path / "cli.db")]
    return cli, rec, base


def test_cli_alert_sends_a_one_off_message(cli_env):
    cli, rec, base = cli_env
    assert cli.main(base + ["alert", "Run failed", "see the log"]) == 0
    assert rec.messages == [("Run failed", "see the log")]


def test_cli_health_reports_problems_and_alerts(cli_env, capsys):
    cli, rec, base = cli_env
    assert cli.main(base + ["health", "--no-notify"]) == 2
    assert rec.messages == [] and "No trading day" in capsys.readouterr().out
    assert cli.main(base + ["health"]) == 2
    assert rec.messages[0][0] == "NSE monitor: data problem"


def test_cli_backtest_and_analytics_on_real_data(cli_env, market, monkeypatch, capsys):  # noqa: F811
    cli, rec, base = cli_env
    source, cal, _ = market
    monkeypatch.setattr(cli, "create_source", lambda c: source)
    assert cli.main(base + ["replay", "--start", cal[120].isoformat(), "--end", cal[-1].isoformat()]) == 0
    capsys.readouterr()
    assert cli.main(base + ["backtest"]) == 0
    out = capsys.readouterr().out
    assert "Signals measured: 1" in out and "After RSI<30 entry" in out
    assert cli.main(base + ["analytics"]) == 0
