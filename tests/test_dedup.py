"""Dedup + attestations (plan §6.3), driven by a fake embed+confirm provider and a
real sqlite-vec index: a corroborating source attests to an existing claim rather
than duplicating it; a distinct claim gets its own note."""
from pipeline.db import claims_index as ci
from pipeline.ingestors.email import ingest_message
from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator.executor import run_stage
from pipeline.vault.writer import read_note

from .conftest import FakeMsg


def _walk(settings, conn, h):
    for stage in ("source_note", "extract_claims", "dedup"):
        run_stage(settings, conn, h, stage)


def _edition(settings, conn, fake_claims, *, author, body, claim, quote):
    """Ingest one authored newsletter edition and run it through the chain."""
    fake_claims["text"] = f'[{{"claim": {claim!r}, "quote": {quote!r}}}]'.replace("'", '"')
    h = ingest_message(settings, conn, FakeMsg(text=body, from_=author, subject=body[:20]))
    _walk(settings, conn, h)
    return h


# ── sqlite-vec claim index ────────────────────────────────────────────────────
def test_claims_index_nearest_orders_by_distance(conn):
    ci.add_claim(conn, "c1", "h1", "alpha", None, "m", [1.0, 0.0, 0.0])
    ci.add_claim(conn, "c2", "h2", "beta", None, "m", [0.0, 1.0, 0.0])
    res = ci.nearest(conn, [0.9, 0.1, 0.0], 2)
    assert [r["claim_id"] for r in res] == ["c1", "c2"]
    assert res[0]["distance"] < res[1]["distance"]


def test_claims_index_nearest_empty_before_any_insert(conn):
    assert ci.nearest(conn, [1.0, 0.0, 0.0], 5) == []


def test_claims_index_add_is_an_upsert(conn):
    """vec0 parses `INSERT OR REPLACE` but drops the conflict clause, so re-adding a
    claim_id used to raise instead of replacing. claim_ids are deterministic, so that
    made every re-derivation die in dedup."""
    ci.add_claim(conn, "c1", "h1", "alpha", None, "m", [1.0, 0.0, 0.0])
    ci.add_claim(conn, "c1", "h1", "alpha revised", None, "m", [0.0, 1.0, 0.0])

    res = ci.nearest(conn, [0.0, 1.0, 0.0], 5)
    assert [r["claim_id"] for r in res] == ["c1"]  # one row, not two
    assert res[0]["distance"] < 0.1  # holding the NEW vector


# ── attestation vs new note ───────────────────────────────────────────────────
def test_corroborating_source_attests_not_duplicates(settings, conn, fake_claims):
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]  # every claim embeds identically → near

    fake_claims["text"] = '[{"claim": "Taste differentiates software.", "quote": "taste A"}]'
    a = add_paste(settings, conn, "source A", source_url="https://a")
    _walk(settings, conn, a)

    fake_claims["text"] = '[{"claim": "Taste is what sets software apart.", "quote": "taste B"}]'
    fake_claims["same"] = True  # confirm: same claim
    b = add_paste(settings, conn, "source B", source_url="https://b")
    _walk(settings, conn, b)

    # B did not create its own note; A's note gained a second attestation.
    assert not (settings.vault_dir / f"corpus/claims/claim-{b[:8]}-00.md").exists()
    post = read_note(settings.vault_dir / f"corpus/claims/claim-{a[:8]}-00.md")
    assert len(post["attestations"]) == 2
    assert "taste B" in post.content  # the corroborating quote is appended
    row = conn.execute("SELECT attestations FROM claims WHERE claim_id=?", (f"claim-{a[:8]}-00",)).fetchone()
    assert row["attestations"] == 2


def test_distinct_claim_gets_new_note(settings, conn, fake_claims):
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]  # near, but confirm will say distinct
    fake_claims["same"] = False

    fake_claims["text"] = '[{"claim": "Alpha.", "quote": "a"}]'
    a = add_paste(settings, conn, "A")
    _walk(settings, conn, a)

    fake_claims["text"] = '[{"claim": "Beta.", "quote": "b"}]'
    b = add_paste(settings, conn, "B")
    _walk(settings, conn, b)

    assert (settings.vault_dir / f"corpus/claims/claim-{a[:8]}-00.md").exists()
    assert (settings.vault_dir / f"corpus/claims/claim-{b[:8]}-00.md").exists()


# ── corroboration is cross-author, not repetition ─────────────────────────────
def test_same_author_repeating_is_emphasis_not_corroboration(settings, conn, fake_claims):
    """One writer restating a point across editions must not inflate corroboration."""
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]  # every claim embeds identically → near
    fake_claims["same"] = True  # confirm: the same core assertion

    a = _edition(
        settings, conn, fake_claims, author="author@substack.com",
        body="Edition one.", claim="Taste differentiates software.", quote="taste 1",
    )
    _edition(
        settings, conn, fake_claims, author="author@substack.com",
        body="Edition two, same point again.", claim="Taste is what sets software apart.", quote="taste 2",
    )

    claim_id = f"claim-{a[:8]}-00"
    post = read_note(settings.vault_dir / f"corpus/claims/{claim_id}.md")
    assert len(post["attestations"]) == 1  # the repeat is dropped, not appended
    assert "taste 2" not in post.content
    row = conn.execute("SELECT attestations FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
    assert row["attestations"] == 1


def test_distinct_authors_corroborate(settings, conn, fake_claims):
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    fake_claims["same"] = True

    a = _edition(
        settings, conn, fake_claims, author="first@substack.com",
        body="First writer.", claim="Taste differentiates software.", quote="taste A",
    )
    _edition(
        settings, conn, fake_claims, author="second@example.com",
        body="Second writer, independently.", claim="Taste is what sets software apart.", quote="taste B",
    )

    claim_id = f"claim-{a[:8]}-00"
    post = read_note(settings.vault_dir / f"corpus/claims/{claim_id}.md")
    assert len(post["attestations"]) == 2
    assert {x["author"] for x in post["attestations"]} == {"first@substack.com", "second@example.com"}
    row = conn.execute("SELECT attestations FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
    assert row["attestations"] == 2


def test_author_identity_survives_display_name_and_plus_suffix(settings, conn, fake_claims):
    """`Name <Author+newsletter@Substack.com>` and `author@substack.com` are one author,
    so a provider that varies the From header can't fake corroboration."""
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    fake_claims["same"] = True

    a = _edition(
        settings, conn, fake_claims, author="author@substack.com",
        body="Plain header.", claim="Taste differentiates software.", quote="taste 1",
    )
    _edition(
        settings, conn, fake_claims, author="The Author <Author+weekly@Substack.com>",
        body="Decorated header, same person.", claim="Taste is what sets software apart.", quote="taste 2",
    )

    post = read_note(settings.vault_dir / f"corpus/claims/claim-{a[:8]}-00.md")
    assert len(post["attestations"]) == 1


def test_reprocessing_an_artifact_that_already_produced_claims(settings, conn, fake_claims):
    """Re-deriving is routine — a raised max_tokens, a new prompt version, a model swap.
    The second pass re-uses the same deterministic claim_ids, which used to collide in
    the vec index and abort dedup after extract_claims had already been paid for."""
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    fake_claims["text"] = '[{"claim": "Truncated run.", "quote": "q1"}]'
    h = add_paste(settings, conn, "an edition whose first extraction hit the output cap")
    _walk(settings, conn, h)

    # Second pass: a fuller extraction from the same source, same claim ids.
    fake_claims["text"] = (
        '[{"claim": "Truncated run.", "quote": "q1"},'
        ' {"claim": "The claim the cap cut off.", "quote": "q2"}]'
    )
    fake_claims["same"] = False
    run_stage(settings, conn, h, "extract_claims")
    run_stage(settings, conn, h, "dedup")

    assert (settings.vault_dir / f"corpus/claims/claim-{h[:8]}-01.md").exists()
    rows = conn.execute("SELECT claim_id FROM claims WHERE artifact_hash=?", (h,)).fetchall()
    assert sorted(r["claim_id"] for r in rows) == [f"claim-{h[:8]}-00", f"claim-{h[:8]}-01"]


def test_dedup_records_embed_cost(settings, conn, fake_claims):
    fake_claims["text"] = '[{"claim": "One claim.", "quote": "q"}]'
    h = add_paste(settings, conn, "text")
    _walk(settings, conn, h)
    row = conn.execute("SELECT provider, latency_ms FROM costs WHERE stage='dedup:embed'").fetchone()
    assert row is not None and row["provider"] == "fake"
