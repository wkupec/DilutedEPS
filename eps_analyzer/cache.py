"""
cache.py — SQLite-based local caching layer for EPS data.
"""

import sqlite3
import json
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "eps_cache.db")


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            ticker      TEXT PRIMARY KEY,
            name        TEXT,
            sector      TEXT,
            source      TEXT DEFAULT 'unknown',
            last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS eps_data (
            ticker      TEXT,
            fiscal_year INTEGER,
            diluted_eps REAL,
            PRIMARY KEY (ticker, fiscal_year),
            FOREIGN KEY (ticker) REFERENCES companies(ticker)
        );
    """)
    # Migrate older DBs that lack the source column
    try:
        conn.execute("ALTER TABLE companies ADD COLUMN source TEXT DEFAULT 'unknown'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


def save_to_cache(data: dict) -> None:
    """
    Persist fetched EPS data to SQLite.

    data format:
        { ticker: { name, sector, eps_by_year: {year: float} } }
    """
    now = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        _init_db(conn)
        for ticker, info in data.items():
            conn.execute(
                """
                INSERT INTO companies (ticker, name, sector, source, last_updated)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    name=excluded.name,
                    sector=excluded.sector,
                    source=excluded.source,
                    last_updated=excluded.last_updated
                """,
                (ticker, info.get("name", ""), info.get("sector", ""),
                 info.get("source", "unknown"), now),
            )
            # Remove old EPS rows so stale years don't linger
            conn.execute("DELETE FROM eps_data WHERE ticker = ?", (ticker,))
            for year, eps in info.get("eps_by_year", {}).items():
                conn.execute(
                    """
                    INSERT OR REPLACE INTO eps_data (ticker, fiscal_year, diluted_eps)
                    VALUES (?, ?, ?)
                    """,
                    (ticker, int(year), float(eps)),
                )
        conn.commit()


def load_from_cache() -> dict:
    """
    Load all cached EPS data.

    Returns dict in the same shape as save_to_cache expects.
    Returns an empty dict if no data is cached.
    """
    if not os.path.exists(DB_PATH):
        return {}

    result: dict = {}
    with _get_connection() as conn:
        _init_db(conn)
        rows = conn.execute("SELECT ticker, name, sector, source FROM companies").fetchall()
        for row in rows:
            ticker = row["ticker"]
            result[ticker] = {
                "name": row["name"],
                "sector": row["sector"],
                "source": row["source"] or "unknown",
                "eps_by_year": {},
            }
        eps_rows = conn.execute(
            "SELECT ticker, fiscal_year, diluted_eps FROM eps_data"
        ).fetchall()
        for row in eps_rows:
            t = row["ticker"]
            if t in result:
                result[t]["eps_by_year"][row["fiscal_year"]] = row["diluted_eps"]

    return result


def is_cache_stale(hours: float = 24) -> bool:
    """Return True if no cache exists or the most recent update is older than `hours`."""
    if not os.path.exists(DB_PATH):
        return True

    with _get_connection() as conn:
        _init_db(conn)
        row = conn.execute(
            "SELECT MAX(last_updated) AS latest FROM companies"
        ).fetchone()

    if row is None or row["latest"] is None:
        return True

    latest = datetime.fromisoformat(row["latest"])
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600
    return age_hours >= hours


def get_last_updated() -> str | None:
    """Return the last_updated timestamp string, or None if cache is empty."""
    if not os.path.exists(DB_PATH):
        return None
    with _get_connection() as conn:
        _init_db(conn)
        row = conn.execute(
            "SELECT MAX(last_updated) AS latest FROM companies"
        ).fetchone()
    if row is None or row["latest"] is None:
        return None
    return row["latest"]


def bust_cache() -> None:
    """Delete the SQLite database file to force a full re-fetch."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
