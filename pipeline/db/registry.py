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
    word_count: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO artifacts(artifact_hash, source_type, author, source, media, title, word_count) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(artifact_hash) DO UPDATE SET "
        "source_type=excluded.source_type, author=excluded.author, source=excluded.source, "
        "media=excluded.media, title=excluded.title, word_count=excluded.word_count",
        (artifact_hash, source_type, author, source, media, title, word_count),
    )


def get(conn: sqlite3.Connection, artifact_hash: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM artifacts WHERE artifact_hash=?", (artifact_hash,)).fetchone()


# Per-artifact current status derived from its job rows (worst-first priority).
# Matches the dashboard's frontier logic since a linear chain has one live stage.
_STATUS_SQL = (
    "(SELECT artifact_hash, CASE "
    "WHEN SUM(status='failed')>0 THEN 'failed' "
    "WHEN SUM(status='running')>0 THEN 'running' "
    "WHEN SUM(status='held')>0 THEN 'held' "
    "WHEN SUM(status='ready')>0 THEN 'ready' "
    "ELSE 'done' END AS current_status FROM jobs GROUP BY artifact_hash)"
)


def _build(filters: dict[str, Any] | None) -> tuple[str, str, list]:
    """Return (join, where, params). Facet/search filter the registry directly;
    `status` / `hide_completed` join the derived per-artifact status."""
    filters = filters or {}
    clauses, params = [], []
    for facet in FACETS:
        if filters.get(facet):
            clauses.append(f"a.{facet}=?")
            params.append(filters[facet])
    if filters.get("search"):
        clauses.append("(a.title LIKE ? OR a.author LIKE ? OR a.source LIKE ?)")
        params += [f"%{filters['search']}%"] * 3
    join = ""
    if filters.get("status") or filters.get("hide_completed"):
        join = f" JOIN {_STATUS_SQL} j ON a.artifact_hash=j.artifact_hash"
        if filters.get("status"):
            clauses.append("j.current_status=?")
            params.append(filters["status"])
        if filters.get("hide_completed"):
            clauses.append("j.current_status!='done'")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return join, where, params


def query(conn: sqlite3.Connection, *, filters: dict | None = None, limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
    join, where, params = _build(filters)
    return conn.execute(
        f"SELECT a.* FROM artifacts a{join}{where} ORDER BY a.created_at DESC, a.artifact_hash LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()


def count(conn: sqlite3.Connection, *, filters: dict | None = None) -> int:
    join, where, params = _build(filters)
    return conn.execute(f"SELECT COUNT(*) n FROM artifacts a{join}{where}", params).fetchone()["n"]


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
