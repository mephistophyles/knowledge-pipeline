"""Eval-compare (plan §6.5): running a producer stage under several variants holds
the artifact with N keyed intermediates; approving one resumes the chain from that
output without recompute."""
import json

import pytest

from pipeline import eval_compare
from pipeline.db import jobs
from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator.executor import run_stage


def _ready_extract(settings, conn):
    h = add_paste(settings, conn, "Taste matters and quality compounds.")
    run_stage(settings, conn, h, "source_note")  # extract_claims becomes ready
    return h


def test_eval_run_produces_variants_and_holds(settings, conn, fake_claims):
    h = _ready_extract(settings, conn)
    manifest = eval_compare.run_eval(settings, conn, h, "extract_claims")

    assert len(manifest["variants"]) == 2
    assert len({v["intermediate"] for v in manifest["variants"]}) == 2  # distinct keyed intermediates
    assert all(v["num_outputs"] >= 1 for v in manifest["variants"])
    assert all(v["latency_ms"] == 42 for v in manifest["variants"])  # from the fake

    # Artifact is frozen for review; the chain has NOT advanced.
    assert jobs.get_job(conn, h, "extract_claims")["status"] == "held"
    assert jobs.get_job(conn, h, "dedup") is None
    assert (settings.intermediate_dir / h / "eval.extract_claims.json").exists()
    assert (settings.intermediate_dir / h / "eval.extract_claims.md").exists()


def test_eval_approve_resumes_chain_from_chosen(settings, conn, fake_claims):
    h = _ready_extract(settings, conn)
    eval_compare.run_eval(settings, conn, h, "extract_claims")
    result = eval_compare.approve_eval(settings, conn, h, "extract_claims", 1)

    variants = json.loads((settings.intermediate_dir / h / "eval.extract_claims.json").read_text())["variants"]
    chosen = variants[1]["intermediate"]

    job = jobs.get_job(conn, h, "extract_claims")
    assert job["status"] == "done"
    assert job["output_path"] == chosen

    dedup = jobs.get_job(conn, h, "dedup")
    assert dedup is not None and dedup["status"] == "ready"
    assert dedup["input_path"] == chosen  # committer consumes the approved output
    assert result["next_stage"] == "dedup"


def test_eval_report_inlines_actual_outputs(settings, conn, fake_claims):
    fake_claims["text"] = '[{"claim": "Distribution beats product.", "quote": "distribution wins"}]'
    h = _ready_extract(settings, conn)
    manifest = eval_compare.run_eval(settings, conn, h, "extract_claims")

    # Judge by outputs, not counts: the claim text (+ quote) is inlined in the report.
    report = eval_compare.render_report(manifest)
    assert "Distribution beats product." in report
    assert "distribution wins" in report
    assert manifest["variants"][0]["outputs"][0]["text"] == "Distribution beats product."


def test_eval_rejects_committer_stage(settings, conn, fake_claims):
    h = _ready_extract(settings, conn)
    with pytest.raises(ValueError):
        eval_compare.run_eval(settings, conn, h, "dedup")  # commits — not eval-able


def test_eval_approve_bad_index(settings, conn, fake_claims):
    h = _ready_extract(settings, conn)
    eval_compare.run_eval(settings, conn, h, "extract_claims")
    with pytest.raises(ValueError):
        eval_compare.approve_eval(settings, conn, h, "extract_claims", 5)
