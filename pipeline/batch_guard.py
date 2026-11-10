"""Estimate before you start, and stop cleanly when asked.

Rules of engagement (Phil, 2026-08-06), after I twice kicked off multi-hour work without
saying so:

  1. Anything expected to exceed ~15 minutes must be FLAGGED before it starts, not
     narrated afterwards.
  2. Anything hitting a paid provider must give a rough duration and cost up front, so it
     can be planned around other obligations.
  3. Long batches must be interruptible. This laptop moves regularly, and until the work
     runs on always-on infrastructure a job that cannot survive a closed lid is a job that
     silently loses its progress.

The estimate is deliberately crude — an order of magnitude decides "now or later", and a
precise number would imply a confidence these rates do not have.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

FLAG_THRESHOLD_MIN = 15.0     # above this, say so before starting

# Observed throughput, from real runs rather than vendor claims. Update when measured.
RATES = {
    "dedup_confirm_serial": 15.0,      # calls/min, one at a time
    "dedup_confirm_parallel": 136.0,   # calls/min at 8 workers (measured 2026-08-06)
    "extract_claims": 2.0,             # ~31s/edition on mixed email + long web articles
    "entities": 3.5,                   # ~17s/call, nemotron super
}


@dataclass
class Estimate:
    calls: int
    rate_per_min: float
    usd: float = 0.0
    label: str = ""

    @property
    def minutes(self) -> float:
        return self.calls / self.rate_per_min if self.rate_per_min else 0.0

    @property
    def needs_flagging(self) -> bool:
        return self.minutes > FLAG_THRESHOLD_MIN

    def render(self) -> str:
        mins = self.minutes
        dur = f"{mins:.0f} min" if mins < 90 else f"{mins / 60:.1f} hours"
        cost = f", ~${self.usd:.2f}" if self.usd else ", no metered cost"
        head = f"{self.label}: ~{self.calls:,} call(s) → ~{dur}{cost}"
        if self.needs_flagging:
            head += (f"\n  ⚠ over the {FLAG_THRESHOLD_MIN:.0f}-minute threshold — "
                     f"safe to interrupt, and it resumes from cache")
        return head


def estimate(calls: int, *, rate_key: str, usd_per_call: float = 0.0, label: str = "") -> Estimate:
    return Estimate(calls=calls, rate_per_min=RATES.get(rate_key, 60.0),
                    usd=calls * usd_per_call, label=label or rate_key)


# ── clean interruption ────────────────────────────────────────────────────────
def _stop_file(root: Path) -> Path:
    return Path(root) / ".stop-batch"


def request_stop(root: Path) -> Path:
    """Ask any running batch to finish its current item and exit."""
    p = _stop_file(root)
    p.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
    return p


def clear_stop(root: Path) -> None:
    _stop_file(root).unlink(missing_ok=True)


def should_stop(root: Path, conn: sqlite3.Connection | None = None) -> bool:
    """True when the batch should wind up.

    Two ways to ask, because they mean different things: the stop FILE is "stop this run"
    and clears when the run ends, while a global control-plane pause is "the pipeline is
    down" and persists. A long loop honouring only one of them will surprise someone.
    """
    if _stop_file(root).exists():
        return True
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT state FROM controls WHERE scope='global' AND key='*'"
            ).fetchone()
            return bool(row and row["state"] == "paused")
        except sqlite3.OperationalError:
            return False
    return False


# ── visibility ────────────────────────────────────────────────────────────────
def start(conn: sqlite3.Connection, batch_id: str, label: str, total: int,
          *, rate_key: str = "", note: str = "", workflow: str = "",
          step: int = 1, steps_total: int = 1) -> str:
    """Register a batch so the dashboard can show it while it runs.

    `total` and the value passed to `tick` MUST be in the same units. They were not on the
    first live run — total counted candidate pairs while done counted LLM calls — and the
    resulting ETA was meaningless. Both are now "units of work this task will actually do".

    `workflow` groups tasks that the operator thinks of as one job. The bake-off is one job
    to a person and four tasks to the machine, and quoting one task's remaining time as if
    it were the whole run is how two contradictory estimates get reported.
    """
    conn.execute(
        "INSERT INTO batches(batch_id, label, total, done, rate_key, state, note, "
        "workflow, step, steps_total) "
        "VALUES(?,?,?,0,?, 'running', ?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET "
        "label=excluded.label, total=excluded.total, done=0, state='running', "
        "note=excluded.note, workflow=excluded.workflow, step=excluded.step, "
        "steps_total=excluded.steps_total, started_at=datetime('now'), "
        "updated_at=datetime('now')",
        (batch_id, label, total, rate_key, note, workflow or None, step, steps_total),
    )
    conn.commit()
    return batch_id


def tick(conn: sqlite3.Connection, batch_id: str, done: int, *, usd: float = 0.0) -> None:
    conn.execute(
        "UPDATE batches SET done=?, usd=?, updated_at=datetime('now') WHERE batch_id=?",
        (done, usd, batch_id),
    )
    conn.commit()


def finish(conn: sqlite3.Connection, batch_id: str, state: str = "done") -> None:
    conn.execute(
        "UPDATE batches SET state=?, updated_at=datetime('now') WHERE batch_id=?",
        (state, batch_id),
    )
    conn.commit()


def running(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Batches that claim to be running, newest first.

    A row is only a claim: a process killed outright cannot mark itself stopped, so the
    dashboard shows how stale the heartbeat is rather than asserting the job is alive.
    """
    try:
        return conn.execute(
            "SELECT *, CAST((julianday('now') - julianday(updated_at)) * 86400 AS INTEGER) "
            "AS stale_seconds FROM batches WHERE state='running' ORDER BY started_at DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def eta_minutes(row: sqlite3.Row) -> float | None:
    total, done = row["total"] or 0, row["done"] or 0
    rate = RATES.get(row["rate_key"] or "")
    if not rate or total <= done:
        return None
    return (total - done) / rate


def workflow_eta(conn: sqlite3.Connection, workflow: str) -> dict:
    """Remaining time for a WHOLE workflow, not just the task in flight.

    Steps not yet started have no measured size, so their cost is projected from the
    average of the steps already sized. That is explicitly a projection, and is reported
    separately from the running task's remaining time so the two are never conflated.
    """
    rows = conn.execute(
        "SELECT * FROM batches WHERE workflow=? ORDER BY step", (workflow,)
    ).fetchall()
    if not rows:
        return {}
    current = next((r for r in rows if r["state"] == "running"), None)
    task_min = eta_minutes(current) if current is not None else 0.0

    sized = [r for r in rows if (r["total"] or 0) > 0]
    avg_total = sum(r["total"] for r in sized) / len(sized) if sized else 0
    steps_total = max((r["steps_total"] or 1) for r in rows)
    remaining_steps = max(0, steps_total - len(rows))
    rate = RATES.get((current or rows[-1])["rate_key"] or "", 60.0)
    projected = (remaining_steps * avg_total / rate) if rate else 0.0

    return {
        "task": current["label"] if current is not None else None,
        "task_minutes": task_min or 0.0,
        "workflow_minutes": (task_min or 0.0) + projected,
        "steps_done": sum(1 for r in rows if r["state"] == "done"),
        "steps_total": steps_total,
        "projected_steps": remaining_steps,
    }
