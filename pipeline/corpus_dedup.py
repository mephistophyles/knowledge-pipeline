"""Retroactive corpus grooming — dedup claims ALREADY in the vault.

The per-source `dedup` stage only sees one source's candidates against the corpus as
it stood at the time. Anything committed while dedup was off, or under a looser
threshold, never gets a second look. This is the pass that gives it one.

Nothing is deleted. A merge:
  - records the absorbed claim's wording on the survivor (`alternate_phrasings`),
  - copies its attestations across,
  - marks the absorbed note `merged_into:` and MOVES it to `corpus/claims/merged/`,
  - sets `claims.merged_into` so it stops matching, keeping the row and its vector.

So a merge is reversible and auditable, which is what makes running it on a corpus you
care about a reasonable thing to do. `--dry-run` reports the pairs and spends nothing
beyond confirm calls.
"""
from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.config import Settings
from pipeline.db import claims_index, costs
from pipeline.llm import Message, prompts, registry
from pipeline.orchestrator.handlers import _parse_same
from pipeline.vault import VaultWriter
from pipeline.vault.writer import read_note

MERGED_DIR = "corpus/claims/merged"


@dataclass
class Plan:
    pairs: list[tuple[str, str, float]] = field(default_factory=list)  # (survivor, absorbed, distance)
    confirms: int = 0
    examined: int = 0

    def save(self, path: str | Path) -> Path:
        """Persist so review and apply don't re-run the pass.

        Planning is the expensive half (an hour and ~1.3k confirm calls on 1.2k
        claims); applying is instant. Without this, every dry run you actually want
        to act on has to be paid for twice — which quietly pressures you into
        applying blind, the exact thing the dry run exists to prevent.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"examined": self.examined, "confirms": self.confirms,
             "pairs": [[s, a, d] for s, a, d in self.pairs]}, indent=1))
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Plan":
        raw = json.loads(Path(path).read_text())
        return cls(
            pairs=[(s, a, float(d)) for s, a, d in raw["pairs"]],
            confirms=raw.get("confirms", 0), examined=raw.get("examined", 0),
        )


def _live_claims(conn: sqlite3.Connection, batch_author: str | None) -> list[sqlite3.Row]:
    sql = (
        "SELECT c.claim_id, c.text, c.artifact_hash, c.created_at FROM claims c "
        "WHERE c.merged_into IS NULL"
    )
    params: list = []
    if batch_author:
        sql += (
            " AND c.artifact_hash IN (SELECT artifact_hash FROM backlog WHERE author=?"
            " AND artifact_hash IS NOT NULL)"
        )
        params.append(batch_author)
    # Oldest first: the earliest phrasing survives, so provenance points at the source
    # that said it first rather than whichever happened to be processed last.
    return conn.execute(sql + " ORDER BY c.created_at, c.claim_id", params).fetchall()


def plan_merges(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    max_distance: float | None = None,
    author: str | None = None,
    progress=None,
) -> Plan:
    """Find claims that should merge. Read-only: no vault or index writes."""
    dcfg = settings.dedup_config
    threshold = dcfg["max_distance"] if max_distance is None else max_distance
    confirm_mc = settings.stage_model("dedup")
    provider = registry.get_provider(settings, confirm_mc.provider)
    prompt = prompts.load_prompt(settings, "dedup_confirm", confirm_mc.prompt_version)

    plan = Plan()
    absorbed: set[str] = set()
    for row in _live_claims(conn, author):
        plan.examined += 1
        if row["claim_id"] in absorbed:
            continue
        vec = claims_index.get_vector(conn, row["claim_id"])
        if vec is None:
            continue
        for m in claims_index.nearest(conn, vec, dcfg["shortlist_k"] + 1):
            if m["claim_id"] == row["claim_id"] or m["claim_id"] in absorbed:
                continue
            if m["distance"] > threshold:
                break  # ascending — nothing closer remains
            if conn.execute(
                "SELECT merged_into FROM claims WHERE claim_id=?", (m["claim_id"],)
            ).fetchone()["merged_into"]:
                continue
            verdict = provider.complete(
                [Message("system", prompt), Message("user", f"A: {m['text']}\nB: {row['text']}")],
                confirm_mc.model, confirm_mc.params,
            )
            plan.confirms += 1
            costs.record(
                conn, row["artifact_hash"] or "", "corpus_dedup:confirm", verdict.model,
                verdict.tokens_in, verdict.tokens_out, verdict.usd,
                provider=verdict.provider, latency_ms=verdict.latency_ms,
            )
            if _parse_same(verdict.text):
                # The neighbour is older (it was indexed first), so it survives.
                plan.pairs.append((m["claim_id"], row["claim_id"], m["distance"]))
                absorbed.add(row["claim_id"])
                break
        if progress and plan.examined % 50 == 0:
            progress(plan)
    return plan


def apply_merges(settings: Settings, conn: sqlite3.Connection, plan: Plan) -> int:
    """Apply a plan. Additive and reversible — nothing is deleted."""
    vault = VaultWriter(settings.vault_dir)
    applied = 0
    for survivor_id, absorbed_id, _dist in plan.pairs:
        s_path = vault.root / f"corpus/claims/{survivor_id}.md"
        a_path = vault.root / f"corpus/claims/{absorbed_id}.md"
        if not s_path.exists() or not a_path.exists():
            continue
        survivor, absorbed_note = read_note(s_path), read_note(a_path)

        s_meta = dict(survivor.metadata)
        headline = (absorbed_note.content.strip().splitlines() or [""])[0].lstrip("# ").strip()
        alts = list(s_meta.get("alternate_phrasings") or [])
        if headline and headline not in alts:
            alts.append(headline)
        s_meta["alternate_phrasings"] = alts

        s_atts = list(s_meta.get("attestations") or [])
        known = {(a.get("author"), a.get("source_hash")) for a in s_atts}
        added = [a for a in (absorbed_note.metadata.get("attestations") or [])
                 if (a.get("author"), a.get("source_hash")) not in known]
        s_meta["attestations"] = s_atts + added
        s_meta.setdefault("merged_from", []).append(absorbed_id)

        body = survivor.content.rstrip()
        if headline:
            body += f"\n\n## Alternate phrasings\n\n- {headline}\n"
        for a in added:
            body += f"- **{a.get('author') or '—'}** `{(a.get('source_hash') or '')[:12]}` — \"{a.get('quote','')}\"\n"
        vault.write_note(f"corpus/claims/{survivor_id}.md", s_meta, body)

        a_meta = dict(absorbed_note.metadata)
        a_meta["merged_into"] = survivor_id
        vault.write_note(f"{MERGED_DIR}/{absorbed_id}.md", a_meta, absorbed_note.content)
        a_path.unlink()  # the note lives on under merged/ — and the vault is a git repo

        # The survivor stays live and only gains corroboration; ONLY the absorbed row
        # is marked, which is what stops it matching again.
        conn.execute(
            "UPDATE claims SET attestations=attestations+? WHERE claim_id=?", (len(added), survivor_id)
        )
        conn.execute("UPDATE claims SET merged_into=? WHERE claim_id=?", (survivor_id, absorbed_id))
        applied += 1
    conn.commit()
    vault.commit(f"[groom] merged {applied} duplicate claim(s)")
    return applied
