from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from nse_monitor.storage import Repository  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "streamlit_app.py")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "dash.db"
    repo = Repository(path)
    base = date(2026, 8, 3)
    with repo.transaction():
        for n in range(35):
            exit_d = base + timedelta(days=n % 7)
            tid = repo.add_active(f"S{n:02d}", f"Company {n}", base, 25.0, 100.0, "ACTIVE")
            rec = next(r for r in repo.active() if r["id"] == tid)
            repo.close_tracking(rec, "SMA50_CROSSED", exit_date=exit_d, crossover_date=exit_d, cmp=100.0,
                                sma50=99.0, days_taken=3 + n % 5, market_cap=float(n + 1) * 1e9, volume=1000.0)
        for sym, name, size in [("BIGCO", "Big Co", 5e12), ("GOLDETF", "Gold ETF [GOLD]", 2e11)]:
            repo.add_active(sym, name, base, 25.0, 100.0, "ACTIVE")
            repo.conn.execute("UPDATE active_tracking SET market_cap=?, cmp=100, volume=500 WHERE symbol=?", (size, sym))
        repo.conn.execute("INSERT INTO instruments(symbol, company, kind, is_listed, sector) "
                          "VALUES ('GOLDETF','Gold ETF','ETF',1,'GOLD')")
        repo.mark_processed(date.today(), {}, coverage=0.99)  # fresh, so the health banner stays quiet
        repo.save_breadth(date.today(), 1000, 150, 80, 400, 500, 900)
        repo.save_signals(date.today(), [("BIGCO", "GOLDEN_CROSS", 101.0), ("GOLDETF", "HIGH_52W", 55.0)])
        repo.conn.execute("UPDATE active_tracking SET quality=72, quality_notes='RSI 18; stock-specific', sma50=104 "
                          "WHERE symbol='BIGCO'")
        repo.conn.execute("INSERT INTO instruments(symbol, company, kind, is_listed, sector) "
                          "VALUES ('BIGCO','Big Co','EQUITY',1,'Industrials')")
        repo.replace_chart_bars("BIGCO", pd.DataFrame({
            "date": [date.today() - timedelta(days=n) for n in range(60, 0, -1)],
            "close": [100 + n * 0.1 for n in range(60)], "sma50": [101.0] * 60,
            "rsi": [25 + n * 0.5 for n in range(60)]}))
        repo.save_ai_note("BIGCO", base, "LOW", "Looks like a sector-wide dip.")
    repo.close()
    return path


def _run(db, monkeypatch):
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.setenv("NSE_MONITOR_DB", str(db))
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def test_dashboard_renders_history_first_limited_to_30(db, monkeypatch):
    at = _run(db, monkeypatch)
    headers = [h.value for h in at.subheader]
    assert headers[:2] == ["Historical crossover database", "Active tracking list"]
    assert {"Stock chart", "Market mood", "What happened after each signal", "Data health", "Ask your data"} <= set(headers)
    hist_df, active_df = at.dataframe[0].value, at.dataframe[1].value
    assert len(hist_df) == 30
    assert hist_df["Exit Date"].is_monotonic_decreasing
    assert list(active_df["Symbol"][:2]) == ["BIGCO", "GOLDETF"]
    assert at.metric[0].value == "2"  # active
    assert not [w for w in at.warning]  # no deprecation or runtime warnings rendered

    at.toggle[0].set_value(True).run()
    assert len(at.dataframe[0].value) == 35


def test_dashboard_filters(db, monkeypatch):
    at = _run(db, monkeypatch)
    at.selectbox[0].set_value("ETF").run()
    assert list(at.dataframe[1].value["Symbol"]) == ["GOLDETF"]
    at.selectbox[0].set_value("All").run()
    at.text_input[0].set_value("big").run()
    assert list(at.dataframe[1].value["Symbol"]) == ["BIGCO"]


def test_active_list_has_quality_sector_and_distance_to_sma(db, monkeypatch):
    at = _run(db, monkeypatch)
    active_df = at.dataframe[1].value
    big = active_df[active_df["Symbol"] == "BIGCO"].iloc[0]
    assert big["Quality"] == 72 and big["Sector"] == "Industrials"
    assert big["% Below SMA 50"] == pytest.approx(3.85, abs=0.01)  # (104 - 100) / 104
    assert big["News Risk"] == "LOW"


def test_closest_to_sma_order_and_sector_filter_and_compact_view(db, monkeypatch):
    at = _run(db, monkeypatch)
    at.selectbox[1].set_value("Closest to SMA 50").run()
    assert list(at.dataframe[1].value["Symbol"]) == ["BIGCO"]  # only BIGCO has an SMA 50 and sits below it
    at.selectbox[1].set_value("Size (default)").run()
    at.selectbox[2].set_value("Industrials").run()
    assert list(at.dataframe[1].value["Symbol"]) == ["BIGCO"]
    at.selectbox[2].set_value("All").run()
    compact = [t for t in at.toggle if t.label == "Compact view"][0]
    compact.set_value(True).run()
    assert "Company" not in at.dataframe[1].value.columns and "Symbol" in at.dataframe[1].value.columns


def test_watchlist_only_shows_picked_symbols(db, monkeypatch):
    at = _run(db, monkeypatch)
    at.multiselect(key="watch").set_value(["GOLDETF"]).run()
    [t for t in at.toggle if t.label == "Watchlist only"][0].set_value(True).run()
    assert list(at.dataframe[1].value["Symbol"]) == ["GOLDETF"]


def test_chart_mood_and_signal_panels_render(db, monkeypatch):
    at = _run(db, monkeypatch)
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "Below RSI 30" in labels and "Above SMA 200" in labels
    assert [m.value for m in at.metric if m.label == "Below RSI 30"] == ["15.0%"]
    sig = [s for s in at.selectbox if s.label == "Signal"][0]
    sig.set_value("GOLDEN_CROSS").run()
    shown = [d.value for d in at.dataframe if "Signal" not in d.value.columns and "Value" in d.value.columns][-1]
    assert list(shown["Symbol"]) == ["BIGCO"]
    chart_pick = [s for s in at.selectbox if s.label == "Stock"][0]
    assert chart_pick.options[0] == "BIGCO"  # tracked, with chart data
    assert any("News risk LOW" in md.value for md in at.markdown)


def test_ask_ai_needs_a_key_and_applies_a_valid_filter(db, monkeypatch):
    at = _run(db, monkeypatch)
    assert any("Not set up" in i.value for i in at.info)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from nse_monitor import ai
    monkeypatch.setattr(ai, "question_to_spec", lambda q, tables, cfg, key=None, client=None: {
        "table": "active", "filters": [{"column": "Type", "op": "==", "value": "ETF"}],
        "explanation": "ETFs being tracked", "limit": 10})
    at = _run(db, monkeypatch)
    at.text_input(key="ask_question").set_value("which etfs?")
    at.button(key="ask_submit").click().run()
    assert not at.exception, at.exception
    assert not at.error
    assert any("ETFs being tracked" in md.value for md in at.markdown)
    assert list(at.dataframe[-1].value["Symbol"]) == ["GOLDETF"]


def test_ask_ai_rejects_an_invalid_filter_politely(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from nse_monitor import ai
    monkeypatch.setattr(ai, "question_to_spec", lambda *a, **k: {"table": "active", "filters": [
        {"column": "DROP TABLE", "op": "==", "value": 1}]})
    at = _run(db, monkeypatch)
    at.text_input(key="ask_question").set_value("anything")
    at.button(key="ask_submit").click().run()
    assert not at.exception
    assert any("could not turn that into a valid filter" in e.value for e in at.error)


def test_exchange_filter_separates_bse_only_stocks(tmp_path, monkeypatch):
    path = tmp_path / "bse.db"
    repo = Repository(path)
    base = date(2026, 8, 3)
    with repo.transaction():
        repo.add_active("RELIANCE", "Reliance", base, 25.0, 100.0, "ACTIVE")
        repo.add_active("ANDHRAPET.BO", "Andhra Petrochemicals", base, 25.0, 40.0, "ACTIVE")
        repo.mark_processed(date.today(), {}, coverage=1.0)
    repo.close()
    at = _run(path, monkeypatch)
    table = at.dataframe[1].value
    assert dict(zip(table["Symbol"], table["Exchange"])) == {"RELIANCE": "NSE", "ANDHRAPET.BO": "BSE"}
    exchange = [s for s in at.selectbox if s.label == "Exchange"][0]
    exchange.set_value("BSE").run()
    assert list(at.dataframe[1].value["Symbol"]) == ["ANDHRAPET.BO"]
    exchange.set_value("NSE").run()
    assert list(at.dataframe[1].value["Symbol"]) == ["RELIANCE"]


def test_stale_data_shows_a_health_warning(tmp_path, monkeypatch):
    path = tmp_path / "stale.db"
    repo = Repository(path)
    repo.mark_processed(date.today() - timedelta(days=20), {}, coverage=0.99)
    repo.close()
    at = _run(path, monkeypatch)
    assert any("stale" in w.value for w in at.warning)


def test_dashboard_without_data_source_explains(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.setenv("DEFAULT_GITHUB_REPO", "")  # disable the built-in default for this test
    monkeypatch.setenv("NSE_MONITOR_DB", str(tmp_path / "missing.db"))
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert at.error and "GITHUB_REPO" in at.error[0].value


# ---------------------------------------------------------------- "Run update now" panel
class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text
        self.ok = status < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


class FakeGitHub:
    """Stands in for the GitHub API: records dispatches, reports workflow runs."""

    def __init__(self, latest_status="completed"):
        self.posts = []
        self.runs = [{"id": 1, "status": latest_status, "conclusion": "success" if latest_status == "completed" else None,
                      "created_at": "2026-09-01T12:00:00Z", "html_url": "https://github.com/o/r/actions/runs/1"}]

    def get(self, url, **kw):
        assert "/actions/workflows/daily.yml/runs" in url, url
        return _Resp(payload={"workflow_runs": self.runs[-1:]})

    def post(self, url, **kw):
        from datetime import datetime, timezone
        self.posts.append((url, kw))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.runs.append({"id": 2, "status": "completed", "conclusion": "success", "created_at": now,
                          "html_url": "https://github.com/o/r/actions/runs/2"})
        return _Resp(status=204)


def _run_app(db, monkeypatch, fake=None, configured=True):
    import requests
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.setenv("NSE_MONITOR_DB", str(db))
    monkeypatch.setenv("DEFAULT_GITHUB_REPO", "o/r")
    monkeypatch.setenv("RUN_POLL_SECONDS", "0")
    monkeypatch.setenv("WRONG_PASSWORD_DELAY", "0")
    if configured:
        monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "tok")
        monkeypatch.setenv("RUN_PASSWORD", "s3cret")
    else:
        monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
        monkeypatch.delenv("RUN_PASSWORD", raising=False)
    if fake:
        monkeypatch.setattr(requests, "get", fake.get)
        monkeypatch.setattr(requests, "post", fake.post)
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def test_run_panel_not_configured_points_to_github(db, monkeypatch):
    at = _run_app(db, monkeypatch, configured=False)
    assert any("not set up" in i.value and "o/r/actions/workflows/daily.yml" in i.value for i in at.info)


def test_run_panel_wrong_password_dispatches_nothing(db, monkeypatch):
    fake = FakeGitHub()
    at = _run_app(db, monkeypatch, fake)
    at.text_input(key="run_password").set_value("nope")
    at.button(key="run_submit").click().run()
    assert any("Wrong password" in e.value for e in at.error)
    assert fake.posts == []


def test_run_panel_starts_run_and_waits_for_success(db, monkeypatch):
    fake = FakeGitHub()
    at = _run_app(db, monkeypatch, fake)
    at.text_input(key="run_password").set_value("s3cret")
    at.button(key="run_submit").click().run()
    assert not at.exception, at.exception
    [(url, kw)] = fake.posts
    assert url == "https://api.github.com/repos/o/r/actions/workflows/daily.yml/dispatches"
    assert kw["json"] == {"ref": "main"} and kw["headers"]["Authorization"] == "Bearer tok"
    assert not at.error


def test_run_panel_blocks_second_run_while_one_is_running(db, monkeypatch):
    fake = FakeGitHub(latest_status="in_progress")
    at = _run_app(db, monkeypatch, fake)
    assert any("already running" in i.value for i in at.info)
    assert not [w for w in at.text_input if w.key == "run_password"]
