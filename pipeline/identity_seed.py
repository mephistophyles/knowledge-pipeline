"""Seed the identity table by harvesting display names from the archived `.eml` bytes.

The backlog table stores only the normalized channel key, but the raw messages retain the
`From` display name — and in most cases that IS the person. Three shapes occur in the real
corpus:

    Ben Thompson <email@stratechery.com>                 → bare person
    CJ Gustafson from Mostly Metrics <…>                 → person + venue
    Department of Product <departmentofproduct@…>        → venue only, person absent

Only the first two yield a person. The third cannot be resolved from the data at all, and
guessing would be worse than admitting it: a confidently wrong author silently merges two
writers or splits one.

So this PROPOSES and Phil confirms. Every row lands as `confidence='proposed'`, which
`identity_of` refuses to trust, and `pipeline identity confirm` promotes. Curated rows are
never downgraded by a re-run.
"""
from __future__ import annotations

import email
import re
import sqlite3
from dataclasses import dataclass
from email.header import decode_header, make_header


from pipeline import authors
from pipeline.config import Settings

# "X from Y", "X at Y", "X | Y", "X - Y", "X (Y)" — the venue suffix, stripped to leave
# the person. Kept deliberately narrow: over-eager splitting mangles real names.
_VENUE_SUFFIX = re.compile(r"\s+(?:from|at|@|\||//|·)\s+.+$|\s+\(.+\)$|,\s+.+$", re.I)

# "Release Notes by Jake MccGwire" — the venue leads and the person trails.
_BY_AUTHOR = re.compile(r"^.+\bby\s+(?P<person>[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*){1,3})$")

# "Elena's Growth Scoop" — a possessive is a publication naming itself after its writer,
# not the writer. The person is in there but the string is not their name.
_POSSESSIVE = re.compile(r"\w['’]s\s")

# Words that mark a display name as a publication rather than a human. A name containing
# one of these is not proposed as a person — it goes to the review list instead.
_VENUE_WORDS = re.compile(
    r"\b(newsletter|weekly|daily|digest|report|insights|bulletin|labs?|media|team|group|"
    r"blog|podcast|show|club|corner|notes?|dispatch|brief|post|times|journal|review|"
    r"inc|llc|ltd|co|hq|the)\b",
    re.I,
)


@dataclass
class Proposal:
    alias: str                  # the channel key
    display: str                # raw From display name, as seen
    person: str | None          # extracted person, or None when the header names a venue
    kind: str                   # 'person' | 'org'
    editions: int
    reason: str

    @property
    def identity_id(self) -> str:
        base = self.person or self.display
        return f"{self.kind}:{authors.slug(base)}"


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def display_name_of(from_header: str | None) -> str:
    """The display portion of a From header, decoded, unfolded, and unquoted.

    Headers wrap across lines (RFC 5322 folding), which arrives as an embedded newline —
    `Sebastian Raschka,\\n PhD` — so whitespace is collapsed before anything parses it.
    """
    raw = _decode(from_header)
    if "<" in raw:
        raw = raw[: raw.index("<")]
    return re.sub(r"\s+", " ", raw).strip().strip('"').strip()


def classify(display: str) -> tuple[str | None, str, str]:
    """`(person, kind, reason)` for a display name.

    Returns `person=None` when the header names a publication, because the writer is
    genuinely absent from the data — a case for curation, not inference.
    """
    if not display:
        return None, "org", "no display name"

    by = _BY_AUTHOR.match(display)
    if by:  # the venue leads, the writer trails — take the writer
        return by.group("person"), "person", "person after 'by'"
    if _POSSESSIVE.search(display):
        return None, "org", f"possessive publication name {display!r}"

    stripped = _VENUE_SUFFIX.sub("", display).strip()
    if not stripped:
        return None, "org", "venue-only after suffix strip"

    had_suffix = stripped != display
    tokens = [t for t in stripped.split() if t]

    if _VENUE_WORDS.search(stripped):
        return None, "org", f"publication word in {stripped!r}"
    if "@" in stripped or stripped.lower().startswith(("no-reply", "noreply")):
        return None, "org", "role address as display name"
    if len(tokens) < 2:
        # A single token is ambiguous: a first name ("Sangram", "Dragos") or a brand.
        return None, "org", f"single token {stripped!r} — cannot tell person from brand"
    if len(tokens) > 4:
        return None, "org", "too many tokens for a personal name"
    if not all(t[:1].isupper() for t in tokens if t[:1].isalpha()):
        return None, "org", "not capitalised like a name"

    reason = "person + venue suffix" if had_suffix else "bare person name"
    return stripped, "person", reason


def harvest(settings: Settings, conn: sqlite3.Connection, *, limit_per_author: int = 5) -> list[Proposal]:
    """Read one sample message per backlog author and propose an identity for each.

    Samples several messages per author because a single edition can carry an atypical
    header (a guest issue, a one-off "from the team"); the most common display name across
    the sample is the stable one.
    """
    store = settings.blobstore  # read through the store, so an S3-backed archive works too
    rows = conn.execute(
        "SELECT author, COUNT(*) n FROM backlog WHERE author IS NOT NULL GROUP BY author ORDER BY n DESC"
    ).fetchall()

    proposals: list[Proposal] = []
    for row in rows:
        keys = conn.execute(
            "SELECT eml_key FROM backlog WHERE author=? LIMIT ?", (row["author"], limit_per_author)
        ).fetchall()
        seen: dict[str, int] = {}
        for k in keys:
            try:
                msg = email.message_from_bytes(store.read(k["eml_key"]))
            except Exception:
                continue
            disp = display_name_of(msg.get("From"))
            if disp:
                seen[disp] = seen.get(disp, 0) + 1
        if not seen:
            proposals.append(Proposal(row["author"], "", None, "org", row["n"], "no readable From header"))
            continue
        display = max(seen, key=lambda d: seen[d])
        person, kind, reason = classify(display)
        proposals.append(Proposal(row["author"], display, person, kind, row["n"], reason))
    return proposals


def apply(conn: sqlite3.Connection, proposals: list[Proposal]) -> int:
    """Write proposals to the table as unconfirmed rows. Curated rows are left alone."""
    n = 0
    for p in proposals:
        name = p.person or p.display or p.alias
        authors.upsert_identity(conn, p.identity_id, name, kind=p.kind, note=p.reason)
        before = conn.execute(
            "SELECT confidence FROM identity_aliases WHERE alias=?", (p.alias,)
        ).fetchone()
        if before and before["confidence"] == "curated":
            continue
        authors.add_alias(
            conn, p.alias, p.identity_id, kind="email", confidence="proposed", source="eml-display-name"
        )
        n += 1
    conn.commit()
    return n
