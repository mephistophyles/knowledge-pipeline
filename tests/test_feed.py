"""Weekly feed batching: Saturday→Friday windows, where the window IS the cursor."""
from datetime import date

from pipeline import feed


def test_the_cutoff_is_itself_a_saturday():
    """Phil's seed cutoff and the week anchor coincide, so seed and feed meet with no gap
    and no overlap."""
    assert date(2026, 8, 1).strftime("%A") == "Saturday"
    assert feed.week_start(date(2026, 8, 1)) == date(2026, 8, 1)


def test_week_start_snaps_back_to_saturday():
    assert feed.week_start(date(2026, 8, 5)) == date(2026, 8, 1)   # Wednesday
    assert feed.week_start(date(2026, 8, 7)) == date(2026, 8, 1)   # Friday
    assert feed.week_start(date(2026, 8, 8)) == date(2026, 8, 8)   # next Saturday


def test_weeks_tile_without_gaps_or_overlap():
    weeks = feed.weeks_since(date(2026, 8, 1), date(2026, 8, 20))
    assert [w.start.isoformat() for w in weeks] == ["2026-08-01", "2026-08-08", "2026-08-15"]
    for a, b in zip(weeks, weeks[1:]):
        assert a.end.toordinal() + 1 == b.start.toordinal()


def test_a_week_is_complete_only_once_its_friday_has_passed():
    w = feed.Week(date(2026, 8, 1), date(2026, 8, 7))
    assert not w.is_complete(date(2026, 8, 5))    # mid-week
    assert not w.is_complete(date(2026, 8, 7))    # the Friday itself
    assert w.is_complete(date(2026, 8, 8))


def test_batch_id_is_derived_from_the_window_not_stored_state():
    """A named window is re-runnable; a UID cursor is not."""
    assert feed.Week(date(2026, 8, 8), date(2026, 8, 14)).batch_id == "week:2026-08-08"


def test_week_status_reports_zero_for_a_week_never_pulled(settings, conn):
    """A missed week must be visible rather than silently skipped."""
    weeks = feed.weeks_since(date(2026, 8, 1), date(2026, 8, 15))
    rows = feed.week_status(conn, weeks)
    assert len(rows) == 3
    assert all(r["archived"] == 0 for r in rows)


def test_week_status_counts_rows_tagged_to_that_batch(settings, conn):
    from pipeline.db import backlog as bl

    bl.add(conn, eml_hash="h1", eml_key="k1", message_id="m1", author="a@b.com",
           sent_at="2026-08-03T10:00:00", subject="s")
    conn.execute("UPDATE backlog SET batch_id='week:2026-08-01', triage='process' WHERE eml_hash='h1'")
    conn.commit()

    rows = feed.week_status(conn, feed.weeks_since(date(2026, 8, 1), date(2026, 8, 5)))
    assert rows[0]["archived"] == 1 and rows[0]["process"] == 1
