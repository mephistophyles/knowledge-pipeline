"""URL canonicalization — deciding when two URLs are the same article.

Applied BEFORE anything is fetched or hashed. Without it the same essay arrives twice
under a tracking parameter and becomes two artifacts by one author, which reads as that
author corroborating themselves — the failure the identity layer exists to prevent,
re-entering through the URL instead of the byline.

Deliberately conservative. Only parameters known to be tracking are dropped: many sites
carry meaning in the query string (`?p=123`, `?page=2`), and stripping those would fuse
genuinely different articles — a far worse error than keeping a duplicate.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Analytics and campaign parameters. Present or absent, the page is the same page.
_TRACKING = {
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "igshid", "twclid",
    "mc_cid", "mc_eid", "ck_subscriber_id", "_hsenc", "_hsmi", "hsctatracking",
    "ref", "referrer", "source", "src", "share", "shared", "triedsignin",
    "utm_id", "utm_name", "utm_reader", "utm_brand", "utm_social", "utm_social-type",
}
_TRACKING_PREFIXES = ("utm_", "pk_", "piwik_", "matomo_", "at_", "vero_", "spm_")

# Sending platforms: a hostname here identifies the platform, never the writer.
SHARED_PLATFORMS = {
    "substack.com", "beehiiv.com", "medium.com", "ghost.io", "wordpress.com",
    "blogspot.com", "tumblr.com", "notion.site", "gmail.com",
}


def is_tracking_param(name: str) -> bool:
    n = name.lower()
    return n in _TRACKING or n.startswith(_TRACKING_PREFIXES)


def canonicalize(url: str) -> str:
    """Normalize a URL to the form used for identity and dedup.

    Lowercases scheme and host, drops `www.`, removes tracking parameters and the
    fragment, and normalizes the trailing slash. Remaining query parameters are sorted so
    that parameter ORDER cannot make one article look like two.
    """
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if parts.port and parts.port not in (80, 443):
        netloc = f"{host}:{parts.port}"

    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not is_tracking_param(k)
    ))

    path = re.sub(r"//+", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")          # /post/ and /post are one page
    if not path:
        path = "/"

    return urlunsplit((scheme, netloc, path, query, ""))  # fragment always dropped


def site_of(url: str | None) -> str | None:
    """Bare hostname — the publication facet, and the fallback identity alias."""
    if not url:
        return None
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def channel_key(url: str | None) -> str | None:
    """The finest STABLE key identifying who publishes at a URL.

    For an ordinary site the hostname is the channel — one blog, one voice. On a shared
    platform it is not: medium.com hosts everybody, so keying on the host would fuse every
    Medium writer into one identity. That is worse than the false-corroboration bug this
    layer exists to prevent, because it merges distinct PEOPLE rather than inflating a
    count.

    So on a platform the first path segment is included when it identifies a publisher:
    `medium.com/@stewart` is a stable author key and reusable for their next piece. A
    publication path (`medium.com/firm-narrative`) is a publication, not an author — still
    far better than the bare host, and the byline decides the rest.
    """
    if not url:
        return None
    host = site_of(url)
    if not host:
        return None
    if host not in SHARED_PLATFORMS:
        return host
    seg = next((s for s in urlsplit(url).path.split("/") if s), None)
    return f"{host}/{seg}" if seg else host


def registrable(host: str | None) -> str | None:
    """Approximate registrable domain: the last two labels.

    Used only to decide whether a `<link rel=canonical>` points somewhere else, where
    being slightly wrong on a multi-part TLD is harmless — it makes us treat a same-site
    canonical as cross-site and keep our own URL, which is the safe direction.
    """
    if not host:
        return None
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def resolve_canonical_link(fetched_url: str, canonical_href: str | None) -> tuple[str, bool]:
    """`(url, is_syndicated)` given the page's own `<link rel="canonical">`.

    A same-site canonical is authoritative and replaces ours. A CROSS-SITE canonical is
    not followed: it means this page is a republication, and quietly adopting the origin's
    URL would attribute the copy to the original site. Flagged instead, so syndication is
    a decision rather than a silent rewrite.
    """
    ours = canonicalize(fetched_url)
    if not canonical_href:
        return ours, False
    try:
        theirs = canonicalize(canonical_href if "://" in canonical_href else fetched_url)
    except Exception:
        return ours, False
    if not canonical_href.strip() or "://" not in canonical_href:
        return ours, False
    if registrable(site_of(theirs)) == registrable(site_of(ours)):
        return theirs, False
    return ours, True
