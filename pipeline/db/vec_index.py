"""Generic sqlite-vec (vec0) helpers shared by the claim and entity indexes.

Each domain owns its own metadata table; this module owns only the vector side —
a `vec0` virtual table created lazily at the embedding's dimension. Callers pass
unit-normalized vectors so vec0's L2 distance ranks by cosine similarity.
"""
from __future__ import annotations

import sqlite3
import struct


def _serialize(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def exists(conn: sqlite3.Connection, vec_table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (vec_table,)).fetchone() is not None


def ensure(conn: sqlite3.Connection, vec_table: str, dim: int) -> None:
    if not exists(conn, vec_table):
        # vec_table is an internal constant, never user input.
        conn.execute(
            f"CREATE VIRTUAL TABLE {vec_table} USING vec0(item_id TEXT PRIMARY KEY, embedding FLOAT[{dim}])"
        )


def add(conn: sqlite3.Connection, vec_table: str, item_id: str, embedding: list[float]) -> None:
    ensure(conn, vec_table, len(embedding))
    conn.execute(
        f"INSERT OR REPLACE INTO {vec_table}(item_id, embedding) VALUES(?,?)",
        (item_id, _serialize(embedding)),
    )


def nearest(conn: sqlite3.Connection, vec_table: str, embedding: list[float], k: int) -> list[sqlite3.Row]:
    """Rows of (item_id, distance), closest first; empty if the table doesn't exist yet."""
    if not exists(conn, vec_table):
        return []
    return conn.execute(
        f"SELECT item_id, distance FROM {vec_table} WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (_serialize(embedding), k),
    ).fetchall()
