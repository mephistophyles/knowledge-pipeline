"""Read-only dashboard: query helpers over the jobs/costs tables, plus a health
smoke. Helpers take a connection so they test without the Settings/disk plumbing."""
from fastapi.testclient import TestClient

from dashboard import app as dash
from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator.executor import run_stage


def test_health():
    assert TestClient(dash.app).get("/health").json() == {"status": "ok"}


def test_artifacts_reports_frontier_and_progress(settings, conn):
    h = add_paste(settings, conn, "hello world")
    run_stage(settings, conn, h, "source_note")  # source_note done, extract_claims ready

    arts = dash.artifacts(conn)
    assert len(arts) == 1
    a = arts[0]
    assert a["artifact_hash"] == h
    assert (a["done"], a["total"]) == (1, 2)
    assert a["active_stage"] == "extract_claims"
    assert a["active_status"] == "ready"


def test_status_counts_and_stage_matrix(settings, conn):
    h = add_paste(settings, conn, "x")
    run_stage(settings, conn, h, "source_note")

    counts = dash.status_counts(conn)
    assert counts.get("done") == 1 and counts.get("ready") == 1

    matrix = dash.stage_matrix(conn)
    assert matrix["source_note"]["done"] == 1
    assert matrix["extract_claims"]["ready"] == 1


def test_artifact_detail_is_chain_ordered(settings, conn):
    h = add_paste(settings, conn, "x")
    run_stage(settings, conn, h, "source_note")

    detail = dash.artifact_detail(conn, h)
    assert [s["stage"] for s in detail["stages"]] == ["source_note", "extract_claims"]
    assert detail["stages"][0]["status"] == "done"


def test_render_overview_smoke(settings, conn):
    h = add_paste(settings, conn, "x")
    run_stage(settings, conn, h, "source_note")
    out = dash._render_overview(
        dash.status_counts(conn), dash.stage_matrix(conn), dash.cost_summary(conn), dash.artifacts(conn)
    )
    assert h[:12] in out and "extract_claims" in out
