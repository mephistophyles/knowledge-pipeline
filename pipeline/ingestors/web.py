"""Web page ingestor — v1: you paste the body text you care about.

Deliberately manual to start. Automated boilerplate stripping (nav, related-post
teasers, footers, comment threads) is the hard part of web ingestion and it is the
part most likely to silently corrupt a claim's provenance: a quote lifted from a
"you might also like" teaser is attributed to an article that never said it. Doing
the selection by hand while the rest of the chain is tuned keeps that risk at zero
and still produces real corpus material.

The one thing this adds over `add paste` is AUTHOR. Attestation asks "did a second
author say this too?", and `_author_key` reads `manifest.extra["from"]`. Without it
every URL is its own author, so ten posts from one blog would read as ten
independent corroborations of each other — inflating exactly the signal the
author-aware attestation work exists to protect.
"""
from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlparse

from pipeline.config import Settings
from pipeline.db import jobs, registry
from pipeline.orchestrator import stages
from pipeline.storage.manifest import write_artifact

INGESTOR_VERSION = "web/0.1.0"
SOURCE_TYPE = "web"


def site_of(url: str | None) -> str | None:
    """Bare hostname, used as the feed/publication facet."""
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or None


def normalize(text: str) -> str:
    """Light tidy only — collapse runs of blank lines and trailing spaces.

    No boilerplate stripping: see the module docstring. What you paste is what gets
    extracted, so the artifact hash means exactly what it looks like.
    """
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def add_web(
    settings: Settings,
    conn: sqlite3.Connection,
    text: str,
    *,
    url: str | None = None,
    author: str | None = None,
    title: str | None = None,
) -> str | None:
    """Ingest pasted article body → artifact + queued chain. Returns hash, or None if empty.

    `author` should be stable across a site's posts (an email, handle, or name) — it is
    what makes two posts by one writer count as one voice rather than two.
    """
    body = normalize(text)
    if not body:
        return None

    site = site_of(url)
    heading = title or next((ln.strip().lstrip("# ") for ln in body.splitlines() if ln.strip()), None)
    extra = {
        "from": author or site,  # falls back to the site so a blog is one voice, not one per post
        "subject": heading,
        "canonical_url": url,
        "site": site,
        "word_count": len(body.split()),
    }
    h, _ = write_artifact(
        settings.blobstore,
        body.encode("utf-8"),
        source_type=SOURCE_TYPE,
        ingestor_version=INGESTOR_VERSION,
        ext="md",
        source_url=url,
        extra=extra,
    )
    jobs.insert_job(conn, h, stages.first_stage(SOURCE_TYPE), SOURCE_TYPE)
    registry.register(
        conn, h, source_type=SOURCE_TYPE,
        author=author or site,
        source=site,
        media="text",
        title=heading[:120] if heading else url,
        word_count=extra["word_count"],
    )
    return h
