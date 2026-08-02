"""Importing locally saved pages.

The path that involves no server request, so robots.txt has nothing to have an opinion
about — which is why it exists instead of a robots override."""
import pytest

from pipeline import authors
from pipeline.web import snapshot


def _chrome_save(url="https://stratechery.com/2024/aggregation/", title="On Aggregation"):
    return (
        f'<!-- saved from url=({len(url):04d}){url} -->\n'
        f"<html><head><title>{title}</title></head><body><p>Body text.</p></body></html>"
    ).encode()


def _mhtml(url="https://example.com/post", title="Saved Post"):
    return (
        "From: <Saved by Blink>\r\n"
        "Snapshot-Content-Location: " + url + "\r\n"
        "Subject: " + title + "\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/related; boundary="----B"\r\n'
        "\r\n"
        "------B\r\n"
        "Content-Type: text/html\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n"
        "Content-Location: " + url + "\r\n"
        "\r\n"
        f"<html><head><title>{title}</title></head><body><p>Hi</p></body></html>\r\n"
        "------B--\r\n"
    ).encode()


# ── recovering provenance ─────────────────────────────────────────────────────
def test_url_recovered_from_a_chrome_save_comment():
    assert snapshot.url_of(_chrome_save()) == "https://stratechery.com/2024/aggregation/"


def test_url_recovered_from_a_canonical_link():
    raw = b'<html><head><link rel="canonical" href="https://example.com/real"></head></html>'
    assert snapshot.url_of(raw) == "https://example.com/real"


def test_url_recovered_when_href_precedes_rel():
    raw = b'<html><head><link href="https://example.com/real" rel="canonical"></head></html>'
    assert snapshot.url_of(raw) == "https://example.com/real"


def test_no_url_when_the_file_records_none():
    assert snapshot.url_of(b"<html><body>nothing</body></html>") is None


def test_mhtml_yields_html_and_its_recorded_url(tmp_path):
    f = tmp_path / "page.mhtml"
    f.write_bytes(_mhtml())
    html, url = snapshot.read_snapshot(f)
    assert url == "https://example.com/post"
    assert b"<title>Saved Post</title>" in html


# ── importing ─────────────────────────────────────────────────────────────────
def test_import_archives_and_resolves_the_author(settings, conn, tmp_path):
    authors.upsert_identity(conn, "person:ben-thompson", "Ben Thompson")
    authors.add_alias(conn, "stratechery.com", "person:ben-thompson", confidence="curated")
    f = tmp_path / "saved.html"
    f.write_bytes(_chrome_save())

    r = snapshot.import_file(settings, conn, f)

    assert r["state"] == "archived"
    assert r["url"] == "https://stratechery.com/2024/aggregation"
    assert r["identity_id"] == "person:ben-thompson"
    assert r["title"] == "On Aggregation"
    row = conn.execute("SELECT * FROM web_backlog WHERE fetch_hash=?", (r["fetch_hash"],)).fetchone()
    assert row["http_status"] == 0          # 0 = never requested; it came off disk
    assert settings.blobstore.exists(row["fetch_key"])


def test_explicit_url_wins_over_the_file(settings, conn, tmp_path):
    """A reader-mode export or hand-edited save may record the wrong URL; the caller knows
    what they read."""
    f = tmp_path / "saved.html"
    f.write_bytes(_chrome_save(url="https://wrong.example/x"))

    r = snapshot.import_file(settings, conn, f, url="https://right.example/y")
    assert r["url"] == "https://right.example/y" and r["resolved_url_from"] == "flag"


def test_a_snapshot_with_no_provenance_is_refused(settings, conn, tmp_path):
    """No URL means no hostname, so no author — a claim with no attributable source is
    worth less than no claim."""
    f = tmp_path / "bare.html"
    f.write_bytes(b"<html><body><p>orphan</p></body></html>")

    with pytest.raises(snapshot.SnapshotError, match="--url"):
        snapshot.import_file(settings, conn, f)


def test_snapshot_supersedes_an_earlier_escalation(settings, conn, tmp_path):
    """The reason a page could not be fetched is precisely why it was saved by hand."""
    from pipeline.web import ledger
    from pipeline.web.fetch import Fetcher

    blocked = Fetcher(transport=lambda u, h: (403, {"Content-Type": "text/html"}, b"no"),
                      respect_robots=False, min_interval=0)
    esc = ledger.add_url(settings, conn, "https://stratechery.com/2024/aggregation/", fetcher=blocked)
    assert esc["state"] == "escalated"

    f = tmp_path / "saved.html"
    f.write_bytes(_chrome_save())
    r = snapshot.import_file(settings, conn, f)

    assert r["state"] == "archived"
    rows = conn.execute(
        "SELECT state FROM web_backlog WHERE url=?", ("https://stratechery.com/2024/aggregation",)
    ).fetchall()
    assert [x["state"] for x in rows] == ["archived"]   # the escalated row is replaced, not doubled


def test_the_same_page_fetched_and_saved_is_not_two_articles(settings, conn, tmp_path):
    f = tmp_path / "saved.html"
    f.write_bytes(_chrome_save())
    first = snapshot.import_file(settings, conn, f)
    second = snapshot.import_file(settings, conn, f, url="https://stratechery.com/2024/aggregation/?utm_source=x")

    assert first["state"] == "archived" and second["state"] == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM web_backlog").fetchone()[0] == 1


def test_import_dir_reports_failures_without_stopping(settings, conn, tmp_path):
    (tmp_path / "a.html").write_bytes(_chrome_save(url="https://a.com/1"))
    (tmp_path / "b.html").write_bytes(b"<html><body>no provenance</body></html>")
    (tmp_path / "c.html").write_bytes(_chrome_save(url="https://a.com/2"))
    (tmp_path / "ignored.txt").write_text("not a snapshot")

    counts = snapshot.import_dir(settings, conn, tmp_path)

    assert counts["archived"] == 2 and counts["failed"] == 1


def test_snapshot_and_fetch_share_one_archive(settings, conn, tmp_path):
    """Extraction reads the archive without caring how the bytes arrived."""
    from pipeline.web import ledger
    from pipeline.web.fetch import Fetcher

    body = b"<html><head><title>T</title></head><body><p>same bytes</p></body></html>"
    ledger.add_url(settings, conn, "https://a.com/1",
                   fetcher=Fetcher(transport=lambda u, h: (200, {"Content-Type": "text/html"}, body),
                                   respect_robots=False, min_interval=0))
    f = tmp_path / "s.html"
    f.write_bytes(body)
    r = snapshot.import_file(settings, conn, f, url="https://a.com/2")

    # Identical bytes hash identically, so the archive holds ONE blob for both.
    keys = {row["fetch_key"] for row in conn.execute("SELECT fetch_key FROM web_backlog")}
    assert len(keys) == 1
    assert r["state"] == "duplicate"


def test_a_subdomain_resolves_to_the_curated_root_domain(settings, conn, tmp_path):
    """A writer publishing at newsletter.example.com and example.com is one writer;
    curating every subdomain separately would fracture them for no reason."""
    from pipeline.web import ledger

    authors.upsert_identity(conn, "person:gergely-orosz", "Gergely Orosz")
    authors.add_alias(conn, "pragmaticengineer.com", "person:gergely-orosz", confidence="curated")

    assert ledger.identity_for(conn, "https://newsletter.pragmaticengineer.com/p/x") == "person:gergely-orosz"


def test_platform_subdomains_still_resolve_to_nobody(settings, conn):
    """someone.substack.com must not fall back to substack.com."""
    from pipeline.web import ledger

    authors.upsert_identity(conn, "person:wrong", "Wrong")
    authors.add_alias(conn, "substack.com", "person:wrong", confidence="curated")

    assert ledger.identity_for(conn, "https://someone.substack.com/p/x") is None
