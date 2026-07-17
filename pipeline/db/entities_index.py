"""Entity index for entity resolution (plan §6.4).

`entities` holds entity metadata; vectors live in the shared `entities_vec` vec0
table via `vec_index`. Resolution uses a cheap (name, entity_type) string match
first, then embedding-nearest + an LLM tiebreak for near-but-not-exact names.
"""
from __future__ import annotations

import sqlite3

from pipeline.db import vec_index

_VEC = "entities_vec"


def add_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    name: str,
    entity_type: str,
    embedding: list[float],
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO entities(entity_id, name, entity_type) VALUES(?,?,?)",
        (entity_id, name, entity_type),
    )
    vec_index.add(conn, _VEC, entity_id, embedding)


def bump_mention(conn: sqlite3.Connection, entity_id: str) -> None:
    conn.execute("UPDATE entities SET mentions = mentions + 1 WHERE entity_id=?", (entity_id,))


def get_entity(conn: sqlite3.Connection, entity_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM entities WHERE entity_id=?", (entity_id,)).fetchone()


def by_name(conn: sqlite3.Connection, name: str, entity_type: str) -> sqlite3.Row | None:
    """Exact (name, entity_type) match — the cheap first resolution step."""
    return conn.execute(
        "SELECT * FROM entities WHERE name=? AND entity_type=?", (name, entity_type)
    ).fetchone()


def nearest(conn: sqlite3.Connection, embedding: list[float], k: int) -> list[dict]:
    """Up to `k` nearest entities as {entity_id, name, entity_type, distance}."""
    out: list[dict] = []
    for h in vec_index.nearest(conn, _VEC, embedding, k):
        e = get_entity(conn, h["item_id"])
        if e is not None:
            out.append(
                {"entity_id": h["item_id"], "name": e["name"], "entity_type": e["entity_type"], "distance": h["distance"]}
            )
    return out
