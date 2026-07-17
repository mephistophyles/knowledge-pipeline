"""Integration coverage for the derivation chain via a fake provider (no network):
extract_claims produces a keyed intermediate + a cost/latency row; dedup
materializes claim notes that carry the generating key."""
import json
from pathlib import Path

from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator.executor import run_stage
from pipeline.vault.writer import read_note


def _walk(settings, conn, h, *stages):
    for stage in stages:
        run_stage(settings, conn, h, stage)


def test_extract_claims_produces_keyed_intermediate_and_cost(settings, conn, fake_claims):
    h = add_paste(settings, conn, "Taste is the differentiator.")
    run_stage(settings, conn, h, "source_note")
    outcome = run_stage(settings, conn, h, "extract_claims")

    # Producer wrote a gen-key-keyed intermediate, no vault note yet.
    assert outcome.output_path and Path(outcome.output_path).name.startswith("extract_claims.")
    payload = json.loads(Path(outcome.output_path).read_text())
    assert payload["claims"][0]["text"] == "Taste is the differentiator."
    assert payload["gen_key"]["provider"] == "fake"
    assert payload["gen_key"]["input_hash"] == h
    assert not (settings.vault_dir / f"corpus/claims/claim-{h[:8]}-00.md").exists()

    # Cost/latency ledger row recorded for the call.
    row = conn.execute(
        "SELECT provider, model, tokens_in, latency_ms FROM costs WHERE stage='extract_claims'"
    ).fetchone()
    assert row["provider"] == "fake"
    assert row["model"] == "fake-1"
    assert row["latency_ms"] == 42
    assert row["tokens_in"] == 11


def test_dedup_writes_claim_note_with_generating_key(settings, conn, fake_claims):
    fake_claims["text"] = '[{"claim": "Quality compounds.", "quote": "Quality compounds over time."}]'
    h = add_paste(settings, conn, "Quality compounds over time.", source_url="https://ex")
    _walk(settings, conn, h, "source_note", "extract_claims", "dedup")

    note_path = settings.vault_dir / f"corpus/claims/claim-{h[:8]}-00.md"
    assert note_path.exists()
    post = read_note(note_path)
    assert post["type"] == "claim"
    assert post["authority"] == "derived"
    # The generating key rides in the frontmatter — the basis for promote/regen.
    assert post["provider"] == "fake"
    assert post["model"] == "fake-1"
    assert post["prompt_version"] == "v1"
    assert post["source_hash"] == h
    assert "Quality compounds." in post.content
    assert "Quality compounds over time." in post.content


def test_dedup_handles_zero_claims(settings, conn, fake_claims):
    fake_claims["text"] = "[]"
    h = add_paste(settings, conn, "Just a greeting, nothing checkable.")
    _walk(settings, conn, h, "source_note", "extract_claims", "dedup")

    dedup_out = json.loads((settings.intermediate_dir / h / "dedup.json").read_text())
    assert dedup_out["count"] == 0
    assert not list((settings.vault_dir / "corpus/claims").glob("claim-*.md"))
