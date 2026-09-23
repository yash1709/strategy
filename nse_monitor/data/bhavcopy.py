"""NSE's official end-of-day file ("bhavcopy") for one trading day.

https://archives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv

Every security that traded that day, with the official close and traded quantity.
Yahoo Finance can take more than 12 hours to fill in a session for most NSE symbols
(on 24-Sep-2026 at 05:00 IST it had closes for only 31% of symbols for 23-Sep, while
the bhavcopy had 99.9%), so the engine uses this file to fill gaps in recent days.
Closes are *not* adjusted for later corporate actions; that only matters for older
history, which keeps coming from Yahoo.
"""
from __future__ import annotations

import io
import logging
from datetime import date

import pandas as pd
import requests

log = logging.getLogger(__name__)

URL = "https://archives.nseindia.com/products/content/sec_bhavdata_full_{:%d%m%Y}.csv"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,*/*",
}


def parse_bhavcopy(text: str, series: set[str], expected: date | None = None) -> pd.DataFrame:
    """CSV text -> DataFrame indexed by symbol with columns close, volume."""
    df = pd.read_csv(io.StringIO(text), skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    for col in ("SYMBOL", "SERIES", "DATE1"):
        df[col] = df[col].astype(str).str.strip()
    if expected is not None and len(df):
        stamped = pd.to_datetime(df["DATE1"].iloc[0], format="%d-%b-%Y", errors="coerce")
        if pd.notna(stamped) and stamped.date() != expected:
            raise ValueError(f"bhavcopy for {expected} is stamped {stamped.date()}")
    df = df[df["SERIES"].isin(series)]
    out = pd.DataFrame({
        "close": pd.to_numeric(df["CLOSE_PRICE"], errors="coerce").to_numpy(),
        "volume": pd.to_numeric(df["TTL_TRD_QNTY"], errors="coerce").to_numpy(),
    }, index=df["SYMBOL"].to_numpy())
    out = out[out["close"].notna() & (out["close"] > 0)]
    return out[~out.index.duplicated(keep="first")]


def fetch_bhavcopy(d: date, series: set[str], timeout: int = 30) -> pd.DataFrame | None:
    """The day's bars, or None if NSE has not published a file for ``d`` (holiday / not yet)."""
    resp = requests.get(URL.format(d), headers=_HEADERS, timeout=timeout)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    bars = parse_bhavcopy(resp.text, series, expected=d)
    log.info("NSE bhavcopy %s: %d securities", d, len(bars))
    return bars
