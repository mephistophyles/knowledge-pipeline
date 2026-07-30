"""Retroactive corpus grooming: dedup claims already in the vault, losing nothing."""
from pipeline import corpus_dedup
from pipeline.db import claims_index as ci
from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator.executor import run_stage
from pipeline.vault.writer import read_note


def _walk(settings, conn, h):
    for stage in ("source_note", "extract_claims", "dedup"):
        run_stage(settings, conn, h, stage)


def _two_similar_claims(settings, conn, fake_claims):
    """Two sources asserting the same idea, committed with dedup effectively OFF."""
    settings.raw["dedup"]["max_distance"] = -1.0
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    fake_claims["text"] = '[{"claim": "Taste differentiates software.", "quote": "q A"}]'
    a = add_paste(settings, conn, "source A", source_url="https://a")
    _walk(settings, conn, a)
    fake_claims["text"] = '[{"claim": "Taste is what sets software apart.", "quote": "q B"}]'
    b = add_paste(settings, conn, "source B", source_url="https://b")
    _walk(settings, conn, b)
    return a, b


def test_groom_finds_merges_that_dedup_missed_while_off(settings, conn, fake_claims):
    a, b = _two_similar_claims(settings, conn, fake_claims)
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2  # no merge happened

    fake_claims["same"] = True
    plan = corpus_dedup.plan_merges(settings, conn, max_distance=0.72)
    assert len(plan.pairs) == 1


def test_groom_dry_run_writes_nothing(settings, conn, fake_claims):
    a, b = _two_similar_claims(settings, conn, fake_claims)
    fake_claims["same"] = True
    corpus_dedup.plan_merges(settings, conn, max_distance=0.72)

    assert (settings.vault_dir / f"corpus/claims/claim-{b[:8]}-00.md").exists()
    assert conn.execute("SELECT COUNT(*) FROM claims WHERE merged_into IS NOT NULL").fetchone()[0] == 0


def test_apply_preserves_the_absorbed_phrasing_and_note(settings, conn, fake_claims):
    """The whole point: a merge must never destroy how the other source put it."""
    a, b = _two_similar_claims(settings, conn, fake_claims)
    fake_claims["same"] = True
    plan = corpus_dedup.plan_merges(settings, conn, max_distance=0.72)
    assert corpus_dedup.apply_merges(settings, conn, plan) == 1

    survivor_id, absorbed_id, _ = plan.pairs[0]
    survivor = read_note(settings.vault_dir / f"corpus/claims/{survivor_id}.md")
    assert "Taste is what sets software apart." in survivor.metadata["alternate_phrasings"]
    assert "Alternate phrasings" in survivor.content
    assert len(survivor.metadata["attestations"]) == 2  # both sources' quotes survive

    # The absorbed note is kept, not deleted — the merge is reversible.
    assert not (settings.vault_dir / f"corpus/claims/{absorbed_id}.md").exists()
    moved = settings.vault_dir / f"{corpus_dedup.MERGED_DIR}/{absorbed_id}.md"
    assert moved.exists() and read_note(moved).metadata["merged_into"] == survivor_id


def test_merged_claims_stop_matching_and_survivor_stays_live(settings, conn, fake_claims):
    a, b = _two_similar_claims(settings, conn, fake_claims)
    fake_claims["same"] = True
    plan = corpus_dedup.plan_merges(settings, conn, max_distance=0.72)
    survivor_id, absorbed_id, _ = plan.pairs[0]
    corpus_dedup.apply_merges(settings, conn, plan)

    assert ci.get_claim(conn, absorbed_id)["merged_into"] == survivor_id
    assert ci.get_claim(conn, survivor_id)["merged_into"] is None  # survivor is untouched
    assert corpus_dedup.plan_merges(settings, conn, max_distance=0.72).pairs == []  # idempotent


def test_stored_vectors_are_reusable_so_grooming_never_re_embeds(settings, conn, fake_claims):
    a, _ = _two_similar_claims(settings, conn, fake_claims)
    vec = ci.get_vector(conn, f"claim-{a[:8]}-00")
    assert vec is not None and len(vec) == 8


def test_plan_round_trips_so_apply_need_not_re_run_the_pass(settings, conn, fake_claims, tmp_path):
    """Planning costs an hour and ~1.3k confirm calls on 1.2k claims; applying is
    instant. If the plan can't be saved, acting on a dry run means paying twice."""
    _two_similar_claims(settings, conn, fake_claims)
    fake_claims["same"] = True
    plan = corpus_dedup.plan_merges(settings, conn, max_distance=0.72)
    path = plan.save(tmp_path / "plan.json")

    reloaded = corpus_dedup.Plan.load(path)
    assert reloaded.pairs == plan.pairs and reloaded.confirms == plan.confirms
    assert corpus_dedup.apply_merges(settings, conn, reloaded) == 1
