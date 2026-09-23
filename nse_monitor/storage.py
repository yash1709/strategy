"""SQLite persistence: price cache, trading calendar, active tracking list,
crossover history, per-day observations, audit log and the alert outbox."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd

from .data.base import Instrument

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    symbol       TEXT PRIMARY KEY,
    company      TEXT,
    series       TEXT,
    isin         TEXT,
    listing_date TEXT,
    first_seen   TEXT,
    last_seen    TEXT,
    is_listed    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    close  REAL NOT NULL,
    volume REAL,
    PRIMARY KEY (symbol, date)
) WITHOUT ROWID;

-- How far back each symbol's cached history was requested (decides full vs incremental refresh).
CREATE TABLE IF NOT EXISTS price_meta (
    symbol       TEXT PRIMARY KEY,
    fetched_from TEXT NOT NULL,
    refreshed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trading_days (date TEXT PRIMARY KEY);

CREATE TABLE IF NOT EXISTS processed_days (
    date         TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    screened     INTEGER, added INTEGER, crossed INTEGER, removed INTEGER
);

-- One row per stock currently being tracked. UNIQUE(symbol) makes duplicate
-- tracking records impossible.
CREATE TABLE IF NOT EXISTS active_tracking (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL UNIQUE,
    company             TEXT,
    rsi_date            TEXT NOT NULL,
    rsi_value           REAL NOT NULL,
    rsi_close           REAL,
    tracking_start_date TEXT,          -- first trading day after rsi_date (NULL until it happens)
    last_eval_date      TEXT,
    cmp                 REAL,
    sma50               REAL,
    days_tracked        INTEGER NOT NULL DEFAULT 0,
    missing_days        INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crossover_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_id         INTEGER NOT NULL UNIQUE,
    symbol              TEXT NOT NULL,
    company             TEXT,
    rsi_date            TEXT NOT NULL,
    rsi_value           REAL NOT NULL,
    tracking_start_date TEXT,
    crossover_date      TEXT,
    cmp_on_crossover    REAL,
    sma50_on_crossover  REAL,
    trading_days_taken  INTEGER,
    status              TEXT NOT NULL,   -- SMA50_CROSSED | DELISTED | DATA_UNAVAILABLE
    exit_date           TEXT NOT NULL,
    notes               TEXT,
    closed_at           TEXT NOT NULL,
    UNIQUE (symbol, rsi_date)
);

CREATE TABLE IF NOT EXISTS daily_observations (
    tracking_id INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,
    close       REAL,
    sma50       REAL,
    rsi         REAL,
    above_sma   INTEGER,
    PRIMARY KEY (tracking_id, date)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    trade_date TEXT,
    symbol     TEXT,
    event      TEXT NOT NULL,
    details    TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_symbol ON audit_log(symbol);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    trade_date  TEXT,
    symbol      TEXT,
    kind        TEXT NOT NULL,
    channel     TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT
);
"""

# Columns added after the first release; created on existing databases by _migrate().
MIGRATIONS = {
    "instruments": {"shares_outstanding": "REAL", "shares_updated": "TEXT", "kind": "TEXT DEFAULT 'EQUITY'",
                    "aum": "REAL", "aum_period": "TEXT", "aum_updated": "TEXT"},
    "active_tracking": {"volume": "REAL", "market_cap": "REAL"},
    "crossover_history": {"volume_on_crossover": "REAL", "market_cap_on_crossover": "REAL"},
}


def _d(value: date | str | None) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else value.isoformat()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Repository:
    def __init__(self, db_path: str | Path):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._depth = 0

    def _migrate(self) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic unit of work (re-entrant). A trading day is processed inside one
        transaction, so a crash can never leave it half-applied."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params))]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    # -- instruments ----------------------------------------------------------
    def sync_instruments(self, instruments: list[Instrument], seen: date) -> tuple[list[str], list[str]]:
        """Upsert the current listing; mark vanished symbols as unlisted.
        Returns (newly_listed, newly_unlisted)."""
        current = {i.symbol: i for i in instruments}
        before = {r["symbol"]: r["is_listed"] for r in self._all("SELECT symbol, is_listed FROM instruments")}
        with self.transaction():
            for inst in instruments:
                self.conn.execute(
                    """INSERT INTO instruments(symbol, company, series, isin, listing_date, first_seen, last_seen,
                           is_listed, kind)
                       VALUES (?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(symbol) DO UPDATE SET company=excluded.company, series=excluded.series,
                           isin=excluded.isin, listing_date=excluded.listing_date,
                           last_seen=excluded.last_seen, is_listed=1, kind=excluded.kind""",
                    (inst.symbol, inst.company, inst.series, inst.isin, _d(inst.listing_date), _d(seen), _d(seen),
                     inst.kind),
                )
            gone = [s for s, listed in before.items() if listed and s not in current]
            for symbol in gone:
                self.conn.execute("UPDATE instruments SET is_listed=0 WHERE symbol=?", (symbol,))
        new = [s for s in current if not before.get(s)]
        return new, gone

    def listed_symbols(self) -> dict[str, str]:
        return {r["symbol"]: r["company"] or "" for r in
                self._all("SELECT symbol, company FROM instruments WHERE is_listed=1 ORDER BY symbol")}

    def instrument(self, symbol: str) -> dict | None:
        return self._one("SELECT * FROM instruments WHERE symbol=?", (symbol,))

    # -- shares outstanding (for market cap) ------------------------------------
    def shares_outstanding(self, symbol: str) -> float | None:
        row = self._one("SELECT shares_outstanding FROM instruments WHERE symbol=?", (symbol,))
        return row["shares_outstanding"] if row else None

    def symbols_needing_shares(self, symbols: Iterable[str], max_age_days: int, today: date) -> list[str]:
        cutoff = _d(today - timedelta(days=max_age_days))
        fresh = {r["symbol"] for r in self._all(
            """SELECT symbol FROM instruments
               WHERE (shares_outstanding IS NOT NULL AND shares_updated >= ?) OR kind = 'ETF'""", (cutoff,))}
        return sorted(set(symbols) - fresh)

    def set_shares(self, shares: dict[str, float], today: date) -> None:
        with self.transaction():
            self.conn.executemany("UPDATE instruments SET shares_outstanding=?, shares_updated=? WHERE symbol=?",
                                  [(float(v), _d(today), s) for s, v in shares.items()])

    def expire_shares(self, symbols: Iterable[str]) -> None:
        """Force a re-fetch (e.g. after a split/bonus changes the share count)."""
        self.conn.executemany("UPDATE instruments SET shares_updated=NULL WHERE symbol=?", [(s,) for s in symbols])

    # -- ETF AUM (AMFI) -----------------------------------------------------------
    def etf_isins(self) -> dict[str, str]:
        """{ISIN: symbol} for listed ETFs."""
        return {r["isin"]: r["symbol"] for r in self._all(
            "SELECT symbol, isin FROM instruments WHERE kind='ETF' AND is_listed=1 AND isin LIKE 'INF%'")}

    def aum_is_stale(self, max_age_days: int, today: date) -> bool:
        row = self._one("""SELECT COUNT(*) AS n FROM instruments WHERE kind='ETF' AND is_listed=1
                           AND (aum_updated IS NULL OR aum_updated < ?)""", (_d(today - timedelta(days=max_age_days)),))
        return bool(row and row["n"])

    def set_aum(self, period: str | None, aum_by_isin: dict[str, float], today: date) -> None:
        """Store the latest AUM for every listed ETF (NULL for those AMFI has no figure for yet)."""
        with self.transaction():
            for isin, symbol in self.etf_isins().items():
                value = aum_by_isin.get(isin)
                self.conn.execute("UPDATE instruments SET aum=?, aum_period=?, aum_updated=? WHERE symbol=?",
                                  (value, period if value is not None else None, _d(today), symbol))

    def market_size(self, symbol: str, close: float) -> float | None:
        """Stocks: shares outstanding x close. ETFs: latest AMFI AUM."""
        row = self._one("SELECT kind, shares_outstanding, aum FROM instruments WHERE symbol=?", (symbol,))
        if not row:
            return None
        if row["kind"] == "ETF":
            return row["aum"]
        return row["shares_outstanding"] * close if row["shares_outstanding"] else None

    def refresh_market_caps(self) -> None:
        """Active list: size = shares x latest close (stocks) or AMFI AUM (ETFs). History: fill in
        the size on the crossover date once known (it is never changed after that)."""
        with self.transaction():
            # repair rows created before close/volume were recorded on the RSI day
            self.conn.execute(
                """UPDATE active_tracking SET cmp = COALESCE(cmp, rsi_close), last_eval_date = rsi_date
                   WHERE last_eval_date IS NULL""")
            # volume backfill for rows created before the volume columns existed
            self.conn.execute(
                """UPDATE active_tracking SET volume = (SELECT p.volume FROM prices p
                       WHERE p.symbol = active_tracking.symbol AND p.date = active_tracking.last_eval_date)
                   WHERE volume IS NULL AND last_eval_date IS NOT NULL""")
            self.conn.execute(
                """UPDATE crossover_history SET volume_on_crossover = (SELECT p.volume FROM prices p
                       WHERE p.symbol = crossover_history.symbol AND p.date = crossover_history.crossover_date)
                   WHERE volume_on_crossover IS NULL AND crossover_date IS NOT NULL""")
            self.conn.execute(
                """UPDATE active_tracking SET market_cap = (
                       SELECT CASE WHEN i.kind = 'ETF' THEN i.aum
                                   ELSE i.shares_outstanding * COALESCE(active_tracking.cmp, active_tracking.rsi_close)
                              END
                       FROM instruments i WHERE i.symbol = active_tracking.symbol)""")
            self.conn.execute(
                """UPDATE crossover_history SET market_cap_on_crossover = (
                       SELECT CASE WHEN i.kind = 'ETF' THEN i.aum
                                   ELSE i.shares_outstanding * crossover_history.cmp_on_crossover END
                       FROM instruments i WHERE i.symbol = crossover_history.symbol)
                   WHERE market_cap_on_crossover IS NULL AND cmp_on_crossover IS NOT NULL""")

    def history_symbols(self) -> set[str]:
        return {r["symbol"] for r in self._all("SELECT DISTINCT symbol FROM crossover_history")}

    # -- prices ---------------------------------------------------------------
    def last_price_dates(self) -> dict[str, date]:
        return {r["symbol"]: date.fromisoformat(r["d"]) for r in
                self._all("SELECT symbol, MAX(date) AS d FROM prices GROUP BY symbol")}

    def get_prices(self, symbol: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        rows = self._all(
            "SELECT date, close, volume FROM prices WHERE symbol=? AND date>=? AND date<=? ORDER BY date",
            (symbol, _d(start) or "0000", _d(end) or "9999"),
        )
        df = pd.DataFrame(rows, columns=["date", "close", "volume"])
        df.index = [date.fromisoformat(x) for x in df.pop("date")]
        return df

    def upsert_prices(self, symbol: str, bars: pd.DataFrame, replace: bool = False) -> None:
        with self.transaction():
            if replace:
                self.conn.execute("DELETE FROM prices WHERE symbol=?", (symbol,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO prices(symbol, date, close, volume) VALUES (?,?,?,?)",
                [(symbol, _d(d), float(r.close), None if pd.isna(r.volume) else float(r.volume))
                 for d, r in bars.iterrows()],
            )

    def bar_counts_by_date(self, start: date, end: date) -> dict[date, int]:
        return {date.fromisoformat(r["date"]): r["n"] for r in self._all(
            "SELECT date, COUNT(*) AS n FROM prices WHERE date>=? AND date<=? GROUP BY date", (_d(start), _d(end)))}

    def fetched_from(self) -> dict[str, date]:
        return {r["symbol"]: date.fromisoformat(r["fetched_from"]) for r in self._all("SELECT * FROM price_meta")}

    def set_fetched_from(self, symbol: str, start: date) -> None:
        self.conn.execute(
            """INSERT INTO price_meta(symbol, fetched_from, refreshed_at) VALUES (?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET fetched_from=excluded.fetched_from, refreshed_at=excluded.refreshed_at""",
            (symbol, _d(start), _now()),
        )

    def fill_missing_bars(self, d: date, bars: pd.DataFrame, symbols: set[str]) -> int:
        """Insert bars for ``d`` only where none exists (never overwrites). Returns rows added."""
        rows = [(s, _d(d), float(r.close), None if pd.isna(r.volume) else float(r.volume))
                for s, r in bars.iterrows() if s in symbols]
        before = self.conn.total_changes
        with self.transaction():
            self.conn.executemany("INSERT OR IGNORE INTO prices(symbol, date, close, volume) VALUES (?,?,?,?)", rows)
        return self.conn.total_changes - before

    def load_all_prices(self) -> dict[str, pd.DataFrame]:
        df = pd.read_sql_query("SELECT symbol, date, close, volume FROM prices ORDER BY symbol, date", self.conn)
        out = {}
        for symbol, grp in df.groupby("symbol", sort=False):
            frame = grp[["close", "volume"]].copy()
            frame.index = [date.fromisoformat(x) for x in grp["date"]]
            out[symbol] = frame
        return out

    # -- trading calendar -----------------------------------------------------
    def add_trading_days(self, days: Iterable[date]) -> None:
        with self.transaction():
            self.conn.executemany("INSERT OR IGNORE INTO trading_days(date) VALUES (?)", [(_d(d),) for d in days])

    def replace_trading_days(self, start: date, end: date, days: Iterable[date]) -> None:
        with self.transaction():
            self.conn.execute("DELETE FROM trading_days WHERE date>=? AND date<=?", (_d(start), _d(end)))
            self.add_trading_days(days)

    def trading_days(self, start: date | None = None, end: date | None = None) -> list[date]:
        return [date.fromisoformat(r["date"]) for r in self._all(
            "SELECT date FROM trading_days WHERE date>=? AND date<=? ORDER BY date",
            (_d(start) or "0000", _d(end) or "9999"))]

    def count_trading_days(self, start: date, end: date) -> int:
        """Trading sessions in [start, end], both inclusive."""
        row = self._one("SELECT COUNT(*) AS n FROM trading_days WHERE date>=? AND date<=?", (_d(start), _d(end)))
        return int(row["n"])

    # -- processed days -------------------------------------------------------
    def last_processed_day(self) -> date | None:
        row = self._one("SELECT MAX(date) AS d FROM processed_days")
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def is_processed(self, d: date) -> bool:
        return self._one("SELECT 1 AS x FROM processed_days WHERE date=?", (_d(d),)) is not None

    def mark_processed(self, d: date, stats: dict[str, int]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO processed_days(date, processed_at, screened, added, crossed, removed) VALUES (?,?,?,?,?,?)",
            (_d(d), _now(), stats.get("screened", 0), stats.get("added", 0), stats.get("crossed", 0), stats.get("removed", 0)),
        )

    # -- active tracking ------------------------------------------------------
    def active(self) -> list[dict]:
        return self._all("""SELECT a.*, COALESCE(i.kind, 'EQUITY') AS kind, i.aum_period FROM active_tracking a
                              LEFT JOIN instruments i ON i.symbol = a.symbol
                              ORDER BY a.market_cap IS NULL, a.market_cap DESC, a.symbol""")

    def active_symbols(self) -> set[str]:
        return {r["symbol"] for r in self._all("SELECT symbol FROM active_tracking")}

    def add_active(self, symbol: str, company: str, rsi_date: date, rsi_value: float, rsi_close: float,
                   status: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO active_tracking(symbol, company, rsi_date, rsi_value, rsi_close, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (symbol, company, _d(rsi_date), rsi_value, rsi_close, status, _now(), _now()),
        )
        return int(cur.lastrowid)

    def update_active(self, tracking_id: int, **values: Any) -> None:
        values = {k: _d(v) if isinstance(v, date) else v for k, v in values.items()}
        values["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in values)
        self.conn.execute(f"UPDATE active_tracking SET {cols} WHERE id=?", (*values.values(), tracking_id))

    def close_tracking(self, record: dict, status: str, exit_date: date, crossover_date: date | None = None,
                       cmp: float | None = None, sma50: float | None = None, days_taken: int | None = None,
                       notes: str = "", volume: float | None = None, market_cap: float | None = None) -> None:
        """Move an active record into crossover_history (atomically)."""
        with self.transaction():
            self.conn.execute(
                """INSERT INTO crossover_history(tracking_id, symbol, company, rsi_date, rsi_value, tracking_start_date,
                       crossover_date, cmp_on_crossover, sma50_on_crossover, trading_days_taken, status, exit_date,
                       notes, closed_at, volume_on_crossover, market_cap_on_crossover)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record["id"], record["symbol"], record["company"], record["rsi_date"], record["rsi_value"],
                 record["tracking_start_date"], _d(crossover_date), cmp, sma50, days_taken, status,
                 _d(exit_date), notes, _now(), volume, market_cap),
            )
            self.conn.execute("DELETE FROM active_tracking WHERE id=?", (record["id"],))

    def history(self) -> list[dict]:
        return self._all("""SELECT h.*, COALESCE(i.kind, 'EQUITY') AS kind, i.aum_period FROM crossover_history h
                              LEFT JOIN instruments i ON i.symbol = h.symbol
                              ORDER BY h.exit_date DESC,
                                       h.market_cap_on_crossover IS NULL, h.market_cap_on_crossover DESC,
                                       h.symbol""")

    def add_observation(self, tracking_id: int, symbol: str, d: date, close: float | None, sma50: float | None,
                        rsi: float | None) -> None:
        above = None if close is None or sma50 is None else int(close > sma50)
        self.conn.execute(
            "INSERT OR REPLACE INTO daily_observations VALUES (?,?,?,?,?,?,?)",
            (tracking_id, symbol, _d(d), close, sma50, rsi, above),
        )

    # -- audit ----------------------------------------------------------------
    def audit(self, event: str, symbol: str | None = None, trade_date: date | None = None, **details: Any) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(ts, trade_date, symbol, event, details) VALUES (?,?,?,?,?)",
            (_now(), _d(trade_date), symbol, event, json.dumps(details, default=str) if details else None),
        )

    def audit_once(self, event: str, symbol: str | None, trade_date: date | None, **details: Any) -> bool:
        """Audit unless the same (event, symbol, date) was already recorded. Returns True if written."""
        if self._one("SELECT 1 AS x FROM audit_log WHERE event=? AND symbol IS ? AND trade_date IS ?",
                     (event, symbol, _d(trade_date))):
            return False
        self.audit(event, symbol, trade_date, **details)
        return True

    def audit_log(self, symbol: str | None = None, limit: int = 200) -> list[dict]:
        if symbol:
            return self._all("SELECT * FROM audit_log WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit))
        return self._all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    # -- alert outbox ---------------------------------------------------------
    def queue_alert(self, kind: str, subject: str, body: str, channels: Iterable[str],
                    trade_date: date | None = None, symbol: str | None = None) -> None:
        for channel in channels:
            self.conn.execute(
                """INSERT INTO alerts(created_at, trade_date, symbol, kind, channel, subject, body)
                   VALUES (?,?,?,?,?,?,?)""",
                (_now(), _d(trade_date), symbol, kind, channel, subject, body),
            )

    def pending_alerts(self, max_attempts: int) -> list[dict]:
        return self._all("SELECT * FROM alerts WHERE delivered=0 AND attempts<? ORDER BY id", (max_attempts,))

    def suppress_pending_alerts(self, reason: str) -> int:
        """Retire undelivered alerts without sending them (e.g. those produced by a replay)."""
        cur = self.conn.execute("UPDATE alerts SET delivered=1, last_error=? WHERE delivered=0", (reason,))
        return cur.rowcount

    def mark_alert(self, alert_id: int, ok: bool, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE alerts SET delivered=?, attempts=attempts+1, last_error=? WHERE id=?",
            (int(ok), error, alert_id),
        )
