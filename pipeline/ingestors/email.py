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

import hashlib
import os
import re
import sqlite3
from email import message_from_bytes, policy

from pipeline.config import Settings
from pipeline.db import backlog as backlog_db
from pipeline.db import jobs, registry
from pipeline.orchestrator import stages

# Newsletter boilerplate the extraction passes through (seen on real Substack mail):
# a "view on the web" header (whose URL is the canonical article link) and bracketed
# tracking-redirect URLs wrapping every link. Strip both; keep the canonical URL.
_VIEW_ON_WEB = re.compile(r"(?im)^.*view this (?:post|email) (?:on the web|in your browser).*$")
_CANONICAL = re.compile(r"view this (?:post|email) on the web at\s+(\S+)", re.I)
_REDIRECT = re.compile(
    r"\s*\[\s*https?://[^\]]*?(?:/redirect/|redirect\?|/CL0/|list-manage|click\.|/track|utm_)[^\]]*\]",
    re.I,
)
_UNSUB = re.compile(r"(?im)^.*(unsubscribe|manage your subscription|update your preferences|©\s*\d{4}).*$")
from pipeline.authors import author_key
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


def _normalize(text: str) -> tuple[str, str | None]:
    """Strip newsletter boilerplate; return (clean_text, canonical_article_url)."""
    m = _CANONICAL.search(text)
    canonical = m.group(1) if m else None
    text = _VIEW_ON_WEB.sub("", text)
    text = _REDIRECT.sub("", text)
    text = _UNSUB.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), canonical


def ingest_message(settings: Settings, conn: sqlite3.Connection, msg, *, reprocess: bool = False) -> str | None:
    """Ingest one message object → artifact + first job. Returns hash, or None if empty.

    `reprocess=True` re-queues an artifact whose chain already finished; the default
    leaves completed work alone (see `jobs.insert_job`).
    """
    raw = _clean_text(msg)
    if not raw:
        return None
    text, canonical = _normalize(raw)
    if not text:
        return None
    meta = _meta(msg)
    meta["canonical_url"] = canonical
    meta["word_count"] = len(text.split())
    h, _ = write_artifact(
        settings.blobstore,
        text.encode("utf-8"),
        source_type="email",
        ingestor_version=INGESTOR_VERSION,
        ext="md",
        source_url=canonical or meta.get("message_id"),  # prefer the real article link
        extra=meta,
    )
    jobs.insert_job(conn, h, stages.first_stage("email"), "email", reprocess=reprocess)
    registry.register(
        conn, h, source_type="email",
        author=meta.get("from"),
        source=meta.get("list_id") or meta.get("from"),  # the newsletter feed
        media="text",
        title=meta.get("subject"),
        word_count=meta["word_count"],
    )
    return h


def ingest_messages(settings: Settings, conn: sqlite3.Connection, messages) -> list[str]:
    return [h for msg in messages if (h := ingest_message(settings, conn, msg)) is not None]


# ── raw .eml archive + backlog scan ──────────────────────────────────────────
# The artifact the chain consumes is NORMALIZED markdown, so its hash moves whenever
# the boilerplate stripper changes. Archiving the raw bytes decouples identity from
# that: `eml_hash` is stable, re-derivation is local, and the mailbox is read once.

EML_PREFIX = "email/eml"


def eml_key(eml_hash: str) -> str:
    """Content-addressed key, sharded two levels so no directory holds 3,000 files."""
    return f"{EML_PREFIX}/{eml_hash[:2]}/{eml_hash[2:4]}/{eml_hash}.eml"


class EmlMessage:
    """Duck-types `imap_tools.MailMessage` from raw RFC822 bytes.

    Both the first ingest and every later re-derivation go through this, so a re-run
    can't silently diverge from the original — there is only one parse path.
    """

    def __init__(self, raw: bytes):
        m = message_from_bytes(raw, policy=policy.default)
        self._m = m
        self.subject = str(m.get("Subject") or "")
        self.from_ = str(m.get("From") or "")
        self.date = str(m.get("Date") or "")
        headers: dict[str, list[str]] = {}
        for key, value in m.items():
            headers.setdefault(key.lower(), []).append(str(value))
        self.headers = {k: tuple(v) for k, v in headers.items()}
        self.text = self._body("plain")
        self.html = self._body("html")

    def _body(self, subtype: str) -> str:
        try:
            part = self._m.get_body(preferencelist=(subtype,))
            return part.get_content() if part is not None else ""
        except Exception:  # malformed MIME shouldn't sink a 3,000-message scan
            return ""


def raw_bytes(msg) -> bytes:
    """Raw RFC822 bytes of an imap_tools message."""
    obj = getattr(msg, "obj", None)
    if obj is not None and hasattr(obj, "as_bytes"):
        return obj.as_bytes()
    raise TypeError("message exposes no raw bytes to archive")


def archive_message(settings: Settings, conn: sqlite3.Connection, msg, *, sent_at: str | None = None) -> dict:
    """Archive one message's raw bytes + record it in the backlog ledger.

    Does NOT ingest — archiving and deriving are separated so the mailbox read can
    finish (and be resumed) without any LLM work happening. Returns a status dict:
    `added`, `known` (already archived), or `duplicate` (another .eml, same Message-ID).
    """
    raw = raw_bytes(msg)
    h = hashlib.sha256(raw).hexdigest()
    key = eml_key(h)
    if not settings.blobstore.exists(key):
        settings.blobstore.write(key, raw)

    # Metadata comes from the ARCHIVED BYTES, not the IMAP object: the ledger must
    # describe what re-derivation will actually see, and the two can differ (the
    # server's parsed view vs. the message itself).
    meta = _meta(EmlMessage(raw))
    if backlog_db.message_id_seen(conn, meta.get("message_id"), h):
        backlog_db.add(
            conn, eml_hash=h, eml_key=key, message_id=meta.get("message_id"),
            author=author_key(meta.get("from")), sent_at=sent_at or meta.get("date"),
            subject=meta.get("subject"), state="duplicate",
        )
        return {"eml_hash": h, "status": "duplicate"}

    added = backlog_db.add(
        conn, eml_hash=h, eml_key=key, message_id=meta.get("message_id"),
        author=author_key(meta.get("from")), sent_at=sent_at or meta.get("date"),
        subject=meta.get("subject"),
    )
    return {"eml_hash": h, "status": "added" if added else "known"}


def ingest_from_eml(settings: Settings, conn: sqlite3.Connection, eml_hash: str, *, reprocess: bool = False) -> str | None:
    """Derive the normalized artifact for one archived .eml and queue its chain.

    This is the re-runnable half: change the normalizer or the prompts, re-run this,
    and the corpus rebuilds from local bytes with no mailbox access.
    """
    row = backlog_db.get(conn, eml_hash)
    if row is None:
        raise KeyError(f"unknown eml_hash {eml_hash[:12]}")
    msg = EmlMessage(settings.blobstore.read(row["eml_key"]))
    h = ingest_message(settings, conn, msg, reprocess=reprocess)
    backlog_db.set_state(conn, eml_hash, "ingested" if h else "skipped", artifact_hash=h)
    return h


def scan_backlog(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    label: str,
    before: str,
    batch_size: int = 200,
    progress=None,
) -> dict:
    """One-time read of every message in `label` sent BEFORE `before` (YYYY-MM-DD).

    Archives raw bytes and fills the ledger; ingests nothing. Read-only against the
    mailbox (`mark_seen=False`). Safe to re-run: archiving is idempotent by content
    hash, so an interrupted scan resumes by simply running it again.
    """
    from datetime import date as _date

    from imap_tools import AND, MailBox

    host = settings.email_config.get("host", "imap.gmail.com")
    user, password = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
    if not user or not password:
        raise RuntimeError("set IMAP_USER and IMAP_PASSWORD (Gmail app password) to scan the backlog")

    y, m, d = (int(x) for x in before.split("-"))
    counts = {"added": 0, "known": 0, "duplicate": 0, "seen": 0}
    with MailBox(host).login(user, password, initial_folder=label) as mailbox:
        # headers-only would be cheaper, but we need the full body to archive it —
        # this is the single pass that buys never touching IMAP again.
        for msg in mailbox.fetch(AND(date_lt=_date(y, m, d)), mark_seen=False, bulk=batch_size):
            sent = getattr(msg, "date", None)
            result = archive_message(settings, conn, msg, sent_at=sent.isoformat() if sent else None)
            counts[result["status"]] += 1
            counts["seen"] += 1
            if progress and counts["seen"] % 100 == 0:
                progress(counts)
    return counts


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
