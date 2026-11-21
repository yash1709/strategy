"""After-the-fact analysis: what happened after each signal, chart data for the dashboard,
and data-health checks.

Everything here is computed from the price cache during a run and stored in small tables in
``state.db``, because the dashboard only receives ``state.db`` (the price cache stays in the
cloud job). Forward returns use split-adjusted closes and count *bars of that stock*.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .storage import Repository
from .strategy import IndicatorBook

log = logging.getLogger(__name__)

HORIZONS = (5, 10, 20)
MIN_BENCH_STOCKS = 5
CHART_BARS = 90          # about 4 months of sessions: enough context around an entry, and keeps state.db small
CHART_HISTORY_DAYS = 30  # stocks that exited more recently than this keep their chart
CRORE = 1e7
LARGE_CR, MID_CR = 20_000, 5_000

BASE_LABELS = {"ENTRY": "After RSI<30 entry (from RSI-day close)", "CROSSOVER": "After SMA 50 crossover (from crossover close)"}


# ---------------------------------------------------------------- outcomes
def update_outcomes(repo: Repository, book: IndicatorBook) -> int:
    """Fill in 5/10/20-bar forward returns (and the typical-stock return over the same window) for every
    entry and crossover. Rows that are already complete are left alone. Returns rows written."""
    existing = repo.existing_outcomes()
    bench_cache: dict[tuple[date, int], float | None] = {}

    def bench(d: date, n: int) -> float | None:
        key = (d, n)
        if key not in bench_cache:
            vals = [r for s in book.symbols() if (r := book.forward_return(s, d, n)) is not None]
            bench_cache[key] = float(np.median(vals)) if len(vals) >= MIN_BENCH_STOCKS else None
        return bench_cache[key]

    out = []
    for t in repo.outcome_targets():
        key = (t["symbol"], t["rsi_date"], t["base"])
        old = existing.get(key)
        if old and old["ret_20"] is not None:
            continue
        d = date.fromisoformat(t["base_date"])
        row = dict(t)
        for h in HORIZONS:
            row[f"ret_{h}"] = book.forward_return(t["symbol"], d, h)
            row[f"bench_{h}"] = bench(d, h) if row[f"ret_{h}"] is not None else None
        if any(row[f"ret_{h}"] is not None for h in HORIZONS):
            out.append(row)
    if out:
        repo.save_outcomes(out)
    return len(out)


def _stats(df: pd.DataFrame, group: str, horizon: int) -> dict | None:
    r = df[f"ret_{horizon}"].dropna()
    if r.empty:
        return None
    ex = (df[f"ret_{horizon}"] - df[f"bench_{horizon}"]).dropna()
    return {"Group": group, "Horizon (days)": horizon, "Signals": int(len(r)),
            "Win rate %": round(100 * float((r > 0).mean()), 1),
            "Avg return %": round(100 * float(r.mean()), 2), "Median return %": round(100 * float(r.median()), 2),
            "Worst %": round(100 * float(r.min()), 2), "Best %": round(100 * float(r.max()), 2),
            "Avg vs typical stock %": round(100 * float(ex.mean()), 2) if len(ex) else None}


def _size_bucket(row) -> str:
    if row["type"] == "ETF":
        return "ETF"
    cap = row["cap_hist"] if pd.notna(row["cap_hist"]) else row["cap_active"]
    if pd.isna(cap):
        return "Unknown size"
    cr = cap / CRORE
    return "Large cap (20,000 Cr+)" if cr >= LARGE_CR else "Mid cap (5,000-20,000 Cr)" if cr >= MID_CR else "Small cap (< 5,000 Cr)"


def _speed_bucket(days) -> str:
    if pd.isna(days):
        return "Not crossed (yet)"
    return "1-3 days to cross" if days <= 3 else "4-7 days" if days <= 7 else "8-15 days" if days <= 15 else "16+ days"


def backtest_summary(repo: Repository) -> dict:
    """Tables for the dashboard's Backtest tab. All returns are in percent."""
    rows = repo.outcomes_joined()
    empty = pd.DataFrame()
    if not rows:
        return {"signals": 0, "overall": empty, "by_size": empty, "by_sector": empty, "by_month": empty,
                "by_speed": empty}
    df = pd.DataFrame(rows)
    entry = df[df["base"] == "ENTRY"].copy()
    entry["size"] = entry.apply(_size_bucket, axis=1)
    entry["speed"] = entry["trading_days_taken"].map(_speed_bucket)
    entry["month"] = entry["rsi_date"].str[:7]
    entry["sector"] = entry["sector"].fillna("Unknown")

    def table(frame: pd.DataFrame, key: str | None, horizons=(10, 20)) -> pd.DataFrame:
        out = []
        groups = [("All", frame)] if key is None else sorted(frame.groupby(key), key=lambda kv: str(kv[0]))
        for name, g in groups:
            for h in horizons:
                s = _stats(g, str(name), h)
                if s:
                    out.append(s)
        return pd.DataFrame(out)

    overall = []
    for base, label in BASE_LABELS.items():
        g = df[df["base"] == base]
        for h in HORIZONS:
            s = _stats(g, label, h)
            if s:
                overall.append(s)
    return {"signals": int(len(entry)), "overall": pd.DataFrame(overall), "by_size": table(entry, "size"),
            "by_sector": table(entry, "sector", horizons=(20,)), "by_month": table(entry, "month"),
            "by_speed": table(entry, "speed")}


# ---------------------------------------------------------------- chart bars
def refresh_chart_bars(repo: Repository, book: IndicatorBook, today: date,
                       history_days: int = CHART_HISTORY_DAYS) -> int:
    """Keep the last ``CHART_BARS`` bars for tracked stocks and stocks that exited recently."""
    symbols = repo.active_symbols() | repo.recent_history_symbols(today - timedelta(days=history_days))
    for s in sorted(symbols):
        bars = book.series(s, CHART_BARS)
        if not bars.empty:
            repo.replace_chart_bars(s, bars)
    repo.prune_chart_bars(symbols)
    return len(symbols)


def trim_chart_bars(repo: Repository, today: date, max_bars: int = CHART_BARS,
                    history_days: int = CHART_HISTORY_DAYS) -> int:
    """Apply the chart retention rules to what is already stored, without needing prices: drop charts of
    stocks that are neither tracked nor recent exits, and keep only the newest ``max_bars`` of the rest.
    Runs on every job run (a run with no new trading day never refreshes charts, so without this a change of
    the limits would not shrink the published file until the next session). Returns rows removed."""
    keep = repo.active_symbols() | repo.recent_history_symbols(today - timedelta(days=history_days))
    return repo.trim_chart_bars(keep, max_bars)


# ---------------------------------------------------------------- data health
HEALTH_EVENTS = ["OFFICIAL_EOD_FILL", "PRICE_SOURCE_MISMATCH", "PRICE_DISCONTINUITY_ADJUSTED", "PRICE_HISTORY_ADJUSTED",
                 "LOW_COVERAGE_DAY", "DAY_DEFERRED", "PRICE_FETCH_FAILED", "UNIVERSE_REFRESH_FAILED",
                 "SHARES_FETCH_FAILED", "AUM_FETCH_FAILED", "DATA_MISSING"]


def health_table(repo: Repository, days: int = 20) -> pd.DataFrame:
    """One row per recently processed day: price coverage plus counts of data-quality events."""
    processed = repo.processed_days_table(days)
    if not processed:
        return pd.DataFrame()
    counts: dict[tuple[str, str], int] = {}
    for a in repo.audit_log(limit=20000):
        if a["event"] in HEALTH_EVENTS and a["trade_date"]:
            counts[(a["trade_date"], a["event"])] = counts.get((a["trade_date"], a["event"]), 0) + 1
    rows = []
    for p in processed:
        row = {"Day": p["date"], "Price coverage %": None if p.get("coverage") is None else round(100 * p["coverage"], 1),
               "Screened": p["screened"], "New entries": p["added"], "Crossovers": p["crossed"], "Closed": p["removed"]}
        for ev in HEALTH_EVENTS:
            n = counts.get((p["date"], ev), 0)
            if n:
                row[ev.replace("_", " ").title()] = n
        rows.append(row)
    df = pd.DataFrame(rows)
    base = ["Day", "Price coverage %", "Screened", "New entries", "Crossovers", "Closed"]
    extra = [c for c in df.columns if c not in base]
    df[extra] = df[extra].fillna(0).astype(int)
    return df


def _weekdays_between(a: date, b: date) -> int:
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def health_check(repo: Repository, today: date, max_stale_weekdays: int = 3, min_coverage: float = 0.9) -> list[str]:
    """Problems worth an alert: stale data, thin recent coverage, repeated feed failures. Empty list = healthy."""
    problems: list[str] = []
    last = repo.last_processed_day()
    if last is None:
        return ["No trading day has been processed yet."]
    stale = _weekdays_between(last, today)
    if stale > max_stale_weekdays:
        problems.append(f"Data is stale: last processed day is {last} ({stale} weekdays ago).")
    recent = repo.processed_days_table(3)
    low = [p["date"] for p in recent if p.get("coverage") is not None and p["coverage"] < min_coverage]
    if low:
        problems.append(f"Low price coverage (< {min_coverage:.0%}) on processed day(s): {', '.join(low)}.")
    cutoff = (today - timedelta(days=2)).isoformat()
    for a in repo.audit_log(limit=300):
        if a["event"] in ("PRICE_FETCH_FAILED", "UNIVERSE_REFRESH_FAILED") and (a["trade_date"] or "") >= cutoff:
            problems.append(f"{a['event'].replace('_', ' ').title()} on {a['trade_date']}.")
            break
    return problems
