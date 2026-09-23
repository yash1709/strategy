"""Offline source reading local CSV files -- handy for testing, for feeding data
exported from another tool, or as a template for writing a new provider.

Layout::

    <csv_dir>/instruments.csv      symbol,company[,series,isin,listing_date,kind]   (kind: EQUITY or ETF)
    <csv_dir>/prices/<SYMBOL>.csv  date,close[,volume]   (split-adjusted closes)
    <csv_dir>/trading_days.csv     date                  (optional; else union of price dates)
    <csv_dir>/shares.csv           symbol,shares         (optional; enables market cap)
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd

from .base import Instrument, MarketDataSource, normalize_bars


class CsvSource(MarketDataSource):
    name = "csv"

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def list_instruments(self) -> list[Instrument]:
        df = pd.read_csv(self.root / "instruments.csv", dtype=str).fillna("")
        return [
            Instrument(
                symbol=r["symbol"].strip(),
                company=r.get("company", "").strip(),
                series=(r.get("series") or "EQ").strip(),
                isin=r.get("isin", "").strip(),
                listing_date=pd.to_datetime(r["listing_date"]).date() if r.get("listing_date") else None,
                kind=(r.get("kind") or "EQUITY").strip().upper(),
            )
            for r in df.to_dict("records")
        ]

    def fetch_daily_bars(self, symbols: Iterable[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        out = {}
        for symbol in symbols:
            path = self.root / "prices" / f"{symbol}.csv"
            if not path.exists():
                continue
            bars = normalize_bars(pd.read_csv(path, index_col="date", parse_dates=True))
            bars = bars[(bars.index >= start) & (bars.index <= end)]
            if not bars.empty:
                out[symbol] = bars
        return out

    def fetch_shares_outstanding(self, symbols: Iterable[str]) -> dict[str, float]:
        path = self.root / "shares.csv"
        if not path.exists():
            return {}
        df = pd.read_csv(path, dtype={"symbol": str})
        wanted = set(symbols)
        return {r.symbol: float(r.shares) for r in df.itertuples() if r.symbol in wanted and r.shares > 0}

    def fetch_trading_days(self, start: date, end: date) -> list[date]:
        cal = self.root / "trading_days.csv"
        if cal.exists():
            days = pd.to_datetime(pd.read_csv(cal)["date"]).dt.date
        else:
            days = set()
            for path in (self.root / "prices").glob("*.csv"):
                days.update(pd.to_datetime(pd.read_csv(path)["date"]).dt.date)
        return sorted(d for d in days if start <= d <= end)
