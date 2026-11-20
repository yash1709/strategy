"""Daily pipeline orchestration:

NSE list -> price refresh (with corporate-action detection) -> trading calendar
-> for each unprocessed trading day, in order: SMA50 monitoring, RSI<30 screening
-> alert outbox -> notification delivery -> report export.

Missed runs are caught up automatically: every unprocessed trading day since the
last processed one is replayed in date order, each seeing only its own data.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd

from . import ai, analytics
from .config import AppConfig
from .data.base import MarketDataSource, is_bse
from .notifications import (Notifier, crossover_message, dispatch_pending, entry_digest_message, mood_line,
                            near_cross_message, summary_message)
from .reports import export_reports
from .storage import Repository
from .strategy import Event, IndicatorBook, Strategy, quality_score

CRORE = 1e7

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
        # Coverage is judged on the NSE stocks only: many small BSE names legitimately have no bar on a normal
        # day, and they must not hold up (or defer) a trading day. Their gaps are handled like any missing data.
        listed = [s for s in universe if not is_bse(s)] or list(universe)
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
                    if ev.kind == "CROSSED" and self._alert_allowed(ev.symbol, kind=ev.data.get("kind"),
                                                                    size=ev.data.get("market_cap")):
                        subject, body = crossover_message(ev)
                        self.repo.queue_alert("CROSSOVER", subject, body, self._channels(), d, ev.symbol)
                self.repo.mark_processed(d, day.stats, coverage=round(cov, 4))
            log.info("%s processed: %s", d, day.stats)
            res.processed_days.append(d)
            res.events.extend(day.events)

        if res.processed_days:
            self._post_process(res, book)
            if self.cfg.notify.daily_summary:
                mood = mood_line(next(iter(self.repo.breadth_history(1)), None))
                subject, body = summary_message(res.processed_days, res.events, len(self.repo.active()), mood)
                self.repo.queue_alert("SUMMARY", subject, body, self._channels(), res.processed_days[-1])
        return self._finish(res, notify)

    # ------------------------------------------------------- after-the-day work
    def _post_process(self, res: RunResult, book: IndicatorBook) -> None:
        """Everything that happens after the trading days are committed. None of it can change the
        strategy's decisions, so a failure here is logged and never fails the run."""
        last = res.processed_days[-1]
        for name, step in (("analytics", lambda: self._refresh_analytics(book, last)),
                           ("sectors", lambda: self._refresh_sectors(date.today())),
                           ("alerts", lambda: self._queue_signal_alerts(res, last))):
            try:
                step()
            except Exception as exc:
                log.error("Post-processing step '%s' failed: %s", name, exc)
                self.repo.audit("POST_STEP_FAILED", trade_date=last, step=name, error=str(exc)[:300])

    def _refresh_analytics(self, book: IndicatorBook, today: date) -> None:
        n = analytics.update_outcomes(self.repo, book)
        m = analytics.refresh_chart_bars(self.repo, book, today)
        log.info("Analytics: %d outcome rows updated, chart data for %d symbols", n, m)

    def rebuild_analytics(self) -> None:
        """Recompute outcomes / chart data from the price cache and back-fill what older runs never recorded
        (CLI ``analytics``; also runs once automatically, see ``_catch_up``). Safe to repeat."""
        book = IndicatorBook(self.repo.load_all_prices(), self.cfg.strategy)
        last = self.repo.last_processed_day() or date.today()
        self._refresh_analytics(book, last)
        self._backfill(book, last)
        self._refresh_sectors(date.today())

    def _backfill(self, book: IndicatorBook, last: date, days: int = 60) -> None:
        """Fill in what runs before these features never recorded, from the price cache:
        * quality scores for tracked stocks (computed on their RSI day; market context is unknown, so the
          note omits it and says '(backfilled)');
        * market mood and extra signals for the last ``days`` processed days that have none.
        Past decisions are never touched."""
        q = 0
        with self.repo.transaction():
            for rec in self.repo.active():
                if rec["quality"] is not None:
                    continue
                bar = book.bar(rec["symbol"], date.fromisoformat(rec["rsi_date"]))
                if bar is not None and bar.rsi == bar.rsi:  # not NaN
                    score, notes = quality_score(bar, None)
                    self.repo.update_active(rec["id"], quality=score, quality_notes=f"{notes} (backfilled)")
                    q += 1
        universe = self.repo.listed_symbols()
        missing = sorted(date.fromisoformat(p["date"]) for p in self.repo.processed_days_table(days)
                         if not self.repo.has_breadth(date.fromisoformat(p["date"])))
        for d in missing:  # oldest first, so the signal clean-up that follows each day keeps the right window
            with self.repo.transaction():
                self.strategy._breadth_and_signals(d, book, self.repo, universe)
        log.info("Back-filled quality for %d tracked stocks and market mood for %d days", q, len(missing))

    def _catch_up(self) -> None:
        """One-time catch-up after an upgrade: the first run after the new features are deployed may find
        nothing new to process, which would leave the new dashboard panels empty until the next trading day.
        Runs once (an audit marker records it) and only when a price cache is available."""
        if self.repo.has_audit("ANALYTICS_CATCH_UP"):
            return
        last = self.repo.last_processed_day()
        prices = self.repo.load_all_prices() if last else {}
        if not prices:
            return
        book = IndicatorBook(prices, self.cfg.strategy)
        self._refresh_analytics(book, last)
        self._backfill(book, last)
        self._refresh_sectors(date.today())
        self.repo.audit("ANALYTICS_CATCH_UP", trade_date=last)

    def _refresh_sectors(self, today: date) -> None:
        """Sector / industry for tracked and historical symbols (ETFs use their NSE category)."""
        dcfg = self.cfg.data
        symbols = self.repo.active_symbols() | self.repo.history_symbols()
        need = self.repo.symbols_needing_sector(symbols, dcfg.sector_refresh_days, today)
        etf, equity = {}, []
        for s in need:
            inst = self.repo.instrument(s) or {}
            if inst.get("kind") == "ETF":
                m = re.search(r"\[(.+)\]\s*$", inst.get("company") or "")
                etf[s] = (m.group(1) if m else "ETF", "ETF")
            else:
                equity.append(s)
        if etf:
            self.repo.set_sectors(etf, today)
        batch = equity[:dcfg.sector_batch_limit]
        if batch:
            try:
                self.repo.set_sectors(self.source.fetch_sectors(batch), today)
            except Exception as exc:
                log.error("Sector fetch failed: %s", exc)
                self.repo.audit("SECTOR_FETCH_FAILED", trade_date=today, error=str(exc)[:300])

    # ---------------------------------------------------------------- alerts
    def _alert_allowed(self, symbol: str, close: float | None = None, kind: str | None = None,
                       size: float | None = None) -> bool:
        """The user's alert filters (instrument types, minimum size). Unknown size passes the size filter."""
        a = self.cfg.alerts
        if kind is None:
            inst = self.repo.instrument(symbol)
            kind = (inst or {}).get("kind") or "EQUITY"
        if kind not in a.kinds:
            return False
        if a.min_market_cap_cr:
            if size is None and close:
                size = self.repo.market_size(symbol, close)
            if size is not None and size / CRORE < a.min_market_cap_cr:
                return False
        return True

    def _queue_signal_alerts(self, res: RunResult, last: date) -> None:
        a = self.cfg.alerts
        channels = self._channels()
        mood = mood_line(next(iter(self.repo.breadth_history(1)), None))

        entered = [e for e in res.events if e.kind == "ENTERED"
                   and self._alert_allowed(e.symbol, close=e.data.get("close"))
                   and (e.data.get("quality") or 0) >= a.entry_min_quality]
        entered.sort(key=lambda e: (-(e.data.get("quality") or 0), e.symbol))
        notes = self._ai_notes(entered[:self.cfg.ai.max_notes_per_run], mood)
        if a.on_entry and entered:
            subject, body = entry_digest_message(last, entered, notes, mood, len(entered), a.entry_max_listed)
            self.repo.queue_alert("ENTRY", subject, body, channels, last)

        if a.on_near_cross:
            near = [r for r in self.repo.near_cross_candidates(last, a.near_cross_pct)
                    if self._alert_allowed(r["symbol"], kind=r["kind"], size=r["market_cap"])]
            for rec in near[:a.near_cross_max]:  # active() is already largest size first
                subject, body = near_cross_message(rec)
                self.repo.queue_alert("NEAR_CROSS", subject, body, channels, last, rec["symbol"])
            self.repo.mark_near_alerted(r["id"] for r in near[:a.near_cross_max])

    def _ai_notes(self, entries: list[Event], mood: str | None) -> dict[str, dict]:
        """Claude notes for the best entries of this run (optional; empty when AI is off or unavailable)."""
        if not entries or not ai.available(self.cfg.ai):
            return {}
        notes: dict[str, dict] = {}
        for e in entries:
            if self.repo.has_ai_note(e.symbol, e.trade_date):
                continue
            headlines: list[str] = []
            if self.cfg.ai.use_news:
                try:
                    headlines = self.source.fetch_news(e.symbol)
                except Exception as exc:
                    log.debug("No news for %s: %s", e.symbol, exc)
            facts = {"RSI(14)": e.data["rsi_value"], "Close": e.data.get("close"),
                     "Quality score": f"{e.data.get('quality')}/100 ({e.data.get('quality_notes')})",
                     "Date": e.trade_date.isoformat()}
            if mood:
                facts["Market mood"] = mood
            note = ai.entry_note(self.cfg.ai, e.symbol, e.company, facts, headlines)
            if note:
                self.repo.save_ai_note(e.symbol, e.trade_date, note["risk"], note["summary"])
                notes[e.symbol] = note
        return notes

    def _finish(self, res: RunResult, notify: bool) -> RunResult:
        try:
            self._catch_up()
        except Exception as exc:  # optional extras must never fail the run
            log.error("Analytics catch-up failed: %s", exc)
            self.repo.audit("POST_STEP_FAILED", step="catch_up", error=str(exc)[:300])
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
        """Yahoo can take 12+ hours to complete a session, and even on a "complete" day it can miss a
        few percent of stocks. For every recent, not-yet-processed weekday, fill each symbol that has
        no bar from the exchange's official end-of-day file (existing bars are never overwritten),
        and compare the bars both sources have so a disagreement is visible in the data-health panel.

        A processed day is never revisited, so gaps must be closed *before* the day is processed."""
        start = max(first_needed, as_of - timedelta(days=self.cfg.data.official_fill_days))
        counts = self.repo.bar_counts_by_date(start, as_of)
        wanted = set(symbols)
        d = start
        while d <= as_of:
            if d.weekday() < 5 and counts.get(d, 0) < len(symbols) and not self.repo.is_processed(d):
                try:
                    bars = self.source.fetch_official_day(d)
                except Exception as exc:
                    log.warning("Official end-of-day file for %s unavailable: %s", d, exc)
                    bars = None
                if bars is not None and len(bars):
                    self._cross_check_official(d, bars, wanted)
                    added = self.repo.fill_missing_bars(d, bars, wanted)
                    if added:
                        log.info("%s: filled %d missing bars from the official end-of-day file "
                                 "(had %d of %d)", d, added, counts.get(d, 0), len(symbols))
                        self.repo.audit("OFFICIAL_EOD_FILL", trade_date=d, bars_added=added,
                                        had_before=counts.get(d, 0))
            d += timedelta(days=1)

    def _cross_check_official(self, d: date, official: pd.DataFrame, wanted: set[str], max_rows: int = 25) -> int:
        """Flag symbols whose Yahoo close differs from NSE's official close by more than the tolerance.
        Nothing is overwritten (the official file is unadjusted, Yahoo's is split-adjusted); the
        differences are recorded in the audit log and shown on the dashboard. Returns how many differ."""
        tol = self.cfg.data.price_mismatch_pct / 100
        mine = self.repo.closes_on(d)
        bad = []
        for symbol, close in mine.items():
            if symbol in wanted and symbol in official.index:
                ref = float(official.loc[symbol, "close"])
                if ref > 0 and abs(close / ref - 1) > tol:
                    bad.append((abs(close / ref - 1), symbol, close, ref))
        for diff, symbol, close, ref in sorted(bad, reverse=True)[:max_rows]:
            self.repo.audit_once("PRICE_SOURCE_MISMATCH", symbol, d, yahoo=round(close, 4), official=round(ref, 4),
                                 diff_pct=round(100 * diff, 2))
        if bad:
            log.warning("%s: %d symbols differ from the official close by more than %.1f%%", d, len(bad),
                        100 * tol)
        return len(bad)

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
        core = [s for s in symbols if not is_bse(s)] or list(symbols)  # BSE-only names never decide this
        counts = self.repo.bar_counts_by_date(start, end, nse_only=len(core) < len(symbols))
        threshold = max(1, int(0.5 * len(core)))
        return sorted(d for d, n in counts.items() if n >= threshold)
