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
