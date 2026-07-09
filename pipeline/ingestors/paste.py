"""`pipeline add paste` — the no-network ingestor (plan §4.5).

Contract shared by every ingestor: fetch → hash → write blob + manifest →
insert first job row (plan §4). Sidesteps scraping ToS: you paste the text and
supply provenance via --url/--type.
"""
from __future__ import annotations

import sqlite3

from pipeline.config import Settings
from pipeline.db import jobs
from pipeline.orchestrator import stages
from pipeline.storage.manifest import write_artifact

INGESTOR_VERSION = "paste/0.1.0"


def add_paste(
    settings: Settings,
    conn: sqlite3.Connection,
    text: str,
    *,
    source_type: str = "paste",
    source_url: str | None = None,
) -> str:
    """Store pasted text as a raw artifact and queue its first stage. Returns hash."""
    h, _manifest = write_artifact(
        settings.blobstore,
        text.encode("utf-8"),
        source_type=source_type,
        ingestor_version=INGESTOR_VERSION,
        ext="md",
        source_url=source_url,
    )
    jobs.insert_job(conn, h, stages.first_stage(source_type), source_type)
    return h
