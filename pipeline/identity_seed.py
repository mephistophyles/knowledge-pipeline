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
from pathlib import Path


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

    stripped = _VENUE_SUFFIX.sub("", display).strip()
    if not stripped:
        return None, "org", "venue-only after suffix strip"

    # Possessives are checked on the REMAINDER, not the raw display name: the venue is
    # very often possessive while the writer is not — `Jim Cook from Cook's PlayBooks`
    # is a person, and testing before the strip abstained on him.
    if _POSSESSIVE.search(stripped):
        return None, "org", f"possessive publication name {stripped!r}"

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


# ── the curated record ────────────────────────────────────────────────────────
CURATION_PATH = "config/identities.yaml"


def load_curation(settings: Settings, path: str | None = None) -> list[dict]:
    """Read the curated identity file. Missing file → empty, not an error."""
    import yaml

    p = Path(path) if path else (settings.root / CURATION_PATH)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text()) or {}
    return data.get("identities") or []


def upsert_curation(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    identity_id: str,
    name: str,
    kind: str,
    alias: str,
    path: str | None = None,
) -> dict:
    """Record a mapping in BOTH the database and `config/identities.yaml`.

    The file is the canonical record, so a decision made in the dashboard has to land
    there too — otherwise the DB drifts from the file and the next `identity sync`
    silently reverts what a human just decided.

    Existing identities gain the alias; new ones are appended. The header comment is
    preserved because it states the rules the file is curated by, and a round-trip through
    yaml would drop it.
    """
    import yaml

    p = Path(path) if path else (settings.root / CURATION_PATH)
    header, entries = "", []
    if p.exists():
        text = p.read_text()
        header = "".join(
            ln for ln in text.splitlines(keepends=True)[: _header_len(text)]
        )
        entries = (yaml.safe_load(text) or {}).get("identities") or []

    alias = alias.strip().lower()
    entry = next((e for e in entries if e.get("id") == identity_id), None)
    if entry is None:
        entry = {"id": identity_id, "name": name, "kind": kind, "aliases": []}
        entries.append(entry)
    entry["name"] = name or entry.get("name") or identity_id
    entry["kind"] = kind or entry.get("kind", "person")
    # An alias belongs to exactly one identity; moving it means removing it elsewhere,
    # or resolution would depend on row order.
    for other in entries:
        if other is not entry:
            other["aliases"] = [a for a in (other.get("aliases") or []) if a != alias]
    if alias not in (entry.get("aliases") or []):
        entry.setdefault("aliases", []).append(alias)

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(header + yaml.safe_dump({"identities": entries}, sort_keys=False,
                                         allow_unicode=True, width=100))

    authors.upsert_identity(conn, identity_id, entry["name"], kind=entry["kind"])
    authors.add_alias(conn, alias, identity_id, confidence="curated", source="dashboard")
    conn.commit()
    return {"identity_id": identity_id, "name": entry["name"], "alias": alias}


def _header_len(text: str) -> int:
    """Number of leading comment/blank lines to keep verbatim."""
    n = 0
    for ln in text.splitlines():
        if ln.startswith("#") or not ln.strip():
            n += 1
        else:
            break
    return n


def sync(settings: Settings, conn: sqlite3.Connection, path: str | None = None) -> tuple[int, int, list[str]]:
    """Apply the curated file: every listed alias becomes `curated`.

    This file is the canonical record — the thing that guarantees a source defined as
    canonical stays canonical, so a channel seen for the first time next month resolves to
    the identity decided today rather than becoming a new author. Returns
    `(identities, aliases, conflicts)`; a conflict is one alias claimed by two identities,
    which would make resolution order-dependent.
    """
    entries = load_curation(settings, path)
    claimed: dict[str, str] = {}
    conflicts: list[str] = []
    n_alias = 0
    for e in entries:
        ident = e["id"]
        authors.upsert_identity(
            conn, ident, e.get("name") or ident, kind=e.get("kind", "person"), note=e.get("note")
        )
        for alias in e.get("aliases") or []:
            key = alias.strip().lower()
            if key in claimed and claimed[key] != ident:
                conflicts.append(f"{key}: {claimed[key]} vs {ident}")
                continue
            claimed[key] = ident
            authors.add_alias(
                conn, key, ident,
                kind="host" if "@" not in key else "email",
                confidence="curated", source="config/identities.yaml",
            )
            n_alias += 1
    conn.commit()
    return len(entries), n_alias, conflicts
