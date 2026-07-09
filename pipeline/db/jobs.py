"""Job-row lifecycle: insert, atomic claim, advance, fail, and the control-aware
claim used by workers.

State machine per (artifact_hash, stage):
    ready → running → done          (and the next stage's row is created ready)
                    → failed        (attempts exhausted)
    ready ⇄ held                    (hold/release freeze one artifact mid-pipeline)
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

from pipeline.db import controls as ctl


def insert_job(
    conn: sqlite3.Connection,
    artifact_hash: str,
    stage: str,
    source_type: str | None,
    *,
    status: str = "ready",
    input_path: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO jobs(artifact_hash, stage, status, source_type, input_path) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(artifact_hash, stage) DO UPDATE SET "
        "status=excluded.status, input_path=excluded.input_path, "
        "attempts=0, error=NULL, updated_at=datetime('now')",
        (artifact_hash, stage, status, source_type, input_path),
    )


def get_job(conn: sqlite3.Connection, artifact_hash: str, stage: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE artifact_hash=? AND stage=?", (artifact_hash, stage)
    ).fetchone()


def artifact_hashes(conn: sqlite3.Connection) -> list[str]:
    return [r["artifact_hash"] for r in conn.execute(
        "SELECT DISTINCT artifact_hash FROM jobs"
    ).fetchall()]


def resolve_ref(conn: sqlite3.Connection, ref: str) -> str | None:
    """Resolve a full or prefix hash to a unique artifact_hash, else None."""
    matches = conn.execute(
        "SELECT DISTINCT artifact_hash FROM jobs WHERE artifact_hash LIKE ?",
        (ref + "%",),
    ).fetchall()
    if len(matches) == 1:
        return matches[0]["artifact_hash"]
    return None


def is_frozen(conn: sqlite3.Connection, artifact_hash: str) -> bool:
    """True if an artifact-scope hold is active (set by hold_artifact)."""
    row = conn.execute(
        "SELECT state FROM controls WHERE scope='artifact' AND key=?", (artifact_hash,)
    ).fetchone()
    return row is not None and row["state"] == "paused"


def hold_artifact(conn: sqlite3.Connection, artifact_hash: str) -> int:
    """Freeze an artifact: artifact-scope pause + flip its live stage to `held`.

    The control gates claiming AND advancing (so a hold placed while a stage is
    mid-flight still freezes the next stage); the `held` status makes it visible.
    """
    ctl.set_control(conn, "artifact", artifact_hash, state="paused")
    cur = conn.execute(
        "UPDATE jobs SET status='held', updated_at=datetime('now') "
        "WHERE artifact_hash=? AND status IN ('ready','pending')",
        (artifact_hash,),
    )
    return cur.rowcount


def release_artifact(conn: sqlite3.Connection, artifact_hash: str) -> int:
    ctl.clear_control(conn, "artifact", artifact_hash)
    cur = conn.execute(
        "UPDATE jobs SET status='ready', updated_at=datetime('now') "
        "WHERE artifact_hash=? AND status='held'",
        (artifact_hash,),
    )
    return cur.rowcount


def mark_done(conn: sqlite3.Connection, artifact_hash: str, stage: str, output_path: str | None) -> None:
    conn.execute(
        "UPDATE jobs SET status='done', output_path=?, error=NULL, "
        "updated_at=datetime('now') WHERE artifact_hash=? AND stage=?",
        (output_path, artifact_hash, stage),
    )


def mark_failed(conn: sqlite3.Connection, artifact_hash: str, stage: str, error: str) -> None:
    conn.execute(
        "UPDATE jobs SET status='failed', error=?, updated_at=datetime('now') "
        "WHERE artifact_hash=? AND stage=?",
        (error, artifact_hash, stage),
    )


def record_attempt_failure(
    conn: sqlite3.Connection, artifact_hash: str, stage: str, error: str, max_attempts: int
) -> str:
    """Increment attempts; return the resulting status ('ready' to retry or 'failed')."""
    row = conn.execute(
        "SELECT attempts FROM jobs WHERE artifact_hash=? AND stage=?", (artifact_hash, stage)
    ).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    status = "failed" if attempts >= max_attempts else "ready"
    conn.execute(
        "UPDATE jobs SET status=?, attempts=?, error=?, claimed_by=NULL, "
        "updated_at=datetime('now') WHERE artifact_hash=? AND stage=?",
        (status, attempts, error, artifact_hash, stage),
    )
    return status


def claim_next(
    conn: sqlite3.Connection,
    worker_id: str,
    stages: Iterable[str],
    processed: dict[tuple[str, str], int] | None = None,
) -> sqlite3.Row | None:
    """Atomically claim the oldest runnable `ready` job for one of `stages`.

    Skips jobs whose (stage / source_type / artifact) is paused via the control
    plane, and jobs whose scope has hit its per-run batch limit. Uses
    BEGIN IMMEDIATE so concurrent workers can't double-claim.
    """
    stages = list(stages)
    if not stages:
        return None
    placeholders = ",".join("?" for _ in stages)
    processed = processed if processed is not None else {}
    limits = {(s, k): lim for s, k, lim in ctl.batch_limits(conn)}

    conn.execute("BEGIN IMMEDIATE")
    try:
        candidates = conn.execute(
            f"SELECT * FROM jobs WHERE status='ready' AND stage IN ({placeholders}) "
            f"ORDER BY created_at LIMIT 25",
            stages,
        ).fetchall()
        chosen = None
        for job in candidates:
            if ctl.effective_state(conn, job["stage"], job["source_type"], job["artifact_hash"]) == "paused":
                continue
            if _over_limit(job, limits, processed):
                continue
            chosen = job
            break
        if chosen is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE jobs SET status='running', claimed_by=?, updated_at=datetime('now') "
            "WHERE artifact_hash=? AND stage=?",
            (worker_id, chosen["artifact_hash"], chosen["stage"]),
        )
        conn.execute("COMMIT")
        return chosen
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _over_limit(job: sqlite3.Row, limits: dict, processed: dict) -> bool:
    for scope, key in (("stage", job["stage"]), ("source_type", job["source_type"] or "")):
        lim = limits.get((scope, key))
        if lim is not None and processed.get((scope, key), 0) >= lim:
            return True
    return False


def count_processed(processed: dict, job: sqlite3.Row) -> None:
    """Tally a completed job against its stage/source scopes for batch-limit tracking."""
    processed[("stage", job["stage"])] = processed.get(("stage", job["stage"]), 0) + 1
    st = job["source_type"] or ""
    processed[("source_type", st)] = processed.get(("source_type", st), 0) + 1
