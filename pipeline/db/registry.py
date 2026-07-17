"""Artifact registry (dashboard backlog browsing).

One row per ingested artifact — what a thing *is* (type/author/source/media/title)
— so the backlog can be filtered by facet at scale without reading every manifest.
`jobs` still owns progress; this owns identity. Ingestors call `register`;
`backfill` populates rows for artifacts ingested before the registry existed.
"""
from __future__ import annotations

import sqlite3
from typing import Any

FACETS = ("source_type", "author", "source", "media")


def register(
    conn: sqlite3.Connection,
    artifact_hash: str,
    *,
    source_type: str,
    author: str | None = None,
    source: str | None = None,
    media: str = "text",
    title: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO artifacts(artifact_hash, source_type, author, source, media, title) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(artifact_hash) DO UPDATE SET "
        "source_type=excluded.source_type, author=excluded.author, source=excluded.source, "
        "media=excluded.media, title=excluded.title",
        (artifact_hash, source_type, author, source, media, title),
    )


def get(conn: sqlite3.Connection, artifact_hash: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM artifacts WHERE artifact_hash=?", (artifact_hash,)).fetchone()


def _where(filters: dict[str, Any] | None) -> tuple[str, list]:
    filters = filters or {}
    clauses, params = [], []
    for facet in FACETS:
        val = filters.get(facet)
        if val:
            clauses.append(f"{facet}=?")
            params.append(val)
    search = filters.get("search")
    if search:
        clauses.append("(title LIKE ? OR author LIKE ? OR source LIKE ?)")
        params += [f"%{search}%"] * 3
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def query(conn: sqlite3.Connection, *, filters: dict | None = None, limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
    where, params = _where(filters)
    return conn.execute(
        f"SELECT * FROM artifacts{where} ORDER BY created_at DESC, artifact_hash LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()


def count(conn: sqlite3.Connection, *, filters: dict | None = None) -> int:
    where, params = _where(filters)
    return conn.execute(f"SELECT COUNT(*) n FROM artifacts{where}", params).fetchone()["n"]


def facet_values(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Distinct values per facet, for filter dropdowns."""
    out: dict[str, list[str]] = {}
    for facet in FACETS:
        out[facet] = [
            r[facet] for r in conn.execute(
                f"SELECT DISTINCT {facet} FROM artifacts WHERE {facet} IS NOT NULL ORDER BY {facet}"
            )
        ]
    return out


def unregistered_count(conn: sqlite3.Connection) -> int:
    """Artifacts present in jobs but missing from the registry (nudge to backfill)."""
    return conn.execute(
        "SELECT COUNT(*) n FROM (SELECT DISTINCT artifact_hash FROM jobs "
        "WHERE artifact_hash NOT IN (SELECT artifact_hash FROM artifacts))"
    ).fetchone()["n"]


def backfill(settings, conn: sqlite3.Connection) -> int:
    """Register any artifact in jobs but not yet in the registry, from its manifest."""
    from pipeline.storage.manifest import load_manifest

    hashes = [
        r["artifact_hash"] for r in conn.execute(
            "SELECT DISTINCT artifact_hash FROM jobs "
            "WHERE artifact_hash NOT IN (SELECT artifact_hash FROM artifacts)"
        )
    ]
    n = 0
    for h in hashes:
        try:
            m = load_manifest(settings.blobstore, h)
        except Exception:
            continue
        extra = getattr(m, "extra", None) or {}
        register(
            conn, h,
            source_type=m.source_type,
            author=extra.get("from"),
            source=extra.get("list_id") or m.source_url,
            media="text",
            title=extra.get("subject") or m.source_url,
        )
        n += 1
    return n
