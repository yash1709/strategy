"""Core strategy: RSI(14) < 30 screening -> SMA(50) crossover tracking.

This module knows nothing about where prices come from. It consumes an
``IndicatorBook`` (closing prices + causal indicators) and a ``Repository``,
and processes exactly one trading day at a time.

Rules implemented here
----------------------
* Screening on day D uses only bars dated <= D (indicators are causal and the
  book is always read at D), so there is no look-ahead.
* A stock that is already on the active list is never added again (the DB also
  enforces UNIQUE(symbol)). After it exits, a later RSI<30 day starts a new cycle.
* Tracking starts on the first trading day *after* the RSI date. That date is
  filled in when that day is actually processed, so holidays need no special
  calendar logic.
* Each trading day from the tracking start: CMP = close(D), SMA50 = mean of the
  last 50 closes up to D. CMP > SMA50 (strict) => crossover, alert, move to history.
* Days tracked / trading days taken = trading sessions in [tracking start, D],
  inclusive -- a crossover on the first tracked day counts as 1.
* Missing bars (suspension, data gap): the day still counts as a trading day,
  the stock is not evaluated, ``missing_days`` increments. Long gaps are flagged
  and eventually closed out; delisted stocks are closed as DELISTED.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import NamedTuple

import numpy as np
import pandas as pd

from .cleaning import adjust_discontinuities
from .config import StrategyConfig
from .indicators import rsi_wilder, sma
from .storage import Repository

# active_tracking.status values
PENDING_START = "PENDING_START"          # qualified today; tracking starts next trading day
ACTIVE = "ACTIVE"
DATA_MISSING = "DATA_MISSING"            # no bar for >= stale_after_missing_days sessions
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # fewer than sma_period bars so far

# crossover_history.status values
SMA50_CROSSED = "SMA50_CROSSED"
DELISTED = "DELISTED"
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class Bar(NamedTuple):
    close: float
    volume: float
    rsi: float
    sma: float
    avg_volume: float
    bars: int  # number of bars available up to and including this day


class IndicatorBook:
    """Per-symbol closes with RSI / SMA / avg volume precomputed.

    Every column is causal (value at a date uses only bars up to that date),
    so reading ``bar(symbol, d)`` is equivalent to recomputing on data truncated
    at ``d``.
    """

    def __init__(self, prices: dict[str, pd.DataFrame], cfg: StrategyConfig):
        self._data: dict[str, tuple[dict[date, int], np.ndarray]] = {}
        self.adjustments: dict[str, list[dict]] = {}  # unadjusted splits / gaps repaired, per symbol
        for symbol, df in prices.items():
            if df.empty:
                continue
            close = df["close"].astype(float)
            volume = df["volume"].astype(float)
            # Indicators run on a discontinuity-free series; SMA is then expressed in the
            # price scale of its own day (divide by that bar's scale) so that CMP (the real
            # traded close) and SMA 50 are directly comparable and match what a live run shows.
            adjusted, scale, events = adjust_discontinuities(close, cfg.max_daily_move)
            if events:
                self.adjustments[symbol] = events
            adj = pd.Series(adjusted, index=close.index)
            matrix = np.column_stack([
                close.to_numpy(),
                volume.to_numpy(),
                rsi_wilder(adj, cfg.rsi_period).to_numpy(),
                sma(adj, cfg.sma_period).to_numpy() / scale,
                volume.rolling(20, min_periods=20).mean().to_numpy(),
            ])
            self._data[symbol] = ({d: i for i, d in enumerate(df.index)}, matrix)

    def bar(self, symbol: str, d: date) -> Bar | None:
        entry = self._data.get(symbol)
        if entry is None:
            return None
        pos = entry[0].get(d)
        if pos is None:
            return None
        c, v, r, s, av = entry[1][pos]
        return Bar(c, v, r, s, av, pos + 1)

    def last_date(self, symbol: str) -> date | None:
        entry = self._data.get(symbol)
        return max(entry[0]) if entry else None

    def coverage(self, d: date, symbols: list[str]) -> float:
        if not symbols:
            return 0.0
        return sum(1 for s in symbols if s in self._data and d in self._data[s][0]) / len(symbols)


@dataclass
class Event:
    kind: str            # ENTERED | CROSSED | CLOSED
    symbol: str
    company: str
    trade_date: date
    data: dict = field(default_factory=dict)


@dataclass
class DayResult:
    day: date
    events: list[Event] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=lambda: {"screened": 0, "added": 0, "crossed": 0, "removed": 0})


def _num(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 4)


class Strategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def process_day(self, d: date, book: IndicatorBook, repo: Repository, universe: dict[str, str]) -> DayResult:
        """Apply one trading day. Caller wraps this in a DB transaction."""
        result = DayResult(d)
        exited_today = self._monitor(d, book, repo, universe, result)
        self._screen(d, book, repo, universe, exited_today, result)
        return result

    # -- step 1: SMA 50 monitoring of the active list ------------------------
    def _monitor(self, d: date, book: IndicatorBook, repo: Repository, universe: dict[str, str],
                 result: DayResult) -> set[str]:
        exited: set[str] = set()
        for rec in repo.active():
            symbol = rec["symbol"]
            rsi_date = date.fromisoformat(rec["rsi_date"])
            if d <= rsi_date:
                continue  # qualified today: tracking begins next trading day

            if rec["tracking_start_date"] is None:
                start = d
                repo.update_active(rec["id"], tracking_start_date=start, status=ACTIVE)
                rec["tracking_start_date"] = start.isoformat()
                repo.audit("TRACKING_STARTED", symbol, d, rsi_date=rsi_date)
            else:
                start = date.fromisoformat(rec["tracking_start_date"])
            days = repo.count_trading_days(start, d)
            bar = book.bar(symbol, d)

            if bar is None:
                self._handle_missing(d, rec, days, book, repo, universe, result, exited)
                continue

            cmp_, sma50 = _num(bar.close), _num(bar.sma)
            repo.add_observation(rec["id"], symbol, d, cmp_, sma50, _num(bar.rsi))
            if sma50 is None:
                repo.update_active(rec["id"], cmp=cmp_, sma50=None, volume=_num(bar.volume), days_tracked=days,
                                   last_eval_date=d, missing_days=0, status=INSUFFICIENT_DATA)
                continue

            volume = _num(bar.volume)
            if bar.close > bar.sma:
                size = repo.market_size(symbol, bar.close)
                mcap = round(size, 2) if size else None
                repo.close_tracking(rec, SMA50_CROSSED, exit_date=d, crossover_date=d, cmp=cmp_, sma50=sma50,
                                    days_taken=days, volume=volume, market_cap=mcap)
                repo.audit("SMA50_CROSSED", symbol, d, cmp=cmp_, sma50=sma50, trading_days_taken=days)
                result.events.append(Event("CROSSED", symbol, rec["company"], d, {
                    "rsi_date": rsi_date, "rsi_value": rec["rsi_value"], "tracking_start_date": start,
                    "cmp": cmp_, "sma50": sma50, "trading_days_taken": days, "volume": volume, "market_cap": mcap, "kind": rec.get("kind", "EQUITY"),
                }))
                result.stats["crossed"] += 1
                exited.add(symbol)
            else:
                repo.update_active(rec["id"], cmp=cmp_, sma50=sma50, volume=volume, days_tracked=days,
                                   last_eval_date=d, missing_days=0, status=ACTIVE)
        return exited

    def _handle_missing(self, d: date, rec: dict, days: int, book: IndicatorBook, repo: Repository,
                        universe: dict[str, str], result: DayResult, exited: set[str]) -> None:
        symbol = rec["symbol"]
        missing = rec["missing_days"] + 1
        last_bar = book.last_date(symbol)
        gone = symbol not in universe and (last_bar is None or last_bar < d)

        if gone or missing >= self.cfg.remove_after_missing_days:
            status = DELISTED if gone else DATA_UNAVAILABLE
            note = (f"No longer in NSE list; last bar {last_bar}" if gone
                    else f"No price data for {missing} consecutive trading days")
            repo.close_tracking(rec, status, exit_date=d, days_taken=days, notes=note)
            repo.audit(status, symbol, d, missing_days=missing, last_bar=last_bar)
            result.events.append(Event("CLOSED", symbol, rec["company"], d, {"status": status, "notes": note}))
            result.stats["removed"] += 1
            exited.add(symbol)
            return

        status = DATA_MISSING if missing >= self.cfg.stale_after_missing_days else rec["status"]
        if status == PENDING_START:
            status = ACTIVE
        repo.update_active(rec["id"], days_tracked=days, missing_days=missing, status=status)
        repo.add_observation(rec["id"], symbol, d, None, None, None)
        if missing == 1 or missing == self.cfg.stale_after_missing_days:
            repo.audit("DATA_MISSING", symbol, d, consecutive_missing_days=missing)

    # -- step 2: RSI < 30 screening ------------------------------------------
    def _screen(self, d: date, book: IndicatorBook, repo: Repository, universe: dict[str, str],
                exited_today: set[str], result: DayResult) -> None:
        cfg = self.cfg
        active = repo.active_symbols()
        for symbol, company in universe.items():
            if symbol in active or symbol in exited_today:
                continue  # never duplicate a tracked stock; no same-day re-entry after exit
            bar = book.bar(symbol, d)
            if bar is None or bar.bars < cfg.min_history_bars or math.isnan(bar.rsi):
                continue
            result.stats["screened"] += 1
            if bar.rsi >= cfg.rsi_threshold:
                continue
            if cfg.min_close and bar.close < cfg.min_close:
                continue
            if cfg.min_avg_volume and not (bar.avg_volume >= cfg.min_avg_volume):
                continue
            rsi_value = round(float(bar.rsi), 2)
            tid = repo.add_active(symbol, company, d, rsi_value, _num(bar.close), PENDING_START)
            repo.update_active(tid, cmp=_num(bar.close), volume=_num(bar.volume), last_eval_date=d)
            repo.add_observation(tid, symbol, d, _num(bar.close), _num(bar.sma), rsi_value)
            repo.audit("ENTERED", symbol, d, rsi=rsi_value, close=_num(bar.close))
            result.events.append(Event("ENTERED", symbol, company, d, {"rsi_value": rsi_value,
                                                                       "close": _num(bar.close)}))
            result.stats["added"] += 1
