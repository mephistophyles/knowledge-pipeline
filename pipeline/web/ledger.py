"""The web backlog ledger: archive once, then page the table.

Mirrors the email ledger deliberately. `scan` fetches and archives; nothing derives here.
Separating the cheap resumable half (fetching) from the costly half (extraction and LLM
work) is what lets a backlog be drained incrementally under the control plane.
"""
from __future__ import annotations

import sqlite3

from pipeline import authors
from pipeline.config import Settings
from pipeline.web.canonical import SHARED_PLATFORMS, canonicalize, registrable, site_of
from pipeline.web.fetch import Escalation, Fetcher, Fetched


def record_escalation(conn: sqlite3.Connection, url: str, cause: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO web_escalations(url, cause, detail) VALUES(?,?,?)", (url, cause, detail)
    )


def identity_for(conn: sqlite3.Connection, url: str) -> str | None:
    """Resolve a page's author from its hostname.

    Tries the exact host, then its registrable domain: a writer who publishes at
    `newsletter.example.com` and `example.com` is one writer, and requiring every
    subdomain to be curated separately would fracture them for no reason.

    A shared platform is never consulted at either level — `substack.com` identifies the
    platform, so resolving through it would make every Substack writer the same author.
    Pages that stay unmapped attest provisionally, which is the safe direction.
    """
    host = site_of(url)
    if not host or host in SHARED_PLATFORMS:
        return None
    found = authors.identity_of(conn, host)
    if found:
        return found
    root = registrable(host)
    if root and root != host and root not in SHARED_PLATFORMS:
        return authors.identity_of(conn, root)
    return None


def already_have(conn: sqlite3.Connection, *, url: str | None = None, fetch_hash: str | None = None):
    if fetch_hash:
        row = conn.execute("SELECT * FROM web_backlog WHERE fetch_hash=?", (fetch_hash,)).fetchone()
        if row:
            return row
    if url:
        return conn.execute("SELECT * FROM web_backlog WHERE url=?", (canonicalize(url),)).fetchone()
    return None


def archive(settings: Settings, conn: sqlite3.Connection, fetched: Fetched) -> tuple[str, bool]:
    """Store the raw response and record it. Returns `(fetch_hash, is_new)`."""
    h = fetched.fetch_hash
    if conn.execute("SELECT 1 FROM web_backlog WHERE fetch_hash=?", (h,)).fetchone():
        return h, False

    key = f"web/{h[:2]}/{h}.html"
    settings.blobstore.write(key, fetched.body)
    conn.execute(
        "INSERT INTO web_backlog(fetch_hash, url, requested_url, fetch_key, site, title, "
        "identity_id, content_type, http_status, syndicated_from) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(url) DO NOTHING",
        (h, fetched.url, fetched.requested_url, key, site_of(fetched.url), fetched.title,
         identity_for(conn, fetched.url), fetched.content_type, fetched.http_status,
         fetched.syndicated_from),
    )
    conn.commit()
    return h, True


def add_url(
    settings: Settings, conn: sqlite3.Connection, url: str, *, fetcher: Fetcher | None = None
) -> dict:
    """Fetch and archive one URL. Never raises for an unfetchable page — it escalates.

    The result dict always reports what happened, because a scan over a reading list must
    not stop at the first paywall.
    """
    canonical = canonicalize(url)
    existing = already_have(conn, url=canonical)
    if existing:
        return {"url": canonical, "state": "duplicate", "fetch_hash": existing["fetch_hash"]}

    fetcher = fetcher or Fetcher()
    try:
        fetched = fetcher.fetch(canonical)
    except Escalation as e:
        record_escalation(conn, canonical, e.cause, e.detail)
        conn.execute(
            "INSERT INTO web_backlog(fetch_hash, url, requested_url, site, state, escalation) "
            "VALUES(?,?,?,?,'escalated',?) ON CONFLICT(url) DO UPDATE SET "
            "state='escalated', escalation=excluded.escalation, updated_at=datetime('now')",
            (f"escalated:{canonical}", canonical, canonical, site_of(canonical), e.cause),
        )
        conn.commit()
        return {"url": canonical, "state": "escalated", "cause": e.cause, "detail": e.detail}

    h, is_new = archive(settings, conn, fetched)
    return {
        "url": fetched.url, "state": "archived" if is_new else "duplicate", "fetch_hash": h,
        "title": fetched.title, "identity_id": identity_for(conn, fetched.url),
        "syndicated_from": fetched.syndicated_from,
    }


def scan(
    settings: Settings, conn: sqlite3.Connection, urls: list[str], *, fetcher: Fetcher | None = None,
    progress=None,
) -> dict:
    """Fetch a whole reading list, counting outcomes. One shared fetcher, so rate limiting
    and the robots cache apply across the run rather than per URL."""
    fetcher = fetcher or Fetcher()
    counts: dict[str, int] = {}
    for url in urls:
        result = add_url(settings, conn, url, fetcher=fetcher)
        counts[result["state"]] = counts.get(result["state"], 0) + 1
        if result["state"] == "escalated":
            counts[f"cause:{result['cause']}"] = counts.get(f"cause:{result['cause']}", 0) + 1
        if progress:
            progress(result)
    return counts


def read_url_list(path: str) -> list[str]:
    """URLs from a plain list or a CSV export, deduplicated, order preserved.

    Reading lists arrive in whatever shape the source exported — one per line from a
    scratch file, or a CSV from Pocket/Instapaper/Readwise with a header and extra
    columns. Rather than making the caller reshape it, take the first cell of each row
    that looks like a URL and ignore everything else; a header row has no URL in it and
    drops out for free.

    Deduplicated on the CANONICAL form, so the same article listed twice under different
    tracking parameters is fetched once.
    """
    import csv
    import io

    from pathlib import Path

    text = Path(path).read_text()
    urls: list[str] = []
    for row in csv.reader(io.StringIO(text)):
        for cell in row:
            cell = cell.strip().strip('"').strip()
            if not cell or cell.startswith("#"):
                continue
            if cell.lower().startswith(("http://", "https://")):
                urls.append(cell)
                break  # one URL per row; later columns are metadata

    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        try:
            key = canonicalize(u)
        except Exception:
            continue
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def escalation_rates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Escalations by cause and month — the evidence for the deferred fetch decision."""
    return conn.execute(
        "SELECT substr(at,1,7) month, cause, COUNT(*) n FROM web_escalations "
        "GROUP BY month, cause ORDER BY month DESC, n DESC"
    ).fetchall()


def summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT state, COUNT(*) n FROM web_backlog GROUP BY state").fetchall()
    out = {r["state"]: r["n"] for r in rows}
    out["unmapped_authors"] = conn.execute(
        "SELECT COUNT(DISTINCT site) FROM web_backlog WHERE identity_id IS NULL AND state<>'escalated'"
    ).fetchone()[0]
    return out
