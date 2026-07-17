"""Dedup + attestations (plan §6.3), driven by a fake embed+confirm provider and a
real sqlite-vec index: a corroborating source attests to an existing claim rather
than duplicating it; a distinct claim gets its own note."""
from pipeline.db import claims_index as ci
from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator.executor import run_stage
from pipeline.vault.writer import read_note


def _walk(settings, conn, h):
    for stage in ("source_note", "extract_claims", "dedup"):
        run_stage(settings, conn, h, stage)


# ── sqlite-vec claim index ────────────────────────────────────────────────────
def test_claims_index_nearest_orders_by_distance(conn):
    ci.add_claim(conn, "c1", "h1", "alpha", None, "m", [1.0, 0.0, 0.0])
    ci.add_claim(conn, "c2", "h2", "beta", None, "m", [0.0, 1.0, 0.0])
    res = ci.nearest(conn, [0.9, 0.1, 0.0], 2)
    assert [r["claim_id"] for r in res] == ["c1", "c2"]
    assert res[0]["distance"] < res[1]["distance"]


def test_claims_index_nearest_empty_before_any_insert(conn):
    assert ci.nearest(conn, [1.0, 0.0, 0.0], 5) == []


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


def test_dedup_records_embed_cost(settings, conn, fake_claims):
    fake_claims["text"] = '[{"claim": "One claim.", "quote": "q"}]'
    h = add_paste(settings, conn, "text")
    _walk(settings, conn, h)
    row = conn.execute("SELECT provider, latency_ms FROM costs WHERE stage='dedup:embed'").fetchone()
    assert row is not None and row["provider"] == "fake"
