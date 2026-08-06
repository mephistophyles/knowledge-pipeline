"""Rebuild the vector indexes under a different embedder.

This is the escape hatch that makes an embedder swap reversible. Vectors are DERIVED data:
`claims.text` and `entities.name` are stored, so an index can always be rebuilt from the
corpus. Nothing here touches the vault.

It is also mandatory rather than optional when the dimensionality changes — a vec0 table is
created at a fixed width, so a 768-dim index cannot accept 2048-dim vectors. Worse, mixing
embedders within one index would not error: L2 distances simply stop being comparable and
dedup quietly degrades. Rebuilding wholesale is what keeps that from happening silently.
"""
from __future__ import annotations

import math
import sqlite3

from pipeline.config import Settings
from pipeline.db import claims_index, entities_index, vec_index
from pipeline.llm import registry


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def _drop(conn: sqlite3.Connection, table: str) -> None:
    """Drop a vec0 virtual table and its shadow tables."""
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE name = ? OR name LIKE ?", (table, f"{table}\\_%")
    ).fetchall():
        # vec0 owns its shadow tables; dropping the virtual table takes them with it.
        if row["name"] == table:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def rebuild(
    settings: Settings, conn: sqlite3.Connection, *, batch: int = 32, dry_run: bool = False,
    progress=None,
) -> dict:
    """Re-embed every live claim and entity with the CONFIGURED embedder."""
    cfg = settings.embeddings_config
    provider = registry.get_provider(settings, cfg["provider"])
    model, input_type = cfg["model"], cfg.get("input_type")

    claims = conn.execute(
        "SELECT claim_id, text FROM claims WHERE merged_into IS NULL ORDER BY claim_id"
    ).fetchall()
    try:
        ents = conn.execute("SELECT entity_id, name FROM entities ORDER BY entity_id").fetchall()
    except sqlite3.OperationalError:
        ents = []

    # Probe one vector first: discovering the width after dropping a live index would
    # leave the corpus with no index at all if the provider is misconfigured.
    probe = provider.embed([claims[0]["text"] if claims else "probe"], model, input_type=input_type)
    dims = len(probe.vectors[0])
    out = {"model": model, "input_type": input_type, "dims": dims,
           "claims": len(claims), "entities": len(ents), "applied": not dry_run}
    if dry_run:
        return out

    _drop(conn, "claims_vec")
    _drop(conn, "entities_vec")

    for i in range(0, len(claims), batch):
        chunk = claims[i:i + batch]
        emb = provider.embed([r["text"] for r in chunk], model, input_type=input_type)
        for row, vec in zip(chunk, emb.vectors):
            vec_index.add(conn, "claims_vec", row["claim_id"], _normalize(vec))
        conn.commit()
        if progress:
            progress("claims", min(i + batch, len(claims)), len(claims))

    for i in range(0, len(ents), batch):
        chunk = ents[i:i + batch]
        emb = provider.embed([r["name"] for r in chunk], model, input_type=input_type)
        for row, vec in zip(chunk, emb.vectors):
            vec_index.add(conn, "entities_vec", row["entity_id"], _normalize(vec))
        conn.commit()
        if progress:
            progress("entities", min(i + batch, len(ents)), len(ents))

    out["indexed_claims"] = conn.execute("SELECT COUNT(*) FROM claims_vec").fetchone()[0]
    out["indexed_entities"] = (
        conn.execute("SELECT COUNT(*) FROM entities_vec").fetchone()[0] if ents else 0
    )
    return out
