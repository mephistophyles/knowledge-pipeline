"""Email ingestion (plan §4.1): each message → one clean-markdown artifact routed
into the corpus chain, with headers captured in the manifest. Uses fake message
objects (shape mirrors imap_tools.MailMessage) so no live IMAP is needed."""
from pipeline.db import jobs
from pipeline.ingestors.email import _clean_text, ingest_messages
from pipeline.storage.manifest import load_artifact, load_manifest


class FakeMsg:
    def __init__(self, text="", html="", from_="a@x.com", subject="Hi", date="2026-01-01", headers=None):
        self.text = text
        self.html = html
        self.from_ = from_
        self.subject = subject
        self.date = date
        self.headers = headers or {}


def test_ingest_email_creates_artifact_with_metadata(settings, conn):
    msg = FakeMsg(
        text="Taste is the differentiator.",
        from_="author@substack.com",
        subject="On Taste",
        date="2026-07-01",
        headers={"message-id": ("<abc@substack.com>",), "list-id": ("Author <author.substack.com>",)},
    )
    hashes = ingest_messages(settings, conn, [msg])
    assert len(hashes) == 1
    h = hashes[0]

    job = jobs.get_job(conn, h, "source_note")  # email routes into the corpus chain
    assert job["status"] == "ready" and job["source_type"] == "email"

    m = load_manifest(settings.blobstore, h)
    assert m.source_type == "email"
    assert m.extra["subject"] == "On Taste"
    assert m.extra["from"] == "author@substack.com"
    assert m.extra["message_id"] == "<abc@substack.com>"
    assert "Author" in m.extra["list_id"]


def test_html_only_email_is_converted_to_markdown(settings, conn):
    hashes = ingest_messages(settings, conn, [FakeMsg(html="<h1>Title</h1><p>Body <b>text</b>.</p>")])
    body = load_artifact(settings.blobstore, load_manifest(settings.blobstore, hashes[0])).decode()
    assert "Title" in body and "text" in body


def test_ingest_skips_empty_messages(settings, conn):
    assert ingest_messages(settings, conn, [FakeMsg(text="", html="")]) == []


def test_clean_text_prefers_plain_over_html():
    assert _clean_text(FakeMsg(text="plain here", html="<p>html</p>")) == "plain here"
