"""Stage handlers. A handler takes a StageContext and returns the path of the
intermediate artifact it produced (or None).

`source_note` and `personal` are real (they write vault notes with no LLM).
`extract_claims` / `dedup` / `entities` are STUBS for build step 5 — they persist
a placeholder intermediate and advance, so the state machine, control plane, and
hand-walk are fully exercisable now.
"""
from __future__ import annotations

import json
from typing import Callable

from pipeline.orchestrator.context import StageContext
from pipeline.storage.manifest import load_artifact
from pipeline.vault import frontmatter_schema as fm


def _source_id(ctx: StageContext) -> str:
    return f"{ctx.manifest.source_type}-{ctx.artifact_hash[:8]}"


def source_note(ctx: StageContext) -> str:
    """Always-written source note (plan §6.1 step 1), even at 0 claims."""
    source_id = _source_id(ctx)
    metadata = fm.source_note(
        source_hash=ctx.artifact_hash,
        source_type=ctx.manifest.source_type,
        pipeline_version=ctx.settings.pipeline_version,
        source_url=ctx.manifest.source_url,
        claims_added=0,
        claims_matched=0,
    )
    body = (
        f"# {source_id}\n\n"
        f"> Source note for artifact `{ctx.artifact_hash[:12]}` "
        f"({ctx.manifest.source_type}).\n\n"
        f"- Raw manifest: `{ctx.manifest.content_hash}`\n"
        f"- Fetched: {ctx.manifest.fetched_at}\n"
        f"- Source URL: {ctx.manifest.source_url or '—'}\n\n"
        "## Abstract\n\n_Structure map + abstract land here once the derivation "
        "chain is built (build step 5)._\n"
    )
    ctx.vault.write_note(f"corpus/sources/{source_id}.md", metadata, body)
    ctx.vault.commit(f"[source] {source_id}")
    return ctx.write_intermediate("source_note.json", {"source_id": source_id})


def personal(ctx: StageContext) -> str:
    """Verbatim personal deriver (plan §4.7 default path): your words, no LLM."""
    text = load_artifact(ctx.blobstore, ctx.manifest).decode("utf-8", errors="replace")
    target = ctx.manifest.annotates
    note_id = f"personal-{ctx.artifact_hash[:8]}"
    metadata = fm.commentary_note(
        source_hash=ctx.artifact_hash,
        pipeline_version=ctx.settings.pipeline_version,
        source_url=ctx.manifest.source_url,
    )
    if target:
        metadata["annotates"] = target
    link = f"\n\nAnnotates: `{target[:12]}`\n" if target else "\n"
    body = f"# Commentary {note_id}\n{link}\n---\n\n{text}\n"
    ctx.vault.write_note(f"personal/commentary/{note_id}.md", metadata, body)
    ctx.vault.commit(f"[personal] {note_id}")
    return ctx.write_intermediate("personal.json", {"note_id": note_id, "annotates": target})


def _stub(stage: str) -> Callable[[StageContext], str]:
    def handler(ctx: StageContext) -> str:
        payload = {
            "stage": stage,
            "status": "stub",
            "note": f"{stage} deriver is a build-step-5 stub; no LLM call made.",
            "artifact_hash": ctx.artifact_hash,
        }
        return ctx.write_intermediate(f"{stage}.json", payload)

    return handler


HANDLERS: dict[str, Callable[[StageContext], str]] = {
    "source_note": source_note,
    "extract_claims": _stub("extract_claims"),
    "dedup": _stub("dedup"),
    "entities": _stub("entities"),
    "personal": personal,
}


def get_handler(stage: str) -> Callable[[StageContext], str]:
    try:
        return HANDLERS[stage]
    except KeyError:
        raise KeyError(f"no handler registered for stage {stage!r}")
