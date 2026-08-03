"""Audit a site BEFORE ingesting its backlog.

One-off articles and whole-archive ingestion are different asks, and the second deserves
checking first. This reports what a site publishes about how it wants to be read:

  - robots.txt, as it applies to US, plus any Crawl-delay it asks for
  - the sitemaps and feeds it PUBLISHES — a site's own index is sanctioned enumeration,
    and far better citizenship than crawling its link graph to discover posts
  - its terms page, and any language about automated access

It renders findings; it does not render verdicts. Whether a given site's terms permit a
personal reading archive is a judgment for the person doing it, and a keyword scan of
legal text is evidence, not advice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pipeline.web.canonical import canonicalize, site_of
from pipeline.web.fetch import USER_AGENT, Escalation, Fetcher

# Phrases worth reading in full if present. Deliberately broad — a hit means "go look",
# never "this is forbidden".
_TERMS_FLAGS = re.compile(
    r"\b(scrap(e|ing)|crawl(er|ing)?|spider|robot|automated (access|means|tools?|systems?)|"
    r"data ?mining|text ?mining|harvest(ing)?|bulk (download|access)|"
    r"machine learning|train(ing)? (an? )?(ai|model)|artificial intelligence)\b",
    re.I,
)
_TERMS_PATHS = ("/terms", "/terms-of-service", "/terms-of-use", "/tos", "/legal", "/policies")
_FEED_RE = re.compile(
    r"""<link[^>]+type=["']application/(?:rss\+xml|atom\+xml|feed\+json)["'][^>]*>""", re.I)
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


@dataclass
class Audit:
    host: str
    robots_status: int | None = None
    robots_body: str = ""
    allowed: bool | None = None
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    sitemap_urls: int | None = None      # rough backlog size, when a sitemap is readable
    terms_url: str | None = None
    terms_flags: list[str] = field(default_factory=list)
    terms_snippets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _get(fetcher: Fetcher, url: str) -> tuple[int, bytes] | None:
    try:
        status, _headers, body = fetcher._get(url)
        return status, body
    except Exception:
        return None


def _discover_feeds(html: str, base: str) -> list[str]:
    out = []
    for tag in _FEED_RE.findall(html):
        m = _HREF_RE.search(tag)
        if not m:
            continue
        href = m.group(1)
        out.append(href if "://" in href else base.rstrip("/") + "/" + href.lstrip("/"))
    # Conventional paths cost nothing to name and are often undeclared.
    return list(dict.fromkeys(out))


def audit_site(url: str, *, fetcher: Fetcher | None = None, count_sitemap: bool = True) -> Audit:
    """Gather what one site publishes about being read."""
    # A bare `example.com/blog` has no scheme, so urlsplit finds no hostname and every
    # subsequent URL is built against a path instead of the site root — which silently
    # audits `https://example.com/blog/robots.txt` and reports its 404 as fact.
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    host = site_of(url)
    if not host:
        return Audit(host=url, notes=["could not parse a hostname from this input"])
    base = f"https://{host}"
    fetcher = fetcher or Fetcher(min_interval=0.5)
    a = Audit(host=host)

    got = _get(fetcher, f"{base}/robots.txt")
    if got:
        a.robots_status, raw = got
        if a.robots_status and a.robots_status < 400:
            a.robots_body = raw.decode("utf-8", errors="replace")
            a.sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", a.robots_body)
    else:
        a.notes.append("robots.txt unreachable")

    sample = canonicalize(url if url.count("/") > 2 else f"{base}/a-representative-post")
    try:
        a.allowed = fetcher._allowed(sample)
        a.crawl_delay = fetcher._crawl_delay(sample)
    except Exception as e:  # pragma: no cover - defensive
        a.notes.append(f"robots evaluation failed: {e}")

    home = _get(fetcher, base + "/")
    if home and home[0] < 400:
        html = home[1].decode("utf-8", errors="replace")
        a.feeds = _discover_feeds(html, base)
    else:
        a.notes.append("homepage not readable for feed discovery")

    if count_sitemap and a.sitemaps:
        sm = _get(fetcher, a.sitemaps[0])
        if sm and sm[0] < 400:
            body = sm[1].decode("utf-8", errors="replace")
            locs = _LOC_RE.findall(body)
            # A sitemap index points at more sitemaps rather than pages; say so instead of
            # reporting "12 posts" for a site with thousands.
            if "<sitemapindex" in body.lower():
                a.notes.append(f"sitemap is an index of {len(locs)} sitemaps")
            else:
                a.sitemap_urls = len(locs)

    for path in _TERMS_PATHS:
        got = _get(fetcher, base + path)
        if got and got[0] < 400 and len(got[1]) > 500:
            a.terms_url = base + path
            # Script and style bodies must go BEFORE tags are stripped: Reforge's page
            # carries `/bot|crawl|spider/.test(navigator.userAgent)` in inline JS, which
            # otherwise reads as a terms clause about crawling. A false positive here is
            # worse than a miss — it would talk you out of a site that permits you.
            html_text = got[1].decode("utf-8", errors="replace")
            html_text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html_text)
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text))
            a.terms_flags = sorted({m.group(0).lower() for m in _TERMS_FLAGS.finditer(text)})
            # Keep the surrounding sentence for each hit. A keyword alone says nothing
            # about whether a clause permits or forbids — the sentence is the evidence.
            for m in _TERMS_FLAGS.finditer(text):
                start, end = max(0, m.start() - 180), min(len(text), m.end() + 180)
                snippet = text[start:end].strip()
                if snippet not in a.terms_snippets:
                    a.terms_snippets.append(snippet)
                if len(a.terms_snippets) >= 4:
                    break
            break
    if not a.terms_url:
        a.notes.append("no terms page found at the usual paths")
    return a


def audit_many(urls: list[str], *, fetcher: Fetcher | None = None) -> list[Audit]:
    fetcher = fetcher or Fetcher(min_interval=0.5)
    return [audit_site(u, fetcher=fetcher) for u in urls]
