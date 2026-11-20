"""BSE (Bombay Stock Exchange): stocks that are listed on BSE but NOT on NSE.

BSE's scrip-list API refuses scripted requests (HTTP 403), but its daily end-of-day file (the BSE
"bhavcopy") downloads fine and has everything needed: every security that traded, with ISIN, ticker,
trading group, official close and volume.

    https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV

* Universe: the latest bhavcopy, restricted to the chosen trading groups. Securities whose ISIN is
  already listed on NSE are skipped by the caller, so a dual-listed company is tracked once (on NSE).
* Symbols are ``<TICKER>.BO`` (for example ``ANDHRAPET.BO``). That is also the Yahoo Finance ticker, and
  it keeps BSE names from colliding with NSE symbols.
* Prices come from Yahoo like NSE names; this file is also used to fill recent gaps and to cross-check.

BSE groups: A and B are the larger, more liquid companies; X and XT the regular small-company groups.
M/MT/MS are SME, F is debt, G government securities, Z non-compliant, T trade-for-trade. The default is
A, B, X, XT.

When BSE publishes nothing (weekend, holiday, not yet) it serves an HTML page with HTTP 200, not a 404,
so the content is checked rather than the status.
"""
from __future__ import annotations

import io
import logging
from datetime import date, timedelta

import pandas as pd
import requests

from .base import Instrument

log = logging.getLogger(__name__)

URL = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{:%Y%m%d}_F_0000.CSV"
SUFFIX = ".BO"
DEFAULT_GROUPS = ("A", "B", "X", "XT")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.bseindia.com/",
    "Accept": "text/csv,*/*",
}


def _read(text: str, groups: set[str] | list[str] | tuple[str, ...], expected: date | None = None) -> pd.DataFrame:
    """The rows of one bhavcopy for the wanted groups, with cleaned ticker / ISIN / numbers."""
    if not text.lstrip().startswith("TradDt"):
        raise ValueError("not a BSE bhavcopy")
    df = pd.read_csv(io.StringIO(text), dtype=str, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    if expected is not None and len(df):
        stamped = pd.to_datetime(df["TradDt"].iloc[0], errors="coerce")
        if pd.notna(stamped) and stamped.date() != expected:
            raise ValueError(f"BSE bhavcopy for {expected} is stamped {stamped.date()}")
    for col in ("TckrSymb", "SctySrs", "ISIN", "FinInstrmNm", "FinInstrmTp"):
        df[col] = df[col].fillna("").str.strip()
    df = df[(df["FinInstrmTp"] == "STK") & df["SctySrs"].isin(set(groups)) & (df["TckrSymb"] != "")]
    df = df.assign(close=pd.to_numeric(df["ClsPric"], errors="coerce"),
                   volume=pd.to_numeric(df["TtlTradgVol"], errors="coerce"))
    return df[df["close"].notna() & (df["close"] > 0)]


def parse_bars(text: str, groups=DEFAULT_GROUPS, expected: date | None = None) -> pd.DataFrame:
    """Bhavcopy text -> DataFrame indexed by ``TICKER.BO`` with columns close, volume."""
    df = _read(text, groups, expected)
    out = pd.DataFrame({"close": df["close"].to_numpy(), "volume": df["volume"].to_numpy()},
                       index=(df["TckrSymb"] + SUFFIX).to_numpy())
    return out[~out.index.duplicated(keep="first")]


def parse_instruments(text: str, groups=DEFAULT_GROUPS) -> list[Instrument]:
    df = _read(text, groups).drop_duplicates("TckrSymb")
    return [Instrument(symbol=r.TckrSymb + SUFFIX, company=r.FinInstrmNm, series=r.SctySrs, isin=r.ISIN,
                       listing_date=None, kind="EQUITY", exchange="BSE")
            for r in df.itertuples(index=False)]


def _download(d: date, timeout: int = 60) -> str | None:
    """The day's file text, or None if BSE has not published one for ``d``."""
    resp = requests.get(URL.format(d), headers=_HEADERS, timeout=timeout)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig", errors="replace")
    return text if text.lstrip().startswith("TradDt") else None  # weekend / holiday: an HTML page


def fetch_bars(d: date, groups=DEFAULT_GROUPS) -> pd.DataFrame | None:
    """The day's official closes for BSE stocks, or None if not published."""
    text = _download(d)
    if text is None:
        return None
    bars = parse_bars(text, groups, expected=d)
    log.info("BSE bhavcopy %s: %d securities", d, len(bars))
    return bars


def latest_instruments(groups=DEFAULT_GROUPS, today: date | None = None, lookback_days: int = 10) -> list[Instrument]:
    """BSE stocks in the most recent bhavcopy. Raises if none is found in the last ``lookback_days``,
    so the engine keeps its cached universe instead of treating every BSE name as delisted."""
    today = today or date.today()
    for back in range(lookback_days + 1):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        text = _download(d)
        if text is not None:
            out = parse_instruments(text, groups)
            log.info("BSE universe: %d stocks in groups %s (bhavcopy %s)", len(out), sorted(groups), d)
            return out
    raise RuntimeError(f"No BSE bhavcopy found in the last {lookback_days} days")
