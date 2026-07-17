"""Run one stage and advance the job graph.

`run_stage` executes a handler, persists its intermediate, marks the job done,
and creates the next stage's row. It's the shared engine behind the worker loop,
`pipeline step`, and `pipeline walk` — the only difference between those is who
decides *when* to run the next stage.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pipeline.config import Settings
from pipeline.db import jobs
from pipeline.orchestrator import stages
from pipeline.orchestrator.context import StageContext
from pipeline.orchestrator.handlers import get_handler


@dataclass
class StageOutcome:
    output_path: str | None
    next_stage: str | None


def run_stage(
    settings: Settings, conn: sqlite3.Connection, artifact_hash: str, stage: str
) -> StageOutcome:
    """Execute `stage` for `artifact_hash`, mark it done, queue the next stage.

    Raises on handler failure — callers decide retry vs. abort. On success the
    intermediate is persisted before the DB advances (plan invariant 6).
    """
    job = jobs.get_job(conn, artifact_hash, stage)
    if job is None:
        raise ValueError(f"no job {artifact_hash[:12]}/{stage}")
    source_type = job["source_type"]

    ctx = StageContext.build(settings, conn, artifact_hash, stage, job["input_path"])
    handler = get_handler(stage)
    output_path = handler(ctx)  # writes intermediate before we touch job state

    jobs.mark_done(conn, artifact_hash, stage, output_path)

    nxt = stages.next_stage(source_type, stage)
    if nxt is not None:
        status = "held" if jobs.is_frozen(conn, artifact_hash) else "ready"
        jobs.insert_job(conn, artifact_hash, nxt, source_type, status=status, input_path=output_path)
    return StageOutcome(output_path=output_path, next_stage=nxt)
