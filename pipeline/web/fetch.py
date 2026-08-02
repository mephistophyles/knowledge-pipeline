"""HTTP fetching, with one instrumented escalation seam.

Everything that cannot be fetched plainly — JS-only pages, paywalls, robots-disallowed
paths, wrong content types — exits through `Escalation` and is COUNTED by cause. That is
deliberate: the choice between a headless browser and a submit form is deferred until the
monthly rate is known, and one exit point means whichever is chosen later slots in here
without touching anything upstream.

Politeness is not optional. A backlog scan is a bulk read of other people's servers, so it
honours robots.txt, waits between requests to the same host, and identifies itself.
"""
from __future__ import annotations

import hashlib
import html
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pipeline.web.canonical import canonicalize, resolve_canonical_link, site_of

USER_AGENT = (
    "mimir-knowledge-pipeline/0.1 (personal reading archive; "
    "+https://github.com/mephistophyles/knowledge-pipeline)"
)
ACCEPTED_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown")
MIN_HOST_INTERVAL = 1.0  # seconds between requests to one host

_CANONICAL_RE = re.compile(
    r"""<link[^>]+rel=["']?canonical["']?[^>]*>""", re.I)
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


class Escalation(Exception):
    """A page this path cannot handle. `cause` is what gets counted."""

    def __init__(self, cause: str, detail: str = ""):
        super().__init__(f"{cause}: {detail}" if detail else cause)
        self.cause = cause
        self.detail = detail


@dataclass
class Fetched:
    url: str                 # canonical URL (ours, or the page's same-site canonical)
    requested_url: str
    body: bytes              # RAW response bytes — what fetch_hash is taken over
    content_type: str
    http_status: int
    title: str | None = None
    syndicated_from: str | None = None   # set when the page declares a cross-site canonical
    headers: dict = field(default_factory=dict)

    @property
    def fetch_hash(self) -> str:
        """sha256 of the RAW bytes.

        Not of the extracted body: the extractor is the component most likely to change,
        and keying identity on its output would make every extractor tweak a corpus-wide
        identity reset. Same reasoning as `eml_hash`.
        """
        return hashlib.sha256(self.body).hexdigest()


class Fetcher:
    """Polite HTTP fetcher. `transport` is injectable so tests never touch the network."""

    def __init__(self, transport=None, *, respect_robots: bool = True, min_interval: float = MIN_HOST_INTERVAL):
        self._transport = transport
        self._respect_robots = respect_robots
        self._min_interval = min_interval
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # ── politeness ────────────────────────────────────────────────────────────
    def _wait(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            delay = self._min_interval - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
        self._last_hit[host] = time.monotonic()

    def _allowed(self, url: str) -> bool:
        if not self._respect_robots:
            return True
        host = site_of(url)
        if host not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            scheme = urlsplit(url).scheme or "https"
            parser.set_url(f"{scheme}://{host}/robots.txt")
            try:
                parser.read()
            except Exception:
                # An unreachable robots.txt is not permission to ignore it, but it is also
                # not a refusal; treat as permissive, which is the documented convention.
                parser = None
            self._robots[host] = parser
        parser = self._robots[host]
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    # ── the fetch ─────────────────────────────────────────────────────────────
    def _get(self, url: str):
        """`(status, headers, body)`.

        urllib RAISES HTTPError on 4xx/5xx instead of returning the status, so it is
        caught and converted here. Without this every paywall and dead link counted as
        `fetch_error`, collapsing two things the escalation rate needs to tell apart: a
        403 is a candidate for the snapshot path, a DNS failure is a broken link.
        """
        if self._transport is not None:
            return self._transport(url, {"User-Agent": USER_AGENT})
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read() or b""

    def fetch(self, url: str) -> Fetched:
        """Fetch one URL, or raise `Escalation` with a countable cause."""
        target = canonicalize(url)
        if not urlsplit(target).scheme.startswith("http"):
            raise Escalation("unsupported_scheme", target)
        if not self._allowed(target):
            raise Escalation("robots_disallowed", target)

        self._wait(site_of(target) or "")
        try:
            status, headers, body = self._get(target)
        except Escalation:
            raise
        except Exception as exc:
            raise Escalation("fetch_error", f"{type(exc).__name__}: {exc}") from exc

        if status >= 400:
            raise Escalation("http_error", str(status))

        ctype = (headers.get("Content-Type") or headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and not any(ctype.startswith(t) for t in ACCEPTED_TYPES):
            # PDFs are REJECTED rather than escalated: a different ingestor with different
            # extraction, not a web page we failed to read.
            cause = "pdf" if "pdf" in ctype else "unsupported_content_type"
            raise Escalation(cause, ctype)
        if not body:
            raise Escalation("empty_body", target)

        text = body.decode("utf-8", errors="replace")
        final_url, syndicated = resolve_canonical_link(target, _canonical_href(text))
        title = _title(text)
        return Fetched(
            url=final_url, requested_url=target, body=body, content_type=ctype or "text/html",
            http_status=status, title=title,
            syndicated_from=site_of(target) if syndicated else None,
            headers=headers,
        )


def _canonical_href(html_text: str) -> str | None:
    tag = _CANONICAL_RE.search(html_text)
    if not tag:
        return None
    href = _HREF_RE.search(tag.group(0))
    return href.group(1).strip() if href else None


def _title(html_text: str) -> str | None:
    m = _TITLE_RE.search(html_text)
    if not m:
        return None
    # Entities are decoded here rather than downstream: the title flows into the registry
    # and note frontmatter, and `Bezos&#x27;s` would be carried all the way to the vault.
    text = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
    return re.sub(r"\s+", " ", text).strip()[:300] or None
