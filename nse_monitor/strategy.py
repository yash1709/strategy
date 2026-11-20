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
  and eventually closed out; delisted stocks are closed as DELISTED after a short
  grace period (so a one-day glitch in the NSE list cannot close a position).

Optional extras (all off, or informational only, by default)
------------------------------------------------------------
* ``entry_confirmation = "rsi_turn"``: only enter once RSI has turned up.
* ``max_tracking_days`` / ``exit_rsi`` / ``stop_loss_pct``: extra ways to close a cycle.
* Every entry gets a 0-100 quality score and a note (see ``quality_score``).
* Each day the market breadth and a few extra signals (overbought, 52-week
  high/low, golden/death cross) are recorded.
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
from .data.base import is_bse
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
TIME_LIMIT = "TIME_LIMIT"                # optional rules
RSI_RECOVERED = "RSI_RECOVERED"
STOP_LOSS = "STOP_LOSS"

# one trading year, and the long average used for the trend / golden-cross signals
YEAR_BARS = 252
LONG_SMA = 200

# IndicatorBook matrix columns
(C_CLOSE, C_VOL, C_RSI, C_SMA, C_AVGVOL, C_ADJ, C_SMA_ADJ, C_SMA200_ADJ,
 C_SMA200, C_LOW252, C_HIGH252) = range(11)
NAN = float("nan")


class Bar(NamedTuple):
    close: float
    volume: float
    rsi: float
    sma: float
    avg_volume: float
    bars: int  # number of bars available up to and including this day
    # extras (all expressed in the price scale of this bar's own day unless noted)
    sma200: float = NAN
    low252: float = NAN
    high252: float = NAN
    prev_rsi: float = NAN
    prev_close: float = NAN
    prev_sma_adj: float = NAN     # adjusted-scale values, comparable with sma_adj / sma200_adj
    prev_sma200_adj: float = NAN
    sma_adj: float = NAN
    sma200_adj: float = NAN


class IndicatorBook:
    """Per-symbol closes with RSI / SMA / avg volume precomputed.

    Every column is causal (value at a date uses only bars up to that date),
    so reading ``bar(symbol, d)`` is equivalent to recomputing on data truncated
    at ``d``.
    """

    def __init__(self, prices: dict[str, pd.DataFrame], cfg: StrategyConfig):
        self._data: dict[str, tuple[dict[date, int], np.ndarray]] = {}
        self._dates: dict[str, list[date]] = {}
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
            sma_adj = sma(adj, cfg.sma_period)
            sma200_adj = sma(adj, LONG_SMA)
            matrix = np.column_stack([
                close.to_numpy(),
                volume.to_numpy(),
                rsi_wilder(adj, cfg.rsi_period).to_numpy(),
                sma_adj.to_numpy() / scale,
                volume.rolling(20, min_periods=20).mean().to_numpy(),
                adjusted,
                sma_adj.to_numpy(),
                sma200_adj.to_numpy(),
                sma200_adj.to_numpy() / scale,
                adj.rolling(YEAR_BARS, min_periods=YEAR_BARS).min().to_numpy() / scale,
                adj.rolling(YEAR_BARS, min_periods=YEAR_BARS).max().to_numpy() / scale,
            ])
            self._data[symbol] = ({d: i for i, d in enumerate(df.index)}, matrix)
            self._dates[symbol] = list(df.index)

    def bar(self, symbol: str, d: date) -> Bar | None:
        entry = self._data.get(symbol)
        if entry is None:
            return None
        pos = entry[0].get(d)
        if pos is None:
            return None
        m = entry[1]
        row = m[pos]
        prev = m[pos - 1] if pos > 0 else None
        return Bar(row[C_CLOSE], row[C_VOL], row[C_RSI], row[C_SMA], row[C_AVGVOL], pos + 1,
                   row[C_SMA200], row[C_LOW252], row[C_HIGH252],
                   prev[C_RSI] if prev is not None else NAN,
                   prev[C_CLOSE] if prev is not None else NAN,
                   prev[C_SMA_ADJ] if prev is not None else NAN,
                   prev[C_SMA200_ADJ] if prev is not None else NAN,
                   row[C_SMA_ADJ], row[C_SMA200_ADJ])

    def last_date(self, symbol: str) -> date | None:
        entry = self._data.get(symbol)
        return max(entry[0]) if entry else None

    def coverage(self, d: date, symbols: list[str]) -> float:
        if not symbols:
            return 0.0
        return sum(1 for s in symbols if s in self._data and d in self._data[s][0]) / len(symbols)

    # -- helpers for charts and the backtest ------------------------------------
    def symbols(self) -> list[str]:
        return list(self._data)

    def series(self, symbol: str, last: int = 150) -> pd.DataFrame:
        """Last ``last`` bars as (date, close, sma50, rsi), in the CURRENT price scale
        (splits removed) so a chart has no artificial jumps."""
        entry = self._data.get(symbol)
        if entry is None:
            return pd.DataFrame(columns=["date", "close", "sma50", "rsi"])
        m, dates = entry[1][-last:], self._dates[symbol][-last:]
        return pd.DataFrame({"date": dates, "close": m[:, C_ADJ], "sma50": m[:, C_SMA_ADJ], "rsi": m[:, C_RSI]})

    def forward_return(self, symbol: str, d: date, n: int) -> float | None:
        """Return from the close on ``d`` to the close ``n`` bars later (split-adjusted); None if unknown."""
        entry = self._data.get(symbol)
        if entry is None:
            return None
        pos = entry[0].get(d)
        if pos is None or pos + n >= len(entry[1]):
            return None
        a, b = entry[1][pos, C_ADJ], entry[1][pos + n, C_ADJ]
        return None if not (a > 0 and b > 0) else float(b / a - 1)


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


def _ok(x: float) -> bool:
    return x is not None and not math.isnan(x)


def quality_score(bar: Bar, market_oversold_pct: float | None = 0.0) -> tuple[int, str]:
    """A transparent 0-100 heuristic for a fresh RSI<30 entry (not a prediction).

    Four parts of up to 25 points each:
    * depth   -- how far below 30 the RSI is (15 or lower = full marks)
    * volume  -- today's volume vs its 20-day average (2x or more = full marks)
    * trend   -- close vs SMA 200: at or above = full marks, 30% below = zero
                 (a pull-back in an uptrend scores higher than a broken stock)
    * turn    -- RSI higher than the day before (selling pressure easing)
    The note also says whether the whole market is oversold (>= 15% of stocks below RSI 30).
    """
    depth = max(0.0, min(1.0, (30.0 - bar.rsi) / 15.0)) * 25
    notes = [f"RSI {bar.rsi:.0f}"]
    if _ok(bar.avg_volume) and bar.avg_volume > 0 and _ok(bar.volume):
        ratio = bar.volume / bar.avg_volume
        vol = max(0.0, min(1.0, (ratio - 0.8) / 1.2)) * 25
        notes.append(f"volume {ratio:.1f}x avg")
    else:
        vol = 0.0
        notes.append("volume n/a")
    if _ok(bar.sma200) and bar.sma200 > 0:
        dist = bar.close / bar.sma200 - 1
        trend = 25.0 if dist >= 0 else max(0.0, 1 + dist / 0.30) * 25
        notes.append(f"{dist * 100:+.0f}% vs SMA200")
    else:
        trend = 12.0
        notes.append("SMA200 n/a")
    if _ok(bar.prev_rsi) and bar.rsi > bar.prev_rsi:
        turn = 25.0
        notes.append("RSI turning up")
    else:
        turn = 0.0
    if market_oversold_pct is not None:  # None = market context unknown (back-filled scores)
        notes.append("market-wide selloff" if market_oversold_pct >= 15 else "stock-specific")
    return int(round(depth + vol + trend + turn)), "; ".join(notes)


class Strategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def process_day(self, d: date, book: IndicatorBook, repo: Repository, universe: dict[str, str]) -> DayResult:
        """Apply one trading day. Caller wraps this in a DB transaction."""
        result = DayResult(d)
        breadth = self._breadth_and_signals(d, book, repo, universe)
        exited_today = self._monitor(d, book, repo, universe, result)
        self._screen(d, book, repo, universe, exited_today, result, breadth)
        return result

    # -- step 0: market breadth + extra signals ------------------------------
    def _breadth_and_signals(self, d: date, book: IndicatorBook, repo: Repository,
                             universe: dict[str, str]) -> float:
        """Record how oversold the whole market is and any extra signals. Returns % of stocks with RSI < threshold."""
        n = oversold = overbought = below50 = above200 = with200 = 0
        rows: list[tuple[str, str, float | None]] = []
        for symbol in universe:
            bar = book.bar(symbol, d)
            if bar is None or math.isnan(bar.rsi):
                continue
            core = not is_bse(symbol)  # market mood is measured on NSE stocks; BSE-only names still get signals
            if core:
                n += 1
                if bar.rsi < self.cfg.rsi_threshold:
                    oversold += 1
                if bar.rsi > 70:
                    overbought += 1
                if _ok(bar.sma) and bar.close < bar.sma:
                    below50 += 1
                if _ok(bar.sma200):
                    with200 += 1
                    if bar.close > bar.sma200:
                        above200 += 1
            if bar.rsi > 70 and _ok(bar.prev_rsi) and bar.prev_rsi <= 70:
                rows.append((symbol, "OVERBOUGHT", _num(bar.rsi)))
            if _ok(bar.low252) and bar.close <= bar.low252:
                rows.append((symbol, "LOW_52W", _num(bar.close)))
            if _ok(bar.high252) and bar.close >= bar.high252:
                rows.append((symbol, "HIGH_52W", _num(bar.close)))
            if _ok(bar.sma_adj) and _ok(bar.sma200_adj) and _ok(bar.prev_sma_adj) and _ok(bar.prev_sma200_adj):
                if bar.prev_sma_adj <= bar.prev_sma200_adj and bar.sma_adj > bar.sma200_adj:
                    rows.append((symbol, "GOLDEN_CROSS", _num(bar.close)))
                elif bar.prev_sma_adj >= bar.prev_sma200_adj and bar.sma_adj < bar.sma200_adj:
                    rows.append((symbol, "DEATH_CROSS", _num(bar.close)))
        repo.save_breadth(d, n, oversold, overbought, below50, above200, with200)
        repo.save_signals(d, rows)
        return 100.0 * oversold / n if n else 0.0

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
                continue

            reason = self._early_exit(rec, bar, days)
            if reason:
                status, note = reason
                repo.close_tracking(rec, status, exit_date=d, cmp=cmp_, sma50=sma50, days_taken=days,
                                    notes=note, volume=volume)
                repo.audit(status, symbol, d, cmp=cmp_, sma50=sma50, days=days)
                result.events.append(Event("CLOSED", symbol, rec["company"], d, {"status": status, "notes": note}))
                result.stats["removed"] += 1
                exited.add(symbol)
            else:
                repo.update_active(rec["id"], cmp=cmp_, sma50=sma50, volume=volume, days_tracked=days,
                                   last_eval_date=d, missing_days=0, status=ACTIVE)
        return exited

    def _early_exit(self, rec: dict, bar: Bar, days: int) -> tuple[str, str] | None:
        """Optional extra exit rules (off by default). The SMA 50 crossover is always checked first."""
        cfg = self.cfg
        if cfg.stop_loss_pct and rec.get("rsi_close") and bar.close < rec["rsi_close"] * (1 - cfg.stop_loss_pct / 100):
            return STOP_LOSS, f"Close fell more than {cfg.stop_loss_pct:g}% below the RSI-day close"
        if cfg.exit_rsi and _ok(bar.rsi) and bar.rsi > cfg.exit_rsi:
            return RSI_RECOVERED, f"RSI rose above {cfg.exit_rsi:g} before the close crossed SMA 50"
        if cfg.max_tracking_days and days >= cfg.max_tracking_days:
            return TIME_LIMIT, f"No SMA 50 crossover within {cfg.max_tracking_days} trading days"
        return None

    def _handle_missing(self, d: date, rec: dict, days: int, book: IndicatorBook, repo: Repository,
                        universe: dict[str, str], result: DayResult, exited: set[str]) -> None:
        symbol = rec["symbol"]
        missing = rec["missing_days"] + 1
        last_bar = book.last_date(symbol)
        gone = symbol not in universe and (last_bar is None or last_bar < d)
        # a stock missing from the NSE list for only a session or two is more likely a list glitch than a delisting
        delisted = gone and missing >= self.cfg.delist_grace_days

        if delisted or missing >= self.cfg.remove_after_missing_days:
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
                exited_today: set[str], result: DayResult, market_oversold_pct: float = 0.0) -> None:
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
            if cfg.entry_confirmation == "rsi_turn" and not (_ok(bar.prev_rsi) and bar.rsi > bar.prev_rsi):
                continue  # still falling: wait for the first uptick while RSI is below the threshold
            rsi_value = round(float(bar.rsi), 2)
            quality, notes = quality_score(bar, market_oversold_pct)
            tid = repo.add_active(symbol, company, d, rsi_value, _num(bar.close), PENDING_START)
            repo.update_active(tid, cmp=_num(bar.close), volume=_num(bar.volume), last_eval_date=d,
                               quality=quality, quality_notes=notes)
            repo.add_observation(tid, symbol, d, _num(bar.close), _num(bar.sma), rsi_value)
            repo.audit("ENTERED", symbol, d, rsi=rsi_value, close=_num(bar.close), quality=quality)
            result.events.append(Event("ENTERED", symbol, company, d, {
                "rsi_value": rsi_value, "close": _num(bar.close), "quality": quality, "quality_notes": notes,
                "volume": _num(bar.volume), "tracking_id": tid}))
            result.stats["added"] += 1
