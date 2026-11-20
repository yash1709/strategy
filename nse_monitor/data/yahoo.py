"""Default source: NSE's official equity list + Yahoo Finance daily prices.

* Universe: https://archives.nseindia.com/content/equities/EQUITY_L.csv (stocks)
            https://archives.nseindia.com/content/equities/eq_etfseclist.csv (ETFs)
* Prices:   yfinance, ticker ``<SYMBOL>.NS``. Yahoo's ``Close`` is adjusted for
  splits/bonuses (not dividends), which is what we want for RSI / SMA.
* Calendar: sessions of the NIFTY 50 index (``^NSEI``).
* ETF AUM:  AMFI (see amfi.py).
* Gap fill: NSE bhavcopy for recent sessions Yahoo has not completed yet (see bhavcopy.py).
"""
from __future__ import annotations

import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Iterable

import pandas as pd
import requests

from . import bse
from .base import Instrument, MarketDataSource, is_bse, normalize_bars

log = logging.getLogger(__name__)

EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
ETF_LIST_URL = "https://archives.nseindia.com/content/equities/eq_etfseclist.csv"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,*/*",
}


class YahooNseSource(MarketDataSource):
    name = "yahoo"

    def __init__(self, series: list[str], batch_size: int = 100, reference_symbol: str = "^NSEI",
                 retries: int = 3, include_etfs: bool = True, etf_exclude_categories: list[str] | None = None,
                 include_bse: bool = False, bse_groups: list[str] | None = None):
        self.series = {s.upper() for s in series}
        self.include_etfs = include_etfs
        self.etf_exclude_categories = etf_exclude_categories or []
        self.include_bse = include_bse
        self.bse_groups = tuple(g.strip().upper() for g in (bse_groups or bse.DEFAULT_GROUPS))
        self.batch_size = batch_size
        self.reference_symbol = reference_symbol
        self.retries = retries

    # -- universe -----------------------------------------------------------
    def list_instruments(self) -> list[Instrument]:
        """Stocks, plus ETFs if enabled. If either list fails to download this raises, so
        the engine keeps the cached universe instead of wrongly marking symbols as delisted."""
        out = self._equities()
        if self.include_etfs:
            etfs = parse_etf_list(pd.read_csv(io.StringIO(self._get(ETF_LIST_URL))), self.etf_exclude_categories)
            log.info("NSE ETF list: %d ETFs", len(etfs))
            out += etfs
        if self.include_bse:
            out += self._bse_only(out)
        return out

    def _bse_only(self, nse: list[Instrument]) -> list[Instrument]:
        """BSE stocks that are not also listed on NSE (matched by ISIN: NSE is preferred, so a
        dual-listed company is tracked once). Raises if the BSE file is unavailable, so the engine keeps
        its cached universe rather than treating every BSE stock as delisted."""
        # the NSE ETF list may exclude liquid ETFs; match against the full ETF list so those are not
        # picked up as "BSE-only" either
        known = {i.isin for i in nse if i.isin}
        try:
            known |= {str(x).strip() for x in pd.read_csv(io.StringIO(self._get(ETF_LIST_URL)))["ISINNumber"]}
        except Exception as exc:
            log.debug("Could not read the full NSE ETF list for de-duplication: %s", exc)
        found = bse.latest_instruments(self.bse_groups)
        only = [i for i in found if i.isin not in known]
        log.info("BSE: %d stocks in the chosen groups, %d are not listed on NSE", len(found), len(only))
        return only

    def _get(self, url: str) -> str:
        resp = self._retry(lambda: requests.get(url, headers=_HEADERS, timeout=30))
        resp.raise_for_status()
        return resp.text

    def _equities(self) -> list[Instrument]:
        df = pd.read_csv(io.StringIO(self._get(EQUITY_LIST_URL)))
        df.columns = [c.strip().upper() for c in df.columns]
        out = []
        for row in df.itertuples(index=False):
            rec = dict(zip(df.columns, row))
            series = str(rec.get("SERIES", "")).strip().upper()
            if self.series and series not in self.series:
                continue
            listing = pd.to_datetime(rec.get("DATE OF LISTING"), format="%d-%b-%Y", errors="coerce")
            out.append(Instrument(
                symbol=str(rec["SYMBOL"]).strip(),
                company=str(rec.get("NAME OF COMPANY", "")).strip(),
                series=series,
                isin=str(rec.get("ISIN NUMBER", "")).strip(),
                listing_date=None if pd.isna(listing) else listing.date(),
            ))
        log.info("NSE equity list: %d instruments (series %s)", len(out), sorted(self.series))
        return out

    # -- prices -------------------------------------------------------------
    def fetch_daily_bars(self, symbols: Iterable[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        symbols = list(symbols)
        result: dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), self.batch_size):
            batch = symbols[i:i + self.batch_size]
            tickers = {self._ticker(s): s for s in batch}
            raw = self._retry(lambda: self._download(list(tickers), start, end))
            for ticker, symbol in tickers.items():
                frame = _extract(raw, ticker, single=len(tickers) == 1)
                if frame is not None:
                    bars = normalize_bars(frame)
                    bars = bars[(bars.index >= start) & (bars.index <= end)]
                    if not bars.empty:
                        result[symbol] = bars
            log.info("Prices: batch %d-%d of %d fetched", i + 1, i + len(batch), len(symbols))
        return result

    def fetch_trading_days(self, start: date, end: date) -> list[date]:
        raw = self._retry(lambda: self._download([self.reference_symbol], start, end))
        frame = _extract(raw, self.reference_symbol, single=True)
        bars = normalize_bars(frame) if frame is not None else None
        if bars is None or bars.empty:
            return []
        return [d for d in bars.index if start <= d <= end]

    def fetch_shares_outstanding(self, symbols: Iterable[str]) -> dict[str, float]:
        import yfinance as yf

        def one(symbol: str) -> tuple[str, float | None]:
            try:
                shares = yf.Ticker(self._ticker(symbol)).fast_info["shares"]
                return symbol, float(shares) if shares and shares > 0 else None
            except Exception as exc:
                log.debug("Shares outstanding unavailable for %s: %s", symbol, exc)
                return symbol, None

        symbols = list(symbols)
        with ThreadPoolExecutor(max_workers=8) as pool:
            found = {s: v for s, v in pool.map(one, symbols) if v}
        log.info("Shares outstanding: %d of %d symbols", len(found), len(symbols))
        return found

    def fetch_sectors(self, symbols: Iterable[str]) -> dict[str, tuple[str, str]]:
        import yfinance as yf

        def one(symbol: str) -> tuple[str, tuple[str, str] | None]:
            try:
                info = yf.Ticker(self._ticker(symbol)).info or {}
                return symbol, (info.get("sector") or "Unknown", info.get("industry") or "")
            except Exception as exc:
                log.debug("Sector unavailable for %s: %s", symbol, exc)
                return symbol, None

        symbols = list(symbols)
        with ThreadPoolExecutor(max_workers=6) as pool:
            found = {s: v for s, v in pool.map(one, symbols) if v}
        log.info("Sectors: %d of %d symbols", len(found), len(symbols))
        return found

    def fetch_news(self, symbol: str, limit: int = 8) -> list[str]:
        import yfinance as yf

        out: list[str] = []
        for item in (yf.Ticker(self._ticker(symbol)).news or [])[:limit]:
            content = item.get("content") if isinstance(item.get("content"), dict) else item
            title = (content.get("title") or "").strip()
            when = str(content.get("pubDate") or content.get("displayTime") or "")[:10]
            if title:
                out.append(f"{when}: {title}" if when else title)
        return out

    def fetch_official_day(self, d: date) -> pd.DataFrame | None:
        """NSE's end-of-day file, plus BSE's for the BSE-only stocks. A BSE failure never blocks the NSE bars."""
        from .bhavcopy import fetch_bhavcopy

        parts = [fetch_bhavcopy(d, self.series)]
        if self.include_bse:
            try:
                parts.append(bse.fetch_bars(d, self.bse_groups))
            except Exception as exc:
                log.warning("BSE end-of-day file for %s unavailable: %s", d, exc)
        parts = [p for p in parts if p is not None and len(p)]
        return pd.concat(parts) if parts else None

    def fetch_fund_aum(self, isins: Iterable[str]) -> tuple[str | None, dict[str, float]]:
        from .amfi import AmfiClient

        snap = AmfiClient().fetch_etf_aum(isins)
        return snap.period, snap.aum_by_isin

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _ticker(symbol: str) -> str:
        return symbol if is_bse(symbol) else f"{symbol}.NS"  # BSE symbols already end in .BO, Yahoo's BSE suffix

    @staticmethod
    def _download(tickers: list[str], start: date, end: date) -> pd.DataFrame:
        import yfinance as yf  # imported lazily so other sources don't need it

        return yf.download(
            tickers=tickers,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),  # yfinance end is exclusive
            interval="1d",
            auto_adjust=False,
            actions=False,
            group_by="ticker",
            threads=True,
            progress=False,
        )

    def _retry(self, fn):
        delay = 2.0
        for attempt in range(1, self.retries + 1):
            try:
                return fn()
            except Exception as exc:  # network hiccups, rate limits
                if attempt == self.retries:
                    raise
                log.warning("Attempt %d failed (%s); retrying in %.0fs", attempt, exc, delay)
                time.sleep(delay)
                delay *= 2


def parse_etf_list(df: pd.DataFrame, exclude_categories: list[str]) -> list[Instrument]:
    """NSE eq_etfseclist.csv -> Instruments. Company = scheme name with its underlying."""
    df = df.rename(columns=lambda c: c.strip())
    excluded = {c.strip().lower() for c in exclude_categories}
    out = []
    for r in df.to_dict("records"):
        category = str(r.get("Underlying Key", "")).strip()
        if category.lower() in excluded:
            continue
        listing = pd.to_datetime(r.get("DateofListing"), format="%d-%b-%y", errors="coerce")
        name = str(r.get("SecurityName", "")).strip()
        out.append(Instrument(
            symbol=str(r["Symbol"]).strip(),
            company=f"{name} [{category}]" if category else name,
            series="EQ",
            isin=str(r.get("ISINNumber", "")).strip(),
            listing_date=None if pd.isna(listing) else listing.date(),
            kind="ETF",
        ))
    return out


def _extract(raw: pd.DataFrame | None, ticker: str, single: bool) -> pd.DataFrame | None:
    """Pull one ticker's Close/Volume out of a yfinance download frame."""
    if raw is None or raw.empty:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = raw.columns.get_level_values(0)
        if ticker in level0:
            sub = raw[ticker]
        elif single:
            sub = raw.droplevel(1, axis=1) if ticker in raw.columns.get_level_values(1) else raw.droplevel(0, axis=1)
        else:
            return None
    elif single:
        sub = raw
    else:
        return None
    cols = {c.lower(): c for c in sub.columns}
    if "close" not in cols:
        return None
    return pd.DataFrame({
        "close": sub[cols["close"]],
        "volume": sub[cols["volume"]] if "volume" in cols else float("nan"),
    })
