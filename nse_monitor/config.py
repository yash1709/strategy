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
class AppConfig:
    db_path: str = "data/nse_monitor.db"
    reports_dir: str = "reports"
    log_dir: str = "logs"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    data: DataConfig = field(default_factory=DataConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

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
