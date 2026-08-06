"""Compare embedding models by the only thing that matters: the merges they cause.

An embedder is not judged on its own — it is judged through the threshold that sits on top
of it. `max_distance` was tuned to 0.72 for nomic over 1,868 judged pairs, and L2 distances
are not comparable across models, so a swap invalidates the number rather than inheriting
it. Answering "is nemotron better" therefore means re-deriving its threshold first and
comparing each model AT ITS OWN best setting.

The expensive half is already paid for. `dedup_verdicts` caches same/distinct judgments
keyed by (prompt, model, claim_a, claim_b) — a judgment about two pieces of TEXT, which is
independent of whichever embedder proposed the pair. So a new embedder only pays for the
pairs it surfaces that the old one never did.

Candidates come from a real vec0 index, the same mechanism dedup uses in production, so
the comparison reflects the pipeline rather than an idealised brute-force search.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

from pipeline.config import Settings
from pipeline.db import claims_index, vec_index
from pipeline.llm import Message, prompts, registry
from pipeline.orchestrator.handlers import _parse_same


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


@dataclass
class SweepPoint:
    threshold: float
    pairs: int = 0            # candidate pairs within the threshold
    same: int = 0             # judged the same claim
    distinct: int = 0
    new_calls: int = 0        # verdicts not already cached

    @property
    def precision(self) -> float:
        """Share of proposed pairs the judge accepts.

        A proxy, not ground truth: it says how much of what the embedder surfaced was
        worth surfacing. Recall needs a labelled set, which we do not have — so this
        number ranks embedders, it does not score them.
        """
        return self.same / self.pairs if self.pairs else 0.0


@dataclass
class SweepResult:
    model: str
    table: str
    dims: int
    claims: int
    points: list[SweepPoint] = field(default_factory=list)
    merged_at: dict[float, set[tuple[str, str]]] = field(default_factory=dict)


def embed_corpus(
    settings: Settings, conn: sqlite3.Connection, *, provider_name: str, model: str,
    table: str, batch: int = 32, progress=None,
) -> tuple[int, int]:
    """Embed every live claim into `table`. Returns (claims, dims).

    Writes to a SEPARATE vec table so the production index is untouched — an evaluation
    that mutates the thing it is evaluating cannot be re-run.
    """
    rows = conn.execute(
        "SELECT claim_id, text FROM claims WHERE merged_into IS NULL ORDER BY claim_id"
    ).fetchall()
    provider = registry.get_provider(settings, provider_name)
    dims = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        emb = provider.embed([r["text"] for r in chunk], model)
        for row, vec in zip(chunk, emb.vectors):
            v = _normalize(vec)
            dims = len(v)
            vec_index.add(conn, table, row["claim_id"], v)
        conn.commit()
        if progress:
            progress(min(i + batch, len(rows)), len(rows))
    return len(rows), dims


def _verdict(
    settings: Settings, conn: sqlite3.Connection, a_text: str, b_text: str, mc, prompt: str
) -> tuple[bool, bool]:
    """(same, was_new). Reuses the cache the threshold sweeps already populated."""
    hit = conn.execute(
        "SELECT same FROM dedup_verdicts WHERE prompt_version=? AND model=? "
        "AND claim_a=? AND claim_b=?",
        (mc.prompt_version, mc.model, a_text, b_text),
    ).fetchone()
    if hit is not None:
        return bool(hit["same"]), False
    provider = registry.get_provider(settings, mc.provider)
    out = provider.complete(
        [Message("system", prompt), Message("user", f"A: {a_text}\nB: {b_text}")],
        mc.model, mc.params,
    )
    same = _parse_same(out.text)
    conn.execute(
        "INSERT OR REPLACE INTO dedup_verdicts"
        "(prompt_version, model, claim_a, claim_b, same, distance) VALUES(?,?,?,?,?,NULL)",
        (mc.prompt_version, mc.model, a_text, b_text, int(same)),
    )
    conn.commit()
    return same, True


def sweep(
    settings: Settings, conn: sqlite3.Connection, *, table: str, model: str,
    thresholds: list[float], shortlist_k: int = 5, progress=None,
) -> SweepResult:
    """Candidate pairs and judgments at each threshold, over `table`'s vectors."""
    mc = settings.stage_model("dedup")
    prompt = prompts.load_prompt(settings, "dedup_confirm", mc.prompt_version)
    rows = conn.execute(
        "SELECT claim_id, text FROM claims WHERE merged_into IS NULL ORDER BY claim_id"
    ).fetchall()
    texts = {r["claim_id"]: r["text"] for r in rows}
    result = SweepResult(model=model, table=table, dims=0, claims=len(rows))

    # One pass over the index collects every neighbour within the widest threshold; the
    # narrower ones are subsets, so each pair is judged at most once across the sweep.
    widest = max(thresholds)
    candidates: list[tuple[str, str, float]] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        vec = vec_index.get_vector(conn, table, r["claim_id"])
        if vec is None:
            continue
        result.dims = len(vec)
        for hit in vec_index.nearest(conn, table, vec, shortlist_k + 1):
            other = hit["item_id"]
            if other == r["claim_id"] or other not in texts:
                continue
            if hit["distance"] > widest:
                break
            key = tuple(sorted((r["claim_id"], other)))
            if key in seen:
                continue
            seen.add(key)
            candidates.append((key[0], key[1], hit["distance"]))

    judged: dict[tuple[str, str], bool] = {}
    new_calls = 0
    for i, (a, b, dist) in enumerate(candidates):
        same, was_new = _verdict(settings, conn, texts[a], texts[b], mc, prompt)
        judged[(a, b)] = same
        new_calls += int(was_new)
        if progress:
            progress(i + 1, len(candidates), new_calls)

    for t in sorted(thresholds):
        pt = SweepPoint(threshold=t)
        merged: set[tuple[str, str]] = set()
        for a, b, dist in candidates:
            if dist > t:
                continue
            pt.pairs += 1
            if judged[(a, b)]:
                pt.same += 1
                merged.add((a, b))
            else:
                pt.distinct += 1
        result.points.append(pt)
        result.merged_at[t] = merged
    result.points[-1].new_calls = new_calls
    return result


def agreement(a: SweepResult, ta: float, b: SweepResult, tb: float) -> dict:
    """How two embedders compare at their OWN chosen thresholds."""
    ma, mb = a.merged_at.get(ta, set()), b.merged_at.get(tb, set())
    both = ma & mb
    return {
        "a_only": len(ma - mb), "b_only": len(mb - ma), "both": len(both),
        "jaccard": len(both) / len(ma | mb) if (ma or mb) else 1.0,
    }
