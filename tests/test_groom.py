"""Retroactive corpus grooming: dedup claims already in the vault, losing nothing."""
from pipeline import corpus_dedup
from pipeline.db import claims_index as ci
from pipeline.ingestors.email import ingest_message
from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator.executor import run_stage
from pipeline.vault.writer import read_note

from .conftest import FakeMsg


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


def test_live_dedup_attests_to_the_survivor_of_an_earlier_merge(settings, conn, fake_claims):
    """A groomed-away claim keeps its vector, so a later edition's KNN can still match
    it — and its note has moved to `merged/`. Attesting there used to crash the stage
    with FileNotFoundError after extract_claims had already been paid for."""
    a, b = _two_similar_claims(settings, conn, fake_claims)
    fake_claims["same"] = True
    plan = corpus_dedup.plan_merges(settings, conn, max_distance=0.72)
    survivor_id, absorbed_id, _ = plan.pairs[0]
    corpus_dedup.apply_merges(settings, conn, plan)

    # A third source asserts the same idea, with dedup back on.
    settings.raw["dedup"]["max_distance"] = 0.72
    fake_claims["text"] = '[{"claim": "Software is set apart by taste.", "quote": "q C"}]'
    c = add_paste(settings, conn, "source C", source_url="https://c")
    _walk(settings, conn, c)

    assert not (settings.vault_dir / f"corpus/claims/claim-{c[:8]}-00.md").exists()  # no new note
    survivor = read_note(settings.vault_dir / f"corpus/claims/{survivor_id}.md")
    assert "q C" in survivor.content  # the attestation landed on the survivor
    assert ci.get_claim(conn, absorbed_id)["merged_into"] == survivor_id  # merge untouched


def test_resolve_live_follows_a_chain_and_refuses_to_spin_on_a_cycle(conn):
    for cid in ("c1", "c2", "c3"):
        ci.add_claim(conn, cid, "h", cid, None, "m", [1.0, 0.0, 0.0])
    conn.execute("UPDATE claims SET merged_into='c2' WHERE claim_id='c1'")
    conn.execute("UPDATE claims SET merged_into='c3' WHERE claim_id='c2'")
    assert ci.resolve_live(conn, "c1") == "c3"
    assert ci.resolve_live(conn, "c3") == "c3"

    conn.execute("UPDATE claims SET merged_into='c1' WHERE claim_id='c3'")
    assert ci.resolve_live(conn, "c1") in {"c1", "c2", "c3"}  # terminates, doesn't hang


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


def test_grooming_does_not_let_one_writer_corroborate_themselves(settings, conn, fake_claims):
    """A merge copies the absorbed claim's attestations onto the survivor. Keying that
    copy on (author, source_hash) meant the SAME writer's other edition counted as new
    support — within-author repetition reintroduced through the grooming path, which is
    exactly what author-aware attestation exists to suppress."""
    settings.raw["dedup"]["max_distance"] = -1.0
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]

    fake_claims["text"] = '[{"claim": "Taste differentiates software.", "quote": "q1"}]'
    a = ingest_message(settings, conn, FakeMsg(text="edition one", from_="solo@example.com", subject="A"))
    _walk(settings, conn, a)
    fake_claims["text"] = '[{"claim": "Taste sets software apart.", "quote": "q2"}]'
    b = ingest_message(settings, conn, FakeMsg(text="edition two", from_="solo@example.com", subject="B"))
    _walk(settings, conn, b)

    fake_claims["same"] = True
    plan = corpus_dedup.plan_merges(settings, conn, max_distance=0.72)
    corpus_dedup.apply_merges(settings, conn, plan)
    survivor_id, _, _ = plan.pairs[0]

    assert ci.get_claim(conn, survivor_id)["attestations"] == 1  # one voice, not two
    post = read_note(settings.vault_dir / f"corpus/claims/{survivor_id}.md")
    assert len(post.metadata["attestations"]) == 2  # both editions kept as provenance


def test_recount_corrects_a_corpus_inflated_by_earlier_grooming(settings, conn, fake_claims):
    settings.raw["dedup"]["max_distance"] = -1.0
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    fake_claims["text"] = '[{"claim": "Taste differentiates software.", "quote": "q1"}]'
    a = ingest_message(settings, conn, FakeMsg(text="one", from_="solo@example.com", subject="A"))
    _walk(settings, conn, a)
    fake_claims["text"] = '[{"claim": "Taste sets software apart.", "quote": "q2"}]'
    b = ingest_message(settings, conn, FakeMsg(text="two", from_="solo@example.com", subject="B"))
    _walk(settings, conn, b)
    fake_claims["same"] = True
    plan = corpus_dedup.plan_merges(settings, conn, max_distance=0.72)
    corpus_dedup.apply_merges(settings, conn, plan)
    survivor_id, _, _ = plan.pairs[0]

    conn.execute("UPDATE claims SET attestations=2 WHERE claim_id=?", (survivor_id,))  # the old bug
    changed = corpus_dedup.recount_attestations(settings, conn)

    assert (survivor_id, 2, 1) in changed
    assert ci.get_claim(conn, survivor_id)["attestations"] == 1


def test_recount_dry_run_writes_nothing(settings, conn, fake_claims):
    """The dry run must not commit. An earlier version computed, committed, and only then
    tried to roll back — so `pipeline recount` with no --apply silently wrote."""
    settings.raw["dedup"]["max_distance"] = -1.0
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    fake_claims["text"] = '[{"claim": "One claim.", "quote": "q"}]'
    h = ingest_message(settings, conn, FakeMsg(text="ed", from_="solo@example.com", subject="A"))
    _walk(settings, conn, h)
    claim_id = f"claim-{h[:8]}-00"
    conn.execute("UPDATE claims SET attestations=7 WHERE claim_id=?", (claim_id,))

    changed = corpus_dedup.recount_attestations(settings, conn, dry_run=True)

    assert (claim_id, 7, 1) in changed                              # reported
    assert ci.get_claim(conn, claim_id)["attestations"] == 7        # but not written
