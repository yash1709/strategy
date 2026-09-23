"""Data-source registry. Add new providers here."""
from __future__ import annotations

from ..config import AppConfig
from .base import BAR_COLUMNS, Instrument, MarketDataSource, normalize_bars

__all__ = ["BAR_COLUMNS", "Instrument", "MarketDataSource", "normalize_bars", "create_source"]


def create_source(cfg: AppConfig) -> MarketDataSource:
    name = cfg.data.source.lower()
    if name == "yahoo":
        from .yahoo import YahooNseSource

        return YahooNseSource(series=cfg.data.series, batch_size=cfg.data.batch_size,
                              reference_symbol=cfg.data.reference_symbol, include_etfs=cfg.data.include_etfs,
                              etf_exclude_categories=cfg.data.etf_exclude_categories)
    if name == "csv":
        from .csv_source import CsvSource

        return CsvSource(cfg.path(cfg.data.csv_dir))
    raise ValueError(f"Unknown data source '{cfg.data.source}' (expected 'yahoo' or 'csv')")
