"""Weekly feed batching — the ongoing half, after the pre-cutoff seed.

Batches are Saturday→Friday windows. Phil's cutoff (2026-08-01) is itself a Saturday, so
the seed corpus and the first feed week meet exactly with no gap and no overlap.

The window IS the cursor. That is the point: a UID or "last seen" pointer is stateful and
goes wrong in the ways mailboxes go wrong — messages arriving out of order, a reconnect
losing its place, a re-run double-counting. A date range is a NAME, so pulling week
2026-08-01 is the same operation whenever you run it, and re-running is free because
archiving is idempotent by content hash.

Feed weeks land in the SAME `backlog` ledger as the seed, with `batch_id` set to
`week:<saturday>`. Everything already built on that table — run, status, failures, retry,
triage — works on a feed week without changes.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from pipeline.config import Settings

WEEK_ANCHOR = 5   # Saturday, in Python's Monday=0 convention


@dataclass
class Week:
    start: date       # Saturday
    end: date         # the following Friday, inclusive

    @property
    def batch_id(self) -> str:
        return f"week:{self.start.isoformat()}"

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()} → {self.end.isoformat()}"

    def is_complete(self, today: date) -> bool:
        """A week is complete once its Friday has passed — only then is it a full batch."""
        return self.end < today


def week_start(d: date) -> date:
    """The Saturday on or before `d`."""
    return d - timedelta(days=(d.weekday() - WEEK_ANCHOR) % 7)


def weeks_since(start: date, today: date) -> list[Week]:
    """Every Saturday→Friday window from `start` up to and including the current one."""
    out, cur = [], week_start(start)
    while cur <= today:
        out.append(Week(cur, cur + timedelta(days=6)))
        cur += timedelta(days=7)
    return out


def pull_week(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    label: str,
    week: Week,
    batch_size: int = 200,
    progress=None,
) -> dict:
    """Archive one week's messages from `label` into the ledger, tagged with its batch id.

    Read-only against the mailbox. Re-running a week is a no-op beyond re-reading it,
    because `archive_message` keys on the sha256 of the raw bytes.
    """
    from imap_tools import AND, MailBox

    from pipeline.ingestors.email import archive_message

    host = settings.email_config.get("host", "imap.gmail.com")
    user, password = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
    if not user or not password:
        raise RuntimeError("set IMAP_USER and IMAP_PASSWORD (Gmail app password) to pull a feed week")

    counts = {"added": 0, "known": 0, "duplicate": 0, "seen": 0}
    with MailBox(host).login(user, password, initial_folder=label) as mailbox:
        # date_gte/date_lt is a half-open range: the Saturday is included, the NEXT
        # Saturday is not, so consecutive weeks tile without overlapping.
        criteria = AND(date_gte=week.start, date_lt=week.end + timedelta(days=1))
        for msg in mailbox.fetch(criteria, mark_seen=False, bulk=batch_size):
            sent = getattr(msg, "date", None)
            result = archive_message(
                settings, conn, msg, sent_at=sent.isoformat() if sent else None
            )
            counts[result["status"]] += 1
            counts["seen"] += 1
            if result.get("eml_hash"):
                conn.execute(
                    "UPDATE backlog SET batch_id=?, updated_at=datetime('now') WHERE eml_hash=?",
                    (week.batch_id, result["eml_hash"]),
                )
            if progress and counts["seen"] % 25 == 0:
                progress(counts)
    conn.commit()
    return counts


def week_status(conn: sqlite3.Connection, weeks: list[Week]) -> list[dict]:
    """Per-week ledger counts, so a missed week is visible rather than silently skipped."""
    out = []
    for w in weeks:
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(state='ingested') ingested, "
            "SUM(triage='process') process, SUM(triage IS NULL) untriaged "
            "FROM backlog WHERE batch_id=?", (w.batch_id,),
        ).fetchone()
        out.append({
            "week": w, "archived": row["n"] or 0, "ingested": row["ingested"] or 0,
            "process": row["process"] or 0, "untriaged": row["untriaged"] or 0,
        })
    return out
