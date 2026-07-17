"""Claim vector index for dedup (plan §6.3).

`claims` (regular table) holds claim metadata; `claims_vec` (sqlite-vec `vec0`)
holds the embedding for nearest-neighbour search. The vec table is created lazily
on first insert, sized to the embedding's dimension, so swapping embedding models
is a matter of re-indexing rather than a schema edit.

Vectors are expected pre-normalised to unit length by the caller, so `vec0`'s L2
distance ranks by cosine similarity (smaller distance = more similar).
"""
from __future__ import annotations

import sqlite3
import struct


def _serialize(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _vec_exists(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name='claims_vec'").fetchone() is not None
    )


def _ensure_vec(conn: sqlite3.Connection, dim: int) -> None:
    if not _vec_exists(conn):
        conn.execute(
            f"CREATE VIRTUAL TABLE claims_vec USING vec0(claim_id TEXT PRIMARY KEY, embedding FLOAT[{dim}])"
        )


def add_claim(
    conn: sqlite3.Connection,
    claim_id: str,
    artifact_hash: str,
    text: str,
    source_url: str | None,
    model: str | None,
    embedding: list[float],
) -> None:
    _ensure_vec(conn, len(embedding))
    conn.execute(
        "INSERT OR REPLACE INTO claims(claim_id, artifact_hash, text, source_url, model) "
        "VALUES(?,?,?,?,?)",
        (claim_id, artifact_hash, text, source_url, model),
    )
    conn.execute(
        "INSERT OR REPLACE INTO claims_vec(claim_id, embedding) VALUES(?,?)",
        (claim_id, _serialize(embedding)),
    )


def bump_attestation(conn: sqlite3.Connection, claim_id: str) -> None:
    conn.execute("UPDATE claims SET attestations = attestations + 1 WHERE claim_id=?", (claim_id,))


def get_claim(conn: sqlite3.Connection, claim_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()


def nearest(conn: sqlite3.Connection, embedding: list[float], k: int) -> list[dict]:
    """Return up to `k` nearest claims as {claim_id, text, distance}, closest first.

    The KNN runs as a bare vec0 MATCH (no JOIN — vec0 doesn't allow it); claim
    text is fetched in a follow-up lookup.
    """
    if not _vec_exists(conn):
        return []
    hits = conn.execute(
        "SELECT claim_id, distance FROM claims_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (_serialize(embedding), k),
    ).fetchall()
    out: list[dict] = []
    for h in hits:
        claim = get_claim(conn, h["claim_id"])
        if claim is not None:
            out.append({"claim_id": h["claim_id"], "text": claim["text"], "distance": h["distance"]})
    return out
