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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from pipeline import batch_guard
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
    stopped_early: bool = False
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


def sweep(
    settings: Settings, conn: sqlite3.Connection, *, table: str, model: str,
    thresholds: list[float], shortlist_k: int = 5, workers: int = 8, progress=None,
    on_estimate=None, confirm=None,
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

    # Judged in PARALLEL. Confirm calls are independent of one another, so running them
    # serially made this an 8-hour job at ~15 verdicts/min; `warm_cache` already
    # established the pattern at ~10x. Every DB write stays on this thread — the sqlite
    # connection is not shared across threads.
    cached: dict[tuple[str, str], bool] = {}
    todo: list[tuple[str, str]] = []
    for a, b, _dist in candidates:
        hit = conn.execute(
            "SELECT same FROM dedup_verdicts WHERE prompt_version=? AND model=? "
            "AND claim_a=? AND claim_b=?",
            (mc.prompt_version, mc.model, texts[a], texts[b]),
        ).fetchone()
        if hit is None:
            todo.append((a, b))
        else:
            cached[(a, b)] = bool(hit["same"])

    provider = registry.get_provider(settings, mc.provider)
    judged: dict[tuple[str, str], bool] = dict(cached)
    new_calls = 0

    def _judge(pair):
        a, b = pair
        out = provider.complete(
            [Message("system", prompt), Message("user", f"A: {texts[a]}\nB: {texts[b]}")],
            mc.model, mc.params,
        )
        return pair, _parse_same(out.text)

    if todo:
        est = batch_guard.estimate(
            len(todo), rate_key="dedup_confirm_parallel", usd_per_call=0.0002,
            label=f"sweep/{model}",
        )
        if on_estimate:
            on_estimate(est)
        if confirm and not confirm(est):
            return result
        batch_id = f"sweep:{model}"
        batch_guard.start(conn, batch_id, f"sweep/{model}", len(todo),
                          rate_key="dedup_confirm_parallel")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_judge, p) for p in todo]
            for fut in as_completed(futures):
                # Wind up cleanly when asked. Cancelling the pending futures rather than
                # draining them is what makes a closed laptop cost seconds, not the run.
                if batch_guard.should_stop(settings.root, conn):
                    for f2 in futures:
                        f2.cancel()
                    result.stopped_early = True
                    break
                try:
                    pair, same = fut.result()
                except Exception:
                    continue          # a failed judgment is simply not counted
                judged[pair] = same
                conn.execute(
                    "INSERT OR REPLACE INTO dedup_verdicts"
                    "(prompt_version, model, claim_a, claim_b, same, distance) "
                    "VALUES(?,?,?,?,?,NULL)",
                    (mc.prompt_version, mc.model, texts[pair[0]], texts[pair[1]], int(same)),
                )
                new_calls += 1
                if new_calls % 25 == 0:
                    conn.commit()
                    batch_guard.tick(conn, batch_id, new_calls,
                                     usd=new_calls * 0.0002)
                if progress:
                    progress(len(cached) + new_calls, len(candidates), new_calls)
        conn.commit()
        batch_guard.finish(conn, batch_id, "stopped" if result.stopped_early else "done")

    for t in sorted(thresholds):
        pt = SweepPoint(threshold=t)
        merged: set[tuple[str, str]] = set()
        for a, b, dist in candidates:
            if dist > t:
                continue
            if (a, b) not in judged:
                continue          # judgment failed; not counted either way
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
