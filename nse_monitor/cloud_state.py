"""Split / reassemble the database for cloud runs (GitHub Actions).

The full database is ~40 MB, almost all of it the price cache. For the cloud:

* ``state.db``  -- everything except prices: tracking list, history, audit log,
  instruments (shares/AUM), calendar, alerts. Small, durable, published to the
  repository's ``data`` branch, and read by the Streamlit dashboard.
* ``prices.db`` -- the price cache. Kept in the GitHub Actions cache; if it is ever
  evicted, the next run simply re-downloads history (a few minutes).

    python -m nse_monitor.cloud_state pack   --db data/nse_monitor.db --state cloud/state.db --prices cloud/prices.db --meta cloud/meta.json
    python -m nse_monitor.cloud_state unpack --db data/nse_monitor.db --state cloud/state.db --prices cloud/prices.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .storage import Repository

PRICE_TABLES = ("prices", "price_meta")
IST = timezone(timedelta(hours=5, minutes=30))


def _copy_db(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    with closing(sqlite3.connect(src)) as s, closing(sqlite3.connect(dest)) as d:
        s.backup(d)


def pack(db: Path, state: Path, prices: Path, meta: Path | None = None) -> dict:
    _copy_db(db, state)
    with closing(sqlite3.connect(state)) as c:
        for t in PRICE_TABLES:
            c.execute(f"DROP TABLE IF EXISTS {t}")
        c.commit()
        c.execute("PRAGMA journal_mode=DELETE")  # single self-contained file
    with closing(sqlite3.connect(state)) as c:
        c.execute("VACUUM")

    _copy_db(db, prices)
    with closing(sqlite3.connect(prices)) as c:
        keep = set(PRICE_TABLES)
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            if name not in keep:
                c.execute(f"DROP TABLE IF EXISTS {name}")
        c.commit()
        c.execute("PRAGMA journal_mode=DELETE")
    with closing(sqlite3.connect(prices)) as c:
        c.execute("VACUUM")

    with closing(sqlite3.connect(state)) as c:
        q = lambda sql: c.execute(sql).fetchone()[0]
        info = {
            "published_at": datetime.now(IST).isoformat(timespec="seconds"),
            "last_processed_day": q("SELECT MAX(date) FROM processed_days"),
            "active": q("SELECT COUNT(*) FROM active_tracking"),
            "history": q("SELECT COUNT(*) FROM crossover_history"),
            "state_bytes": state.stat().st_size,
        }
    if meta:
        meta.write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def unpack(db: Path, state: Path, prices: Path | None) -> str:
    """Rebuild the working database. Missing state => fresh start; missing prices => re-download."""
    db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    if state.exists():
        _copy_db(state, db)
    Repository(db).close()  # creates any missing tables (incl. prices) and runs migrations
    if prices and prices.exists():
        with closing(sqlite3.connect(db)) as c:
            c.execute("ATTACH DATABASE ? AS cache", (str(prices),))
            for t in PRICE_TABLES:
                c.execute(f"INSERT OR REPLACE INTO main.{t} SELECT * FROM cache.{t}")
            c.commit()  # (closing() does not commit on its own)
            c.execute("DETACH DATABASE cache")
    with closing(sqlite3.connect(db)) as c:
        n_prices = c.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        last = c.execute("SELECT MAX(date) FROM processed_days").fetchone()[0]
    return (f"state: {'restored' if state.exists() else 'none (fresh start)'}; "
            f"prices: {n_prices:,} cached bars; last processed day: {last}")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m nse_monitor.cloud_state")
    p.add_argument("action", choices=["pack", "unpack"])
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--prices", type=Path, required=True)
    p.add_argument("--meta", type=Path)
    a = p.parse_args()
    if a.action == "pack":
        print(json.dumps(pack(a.db, a.state, a.prices, a.meta), indent=2))
    else:
        print(unpack(a.db, a.state, a.prices))


if __name__ == "__main__":
    main()
