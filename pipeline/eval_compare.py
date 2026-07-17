"""Eval-compare (plan §6.5): run a producer stage under several
{provider, model, params} variants over one source, hold the artifact, and write
a side-by-side report of outputs + cost + latency. Approve one variant and the
chain resumes committing from that output — no recompute.

Only *producer* stages are eval-able: they write a keyed intermediate and no vault
note, so running N variants is side-effect-free (N intermediates + N cost rows,
never N commits). `dedup`/`entities` commit, so they're excluded.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pipeline.config import Settings
from pipeline.db import controls as ctl
from pipeline.db import jobs
from pipeline.orchestrator import stages
from pipeline.orchestrator.context import StageContext
from pipeline.orchestrator.handlers import get_handler

EVALABLE = {"extract_claims"}


def run_eval(settings: Settings, conn: sqlite3.Connection, artifact_hash: str, stage: str) -> dict:
    """Run `stage` under every configured variant; hold the artifact; write a report."""
    if stage not in EVALABLE:
        raise ValueError(f"stage {stage!r} is not eval-able (producer stages only: {sorted(EVALABLE)})")
    variants = (settings.raw.get("evals", {}) or {}).get(stage)
    if not variants:
        raise ValueError(f"no `evals.{stage}:` variants configured")
    job = jobs.get_job(conn, artifact_hash, stage)
    if job is None:
        raise ValueError(f"no job {artifact_hash[:12]}/{stage} (run its upstream stages first)")
    if job["status"] == "done":
        raise ValueError(f"{stage} is already done for {artifact_hash[:12]}")

    results: list[dict] = []
    original = (settings.raw.get("models", {}) or {}).get(stage)
    try:
        for i, variant in enumerate(variants):
            settings.raw.setdefault("models", {})[stage] = variant
            ctx = StageContext.build(settings, conn, artifact_hash, stage, job["input_path"])
            out = get_handler(stage)(ctx)  # producer: keyed intermediate + cost row, no commit
            payload = json.loads(Path(out).read_text())
            usage = payload.get("usage", {}) or {}
            results.append(
                {
                    "index": i,
                    "provider": variant.get("provider"),
                    "model": variant.get("model"),
                    "params": variant.get("params", {}),
                    "prompt_version": variant.get("prompt_version", "v1"),
                    "intermediate": out,
                    "num_outputs": len(payload.get("claims") or []),
                    "outputs": [
                        {"text": c.get("text", ""), "quote": c.get("quote", "")}
                        for c in (payload.get("claims") or [])
                    ],
                    "tokens_in": usage.get("tokens_in"),
                    "tokens_out": usage.get("tokens_out"),
                    "usd": usage.get("usd"),
                    "latency_ms": usage.get("latency_ms"),
                }
            )
    finally:
        if original is not None:
            settings.raw["models"][stage] = original

    # Freeze the artifact so a worker can't advance it while you're comparing.
    jobs.hold_artifact(conn, artifact_hash)

    manifest = {"stage": stage, "artifact_hash": artifact_hash, "variants": results}
    _write_report(settings, artifact_hash, stage, manifest)
    return manifest


def approve_eval(settings: Settings, conn: sqlite3.Connection, artifact_hash: str, stage: str, index: int) -> dict:
    """Pick variant `index`; mark the stage done with that output and resume the chain."""
    manifest = _read_manifest(settings, artifact_hash, stage)
    variants = manifest["variants"]
    if not 0 <= index < len(variants):
        raise ValueError(f"variant index {index} out of range (0..{len(variants) - 1})")
    chosen = variants[index]["intermediate"]

    job = jobs.get_job(conn, artifact_hash, stage)
    if job is None:
        raise ValueError(f"no job {artifact_hash[:12]}/{stage}")

    ctl.clear_control(conn, "artifact", artifact_hash)  # lift the eval hold
    jobs.mark_done(conn, artifact_hash, stage, chosen)
    nxt = stages.next_stage(job["source_type"], stage)
    if nxt is not None:
        jobs.insert_job(conn, artifact_hash, nxt, job["source_type"], input_path=chosen)
    return {"chosen_index": index, "chosen_intermediate": chosen, "next_stage": nxt}


def _eval_dir(settings: Settings, artifact_hash: str) -> Path:
    return settings.intermediate_dir / artifact_hash


def _read_manifest(settings: Settings, artifact_hash: str, stage: str) -> dict:
    path = _eval_dir(settings, artifact_hash) / f"eval.{stage}.json"
    if not path.exists():
        raise ValueError(f"no eval run found for {artifact_hash[:12]}/{stage} (run `eval run` first)")
    return json.loads(path.read_text())


def _write_report(settings: Settings, artifact_hash: str, stage: str, manifest: dict) -> None:
    d = _eval_dir(settings, artifact_hash)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"eval.{stage}.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (d / f"eval.{stage}.md").write_text(render_report(manifest))


def render_report(manifest: dict) -> str:
    """Human-readable side-by-side of the variants."""
    stage = manifest["stage"]
    lines = [f"# eval: {stage} — `{manifest['artifact_hash'][:12]}`", "", "| # | provider | model | outputs | tok in/out | usd | latency ms |", "|---|---|---|---|---|---|---|"]
    for v in manifest["variants"]:
        toks = f"{v.get('tokens_in')}/{v.get('tokens_out')}"
        usd = f"{v['usd']:.4f}" if isinstance(v.get("usd"), (int, float)) else "—"
        lines.append(
            f"| {v['index']} | {v.get('provider')} | {v.get('model')} | {v.get('num_outputs')} | {toks} | {usd} | {v.get('latency_ms')} |"
        )
    lines += ["", "Approve one with: `pipeline eval approve <ref> " + stage + " <#>`", ""]
    for v in manifest["variants"]:
        lines += [f"## [{v['index']}] {v.get('provider')}/{v.get('model')}  ({v.get('num_outputs')} outputs, {v.get('latency_ms')}ms)"]
        outputs = v.get("outputs") or []
        if not outputs:
            lines.append("_(no outputs — inspect the intermediate)_")
        for j, o in enumerate(outputs, 1):
            text = o.get("text", "") if isinstance(o, dict) else str(o)
            quote = o.get("quote", "") if isinstance(o, dict) else ""
            lines.append(f"{j}. {text}" + (f"\n   > {quote}" if quote else ""))
        lines += [f"\nintermediate: `{v['intermediate']}`", ""]
    return "\n".join(lines)
