"""`pipeline annotate <ref>` — attach personal notes to an existing artifact
(plan §4.7).

Notes are stored as their OWN raw artifact (`source_type: personal_note`,
manifest `annotates: <source_hash>`) and are invisible to the corpus derivation
chain — the source is processed as if no notes exist, so LLM output is never
contaminated by your framing. A separate personal deriver produces the
commentary sibling note (verbatim path in build steps 1–2).
"""
from __future__ import annotations

import sqlite3

from pipeline.config import Settings
from pipeline.db import jobs
from pipeline.orchestrator import stages
from pipeline.storage.manifest import write_artifact

INGESTOR_VERSION = "annotate/0.1.0"
PERSONAL_TYPE = "personal_note"


def annotate(
    settings: Settings,
    conn: sqlite3.Connection,
    target_ref: str,
    notes: str,
    *,
    source_url: str | None = None,
) -> str:
    """Store notes annotating `target_ref`; queue the personal deriver. Returns hash."""
    target_hash = jobs.resolve_ref(conn, target_ref)
    if target_hash is None:
        raise ValueError(f"could not resolve artifact ref {target_ref!r} (unknown or ambiguous)")
    h, _manifest = write_artifact(
        settings.blobstore,
        notes.encode("utf-8"),
        source_type=PERSONAL_TYPE,
        ingestor_version=INGESTOR_VERSION,
        ext="md",
        source_url=source_url,
        annotates=target_hash,
    )
    jobs.insert_job(conn, h, stages.first_stage(PERSONAL_TYPE), PERSONAL_TYPE)
    return h
