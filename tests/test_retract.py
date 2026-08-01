"""Retract-then-rederive: claim ids encode extraction position, so re-derivation has to
withdraw the previous pass rather than write on top of it."""
import pytest

from pipeline import authors, corpus_dedup, retract
from pipeline.db import claims_index as ci
from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator.executor import run_stage
from pipeline.vault.writer import read_note


def _walk(settings, conn, h):
    for stage in ("source_note", "extract_claims", "dedup"):
        run_stage(settings, conn, h, stage)


def _source(settings, conn, fake_claims, body, claim, quote, url):
    fake_claims["text"] = f'[{{"claim": {claim!r}, "quote": {quote!r}}}]'.replace("'", '"')
    h = add_paste(settings, conn, body, source_url=url)
    _walk(settings, conn, h)
    return h


# ── (1) owned claims ──────────────────────────────────────────────────────────
def test_retract_removes_row_vector_and_note(settings, conn, fake_claims):
    h = _source(settings, conn, fake_claims, "source A", "Taste differentiates software.", "q", "https://a")
    claim_id = f"claim-{h[:8]}-00"
    assert (settings.vault_dir / f"corpus/claims/{claim_id}.md").exists()

    report = retract.retract(settings, conn, h)

    assert report.removed == [claim_id]
    assert not (settings.vault_dir / f"corpus/claims/{claim_id}.md").exists()
    assert ci.get_claim(conn, claim_id) is None
    assert ci.get_vector(conn, claim_id) is None


def test_retracted_claims_stop_matching_so_the_vector_cannot_resurface(settings, conn, fake_claims):
    """A stale vector would let a withdrawn claim be returned by KNN and attested to."""
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    h = _source(settings, conn, fake_claims, "source A", "Taste differentiates software.", "q", "https://a")
    retract.retract(settings, conn, h)
    assert ci.nearest(conn, [1, 0, 0, 0, 0, 0, 0, 0], 5) == []


# ── (2) attestations left on other artifacts' notes ───────────────────────────
def test_retract_detaches_attestations_it_left_elsewhere(settings, conn, fake_claims):
    """A withdrawn source must stop corroborating, or it keeps inflating support for a
    claim it no longer makes."""
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    for key, person in (("https://a", "Writer A"), ("https://b", "Writer B")):
        ident = f"person:{authors.slug(person)}"
        authors.upsert_identity(conn, ident, person)
        authors.add_alias(conn, key, ident, confidence="curated", source="test")
    a = _source(settings, conn, fake_claims, "source A", "Taste differentiates software.", "q A", "https://a")
    fake_claims["same"] = True
    b = _source(settings, conn, fake_claims, "source B", "Taste sets software apart.", "q B", "https://b")

    survivor = f"claim-{a[:8]}-00"
    assert ci.get_claim(conn, survivor)["attestations"] == 2

    report = retract.retract(settings, conn, b)

    assert report.detached == [survivor]
    post = read_note(settings.vault_dir / f"corpus/claims/{survivor}.md")
    assert [x["source_hash"] for x in post.metadata["attestations"]] == [a]
    assert "q B" not in post.content
    assert ci.get_claim(conn, survivor)["attestations"] == 1


# ── (3)/(4) merges the artifact took part in ──────────────────────────────────
def _merged_pair(settings, conn, fake_claims):
    settings.raw["dedup"]["max_distance"] = -1.0  # commit both, then groom
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    a = _source(settings, conn, fake_claims, "source A", "Taste differentiates software.", "q A", "https://a")
    b = _source(settings, conn, fake_claims, "source B", "Taste sets software apart.", "q B", "https://b")
    fake_claims["same"] = True
    plan = corpus_dedup.plan_merges(settings, conn, max_distance=0.72)
    corpus_dedup.apply_merges(settings, conn, plan)
    survivor_id, absorbed_id, _ = plan.pairs[0]
    return a, b, survivor_id, absorbed_id


def test_retracting_the_absorbed_side_unpicks_the_survivor(settings, conn, fake_claims):
    a, b, survivor_id, absorbed_id = _merged_pair(settings, conn, fake_claims)
    absorbed_owner = b if absorbed_id.startswith(f"claim-{b[:8]}") else a

    report = retract.retract(settings, conn, absorbed_owner)

    assert survivor_id in report.cleaned_survivors
    assert not (settings.vault_dir / f"corpus/claims/merged/{absorbed_id}.md").exists()
    post = read_note(settings.vault_dir / f"corpus/claims/{survivor_id}.md")
    assert not post.metadata.get("alternate_phrasings")  # the absorbed phrasing is gone
    assert [x["source_hash"] for x in post.metadata["attestations"]] != [absorbed_owner]
    assert ci.get_claim(conn, survivor_id)["attestations"] == 1


def test_retracting_the_survivor_restores_the_claim_it_absorbed(settings, conn, fake_claims):
    """Otherwise the absorbed note is orphaned under merged/, pointing at a survivor
    that no longer exists — exactly the residue the 2026-08-01 re-extraction left."""
    a, b, survivor_id, absorbed_id = _merged_pair(settings, conn, fake_claims)
    survivor_owner = a if survivor_id.startswith(f"claim-{a[:8]}") else b

    report = retract.retract(settings, conn, survivor_owner)

    assert report.unmerged == [absorbed_id]
    assert not (settings.vault_dir / f"corpus/claims/merged/{absorbed_id}.md").exists()
    restored = settings.vault_dir / f"corpus/claims/{absorbed_id}.md"
    assert restored.exists()
    assert "merged_into" not in read_note(restored).metadata
    assert ci.get_claim(conn, absorbed_id)["merged_into"] is None


def test_no_orphans_remain_after_retracting_either_side(settings, conn, fake_claims):
    """The invariant the residue violated: merged/ notes and merged rows must agree."""
    a, b, _, _ = _merged_pair(settings, conn, fake_claims)
    retract.retract(settings, conn, a)

    merged_dir = settings.vault_dir / "corpus/claims/merged"
    on_disk = len(list(merged_dir.glob("*.md"))) if merged_dir.exists() else 0
    rows = conn.execute("SELECT COUNT(*) FROM claims WHERE merged_into IS NOT NULL").fetchone()[0]
    assert on_disk == rows

    live_notes = len(list((settings.vault_dir / "corpus/claims").glob("*.md")))
    live_rows = conn.execute("SELECT COUNT(*) FROM claims WHERE merged_into IS NULL").fetchone()[0]
    assert live_notes == live_rows


# ── the guard ─────────────────────────────────────────────────────────────────
def test_dedup_refuses_to_commit_over_a_previous_pass(settings, conn, fake_claims):
    h = _source(settings, conn, fake_claims, "source A", "Taste differentiates software.", "q", "https://a")
    run_stage(settings, conn, h, "extract_claims")
    with pytest.raises(RuntimeError, match="retract"):
        run_stage(settings, conn, h, "dedup")


def test_retract_then_rederive_is_the_supported_path(settings, conn, fake_claims):
    h = _source(settings, conn, fake_claims, "source A", "Truncated run.", "q1", "https://a")
    retract.retract(settings, conn, h)

    fake_claims["text"] = (
        '[{"claim": "Truncated run.", "quote": "q1"},'
        ' {"claim": "The claim the cap cut off.", "quote": "q2"}]'
    )
    run_stage(settings, conn, h, "extract_claims")
    run_stage(settings, conn, h, "dedup")

    rows = conn.execute("SELECT claim_id FROM claims WHERE artifact_hash=?", (h,)).fetchall()
    assert sorted(r["claim_id"] for r in rows) == [f"claim-{h[:8]}-00", f"claim-{h[:8]}-01"]
    assert (settings.vault_dir / f"corpus/claims/claim-{h[:8]}-01.md").exists()


def test_retract_is_a_no_op_on_an_artifact_with_no_claims(settings, conn, fake_claims):
    h = add_paste(settings, conn, "never derived", source_url="https://x")
    assert not retract.retract(settings, conn, h)
