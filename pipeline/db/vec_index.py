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
    """Upsert one vector.

    vec0 parses `INSERT OR REPLACE` but does not honour it — the conflict clause
    is dropped and the write raises `UNIQUE constraint failed on <table> primary
    key` instead of replacing. Since claim_ids are deterministic (`claim-<source>
    -NN`), re-deriving any artifact that already produced claims re-uses its ids,
    so the upsert has to be spelled out as delete-then-insert or every reprocess
    dies in dedup.
    """
    ensure(conn, vec_table, len(embedding))
    conn.execute(f"DELETE FROM {vec_table} WHERE item_id=?", (item_id,))
    conn.execute(
        f"INSERT INTO {vec_table}(item_id, embedding) VALUES(?,?)",
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


def get_vector(conn: sqlite3.Connection, vec_table: str, item_id: str) -> list[float] | None:
    """Read a stored embedding back out.

    Retroactive grooming re-queries the index with vectors that were computed at
    ingest, so a corpus-wide pass costs confirm calls only — never re-embedding.
    """
    try:
        row = conn.execute(
            f"SELECT embedding FROM {vec_table} WHERE item_id=?", (item_id,)
        ).fetchone()
    except sqlite3.OperationalError:  # table not created yet
        return None
    if row is None or row["embedding"] is None:
        return None
    return list(struct.unpack(f"{len(row['embedding']) // 4}f", row["embedding"]))
