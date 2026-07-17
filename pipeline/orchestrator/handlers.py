"""Stage handlers. A handler takes a StageContext and returns the path of the
intermediate artifact it produced (or None).

The derivation chain splits **producing** from **committing** (the rule that keeps
eval-compare / offline / promote clean, plan invariant 3):

  - `source_note`  — writes the always-present source note (no LLM).
  - `extract_claims` — PRODUCER: one LLM call → candidate claims persisted as a
    keyed intermediate + a cost/latency row. Writes NO vault note.
  - `dedup` — COMMITTER: materializes candidates into claim notes carrying the
    generating key. (Minimal for now: one note per candidate; real embedding +
    attestation dedup, plan §6.3, is a later PR.)
  - `entities` — still a build-step-5 stub.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Callable

from pipeline.db import claims_index, costs
from pipeline.llm import Message, gen_key
from pipeline.llm import prompts, registry
from pipeline.orchestrator.context import StageContext
from pipeline.storage.manifest import load_artifact
from pipeline.vault import frontmatter_schema as fm
from pipeline.vault.writer import read_note


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


def extract_claims(ctx: StageContext) -> str:
    """PRODUCER: extract candidate claims via the configured provider/model.

    Persists a keyed intermediate (candidates + generating key + usage) and logs
    a cost/latency row. Writes no vault note — that is `dedup`'s job.
    """
    text = load_artifact(ctx.blobstore, ctx.manifest).decode("utf-8", errors="replace")
    mc = ctx.settings.stage_model("extract_claims")
    provider = registry.get_provider(ctx.settings, mc.provider)
    system = prompts.load_prompt(ctx.settings, "extract_claims", mc.prompt_version)

    completion = provider.complete(
        [Message("system", system), Message("user", text)], mc.model, mc.params
    )
    claims = _parse_claims(completion.text)

    key = gen_key(
        provider=provider.name,
        model=mc.model,
        params=mc.params,
        prompt_version=mc.prompt_version,
        input_hash=ctx.artifact_hash,
    )
    payload = {
        "gen_key": key,
        "claims": claims,
        "raw_response": completion.text,
        "usage": {
            "tokens_in": completion.tokens_in,
            "tokens_out": completion.tokens_out,
            "usd": completion.usd,
            "latency_ms": completion.latency_ms,
        },
    }
    out = ctx.write_intermediate_keyed("extract_claims", key, payload)
    costs.record(
        ctx.conn,
        ctx.artifact_hash,
        "extract_claims",
        completion.model,
        completion.tokens_in,
        completion.tokens_out,
        completion.usd,
        provider=provider.name,
        latency_ms=completion.latency_ms,
    )
    return out


def dedup(ctx: StageContext) -> str:
    """COMMITTER (plan §6.3): for each candidate, embed → shortlist nearest claims
    → cheap-LLM confirm. On a match, append an attestation to the existing claim
    note (rolling linkage); on a miss, create a new claim note and index it.
    """
    data = json.loads(Path(ctx.input_path).read_text()) if ctx.input_path else {}
    key = data.get("gen_key", {})
    candidates = data.get("claims", [])
    source_id = _source_id(ctx)

    emb_cfg = ctx.settings.embeddings_config
    dcfg = ctx.settings.dedup_config
    emb_provider = registry.get_provider(ctx.settings, emb_cfg["provider"])
    confirm_mc = ctx.settings.stage_model("dedup")
    confirm_provider = registry.get_provider(ctx.settings, confirm_mc.provider)
    confirm_prompt = prompts.load_prompt(ctx.settings, "dedup_confirm", confirm_mc.prompt_version)

    new_ids: list[str] = []
    attested: list[str] = []
    for i, c in enumerate(candidates):
        text = (c.get("text") or "").strip()
        if not text:
            continue
        quote = (c.get("quote") or "").strip()

        emb = emb_provider.embed([text], emb_cfg["model"])
        costs.record(
            ctx.conn, ctx.artifact_hash, "dedup:embed", emb.model, emb.tokens, 0, emb.usd,
            provider=emb.provider, latency_ms=emb.latency_ms,
        )
        vec = _normalize(emb.vectors[0])

        matched_id = None
        for m in claims_index.nearest(ctx.conn, vec, dcfg["shortlist_k"]):
            if m["distance"] > dcfg["max_distance"]:
                break  # sorted ascending — nothing closer remains
            verdict = confirm_provider.complete(
                [Message("system", confirm_prompt), Message("user", f"A: {m['text']}\nB: {text}")],
                confirm_mc.model, confirm_mc.params,
            )
            costs.record(
                ctx.conn, ctx.artifact_hash, "dedup:confirm", verdict.model,
                verdict.tokens_in, verdict.tokens_out, verdict.usd,
                provider=verdict.provider, latency_ms=verdict.latency_ms,
            )
            if _parse_same(verdict.text):
                matched_id = m["claim_id"]
                break

        if matched_id:
            _append_attestation(ctx, matched_id, _attestation(ctx, quote, key.get("model")))
            attested.append(matched_id)
        else:
            claim_id = f"claim-{ctx.artifact_hash[:8]}-{i:02d}"
            att = _attestation(ctx, quote, key.get("model"))
            metadata = fm.claim_note(
                source_hash=ctx.artifact_hash,
                pipeline_version=ctx.settings.pipeline_version,
                provider=key.get("provider"),
                model=key.get("model"),
                params=key.get("params"),
                prompt_version=key.get("prompt_version"),
                source_url=ctx.manifest.source_url,
                attestations=[att],
            )
            ctx.vault.write_note(f"corpus/claims/{claim_id}.md", metadata, _claim_body(text, [att]))
            claims_index.add_claim(
                ctx.conn, claim_id, ctx.artifact_hash, text, ctx.manifest.source_url, key.get("model"), vec
            )
            new_ids.append(claim_id)

    ctx.vault.commit(f"[claims] {source_id}: +{len(new_ids)} new, {len(attested)} attested")
    return ctx.write_intermediate(
        "dedup.json", {"new": new_ids, "attested": attested, "count": len(new_ids)}
    )


# ── dedup helpers ─────────────────────────────────────────────────────────────
def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _parse_same(text: str) -> bool:
    obj = _extract_json(text)
    if isinstance(obj, dict):
        return bool(obj.get("same"))
    return False  # conservative: unparseable verdict → distinct, never a bad merge


def _attestation(ctx: StageContext, quote: str, model: str | None) -> dict:
    return {
        "source_hash": ctx.artifact_hash,
        "source_url": ctx.manifest.source_url,
        "date": date.today().isoformat(),
        "model": model,
        "quote": quote,
    }


def _attestation_line(a: dict) -> str:
    return f"- `{a['source_hash'][:12]}` ({a.get('source_url') or '—'}) — \"{a.get('quote', '')}\" [{a.get('date', '')}]"


def _claim_body(text: str, attestations: list[dict]) -> str:
    body = f"# {text}\n\n## Attestations\n\n"
    for a in attestations:
        body += _attestation_line(a) + "\n"
    return body


def _append_attestation(ctx: StageContext, claim_id: str, attestation: dict) -> None:
    relpath = f"corpus/claims/{claim_id}.md"
    post = read_note(ctx.vault.root / relpath)
    meta = dict(post.metadata)
    meta["attestations"] = list(meta.get("attestations") or []) + [attestation]
    content = post.content.rstrip()
    line = _attestation_line(attestation)
    if "## Attestations" in content:
        content = f"{content}\n{line}\n"
    else:
        content = f"{content}\n\n## Attestations\n\n{line}\n"
    ctx.vault.write_note(relpath, meta, content)
    claims_index.bump_attestation(ctx.conn, claim_id)


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


def _parse_claims(text: str) -> list[dict]:
    """Lenient parse of a claims response into ``[{text, quote}, ...]``.

    Providers vary — some wrap JSON in prose or a code fence, some return an
    object. Prompt asks for a bare array; we tolerate the common deviations
    rather than fail a whole stage on formatting.
    """
    raw = _extract_json(text)
    if isinstance(raw, dict):
        raw = raw.get("claims") or raw.get("items") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            claim = item.strip()
            quote = ""
        elif isinstance(item, dict):
            claim = (item.get("claim") or item.get("text") or "").strip()
            quote = (item.get("quote") or "").strip()
        else:
            continue
        if claim:
            out.append({"text": claim, "quote": quote})
    return out


def _extract_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except Exception:
        pass
    # Salvage the outermost array or object embedded in surrounding prose.
    for open_c, close_c in (("[", "]"), ("{", "}")):
        i, j = text.find(open_c), text.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(text[i : j + 1])
            except Exception:
                continue
    return None


HANDLERS: dict[str, Callable[[StageContext], str]] = {
    "source_note": source_note,
    "extract_claims": extract_claims,
    "dedup": dedup,
    "entities": _stub("entities"),
    "personal": personal,
}


def get_handler(stage: str) -> Callable[[StageContext], str]:
    try:
        return HANDLERS[stage]
    except KeyError:
        raise KeyError(f"no handler registered for stage {stage!r}")
