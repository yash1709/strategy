import pandas as pd
import pytest

from conftest import InMemorySource, dip_then_rally, frame, make_calendar, sideways
from nse_monitor.data.base import Instrument
from nse_monitor.data.yahoo import parse_etf_list
from nse_monitor.reports import active_table
from test_strategy import START, expected_cycle


def test_parse_etf_list_excludes_liquid_etfs():
    raw = pd.DataFrame({
        "Symbol": ["NIFTYBEES", "LIQUIDBEES", "GOLDBEES"],
        "Underlying Asset": ["Nifty 50", "Government Securities", "Gold"],
        "SecurityName": ["NIPINDETFNIFTYBEES", "NIPINDETFLIQUIDBEES", "NIPINDETFGOLDBEES"],
        "DateofListing": ["08-Jan-02", "16-Jul-03", "08-Mar-07"],
        "MarketLot": [1, 1, 1],
        "ISINNumber": ["INF204KB14I2", "INF732E01037", "INF204KB17I5"],
        "FaceValue": [1.0, 1000.0, 1.0],
        "ETF Underlying": ["EQUITY", "DEBT", "COMMODITY"],
        "Underlying Key": ["Nifty 50", "Overnight ETFs and Liquid ETF", "GOLD"],
    })
    etfs = parse_etf_list(raw, ["Overnight ETFs and Liquid ETF"])
    assert [e.symbol for e in etfs] == ["NIFTYBEES", "GOLDBEES"]
    assert all(e.kind == "ETF" for e in etfs)
    assert etfs[0].company == "NIPINDETFNIFTYBEES [Nifty 50]"
    assert etfs[0].listing_date.isoformat() == "2002-01-08"


def test_etf_is_tracked_like_a_stock_and_listed_after_stocks(make_engine):
    closes = dip_then_rally()
    cal = make_calendar(START, len(closes))
    exp = expected_cycle(closes, cal)
    instruments = [Instrument("DIPETF", "Dip ETF [Nifty 50]", kind="ETF"),
                   Instrument("DIPCO", "Dip Co Ltd")]
    prices = {"DIPETF": frame(cal, closes), "DIPCO": frame(cal, closes)}
    for k in range(10):
        instruments.append(Instrument(f"FILL{k}", f"Filler {k}"))
        prices[f"FILL{k}"] = frame(cal, sideways(len(cal), base=40.0 + k))
    source = InMemorySource(instruments, prices, cal)
    source.shares = {"DIPCO": 1e8}
    asked = []
    original = source.fetch_shares_outstanding
    source.fetch_shares_outstanding = lambda syms: asked.extend(syms) or original(syms)

    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[cal.index(exp["start"]) + 1], start=cal[120])
    table = active_table(repo)
    assert list(table["Symbol"]) == ["DIPCO", "DIPETF"]  # ETF has no market cap -> after stocks
    assert list(table["Type"]) == ["EQUITY", "ETF"]
    assert pd.isna(table.loc[1, "Market Cap / AUM (₹ Cr)"])
    assert "DIPETF" not in asked  # no pointless share-count lookups for ETFs

    engine.run(as_of=cal[-1])
    etf = next(h for h in repo.history() if h["symbol"] == "DIPETF")
    assert etf["status"] == "SMA50_CROSSED" and etf["kind"] == "ETF"
    assert etf["crossover_date"] == exp["cross"].isoformat()
    assert etf["trading_days_taken"] == exp["days"]


def test_amfi_parsers():
    from nse_monitor.data.amfi import parse_aum_table, parse_nav_isins, short_period
    nav = ("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date\n"
           "\nOpen Ended Schemes(Other Scheme - Index Funds)\n\nNippon India Mutual Fund\n\n"
           "140084;INF204KB14I2;-;Nippon India ETF Nifty 50 BeES;Direct Plan;;267.4352;23-Sep-2026\n"
           "146515;INF204KB1X41;INF204KB1X33;Some FoF;Direct Plan;IDCW Option;27.39;23-Sep-2026\n")
    assert parse_nav_isins(nav) == {"INF204KB14I2": 140084, "INF204KB1X41": 146515, "INF204KB1X33": 146515}
    table = {"data": [{"Mfname": "Nippon", "schemes": [
        {"AMFI_Code": 140084, "AverageAumForTheMonth": {
            "ExcludingFundOfFundsDomesticButIncludingFundOfFundsOverseas": 6303198.09, "FundOfFundsDomestic": 100.0}}]}]}
    assert parse_aum_table(table) == {140084: pytest.approx((6303198.09 + 100.0) * 1e5)}  # Rs lakh -> Rs
    assert short_period("April - June 2026") == "Apr-Jun 2026"


def test_etf_ranked_by_aum_with_stocks_and_frozen_at_crossover(make_engine):
    closes = dip_then_rally()
    cal = make_calendar(START, len(closes))
    exp = expected_cycle(closes, cal)
    instruments = [Instrument("DIPETF", "Dip ETF [Nifty 50]", isin="INF000000001", kind="ETF"),
                   Instrument("DIPCO", "Dip Co Ltd")]
    prices = {"DIPETF": frame(cal, closes), "DIPCO": frame(cal, closes)}
    for k in range(10):
        instruments.append(Instrument(f"FILL{k}", f"Filler {k}"))
        prices[f"FILL{k}"] = frame(cal, sideways(len(cal), base=40.0 + k))
    source = InMemorySource(instruments, prices, cal)
    source.shares = {"DIPCO": 1e8}                       # market cap ~ Rs 70-100 Cr
    source.fetch_fund_aum = lambda isins: ("Apr-Jun 2026", {"INF000000001": 5e11})  # AUM Rs 50,000 Cr

    engine, repo, notifier = make_engine(source)
    engine.run(as_of=cal[cal.index(exp["start"]) + 1], start=cal[120])
    table = active_table(repo)
    assert list(table["Symbol"]) == ["DIPETF", "DIPCO"]  # bigger size first, stocks and ETFs together
    assert table.loc[0, "Market Cap / AUM (₹ Cr)"] == 50000
    assert table.loc[0, "Size Basis"] == "AUM (AMFI avg Apr-Jun 2026)"
    assert table.loc[1, "Size Basis"] == "Market cap"

    engine.run(as_of=cal[-1])
    etf = next(h for h in repo.history() if h["symbol"] == "DIPETF")
    assert etf["market_cap_on_crossover"] == 5e11
    body = next(b for s, b in notifier.messages if "DIPETF" in s)
    assert "AUM: ₹50,000 Cr" in body


def test_history_latest_exit_first_then_size_and_dashboard_shows_30_above_active(tmp_path):
    from datetime import date, timedelta
    from nse_monitor.reports import export_reports, history_table
    from nse_monitor.storage import Repository
    repo = Repository(tmp_path / "h.db")
    base = date(2026, 8, 3)
    with repo.transaction():
        for n in range(35):  # 35 closed records, exit dates spread over 7 days, sizes varying
            exit_d = base + timedelta(days=n % 7)
            tid = repo.add_active(f"S{n:02d}", f"Co {n}", base, 25.0, 100.0, "ACTIVE")
            rec = next(r for r in repo.active() if r["id"] == tid)
            repo.close_tracking(rec, "SMA50_CROSSED", exit_date=exit_d, crossover_date=exit_d, cmp=100.0,
                                sma50=99.0, days_taken=3, market_cap=float(n) * 1e9)
        repo.add_active("LIVE1", "Still Tracked", base, 25.0, 100.0, "ACTIVE")
    hist = history_table(repo)
    exits = [date.fromisoformat(r["exit_date"]) for r in repo.history()]
    assert exits == sorted(exits, reverse=True)  # latest exit first
    same_day = [r["market_cap_on_crossover"] for r in repo.history() if r["exit_date"] == exits[0].isoformat()]
    assert same_day == sorted(same_day, reverse=True)  # then largest size
    assert len(hist) == 35  # table/CSV keep everything

    html = export_reports(repo, tmp_path / "rep")[2].read_text(encoding="utf-8")
    assert html.index("Historical crossover database") < html.index("Active tracking list")
    hist_html = html[html.index("Historical crossover database"):html.index("Active tracking list")]
    assert hist_html.count("<tr>") == 1 + 30  # header + latest 30 rows
    assert "latest 30 of 35" in hist_html
