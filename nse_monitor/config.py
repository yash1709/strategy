"""Configuration loading.

Settings live in a TOML file (see config.example.toml). Any string value of the
form ``${ENV_VAR}`` is expanded from the environment so secrets (bot tokens,
SMTP passwords) never need to be committed to the file.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class StrategyConfig:
    rsi_period: int = 14
    rsi_threshold: float = 30.0          # screen: RSI strictly below this
    max_daily_move: float = 0.35         # a bigger one-day move is a corporate action/bad print -> adjusted
    sma_period: int = 50                 # exit: close strictly above SMA(sma_period)
    min_history_bars: int = 100          # bars (<= screening day) needed before a stock can be screened
    min_close: float = 0.0               # optional penny-stock filter (0 = off)
    min_avg_volume: float = 0.0          # optional liquidity filter on 20-day avg volume (0 = off)
    stale_after_missing_days: int = 5    # consecutive trading days with no bar -> status DATA_MISSING
    remove_after_missing_days: int = 60  # ... -> moved to history as DATA_UNAVAILABLE
    delist_grace_days: int = 3           # sessions with no bar AND absent from the NSE list before closing as DELISTED
    # --- optional rule variants (all off by default, so the core rules are unchanged) ---
    entry_confirmation: str = "none"     # "none" | "rsi_turn" (also require RSI today > RSI yesterday)
    max_tracking_days: int = 0           # close as TIME_LIMIT after this many sessions without a crossover (0 = off)
    exit_rsi: float = 0.0                # close as RSI_RECOVERED once RSI rises above this value (0 = off)
    stop_loss_pct: float = 0.0           # close as STOP_LOSS if the close falls this % below the RSI-day close (0 = off)


@dataclass
class DataConfig:
    source: str = "yahoo"                # "yahoo" or "csv" (see nse_monitor/data)
    csv_dir: str = "data_csv"            # used by the csv source
    series: list[str] = field(default_factory=lambda: ["EQ"])  # NSE series to include (EQ, BE, BZ, SM...)
    include_etfs: bool = True            # also track NSE-listed ETFs
    # ETF categories ("Underlying Key" in NSE's ETF list) to skip. Liquid/overnight ETFs sit at
    # ~Rs 1000 and move by paise, which makes RSI meaningless.
    etf_exclude_categories: list[str] = field(default_factory=lambda: ["Overnight ETFs and Liquid ETF"])
    history_days: int = 550              # calendar days fetched for a symbol with no cached history
    refresh_overlap_days: int = 20       # calendar days re-fetched each run to detect corporate actions
    corp_action_tolerance: float = 0.005 # relative close mismatch on overlap => full re-download
    batch_size: int = 100
    market_close_cutoff: str = "16:00"   # IST; before this, today's bar is not treated as final
    official_fill_days: int = 10         # recent calendar days whose gaps are filled from NSE's bhavcopy
    min_coverage: float = 0.9            # fraction of universe that must have a bar before a new day is processed
    shares_refresh_days: int = 7         # re-fetch shares outstanding (for market cap) after this many days
    reference_symbol: str = "^NSEI"      # index calendar; fallback only (sessions are derived from stock bars)
    include_bse: bool = False            # also track stocks listed on BSE but not on NSE (symbols end in ".BO")
    bse_groups: list[str] = field(default_factory=lambda: ["A", "B", "X", "XT"])  # BSE trading groups to include
    sector_refresh_days: int = 30        # re-fetch sector/industry (tracked symbols only) after this many days
    sector_batch_limit: int = 150        # at most this many sector look-ups per run (spreads the first fill)
    price_mismatch_pct: float = 1.0      # flag Yahoo-vs-NSE official close differences above this many %


@dataclass
class NotifyConfig:
    console: bool = True
    daily_summary: bool = True
    max_attempts: int = 5
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_url: str = ""                # Slack / Discord / ntfy / Teams-compatible JSON webhook
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = ""
    email_to: list[str] = field(default_factory=list)


@dataclass
class AlertConfig:
    """Which events produce alerts. Crossover alerts are always on (subject to the size/type filters)."""
    on_entry: bool = True                # one digest alert listing new RSI<30 entries
    entry_min_quality: float = 0.0       # only list entries with a quality score at least this high
    entry_max_listed: int = 25           # entries listed in the digest (best quality first)
    on_near_cross: bool = True           # alert when a tracked stock gets close below SMA 50
    near_cross_pct: float = 2.0          # "close" = within this % below SMA 50
    near_cross_max: int = 10             # at most this many near-cross alerts per run
    min_market_cap_cr: float = 0.0       # skip alerts for names smaller than this (Rs crore; 0 = no filter)
    kinds: list[str] = field(default_factory=lambda: ["EQUITY", "ETF"])  # which instrument types alert


@dataclass
class AiConfig:
    """Optional Claude-written notes on new entries (needs ANTHROPIC_API_KEY). Off unless enabled."""
    enabled: bool = False
    model: str = "claude-opus-5-5"
    effort: str = "low"                  # low | medium | high : how hard the model thinks per note
    max_notes_per_run: int = 10          # best-quality entries get a note each run
    use_news: bool = True                # include recent Yahoo headlines as context
    max_tokens: int = 2000


@dataclass
class AppConfig:
    db_path: str = "data/nse_monitor.db"
    reports_dir: str = "reports"
    log_dir: str = "logs"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    data: DataConfig = field(default_factory=DataConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    ai: AiConfig = field(default_factory=AiConfig)

    base_dir: Path = field(default_factory=Path.cwd)

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.base_dir / p


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _apply(target: Any, raw: dict[str, Any], section: str) -> None:
    known = {f.name: f for f in fields(target)}
    for key, value in raw.items():
        if key not in known:
            raise ValueError(f"Unknown config key [{section}] {key}")
        current = getattr(target, key)
        if is_dataclass(current):
            _apply(current, value, key)
        else:
            setattr(target, key, _expand(value))


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config from ``path`` (or ./config.toml if present); defaults otherwise."""
    cfg = AppConfig()
    candidate = Path(path) if path else Path("config.toml")
    if candidate.exists():
        with candidate.open("rb") as fh:
            _apply(cfg, tomllib.load(fh), "root")
        cfg.base_dir = candidate.resolve().parent
    elif path:
        raise FileNotFoundError(candidate)
    return cfg
