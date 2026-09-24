# NSE RSI Reversal Monitor

**Automated daily screening of NSE stocks and ETFs for oversold conditions (RSI < 30), with trading-day tracking until price recovers above its 50-day moving average.**

[![Live dashboard](https://img.shields.io/badge/Live%20dashboard-Streamlit-FF4B4B?logo=streamlit&logoColor=white)](https://strategy-gmk9uegg9dtzvennq7xyv4.streamlit.app/)
[![Daily NSE monitor](https://github.com/yash1709/strategy/actions/workflows/daily.yml/badge.svg)](https://github.com/yash1709/strategy/actions/workflows/daily.yml)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)

### 🔗 Live dashboard: **https://strategy-gmk9uegg9dtzvennq7xyv4.streamlit.app/**

Updated automatically every NSE trading day. Works in any browser, on any device, with no login.

---

## Contents
- [What it answers](#what-it-answers)
- [How it works](#how-it-works)
- [Strategy rules](#strategy-rules)
- [Data sources and data quality](#data-sources-and-data-quality)
- [The dashboard](#the-dashboard)
- [Automation and operations](#automation-and-operations)
- [Notifications](#notifications)
- [Running locally](#running-locally)
- [Configuration](#configuration)
- [Project structure](#project-structure)
- [Extending: replacing the data source](#extending-replacing-the-data-source)
- [Testing](#testing)
- [Limitations](#limitations)

---

## What it answers

| Question | Where |
|---|---|
| Which NSE stocks and ETFs have recently fallen below RSI 30 and are being tracked now? | **Active Tracking List**, largest market cap / AUM first |
| For each one, how many trading days did the close take to rise back above SMA 50? | **Historical Crossover Database**, most recent exit first |

**Coverage:** all NSE `EQ`-series equities (about 2,300) and NSE-listed ETFs (about 330: equity, gold, silver, debt and international). Tracking history starts on 1 August 2026.

---

## How it works

```
NSE equity + ETF lists ──┐
Yahoo Finance closes ────┼──▶  RSI(14) < 30  ──▶  Active tracking  ──▶  daily: close > SMA 50 ?
AMFI ETF AUM ────────────┘      (entry)           (from next             │
                                                   trading day)          ▼ yes
                                                              alert + move to Historical
                                                              Crossover Database
```

**Deployment:**

```
GitHub Actions (Mon–Fri 17:30 IST, then 00:15 & 08:30 IST the next day)
   restore state from `data` branch ─▶ python -m nse_monitor run ─▶ publish state to `data` branch
                                                                          │
Streamlit Community Cloud ◀── reads state.db (refreshed every 5 min) ─────┘
   https://strategy-gmk9uegg9dtzvennq7xyv4.streamlit.app/
```

- **GitHub Actions** runs the job in the cloud three times per trading day (17:30 the same day, then 00:15 and 08:30 the next day); no local machine is involved.
- **Tracking state** (active list, history, audit log) is kept in the repository's [`data`](https://github.com/yash1709/strategy/tree/data) branch as a 1.6 MB SQLite file. Only the latest version is kept, so repository history stays small. Every run's state is also saved as a 30-day workflow artifact.
- **The price cache** (about 30 MB) lives in the GitHub Actions cache. If it is ever evicted, the next run re-downloads prices in about 3 minutes.
- **The Streamlit dashboard** reads the published state directly from GitHub.

---

## Strategy rules

| Rule | Implementation |
|---|---|
| **Price basis** | Daily closing prices for RSI, CMP and SMA 50 |
| **RSI** | 14-period Wilder's RSI, verified against an independent reference implementation |
| **Entry** | First trading day with RSI < 30. Records the date and RSI value. Needs at least 100 bars of history |
| **No duplicates** | A tracked symbol is never re-added (enforced by the database). After it exits, a new RSI < 30 day starts a new cycle |
| **Tracking start** | The next trading day after the RSI date, set only when that session is actually processed, so holidays are handled correctly |
| **Exit** | First session from the tracking start where the close is strictly above SMA 50 |
| **Duration** | Trading sessions from the tracking start to the crossover, both included. Crossing on the first tracked day counts as 1 |
| **No look-ahead bias** | Each day uses only data dated on or before it. A test checks that running day by day with only past data gives exactly the same results as a historical replay |
| **Atomic and repeatable** | Each trading day is a single database transaction. Re-running a day has no effect, and missed days are caught up in order |

**Status values**
- *Active:* `PENDING_START` (qualified today) · `ACTIVE` · `DATA_MISSING` (5+ sessions with no price) · `INSUFFICIENT_DATA` (fewer than 50 bars)
- *History:* `SMA50_CROSSED` · `DELISTED` · `DATA_UNAVAILABLE` (60+ sessions with no price)

---

## Data sources and data quality

| Data | Source | Refresh |
|---|---|---|
| Equity universe | NSE [`EQUITY_L.csv`](https://archives.nseindia.com/content/equities/EQUITY_L.csv) | Every run |
| ETF universe | NSE [`eq_etfseclist.csv`](https://archives.nseindia.com/content/equities/eq_etfseclist.csv) | Every run |
| Daily closes and volume | Yahoo Finance (`SYMBOL.NS`) | Every run, re-downloading the last 20 days |
| Recent sessions Yahoo hasn't completed | NSE official end-of-day file ([bhavcopy](https://archives.nseindia.com/products/content/sec_bhavdata_full_23092026.csv)) | Recent days only, filling gaps and never overwriting Yahoo |
| Market cap (stocks) | Shares outstanding (Yahoo) × latest close | Share counts weekly, or right after a split |
| AUM (ETFs) | [AMFI](https://www.amfiindia.com/aum-data/average-aum) scheme-wise quarterly average AUM, matched to each ETF by ISIN | Weekly check; AMFI publishes quarterly |

Free data feeds have real defects. Each of these was found in production data and is handled explicitly:

| Issue | Handling |
|---|---|
| **Holiday placeholder bars.** Yahoo publishes zero-volume bars that repeat the previous close on NSE holidays, e.g. 14-Sep-2026 | Discarded. A day counts as a trading session only if at least half of the stocks actually traded |
| **Unadjusted splits and demergers.** For example 1:100 ETF unit splits (IVZINGOLD, LICMFGOLD) and demergers (VEDL, RAYMOND) | NSE price bands cap a normal day's move at 20%, so a one-day move beyond ±35% is a corporate action. Earlier prices are rescaled to remove the jump, using the exact split/bonus ratio when it matches one. Every adjustment is recorded in the audit log |
| **Late or partial data.** Yahoo can take 12+ hours to fill in a session: on 24-Sep 05:00 IST it had 23-Sep closes for only 31% of symbols | Missing bars for recent days are filled from NSE's official bhavcopy, which had 99.9% coverage and matches Yahoo's closes exactly. A day is processed only once at least 90% of stocks have a price for it. A permanent gap is processed once later days are complete, and logged |
| **Suspensions and delistings** | Missing days still count as trading days. Stocks that stay missing are flagged, then closed out, and delisted stocks move to history |
| **Retroactive corporate-action adjustments by the provider** | Detected by comparing re-downloaded prices with the cache; that stock's full history is then downloaded again |
| **Liquid and overnight ETFs** | Excluded by default: the price stays near ₹1,000 and moves by paise, so RSI is meaningless |

---

## The dashboard

**[strategy-gmk9uegg9dtzvennq7xyv4.streamlit.app](https://strategy-gmk9uegg9dtzvennq7xyv4.streamlit.app/)**

- **Summary:** active count, total crossovers, average and median trading days to cross, and the latest trading day.
- **Historical Crossover Database:** latest exit first, then largest market cap / AUM. Shows the latest 30, with a toggle for the full history.
- **Active Tracking List:** market cap / AUM, the size basis, CMP (close), volume, RSI date and value, tracking start, SMA 50 and days tracked. It can be filtered by type (stock or ETF), by symbol or company name, and by minimum size.
- **CSV downloads** for both tables, and a **Refresh** button. Data is otherwise cached for 5 minutes.

Streamlit Community Cloud puts apps to sleep after a period with no visitors. The first visit after that takes about 30 seconds while the app starts.

---

## Automation and operations

| | |
|---|---|
| Schedule | Mon–Fri **17:30 IST**, then **00:15 IST** (after NSE's end-of-day file, published around 23:55) and **08:30 IST** the next day (Tue–Sat), via [GitHub Actions](https://github.com/yash1709/strategy/actions/workflows/daily.yml). By the 09:15 market open, the previous session is always on the dashboard |
| Manual run | Actions → *Daily NSE monitor* → **Run workflow** |
| Typical duration | About 2–4 minutes, including a full price re-download if the cache was evicted |
| Holidays and weekends | Detected from the data; nothing is processed and nothing breaks |
| Missed runs | The next run catches up on every unprocessed trading day, in order |
| Backups | Each run's `state.db` is kept for 30 days as a workflow artifact |

GitHub may start scheduled runs 5–30 minutes late at busy times. In public repositories, GitHub pauses scheduled workflows after 60 days without commits; it emails a warning first, and one click re-enables them.

---

## Notifications

Crossover alerts are queued in the database and retried if delivery fails. The channels are the workflow log (always on), plus **Telegram**, a **webhook** (Slack, Discord, Teams or ntfy) or **email**. Set the channels you want as repository secrets under *Settings → Secrets and variables → Actions*: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `NSE_MONITOR_WEBHOOK`, `SMTP_PASSWORD`.

```
Stock: OBEROIRLTY (Oberoi Realty Limited)
RSI Entry Date: 15-Sep-2026
RSI: 26.8
Crossover Date: 22-Sep-2026
CMP: ₹1,845.60
SMA 50: ₹1,836.46
Trading Days Taken: 5
Status: SMA 50 Crossed
Market Cap: ₹67,106 Cr
Volume: 470,585
```

A daily summary of new entries and crossovers is also sent.

---

## Running locally

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy config.example.toml config.toml

.\.venv\Scripts\python.exe -m nse_monitor run                      # process new trading days
.\.venv\Scripts\python.exe -m nse_monitor replay --start 2026-08-01 # backfill history (on an empty database)
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py        # dashboard on http://localhost:8501
```

| Command | Purpose |
|---|---|
| `run [--as-of DATE] [--no-notify]` | Process every unprocessed trading day, send alerts, export reports |
| `replay --start DATE [--end DATE]` | Replay history into an empty database. Its alerts are not sent |
| `status` | Print the active tracking list |
| `history [--limit N]` | Print the latest N crossovers (default 30; `0` for all) with statistics |
| `audit [--symbol X]` | Show the audit trail: entries, crossovers, data gaps and price adjustments |
| `export` | Write `reports/active_tracking.csv`, `crossover_history.csv` and `dashboard.html` |
| `notify-test` / `retry-alerts` | Test notification channels / resend failed alerts |

The deployed setup doesn't need a local machine. `scripts/register_task.ps1` can still schedule the job on a Windows PC if you want to run it locally instead.

---

## Configuration

Settings live in a TOML file: `config.example.toml` for local runs and `config.cloud.toml` for GitHub Actions. Any value written as `${ENV_VAR}` is read from the environment, so secrets never go in the file.

| Key | Default | Meaning |
|---|---|---|
| `strategy.rsi_period` / `rsi_threshold` | 14 / 30 | Entry signal |
| `strategy.sma_period` | 50 | Exit signal |
| `strategy.min_history_bars` | 100 | Minimum history before a stock can be screened |
| `strategy.max_daily_move` | 0.35 | Size of one-day jump treated as a corporate action or bad data |
| `strategy.min_close` / `min_avg_volume` | 0 / 0 | Optional penny-stock and liquidity filters |
| `data.series` | `["EQ"]` | NSE series in the universe |
| `data.include_etfs` / `etf_exclude_categories` | `true` / liquid & overnight | ETF coverage |
| `data.min_coverage` | 0.9 | Share of stocks that must have a price before a day is processed |
| `data.market_close_cutoff` | `16:00` | Time (IST) after which today's close is treated as final |

---

## Project structure

```
nse_monitor/
  strategy.py        core rules: screening, tracking, crossover (no data-source code)
  indicators.py      Wilder RSI, SMA (causal)
  cleaning.py        corporate-action / bad-print repair
  engine.py          daily pipeline: universe → prices → calendar → per-day processing → alerts → reports
  storage.py         SQLite: tracking, history, audit log, alert outbox, price cache
  data/              data-source interface and providers (NSE + Yahoo, AMFI, CSV)
  notifications.py   alert formatting and delivery (console, Telegram, webhook, email)
  reports.py         tables, CSV and HTML export
  cloud_state.py     splits the database into state and price cache for cloud runs
  cli.py             command-line interface
streamlit_app.py     dashboard
.github/workflows/   GitHub Actions schedule
tests/               test suite
DEPLOY.md            step-by-step deployment guide
```

---

## Extending: replacing the data source

The strategy depends only on the `MarketDataSource` interface in `nse_monitor/data/base.py`:

```python
list_instruments() -> list[Instrument]
fetch_daily_bars(symbols, start, end) -> {symbol: DataFrame[close, volume]}   # split-adjusted closes
fetch_trading_days(start, end) -> list[date]
fetch_shares_outstanding(symbols)  # optional
fetch_fund_aum(isins)              # optional
```

To switch to an official or broker feed (NSE bhavcopy, Kite, Upstox and so on), implement these methods and register the provider in `nse_monitor/data/__init__.py`. The strategy code doesn't change. `data/csv_source.py` is a minimal working example.

---

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest
```

32 tests cover:
- RSI correctness and causality
- the full entry → crossover cycle
- day-by-day runs matching a replay exactly
- holidays and holiday placeholder bars
- suspensions and delistings
- unadjusted split repair
- ETF parsing and AUM ranking
- re-entry after exit, and re-runs having no effect
- alert retry and outbox suppression
- incomplete-day deferral
- the Streamlit dashboard, rendered headlessly

---

## Limitations

- **Free data feeds.** Yahoo Finance and the NSE archive files are unofficial or best-effort sources. The data-quality safeguards above reduce the impact, but for decisions with money at stake, use an official or broker feed.
- **Quarterly ETF AUM.** AMFI publishes scheme-level AUM only as a quarterly average, about a month after each quarter ends. ETFs listed after the latest quarter show no AUM until the next one.
- **Replay uses today's stock list.** Stocks delisted before the replay date are absent (survivorship bias). Live daily runs are not affected.
- **Point-in-time values.** Values recorded at crossover are kept as they were. Market caps in replayed history use current share counts.

> **Disclaimer:** This project is for research and educational use. It is not investment advice.
