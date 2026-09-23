"""Streamlit dashboard for the NSE RSI<30 -> SMA 50 monitor.

Data source (first match wins):
1. Streamlit secrets / env ``GITHUB_REPO`` ("owner/repo") -> downloads ``state.db`` from the
   repo's ``DATA_BRANCH`` (default "data"), which the GitHub Actions job updates every weekday.
   For a private repo also set ``GITHUB_TOKEN`` (fine-grained, read-only "Contents").
2. ``NSE_MONITOR_DB`` env var, else ``data/nse_monitor.db`` -- the local database, if it exists.
3. ``DEFAULT_GITHUB_REPO`` below (this project's repository), so the hosted app works with no
   secrets at all. Override with a ``DEFAULT_GITHUB_REPO`` secret/env ("" disables it).

Run locally:  streamlit run streamlit_app.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

from nse_monitor.notifications import fmt_date
from nse_monitor.reports import HISTORY_DISPLAY_LIMIT, active_table, crossover_stats, history_table
from nse_monitor.storage import Repository

st.set_page_config(page_title="NSE RSI Monitor", page_icon="📉", layout="wide")

CACHE_SECONDS = 300
DEFAULT_GITHUB_REPO = "yash1709/strategy"
DATE_COLS = ["Last Trading Day", "RSI Date", "Tracking Start Date", "Crossover Date", "Exit Date"]


def _setting(name: str, default: str = "") -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # no secrets file
        pass
    return os.environ.get(name, default)


@st.cache_data(ttl=CACHE_SECONDS, show_spinner="Fetching latest data…")
def _download_state(repo: str, branch: str, token: str) -> bytes:
    if token:
        url = f"https://api.github.com/repos/{repo}/contents/state.db?ref={branch}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"}
    else:
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/state.db"
        headers = {}
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.content


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def load_tables(source_key: str, payload: bytes | None, local_path: str | None):
    """Return (active, history, stats, info) from a private temp copy of the database."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "state.db"
        if payload is not None:
            db.write_bytes(payload)
        else:
            with closing(sqlite3.connect(local_path)) as src, closing(sqlite3.connect(db)) as dst:
                src.backup(dst)
        repo = Repository(db)
        try:
            active, hist, stats = active_table(repo), history_table(repo), crossover_stats(repo)
            row = repo.conn.execute("SELECT MAX(date) d, MAX(processed_at) t FROM processed_days").fetchone()
            info = {"last_day": row["d"], "processed_at": row["t"]}
        finally:
            repo.close()
    return active, hist, stats, info


def _with_dates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in DATE_COLS:
        if col in out:
            out[col] = pd.to_datetime(out[col], format="%d-%b-%Y", errors="coerce")
    return out


def _column_config(df: pd.DataFrame) -> dict:
    cfg = {}
    for col in df.columns:
        if col in DATE_COLS:
            cfg[col] = st.column_config.DateColumn(col, format="DD-MMM-YYYY")
        elif "(₹ Cr)" in col:
            cfg[col] = st.column_config.NumberColumn(col, format="localized")
        elif col in ("CMP (Close)", "SMA 50", "CMP on Crossover Date", "SMA 50 on Crossover Date"):
            cfg[col] = st.column_config.NumberColumn(col, format="₹%.2f")
        elif col.startswith("Volume"):
            cfg[col] = st.column_config.NumberColumn(col, format="localized")
        elif col == "RSI Value":
            cfg[col] = st.column_config.NumberColumn(col, format="%.2f")
    return cfg


def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


# ---------------------------------------------------------------- load data
local = os.environ.get("NSE_MONITOR_DB", "data/nse_monitor.db")
repo_name = _setting("GITHUB_REPO") or ("" if Path(local).exists() else _setting("DEFAULT_GITHUB_REPO", DEFAULT_GITHUB_REPO))
try:
    if repo_name:
        branch, token = _setting("DATA_BRANCH", "data"), _setting("GITHUB_TOKEN")
        payload = _download_state(repo_name, branch, token)
        active, hist, stats, info = load_tables(f"gh:{repo_name}:{branch}", payload, None)
        source = f"GitHub `{repo_name}` · branch `{branch}`"
    else:
        if not Path(local).exists():
            st.error("No data source configured. Set `GITHUB_REPO` in the app's secrets "
                     "(see DEPLOY.md), or run the monitor locally first.")
            st.stop()
        mtime = Path(local).stat().st_mtime
        active, hist, stats, info = load_tables(f"local:{local}:{mtime}", None, local)
        source = f"local database `{local}`"
except requests.HTTPError as exc:
    st.error(f"Could not download the data from GitHub ({exc.response.status_code}). "
             "Check GITHUB_REPO / DATA_BRANCH, and GITHUB_TOKEN if the repository is private.")
    st.stop()

# ---------------------------------------------------------------- header
st.title("NSE RSI < 30 → SMA 50 Monitor")
processed = info.get("processed_at")
processed_txt = datetime.fromisoformat(processed).strftime("%d-%b-%Y %H:%M") if processed else "-"
st.caption(
    f"Stocks and ETFs whose daily RSI(14) fell below 30, tracked from the next trading day until the "
    f"close rises above SMA 50. Durations are in trading days. Latest trading day: "
    f"**{fmt_date(info.get('last_day'))}** · updated {processed_txt} IST · source: {source}"
)

m = st.columns(5)
m[0].metric("Active tracking", f"{len(active):,}")
m[1].metric("SMA 50 crossovers", f"{stats.get('count', 0):,}")
m[2].metric("Avg trading days to cross", stats.get("mean", "-"))
m[3].metric("Median days", stats.get("median", "-"))
m[4].metric("Latest trading day", fmt_date(info.get("last_day")))

if st.button("↻ Refresh data", help=f"Data is cached for {CACHE_SECONDS // 60} minutes"):
    st.cache_data.clear()
    st.rerun()

# ---------------------------------------------------------------- history (first)
st.subheader("Historical crossover database")
st.caption("Latest exit first, then largest market cap / AUM.")
h1, h2 = st.columns([3, 1])
show_all = h1.toggle(f"Show all {len(hist):,} records", value=False) if len(hist) > HISTORY_DISPLAY_LIMIT else True
h2.download_button("Download CSV", _csv(hist), "crossover_history.csv", "text/csv", key="dl_hist",
                   width="stretch")
hist_view = hist if show_all else hist.head(HISTORY_DISPLAY_LIMIT)
if not show_all:
    st.caption(f"Showing latest {HISTORY_DISPLAY_LIMIT} of {len(hist):,}.")
hist_view = _with_dates(hist_view)
st.dataframe(hist_view, hide_index=True, width="stretch", column_config=_column_config(hist_view))

# ---------------------------------------------------------------- active
st.subheader("Active tracking list")
st.caption("Largest market cap (stocks) / AUM (ETFs) first.")
f1, f2, f3, f4 = st.columns([1, 2, 1, 1])
kind = f1.selectbox("Type", ["All", "EQUITY", "ETF"])
query = f2.text_input("Search symbol or company", placeholder="e.g. SBIN, Nifty 50, gold")
min_size = f3.number_input("Min size (₹ Cr)", min_value=0, value=0, step=500)
f4.download_button("Download CSV", _csv(active), "active_tracking.csv", "text/csv", key="dl_active",
                   width="stretch")

view = active
if kind != "All":
    view = view[view["Type"] == kind]
if query:
    q = query.strip().lower()
    view = view[view["Symbol"].str.lower().str.contains(q, regex=False)
                | view["Company"].fillna("").str.lower().str.contains(q, regex=False)]
if min_size:
    view = view[view["Market Cap / AUM (₹ Cr)"].fillna(0) >= min_size]
st.caption(f"{len(view):,} of {len(active):,} shown.")
view = _with_dates(view)
st.dataframe(view, hide_index=True, width="stretch", height=560, column_config=_column_config(view))

st.caption("Market cap = shares outstanding × close (Yahoo Finance). ETF AUM = AMFI quarterly average. "
           "Prices: Yahoo Finance daily closes; universe: NSE equity and ETF lists. Not investment advice.")
