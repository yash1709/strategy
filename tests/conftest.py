from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import numpy as np
import pandas as pd
import pytest

from nse_monitor.config import AppConfig
from nse_monitor.data.base import Instrument, MarketDataSource
from nse_monitor.engine import Engine
from nse_monitor.notifications import Notifier
from nse_monitor.storage import Repository


def make_calendar(start: date, n: int, holidays: Iterable[date] = ()) -> list[date]:
    holidays, days, d = set(holidays), [], start
    while len(days) < n:
        if d.weekday() < 5 and d not in holidays:
            days.append(d)
        d += timedelta(days=1)
    return days


def frame(days: list[date], closes: Iterable[float]) -> pd.DataFrame:
    closes = list(closes)
    return pd.DataFrame({"close": closes, "volume": [1e6] * len(closes)}, index=days[:len(closes)])


def dip_then_rally(n_flat=160, n_drop=12, n_rise=45, base=100.0) -> list[float]:
    """Sideways, then a sharp fall (RSI < 30), then a steady recovery above SMA 50."""
    flat = [base + 0.8 * np.sin(i / 3) for i in range(n_flat)]
    out = list(flat)
    for _ in range(n_drop):
        out.append(out[-1] * 0.975)
    for _ in range(n_rise):
        out.append(out[-1] * 1.015)
    return out


def sideways(n: int, base=50.0) -> list[float]:
    return [base + 0.5 * np.sin(i / 2) for i in range(n)]


class InMemorySource(MarketDataSource):
    """Test double: serves only data inside the requested window, so a live
    day-by-day simulation genuinely cannot see the future."""
    name = "memory"

    def __init__(self, instruments: list[Instrument], prices: dict[str, pd.DataFrame], calendar: list[date]):
        self.instruments, self.prices, self.calendar = instruments, prices, calendar
        self.calls = 0

    def list_instruments(self):
        return list(self.instruments)

    def fetch_daily_bars(self, symbols, start, end):
        self.calls += 1
        out = {}
        for s in symbols:
            df = self.prices.get(s)
            if df is not None:
                part = df[(df.index >= start) & (df.index <= end)]
                if not part.empty:
                    out[s] = part.copy()
        return out

    def fetch_trading_days(self, start, end):
        return [d for d in self.calendar if start <= d <= end]

    shares: dict[str, float] = {}
    shares_error: Exception | None = None

    def fetch_shares_outstanding(self, symbols):
        if self.shares_error:
            raise self.shares_error
        return {s: self.shares[s] for s in symbols if s in self.shares}


class RecordingNotifier(Notifier):
    channel = "record"

    def __init__(self):
        self.messages: list[tuple[str, str]] = []
        self.fail = False

    def send(self, subject, body):
        if self.fail:
            raise ConnectionError("simulated outage")
        self.messages.append((subject, body))


@pytest.fixture
def cfg(tmp_path) -> AppConfig:
    c = AppConfig(base_dir=tmp_path)
    c.db_path = str(tmp_path / "test.db")
    c.notify.console = False
    c.notify.daily_summary = False
    c.data.history_days = 2000
    return c


@pytest.fixture
def make_engine(cfg):
    def _make(source, notifier=None, db=None):
        repo = Repository(db or cfg.path(cfg.db_path))
        notifier = notifier or RecordingNotifier()
        return Engine(cfg, repo, source, [notifier]), repo, notifier
    return _make
