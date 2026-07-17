"""SQLite connection + one-shot bootstrap.

WAL mode + a generous busy timeout let the several worker processes and the
read-only dashboard share one file. Writers still serialize (SQLite allows one
at a time); `BEGIN IMMEDIATE` in the claim path makes that serialization
explicit so two workers never grab the same job.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = Path(__file__).parent / "schema.sql"


def connect(db_path: str | Path) -> sqlite3.Connection:
    # isolation_level=None → autocommit; we open transactions explicitly when claiming.
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def bootstrap(db_path: str | Path) -> sqlite3.Connection:
    """Create the DB file + tables if absent, apply column migrations, connect."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    conn.executescript(_SCHEMA.read_text())
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for DBs created before a schema change.

    `CREATE TABLE IF NOT EXISTS` never alters an existing table, so new columns
    on `costs` (provider, latency_ms) must be added explicitly for older DBs.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(costs)")}
    if "provider" not in cols:
        conn.execute("ALTER TABLE costs ADD COLUMN provider TEXT")
    if "latency_ms" not in cols:
        conn.execute("ALTER TABLE costs ADD COLUMN latency_ms INTEGER")
