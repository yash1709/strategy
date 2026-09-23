"""Data-source contract.

The strategy and engine only ever talk to a ``MarketDataSource``. To switch
providers (NSE bhavcopy, a broker API, a paid feed...), implement these three
methods and register the class in ``nse_monitor/data/__init__.py`` -- nothing
in the strategy needs to change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Iterable

import pandas as pd

BAR_COLUMNS = ["close", "volume"]


@dataclass(frozen=True)
class Instrument:
    symbol: str                 # NSE symbol, e.g. "RELIANCE"
    company: str
    series: str = "EQ"
    isin: str = ""
    listing_date: date | None = None
    kind: str = "EQUITY"        # "EQUITY" or "ETF"


class MarketDataSource(ABC):
    name: str = "base"

    @abstractmethod
    def list_instruments(self) -> list[Instrument]:
        """Currently listed NSE instruments."""

    @abstractmethod
    def fetch_daily_bars(self, symbols: Iterable[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        """Daily bars for ``start..end`` inclusive.

        Returns ``{symbol: DataFrame}`` where the index is ``datetime.date`` (ascending)
        and columns are ``BAR_COLUMNS``. ``close`` must be the official daily close,
        adjusted for splits/bonuses (so that indicators are not distorted by
        corporate actions). Symbols with no data may be omitted.
        """

    @abstractmethod
    def fetch_trading_days(self, start: date, end: date) -> list[date]:
        """NSE trading sessions between ``start`` and ``end`` inclusive."""

    def fetch_shares_outstanding(self, symbols: Iterable[str]) -> dict[str, float]:
        """Shares outstanding per symbol, used for market cap (shares x close).
        Optional: providers without this data return {} and market cap shows as blank."""
        return {}

    def fetch_official_day(self, d: date) -> pd.DataFrame | None:
        """Official end-of-day bars for one session (index = symbol; columns close, volume),
        used to fill recent days the main price feed has not completed yet. Optional:
        return None if unavailable (no file yet, holiday, or not supported)."""
        return None

    def fetch_fund_aum(self, isins: Iterable[str]) -> tuple[str | None, dict[str, float]]:
        """(period label, {ISIN: AUM in rupees}) for ETFs. Optional: default is no AUM."""
        return None, {}


def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a provider frame into the canonical shape and drop unusable rows."""
    if df is None or df.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    out = df.copy()
    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out.index = [ts.date() for ts in idx]
    if "volume" not in out.columns:
        out["volume"] = float("nan")
    out = out[BAR_COLUMNS].apply(pd.to_numeric, errors="coerce")
    out = out[out["close"].notna() & (out["close"] > 0)]
    out = out[~pd.Index(out.index).duplicated(keep="last")].sort_index()
    # Some feeds (Yahoo) publish placeholder bars on exchange holidays: zero volume and
    # the previous close repeated. They are not sessions, so drop them.
    phantom = (out["volume"] == 0) & (out["close"] == out["close"].shift(1))
    return out[~phantom]
