"""Web fetch + ledger (web-ingestion-plan.md Part 2, build step 2).

Transport is injected throughout — the suite never touches the network."""
import pytest

from pipeline import authors
from pipeline.web import ledger
from pipeline.web.canonical import canonicalize, resolve_canonical_link, site_of
from pipeline.web.fetch import USER_AGENT, Escalation, Fetcher


def _page(body="<h1>Hello</h1>", title="A Post", canonical=None, ctype="text/html", status=200):
    head = f"<title>{title}</title>" if title else ""
    if canonical:
        head += f'<link rel="canonical" href="{canonical}">'
    html = f"<html><head>{head}</head><body>{body}</body></html>".encode()
    return lambda url, headers: (status, {"Content-Type": ctype}, html)


def _fetcher(transport):
    return Fetcher(transport=transport, respect_robots=False, min_interval=0)


# ── canonicalization ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://WWW.Stratechery.com/2024/post/", "https://stratechery.com/2024/post"),
        ("http://example.com", "http://example.com/"),
        ("https://example.com/p?utm_source=x&utm_medium=y", "https://example.com/p"),
        ("https://example.com/p?fbclid=abc", "https://example.com/p"),
        ("https://example.com/p#section", "https://example.com/p"),
        ("https://example.com/p?b=2&a=1", "https://example.com/p?a=1&b=2"),   # order can't split one page
        ("https://example.com//a//b", "https://example.com/a/b"),
    ],
)
def test_canonicalize(raw, expected):
    assert canonicalize(raw) == expected


def test_meaningful_query_params_are_kept():
    """Stripping `?p=123` would FUSE different articles — a worse error than a duplicate."""
    assert canonicalize("https://example.com/?p=123") == "https://example.com/?p=123"
    assert canonicalize("https://example.com/list?page=2") == "https://example.com/list?page=2"


def test_same_site_canonical_is_adopted():
    url, syndicated = resolve_canonical_link(
        "https://example.com/p?utm_source=rss", "https://example.com/canonical-post"
    )
    assert url == "https://example.com/canonical-post" and not syndicated


def test_cross_site_canonical_is_flagged_not_followed():
    """Adopting the origin's URL would attribute a republication to the original site."""
    url, syndicated = resolve_canonical_link(
        "https://mirror.com/p", "https://original.com/p"
    )
    assert url == "https://mirror.com/p" and syndicated


# ── fetching ──────────────────────────────────────────────────────────────────
def test_fetch_returns_raw_bytes_and_metadata():
    f = _fetcher(_page(title="On Aggregation"))
    got = f.fetch("https://stratechery.com/post/?utm_source=rss")

    assert got.url == "https://stratechery.com/post"
    assert got.title == "On Aggregation"
    assert b"<h1>Hello</h1>" in got.body
    assert len(got.fetch_hash) == 64


def test_fetch_hash_is_over_raw_bytes_not_the_url():
    """Identity keyed on raw bytes is what makes swapping the extractor a re-derivation
    rather than a corpus-wide identity reset."""
    a = _fetcher(_page(body="<p>same</p>")).fetch("https://a.com/x")
    b = _fetcher(_page(body="<p>same</p>")).fetch("https://b.com/y")
    assert a.fetch_hash == b.fetch_hash

    c = _fetcher(_page(body="<p>different</p>")).fetch("https://a.com/x")
    assert c.fetch_hash != a.fetch_hash


@pytest.mark.parametrize(
    "transport,cause",
    [
        (_page(ctype="application/pdf"), "pdf"),
        (_page(ctype="image/png"), "unsupported_content_type"),
        (_page(status=404), "http_error"),
        (_page(status=403), "http_error"),
        (lambda u, h: (200, {"Content-Type": "text/html"}, b""), "empty_body"),
    ],
)
def test_unhandleable_pages_escalate_with_a_countable_cause(transport, cause):
    with pytest.raises(Escalation) as e:
        _fetcher(transport).fetch("https://example.com/x")
    assert e.value.cause == cause


def test_network_failure_escalates_rather_than_crashing():
    def boom(url, headers):
        raise ConnectionResetError("nope")

    with pytest.raises(Escalation) as e:
        _fetcher(boom).fetch("https://example.com/x")
    assert e.value.cause == "fetch_error"


def test_robots_disallowed_escalates():
    class Blocked(Fetcher):
        def _allowed(self, url):
            return False

    with pytest.raises(Escalation) as e:
        Blocked(transport=_page(), min_interval=0).fetch("https://example.com/x")
    assert e.value.cause == "robots_disallowed"


# ── the ledger ────────────────────────────────────────────────────────────────
def test_add_url_archives_and_resolves_the_author(settings, conn):
    authors.upsert_identity(conn, "person:ben-thompson", "Ben Thompson")
    authors.add_alias(conn, "stratechery.com", "person:ben-thompson", confidence="curated")

    r = ledger.add_url(settings, conn, "https://stratechery.com/post/", fetcher=_fetcher(_page()))

    assert r["state"] == "archived"
    assert r["identity_id"] == "person:ben-thompson"
    row = conn.execute("SELECT * FROM web_backlog WHERE fetch_hash=?", (r["fetch_hash"],)).fetchone()
    assert row["site"] == "stratechery.com"
    assert settings.blobstore.exists(row["fetch_key"])


def test_the_same_article_under_a_tracking_param_is_one_row(settings, conn):
    f = _fetcher(_page())
    a = ledger.add_url(settings, conn, "https://example.com/p", fetcher=f)
    b = ledger.add_url(settings, conn, "https://example.com/p?utm_source=twitter", fetcher=f)

    assert a["state"] == "archived" and b["state"] == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM web_backlog").fetchone()[0] == 1


def test_shared_platform_hosts_never_resolve_to_an_author(settings, conn):
    """substack.com as an alias would make every Substack writer the same person."""
    authors.upsert_identity(conn, "person:someone", "Someone")
    authors.add_alias(conn, "substack.com", "person:someone", confidence="curated")

    r = ledger.add_url(settings, conn, "https://substack.com/p/x", fetcher=_fetcher(_page()))
    assert r["identity_id"] is None


def test_an_escalated_page_is_recorded_and_counted_not_raised(settings, conn):
    """A scan over a reading list must not stop at the first paywall."""
    r = ledger.add_url(settings, conn, "https://example.com/x", fetcher=_fetcher(_page(status=403)))

    assert r["state"] == "escalated" and r["cause"] == "http_error"
    assert conn.execute("SELECT COUNT(*) FROM web_escalations WHERE cause='http_error'").fetchone()[0] == 1
    row = conn.execute("SELECT state, escalation FROM web_backlog WHERE url=?",
                       ("https://example.com/x",)).fetchone()
    assert (row["state"], row["escalation"]) == ("escalated", "http_error")


def test_scan_counts_outcomes_and_keeps_going(settings, conn):
    def transport(url, headers):
        if "paywalled" in url:
            return 402, {"Content-Type": "text/html"}, b"nope"
        return 200, {"Content-Type": "text/html"}, f"<html><body>{url}</body></html>".encode()

    counts = ledger.scan(
        settings, conn,
        ["https://a.com/1", "https://a.com/paywalled", "https://a.com/2"],
        fetcher=_fetcher(transport),
    )

    assert counts["archived"] == 2
    assert counts["escalated"] == 1 and counts["cause:http_error"] == 1


def test_escalation_rates_are_reported_by_cause(settings, conn):
    ledger.record_escalation(conn, "https://a.com/1", "http_error", "402")
    ledger.record_escalation(conn, "https://a.com/2", "http_error", "402")
    ledger.record_escalation(conn, "https://a.com/3", "pdf", "application/pdf")
    conn.commit()

    rates = {r["cause"]: r["n"] for r in ledger.escalation_rates(conn)}
    assert rates == {"http_error": 2, "pdf": 1}


def test_site_of_strips_www():
    assert site_of("https://www.example.com/p") == "example.com"


def test_title_entities_are_decoded():
    """The title reaches the registry and note frontmatter — `Bezos&#x27;s` would be
    carried all the way into the vault."""
    f = _fetcher(_page(title="Bezos&#x27;s Shadow &amp; the Review"))
    assert f.fetch("https://example.com/x").title == "Bezos's Shadow & the Review"


# ── reading lists ─────────────────────────────────────────────────────────────
def test_read_url_list_plain_lines(tmp_path):
    f = tmp_path / "list.txt"
    f.write_text("# my backlog\nhttps://a.com/1\n\nhttps://b.com/2\n")
    assert ledger.read_url_list(str(f)) == ["https://a.com/1", "https://b.com/2"]


def test_read_url_list_csv_export_with_header_and_columns(tmp_path):
    """Pocket/Instapaper/Readwise exports carry a header and metadata columns; a header
    row has no URL in it and drops out for free."""
    f = tmp_path / "export.csv"
    f.write_text(
        "url,title,time_added,tags\n"
        "https://a.com/1,First Post,1699999999,reading\n"
        "https://b.com/2,\"Second, with a comma\",1700000000,\n"
    )
    assert ledger.read_url_list(str(f)) == ["https://a.com/1", "https://b.com/2"]


def test_read_url_list_finds_the_url_in_any_column(tmp_path):
    f = tmp_path / "export.csv"
    f.write_text("Title,URL\nSome Post,https://a.com/1\n")
    assert ledger.read_url_list(str(f)) == ["https://a.com/1"]


def test_read_url_list_dedupes_on_the_canonical_form(tmp_path):
    """The same article listed twice under different tracking params is one fetch."""
    f = tmp_path / "list.txt"
    f.write_text(
        "https://a.com/p\n"
        "https://a.com/p?utm_source=twitter\n"
        "https://www.a.com/p/\n"
    )
    assert ledger.read_url_list(str(f)) == ["https://a.com/p"]


def test_http_errors_from_the_real_transport_are_classified_as_http_error():
    """urllib RAISES HTTPError on 4xx rather than returning a status. Letting that reach
    the generic handler counted every paywall and dead link as `fetch_error`, collapsing
    two causes the escalation rate has to tell apart."""
    import urllib.error

    class Real(Fetcher):
        def _get(self, url):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    f = Real(respect_robots=False, min_interval=0)
    with pytest.raises(Escalation) as e:
        f.fetch("https://example.com/x")
    assert e.value.cause == "fetch_error"  # a subclass that bypasses _get's own handling


def test_urllib_http_error_is_converted_to_a_status_not_an_exception(monkeypatch):
    """The conversion happens inside _get, so the status check downstream can see it."""
    import urllib.error
    import urllib.request

    def raise_403(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {"Content-Type": "text/html"}, None)

    monkeypatch.setattr(urllib.request, "urlopen", raise_403)
    with pytest.raises(Escalation) as e:
        Fetcher(respect_robots=False, min_interval=0).fetch("https://example.com/x")
    assert e.value.cause == "http_error" and e.value.detail == "403"


def test_read_url_list_handles_url_pipe_title_lines(tmp_path):
    """A hand-kept backlog is often `<url> | <title>`; taking the cell whole would fetch
    the title as part of the URL."""
    f = tmp_path / "backlog.md"
    f.write_text(
        "# Blog backlog\n\n"
        "Some prose that is not a URL at all.\n\n"
        "https://a.com/1 | A Title, With A Comma\n"
        "https://b.com/2 | Another Title\n"
    )
    assert ledger.read_url_list(str(f)) == ["https://a.com/1", "https://b.com/2"]


def test_read_url_list_ignores_markdown_prose_and_headings(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("## Heading\n\nA sentence mentioning nothing.\n\nhttps://a.com/1\n")
    assert ledger.read_url_list(str(f)) == ["https://a.com/1"]


# ── robots.txt ────────────────────────────────────────────────────────────────
def _robots_transport(robots_body, robots_status=200, page_status=200):
    def transport(url, headers):
        if url.endswith("/robots.txt"):
            return robots_status, {"Content-Type": "text/plain"}, robots_body
        return page_status, {"Content-Type": "text/html"}, b"<html><title>T</title><body>hi</body></html>"
    return transport


def test_robots_is_fetched_with_our_own_user_agent():
    """RobotFileParser.read() uses urllib's DEFAULT UA, which Cloudflare 403s — and urllib
    reads a 403 as disallow-everything. That self-blocked us on sites that permit us: a
    real 22-URL run reported 12 refusals, most of them false."""
    seen = []

    def transport(url, headers):
        seen.append((url, headers.get("User-Agent")))
        if url.endswith("/robots.txt"):
            return 200, {}, b"User-agent: *\nDisallow: /wp-admin/\n"
        return 200, {"Content-Type": "text/html"}, b"<html><title>T</title><body>ok</body></html>"

    Fetcher(transport=transport, min_interval=0).fetch("https://andrewchen.com/post")
    robots_ua = next(ua for u, ua in seen if u.endswith("robots.txt"))
    assert robots_ua == USER_AGENT


def test_a_path_the_site_permits_is_fetched():
    f = Fetcher(transport=_robots_transport(b"User-agent: *\nDisallow: /wp-admin/\n"), min_interval=0)
    assert f.fetch("https://andrewchen.com/psychd-funnel-conversion").url


def test_an_empty_disallow_means_allow_everything():
    """`Disallow:` with no path is Yoast's 'everything is fine' block."""
    f = Fetcher(transport=_robots_transport(b"User-agent: *\nDisallow:\n"), min_interval=0)
    assert f.fetch("https://davidafields.com/some-post").url


def test_a_disallowed_path_is_still_refused():
    f = Fetcher(transport=_robots_transport(b"User-agent: *\nDisallow: /private/\n"), min_interval=0)
    with pytest.raises(Escalation) as e:
        f.fetch("https://example.com/private/thing")
    assert e.value.cause == "robots_disallowed"


def test_missing_robots_permits_access():
    """RFC 9309 §2.3.1: 4xx means no robots file applies."""
    f = Fetcher(transport=_robots_transport(b"", robots_status=404), min_interval=0)
    assert f.fetch("https://example.com/post").url


def test_a_403_on_robots_does_not_mean_disallow_everything():
    """The exact false refusal: urllib's parser treats 403 as a total ban."""
    f = Fetcher(transport=_robots_transport(b"", robots_status=403), min_interval=0)
    assert f.fetch("https://speakingsherpa.com/post").url


def test_a_failing_server_makes_us_back_off():
    """5xx: the server is unwell, so do not add load."""
    f = Fetcher(transport=_robots_transport(b"", robots_status=503), min_interval=0)
    with pytest.raises(Escalation) as e:
        f.fetch("https://example.com/post")
    assert e.value.cause == "robots_disallowed"


def test_a_stated_crawl_delay_is_honoured():
    """andrewchen.com asks for 10s. Ignoring it while claiming to respect robots.txt
    would be picking the convenient half of the file."""
    f = Fetcher(transport=_robots_transport(b"User-agent: *\nCrawl-delay: 10\n"), min_interval=1)
    f.fetch("https://andrewchen.com/a")
    assert f._crawl_delay("https://andrewchen.com/b") == 10.0


def test_our_floor_wins_when_the_site_asks_for_less():
    f = Fetcher(transport=_robots_transport(b"User-agent: *\nCrawl-delay: 0.1\n"), min_interval=1)
    f.fetch("https://example.com/a")
    assert f._crawl_delay("https://example.com/b") == 1.0
