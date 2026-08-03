"""Body extraction, and the gate that decides whether to trust it.

Extraction is the part most likely to silently corrupt provenance: a quote lifted from a
"you might also like" teaser gets attributed to an article that never said it. So the
settings favour PRECISION over recall — losing a paragraph is recoverable, attributing
someone else's sentence is not — comments are excluded (a commenter is not the author),
and everything that looks wrong is held rather than processed.

The gate is cheap and structural. It cannot tell a good extraction from a subtly truncated
one; that is what quote grounding measures downstream, on the claims themselves. What it
CAN do is catch the loud failures — a nav-only scrape, a swallowed archive index, a teaser
— before they cost an LLM call and land in the vault.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Constraints from web-ingestion-plan.md. The floor doubles as the teaser detector: pilot
# newsletter editions average ~1,635 words, and a paywalled stub is under 300.
MIN_WORDS = 250
MAX_WORDS = 15_000
LANGUAGE = "en"


@dataclass
class Extracted:
    text: str = ""
    title: str | None = None
    author: str | None = None          # byline, when the page states one
    date: str | None = None
    sitename: str | None = None
    language: str | None = None
    word_count: int = 0
    trimmed_words: int = 0                           # site furniture removed from the tail
    holds: list[str] = field(default_factory=list)   # gate failures, empty = usable

    @property
    def ok(self) -> bool:
        return not self.holds


def extract(html: str | bytes, url: str | None = None) -> Extracted:
    """Pull the article body and metadata out of a saved or fetched page."""
    import json

    import trafilatura

    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")

    # `extract` rather than `bare_extraction`: the latter returns a Document whose body is
    # still an lxml tree and never applies `output_format`, so its text comes back empty.
    # JSON gets the serialized text AND the metadata in one pass.
    raw = trafilatura.extract(
        html,
        url=url,
        favor_precision=True,     # a lost paragraph beats a borrowed sentence
        include_comments=False,   # a commenter is not the author
        include_tables=True,
        include_images=False,
        include_links=False,      # link text inflates word counts and adds no prose
        with_metadata=True,
        output_format="json",
    )
    if not raw:
        return Extracted(holds=["extraction_empty"])
    try:
        doc = json.loads(raw)
    except ValueError:
        return Extracted(holds=["extraction_empty"])

    def _field(name):
        v = doc.get(name)
        return v if v not in (None, "", "None") else None   # trafilatura stringifies None

    text = (doc.get("text") or "").strip()
    text, trimmed = trim_promotional_tail(text)
    got = Extracted(
        text=text,
        trimmed_words=trimmed,
        title=_field("title"),
        author=_field("author"),
        date=_field("date"),
        sitename=_field("sitename") or _field("hostname"),
        language=_field("language"),
        word_count=len(text.split()),
    )
    got.holds = assess(got)
    return got


# Site furniture that trafilatura leaves attached to the END of an article: the subscribe
# pitch, the topic-cluster nav, the published-date line. Mid-article these words are
# ordinary prose, which is why they are only ever matched in the trailing window.
_PROMO_TAIL = re.compile(
    r"sign up for (our|the) newsletter|subscribe to|get a weekly dose|"
    r"you'?re in good company|originally published|last updated|"
    r"this (article|post) is part of|read more from this topic|"
    r"all rights reserved|©\s*\d{4}|"
    r"\d[\d,]*\+?\s+(readers|subscribers|investors|operators|people)\s+(read|follow|subscribe)",
    re.I,
)
_TAIL_WINDOW = 25       # lines from the end that may be considered furniture
_MAX_TRIM_WORDS = 200   # a real closing section is longer than this


def trim_promotional_tail(text: str) -> tuple[str, int]:
    """Strip a trailing block of site furniture. Returns `(text, words_removed)`.

    Worth doing even though it is a small fraction of the page: a subscribe pitch is
    exactly the kind of confident sentence that becomes a CLAIM attributed to the author.
    "9,000+ investors and operators read Commoncog" is a marketing line, and in the vault
    it would look like an insight the writer had.

    Deliberately timid — only the trailing window, only a small block, and it stops at the
    first line above the furniture. Cutting a genuine conclusion is worse than leaving a
    footer, because the gate downstream cannot tell that anything went missing.
    """
    lines = text.splitlines()
    start = max(0, len(lines) - _TAIL_WINDOW)
    cut = None
    for i in range(len(lines) - 1, start - 1, -1):
        if _PROMO_TAIL.search(lines[i]):
            cut = i
    if cut is None:
        return text, 0
    removed = "\n".join(lines[cut:])
    n = len(removed.split())
    if n > _MAX_TRIM_WORDS:
        return text, 0
    return "\n".join(lines[:cut]).rstrip(), n


def looks_like_an_index(text: str) -> bool:
    """A listing page — archive, tag page, "related posts" — rather than an article.

    Index pages survive extraction as many short lines and almost no prose. Judged on the
    SHAPE of the text because the alternative (trusting length alone) lets a long archive
    page through, and every headline on it becomes a claim attributed to the author.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 8:
        return False
    short = sum(1 for ln in lines if len(ln) < 60)
    sentences = sum(1 for ln in lines if re.search(r"[.!?]['\"]?$", ln))
    return short / len(lines) > 0.75 and sentences / len(lines) < 0.25


def assess(got: Extracted) -> list[str]:
    """Reasons to hold this extraction, in the order they are worth reporting."""
    holds: list[str] = []
    if not got.text:
        return ["extraction_empty"]
    if got.word_count < MIN_WORDS:
        # Also the teaser detector: a paywalled stub extracts cleanly and is simply short.
        holds.append("too_short")
    if got.word_count > MAX_WORDS:
        holds.append("too_long")
    if got.language and got.language != LANGUAGE:
        holds.append(f"non_{LANGUAGE}")
    if looks_like_an_index(got.text):
        holds.append("index_page")
    return holds
