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
