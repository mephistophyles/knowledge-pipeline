"""Withdraw one artifact's claims from the corpus, so it can be re-derived cleanly.

Claim ids encode the claim's INDEX in its extraction run (`claim-<src8>-NN`), so they
are not stable across re-derivation: raise a token cap or change a prompt and `-05`
becomes a different claim. Writing the new pass on top of the old one therefore
reassigns ids — silently rewriting notes that other sources had attested to, orphaning
the tail when an extraction yields fewer claims, and clearing `merged_into` on claims
grooming had merged away.

The fix is to make re-derivation a two-step: retract, then derive. Retract is the exact
inverse of what `dedup` commits, which means undoing four things:

  1. claims this artifact OWNS — row, vector, and live note;
  2. attestations it left on OTHER artifacts' notes (and their bumped counts);
  3. owned claims that grooming MERGED AWAY — drop the `merged/` note and unpick the
     survivor's copy of the phrasing and attestation;
  4. owned claims that ABSORBED someone else's — un-merge first, so the absorbed claim
     returns to live rather than being orphaned by its survivor's deletion.

Note bodies are generated from frontmatter, so affected notes are rebuilt canonically
rather than patched by string surgery.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from pipeline.config import Settings
from pipeline.db import claims_index, vec_index
from pipeline.vault.writer import VaultWriter, read_note

MERGED_DIR = "corpus/claims/merged"
_LIVE_DIR = "corpus/claims"


@dataclass
class RetractReport:
    removed: list[str] = field(default_factory=list)          # claims dropped outright
    unmerged: list[str] = field(default_factory=list)         # others' claims restored to live
    detached: list[str] = field(default_factory=list)         # others' notes we un-attested
    cleaned_survivors: list[str] = field(default_factory=list)  # survivors we unpicked

    def __bool__(self) -> bool:
        return bool(self.removed or self.unmerged or self.detached or self.cleaned_survivors)

    def summary(self) -> str:
        return (
            f"{len(self.removed)} claim(s) removed, {len(self.unmerged)} un-merged, "
            f"{len(self.detached)} attestation(s) detached, "
            f"{len(self.cleaned_survivors)} survivor(s) cleaned"
        )


def _attestation_line(a: dict) -> str:
    who = a.get("author") or a.get("source_url") or "—"
    return f"- **{who}** `{a['source_hash'][:12]}` — \"{a.get('quote', '')}\" [{a.get('date', '')}]"


def _rebuild_body(headline: str, meta: dict) -> str:
    """Regenerate a claim note's body from its frontmatter.

    Every section is generated in the first place (`_claim_body` plus appends), so a
    rebuild is lossless and avoids editing prose we did not write.
    """
    body = f"# {headline}\n\n## Attestations\n\n"
    for a in meta.get("attestations") or []:
        body += _attestation_line(a) + "\n"
    alts = meta.get("alternate_phrasings") or []
    if alts:
        body += "\n## Alternate phrasings\n\n"
        for alt in alts:
            body += f"- {alt}\n"
    return body


def _headline(post) -> str:
    first = (post.content.strip().splitlines() or [""])[0]
    return first.lstrip("#").strip()


def _absorbed_by(conn: sqlite3.Connection, claim_id: str) -> list[str]:
    return [
        r["claim_id"]
        for r in conn.execute("SELECT claim_id FROM claims WHERE merged_into=?", (claim_id,))
    ]


def retract(settings: Settings, conn: sqlite3.Connection, artifact_hash: str) -> RetractReport:
    """Remove every trace of `artifact_hash`'s claims from the DB, index, and vault."""
    vault = VaultWriter(settings.vault_dir)
    report = RetractReport()

    owned = conn.execute(
        "SELECT claim_id, merged_into FROM claims WHERE artifact_hash=?", (artifact_hash,)
    ).fetchall()
    owned_ids = {r["claim_id"] for r in owned}

    for row in owned:
        claim_id, merged_into = row["claim_id"], row["merged_into"]

        # (4) Our claim absorbed others: restore them to live before we delete it, or
        # they are orphaned under merged/ pointing at a survivor that no longer exists.
        for absorbed_id in _absorbed_by(conn, claim_id):
            if absorbed_id in owned_ids:
                continue  # ours too — it goes away with the rest
            merged_path = vault.root / f"{MERGED_DIR}/{absorbed_id}.md"
            if merged_path.exists():
                post = read_note(merged_path)
                meta = {k: v for k, v in post.metadata.items() if k != "merged_into"}
                vault.write_note(f"{_LIVE_DIR}/{absorbed_id}.md", meta, _rebuild_body(_headline(post), meta))
                merged_path.unlink()
            conn.execute("UPDATE claims SET merged_into=NULL WHERE claim_id=?", (absorbed_id,))
            report.unmerged.append(absorbed_id)

        # (3) Grooming merged OUR claim into someone else's: unpick the survivor's copy.
        if merged_into and merged_into not in owned_ids:
            _clean_survivor(vault, conn, merged_into, claim_id, artifact_hash, report)

        for path in (vault.root / f"{_LIVE_DIR}/{claim_id}.md", vault.root / f"{MERGED_DIR}/{claim_id}.md"):
            if path.exists():
                path.unlink()

        # (1) Row and vector.
        conn.execute("DELETE FROM claims WHERE claim_id=?", (claim_id,))
        vec_index.remove(conn, "claims_vec", claim_id)
        report.removed.append(claim_id)

    # (2) Attestations left on other artifacts' notes.
    _detach_attestations(vault, conn, artifact_hash, owned_ids, report)

    conn.commit()
    if report:
        vault.commit(f"[retract] {artifact_hash[:12]}: {report.summary()}")
    return report


def _clean_survivor(
    vault: VaultWriter,
    conn: sqlite3.Connection,
    survivor_id: str,
    absorbed_id: str,
    artifact_hash: str,
    report: RetractReport,
) -> None:
    """Undo one merge on the surviving note: drop the absorbed phrasing, the attestation
    it contributed, and the `merged_from` entry."""
    path = vault.root / f"{_LIVE_DIR}/{survivor_id}.md"
    if not path.exists():
        return
    post = read_note(path)
    meta = dict(post.metadata)
    absorbed_path = vault.root / f"{MERGED_DIR}/{absorbed_id}.md"
    absorbed_headline = _headline(read_note(absorbed_path)) if absorbed_path.exists() else None

    if absorbed_headline:
        meta["alternate_phrasings"] = [
            a for a in (meta.get("alternate_phrasings") or []) if a != absorbed_headline
        ]
    before = list(meta.get("attestations") or [])
    meta["attestations"] = [a for a in before if a.get("source_hash") != artifact_hash]
    meta["merged_from"] = [m for m in (meta.get("merged_from") or []) if m != absorbed_id]
    for empty_key in ("alternate_phrasings", "merged_from"):
        if not meta.get(empty_key):
            meta.pop(empty_key, None)

    dropped = len(before) - len(meta["attestations"])
    vault.write_note(f"{_LIVE_DIR}/{survivor_id}.md", meta, _rebuild_body(_headline(post), meta))
    if dropped:
        conn.execute(
            "UPDATE claims SET attestations=max(1, attestations-?) WHERE claim_id=?",
            (dropped, survivor_id),
        )
    report.cleaned_survivors.append(survivor_id)


def _detach_attestations(
    vault: VaultWriter,
    conn: sqlite3.Connection,
    artifact_hash: str,
    owned_ids: set[str],
    report: RetractReport,
) -> None:
    """Strip this artifact's attestations from claims owned by OTHER artifacts.

    A retracted source must stop corroborating; leaving the attestation behind would
    keep inflating a claim's support with a source that no longer says it.
    """
    for row in conn.execute(
        "SELECT claim_id FROM claims WHERE artifact_hash <> ? AND merged_into IS NULL",
        (artifact_hash,),
    ).fetchall():
        claim_id = row["claim_id"]
        if claim_id in owned_ids:
            continue
        path = vault.root / f"{_LIVE_DIR}/{claim_id}.md"
        if not path.exists():
            continue
        post = read_note(path)
        meta = dict(post.metadata)
        before = list(meta.get("attestations") or [])
        kept = [a for a in before if a.get("source_hash") != artifact_hash]
        if len(kept) == len(before):
            continue
        if not kept:
            continue  # never strip a note down to zero support; it would read as unsourced
        meta["attestations"] = kept
        vault.write_note(f"{_LIVE_DIR}/{claim_id}.md", meta, _rebuild_body(_headline(post), meta))
        conn.execute(
            "UPDATE claims SET attestations=max(1, attestations-?) WHERE claim_id=?",
            (len(before) - len(kept), claim_id),
        )
        report.detached.append(claim_id)
