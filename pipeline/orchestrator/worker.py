"""Worker loop — one process per resource class (plan §3).

Loop: check controls → claim one `ready` job atomically → run the stage →
persist intermediate → mark done + queue next → repeat. Failures increment
`attempts` with backoff; exhausted attempts land in `failed` (the dead-letter
view is just WHERE status='failed').
"""
from __future__ import annotations

import os
import signal
import time

from pipeline.config import Settings
from pipeline.db import connect
from pipeline.db import jobs
from pipeline.orchestrator import stages
from pipeline.orchestrator.executor import run_stage

_stop = False


def _handle_signal(signum, frame):
    global _stop
    _stop = True


def run_worker(settings: Settings, resource_class: str, once: bool = False) -> None:
    if resource_class not in stages.RESOURCE_CLASSES:
        raise SystemExit(
            f"unknown resource class {resource_class!r}; "
            f"expected one of {stages.RESOURCE_CLASSES}"
        )
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    handled = stages.stages_for_resource(resource_class)
    worker_id = f"{resource_class}-{os.getpid()}"
    conn = connect(settings.db_path)
    processed: dict = {}  # per-process batch-limit tally (scope,key) → count
    print(f"[worker {worker_id}] handling stages: {handled or '(none)'}", flush=True)

    while not _stop:
        job = jobs.claim_next(conn, worker_id, handled, processed)
        if job is None:
            if once:
                break
            time.sleep(settings.poll_interval)
            continue
        _execute(settings, conn, job, processed)
    print(f"[worker {worker_id}] stopped", flush=True)


def _execute(settings: Settings, conn, job, processed: dict) -> None:
    ah, stage = job["artifact_hash"], job["stage"]
    try:
        outcome = run_stage(settings, conn, ah, stage)
        jobs.count_processed(processed, job)
        nxt = outcome.next_stage or "∅"
        print(f"[done] {ah[:12]}/{stage} → {nxt}", flush=True)
    except Exception as exc:  # noqa: BLE001 — worker must survive any stage failure
        status = jobs.record_attempt_failure(conn, ah, stage, repr(exc), settings.max_attempts)
        print(f"[fail] {ah[:12]}/{stage} ({status}): {exc!r}", flush=True)
        if status == "ready":
            time.sleep(min(2 ** job["attempts"], 30))  # exponential backoff
