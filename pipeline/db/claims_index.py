"""Claim index for dedup (plan §6.3).

`claims` holds claim metadata; the vector side lives in the shared `claims_vec`
`vec0` table via `vec_index`. Vectors are expected pre-normalised to unit length
by the caller, so distance ranks by cosine similarity (smaller = more similar).
"""
from __future__ import annotations

import sqlite3

from pipeline.db import vec_index

_VEC = "claims_vec"


def add_claim(
    conn: sqlite3.Connection,
    claim_id: str,
    artifact_hash: str,
    text: str,
    source_url: str | None,
    model: str | None,
    embedding: list[float],
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO claims(claim_id, artifact_hash, text, source_url, model) "
        "VALUES(?,?,?,?,?)",
        (claim_id, artifact_hash, text, source_url, model),
    )
    vec_index.add(conn, _VEC, claim_id, embedding)


def bump_attestation(conn: sqlite3.Connection, claim_id: str) -> None:
    conn.execute("UPDATE claims SET attestations = attestations + 1 WHERE claim_id=?", (claim_id,))


def get_claim(conn: sqlite3.Connection, claim_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()


def nearest(conn: sqlite3.Connection, embedding: list[float], k: int) -> list[dict]:
    """Up to `k` nearest claims as {claim_id, text, distance}, closest first."""
    out: list[dict] = []
    for h in vec_index.nearest(conn, _VEC, embedding, k):
        claim = get_claim(conn, h["item_id"])
        if claim is not None:
            out.append({"claim_id": h["item_id"], "text": claim["text"], "distance": h["distance"]})
    return out
