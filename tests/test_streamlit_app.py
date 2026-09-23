from datetime import date, timedelta
from pathlib import Path

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
        repo.conn.execute("INSERT INTO instruments(symbol, company, kind, is_listed) VALUES ('GOLDETF','Gold ETF','ETF',1)")
        repo.mark_processed(base + timedelta(days=6), {})
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
    assert headers == ["Historical crossover database", "Active tracking list"]
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


def test_dashboard_without_data_source_explains(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.setenv("NSE_MONITOR_DB", str(tmp_path / "missing.db"))
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert at.error and "GITHUB_REPO" in at.error[0].value
