"""Read-only dashboard: query helpers over the jobs/costs tables, plus a health
smoke. Helpers take a connection so they test without the Settings/disk plumbing."""
from fastapi.testclient import TestClient

from dashboard import app as dash
from dashboard import executor as dexec
from pipeline.db import jobs, registry
from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator.executor import run_stage


def _ctx(conn, filters=None, page=1):
    filters = filters or {}
    return {
        "counts": dash.status_counts(conn), "matrix": dash.stage_matrix(conn),
        "costs": dash.cost_summary(conn), "arts": dash.artifacts(conn, filters=filters, page=page),
        "facets": registry.facet_values(conn), "filters": filters, "page": page,
        "total": registry.count(conn, filters=filters), "unregistered": registry.unregistered_count(conn),
    }


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
    add_paste(settings, conn, "hello world backlog item", source_url="https://p")
    out = dash._render_overview(_ctx(conn))
    assert "hello world backlog item" in out  # title shown, not the hash
    assert "filter" in out                     # filter bar present


def test_apply_control_hold_release(settings, conn):
    h = add_paste(settings, conn, "x")
    dash.apply_control(conn, "hold", [h])
    assert jobs.get_job(conn, h, "source_note")["status"] == "held"
    dash.apply_control(conn, "release", [h])
    assert jobs.get_job(conn, h, "source_note")["status"] == "ready"


def test_run_frontier_step_then_process(settings, conn, fake_claims):
    h = add_paste(settings, conn, "Taste is the differentiator.")
    assert dexec.run_frontier(settings, conn, h, to_done=False) == ["source_note"]  # one stage

    ran = dexec.run_frontier(settings, conn, h, to_done=True)  # rest of the chain
    assert "extract_claims" in ran and "entities" in ran
    assert all(r["status"] == "done" for r in conn.execute("SELECT status FROM jobs WHERE artifact_hash=?", (h,)))


def test_enqueue_is_idempotent(monkeypatch):
    monkeypatch.setattr(dexec, "_ensure_workers", lambda: None)  # don't start/consume the queue
    dexec._inflight.clear()
    assert dexec.enqueue(["h1"], True) == ["h1"]
    assert dexec.enqueue(["h1"], True) == []  # double submit → no-op
    dexec._inflight.clear()


def test_run_frontier_skips_a_claimed_stage(settings, conn):
    h = add_paste(settings, conn, "x")
    conn.execute("UPDATE jobs SET status='running' WHERE artifact_hash=? AND stage='source_note'", (h,))
    assert dexec.run_frontier(settings, conn, h, to_done=True) == []  # already running → not re-run


def test_artifacts_filter_by_facet(settings, conn):
    add_paste(settings, conn, "a paste item", source_url="https://a")  # registers source_type=paste
    registry.register(conn, "email1", source_type="email", author="Ann", title="Newsletter #1")

    emails = dash.artifacts(conn, filters={"source_type": "email"})
    assert [a["artifact_hash"] for a in emails] == ["email1"]
    assert emails[0]["title"] == "Newsletter #1"

    pastes = dash.artifacts(conn, filters={"source_type": "paste"})
    assert all(a["source_type"] == "paste" for a in pastes) and len(pastes) == 1
