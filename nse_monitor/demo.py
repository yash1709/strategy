"""Offline demo data: ``python -m nse_monitor demo`` builds a small database of made-up stocks and ETFs
(no internet, about a minute) so the dashboard can be explored before any real download.

The prices are random walks with a few deliberate selloffs and recoveries, so there are plenty of
RSI<30 entries, crossovers, signals and market-mood readings to look at. Nothing here is real market data.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import AppConfig
from .data.base import Instrument, MarketDataSource
from .engine import Engine
from .storage import Repository

SECTORS = ["Banking", "IT", "Pharma", "Auto", "Energy", "FMCG", "Metals", "Realty"]
ETF_KINDS = ["Nifty 50", "GOLD", "SILVER", "Bank Nifty"]
HISTORY_BARS = 420          # about 20 months of sessions: enough for RSI, SMA 200 and 52-week signals
REPLAY_SESSIONS = 90


class DemoSource(MarketDataSource):
    name = "demo"

    def __init__(self, instruments: list[Instrument], prices: dict[str, pd.DataFrame], calendar: list[date],
                 shares: dict[str, float], aum: dict[str, float], sectors: dict[str, tuple[str, str]]):
        self.instruments, self.prices, self.calendar = instruments, prices, calendar
        self.shares, self.aum, self.sectors = shares, aum, sectors

    def list_instruments(self):
        return list(self.instruments)

    def fetch_daily_bars(self, symbols, start, end):
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

    def fetch_shares_outstanding(self, symbols):
        return {s: self.shares[s] for s in symbols if s in self.shares}

    def fetch_fund_aum(self, isins):
        return "Jul-Sep 2026", {i: v for i, v in self.aum.items() if i in set(isins)}

    def fetch_sectors(self, symbols):
        return {s: self.sectors[s] for s in symbols if s in self.sectors}

    def fetch_news(self, symbol, limit=8):
        return [f"{date.today():%Y-%m-%d}: (demo) {symbol} trades lower with the broader market"]


def _sessions(n: int, last: date) -> list[date]:
    days, d = [], last
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


def _walk(rng: np.random.Generator, n: int, market: np.ndarray, beta: float) -> np.ndarray:
    """Daily returns: market move x beta + own noise + 1-3 selloff-and-recovery episodes."""
    ret = beta * market + rng.normal(0.0003, 0.013, n)
    for _ in range(int(rng.integers(1, 4))):
        start = int(rng.integers(130, n - 25))
        fall, rise = int(rng.integers(6, 13)), int(rng.integers(12, 30))
        ret[start:start + fall] += rng.uniform(-0.032, -0.014)
        ret[start + fall:start + fall + rise] += rng.uniform(0.006, 0.016)
    return ret


def build_demo(db_path: Path, force: bool = False, stocks: int = 70, seed: int = 7, log=print) -> dict:
    db_path = Path(db_path)
    if db_path.exists():
        if not force:
            raise FileExistsError(f"{db_path} already exists (use --force to rebuild it)")
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    rng = np.random.default_rng(seed)
    calendar = _sessions(HISTORY_BARS, date.today())
    # a market-wide wobble, with one broad selloff near the end so the market-mood panel has something to show
    market = rng.normal(0, 0.006, HISTORY_BARS)
    market[HISTORY_BARS - 40:HISTORY_BARS - 30] -= 0.012

    instruments, prices, shares, aum, sectors = [], {}, {}, {}, {}
    for i in range(stocks):
        sym = f"DEMO{i:02d}"
        sector = SECTORS[i % len(SECTORS)]
        instruments.append(Instrument(sym, f"Demo {sector} Company {i} Ltd", isin=f"INE000D{i:05d}"))
        closes = rng.uniform(40, 3000) * np.cumprod(1 + _walk(rng, HISTORY_BARS, market, rng.uniform(0.4, 1.4)))
        volume = rng.uniform(2e5, 5e6) * rng.uniform(0.7, 1.6, HISTORY_BARS)
        prices[sym] = pd.DataFrame({"close": closes.round(2), "volume": volume.round()}, index=calendar)
        shares[sym] = float(rng.uniform(2e7, 4e9))
        sectors[sym] = (sector, f"{sector} industry")
    for i in range(8):  # a few stocks listed only on BSE (symbols end in .BO)
        sym = f"DEMOBSE{i:02d}.BO"
        sector = SECTORS[i % len(SECTORS)]
        instruments.append(Instrument(sym, f"Demo BSE {sector} Company {i} Ltd", series="X", isin=f"INE000B{i:05d}",
                                      exchange="BSE"))
        closes = rng.uniform(5, 400) * np.cumprod(1 + _walk(rng, HISTORY_BARS, market, rng.uniform(0.5, 1.6)))
        prices[sym] = pd.DataFrame({"close": closes.round(2), "volume": rng.uniform(2e4, 8e5, HISTORY_BARS).round()},
                                   index=calendar)
        shares[sym] = float(rng.uniform(5e6, 3e8))
        sectors[sym] = (sector, f"{sector} industry")
    for j, kind in enumerate(ETF_KINDS * 2):
        sym = f"DEMOETF{j:02d}"
        isin = f"INFDEMO{j:05d}"
        instruments.append(Instrument(sym, f"Demo {kind} ETF {j} [{kind}]", isin=isin, kind="ETF"))
        closes = rng.uniform(30, 600) * np.cumprod(1 + _walk(rng, HISTORY_BARS, market, 0.8) * 0.6)
        prices[sym] = pd.DataFrame({"close": closes.round(2), "volume": rng.uniform(1e5, 2e6, HISTORY_BARS).round()},
                                   index=calendar)
        aum[isin] = float(rng.uniform(2e9, 8e11))

    cfg = AppConfig(base_dir=db_path.parent, db_path=str(db_path), reports_dir="reports/demo")
    cfg.notify.console = False
    cfg.ai.enabled = False
    cfg.data.history_days = 2000
    source = DemoSource(instruments, prices, calendar, shares, aum, sectors)
    repo = Repository(db_path)
    try:
        engine = Engine(cfg, repo, source, [])  # no notifiers: demo alerts are never sent
        log(f"Building demo data: {stocks} stocks + {len(ETF_KINDS) * 2} ETFs, replaying the last {REPLAY_SESSIONS} sessions...")
        res = engine.run(as_of=calendar[-1], start=calendar[-REPLAY_SESSIONS], notify=False)
        repo.suppress_pending_alerts("suppressed: demo data")
        return {"days": len(res.processed_days), "active": len(repo.active()), "crossovers": len(repo.history()),
                "db": str(db_path)}
    finally:
        repo.close()
