# NSE RSI < 30 → SMA 50 Crossover Monitor

Runs every NSE trading day and answers two questions:

1. **Which NSE stocks recently fell below RSI 30 and are being tracked right now?** → *Active Tracking List*
2. **How many trading days did each one take for its close to cross above SMA 50?** → *Historical Crossover Database*

```
NSE equity list → daily closes → RSI(14) → RSI < 30 screen → add to tracking list
→ (next trading day) CMP vs SMA 50 each day → CMP > SMA 50 → alert → move to history
```

## Quick start (Windows)

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy config.example.toml config.toml        # edit notifications/filters as needed

.\.venv\Scripts\python.exe -m nse_monitor run        # daily job (first run downloads ~1.5y of history, a few minutes)
.\.venv\Scripts\python.exe -m nse_monitor status     # active tracking list
.\.venv\Scripts\python.exe -m nse_monitor history    # crossover history + average days to cross

powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1   # schedule it for every weekday
```

Each run also writes `reports/active_tracking.csv`, `reports/crossover_history.csv` and
`reports/dashboard.html`.

### Commands

| Command | What it does |
| --- | --- |
| `run [--as-of YYYY-MM-DD] [--no-notify]` | Process every unprocessed trading day up to today, send alerts, export reports |
| `replay --start YYYY-MM-DD [--end ...] [--notify]` | Backfill / simulate from a past date. Use a fresh database: `--db data/replay.db` |
| `status` / `history` | Print the active list / the completed records with statistics |
| `audit [--symbol X]` | Every entry, start, crossover, data gap, delisting and price adjustment |
| `export` | Rewrite the CSV/HTML reports |
| `notify-test` / `retry-alerts` | Test notification channels / resend failed alerts |

## Rules and how they are implemented

| Rule | Implementation |
| --- | --- |
| Daily closing prices everywhere | RSI, CMP and SMA 50 all use the daily close (`nse_monitor/strategy.py`) |
| RSI(14) | Wilder's smoothing (`indicators.py`), verified against an independent implementation |
| Entry | First trading day with RSI < 30, stock has ≥ `min_history_bars` bars. The date and RSI value are recorded |
| No duplicates | A tracked stock is never re-added (`UNIQUE(symbol)` on the active table). After it exits, a new RSI < 30 day starts a new cycle |
| Tracking starts next trading day | `tracking_start_date` is filled in when the next session is actually processed, so holidays are handled automatically. Until then the status is `PENDING_START` |
| Exit | First session from the tracking start where close > SMA 50 (strictly greater) |
| Trading days taken | Trading sessions from tracking start to crossover day, both counted. Crossing on the first tracked day = 1. Equals the number of sessions after the RSI date |
| No look-ahead | Each day is processed using only bars dated on or before it. Indicators are causal, and a test checks that running day by day with only past data gives exactly the same result as a replay |
| Holidays | A day counts as a trading session only if at least half of NSE stocks actually traded that day. Yahoo publishes placeholder bars on NSE holidays (zero volume, previous close repeated), and its NIFTY index feed lists those days as sessions. Those bars are discarded, so holidays are never processed or counted. This was seen in live data on 1-May, 28-May, 26-Jun and 14-Sep-2026 |
| Missed runs | The next run catches up every unprocessed trading day, in order |
| Late or partial data | While the newest day has bars for fewer than `min_coverage` (90%) of stocks, it waits for the next run. If a later day is complete, the gap is permanent at the provider, so the day is processed and logged as `LOW_COVERAGE_DAY`. This way the system never stalls |
| Suspended stocks / data gaps | The day still counts as a trading day. The stock is not evaluated and `missing_days` goes up. After 5 sessions the status is `DATA_MISSING`. After 60 the stock moves to history as `DATA_UNAVAILABLE` |
| Delisted stocks | Once a stock leaves the NSE list and has no more bars, it moves to history as `DELISTED` |
| Corporate actions (split/bonus) | Closes are split-adjusted. Each run re-downloads the last 20 days. If they differ from the cached prices by more than 0.5%, the stock's full history is downloaded again and an audit entry is written |
| Splits, demergers and bad prints the provider didn't adjust | NSE price bands cap a normal session's move at 20%. So any one-day move beyond ±35% (`max_daily_move`) is treated as a corporate action or a data error, and all earlier prices are rescaled to remove the jump. If the move matches a standard split or bonus ratio (1:2, 1:10, 1:100, 2:3 and so on), that exact ratio is used. Otherwise the day's move is taken as zero, which also cancels one-day glitches. This matters in practice: Yahoo left many ETF unit splits unadjusted (for example IVZINGOLD and LICMFGOLD at 1:100, PSUBANK at 1:10) and several demergers too (for example VEDL, RAYMOND, INDIAGLYCO). Without the repair, RSI stays falsely below 30 for months. Decisions use only data up to each day, so there is no look-ahead. CMP and SMA 50 are always shown in real traded prices for that day. The cached raw prices are never modified, and every adjustment is recorded in the audit log as `PRICE_DISCONTINUITY_ADJUSTED` |
| ETFs | Tracked together with stocks, using the same rules, from NSE's official ETF list (`eq_etfseclist.csv`), and labelled `ETF` in the Type column. Liquid and overnight ETFs are left out, because their price stays near ₹1,000 and moves only by paise, which makes RSI meaningless. Change this with `etf_exclude_categories`, or set `include_etfs = false` to turn ETFs off. ETFs are sized by AUM from AMFI instead of market cap (see the next row) |
| ETF AUM (AMFI) | Each ETF's ISIN from the NSE list is matched to its AMFI scheme code using AMFI's daily NAV file (`NAVAll.txt`). AUM comes from AMFI's scheme-wise average-AUM data, the source behind amfiindia.com/aum-data/average-aum. AMFI publishes it per quarter, as the quarter's average, about a month after the quarter ends. The **Size Basis** column shows which figure applies to each row, for example `AUM (AMFI avg Apr-Jun 2026)`. Stocks (by market cap) and ETFs (by AUM) are ranked together in one "Market Cap / AUM" column. ETFs listed after the latest published quarter have no AUM until the next one. AUM is refreshed weekly, in one bulk download |
| Market cap, volume, close | Both tables are sorted by market cap, largest first. Market cap = shares outstanding × closing price, shown in ₹ crore. Share counts come from Yahoo and are refreshed every 7 days, or right away after a split or bonus. Volume and close are for the stock's last trading day. For completed records, market cap and volume are frozen on the crossover date. Stocks Yahoo has no share count for show a blank market cap and are listed last |
| Audit trail | `audit_log` stores every event. `daily_observations` stores CMP, SMA 50 and RSI for every tracked stock on every day |
| Atomic and repeatable | Each trading day is one database transaction. Re-running is harmless, and alerts go through an outbox so failed sends are retried |

### Status values

* **Active list:** `PENDING_START`, `ACTIVE`, `DATA_MISSING`, `INSUFFICIENT_DATA` (fewer than 50 bars so far)
* **History:** `SMA50_CROSSED`, `DELISTED`, `DATA_UNAVAILABLE`

## Notifications

Configure in `config.toml`. Any combination works:
console, Telegram, a webhook (Slack, Discord, Teams or ntfy) and email (SMTP). Crossover alert:

```
Stock: ABC (ABC Ltd)
RSI Entry Date: 24-Sep-2026
RSI: 28.5
Crossover Date: 03-Oct-2026
CMP: ₹125.40
SMA 50: ₹124.80
Trading Days Taken: 7
Status: SMA 50 Crossed
```

With `daily_summary = true` you also get one summary per run listing new entries and crossovers.

## Swapping the data source

The strategy only talks to `MarketDataSource` (`nse_monitor/data/base.py`), which has three methods:
`list_instruments()`, `fetch_daily_bars(symbols, start, end)` and `fetch_trading_days(start, end)`.
To add a new provider (NSE bhavcopy, a broker API such as Kite or Upstox, or a paid feed),
implement those methods and register the provider in `nse_monitor/data/__init__.py`.
`fetch_daily_bars` must return split-adjusted closes. `data/csv_source.py` is a small working
example that reads local CSV files.

The default provider uses NSE's official `EQUITY_L.csv` for the stock list and Yahoo Finance
(`SYMBOL.NS`) for prices.

## Caveats

* Yahoo Finance is free and unofficial. It sometimes has gaps or delays. The coverage check,
  the retry run and catch-up limit the damage, but for production money decisions use an
  official or broker feed.
* `replay` uses today's stock list, so stocks delisted before today are missing from the
  replay (survivorship bias). Live daily runs are not affected.
* Values stored at crossover time are kept as they were. If a split happens later, those
  stored prices are not rescaled.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The tests cover RSI correctness and causality, the full entry→crossover cycle, live day-by-day
runs matching a replay exactly, holidays, suspensions, delisting, split adjustment, re-entry,
idempotent re-runs, alert retry and deferral of incomplete days.
