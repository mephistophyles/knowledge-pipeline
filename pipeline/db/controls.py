"""Control-plane reads/writes over the `controls` table (plan §3).

Precedence: global → stage → source_type → artifact, **most specific wins**.
A more specific `running` row un-pauses a broader `paused` row, so you can pause
a whole stage but hand-release a single artifact through it.
"""
from __future__ import annotations

import sqlite3

# Most-specific first. `effective_state` returns the first present row's state.
_SPECIFICITY = ("artifact", "source_type", "stage", "global")


def set_control(
    conn: sqlite3.Connection,
    scope: str,
    key: str,
    *,
    state: str | None = None,
    batch_limit: int | None = None,
    note: str | None = None,
) -> None:
    existing = conn.execute(
        "SELECT state, batch_limit, note FROM controls WHERE scope=? AND key=?",
        (scope, key),
    ).fetchone()
    state = state if state is not None else (existing["state"] if existing else "running")
    batch_limit = batch_limit if batch_limit is not None else (existing["batch_limit"] if existing else None)
    note = note if note is not None else (existing["note"] if existing else None)
    conn.execute(
        "INSERT INTO controls(scope, key, state, batch_limit, note, updated_at) "
        "VALUES(?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(scope, key) DO UPDATE SET "
        "state=excluded.state, batch_limit=excluded.batch_limit, "
        "note=excluded.note, updated_at=excluded.updated_at",
        (scope, key, state, batch_limit, note),
    )


def clear_control(conn: sqlite3.Connection, scope: str, key: str) -> None:
    conn.execute("DELETE FROM controls WHERE scope=? AND key=?", (scope, key))


def effective_state(
    conn: sqlite3.Connection, stage: str, source_type: str | None, artifact_hash: str
) -> str:
    """running | paused for a specific (stage, source_type, artifact) tuple."""
    lookups = {
        "artifact": artifact_hash,
        "source_type": source_type or "",
        "stage": stage,
        "global": "*",
    }
    rows = {
        (r["scope"], r["key"]): r["state"]
        for r in conn.execute("SELECT scope, key, state FROM controls").fetchall()
    }
    for scope in _SPECIFICITY:
        hit = rows.get((scope, lookups[scope]))
        if hit is not None:
            return hit
    return "running"


def batch_limits(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(scope, key, limit) for every control that caps throughput."""
    return [
        (r["scope"], r["key"], r["batch_limit"])
        for r in conn.execute(
            "SELECT scope, key, batch_limit FROM controls WHERE batch_limit IS NOT NULL"
        ).fetchall()
    ]


def list_controls(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT scope, key, state, batch_limit, note, updated_at FROM controls "
        "ORDER BY scope, key"
    ).fetchall()
