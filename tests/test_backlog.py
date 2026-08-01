"""Backlog ledger (pre-cutoff seed corpus).

The guarantees this file exists to hold: the mailbox is read once, the same message
is never counted twice, finished work is never silently redone, and everything can
be re-derived from local bytes without IMAP.
"""
from email.message import EmailMessage

import pytest

from pipeline.db import backlog as bl
from pipeline.db import jobs
from pipeline.ingestors.email import EmlMessage, archive_message, eml_key, ingest_from_eml
from pipeline.orchestrator.executor import run_stage


class _RawMsg:
    """Duck-types the `.obj.as_bytes()` surface `archive_message` reads."""

    def __init__(self, msg: EmailMessage):
        self.obj = msg
        self.date = None


def _eml(body="Some real substance here.", frm="author@substack.com", subject="Edition", mid="<a@x>"):
    m = EmailMessage()
    m["From"] = frm
    m["Subject"] = subject
    m["Date"] = "Tue, 1 Jul 2026 09:00:00 +0000"
    if mid:
        m["Message-ID"] = mid
    m.set_content(body)
    return _RawMsg(m)


# ── archiving ────────────────────────────────────────────────────────────────
def test_archive_stores_raw_bytes_and_ledger_row(settings, conn):
    res = archive_message(settings, conn, _eml())
    assert res["status"] == "added"

    row = bl.get(conn, res["eml_hash"])
    assert row["state"] == "archived"
    assert row["author"] == "author@substack.com"
    assert row["subject"] == "Edition"
    assert settings.blobstore.exists(eml_key(res["eml_hash"]))  # raw bytes are recoverable


def test_archiving_twice_is_idempotent(settings, conn):
    first = archive_message(settings, conn, _eml())
    second = archive_message(settings, conn, _eml())  # identical bytes → identical hash
    assert first["eml_hash"] == second["eml_hash"]
    assert second["status"] == "known"
    assert bl.summary(conn)["total"] == 1  # an interrupted scan can just be re-run


def test_same_message_id_different_bytes_is_flagged_duplicate(settings, conn):
    """A re-delivered edition can gain headers — same Message-ID, different bytes.
    It must not become a second copy of the same edition."""
    archive_message(settings, conn, _eml(body="Body one."))
    res = archive_message(settings, conn, _eml(body="Body one, redelivered."))
    assert res["status"] == "duplicate"
    assert bl.get(conn, res["eml_hash"])["state"] == "duplicate"
    assert bl.pending(conn, limit=10) == [] or len(bl.pending(conn, limit=10)) == 1


def test_missing_message_id_does_not_collapse_distinct_mail(settings, conn):
    """Senders that omit Message-ID must not all collide into one 'duplicate'."""
    a = archive_message(settings, conn, _eml(body="First.", mid=None))
    b = archive_message(settings, conn, _eml(body="Second.", mid=None))
    assert a["status"] == "added" and b["status"] == "added"


# ── re-derivation without IMAP ───────────────────────────────────────────────
def test_ingest_from_eml_derives_artifact_and_advances_state(settings, conn, fake_claims):
    res = archive_message(settings, conn, _eml())
    h = ingest_from_eml(settings, conn, res["eml_hash"])

    row = bl.get(conn, res["eml_hash"])
    assert row["state"] == "ingested" and row["artifact_hash"] == h
    assert jobs.get_job(conn, h, "source_note")["status"] == "ready"


def test_eml_message_roundtrips_headers_and_body(settings, conn):
    """Re-derivation reads the archive through the same parser as first ingest."""
    res = archive_message(settings, conn, _eml(body="The body.", frm="A <a@x.com>", subject="Subj"))
    msg = EmlMessage(settings.blobstore.read(eml_key(res["eml_hash"])))
    assert "The body." in msg.text
    assert msg.from_ == "A <a@x.com>" and msg.subject == "Subj"
    assert msg.headers["message-id"][0] == "<a@x>"


# ── no accidental re-spend ───────────────────────────────────────────────────
def test_completed_chain_is_not_resurrected_by_reingest(settings, conn, fake_claims):
    """Re-ingesting identical bytes must not reset a finished stage back to ready —
    that was silently re-paying for every LLM call on overlapping batches."""
    res = archive_message(settings, conn, _eml())
    h = ingest_from_eml(settings, conn, res["eml_hash"])
    run_stage(settings, conn, h, "source_note")
    assert jobs.get_job(conn, h, "source_note")["status"] == "done"

    ingest_from_eml(settings, conn, res["eml_hash"], reprocess=False)
    assert jobs.get_job(conn, h, "source_note")["status"] == "done"  # still done


def test_reprocess_flag_does_requeue_a_finished_chain(settings, conn, fake_claims):
    res = archive_message(settings, conn, _eml())
    h = ingest_from_eml(settings, conn, res["eml_hash"])
    run_stage(settings, conn, h, "source_note")

    ingest_from_eml(settings, conn, res["eml_hash"], reprocess=True)
    assert jobs.get_job(conn, h, "source_note")["status"] == "ready"  # deliberate re-run works


# ── batching ─────────────────────────────────────────────────────────────────
def test_batches_are_per_author_and_survive_display_name_changes(settings, conn):
    archive_message(settings, conn, _eml(body="One.", frm="author@substack.com", mid="<1@x>"))
    archive_message(settings, conn, _eml(body="Two.", frm="The Author <Author+weekly@Substack.com>", mid="<2@x>"))
    archive_message(settings, conn, _eml(body="Three.", frm="other@example.com", mid="<3@x>"))

    assert bl.assign_batches(conn, only_triage=None) == 3
    rows = {r["batch_id"]: r["total"] for r in bl.batches(conn)}
    assert rows == {"author@substack.com": 2, "other@example.com": 1}


def test_assign_batches_defaults_to_triaged_process_only(settings, conn):
    a = archive_message(settings, conn, _eml(body="Keep.", mid="<1@x>"))
    archive_message(settings, conn, _eml(body="Drop.", mid="<2@x>"))
    bl.set_triage(conn, a["eml_hash"], "process")

    assert bl.assign_batches(conn) == 1  # the untriaged/dropped row stays unbatched
    assert [r["batch_id"] for r in bl.batches(conn)] == ["author@substack.com"]


def test_pending_is_oldest_first_so_corroboration_accumulates_in_order(settings, conn):
    for n, day in ((1, "2026-01-05"), (2, "2026-01-01"), (3, "2026-01-03")):
        res = archive_message(settings, conn, _eml(body=f"Body {n}.", mid=f"<{n}@x>"))
        conn.execute("UPDATE backlog SET sent_at=? WHERE eml_hash=?", (day, res["eml_hash"]))
    conn.commit()
    assert [r["sent_at"] for r in bl.pending(conn, limit=10)] == ["2026-01-01", "2026-01-03", "2026-01-05"]


def test_ingest_from_eml_rejects_unknown_hash(settings, conn):
    with pytest.raises(KeyError):
        ingest_from_eml(settings, conn, "0" * 64)


# ── nuanced retry: which messages failed, and requeue only those ─────────────
def test_failures_and_retry_target_only_the_broken_rows(settings, conn, fake_claims):
    ok = archive_message(settings, conn, _eml(body="Fine.", mid="<1@x>"))
    bad = archive_message(settings, conn, _eml(body="Broken.", mid="<2@x>"))
    h_ok = ingest_from_eml(settings, conn, ok["eml_hash"])
    h_bad = ingest_from_eml(settings, conn, bad["eml_hash"])
    jobs.mark_failed(conn, h_bad, "source_note", "boom")
    conn.commit()

    rows = bl.failures(conn)
    assert [r["eml_hash"] for r in rows] == [bad["eml_hash"]]
    assert rows[0]["stage"] == "source_note" and "boom" in rows[0]["error"]

    assert bl.requeue_failed(conn) == 1  # only the failed one is touched
    assert jobs.get_job(conn, h_bad, "source_note")["status"] == "ready"
    assert jobs.get_job(conn, h_ok, "source_note")["status"] == "ready"
    assert bl.failures(conn) == []


def test_progress_rolls_up_ledger_rows_by_stage(settings, conn, fake_claims):
    res = archive_message(settings, conn, _eml())
    ingest_from_eml(settings, conn, res["eml_hash"])
    rows = {(r["stage"], r["status"]): r["n"] for r in bl.progress(conn)}
    assert rows[("source_note", "ready")] == 1
