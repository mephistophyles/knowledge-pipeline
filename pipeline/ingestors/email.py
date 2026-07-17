"""Email ingestor (plan §4.1) — IMAP, read-only, scoped by Gmail label.

Each email becomes one content-addressed artifact of **clean markdown** (option
(b): the ingestor pre-extracts text; the existing corpus chain runs unchanged).
Per-email headers (from/subject/date/message-id/list-id) are captured in the
manifest's `extra` for provenance and backlog grouping.

Read-only by behaviour: the IMAP fetch never marks messages seen or mutates the
mailbox. `ingest_messages` is decoupled from the IMAP I/O so it's unit-testable
with fake messages; `fetch_and_ingest` does the live pull.
"""
from __future__ import annotations

import os
import sqlite3

from pipeline.config import Settings
from pipeline.db import jobs
from pipeline.orchestrator import stages
from pipeline.storage.manifest import write_artifact

INGESTOR_VERSION = "email/0.1.0"


def _clean_text(msg) -> str:
    """Prefer the plain-text part; fall back to HTML → markdown."""
    text = (getattr(msg, "text", "") or "").strip()
    if text:
        return text
    html = getattr(msg, "html", "") or ""
    if not html:
        return ""
    import html2text

    h = html2text.HTML2Text()
    h.ignore_images = True
    h.body_width = 0  # don't hard-wrap
    return h.handle(html).strip()


def _header(msg, name: str) -> str | None:
    headers = getattr(msg, "headers", {}) or {}
    val = headers.get(name.lower())
    if isinstance(val, (list, tuple)):
        return val[0] if val else None
    return val


def _meta(msg) -> dict:
    return {
        "from": getattr(msg, "from_", None),
        "subject": getattr(msg, "subject", None),
        "date": str(getattr(msg, "date", "") or ""),
        "message_id": _header(msg, "Message-ID"),
        "list_id": _header(msg, "List-Id"),
    }


def ingest_message(settings: Settings, conn: sqlite3.Connection, msg) -> str | None:
    """Ingest one message object → artifact + first job. Returns hash, or None if empty."""
    text = _clean_text(msg)
    if not text:
        return None
    meta = _meta(msg)
    h, _ = write_artifact(
        settings.blobstore,
        text.encode("utf-8"),
        source_type="email",
        ingestor_version=INGESTOR_VERSION,
        ext="md",
        source_url=meta.get("message_id"),
        extra=meta,
    )
    jobs.insert_job(conn, h, stages.first_stage("email"), "email")
    return h


def ingest_messages(settings: Settings, conn: sqlite3.Connection, messages) -> list[str]:
    return [h for msg in messages if (h := ingest_message(settings, conn, msg)) is not None]


def fetch_and_ingest(settings: Settings, conn: sqlite3.Connection, *, label: str, limit: int = 50) -> list[str]:
    """Live IMAP pull of up to `limit` messages from `label`, newest first.

    Read-only: `mark_seen=False` leaves the mailbox untouched. Credentials come
    from IMAP_USER / IMAP_PASSWORD (a Gmail app password); host from config.
    """
    from imap_tools import MailBox

    host = settings.email_config.get("host", "imap.gmail.com")
    user = os.environ.get("IMAP_USER")
    password = os.environ.get("IMAP_PASSWORD")
    if not user or not password:
        raise RuntimeError("set IMAP_USER and IMAP_PASSWORD (Gmail app password) to ingest email")

    with MailBox(host).login(user, password, initial_folder=label) as mailbox:
        messages = list(mailbox.fetch(limit=limit, mark_seen=False, reverse=True))
    return ingest_messages(settings, conn, messages)
