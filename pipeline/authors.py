"""Author identity — one normalization, used everywhere.

Two places depend on answering "is this the same writer?" and they must agree:
attestation (a repeat from an author already attesting is emphasis, not
corroboration) and the backlog ledger (batches are per author). If they drifted,
corroboration counts and batch boundaries would disagree about who wrote what.

Two layers, and the distinction is the whole point:

  `author_key`  — normalizes a raw From header to a bare email. This is a CHANNEL.
  `identity_of` — resolves a channel key to the LITERAL PERSON who wrote it.

A channel is not a person. `email@stratechery.com` and `stratechery.com` are one
writer, so without the second layer the same essay arriving by email and by web would
read as two independent sources corroborating each other — manufacturing precisely the
signal author-aware attestation exists to protect. Anchoring on the person is also what
makes a guest post attribute to the guest rather than to the venue that hosted it.

The mapping cannot be derived: measured over the real ledger, the channel usually does
not name the writer (`email@stratechery.com` -> Ben Thompson). It is curated, and an
unconfirmed row is never trusted for corroboration.
"""
from __future__ import annotations

import re
import sqlite3


def author_key(raw: str | None) -> str | None:
    """Normalize a From header to a bare lowercase email.

    `The Author <Author+weekly@Substack.com>` → `author@substack.com`, so display-name
    changes and per-list `+suffix` addressing don't fragment one writer into several.
    Returns None for empty input; returns the input lowercased if it isn't an address.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if "<" in raw and ">" in raw:
        raw = raw[raw.find("<") + 1 : raw.find(">")]
    email = raw.strip().lower()
    if "@" in email:
        local, _, domain = email.partition("@")
        email = f"{local.split('+')[0]}@{domain}"
    return email or None


# ── identity layer ────────────────────────────────────────────────────────────
def slug(name: str) -> str:
    """`Anne-Laure Le Cunff` → `anne-laure-le-cunff`, for readable identity ids."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower())
    return s.strip("-")


def identity_of(conn: sqlite3.Connection, key: str | None, *, confirmed_only: bool = True) -> str | None:
    """Resolve a channel key to its identity id, or None if unmapped.

    `confirmed_only` is the default because a `proposed` alias is a harvest suggestion,
    and acting on an unconfirmed one would fuse two real writers into a single voice —
    silently deflating corroboration in a way nothing downstream could detect. Callers
    that want to SHOW proposals (review UIs, the seeding report) pass False.
    """
    if not key:
        return None
    sql = "SELECT identity_id FROM identity_aliases WHERE alias=?"
    if confirmed_only:
        sql += " AND confidence='curated'"
    row = conn.execute(sql, (key.strip().lower(),)).fetchone()
    return row["identity_id"] if row else None


def resolve(conn: sqlite3.Connection, raw: str | None) -> tuple[str | None, str | None]:
    """`(channel_key, identity_id)` for a raw From header or byline.

    Returns the key even when the identity is unknown: an unmapped source still gets a
    claim committed, its attestation just cannot corroborate yet (see `provisional`).
    """
    key = author_key(raw)
    return key, identity_of(conn, key)


def upsert_identity(
    conn: sqlite3.Connection, identity_id: str, display_name: str, *, kind: str = "person", note: str | None = None
) -> str:
    conn.execute(
        "INSERT INTO identities(identity_id, display_name, kind, note) VALUES(?,?,?,?) "
        "ON CONFLICT(identity_id) DO UPDATE SET display_name=excluded.display_name, "
        "kind=excluded.kind, note=COALESCE(excluded.note, identities.note)",
        (identity_id, display_name, kind, note),
    )
    return identity_id


def add_alias(
    conn: sqlite3.Connection,
    alias: str,
    identity_id: str,
    *,
    kind: str | None = None,
    confidence: str = "proposed",
    source: str | None = None,
) -> None:
    """Map a channel key to an identity.

    A curated row is never silently downgraded by a later harvest: confirmation is human
    work and re-running the seeder must not undo it.
    """
    existing = conn.execute(
        "SELECT confidence FROM identity_aliases WHERE alias=?", (alias.strip().lower(),)
    ).fetchone()
    if existing and existing["confidence"] == "curated" and confidence != "curated":
        return
    conn.execute(
        "INSERT INTO identity_aliases(alias, identity_id, kind, confidence, source) VALUES(?,?,?,?,?) "
        "ON CONFLICT(alias) DO UPDATE SET identity_id=excluded.identity_id, kind=excluded.kind, "
        "confidence=excluded.confidence, source=excluded.source",
        (alias.strip().lower(), identity_id, kind, confidence, source),
    )


def confirm(conn: sqlite3.Connection, alias: str) -> bool:
    """Promote a proposed alias to curated. Returns False if the alias is unknown."""
    cur = conn.execute(
        "UPDATE identity_aliases SET confidence='curated' WHERE alias=?", (alias.strip().lower(),)
    )
    return cur.rowcount > 0
