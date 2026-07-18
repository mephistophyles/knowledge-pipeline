"""Serial background execution for the dashboard cockpit.

Dashboard-triggered stage execution runs here, on a bounded worker pool (default
**1** — local Ollama tasks must not stampede). Three guarantees the naive
background task lacked:

- **Idempotent enqueue** — an artifact already queued or in-flight is not re-added,
  so an accidental double-click is a no-op.
- **Stage claiming** — each stage is atomically flipped `ready → running` before it
  runs, so a concurrent `pipeline worker` (or a second task) can't double-process it.
- **Ollama unload on drain** — when the queue empties, the pipeline's Ollama models
  are stopped so they don't sit resident in memory.
"""
from __future__ import annotations

import queue
import subprocess
import threading

from pipeline.config import Settings
from pipeline.db import bootstrap, jobs
from pipeline.orchestrator.executor import run_stage

_q: "queue.Queue[tuple[str, bool]]" = queue.Queue()
_inflight: set[str] = set()
_lock = threading.Lock()
_started = False


def enqueue(hashes: list[str], to_done: bool) -> list[str]:
    """Queue artifacts for execution; skip any already queued/in-flight. Returns
    the ones newly queued (idempotent — double submits return [])."""
    added: list[str] = []
    with _lock:
        for h in hashes:
            if h not in _inflight:
                _inflight.add(h)
                _q.put((h, to_done))
                added.append(h)
    if added:
        _ensure_workers()
    return added


def _ensure_workers() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
        n = max(1, Settings.load().max_parallel)
    for _ in range(n):
        threading.Thread(target=_worker, daemon=True).start()


def _worker() -> None:
    settings = Settings.load()
    conn = bootstrap(settings.db_path)  # thread-local connection
    while True:
        h, to_done = _q.get()
        try:
            run_frontier(settings, conn, h, to_done)
        except Exception:
            pass
        finally:
            _q.task_done()
            with _lock:
                _inflight.discard(h)
                idle = _q.empty() and not _inflight
        if idle:
            _unload_ollama(settings)


def _ready_stage(conn, artifact_hash: str) -> str | None:
    row = conn.execute(
        "SELECT stage FROM jobs WHERE artifact_hash=? AND status='ready' ORDER BY created_at LIMIT 1",
        (artifact_hash,),
    ).fetchone()
    return row["stage"] if row else None


def _claim(conn, artifact_hash: str, stage: str) -> bool:
    """Atomically flip a ready stage to running; False if someone else already has it."""
    cur = conn.execute(
        "UPDATE jobs SET status='running', updated_at=datetime('now') "
        "WHERE artifact_hash=? AND stage=? AND status='ready'",
        (artifact_hash, stage),
    )
    return cur.rowcount == 1


def run_frontier(settings: Settings, conn, artifact_hash: str, to_done: bool) -> list[str]:
    """Run the ready frontier stage once (step) or loop to completion (process).
    Claims each stage first; a failing stage is marked failed and stops the run."""
    ran: list[str] = []
    while (stage := _ready_stage(conn, artifact_hash)) is not None:
        if not _claim(conn, artifact_hash, stage):
            break  # a worker/other task grabbed it
        try:
            run_stage(settings, conn, artifact_hash, stage)
        except Exception as e:
            jobs.mark_failed(conn, artifact_hash, stage, str(e)[:500])
            break
        ran.append(stage)
        if not to_done:
            break
    return ran


def _unload_ollama(settings: Settings) -> None:
    """Stop the Ollama models the pipeline uses, so they don't stay resident."""
    raw = settings.raw
    providers = raw.get("providers", {}) or {}

    def is_ollama(pname: str | None) -> bool:
        return "11434" in (providers.get(pname or "", {}) or {}).get("base_url", "")

    models: set[str] = set()
    for cfg in (raw.get("models") or {}).values():
        if is_ollama(cfg.get("provider")) and cfg.get("model"):
            models.add(cfg["model"])
    emb = raw.get("embeddings", {}) or {}
    if is_ollama(emb.get("provider")) and emb.get("model"):
        models.add(emb["model"])

    for model in models:
        try:
            subprocess.run(["ollama", "stop", model], timeout=10, capture_output=True)
        except Exception:
            pass  # ollama CLI absent (e.g. on the box) or already stopped
