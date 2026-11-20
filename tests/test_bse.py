"""BSE-only stocks: parsing the BSE bhavcopy, building the universe without double counting NSE names,
merging official closes, and keeping illiquid BSE names from holding up a trading day."""
from datetime import date

import pandas as pd
import pytest

from conftest import InMemorySource
from nse_monitor.data import bse
from nse_monitor.data.base import Instrument, is_bse
from nse_monitor.data.yahoo import YahooNseSource
from test_strategy import market  # noqa: F401  (fixture)

HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
          "OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
          "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def row(code, isin, ticker, group, name, close, volume, day="2026-11-20", tp="STK"):
    return (f"{day},{day},CM,BSE,{tp},{code},{isin},{ticker},{group},,,,,{name},{close},{close},{close},{close},{close},"
            f"{close},,{close},,,{volume},1000.00,10,F1,1,,,,,")


SAMPLE = "\n".join([
    HEADER,
    row(500002, "INE117A01022", "ABB", "A", "ABB INDIA LIMITED", "6915.00", 12389),            # also on NSE
    row(500012, "INE000012345", "ANDHRAPET", "X", "ANDHRA PETROCHEMICALS LTD.", "38.66", 15389),
    row(500051, "INE000051234", " BHAGGAS ", "XT", "BHAGAWATI GAS LIMITED", "9.20", 47843),
    row(540001, "INE000540001", "SMECO", "M", "SOME SME CO", "55.00", 100),                    # SME group: excluded
    row(500099, "INE000099999", "ZEROCO", "B", "ZERO PRICE CO", "0.00", 10),                    # no usable close
    row(500100, "INE000100000", "ANDHRAPET", "B", "DUPLICATE TICKER", "40.00", 5),              # duplicate ticker
    row(700001, "INE000700001", "NOTSTK", "X", "NOT A STOCK", "10.00", 5, tp="DEB"),
    "",
])


def test_parse_bars_keeps_wanted_groups_and_suffixes_symbols():
    bars = bse.parse_bars(SAMPLE, expected=date(2026, 11, 20))
    assert list(bars.index) == ["ABB.BO", "ANDHRAPET.BO", "BHAGGAS.BO"]  # no SME, zero price, debt, or duplicate
    assert bars.loc["ANDHRAPET.BO", "close"] == 38.66 and bars.loc["BHAGGAS.BO", "volume"] == 47843
    assert list(bse.parse_bars(SAMPLE, groups=("M",)).index) == ["SMECO.BO"]


def test_parse_bars_rejects_the_wrong_day_and_html_pages():
    with pytest.raises(ValueError):
        bse.parse_bars(SAMPLE, expected=date(2026, 11, 19))
    with pytest.raises(ValueError):
        bse.parse_bars("<!DOCTYPE html><html>nothing published</html>")


def test_parse_instruments_carry_isin_group_and_exchange():
    out = bse.parse_instruments(SAMPLE)
    assert [i.symbol for i in out] == ["ABB.BO", "ANDHRAPET.BO", "BHAGGAS.BO"]
    first = out[1]
    assert first.exchange == "BSE" and first.isin == "INE000012345" and first.series == "X" and first.kind == "EQUITY"
    assert all(is_bse(i.symbol) for i in out) and not is_bse("ABB")


class _Resp:
    def __init__(self, content=b"", status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_download_treats_html_and_404_as_not_published(monkeypatch):
    html = _Resp(b"<!DOCTYPE html><html></html>")  # what BSE serves on a weekend or holiday, with HTTP 200
    monkeypatch.setattr(bse.requests, "get", lambda *a, **k: html)
    assert bse.fetch_bars(date(2026, 11, 21)) is None
    monkeypatch.setattr(bse.requests, "get", lambda *a, **k: _Resp(status=404))
    assert bse.fetch_bars(date(2026, 11, 21)) is None
    monkeypatch.setattr(bse.requests, "get", lambda *a, **k: _Resp(SAMPLE.encode("utf-8-sig")))
    assert len(bse.fetch_bars(date(2026, 11, 20))) == 3


def test_latest_instruments_walks_back_over_weekends_and_gives_up(monkeypatch):
    asked = []

    def fake(d, timeout=60):
        asked.append(d)
        return SAMPLE if d == date(2026, 11, 20) else None

    monkeypatch.setattr(bse, "_download", fake)
    out = bse.latest_instruments(today=date(2026, 11, 22))  # a Sunday: Saturday and Sunday are skipped
    assert len(out) == 3 and asked == [date(2026, 11, 20)]
    monkeypatch.setattr(bse, "_download", lambda d, timeout=60: None)
    with pytest.raises(RuntimeError):
        bse.latest_instruments(today=date(2026, 11, 22), lookback_days=3)


NSE_EQUITY = ("SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
              "ABB,ABB India Limited,EQ,01-Jan-2000,2,1,INE117A01022,2\n")
NSE_ETF = ("Symbol,SecurityName,DateofListing,ISINNumber,Underlying Key\n"
           "LIQUIDBEES,NIPPON LIQUID,16-Jul-03,INF732E01037,Overnight ETFs and Liquid ETF\n")


def make_source(monkeypatch, **kw):
    src = YahooNseSource(["EQ"], include_bse=True, **kw)
    monkeypatch.setattr(src, "_get", lambda url: NSE_EQUITY if "EQUITY_L" in url else NSE_ETF)
    monkeypatch.setattr(bse, "_download", lambda d, timeout=60: SAMPLE)
    return src


def test_universe_tracks_a_dual_listed_company_once_and_only_on_nse(monkeypatch):
    out = make_source(monkeypatch).list_instruments()
    symbols = [i.symbol for i in out]
    assert "ABB" in symbols and "ABB.BO" not in symbols            # same ISIN: NSE wins
    assert "ANDHRAPET.BO" in symbols and "BHAGGAS.BO" in symbols   # BSE-only
    assert next(i for i in out if i.symbol == "ABB").exchange == "NSE"


def test_bse_is_off_unless_enabled(monkeypatch):
    src = make_source(monkeypatch)
    src.include_bse = False
    assert not any(is_bse(i.symbol) for i in src.list_instruments())


def test_bse_outage_raises_so_the_cached_universe_is_kept(monkeypatch):
    src = make_source(monkeypatch)
    monkeypatch.setattr(bse, "_download", lambda d, timeout=60: None)
    with pytest.raises(RuntimeError):
        src.list_instruments()


def test_yahoo_tickers_for_bse_and_nse():
    assert YahooNseSource._ticker("ANDHRAPET.BO") == "ANDHRAPET.BO"
    assert YahooNseSource._ticker("RELIANCE") == "RELIANCE.NS"


def test_official_day_merges_nse_and_bse_and_survives_a_bse_failure(monkeypatch):
    from nse_monitor.data import bhavcopy
    nse = pd.DataFrame({"close": [100.0], "volume": [5.0]}, index=["ABB"])
    monkeypatch.setattr(bhavcopy, "fetch_bhavcopy", lambda d, series: nse)
    src = YahooNseSource(["EQ"], include_bse=True)
    monkeypatch.setattr(bse, "_download", lambda d, timeout=60: SAMPLE)
    merged = src.fetch_official_day(date(2026, 11, 20))
    assert set(merged.index) == {"ABB", "ABB.BO", "ANDHRAPET.BO", "BHAGGAS.BO"}

    def boom(d, timeout=60):
        raise ConnectionError("bse down")

    monkeypatch.setattr(bse, "_download", boom)
    assert list(src.fetch_official_day(date(2026, 11, 20)).index) == ["ABB"]
    src.include_bse = False
    assert list(src.fetch_official_day(date(2026, 11, 20)).index) == ["ABB"]


# ---------------------------------------------------------------- engine behaviour with BSE-only names
def with_bse(source, cal, days_missing):
    """Add four BSE-only stocks whose bars are missing on ``days_missing`` (illiquid names often are)."""
    from conftest import frame, sideways
    for k in range(4):
        sym = f"THIN{k}.BO"
        source.instruments.append(Instrument(sym, f"Thin {k}", series="X", isin=f"INE000T{k:05d}", exchange="BSE"))
        df = frame(cal, sideways(len(cal), base=20.0 + k))
        source.prices[sym] = df.drop(index=[d for d in days_missing if d in df.index])


def test_illiquid_bse_names_do_not_defer_a_trading_day(market, make_engine):  # noqa: F811
    source, cal, _ = market
    engine, repo, _ = make_engine(source)
    engine.run(as_of=cal[130], start=cal[125])
    with_bse(source, cal, [cal[131]])      # 12 NSE stocks have the day, all 4 BSE names do not: 75% of everything
    res = engine.run(as_of=cal[131])
    assert res.processed_days == [cal[131]], res.skipped_reason


def test_bse_names_are_screened_tracked_and_labelled(make_engine):
    from conftest import dip_then_rally, frame, make_calendar, sideways
    from test_strategy import START, expected_cycle
    closes = dip_then_rally()
    cal = make_calendar(START, len(closes))
    exp = expected_cycle(closes, cal)
    instruments = [Instrument("DIPBSE.BO", "Dip BSE Ltd", series="X", isin="INE000D00001", exchange="BSE")]
    prices = {"DIPBSE.BO": frame(cal, closes)}
    for k in range(10):
        instruments.append(Instrument(f"FILL{k}", f"Filler {k}"))
        prices[f"FILL{k}"] = frame(cal, sideways(len(cal), base=40.0 + k))
    engine, repo, notifier = make_engine(InMemorySource(instruments, prices, cal))
    engine.run(as_of=cal[-1], start=cal[120])
    [h] = repo.history()
    assert h["symbol"] == "DIPBSE.BO" and h["rsi_date"] == exp["rsi_date"].isoformat()
    from nse_monitor.reports import active_table, history_table
    assert list(history_table(repo)["Exchange"]) == ["BSE"]
    assert list(active_table(repo)["Exchange"]) == []
    # market mood is measured on NSE stocks only, so the lone BSE name does not count towards it
    assert max(r["oversold"] for r in repo.breadth_history(500)) == 0
    assert all(r["stocks"] == 10 for r in repo.breadth_history(500))


def test_nse_only_counts_leave_out_bse_bars(tmp_path):
    from nse_monitor.storage import Repository
    repo = Repository(tmp_path / "c.db")
    bars = pd.DataFrame({"close": [1.0], "volume": [1.0]}, index=[date(2026, 11, 2)])
    repo.upsert_prices("ABB", bars)
    repo.upsert_prices("THIN.BO", bars)
    assert repo.bar_counts_by_date(date(2026, 11, 1), date(2026, 11, 3)) == {date(2026, 11, 2): 2}
    assert repo.bar_counts_by_date(date(2026, 11, 1), date(2026, 11, 3), nse_only=True) == {date(2026, 11, 2): 1}
