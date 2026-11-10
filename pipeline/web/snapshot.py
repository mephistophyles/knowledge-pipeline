"""Import a locally saved page — the path that needs no server request at all.

The Robots Exclusion Protocol governs automated agents issuing their own requests. A page
already rendered in a browser tab involved no additional request, so there is nothing for
robots.txt to have an opinion about. That is why this exists: rather than relaxing the
robots policy anywhere, pages our fetcher may not request are saved by hand and imported
here. A Chrome extension reading the loaded tab would be the same act with a faster
clipboard.

Snapshots land in the SAME content-addressed archive as fetched pages, and extraction
reads the archive without caring how the bytes arrived. `fetch_hash` is still the sha256
of the raw file bytes.

Handles what browsers actually produce:
  - "Webpage, Single File" / .mhtml  → MIME, with the URL in `Snapshot-Content-Location`
  - "Webpage, Complete" / .html      → Chrome writes a `saved from url=(NNNN)…` comment
  - anything else                    → `--url` supplies the provenance
"""
from __future__ import annotations

import email
import email.policy
import re
import sqlite3
from pathlib import Path

from pipeline.config import Settings
from pipeline.web.canonical import canonicalize, site_of
from pipeline.web.fetch import Fetched
from pipeline.web.ledger import archive, already_have, identity_for

# Chrome/IE write this as the first line of a "save complete" page. The (0044) is the
# byte length of the URL that follows.
_SAVED_FROM = re.compile(rb"<!--\s*saved from url=\(\d+\)(?P<url>[^\s>]+)\s*-->", re.I)
_CANONICAL = re.compile(
    rb"""<link[^>]+rel=["']?canonical["']?[^>]*href=["']([^"']+)["']""", re.I)
_CANONICAL_ALT = re.compile(
    rb"""<link[^>]+href=["']([^"']+)["'][^>]*rel=["']?canonical["']?""", re.I)

SNAPSHOT_SUFFIXES = {".html", ".htm", ".mhtml", ".mht", ".xhtml"}


class SnapshotError(Exception):
    pass


def _from_mhtml(raw: bytes) -> tuple[bytes, str | None]:
    """`(html_bytes, url)` from an MHTML archive.

    MHTML is MIME, so the stdlib parses it. The URL is authoritative here — the browser
    recorded where the page actually came from.
    """
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    url = msg.get("Snapshot-Content-Location") or msg.get("Content-Location")
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                loc = part.get("Content-Location")
                return payload, url or loc
    raise SnapshotError("no text/html part in the MHTML archive")


def url_of(raw: bytes) -> str | None:
    """Recover the page's own URL from a saved file, if it recorded one."""
    m = _SAVED_FROM.search(raw[:4096])
    if m:
        candidate = m.group("url").decode("utf-8", "replace").strip()
        if candidate.startswith("http"):
            return candidate
    for pattern in (_CANONICAL, _CANONICAL_ALT):
        m = pattern.search(raw)
        if m:
            candidate = m.group(1).decode("utf-8", "replace").strip()
            if candidate.startswith("http"):
                return candidate
    return None


def read_snapshot(path: Path) -> tuple[bytes, str | None]:
    """`(html_bytes, discovered_url)` for one saved file."""
    raw = path.read_bytes()
    if not raw.strip():
        raise SnapshotError(f"{path.name} is empty")
    if path.suffix.lower() in (".mhtml", ".mht"):
        return _from_mhtml(raw)
    return raw, url_of(raw)


def import_file(
    settings: Settings,
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    url: str | None = None,
    title: str | None = None,
) -> dict:
    """Archive one saved page into the web ledger.

    An explicit `--url` always wins over what the file recorded: the file may be a reader-
    mode export or a hand-edited save, and the caller knows what they read. Provenance is
    REQUIRED — without a URL there is no hostname, so no author, and a claim with no
    attributable source is worth less than no claim.
    """
    path = Path(path)
    if not path.exists():
        raise SnapshotError(f"no such file: {path}")

    raw, discovered = read_snapshot(path)
    final = url or discovered
    if not final:
        raise SnapshotError(
            f"{path.name} records no source URL — pass --url so the page has an author"
        )
    final = canonicalize(final)

    existing = already_have(conn, url=final)
    if existing and existing["state"] != "escalated":
        return {"url": final, "state": "duplicate", "fetch_hash": existing["fetch_hash"]}
    if existing:
        # A snapshot supersedes an earlier escalation — the reason the page could not be
        # fetched is precisely why it was saved by hand. The placeholder row holds the
        # unique url index, so it must go BEFORE the insert or the archive silently
        # no-ops on conflict and the page is lost.
        conn.execute("DELETE FROM web_backlog WHERE fetch_hash=?", (existing["fetch_hash"],))

    fetched = Fetched(
        url=final,
        requested_url=final,
        body=raw,
        content_type="text/html",
        http_status=0,                     # 0 = never requested; this came off disk
        title=title,
    )
    if not fetched.title:
        from pipeline.web.fetch import _title

        fetched.title = _title(raw.decode("utf-8", "replace"))

    h, is_new = archive(settings, conn, fetched)
    conn.execute(
        "UPDATE web_backlog SET state='archived', escalation=NULL, updated_at=datetime('now') "
        "WHERE fetch_hash=?", (h,),
    )
    conn.commit()
    return {
        "url": final, "state": "archived" if is_new else "duplicate", "fetch_hash": h,
        "title": fetched.title, "identity_id": identity_for(conn, final),
        "source": "snapshot", "resolved_url_from": "flag" if url else "file",
    }


def import_dir(settings: Settings, conn: sqlite3.Connection, directory: str | Path, *, progress=None) -> dict:
    """Import every saved page in a directory. Failures are reported, never fatal."""
    directory = Path(directory)
    files = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in SNAPSHOT_SUFFIXES
    )
    counts: dict[str, int] = {}
    for p in files:
        try:
            r = import_file(settings, conn, p)
        except SnapshotError as e:
            r = {"url": p.name, "state": "failed", "error": str(e)}
        counts[r["state"]] = counts.get(r["state"], 0) + 1
        if progress:
            progress(r)
    return counts
