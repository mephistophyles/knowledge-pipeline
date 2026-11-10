"""Estimate before starting; stop cleanly when asked.

Rules of engagement after two multi-hour jobs were started without saying so."""
from pipeline import batch_guard as bg


def test_a_short_job_needs_no_flagging():
    e = bg.estimate(100, rate_key="dedup_confirm_parallel", label="x")
    assert e.minutes < bg.FLAG_THRESHOLD_MIN and not e.needs_flagging


def test_a_long_job_is_flagged_with_its_duration_and_cost():
    """The exact case: 5,819 confirm calls read as 'a sweep' and was 8 hours."""
    e = bg.estimate(5819, rate_key="dedup_confirm_serial", usd_per_call=0.0002, label="sweep")
    assert e.needs_flagging
    text = e.render()
    assert "hours" in text and "$" in text and "interrupt" in text


def test_parallel_rate_changes_the_answer_for_the_same_work():
    serial = bg.estimate(5819, rate_key="dedup_confirm_serial")
    par = bg.estimate(5819, rate_key="dedup_confirm_parallel")
    assert serial.needs_flagging and serial.minutes > par.minutes * 5


def test_stop_is_requested_and_cleared_by_file(tmp_path):
    assert not bg.should_stop(tmp_path)
    bg.request_stop(tmp_path)
    assert bg.should_stop(tmp_path)
    bg.clear_stop(tmp_path)
    assert not bg.should_stop(tmp_path)


def test_a_global_pause_also_stops_a_batch(settings, conn, tmp_path):
    """A paused pipeline and a stopped run mean different things, and a loop honouring
    only one of them will surprise someone."""
    from pipeline.db import controls as ctl

    assert not bg.should_stop(tmp_path, conn)
    ctl.set_control(conn, "global", "*", state="paused")
    assert bg.should_stop(tmp_path, conn)


# ── visible in the dashboard ──────────────────────────────────────────────────
def test_a_running_batch_is_visible_with_progress_and_eta(settings, conn):
    bg.start(conn, "sweep-1", "sweep/nemotron", 7821, rate_key="dedup_confirm_parallel")
    bg.tick(conn, "sweep-1", 1200)

    rows = bg.running(conn)
    assert len(rows) == 1 and rows[0]["done"] == 1200
    eta = bg.eta_minutes(rows[0])
    assert eta and 40 < eta < 60          # ~6,600 left at 136/min


def test_a_finished_batch_drops_out_of_the_running_list(settings, conn):
    bg.start(conn, "b", "x", 10, rate_key="dedup_confirm_parallel")
    bg.finish(conn, "b")
    assert bg.running(conn) == []


def test_staleness_is_reported_rather_than_claiming_liveness(settings, conn):
    """A process killed outright cannot mark itself stopped, so the row is a claim."""
    bg.start(conn, "b", "x", 10, rate_key="dedup_confirm_parallel")
    conn.execute("UPDATE batches SET updated_at=datetime('now','-10 minutes') WHERE batch_id='b'")
    conn.commit()
    assert bg.running(conn)[0]["stale_seconds"] > 300


def test_the_dashboard_shows_a_running_batch_and_a_stop_control(settings, conn, monkeypatch):
    from fastapi.testclient import TestClient

    from dashboard import app as dash

    bg.start(conn, "sweep-1", "judge nemotron", 7821, rate_key="dedup_confirm_parallel",
             workflow="embedding bake-off", step=2, steps_total=2)
    bg.tick(conn, "sweep-1", 1200)
    monkeypatch.setattr(dash.Settings, "load", staticmethod(lambda: settings))

    body = TestClient(dash.app).get("/").text
    assert "background batches" in body
    assert "judge nemotron" in body and "1,200/7,821" in body
    assert "embedding bake-off" in body and "2/2" in body
    assert "task left" in body and "workflow left" in body   # never conflated
    assert "stop batches" in body


def test_stopping_from_the_dashboard_requests_a_stop(settings, conn, monkeypatch):
    from fastapi.testclient import TestClient

    from dashboard import app as dash

    monkeypatch.setattr(dash.Settings, "load", staticmethod(lambda: settings))
    client = TestClient(dash.app)
    client.post("/batches/stop", data={})
    assert bg.should_stop(settings.root)

    client.post("/batches/stop", data={"clear": "1"})
    assert not bg.should_stop(settings.root)


# ── task time vs workflow time ────────────────────────────────────────────────
def test_task_and_workflow_remaining_are_reported_separately(settings, conn):
    """Quoting the running task's ETA as if it were the whole run is how two
    contradictory estimates got reported for one job."""
    bg.start(conn, "s1", "judge nomic", 4800, rate_key="dedup_confirm_parallel",
             workflow="bake-off", step=1, steps_total=2)
    bg.tick(conn, "s1", 4800)
    bg.finish(conn, "s1")
    bg.start(conn, "s2", "judge nemotron", 6400, rate_key="dedup_confirm_parallel",
             workflow="bake-off", step=2, steps_total=2)
    bg.tick(conn, "s2", 1800)

    info = bg.workflow_eta(conn, "bake-off")
    assert info["steps_done"] == 1 and info["steps_total"] == 2
    assert info["projected_steps"] == 0            # both steps are known
    assert info["task_minutes"] == info["workflow_minutes"]   # nothing left after this one


def test_unstarted_steps_are_projected_into_the_workflow_total(settings, conn):
    bg.start(conn, "s1", "step one", 1000, rate_key="dedup_confirm_parallel",
             workflow="wf", step=1, steps_total=3)
    bg.tick(conn, "s1", 500)

    info = bg.workflow_eta(conn, "wf")
    assert info["projected_steps"] == 2
    assert info["workflow_minutes"] > info["task_minutes"]


def test_units_must_match_between_total_and_tick(settings, conn):
    """The first live registration passed candidate PAIRS as total and LLM CALLS as done,
    which made the ETA meaningless. Same units in, sensible ETA out."""
    bg.start(conn, "b", "judge", 6411, rate_key="dedup_confirm_parallel")
    bg.tick(conn, "b", 1801)
    eta = bg.eta_minutes(bg.running(conn)[0])
    assert 30 < eta < 40          # (6411-1801)/136


def test_the_overview_auto_refreshes_only_while_a_batch_runs(settings, conn, monkeypatch):
    """Polling by hand is what the operator was reduced to; refreshing a quiet page
    forever would just be noise."""
    from fastapi.testclient import TestClient

    from dashboard import app as dash

    monkeypatch.setattr(dash.Settings, "load", staticmethod(lambda: settings))
    client = TestClient(dash.app)
    assert "http-equiv=refresh" not in client.get("/").text

    bg.start(conn, "b", "judge", 100, rate_key="dedup_confirm_parallel")
    assert "http-equiv=refresh" in client.get("/").text

    bg.finish(conn, "b")
    assert "http-equiv=refresh" not in client.get("/").text
