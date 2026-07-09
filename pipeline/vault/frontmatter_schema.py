"""Frontmatter manifest present on every derived note (plan §7).

One builder per note type. Every field that affects output is recorded so a note
is fully explainable and reproducible from raw + prompt_version + model.
"""
from __future__ import annotations

from datetime import date

# Closed vocabularies — validated on write so a typo can't silently split a type.
NOTE_TYPES = {"source", "claim", "entity", "hub", "relation", "commentary"}
AUTHORITIES = {"derived", "personal"}


def _today() -> str:
    return date.today().isoformat()


def base(
    *,
    note_type: str,
    authority: str,
    source_hash: str,
    pipeline_version: str,
    source_url: str | None = None,
    attribution_mode: str | None = None,
    prompt_version: str | None = None,
    model: str | None = None,
    locked: bool = False,
) -> dict:
    if note_type not in NOTE_TYPES:
        raise ValueError(f"invalid note type {note_type!r}; expected one of {sorted(NOTE_TYPES)}")
    if authority not in AUTHORITIES:
        raise ValueError(f"invalid authority {authority!r}; expected one of {sorted(AUTHORITIES)}")
    return {
        "type": note_type,
        "authority": authority,
        "source_hash": source_hash,
        "source_url": source_url,
        "attribution_mode": attribution_mode,
        "pipeline_version": pipeline_version,
        "prompt_version": prompt_version,
        "model": model,
        "derived_at": _today(),
        "locked": locked,
    }


def source_note(
    *,
    source_hash: str,
    source_type: str,
    pipeline_version: str,
    source_url: str | None = None,
    attribution_mode: str | None = None,
    claims_added: int = 0,
    claims_matched: int = 0,
) -> dict:
    fm = base(
        note_type="source",
        authority="derived",
        source_hash=source_hash,
        pipeline_version=pipeline_version,
        source_url=source_url,
        attribution_mode=attribution_mode,
    )
    # Coverage bookkeeping (plan §6.1): a source note is ALWAYS written, even at 0 claims.
    fm["source_type"] = source_type
    fm["claims_added"] = claims_added
    fm["claims_matched"] = claims_matched
    return fm


def commentary_note(*, source_hash: str, pipeline_version: str, source_url: str | None = None) -> dict:
    # Verbatim personal commentary (plan §4.7): your words, no LLM, authority=personal.
    return base(
        note_type="commentary",
        authority="personal",
        source_hash=source_hash,
        pipeline_version=pipeline_version,
        source_url=source_url,
    )
