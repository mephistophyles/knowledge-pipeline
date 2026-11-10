"""Stable claim identity: IDs are assigned once, and re-derivation keeps them by matching.

A claim ID used to encode the claim's POSITION in its extraction run (`claim-<src8>-NN`),
so re-deriving a source reassigned IDs. A link to `-05` stayed valid and silently pointed
at a different idea. That was tolerable while only the pipeline referenced claims; it is
not once decisions cite claims (`informed_by`). Content-hash IDs would fail the other way:
every rewording by a new model is a new identity.

So identity is ASSIGNED, not computed:

- New claims get the next ID from a counter (`c-000001`) and that ID is never reused,
  even after retraction. Legacy `claim-<src8>-NN` IDs are already unique and keep working;
  nothing is renamed.
- Re-deriving a source that already has claims runs a reconciliation (`Rederivation`)
  instead of refusing:
    * the new extraction is matched against the source's OWN prior claims, planned over
      the whole pass and assigned one-to-one. The verbatim QUOTE is the primary signal:
      a re-extraction rewords the claim but cites the same span of the source, so a
      near-identical quote within the distance threshold keeps the ID with no LLM call.
      The rest fall back to embedding distance + the confirm model. (Measured on a real
      re-run: the strict cross-source confirm prompt rejected 3 of 6 true re-extractions,
      all with identical quotes.) A match KEEPS the ID:
      the note takes the new wording, the old wording moves to `alternate_phrasings`, and
      every other source's attestation stays where it is;
    * unmatched candidates go through normal corpus dedup (attest, or a new ID);
    * an unmatched prior claim that OTHER sources still attest stays live and changes
      owner (`transferred`). The withdrawn source stops asserting it; the others still do;
    * an unmatched prior claim nobody else attests is RETIRED. Its note moves to
      `corpus/claims/retracted/` with `status: retracted`, so links to it still resolve,
      and the row and vector are removed so it can never match again;
    * attestations this source left on other claims that the new pass did not re-assert
      are detached, so a source stops corroborating what it no longer says.

The cost of this design is that identity becomes state: which ID a claim carries depends
on history, not only on raw + generating key. Every decision is recorded in
`claim_history`, which is the audit trail for the one real risk: a false "same" verdict
keeps an ID on a claim whose meaning drifted. `pipeline claim-history --drift` reads it back.
"""
from __future__ import annotations

import difflib
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from pipeline import retract as _retract
from pipeline.db import claims_index, vec_index
from pipeline.vault.writer import read_note

LIVE_DIR = "corpus/claims"
MERGED_DIR = "corpus/claims/merged"
RETRACTED_DIR = "corpus/claims/retracted"


# ── minting + ledger ──────────────────────────────────────────────────────────
def mint_claim_id(conn: sqlite3.Connection) -> str:
    """Return the next claim ID. Never reused: the counter only moves forward, so a
    retracted or deleted claim's ID cannot come back as a different idea."""
    conn.execute("INSERT INTO id_counters(name, next) VALUES('claim', 1) ON CONFLICT(name) DO NOTHING")
    while True:
        n = conn.execute(
            "UPDATE id_counters SET next = next + 1 WHERE name='claim' RETURNING next - 1"
        ).fetchone()[0]
        claim_id = f"c-{n:06d}"
        # Belt and braces: never hand out an ID that exists in any form.
        if claims_index.get_claim(conn, claim_id) is None and not conn.execute(
            "SELECT 1 FROM claim_history WHERE claim_id=? LIMIT 1", (claim_id,)
        ).fetchone():
            return claim_id


def record(
    conn: sqlite3.Connection,
    claim_id: str,
    event: str,
    *,
    artifact_hash: str | None = None,
    old_text: str | None = None,
    new_text: str | None = None,
    distance: float | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO claim_history(claim_id, artifact_hash, event, old_text, new_text, distance, detail) "
        "VALUES(?,?,?,?,?,?,?)",
        (claim_id, artifact_hash, event, old_text, new_text, distance, detail),
    )


def history(conn: sqlite3.Connection, *, claim_id: str | None = None,
            event: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    sql, params = "SELECT * FROM claim_history WHERE 1=1", []
    if claim_id:
        sql += " AND claim_id=?"
        params.append(claim_id)
    if event:
        sql += " AND event=?"
        params.append(event)
    return conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()


# ── helpers ───────────────────────────────────────────────────────────────────
def _l2(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _voices(conn: sqlite3.Connection, atts: list[dict]) -> int:
    from pipeline.corpus_dedup import _voice  # lazy: corpus_dedup imports the handlers

    return max(1, len({v for v in (_voice(conn, a) for a in atts) if v}))


QUOTE_MATCH = 0.9  # quote similarity at which a same-source pair is the same claim


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def quote_similarity(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class _Prior:
    claim_id: str
    text: str
    merged_into: str | None
    vector: list[float] | None
    quote: str = ""


@dataclass
class Candidate:
    index: int
    text: str
    quote: str
    vector: list[float]


@dataclass
class RederiveReport:
    kept: list[str] = field(default_factory=list)
    transferred: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    detached: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{len(self.kept)} kept, {len(self.transferred)} transferred, "
                f"{len(self.retired)} retired, {len(self.detached)} detached")


# ── re-derivation ─────────────────────────────────────────────────────────────
class Rederivation:
    """Reconciles one source's new extraction against the claims its last pass committed.

    Usage (from the dedup committer): `match_own()` for each candidate before corpus
    dedup, corpus dedup for the rest while skipping `prior_ids`, then `finish()`.
    """

    def __init__(self, ctx, *, max_distance: float, shortlist_k: int):
        self.ctx = ctx
        self.conn = ctx.conn
        self.vault = ctx.vault
        self.artifact = ctx.artifact_hash
        self.max_distance = max_distance
        self.shortlist_k = shortlist_k
        rows = self.conn.execute(
            "SELECT claim_id, text, merged_into FROM claims WHERE artifact_hash=?", (self.artifact,)
        ).fetchall()
        self.priors = {
            r["claim_id"]: _Prior(r["claim_id"], r["text"], r["merged_into"],
                                  claims_index.get_vector(self.conn, r["claim_id"]),
                                  self._own_quote(r["claim_id"], r["merged_into"]))
            for r in rows
        }
        self.matched: set[str] = set()
        self.report = RederiveReport()
        # Other sources' claims this source attested last time. Whatever the new pass does
        # not re-assert is detached in finish().
        self.attested_before = self._attested_elsewhere()

    @property
    def prior_ids(self) -> set[str]:
        return set(self.priors)

    def __bool__(self) -> bool:
        # A source whose previous pass only attested OTHER claims (owns none) still has a
        # footprint to reconcile.
        return bool(self.priors or self.attested_before)

    def plan(self, candidates: list[Candidate], confirm: Callable[[str, str], bool]) -> dict[int, tuple[str, float, str]]:
        """Assign new candidates to prior claims, one-to-one: {candidate index: (prior ID,
        distance, method)}. Pass 1 matches by quote over all pairs, best first; pass 2 asks
        the confirm model about what is left, nearest first."""
        live = [p for p in self.priors.values() if p.vector is not None]
        out: dict[int, tuple[str, float, str]] = {}
        taken: set[str] = set()

        scored = []
        for c in candidates:
            for p in live:
                d = _l2(c.vector, p.vector)
                if d > self.max_distance:
                    continue
                qs = quote_similarity(c.quote, p.quote)
                if qs >= QUOTE_MATCH:
                    scored.append((-qs, d, c.index, p.claim_id))
        for _neg_qs, d, idx, pid in sorted(scored):
            if idx in out or pid in taken:
                continue
            out[idx] = (pid, d, "quote")
            taken.add(pid)

        for c in candidates:
            if c.index in out:
                continue
            shortlist = sorted(
                ((_l2(c.vector, p.vector), p) for p in live if p.claim_id not in taken),
                key=lambda x: x[0],
            )
            for d, p in shortlist[: self.shortlist_k]:
                if d > self.max_distance:
                    break
                if confirm(p.text, c.text):
                    out[c.index] = (p.claim_id, d, "confirm")
                    taken.add(p.claim_id)
                    break
        return out

    def keep(self, prior_id: str, text: str, vec, attestation: dict, model, dist: float,
             method: str) -> None:
        """Commit one planned match: the prior claim keeps its ID with the new wording."""
        prior = self.priors[prior_id]
        self.matched.add(prior_id)
        if prior.merged_into:
            # Grooming already folded this claim into a survivor that carries our
            # attestation; the idea is still asserted, so nothing moves.
            record(self.conn, prior_id, "kept", artifact_hash=self.artifact, old_text=prior.text,
                   new_text=text, distance=dist, detail=f"{method}; merged-away")
        else:
            self._keep(prior, text, vec, attestation, model, dist, method)
        self.report.kept.append(prior_id)

    def _keep(self, prior: _Prior, text: str, vec, attestation: dict, model, dist: float,
              method: str) -> None:
        path = f"{LIVE_DIR}/{prior.claim_id}.md"
        post = read_note(self.vault.root / path)
        meta = dict(post.metadata)
        old = _retract._headline(post)
        if text.strip() != old:
            alts = [a for a in (meta.get("alternate_phrasings") or []) if a != text.strip()]
            if old and old not in alts:
                alts.append(old)
            meta["alternate_phrasings"] = alts
        # Replace this source's attestation with the fresh one; everyone else's stays.
        others = [a for a in (meta.get("attestations") or []) if a.get("source_hash") != self.artifact]
        meta["attestations"] = [attestation] + others
        meta["model"] = model
        self.vault.write_note(path, meta, _retract._rebuild_body(text.strip(), meta))
        self.conn.execute(
            "UPDATE claims SET text=?, model=?, attestations=? WHERE claim_id=?",
            (text.strip(), model, _voices(self.conn, meta["attestations"]), prior.claim_id),
        )
        vec_index.add(self.conn, "claims_vec", prior.claim_id, vec)
        record(self.conn, prior.claim_id, "kept", artifact_hash=self.artifact,
               old_text=old, new_text=text.strip(), distance=dist, detail=method)

    def finish(self, reasserted: set[str]) -> RederiveReport:
        """Settle every prior claim the new pass did not match, then detach attestations
        on other sources' claims that this pass did not re-assert."""
        for prior in self.priors.values():
            if prior.claim_id in self.matched:
                continue
            if prior.merged_into:
                self._retire_merged(prior)
            else:
                self._settle_live(prior)
        self._detach(self.attested_before - reasserted)
        return self.report

    def _settle_live(self, prior: _Prior) -> None:
        path = self.vault.root / f"{LIVE_DIR}/{prior.claim_id}.md"
        post = read_note(path)
        meta = dict(post.metadata)
        others = [a for a in (meta.get("attestations") or []) if a.get("source_hash") != self.artifact]
        if others:
            # Still asserted by other sources: stay live, change owner.
            meta["attestations"] = others
            meta["origin_withdrawn"] = self.artifact
            new_owner = others[0].get("source_hash")
            self.vault.write_note(f"{LIVE_DIR}/{prior.claim_id}.md", meta,
                                  _retract._rebuild_body(_retract._headline(post), meta))
            self.conn.execute(
                "UPDATE claims SET artifact_hash=?, attestations=? WHERE claim_id=?",
                (new_owner, _voices(self.conn, others), prior.claim_id),
            )
            record(self.conn, prior.claim_id, "transferred", artifact_hash=self.artifact,
                   old_text=prior.text, detail=f"owner -> {new_owner}")
            self.report.transferred.append(prior.claim_id)
            return
        # Nobody else asserts it. Any claims it absorbed (case 4 in retract) go back to live.
        for absorbed in _retract._absorbed_by(self.conn, prior.claim_id):
            if absorbed in self.priors:
                continue
            merged = self.vault.root / f"{MERGED_DIR}/{absorbed}.md"
            if merged.exists():
                ap = read_note(merged)
                am = {k: v for k, v in ap.metadata.items() if k != "merged_into"}
                self.vault.write_note(f"{LIVE_DIR}/{absorbed}.md", am,
                                      _retract._rebuild_body(_retract._headline(ap), am))
                merged.unlink()
            self.conn.execute("UPDATE claims SET merged_into=NULL WHERE claim_id=?", (absorbed,))
        self._tombstone(path, post, prior)

    def _retire_merged(self, prior: _Prior) -> None:
        # Unpick our phrasing and attestation from the survivor, as retract does.
        rep = _retract.RetractReport()
        _retract._clean_survivor(self.vault, self.conn, prior.merged_into, prior.claim_id,
                                 self.artifact, rep)
        path = self.vault.root / f"{MERGED_DIR}/{prior.claim_id}.md"
        post = read_note(path) if path.exists() else None
        self._tombstone(path, post, prior)

    def _tombstone(self, path, post, prior: _Prior) -> None:
        if post is not None:
            meta = dict(post.metadata)
            meta.pop("merged_into", None)
            meta["status"] = "retracted"
            meta["retracted_at"] = date.today().isoformat()
            meta["retracted_by"] = self.artifact
            body = _retract._rebuild_body(_retract._headline(post), meta)
            body += ("\n> [!warning] Retracted\n> The source that asserted this claim no longer "
                     "does after re-derivation, and no other source attests it.\n")
            self.vault.write_note(f"{RETRACTED_DIR}/{prior.claim_id}.md", meta, body)
            if path.exists():
                path.unlink()
        self.conn.execute("DELETE FROM claims WHERE claim_id=?", (prior.claim_id,))
        vec_index.remove(self.conn, "claims_vec", prior.claim_id)
        record(self.conn, prior.claim_id, "retired", artifact_hash=self.artifact, old_text=prior.text)
        self.report.retired.append(prior.claim_id)

    def _own_quote(self, claim_id: str, merged_into: str | None) -> str:
        folder = MERGED_DIR if merged_into else LIVE_DIR
        path = self.vault.root / f"{folder}/{claim_id}.md"
        if not path.exists():
            return ""
        for a in read_note(path).metadata.get("attestations") or []:
            if a.get("source_hash") == self.artifact:
                return a.get("quote") or ""
        return ""

    def _attested_elsewhere(self) -> set[str]:
        out: set[str] = set()
        for row in self.conn.execute(
            "SELECT claim_id FROM claims WHERE artifact_hash <> ? AND merged_into IS NULL",
            (self.artifact,),
        ).fetchall():
            path = self.vault.root / f"{LIVE_DIR}/{row['claim_id']}.md"
            if path.exists() and any(a.get("source_hash") == self.artifact
                                     for a in read_note(path).metadata.get("attestations") or []):
                out.add(row["claim_id"])
        return out

    def _detach(self, claim_ids: set[str]) -> None:
        for claim_id in sorted(claim_ids):
            path = self.vault.root / f"{LIVE_DIR}/{claim_id}.md"
            if not path.exists():
                continue
            post = read_note(path)
            meta = dict(post.metadata)
            kept = [a for a in (meta.get("attestations") or []) if a.get("source_hash") != self.artifact]
            if not kept or len(kept) == len(meta.get("attestations") or []):
                continue  # never strip a note to zero support
            meta["attestations"] = kept
            self.vault.write_note(f"{LIVE_DIR}/{claim_id}.md", meta,
                                  _retract._rebuild_body(_retract._headline(post), meta))
            self.conn.execute("UPDATE claims SET attestations=? WHERE claim_id=?",
                              (_voices(self.conn, kept), claim_id))
            record(self.conn, claim_id, "detached", artifact_hash=self.artifact)
            self.report.detached.append(claim_id)
