"""Streamlit dashboard for the NSE RSI<30 -> SMA 50 monitor.

Data source (first match wins):
1. Streamlit secrets / env ``GITHUB_REPO`` ("owner/repo") -> downloads ``state.db`` from the
   repo's ``DATA_BRANCH`` (default "data"), which the GitHub Actions job updates every weekday.
   For a private repo also set ``GITHUB_TOKEN`` (fine-grained, read-only "Contents").
2. ``NSE_MONITOR_DB`` env var, else ``data/nse_monitor.db`` -- the local database, if it exists.
3. ``DEFAULT_GITHUB_REPO`` below (this project's repository), so the hosted app works with no
   secrets at all. Override with a ``DEFAULT_GITHUB_REPO`` secret/env ("" disables it).

Manual runs ("Run update now" panel) need two secrets, set in the Streamlit app settings:
``RUN_PASSWORD`` (your choice) and ``GITHUB_DISPATCH_TOKEN`` -- a fine-grained GitHub token
limited to this repository with only "Actions: Read and write". See DEPLOY.md.

Run locally:  streamlit run streamlit_app.py
"""
from __future__ import annotations

import hmac
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
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
WORKFLOW_FILE = "daily.yml"
RUN_POLL_SECONDS = float(os.environ.get("RUN_POLL_SECONDS", "10"))
RUN_MAX_WAIT_SECONDS = 15 * 60
WRONG_PASSWORD_DELAY = float(os.environ.get("WRONG_PASSWORD_DELAY", "2"))
IST = timezone(timedelta(hours=5, minutes=30))
DATE_COLS = ["Last Trading Day", "RSI Date", "Tracking Start Date", "Crossover Date", "Exit Date"]


def _setting(name: str, default: str = "") -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # no secrets file
        pass
    return os.environ.get(name, default)


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def _branch_sha(repo: str, branch: str, token: str) -> str | None:
    """Latest commit of the data branch. Downloading state.db by commit id avoids the ~5 minute
    cache on branch URLs, so a just-finished run shows up immediately."""
    headers = {"Accept": "application/vnd.github.sha"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(f"https://api.github.com/repos/{repo}/commits/{branch}", headers=headers, timeout=30)
        return resp.text.strip() if resp.ok and len(resp.text.strip()) == 40 else None
    except requests.RequestException:
        return None


@st.cache_data(max_entries=4, show_spinner="Fetching latest data…")
def _download_at(repo: str, ref: str, token: str) -> bytes:
    if token:  # private repository
        url = f"https://api.github.com/repos/{repo}/contents/state.db?ref={ref}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"}
    else:
        url, headers = f"https://raw.githubusercontent.com/{repo}/{ref}/state.db", {}
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.content


def _download_state(repo: str, branch: str, token: str) -> tuple[bytes, str]:
    """(file bytes, ref used). Uses the exact commit when known; otherwise the branch name."""
    ref = _branch_sha(repo, branch, token or _setting("GITHUB_DISPATCH_TOKEN")) or branch
    if ref == branch:  # commit lookup failed: branch URL, cached for CACHE_SECONDS like before
        return _download_branch(repo, branch, token), ref
    return _download_at(repo, ref, token), ref


@st.cache_data(ttl=CACHE_SECONDS, show_spinner="Fetching latest data…")
def _download_branch(repo: str, branch: str, token: str) -> bytes:
    return _download_at.__wrapped__(repo, branch, token)


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


# ---------------------------------------------------------------- manual run (GitHub Actions)
def _gh(method: str, path: str, token: str, **kwargs) -> requests.Response:
    return getattr(requests, method)(f"https://api.github.com/repos/{path}", timeout=30, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}, **kwargs)


def _latest_manual_run(repo: str, token: str) -> dict | None:
    resp = _gh("get", f"{repo}/actions/workflows/{WORKFLOW_FILE}/runs", token, params={"per_page": 1})
    resp.raise_for_status()
    runs = resp.json().get("workflow_runs") or []
    return runs[0] if runs else None


def _ist(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST).strftime("%d-%b %H:%M IST")


def _run_panel(repo: str) -> None:
    token, password = _setting("GITHUB_DISPATCH_TOKEN"), _setting("RUN_PASSWORD")
    page = f"https://github.com/{repo}/actions/workflows/{WORKFLOW_FILE}"
    with st.expander("▶ Run an update now"):
        if not (token and password and repo):
            st.info(f"Manual runs from the dashboard are not set up yet (see DEPLOY.md). "
                    f"You can start one on GitHub: [Daily NSE monitor → Run workflow]({page}).")
            return
        try:
            latest = _latest_manual_run(repo, token)
        except requests.RequestException as exc:
            st.error(f"Could not reach GitHub: {exc}")
            return
        if latest and latest["status"] != "completed":
            st.info(f"An update is already running (started {_ist(latest['created_at'])}). "
                    f"[View progress]({latest['html_url']})")
            if st.button("Check again"):
                st.rerun()
            return
        if latest:
            st.caption(f"Last run: {_ist(latest['created_at'])} · {latest.get('conclusion') or latest['status']} · "
                       f"[details]({latest['html_url']})")
        with st.form("run_form", clear_on_submit=True):
            entered = st.text_input("Password", type="password", key="run_password")
            go = st.form_submit_button("▶ Run update now", type="primary", key="run_submit")
        if not go:
            return
        if not hmac.compare_digest(entered.encode(), password.encode()):
            time.sleep(WRONG_PASSWORD_DELAY)  # slows down guessing
            st.error("Wrong password.")
            return
        started = datetime.now(timezone.utc) - timedelta(seconds=5)
        resp = _gh("post", f"{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches", token, json={"ref": "main"})
        if resp.status_code != 204:
            st.error(f"GitHub refused the request ({resp.status_code}): {resp.text[:200]}. "
                     "Check that GITHUB_DISPATCH_TOKEN has 'Actions: Read and write' on this repository.")
            return
        with st.status("Update requested…", expanded=True) as box:
            run, deadline = None, time.monotonic() + RUN_MAX_WAIT_SECONDS
            while time.monotonic() < deadline:
                time.sleep(RUN_POLL_SECONDS)
                try:
                    candidate = _latest_manual_run(repo, token)
                except requests.RequestException:
                    continue
                if candidate and datetime.fromisoformat(candidate["created_at"].replace("Z", "+00:00")) >= started:
                    run = candidate
                    box.update(label=f"Update {run['status'].replace('_', ' ')}… (usually 2–4 minutes)")
                    if run["status"] == "completed":
                        break
            if not run or run["status"] != "completed":
                box.update(label="Still running. Check back in a few minutes.", state="running")
                st.markdown(f"[View progress on GitHub]({run['html_url'] if run else page})")
                return
            if run.get("conclusion") != "success":
                box.update(label=f"Update finished: {run.get('conclusion')}", state="error")
                st.markdown(f"[See what happened]({run['html_url']})")
                return
            box.update(label="Update complete. Loading the new data…", state="complete")
        st.cache_data.clear()
        time.sleep(min(RUN_POLL_SECONDS, 3))  # let GitHub serve the freshly published data file
        st.rerun()


# ---------------------------------------------------------------- load data
local = os.environ.get("NSE_MONITOR_DB", "data/nse_monitor.db")
repo_name = _setting("GITHUB_REPO") or ("" if Path(local).exists() else _setting("DEFAULT_GITHUB_REPO", DEFAULT_GITHUB_REPO))
try:
    if repo_name:
        branch, token = _setting("DATA_BRANCH", "data"), _setting("GITHUB_TOKEN")
        payload, ref = _download_state(repo_name, branch, token)
        active, hist, stats, info = load_tables(f"gh:{repo_name}:{ref}", payload, None)
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
_run_panel(_setting("GITHUB_REPO") or _setting("DEFAULT_GITHUB_REPO", DEFAULT_GITHUB_REPO))

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
