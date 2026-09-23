"""Tabular views and file exports (CSV + a self-contained HTML dashboard)."""
from __future__ import annotations

import html
import numbers
from datetime import datetime
from pathlib import Path

import pandas as pd

from .notifications import fmt_date
from .storage import Repository

# Active: largest market cap / AUM first. History: latest exit date first, then largest
# market cap / AUM within the same date (see Repository.active / .history).
ACTIVE_COLUMNS = ["Symbol", "Company", "Type", "Market Cap / AUM (₹ Cr)", "Size Basis", "CMP (Close)", "Volume", "Last Trading Day",
                  "RSI Date", "RSI Value", "Tracking Start Date", "SMA 50", "Days Tracked", "Status"]
HISTORY_COLUMNS = ["NSE Symbol", "Company Name", "Type", "Market Cap / AUM on Crossover (₹ Cr)", "Size Basis", "RSI Date", "RSI Value",
                   "Tracking Start Date", "Crossover Date", "CMP on Crossover Date", "Volume on Crossover Date",
                   "SMA 50 on Crossover Date", "Trading Days Taken", "Status", "Exit Date", "Notes"]
MONEY = ("CMP (Close)", "SMA 50", "CMP on Crossover Date", "SMA 50 on Crossover Date")
CRORE = 1e7
HISTORY_DISPLAY_LIMIT = 30  # dashboard / `history` command show the latest N; the CSV has everything


def _crore(value: float | None) -> float | None:
    return None if value is None else round(value / CRORE, 2)


def _basis(row: dict, value: float | None) -> str:
    if value is None:
        return "-"
    if row["kind"] == "ETF":
        return f"AUM (AMFI avg {row['aum_period']})" if row.get("aum_period") else "AUM (AMFI)"
    return "Market cap"


def active_table(repo: Repository) -> pd.DataFrame:
    rows = [{
        "Symbol": r["symbol"],
        "Company": r["company"],
        "Type": r["kind"],
        "Market Cap / AUM (₹ Cr)": _crore(r["market_cap"]),
        "Size Basis": _basis(r, r["market_cap"]),
        "CMP (Close)": r["cmp"],
        "Volume": None if r["volume"] is None else int(r["volume"]),
        "Last Trading Day": fmt_date(r["last_eval_date"]),
        "RSI Date": fmt_date(r["rsi_date"]),
        "RSI Value": r["rsi_value"],
        "Tracking Start Date": fmt_date(r["tracking_start_date"]) if r["tracking_start_date"] else "Next trading day",
        "SMA 50": r["sma50"],
        "Days Tracked": r["days_tracked"],
        "Status": r["status"],
    } for r in repo.active()]
    df = pd.DataFrame(rows, columns=ACTIVE_COLUMNS)
    df["Volume"] = df["Volume"].astype("Int64")
    return df


def history_table(repo: Repository) -> pd.DataFrame:
    rows = [{
        "NSE Symbol": r["symbol"],
        "Company Name": r["company"],
        "Type": r["kind"],
        "Market Cap / AUM on Crossover (₹ Cr)": _crore(r["market_cap_on_crossover"]),
        "Size Basis": _basis(r, r["market_cap_on_crossover"]),
        "RSI Date": fmt_date(r["rsi_date"]),
        "RSI Value": r["rsi_value"],
        "Tracking Start Date": fmt_date(r["tracking_start_date"]),
        "Crossover Date": fmt_date(r["crossover_date"]),
        "CMP on Crossover Date": r["cmp_on_crossover"],
        "Volume on Crossover Date": None if r["volume_on_crossover"] is None else int(r["volume_on_crossover"]),
        "SMA 50 on Crossover Date": r["sma50_on_crossover"],
        "Trading Days Taken": r["trading_days_taken"],
        "Status": r["status"],
        "Exit Date": fmt_date(r["exit_date"]),
        "Notes": r["notes"] or "",
    } for r in repo.history()]
    df = pd.DataFrame(rows, columns=HISTORY_COLUMNS)
    df["Volume on Crossover Date"] = df["Volume on Crossover Date"].astype("Int64")
    return df


def crossover_stats(repo: Repository) -> dict[str, float]:
    days = pd.Series([r["trading_days_taken"] for r in repo.history() if r["status"] == "SMA50_CROSSED"],
                     dtype=float)
    if days.empty:
        return {"count": 0}
    return {"count": int(days.size), "mean": round(days.mean(), 1), "median": float(days.median()),
            "min": int(days.min()), "max": int(days.max())}


def export_reports(repo: Repository, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    active, hist = active_table(repo), history_table(repo)
    paths = [out_dir / "active_tracking.csv", out_dir / "crossover_history.csv", out_dir / "dashboard.html"]
    active.to_csv(paths[0], index=False, encoding="utf-8-sig")  # BOM so Excel shows ₹/names correctly
    hist.to_csv(paths[1], index=False, encoding="utf-8-sig")
    paths[2].write_text(_dashboard(repo, active, hist), encoding="utf-8")
    return paths


def _table_html(df: pd.DataFrame, money: tuple[str, ...] = MONEY) -> str:
    if df.empty:
        return '<p class="empty">No records yet.</p>'
    head = "".join(f"<th>{html.escape(c)}</th>" for c in df.columns)
    body = []
    for row in df.itertuples(index=False):
        cells = []
        for col, val in zip(df.columns, row):
            if val is None or val is pd.NA or (isinstance(val, float) and pd.isna(val)):
                text, cls = "-", ""
            elif col in money:
                text, cls = f"₹{val:,.2f}", "num"
            elif "(₹ Cr)" in col:
                text, cls = f"₹{val:,.0f} Cr", "num"
            elif isinstance(val, numbers.Integral):
                text, cls = f"{int(val):,}", "num"
            elif isinstance(val, numbers.Real):
                text, cls = f"{val:,.2f}", "num"
            else:
                text, cls = html.escape(str(val)), ""
            cells.append(f'<td class="{cls}">{text}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _shown(hist: pd.DataFrame) -> str:
    if len(hist) <= HISTORY_DISPLAY_LIMIT:
        return f"all {len(hist)} records"
    return f"latest {HISTORY_DISPLAY_LIMIT} of {len(hist)}; full list in crossover_history.csv"


def _dashboard(repo: Repository, active: pd.DataFrame, hist: pd.DataFrame) -> str:
    stats = crossover_stats(repo)
    last = repo.last_processed_day()
    tiles = [("Active tracking", str(len(active))), ("SMA 50 crossovers", str(stats.get("count", 0))),
             ("Avg trading days to cross", str(stats.get("mean", "-"))),
             ("Median days", str(stats.get("median", "-"))), ("Last trading day processed", fmt_date(last))]
    tiles_html = "".join(f'<div class="tile"><div class="v">{v}</div><div class="k">{k}</div></div>' for k, v in tiles)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NSE RSI Monitor</title>
<style>
:root {{ --bg:#f7f7f5; --fg:#1d1d1b; --muted:#6b6b66; --card:#fff; --line:#e3e3de; --accent:#0b6e4f; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161615; --fg:#ececea; --muted:#9a9a94; --card:#1f1f1d; --line:#33332f; --accent:#4cc38a; }} }}
body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; }}
main {{ max-width:1200px; margin:0 auto; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 8px; }}
.sub {{ color:var(--muted); margin-bottom:16px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; }}
.tile {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; }}
.tile .v {{ font-size:22px; font-weight:600; color:var(--accent); font-variant-numeric:tabular-nums; }}
.tile .k {{ color:var(--muted); font-size:12px; }}
.wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:8px; }}
table {{ border-collapse:collapse; width:100%; white-space:nowrap; }}
th, td {{ padding:7px 10px; border-bottom:1px solid var(--line); text-align:left; }}
th {{ font-size:12px; color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--card); }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.empty {{ color:var(--muted); }}
</style></head><body><main>
<h1>NSE RSI &lt; 30 → SMA 50 Monitor</h1>
<div class="sub">Generated {datetime.now():%d-%b-%Y %H:%M}. Closing prices; durations in trading days.</div>
<div class="tiles">{tiles_html}</div>
<h2>Historical crossover database <span class="sub">(latest exit first, then largest market cap / AUM;
{_shown(hist)})</span></h2>
{_table_html(hist.head(HISTORY_DISPLAY_LIMIT))}
<h2>Active tracking list <span class="sub">(largest market cap / AUM first)</span></h2>
{_table_html(active)}
</main></body></html>"""
