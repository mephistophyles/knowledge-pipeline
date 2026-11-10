"""Stable claim identity: IDs are minted once and kept across re-derivation by matching."""
from pipeline import authors, claim_identity, retract
from pipeline.db import claims_index as ci
from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator.executor import run_stage
from pipeline.vault.writer import read_note

from .conftest import cid

V = [1, 0, 0, 0, 0, 0, 0, 0]


def _claims(fake_claims, *claims, quotes=None):
    quotes = quotes or [f"the source says: {c}" for c in claims]
    fake_claims["text"] = "[" + ",".join(
        f'{{"claim": "{c}", "quote": "{q}"}}' for c, q in zip(claims, quotes)
    ) + "]"


def _derive(settings, conn, h):
    for stage in ("source_note", "extract_claims", "dedup"):
        run_stage(settings, conn, h, stage)


def _rederive(settings, conn, h):
    run_stage(settings, conn, h, "extract_claims")
    run_stage(settings, conn, h, "dedup")


def _curate(conn, key, person):
    ident = f"person:{authors.slug(person)}"
    authors.upsert_identity(conn, ident, person)
    authors.add_alias(conn, key, ident, confidence="curated", source="test")


def _note(settings, claim_id, folder="corpus/claims"):
    return read_note(settings.vault_dir / f"{folder}/{claim_id}.md")


def _reconciles(settings, conn):
    live_notes = len(list((settings.vault_dir / "corpus/claims").glob("*.md")))
    live_rows = conn.execute("SELECT COUNT(*) FROM claims WHERE merged_into IS NULL").fetchone()[0]
    assert live_notes == live_rows


# ── minting ───────────────────────────────────────────────────────────────────
def test_new_claims_get_minted_sequential_ids(settings, conn, fake_claims):
    _claims(fake_claims, "First idea.", "Second idea.")
    h = add_paste(settings, conn, "source A", source_url="https://a")
    _derive(settings, conn, h)
    assert [cid(conn, h, 0), cid(conn, h, 1)] == ["c-000001", "c-000002"]
    assert claim_identity.history(conn, event="minted")


def test_ids_are_never_reused_after_retraction(settings, conn, fake_claims):
    _claims(fake_claims, "First idea.")
    a = add_paste(settings, conn, "source A", source_url="https://a")
    _derive(settings, conn, a)
    retract.retract(settings, conn, a)
    _claims(fake_claims, "Unrelated idea.")
    b = add_paste(settings, conn, "source B", source_url="https://b")
    _derive(settings, conn, b)
    assert cid(conn, b, 0) == "c-000002"  # c-000001 is gone for good


# ── keep ──────────────────────────────────────────────────────────────────────
def test_rederiving_a_reworded_claim_keeps_its_id(settings, conn, fake_claims):
    fake_claims["vector"] = V
    _claims(fake_claims, "Taste differentiates software.")
    h = add_paste(settings, conn, "source A", source_url="https://a")
    _derive(settings, conn, h)
    original = cid(conn, h, 0)

    fake_claims["same"] = True
    _claims(fake_claims, "Taste is what sets software apart.")
    _rederive(settings, conn, h)

    assert cid(conn, h, 0) == original
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
    note = _note(settings, original)
    assert note.content.startswith("# Taste is what sets software apart.")
    assert "Taste differentiates software." in note.metadata["alternate_phrasings"]
    assert ci.get_claim(conn, original)["text"] == "Taste is what sets software apart."
    kept = claim_identity.history(conn, claim_id=original, event="kept")
    assert kept and kept[0]["old_text"] == "Taste differentiates software."
    _reconciles(settings, conn)


def test_keeping_an_id_preserves_other_sources_corroboration(settings, conn, fake_claims):
    """The case retract-then-rederive lost: B's attestation lives on A's note."""
    fake_claims["vector"] = V
    _curate(conn, "https://a", "Writer A")
    _curate(conn, "https://b", "Writer B")
    _claims(fake_claims, "Taste differentiates software.")
    a = add_paste(settings, conn, "source A", source_url="https://a")
    _derive(settings, conn, a)
    fake_claims["same"] = True
    b = add_paste(settings, conn, "source B", source_url="https://b")
    _derive(settings, conn, b)
    claim = cid(conn, a, 0)
    assert ci.get_claim(conn, claim)["attestations"] == 2

    _claims(fake_claims, "Taste is what sets software apart.")
    _rederive(settings, conn, a)

    assert ci.get_claim(conn, claim)["attestations"] == 2
    hashes = {x["source_hash"] for x in _note(settings, claim).metadata["attestations"]}
    assert hashes == {a, b}


# ── transfer / retire / detach ────────────────────────────────────────────────
def test_unmatched_claim_still_attested_by_others_transfers_ownership(settings, conn, fake_claims):
    fake_claims["vector"] = V
    _curate(conn, "https://a", "Writer A")
    _curate(conn, "https://b", "Writer B")
    _claims(fake_claims, "Taste differentiates software.")
    a = add_paste(settings, conn, "source A", source_url="https://a")
    _derive(settings, conn, a)
    fake_claims["same"] = True
    b = add_paste(settings, conn, "source B", source_url="https://b")
    _derive(settings, conn, b)
    claim = cid(conn, a, 0)

    fake_claims["same"] = False
    fake_claims["vector"] = None  # per-text vectors: the new claim is unrelated
    _claims(fake_claims, "Distribution beats product.")
    _rederive(settings, conn, a)

    row = ci.get_claim(conn, claim)
    assert row is not None and row["artifact_hash"] == b and row["attestations"] == 1
    note = _note(settings, claim)
    assert [x["source_hash"] for x in note.metadata["attestations"]] == [b]
    assert note.metadata["origin_withdrawn"] == a
    assert claim_identity.history(conn, claim_id=claim, event="transferred")
    _reconciles(settings, conn)


def test_unmatched_claim_nobody_else_attests_is_retired_not_deleted(settings, conn, fake_claims):
    _claims(fake_claims, "Taste differentiates software.")
    h = add_paste(settings, conn, "source A", source_url="https://a")
    _derive(settings, conn, h)
    old = cid(conn, h, 0)

    _claims(fake_claims, "Distribution beats product.")
    _rederive(settings, conn, h)

    assert ci.get_claim(conn, old) is None and ci.get_vector(conn, old) is None
    assert not (settings.vault_dir / f"corpus/claims/{old}.md").exists()
    tomb = _note(settings, old, "corpus/claims/retracted")
    assert tomb.metadata["status"] == "retracted"
    assert cid(conn, h, 0) not in (old, None) and cid(conn, h, 0).startswith("c-")
    _reconciles(settings, conn)


def test_attestations_not_reasserted_are_detached(settings, conn, fake_claims):
    fake_claims["vector"] = V
    _curate(conn, "https://a", "Writer A")
    _curate(conn, "https://b", "Writer B")
    _claims(fake_claims, "Taste differentiates software.")
    b = add_paste(settings, conn, "source B", source_url="https://b")
    _derive(settings, conn, b)
    fake_claims["same"] = True
    a = add_paste(settings, conn, "source A", source_url="https://a")
    _derive(settings, conn, a)  # A attests B's claim
    claim = cid(conn, b, 0)
    assert ci.get_claim(conn, claim)["attestations"] == 2

    fake_claims["same"] = False
    fake_claims["vector"] = None
    _claims(fake_claims, "Distribution beats product.")
    _rederive(settings, conn, a)

    assert ci.get_claim(conn, claim)["attestations"] == 1
    assert [x["source_hash"] for x in _note(settings, claim).metadata["attestations"]] == [b]


def test_reasserted_attestations_stay(settings, conn, fake_claims):
    fake_claims["vector"] = V
    _curate(conn, "https://a", "Writer A")
    _curate(conn, "https://b", "Writer B")
    _claims(fake_claims, "Taste differentiates software.")
    b = add_paste(settings, conn, "source B", source_url="https://b")
    _derive(settings, conn, b)
    fake_claims["same"] = True
    a = add_paste(settings, conn, "source A", source_url="https://a")
    _derive(settings, conn, a)
    _rederive(settings, conn, a)  # same claim again
    assert ci.get_claim(conn, cid(conn, b, 0))["attestations"] == 2


def test_rederive_with_no_prior_claims_is_a_plain_derive(settings, conn, fake_claims):
    _claims(fake_claims, "First idea.")
    h = add_paste(settings, conn, "source A", source_url="https://a")
    _derive(settings, conn, h)
    assert not claim_identity.history(conn, event="kept")


# ── quote-first matching ─────────────────────────────────────────────────────
def test_same_quote_keeps_the_id_even_when_confirm_says_distinct(settings, conn, fake_claims):
    """The real-run failure: the strict cross-source confirm prompt rejects a reworded
    re-extraction, but both cite the identical span of the source."""
    fake_claims["vector"] = V
    q = "And just as good systems compound over time, so do bad systems."
    _claims(fake_claims, "Both effective and ineffective systems compound.", quotes=[q])
    h = add_paste(settings, conn, "source A", source_url="https://a")
    _derive(settings, conn, h)
    original = cid(conn, h, 0)

    fake_claims["same"] = False
    _claims(fake_claims, "Systems compound over time, good or bad.", quotes=[q])
    _rederive(settings, conn, h)

    assert cid(conn, h, 0) == original
    assert claim_identity.history(conn, claim_id=original, event="kept")[0]["detail"] == "quote"


def test_one_prior_is_kept_by_at_most_one_candidate(settings, conn, fake_claims):
    fake_claims["vector"] = V
    q = "One passage."
    _claims(fake_claims, "Idea from the passage.", quotes=[q])
    h = add_paste(settings, conn, "source A", source_url="https://a")
    _derive(settings, conn, h)
    original = cid(conn, h, 0)

    fake_claims["same"] = False
    _claims(fake_claims, "First idea from the passage.", "Second idea from it.", quotes=[q, q])
    _rederive(settings, conn, h)

    rows = conn.execute("SELECT claim_id FROM claims WHERE artifact_hash=?", (h,)).fetchall()
    ids = {r["claim_id"] for r in rows}
    assert original in ids and len(ids) == 2  # one kept, one minted
    assert len(claim_identity.history(conn, event="kept")) == 1


def test_quote_similarity():
    assert claim_identity.quote_similarity("While goals a great", "While goals are great") > 0.9
    assert claim_identity.quote_similarity("abc", "") == 0.0
    assert claim_identity.quote_similarity("Every time I made progress", "Keep playing the game") < 0.5
