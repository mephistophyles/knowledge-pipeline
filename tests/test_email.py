"""Email ingestion (plan §4.1): each message → one clean-markdown artifact routed
into the corpus chain, with headers captured in the manifest. Uses fake message
objects (shape mirrors imap_tools.MailMessage) so no live IMAP is needed."""
from pipeline.db import jobs, registry
from pipeline.ingestors.email import _clean_text, _normalize, ingest_messages
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


def test_normalize_strips_boilerplate_and_extracts_canonical():
    raw = (
        "View this post on the web at https://blog.example/p/the-piece\n"
        "The real body has a link [ https://substack.com/redirect/abc?j=xyz ] and more here.\n\n\n"
        "Unsubscribe at https://example/unsub\n© 2026 Author"
    )
    clean, canonical = _normalize(raw)
    assert canonical == "https://blog.example/p/the-piece"
    assert "View this post" not in clean
    assert "substack.com/redirect" not in clean
    assert "Unsubscribe" not in clean
    assert "The real body has a link" in clean and "more here." in clean


def test_email_registers_word_count(settings, conn):
    h = ingest_messages(settings, conn, [FakeMsg(text="one two three four five six", subject="S")])[0]
    assert registry.get(conn, h)["word_count"] == 6


def test_email_ingest_populates_registry(settings, conn):
    msg = FakeMsg(
        text="Body.", from_="author@substack.com", subject="On Taste",
        headers={"list-id": ("Author <author.substack.com>",)},
    )
    h = ingest_messages(settings, conn, [msg])[0]
    r = registry.get(conn, h)
    assert r["source_type"] == "email"
    assert r["author"] == "author@substack.com"
    assert "author.substack.com" in r["source"]  # grouped by List-Id (the newsletter)
    assert r["title"] == "On Taste"
