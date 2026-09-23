"""Daily pipeline orchestration:

NSE list -> price refresh (with corporate-action detection) -> trading calendar
-> for each unprocessed trading day, in order: SMA50 monitoring, RSI<30 screening
-> alert outbox -> notification delivery -> report export.

Missed runs are caught up automatically: every unprocessed trading day since the
last processed one is replayed in date order, each seeing only its own data.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd

from .config import AppConfig
from .data.base import MarketDataSource
from .notifications import (Notifier, crossover_message, dispatch_pending, summary_message)
from .reports import export_reports
from .storage import Repository
from .strategy import Event, IndicatorBook, Strategy

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class RunResult:
    processed_days: list[date] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    skipped_reason: str = ""
    alerts_sent: int = 0
    alerts_failed: int = 0


def last_final_session_date(now: datetime | None, cutoff: str) -> date:
    """Latest calendar date whose daily close is final (today after the cutoff, else yesterday)."""
    now = now or datetime.now(IST)
    now = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    hh, mm = (int(x) for x in cutoff.split(":"))
    return now.date() if now.time() >= time(hh, mm) else now.date() - timedelta(days=1)


class Engine:
    def __init__(self, cfg: AppConfig, repo: Repository, source: MarketDataSource, notifiers: list[Notifier]):
        self.cfg, self.repo, self.source, self.notifiers = cfg, repo, source, notifiers
        self.strategy = Strategy(cfg.strategy)

    # ------------------------------------------------------------------ run
    def run(self, as_of: date | None = None, start: date | None = None, notify: bool = True,
            now: datetime | None = None) -> RunResult:
        """Process every unprocessed trading day up to ``as_of``.

        ``start`` (replay/backfill) processes from that date instead of from the
        last processed day. On a brand-new database without ``start``, only the
        latest trading day is processed.
        """
        res = RunResult()
        as_of = as_of or last_final_session_date(now, self.cfg.data.market_close_cutoff)
        universe = self._sync_universe(as_of)

        last_done = self.repo.last_processed_day()
        if start and last_done and start <= last_done:
            raise ValueError(f"Database already processed up to {last_done}; replaying from {start} would mix "
                             f"timelines. Use a fresh --db for the replay.")
        first_needed = start or (last_done + timedelta(days=1) if last_done else as_of - timedelta(days=10))
        if first_needed > as_of:
            res.skipped_reason = "Nothing to do: already up to date"
            log.info(res.skipped_reason)
            return self._finish(res, notify)

        symbols = sorted(set(universe) | self.repo.active_symbols())
        self._refresh_prices(symbols, first_needed, as_of)
        self._fill_recent_days(symbols, first_needed, as_of)
        calendar = self._sync_calendar(first_needed - timedelta(days=10), as_of, symbols)

        days = [d for d in calendar if first_needed <= d <= as_of and not self.repo.is_processed(d)]
        if not start and not last_done:
            days = days[-1:]  # fresh install: start from the latest session only
        if not days:
            res.skipped_reason = (f"No new trading day with complete data up to {as_of} (weekend/holiday, "
                                  f"already processed, or the provider has not published it yet)")
            log.info(res.skipped_reason)
            return self._finish(res, notify)

        book = IndicatorBook(self.repo.load_all_prices(), self.cfg.strategy)
        self._audit_adjustments(book)
        listed = list(universe)
        min_cov = self.cfg.data.min_coverage
        for n, d in enumerate(days):
            cov = book.coverage(d, listed)
            if cov < min_cov:
                # A later complete session means this gap is permanent at the provider:
                # process it (missing stocks are handled as missing data). Otherwise the
                # data is probably still arriving, so wait for the next run.
                if any(book.coverage(later, listed) >= min_cov for later in days[n + 1:]):
                    log.warning("%s: only %.0f%% coverage but later sessions are complete; processing", d, cov * 100)
                    self.repo.audit("LOW_COVERAGE_DAY", trade_date=d, coverage=round(cov, 3))
                else:
                    res.skipped_reason = (f"Only {cov:.0%} of stocks have a bar for {d}; stopping so it can be "
                                          f"retried when data is complete")
                    log.warning(res.skipped_reason)
                    self.repo.audit("DAY_DEFERRED", trade_date=d, coverage=round(cov, 3))
                    break
            with self.repo.transaction():
                day = self.strategy.process_day(d, book, self.repo, universe)
                for ev in day.events:
                    if ev.kind == "CROSSED":
                        subject, body = crossover_message(ev)
                        self.repo.queue_alert("CROSSOVER", subject, body, self._channels(), d, ev.symbol)
                self.repo.mark_processed(d, day.stats)
            log.info("%s processed: %s", d, day.stats)
            res.processed_days.append(d)
            res.events.extend(day.events)

        if res.processed_days and self.cfg.notify.daily_summary:
            subject, body = summary_message(res.processed_days, res.events, len(self.repo.active()))
            self.repo.queue_alert("SUMMARY", subject, body, self._channels(), res.processed_days[-1])
        return self._finish(res, notify)

    def _finish(self, res: RunResult, notify: bool) -> RunResult:
        self._refresh_market_caps(date.today())
        if notify:
            res.alerts_sent, res.alerts_failed = dispatch_pending(self.repo, self.notifiers,
                                                                  self.cfg.notify.max_attempts)
        export_reports(self.repo, self.cfg.path(self.cfg.reports_dir))
        return res

    def _channels(self) -> list[str]:
        return [n.channel for n in self.notifiers]

    # ------------------------------------------------------------- universe
    def _sync_universe(self, as_of: date) -> dict[str, str]:
        try:
            instruments = self.source.list_instruments()
            if not instruments:
                raise RuntimeError("source returned an empty instrument list")
            new, gone = self.repo.sync_instruments(instruments, as_of)
            with self.repo.transaction():
                if len(new) < len(instruments):  # skip per-symbol noise on the very first sync
                    for s in new:
                        self.repo.audit("INSTRUMENT_LISTED", s, as_of)
                for s in gone:
                    self.repo.audit("INSTRUMENT_UNLISTED", s, as_of)
        except Exception as exc:
            log.error("Could not refresh NSE instrument list (%s); using cached list", exc)
            self.repo.audit("UNIVERSE_REFRESH_FAILED", trade_date=as_of, error=str(exc)[:300])
        universe = self.repo.listed_symbols()
        if not universe:
            raise RuntimeError("No NSE instruments available (download failed and no cached list)")
        return universe

    # --------------------------------------------------------------- prices
    def _refresh_prices(self, symbols: list[str], first_needed: date, as_of: date) -> None:
        dcfg = self.cfg.data
        history_start = first_needed - timedelta(days=dcfg.history_days)
        last = self.repo.last_price_dates()
        fetched_from = self.repo.fetched_from()
        recent_cutoff = as_of - timedelta(days=60)

        incremental = [s for s in symbols
                       if s in last and fetched_from.get(s, date.max) <= history_start + timedelta(days=10)
                       and last[s] >= recent_cutoff]
        inc_set = set(incremental)
        full = [s for s in symbols if s not in inc_set]

        if incremental:
            window_start = min(as_of - timedelta(days=dcfg.refresh_overlap_days),
                               min(last[s] for s in incremental) - timedelta(days=7))
            log.info("Incremental price refresh for %d symbols from %s", len(incremental), window_start)
            fresh = self._safe_fetch(incremental, window_start, as_of)
            adjusted = []
            with self.repo.transaction():
                for symbol, bars in fresh.items():
                    if self._corporate_action(symbol, bars):
                        adjusted.append(symbol)
                    else:
                        self.repo.upsert_prices(symbol, bars)
            if adjusted:
                log.info("Corporate action / revision detected for %s; re-downloading history", adjusted)
                full.extend(adjusted)
                self.repo.expire_shares(adjusted)  # a split/bonus also changes the share count

        if full:
            log.info("Full price download for %d symbols from %s", len(full), history_start)
            fresh = self._safe_fetch(full, history_start, as_of)
            with self.repo.transaction():
                for symbol, bars in fresh.items():
                    self.repo.upsert_prices(symbol, bars, replace=True)
                    self.repo.set_fetched_from(symbol, history_start)

    def _audit_adjustments(self, book: IndicatorBook) -> None:
        new = 0
        with self.repo.transaction():
            for symbol, events in book.adjustments.items():
                for ev in events:
                    details = {k: v for k, v in ev.items() if k != "date"}
                    new += self.repo.audit_once("PRICE_DISCONTINUITY_ADJUSTED", symbol, ev["date"], **details)
        if new:
            log.info("Adjusted %d unadjusted split/bonus/gap price discontinuities (see audit log)", new)

    def _refresh_market_caps(self, today: date) -> None:
        """Keep shares outstanding current for every tracked / historical stock, then
        recompute market caps. A failure here never blocks the strategy."""
        symbols = self.repo.active_symbols() | self.repo.history_symbols()
        stale = self.repo.symbols_needing_shares(symbols, self.cfg.data.shares_refresh_days, today)
        if stale:
            try:
                self.repo.set_shares(self.source.fetch_shares_outstanding(stale), today)
            except Exception as exc:
                log.error("Shares outstanding fetch failed: %s", exc)
                self.repo.audit("SHARES_FETCH_FAILED", trade_date=today, error=str(exc)[:300])
        if self.repo.aum_is_stale(self.cfg.data.shares_refresh_days, today):
            try:  # one bulk AMFI download covers every ETF
                period, aum = self.source.fetch_fund_aum(self.repo.etf_isins())
                self.repo.set_aum(period, aum, today)
            except Exception as exc:
                log.error("ETF AUM fetch failed: %s", exc)
                self.repo.audit("AUM_FETCH_FAILED", trade_date=today, error=str(exc)[:300])
        self.repo.refresh_market_caps()

    def _fill_recent_days(self, symbols: list[str], first_needed: date, as_of: date) -> None:
        """Yahoo can take 12+ hours to complete a session. For recent unprocessed weekdays that
        are still below the coverage threshold, fill the missing bars from the exchange's
        official end-of-day file. Existing bars are never overwritten."""
        start = max(first_needed, as_of - timedelta(days=self.cfg.data.official_fill_days))
        counts = self.repo.bar_counts_by_date(start, as_of)
        wanted, need = set(symbols), self.cfg.data.min_coverage * len(symbols)
        d = start
        while d <= as_of:
            if d.weekday() < 5 and counts.get(d, 0) < need and not self.repo.is_processed(d):
                try:
                    bars = self.source.fetch_official_day(d)
                except Exception as exc:
                    log.warning("Official end-of-day file for %s unavailable: %s", d, exc)
                    bars = None
                if bars is not None and len(bars):
                    added = self.repo.fill_missing_bars(d, bars, wanted)
                    if added:
                        log.info("%s: filled %d missing bars from the official end-of-day file "
                                 "(had %d of %d)", d, added, counts.get(d, 0), len(symbols))
                        self.repo.audit("OFFICIAL_EOD_FILL", trade_date=d, bars_added=added,
                                        had_before=counts.get(d, 0))
            d += timedelta(days=1)

    def _corporate_action(self, symbol: str, bars: pd.DataFrame) -> bool:
        cached = self.repo.get_prices(symbol, bars.index.min(), bars.index.max())
        common = cached.index.intersection(bars.index)
        if len(common) == 0:
            return False
        old = cached.loc[common, "close"].astype(float)
        new = bars.loc[common, "close"].astype(float)
        drift = float(((new - old).abs() / old).max())
        if drift > self.cfg.data.corp_action_tolerance:
            self.repo.audit("PRICE_HISTORY_ADJUSTED", symbol, max(common), max_relative_change=round(drift, 4))
            return True
        return False

    def _safe_fetch(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        try:
            return self.source.fetch_daily_bars(symbols, start, end)
        except Exception as exc:
            log.error("Price download failed for %d symbols: %s", len(symbols), exc)
            self.repo.audit("PRICE_FETCH_FAILED", trade_date=end, symbols=len(symbols), error=str(exc)[:300])
            return {}

    # ------------------------------------------------------------- calendar
    def _sync_calendar(self, start: date, end: date, symbols: list[str]) -> list[date]:
        """Sessions = dates on which at least half the stocks actually traded.

        Stock data is the authority: index feeds both lag (missing recent sessions)
        and carry placeholder bars on holidays. The provider calendar is only a
        fallback when no price data is available at all. Days not yet processed are
        re-derived every run so a wrongly included day can be corrected.
        """
        days = self._derive_calendar(start, end, symbols)
        if not days:
            try:
                days = self.source.fetch_trading_days(start, end)
            except Exception as exc:
                log.warning("Trading calendar fetch failed: %s", exc)
        last_done = self.repo.last_processed_day()
        self.repo.replace_trading_days(max(start, last_done + timedelta(days=1)) if last_done else start, end,
                                       [d for d in days if not last_done or d > last_done])
        return self.repo.trading_days(start, end)

    def _derive_calendar(self, start: date, end: date, symbols: list[str]) -> list[date]:
        """A date is a session if at least half the symbols have a bar on it."""
        counts = self.repo.bar_counts_by_date(start, end)
        threshold = max(1, int(0.5 * len(symbols)))
        return sorted(d for d, n in counts.items() if n >= threshold)
