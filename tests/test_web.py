"""Web article ingestion (v1: hand-selected body text)."""
from pipeline.db import jobs, registry
from pipeline.ingestors.web import add_web, normalize, site_of
from pipeline.orchestrator.executor import run_stage
from pipeline.storage.manifest import load_manifest
from pipeline.vault.writer import read_note


def test_site_of_strips_www():
    assert site_of("https://www.stratechery.com/2026/post/") == "stratechery.com"
    assert site_of(None) is None


def test_normalize_tidies_without_stripping_content():
    """v1 must not remove anything: what you paste is what gets extracted."""
    assert normalize("A   \n\n\n\nB") == "A\n\nB"
    assert "Related posts" in normalize("Body.\n\nRelated posts")


def test_ingest_records_author_and_site(settings, conn):
    h = add_web(
        settings, conn, "# On Taste\n\nThe body of the article.",
        url="https://www.example.com/on-taste", author="writer@example.com",
    )
    man = load_manifest(settings.blobstore, h)
    assert man.source_type == "web"
    assert man.extra["from"] == "writer@example.com"
    assert man.extra["site"] == "example.com"
    assert man.extra["canonical_url"] == "https://www.example.com/on-taste"

    row = registry.get(conn, h)
    assert row["author"] == "writer@example.com" and row["source"] == "example.com"
    assert jobs.get_job(conn, h, "source_note")["status"] == "ready"


def test_author_defaults_to_site_so_one_blog_is_one_voice(settings, conn):
    """Without this, every post is its own author and a single blog's posts would
    corroborate each other — inflating the cross-author signal."""
    a = add_web(settings, conn, "Post one body.", url="https://blog.example.com/a")
    b = add_web(settings, conn, "Post two body.", url="https://blog.example.com/b")
    ma, mb = load_manifest(settings.blobstore, a), load_manifest(settings.blobstore, b)
    assert ma.extra["from"] == mb.extra["from"] == "blog.example.com"


def test_empty_body_is_rejected(settings, conn):
    assert add_web(settings, conn, "   \n\n  ", url="https://example.com/x") is None


def test_web_runs_the_standard_corpus_chain(settings, conn, fake_claims):
    """A web article must flow through the same derivation chain as email."""
    fake_claims["text"] = '[{"claim": "A web-sourced claim.", "quote": "the body"}]'
    h = add_web(settings, conn, "Title\n\nthe body of it", url="https://example.com/p",
                author="writer@example.com")
    for stage in ("source_note", "extract_claims", "dedup"):
        run_stage(settings, conn, h, stage)

    note = read_note(settings.vault_dir / f"corpus/claims/claim-{h[:8]}-00.md")
    assert note.metadata["attestations"][0]["author"] == "writer@example.com"
