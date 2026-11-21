# NSE RSI Reversal Monitor

**Automated daily screening of NSE stocks and ETFs for oversold conditions (RSI < 30), with trading-day tracking until price recovers above its 50-day moving average.**

[![Live dashboard](https://img.shields.io/badge/Live%20dashboard-Streamlit-FF4B4B?logo=streamlit&logoColor=white)](https://strategy-gmk9uegg9dtzvennq7xyv4.streamlit.app/)
[![Daily NSE monitor](https://github.com/yash1709/strategy/actions/workflows/daily.yml/badge.svg)](https://github.com/yash1709/strategy/actions/workflows/daily.yml)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)

### 🔗 Live dashboard: **https://strategy-gmk9uegg9dtzvennq7xyv4.streamlit.app/**

Updated automatically every NSE trading day. Works in any browser, on any device, with no login.

[![Run now](https://img.shields.io/badge/%E2%96%B6%20Run%20now-trigger%20a%20manual%20update-2EA44F?style=for-the-badge&logo=githubactions&logoColor=white)](https://github.com/yash1709/strategy/actions/workflows/daily.yml)

To update the data now, click **▶ Run now**, then on GitHub click **Run workflow** → **Run workflow** (green button). This needs you to be signed in as the repository owner. The run takes about 2–4 minutes, and the dashboard shows the new data about 5 minutes later (or click **↻ Refresh data** on the dashboard).

---

## Contents
- [What it answers](#what-it-answers)
- [How it works](#how-it-works)
- [Strategy rules](#strategy-rules)
- [Extra features](#extra-features)
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
| Which tracked names look strongest, and which are closest to crossing? | **Quality** score and **% Below SMA 50** columns, sortable on the dashboard |
| Is this stock-specific, or is the whole market oversold? | **Market mood & signals** tab |
| Has the signal actually worked? | **Backtest** tab (forward returns after each entry and crossover) |
| Is today's data complete and trustworthy? | **Data health** tab and automatic alerts |

**Coverage:** all NSE `EQ`-series equities (about 2,400) and NSE-listed ETFs (about 360: equity, gold, silver, debt and international), **plus about 1,400 stocks listed only on BSE** (trading groups A, B, X and XT; symbols end in `.BO`, for example `ANDHRAPET.BO`). A company listed on both exchanges is tracked once, on NSE. Tracking history starts on 1 August 2026.

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
GitHub Actions (Mon–Fri 17:37 IST, then 00:22 & 08:37 IST the next day)
   restore state from `data` branch ─▶ python -m nse_monitor run ─▶ publish state to `data` branch
                                                                          │
Streamlit Community Cloud ◀── reads state.db (refreshed every 5 min) ─────┘
   https://strategy-gmk9uegg9dtzvennq7xyv4.streamlit.app/
```

- **GitHub Actions** runs the job in the cloud three times per trading day (17:37 the same day, then 00:22 and 08:37 the next day); no local machine is involved.
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

## Extra features

None of these change the core rules above unless you switch them on.

**Smarter signals**
- **Quality score (0-100)** on every entry: RSI depth, volume against its 20-day average, trend against SMA 200 (a pull-back in an uptrend scores higher than a broken stock), and whether RSI is turning up. The note says whether the fall is *stock-specific* or a *market-wide selloff* (15% or more of all stocks below RSI 30). It is a transparent heuristic, not a prediction.
- **Optional rule variants** (all off by default, see `[strategy]` in the config): `entry_confirmation = "rsi_turn"`, `max_tracking_days`, `exit_rsi`, `stop_loss_pct`. They add statuses `TIME_LIMIT`, `RSI_RECOVERED` and `STOP_LOSS`. A stock that is still oversold after an early exit can be picked up again as a new cycle.
- **Market mood:** each day records how many stocks are below RSI 30 / above RSI 70 / below SMA 50 / above SMA 200.
- **Other signals** (last 10 days, dashboard): RSI just crossed above 70, new 52-week high or low, golden cross and death cross (SMA 50 against SMA 200).

**Backtest.** For every entry and crossover the job stores the return after 5, 10 and 20 trading days, next to the return of the typical (median) stock over the same days. The dashboard shows win rate, average, median, worst and best, overall and by size, sector, month and speed of crossing. Treat small samples as anecdotes. "By days to cross" uses information known only afterwards, so it is descriptive, not something to trade on. For a bigger sample run `replay --start` further back.

**Dashboard.** Tabs for tracking, a price + SMA 50 + RSI **stock chart** (last 90 sessions, for tracked stocks and those that exited in the last 30 days, to keep the published file small) with the entry and crossover dates marked, market mood and signals, backtest, data health and AI. In the tracking tab you can order by *closest to SMA 50* or *best quality*, filter by sector, keep a **watchlist** (stored in the page address, so a bookmark keeps it) and switch to a **compact view** for phones.

**Alerts.** Besides crossovers: a daily **digest of new RSI<30 entries** (best quality first), a once-per-cycle **near SMA 50** alert (default: within 2%), the daily summary with a market-mood line, a **data-health** alert if data is stale or thin, and a **run-failed** alert from the workflow. Filter by instrument type and minimum size in `[alerts]`. Alerts go to Telegram, a webhook (Slack, Discord, Teams, ntfy) or email; there is no WhatsApp channel built in, but a webhook bridge works.

**AI (optional, off by default).** With `ANTHROPIC_API_KEY` set and `[ai] enabled = true`, the best-quality entries of each run get a short plain-English note and a news-risk flag (LOW/MEDIUM/HIGH) from Claude, using recent Yahoo headlines. The dashboard's **Ask AI** tab turns a question such as *"large caps that crossed in under 5 days"* into a filter that the app itself applies to the tables; Claude never runs code and never sees the data. Any AI failure is logged and skipped; the daily job never depends on it. This costs API credits (about 10 short requests per run at most).

**One-time catch-up.** The first run after an upgrade runs a back-fill once (even if there is no new trading day): forward returns and chart data from the price cache, quality scores for stocks already being tracked (marked "backfilled", without market context), and the last 60 days of market mood and extra signals. It never changes past decisions. `analytics` does the same on demand.

**More data safety.** Per-stock gaps are filled from NSE's official end-of-day file *before* a day is processed (previously only below 90% coverage), Yahoo is cross-checked against the official close (differences over 1% are flagged, never overwritten), and a stock missing from the NSE list needs three missing sessions before it is closed as `DELISTED`. Alert delivery errors no longer store or print the secret URL.

**BSE-only stocks.** With `data.include_bse = true` (on in both shipped configs) the job also tracks stocks that are listed on BSE but not on NSE, using exactly the same rules.
- *Where the list comes from:* BSE's scrip-list API refuses scripted requests (HTTP 403), so the universe is read from BSE's daily end-of-day file, which has every traded security with its ISIN, ticker, group, close and volume. If that file cannot be fetched, the cached universe is kept (nothing is marked delisted).
- *No double counting:* a BSE security whose ISIN is already on NSE is skipped, so dual-listed companies appear once, on NSE.
- *Which groups:* `data.bse_groups` defaults to A, B, X, XT. M (SME), F (debt), G (government securities), Z (non-compliant) and T (trade-for-trade) are left out; add them in the config if you want them.
- *Prices:* Yahoo Finance `<TICKER>.BO` (59 of 60 randomly sampled BSE-only tickers had data when I checked), with BSE's official file filling recent gaps and cross-checking closes, the same way NSE's does.
- *Doesn't hold up the day:* many small BSE names have no bar on an ordinary day, so the 90% coverage check and the trading calendar use NSE stocks only. Missing BSE bars are handled like any other missing data. Market mood is also measured on NSE stocks only.
- *On the dashboard:* an **Exchange** filter (NSE / BSE) and an Exchange column in both tables and the CSVs.
- *Caveats:* BSE-only small caps are thinly traded, so RSI and SMA 50 are noisier and a close can be a stale last trade; Yahoo's BSE bars can lag by a day, and the first run downloads about 1,400 extra histories. Set `include_bse = false` to go back to NSE only.

**Wider coverage.** Add NSE series in `data.series` (for example `["EQ", "BE"]`). Other markets (US and so on) are not built in; implement the `MarketDataSource` interface to add one.

---

## Data sources and data quality

| Data | Source | Refresh |
|---|---|---|
| Equity universe | NSE [`EQUITY_L.csv`](https://archives.nseindia.com/content/equities/EQUITY_L.csv) | Every run |
| ETF universe | NSE [`eq_etfseclist.csv`](https://archives.nseindia.com/content/equities/eq_etfseclist.csv) | Every run |
| BSE-only universe | BSE end-of-day file (`BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV`, latest available day), minus ISINs already on NSE | Every run |
| Daily closes and volume | Yahoo Finance (`SYMBOL.NS`; BSE-only stocks `TICKER.BO`) | Every run, re-downloading the last 20 days |
| Recent sessions Yahoo hasn't completed | NSE official end-of-day file ([bhavcopy](https://archives.nseindia.com/products/content/sec_bhavdata_full_23092026.csv)) and BSE's equivalent for BSE-only stocks | Recent days only, filling gaps and never overwriting Yahoo |
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

- **Tabs:** Tracking · Stock chart · Market mood & signals · Backtest · Data health · Ask AI (needs `ANTHROPIC_API_KEY` in the app secrets).
- **Summary:** active count, total crossovers, average and median trading days to cross, and the latest trading day.
- **Historical Crossover Database:** latest exit first, then largest market cap / AUM. Shows the latest 30, with a toggle for the full history.
- **Active Tracking List:** market cap / AUM, the size basis, CMP (close), volume, RSI date and value, tracking start, SMA 50 and days tracked. It can be filtered by type (stock or ETF), exchange (NSE or BSE), sector, symbol or company name, and minimum size.
- **CSV downloads** for both tables, and a **Refresh** button. Data is otherwise cached for 5 minutes.
- **▶ Run an update now:** a password-protected panel that starts the cloud job, shows its progress, and loads the new data when it finishes (about 2–4 minutes). Setup is in [DEPLOY.md](DEPLOY.md#5-enable-the-dashboards-run-update-now-button-optional).

Streamlit Community Cloud puts apps to sleep after a period with no visitors. The first visit after that takes about 30 seconds while the app starts.

---

## Automation and operations

| | |
|---|---|
| Schedule | Mon–Fri **17:37 IST**, then **00:22 IST** (after NSE's end-of-day file, published around 23:55) and **08:37 IST** the next day (Tue–Sat). Runs are set to odd minutes because GitHub starts runs scheduled on the hour hours late at busy times, via [GitHub Actions](https://github.com/yash1709/strategy/actions/workflows/daily.yml). By the 09:15 market open, the previous session is always on the dashboard |
| Manual run | [**▶ Run now**](https://github.com/yash1709/strategy/actions/workflows/daily.yml) → **Run workflow** → **Run workflow**. Only the repository owner can run it (sign-in required). Safe to run at any time: already-processed days are skipped |
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

**1. One-time setup** (PowerShell, in the project folder):

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
copy config.example.toml config.toml
```

**2a. See it in about a minute, with made-up data (no internet needed):**

```powershell
.\.venv\Scripts\python.exe -m nse_monitor demo
$env:NSE_MONITOR_DB = "data/demo.db"
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py        # opens http://localhost:8501
```

The demo database is separate (`data/demo.db`), so it never touches real data. Rebuild it with `demo --force`.

**2b. Real data** (needs internet; the first run downloads about 2 years of prices, roughly 3-10 minutes):

```powershell
Remove-Item Env:NSE_MONITOR_DB -ErrorAction SilentlyContinue         # stop pointing the dashboard at the demo
.\.venv\Scripts\python.exe -m nse_monitor replay --start 2026-08-01  # backfill history (on an empty database)
.\.venv\Scripts\python.exe -m nse_monitor run                        # then daily: process new trading days
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

For AI notes and the Ask AI tab, set `$env:ANTHROPIC_API_KEY = "sk-ant-..."` (and `enabled = true` under `[ai]` in `config.toml` for the daily notes).

| Command | Purpose |
|---|---|
| `run [--as-of DATE] [--no-notify]` | Process every unprocessed trading day, send alerts, export reports |
| `replay --start DATE [--end DATE]` | Replay history into an empty database. Its alerts are not sent |
| `status` | Print the active tracking list |
| `history [--limit N]` | Print the latest N crossovers (default 30; `0` for all) with statistics |
| `audit [--symbol X]` | Show the audit trail: entries, crossovers, data gaps and price adjustments |
| `export` | Write `reports/active_tracking.csv`, `crossover_history.csv` and `dashboard.html` |
| `notify-test` / `retry-alerts` | Test notification channels / resend failed alerts |
| `demo [--force]` | Build an offline demo database of made-up stocks (`data/demo.db`) |
| `backtest` | Print forward returns after each entry and crossover, with breakdowns |
| `analytics` | Rebuild forward returns, chart data and sectors from the price cache |
| `health [--no-notify]` | Check the data is fresh and complete; alert if not (exit code 2 on problems) |
| `alert SUBJECT [BODY]` | Send a one-off message on every channel (used for the run-failed alert) |

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
| `data.include_bse` / `bse_groups` | false in code, true in the shipped configs / `["A","B","X","XT"]` | Also track BSE-only stocks, and which BSE groups |
| `data.include_etfs` / `etf_exclude_categories` | `true` / liquid & overnight | ETF coverage |
| `data.min_coverage` | 0.9 | Share of stocks that must have a price before a day is processed |
| `data.market_close_cutoff` | `16:00` | Time (IST) after which today's close is treated as final |
| `strategy.delist_grace_days` | 3 | Sessions missing from the NSE list *and* without a price before closing as DELISTED |
| `strategy.entry_confirmation` / `max_tracking_days` / `exit_rsi` / `stop_loss_pct` | none / 0 / 0 / 0 | Optional rule variants, off by default |
| `data.price_mismatch_pct` | 1.0 | Flag Yahoo vs NSE official close differences above this |
| `alerts.*` | see file | Which alerts are sent, and size / type filters |
| `ai.*` | off | Optional Claude notes (`model`, `effort`, `max_notes_per_run`, `use_news`) |

---

## Project structure

```
nse_monitor/
  strategy.py        core rules: screening, tracking, crossover (no data-source code)
  indicators.py      Wilder RSI, SMA (causal)
  cleaning.py        corporate-action / bad-print repair
  engine.py          daily pipeline: universe → prices → calendar → per-day processing → alerts → reports
  storage.py         SQLite: tracking, history, audit log, alert outbox, price cache
  data/              data-source interface and providers (NSE + BSE + Yahoo, AMFI, CSV)
  notifications.py   alert formatting and delivery (console, Telegram, webhook, email)
  analytics.py       forward returns (backtest), chart data, data-health checks
  ai.py              optional Claude notes and the safe "ask your data" filter
  demo.py            offline demo database
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
.\.venv\Scripts\python.exe -m pytest        # about 1.5 minutes
```

117 tests cover the core rules, BSE parsing and universe building, the extra features (quality score, optional exits, breadth and signals, backtest, alerts and filters, gap filling, sectors, health checks, AI helpers with a fake client, CLI) and the dashboard. The AI calls themselves are only tested against a fake client, never the live API. The core tests include:
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
- **BSE data is thinner.** BSE-only small caps trade lightly, so signals on them are noisier, and Yahoo's BSE coverage and timeliness are not guaranteed. The BSE list is built from the daily end-of-day file because BSE blocks its scrip-list API; if BSE changes that file's format or location, the BSE part stops working until the parser is updated (NSE names are unaffected unless the failure happens during the universe refresh, in which case the cached list is used).
- **Quarterly ETF AUM.** AMFI publishes scheme-level AUM only as a quarterly average, about a month after each quarter ends. ETFs listed after the latest quarter show no AUM until the next one.
- **Replay uses today's stock list.** Stocks delisted before the replay date are absent (survivorship bias). Live daily runs are not affected.
- **Point-in-time values.** Values recorded at crossover are kept as they were. Market caps in replayed history use current share counts.

> **Disclaimer:** This project is for research and educational use. It is not investment advice.
