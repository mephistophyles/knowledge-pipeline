"""Pre-backlog site audit: report what a site publishes about how it wants to be read."""
from pipeline.web import audit as au
from pipeline.web.fetch import Fetcher

ROBOTS = b"User-agent: *\nDisallow: /admin/\nCrawl-delay: 5\nSitemap: https://example.com/sitemap.xml\n"
HOME = b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed"></head><body>hi</body></html>'
SITEMAP = b"<urlset><url><loc>https://example.com/a</loc></url><url><loc>https://example.com/b</loc></url></urlset>"


def _transport(terms_html=b"", pages=None):
    pages = pages or {}
    def t(url, headers):
        for suffix, body in pages.items():
            if url.endswith(suffix):
                return 200, {"Content-Type": "text/html"}, body
        if url.endswith("/robots.txt"):
            return 200, {}, ROBOTS
        if url.endswith("/sitemap.xml"):
            return 200, {}, SITEMAP
        if url.endswith("/terms"):
            return (200, {}, terms_html) if terms_html else (404, {}, b"")
        if url.rstrip("/").endswith("example.com"):
            return 200, {"Content-Type": "text/html"}, HOME
        return 404, {}, b""
    return t


def _fetcher(transport):
    return Fetcher(transport=transport, min_interval=0)


def test_reports_robots_sitemaps_feeds_and_delay():
    a = au.audit_site("https://example.com", fetcher=_fetcher(_transport()))
    assert a.allowed is True
    assert a.crawl_delay == 5
    assert a.sitemaps == ["https://example.com/sitemap.xml"]
    assert a.sitemap_urls == 2
    assert a.feeds == ["https://example.com/feed"]


def test_a_bare_host_without_a_scheme_is_audited_at_the_site_root():
    """`example.com/blog` has no scheme, so urlsplit finds no hostname and every URL is
    built against a path — silently auditing /blog/robots.txt and reporting its 404."""
    a = au.audit_site("example.com/blog", fetcher=_fetcher(_transport()))
    assert a.host == "example.com"
    assert a.sitemaps == ["https://example.com/sitemap.xml"]


def test_terms_language_is_flagged_with_its_surrounding_sentence():
    terms = (b"<html><body><p>" + b"Ordinary preamble sentence. " * 30 +
             b"You may not use the service in any way that "
             b"crawls, scrapes, or spiders any page through automated means."
             b"</p></body></html>")
    a = au.audit_site("https://example.com", fetcher=_fetcher(_transport(terms_html=terms)))
    assert "automated means" in a.terms_flags
    assert a.terms_snippets and "spiders any page" in a.terms_snippets[0]


def test_inline_scripts_do_not_masquerade_as_terms_language():
    """Reforge's page carries /bot|crawl|spider/.test(navigator.userAgent) in inline JS. A
    false positive here is worse than a miss — it talks you out of a site that permits you."""
    terms = (b"<html><body><script>if(/bot|crawl|spider/iu.test(navigator.userAgent)){}</script>"
             b"<p>Ordinary terms text with nothing of interest, repeated to clear the length "
             b"floor. " + b"Filler sentence. " * 40 + b"</p></body></html>")
    a = au.audit_site("https://example.com", fetcher=_fetcher(_transport(terms_html=terms)))
    assert a.terms_flags == []


def test_a_sitemap_index_is_reported_as_an_index_not_a_post_count():
    index = b"<sitemapindex><sitemap><loc>https://example.com/s1.xml</loc></sitemap></sitemapindex>"
    a = au.audit_site("https://example.com", fetcher=_fetcher(_transport(pages={"/sitemap.xml": index})))
    assert a.sitemap_urls is None
    assert any("index of" in n for n in a.notes)
