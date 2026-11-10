"""Turn archived pages into artifacts — the join between the web ledger and the corpus.

Reads the content-addressed archive, so it does not care whether the bytes were fetched or
saved from a browser. Extraction failures are HELD rather than processed: the page stays in
the ledger with its reason, and can be re-derived after the extractor improves or the body
is pasted by hand.
"""
from __future__ import annotations

import sqlite3

from pipeline import authors
from pipeline.config import Settings
from pipeline.db import jobs, registry
from pipeline.orchestrator import stages
from pipeline.storage.manifest import write_artifact
from pipeline.web import extract as extract_mod
from pipeline.web.canonical import site_of
from pipeline.web.ledger import identity_for, record_escalation

INGESTOR_VERSION = "web/0.2.0"
SOURCE_TYPE = "web"


def author_for(conn: sqlite3.Connection, url: str, byline: str | None) -> tuple[str | None, str | None]:
    """`(identity_id, raw_author)` — byline first, hostname second.

    Byline WINS when it resolves, and that ordering is the whole reason identity is
    anchored to a person: a guest post on someone else's site is written by the guest, and
    resolving by hostname would credit the host. When the byline names nobody we know, the
    hostname is the fallback; when neither resolves the page is unmapped and its
    attestations are provisional.
    """
    if byline:
        key = authors.author_key(byline)
        found = authors.identity_of(conn, key) or authors.identity_of(conn, byline.strip().lower())
        if found:
            return found, byline
    return identity_for(conn, url), byline


def derive_one(settings: Settings, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """Extract one archived page into an artifact, or hold it with a reason."""
    url = row["url"]
    raw = settings.blobstore.read(row["fetch_key"])
    got = extract_mod.extract(raw, url=url)

    if not got.ok:
        reason = ",".join(got.holds)
        record_escalation(conn, url, got.holds[0], f"{got.word_count} words")
        conn.execute(
            "UPDATE web_backlog SET state='held', escalation=?, updated_at=datetime('now') "
            "WHERE fetch_hash=?", (reason, row["fetch_hash"]),
        )
        conn.commit()
        return {"url": url, "state": "held", "reason": reason, "word_count": got.word_count}

    identity, byline = author_for(conn, url, got.author)
    title = got.title or row["title"]
    site = site_of(url)
    extra = {
        # `from` is what attestation reads. The identity when we have one, else the site —
        # never the bare byline, which would fragment one writer across spellings.
        "from": identity or site,
        "identity_id": identity,
        "byline": byline,
        "subject": title,
        "canonical_url": url,
        "site": site,
        "published": got.date,
        "word_count": got.word_count,
        "fetch_hash": row["fetch_hash"],
    }
    h, _ = write_artifact(
        settings.blobstore, got.text.encode("utf-8"),
        source_type=SOURCE_TYPE, ingestor_version=INGESTOR_VERSION, ext="md",
        source_url=url, extra=extra,
    )
    jobs.insert_job(conn, h, stages.first_stage(SOURCE_TYPE), SOURCE_TYPE)
    registry.register(
        conn, h, source_type=SOURCE_TYPE, author=identity or site, source=site,
        media="text", title=(title or url)[:120], word_count=got.word_count,
    )
    conn.execute(
        "UPDATE web_backlog SET artifact_hash=?, identity_id=?, state='ingested', "
        "escalation=NULL, updated_at=datetime('now') WHERE fetch_hash=?",
        (h, identity, row["fetch_hash"]),
    )
    conn.commit()
    return {
        "url": url, "state": "ingested", "artifact_hash": h, "identity_id": identity,
        "byline": byline, "word_count": got.word_count, "title": title,
    }


def pending(conn: sqlite3.Connection, *, limit: int = 50, site: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM web_backlog WHERE state='archived' AND fetch_key IS NOT NULL"
    params: list = []
    if site:
        sql += " AND site=?"
        params.append(site)
    return conn.execute(sql + " ORDER BY fetched_at LIMIT ?", params + [limit]).fetchall()


def derive(
    settings: Settings, conn: sqlite3.Connection, *, limit: int = 50, site: str | None = None,
    progress=None,
) -> dict:
    counts: dict[str, int] = {}
    for row in pending(conn, limit=limit, site=site):
        r = derive_one(settings, conn, row)
        counts[r["state"]] = counts.get(r["state"], 0) + 1
        if r["state"] == "held":
            counts[f"held:{r['reason']}"] = counts.get(f"held:{r['reason']}", 0) + 1
        if progress:
            progress(r)
    return counts
