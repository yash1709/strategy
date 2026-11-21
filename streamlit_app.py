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

from nse_monitor import ai
from nse_monitor.analytics import backtest_summary, health_check, health_table
from nse_monitor.config import AiConfig
from nse_monitor.notifications import fmt_date, mood_line
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
    """Return (active, history, stats, info, extra) from a private temp copy of the database.

    ``extra`` holds the optional panels (market mood, signals, backtest, health, chart bars, AI notes);
    each is simply empty on a database that predates the feature."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "state.db"
        if payload is not None:
            db.write_bytes(payload)
        else:
            with closing(sqlite3.connect(local_path)) as src, closing(sqlite3.connect(db)) as dst:
                src.backup(dst)
        repo = Repository(db)  # creates any tables an older database does not have yet
        try:
            active, hist, stats = active_table(repo), history_table(repo), crossover_stats(repo)
            row = repo.conn.execute("SELECT MAX(date) d, MAX(processed_at) t FROM processed_days").fetchone()
            info = {"last_day": row["d"], "processed_at": row["t"]}
            breadth = repo.breadth_history(120)
            bars = pd.DataFrame([dict(r) for r in repo.conn.execute("SELECT symbol, date, close, sma50, rsi FROM chart_bars")],
                                columns=["symbol", "date", "close", "sma50", "rsi"])
            extra = {
                "breadth": pd.DataFrame(breadth[::-1]),
                "mood": mood_line(breadth[0] if breadth else None),
                "signals": pd.DataFrame(repo.signals_recent(10)),
                "backtest": backtest_summary(repo),
                "health": health_table(repo),
                "problems": health_check(repo, datetime.now(IST).date()),
                "bars": bars,
                "notes": pd.DataFrame(repo.ai_notes(1000)),
            }
        finally:
            repo.close()
    return active, hist, stats, info, extra


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
        elif col == "Quality":
            cfg[col] = st.column_config.ProgressColumn(col, min_value=0, max_value=100, format="%d",
                                                       help="0-100 heuristic: RSI depth, volume, trend vs SMA 200, RSI turning up")
        elif col == "% Below SMA 50":
            cfg[col] = st.column_config.NumberColumn(col, format="%.2f", help="Positive = close is still below SMA 50")
    return cfg


COMPACT_HIDE = ["Company", "Size Basis", "Volume", "Last Trading Day", "Tracking Start Date", "Quality Notes", "Sector",
                "Status", "Notes", "Company Name", "Volume on Crossover Date", "Exit Date"]


def _chart_symbols(active: pd.DataFrame, hist: pd.DataFrame, bars: pd.DataFrame) -> list[str]:
    have = set(bars["symbol"]) if not bars.empty else set()
    order = list(active["Symbol"]) + list(hist["NSE Symbol"])
    return [s for i, s in enumerate(order) if s in have and s not in order[:i]]


def _price_chart(sym_bars: pd.DataFrame, markers: list[dict]):
    import altair as alt
    d = sym_bars.copy()
    d["date"] = pd.to_datetime(d["date"])
    long = d.melt("date", value_vars=["close", "sma50"], var_name="Line", value_name="Price").dropna()
    price = alt.Chart(long).mark_line().encode(
        x=alt.X("date:T", title=None), y=alt.Y("Price:Q", scale=alt.Scale(zero=False), title="Price (₹)"),
        color=alt.Color("Line:N", scale=alt.Scale(domain=["close", "sma50"], range=["#2f6fdb", "#e8890c"]), title=None),
        tooltip=["date:T", "Line:N", alt.Tooltip("Price:Q", format=",.2f")])
    layers = [price]
    if markers:
        mk = pd.DataFrame(markers)
        layers.append(alt.Chart(mk).mark_rule(strokeDash=[4, 4], color="#888").encode(x="date:T", tooltip=["label:N", "date:T"]))
        layers.append(alt.Chart(mk).mark_text(align="left", dx=4, dy=-4, color="#888", baseline="top")
                      .encode(x="date:T", y=alt.value(4), text="label:N"))
    return alt.layer(*layers).properties(height=300)


def _rsi_chart(sym_bars: pd.DataFrame):
    import altair as alt
    d = sym_bars.copy()
    d["date"] = pd.to_datetime(d["date"])
    line = alt.Chart(d).mark_line(color="#7a4fd6").encode(
        x=alt.X("date:T", title=None), y=alt.Y("rsi:Q", scale=alt.Scale(domain=[0, 100]), title="RSI(14)"),
        tooltip=["date:T", alt.Tooltip("rsi:Q", format=".1f")])
    rules = alt.Chart(pd.DataFrame({"y": [30, 70]})).mark_rule(strokeDash=[3, 3], color="#c0392b").encode(y="y:Q")
    return alt.layer(line, rules).properties(height=130)


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
        active, hist, stats, info, extra = load_tables(f"gh:{repo_name}:{ref}", payload, None)
        source = f"GitHub `{repo_name}` · branch `{branch}`"
    else:
        if not Path(local).exists():
            st.error("No data source configured. Set `GITHUB_REPO` in the app's secrets "
                     "(see DEPLOY.md), or run the monitor locally first.")
            st.stop()
        mtime = Path(local).stat().st_mtime
        active, hist, stats, info, extra = load_tables(f"local:{local}:{mtime}", None, local)
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

# ---------------------------------------------------------------- tabs
if extra["problems"]:
    st.warning("Data health: " + " ".join(extra["problems"]) + " See the **Data health** tab.")
if extra["mood"]:
    st.caption(extra["mood"])

tab_main, tab_chart, tab_mood, tab_back, tab_health, tab_ask = st.tabs(
    ["Tracking", "Stock chart", "Market mood & signals", "Backtest", "Data health", "Ask AI"])

# ---------------------------------------------------------------- history (first)
with tab_main:
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

    # ------------------------------------------------------------ active
    st.subheader("Active tracking list")
    st.caption("Largest market cap (stocks) / AUM (ETFs) first, unless you pick another order.")
    f1, f2, f3, f4 = st.columns([1, 2, 1, 1])
    kind = f1.selectbox("Type", ["All", "EQUITY", "ETF"])
    query = f2.text_input("Search symbol or company", placeholder="e.g. SBIN, Nifty 50, gold")
    min_size = f3.number_input("Min size (₹ Cr)", min_value=0, value=0, step=500)
    f4.download_button("Download CSV", _csv(active), "active_tracking.csv", "text/csv", key="dl_active",
                       width="stretch")

    g1, g2, g3, g4 = st.columns([1, 2, 1, 1])
    order = g1.selectbox("Order by", ["Size (default)", "Closest to SMA 50", "Best quality"],
                         help="'Closest to SMA 50' lists the stocks most likely to cross next.")
    sectors = sorted(s for s in active["Sector"].dropna().unique() if s != "-")
    sector_pick = g1.selectbox("Sector", ["All"] + sectors)
    exchange_pick = g1.selectbox("Exchange", ["All", "NSE", "BSE"], help="BSE = stocks listed on BSE but not on NSE")
    try:  # the watchlist travels in the page address (?watch=SBIN,TCS), so a bookmark keeps it
        if "watch" not in st.session_state:
            st.session_state["watch"] = [s for s in st.query_params.get("watch", "").split(",") if s]
    except Exception:
        st.session_state.setdefault("watch", [])
    all_symbols = sorted(active["Symbol"])
    st.session_state["watch"] = [s for s in st.session_state.get("watch", []) if s in all_symbols]
    watch = g2.multiselect("Watchlist", all_symbols, key="watch",
                           help="Pick the names you care about, then switch on 'Watchlist only'.")
    try:
        st.query_params["watch"] = ",".join(watch)
    except Exception:
        pass
    watch_only = g3.toggle("Watchlist only", value=False)
    compact = g4.toggle("Compact view", value=False, help="Fewer columns: easier on a phone")

    view = active
    if kind != "All":
        view = view[view["Type"] == kind]
    if sector_pick != "All":
        view = view[view["Sector"] == sector_pick]
    if exchange_pick != "All":
        view = view[view["Exchange"] == exchange_pick]
    if watch_only:
        view = view[view["Symbol"].isin(watch)]
    if query:
        q = query.strip().lower()
        view = view[view["Symbol"].str.lower().str.contains(q, regex=False)
                    | view["Company"].fillna("").str.lower().str.contains(q, regex=False)]
    if min_size:
        view = view[view["Market Cap / AUM (₹ Cr)"].fillna(0) >= min_size]
    if order == "Closest to SMA 50":
        view = view[view["% Below SMA 50"].notna() & (view["% Below SMA 50"] > 0)].sort_values("% Below SMA 50")
    elif order == "Best quality":
        view = view.sort_values("Quality", ascending=False, na_position="last")
    st.caption(f"{len(view):,} of {len(active):,} shown.")
    view = _with_dates(view)
    if compact:
        view = view.drop(columns=[c for c in COMPACT_HIDE if c in view.columns])
    st.dataframe(view, hide_index=True, width="stretch", height=560, column_config=_column_config(view))

    st.caption("Market cap = shares outstanding × close (Yahoo Finance). ETF AUM = AMFI quarterly average. "
               "Prices: Yahoo Finance daily closes; universe: NSE equity and ETF lists. Not investment advice.")

# ---------------------------------------------------------------- stock chart
with tab_chart:
    st.subheader("Stock chart")
    bars, notes = extra["bars"], extra["notes"]
    choices = _chart_symbols(active, hist, bars)
    if not choices:
        st.info("Chart data appears after the next daily run (it holds the last 90 sessions for every tracked "
                "stock and for stocks that exited in the last 30 days).")
    else:
        sym = st.selectbox("Stock", choices, help="Tracked stocks first, then recent exits")
        sb = bars[bars["symbol"] == sym].sort_values("date")
        markers = []
        for frame, name_col, date_col, label in ((active, "Symbol", "RSI Date", "RSI < 30"),
                                                 (hist, "NSE Symbol", "RSI Date", "RSI < 30"),
                                                 (hist, "NSE Symbol", "Crossover Date", "SMA 50 crossed")):
            for v in frame.loc[frame[name_col] == sym, date_col]:
                ts = pd.to_datetime(v, format="%d-%b-%Y", errors="coerce")
                if pd.notna(ts) and ts >= pd.to_datetime(sb["date"].min()):
                    markers.append({"date": ts, "label": label})
        st.altair_chart(_price_chart(sb, markers), width="stretch")
        st.altair_chart(_rsi_chart(sb), width="stretch")
        st.caption("Blue = close, orange = SMA 50, dashed lines = RSI < 30 entry and SMA 50 crossover. "
                   "Prices are split-adjusted. Red lines on the RSI chart mark 30 and 70.")
        row = active[active["Symbol"] == sym]
        if not row.empty:
            r = row.iloc[0]
            st.markdown(f"**Quality {r['Quality'] if pd.notna(r['Quality']) else '-'}/100** · {r['Quality Notes'] or 'no detail'}")
        if not notes.empty:
            mine = notes[notes["symbol"] == sym]
            if not mine.empty:
                n = mine.iloc[0]
                st.markdown(f"**News risk {n['risk']}** · {n['summary']}")

# ---------------------------------------------------------------- market mood & signals
with tab_mood:
    st.subheader("Market mood")
    br = extra["breadth"]
    if br.empty:
        st.info("Market mood is recorded on each daily run; it will show here after the next one.")
    else:
        latest = br.iloc[-1]
        n = max(int(latest["stocks"]), 1)
        c = st.columns(4)
        c[0].metric("Below RSI 30", f"{100 * latest['oversold'] / n:.1f}%", f"{int(latest['oversold']):,} stocks")
        c[1].metric("Above RSI 70", f"{100 * latest['overbought'] / n:.1f}%", f"{int(latest['overbought']):,} stocks")
        c[2].metric("Below SMA 50", f"{100 * latest['below_sma50'] / n:.1f}%")
        c[3].metric("Above SMA 200", f"{100 * latest['above_sma200'] / max(int(latest['with_sma200']), 1):.1f}%")
        trend = br.assign(**{"Below RSI 30 (%)": 100 * br["oversold"] / br["stocks"].clip(lower=1),
                             "Above RSI 70 (%)": 100 * br["overbought"] / br["stocks"].clip(lower=1)})
        st.line_chart(trend.set_index("date")[["Below RSI 30 (%)", "Above RSI 70 (%)"]])
        st.caption("When 15% or more of stocks are below RSI 30, the fall is market-wide rather than stock-specific.")
    st.subheader("Where the tracked stocks sit")
    if not active.empty and (active["Sector"] != "-").any():
        st.bar_chart(active.loc[active["Sector"] != "-", "Sector"].value_counts())
    else:
        st.caption("Sector data fills in over the first few daily runs.")

    st.subheader("Other signals (last 10 days)")
    sg = extra["signals"]
    if sg.empty:
        st.info("No signals recorded yet.")
    else:
        names = {"OVERBOUGHT": "RSI just crossed above 70", "LOW_52W": "New 52-week low", "HIGH_52W": "New 52-week high",
                 "GOLDEN_CROSS": "Golden cross (SMA 50 above SMA 200)", "DEATH_CROSS": "Death cross (SMA 50 below SMA 200)"}
        pick = st.selectbox("Signal", list(names), format_func=lambda k: names[k])
        shown = sg[sg["kind"] == pick].rename(columns={"trade_date": "Date", "symbol": "Symbol", "company": "Company",
                                                       "type": "Type", "sector": "Sector", "value": "Value"})
        st.caption(f"{len(shown):,} signals")
        st.dataframe(shown[["Date", "Symbol", "Company", "Type", "Sector", "Value"]], hide_index=True, width="stretch")

# ---------------------------------------------------------------- backtest
with tab_back:
    st.subheader("What happened after each signal")
    bt = extra["backtest"]
    if not bt["signals"]:
        st.info("Forward returns appear once signals are at least 5 trading days old.")
    else:
        st.caption(f"{bt['signals']:,} entries measured. Returns are split-adjusted closes, counted in that stock's own "
                   "trading days. 'vs typical stock' subtracts the median stock's return over the same days.")
        if bt["signals"] < 30:
            st.caption("⚠ Small sample: treat these numbers as anecdotes, not evidence.")
        st.markdown("**Overall**")
        st.dataframe(bt["overall"], hide_index=True, width="stretch")
        for key, title, note in (
                ("by_size", "By size", None),
                ("by_speed", "By days to cross",
                 "Descriptive only: grouping by how long a stock took to cross uses information that is only known "
                 "afterwards, so it flatters the quick crossers. It cannot be used to pick entries in advance."),
                ("by_sector", "By sector (20 days)", None), ("by_month", "By month of entry", None)):
            if not bt[key].empty:
                st.markdown(f"**{title}**")
                if note:
                    st.caption(note)
                st.dataframe(bt[key], hide_index=True, width="stretch")
        st.caption("Tracking history is short and uses today's stock list (survivorship bias). "
                   "A longer replay (`replay --start`) gives a bigger sample.")

# ---------------------------------------------------------------- data health
with tab_health:
    st.subheader("Data health")
    if extra["problems"]:
        for p in extra["problems"]:
            st.markdown(f"- {p}")
    else:
        st.success("No problems found: data is fresh and price coverage is complete.")
    hh = extra["health"]
    if hh.empty:
        st.info("No processed days yet.")
    else:
        st.caption("Per processed day: share of stocks that had a price, plus data-quality events. 'Official Eod Fill' = "
                   "bars filled from NSE's end-of-day file; 'Price Source Mismatch' = Yahoo and NSE closes differ.")
        st.dataframe(hh, hide_index=True, width="stretch")

# ---------------------------------------------------------------- ask AI
with tab_ask:
    st.subheader("Ask your data")
    ai_key = _setting("ANTHROPIC_API_KEY")
    ai_cfg = AiConfig(enabled=True, model=_setting("AI_MODEL", AiConfig.model), effort="low", max_tokens=1500)
    st.caption("Type a question in plain English. Claude turns it into a filter; the filter is applied to the tables "
               "here (Claude never sees the data and never runs code).")
    if not ai_key:
        st.info("Not set up: add `ANTHROPIC_API_KEY` to the app's secrets to turn this on. "
                "Examples: *large caps that crossed in under 5 days*, *ETFs with RSI below 20*.")
    with st.form("ask_form"):
        question = st.text_input("Question", key="ask_question",
                                 placeholder="Which stocks have quality above 70 and are within 2% of SMA 50?")
        asked = st.form_submit_button("Ask", disabled=not ai_key, key="ask_submit")
    if asked and question.strip():
        tables = {"active": _with_dates(active), "history": _with_dates(hist)}
        with st.spinner("Thinking…"):
            spec = ai.question_to_spec(question, tables, ai_cfg, key=ai_key)
        if not spec:
            st.error("Could not get an answer right now. Try rephrasing, or try again in a moment.")
        else:
            try:
                name, result = ai.apply_spec(spec, tables)
                st.markdown(f"**{spec.get('explanation') or 'Result'}** · table `{name}` · {len(result):,} rows")
                st.dataframe(result, hide_index=True, width="stretch", column_config=_column_config(result))
            except (ValueError, TypeError) as exc:
                st.error(f"I could not turn that into a valid filter: {exc}")
