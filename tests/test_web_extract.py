"""Body extraction + the quality gate (build step 3).

The gate cannot tell a good extraction from a subtly truncated one — quote grounding
measures that downstream. It catches the LOUD failures before they cost an LLM call."""
import pytest

from pipeline import authors
from pipeline.web import derive as d
from pipeline.web import extract as ex


def _article(paragraphs=14, words=40, title="A Real Article", byline=None, extra_body=""):
    # Paragraphs must DIFFER: trafilatura deduplicates identical blocks, so a fixture of
    # repeated sentences extracts to nothing and tests the wrong thing.
    def para(i):
        filler = " ".join(f"detail{i}x{j}" for j in range(max(0, words - 22)))
        return (
            f"<p>Paragraph {i} makes a distinct point about how practitioners actually "
            f"acquire expertise over time, unlike textbook {i}. The mechanism is "
            f"repetition against feedback, not instruction. {filler}</p>"
        )
    body = "\n".join(para(i) for i in range(paragraphs))
    author_meta = f'<meta name="author" content="{byline}">' if byline else ""
    return (
        f"<html><head><title>{title}</title>{author_meta}</head>"
        f"<body><article>{body}{extra_body}</article></body></html>"
    ).encode()


# ── extraction ────────────────────────────────────────────────────────────────
def test_extracts_body_and_title():
    got = ex.extract(_article())
    assert got.ok, got.holds
    assert got.word_count >= ex.MIN_WORDS
    assert "Paragraph 0" in got.text and "detail13x" in got.text  # first and last survive


def test_boilerplate_outside_the_article_is_not_extracted():
    """A quote lifted from a 'related posts' teaser would be attributed to an article that
    never said it — the failure the manual v1 ingestor existed to avoid."""
    html = _article().replace(
        b"</article>",
        b"</article><aside><h2>You might also like</h2>"
        b"<p>UNRELATED_TEASER_SENTENCE about something else entirely.</p></aside>",
    )
    got = ex.extract(html)
    assert got.ok
    assert "UNRELATED_TEASER" not in got.text


def test_comments_are_excluded():
    """A commenter is not the author."""
    html = _article().replace(
        b"</article>",
        b"</article><div class='comments'><p>COMMENTER_OPINION goes here.</p></div>",
    )
    got = ex.extract(html)
    assert "COMMENTER_OPINION" not in got.text


def test_empty_page_is_held():
    got = ex.extract(b"<html><body></body></html>")
    assert not got.ok and "extraction_empty" in got.holds


# ── the gate ──────────────────────────────────────────────────────────────────
def test_a_teaser_is_held_as_too_short():
    """The floor doubles as the paywall-stub detector: a teaser extracts cleanly and is
    simply short."""
    got = ex.extract(_article(paragraphs=2, words=20))
    assert not got.ok and "too_short" in got.holds


def test_an_oversized_page_is_held():
    got = ex.assess(ex.Extracted(text="w " * 20_000, word_count=20_000))
    assert "too_long" in got


def test_non_english_is_held():
    got = ex.assess(ex.Extracted(text="x", word_count=500, language="fr"))
    assert "non_en" in got


def test_an_archive_index_is_held_not_processed():
    """Every headline on a listing page would otherwise become a claim attributed to the
    author."""
    listing = "\n".join(f"Some Post Title Number {i}" for i in range(40))
    assert ex.looks_like_an_index(listing)
    assert "index_page" in ex.assess(ex.Extracted(text=listing, word_count=280))


def test_real_prose_is_not_mistaken_for_an_index():
    prose = "\n".join(
        "This is a full sentence of real prose that continues for a while and then ends."
        for _ in range(20)
    )
    assert not ex.looks_like_an_index(prose)


def test_short_documents_are_never_called_an_index():
    assert not ex.looks_like_an_index("One line\nTwo line\nThree line")


# ── deriving into the corpus ──────────────────────────────────────────────────
def _archive(settings, conn, url, html, title="T"):
    from pipeline.web.fetch import Fetched
    from pipeline.web.ledger import archive

    h, _ = archive(settings, conn, Fetched(url=url, requested_url=url, body=html,
                                           content_type="text/html", http_status=200, title=title))
    return conn.execute("SELECT * FROM web_backlog WHERE fetch_hash=?", (h,)).fetchone()


def test_derive_creates_an_artifact_and_queues_the_chain(settings, conn):
    authors.upsert_identity(conn, "org:commoncog", "Commoncog", kind="org")
    authors.add_alias(conn, "commoncog.com", "org:commoncog", confidence="curated")
    row = _archive(settings, conn, "https://commoncog.com/post", _article())

    r = d.derive_one(settings, conn, row)

    assert r["state"] == "ingested"
    assert r["identity_id"] == "org:commoncog"
    job = conn.execute("SELECT stage, status FROM jobs WHERE artifact_hash=?", (r["artifact_hash"],)).fetchone()
    assert job["status"] == "ready"
    led = conn.execute("SELECT state, artifact_hash FROM web_backlog WHERE url=?",
                       ("https://commoncog.com/post",)).fetchone()
    assert led["state"] == "ingested" and led["artifact_hash"] == r["artifact_hash"]


def test_a_held_page_creates_no_artifact(settings, conn):
    """A bad extraction costs an LLM call and lands in the vault, so it must not proceed."""
    row = _archive(settings, conn, "https://example.com/teaser", _article(paragraphs=1, words=10))

    r = d.derive_one(settings, conn, row)

    assert r["state"] == "held" and "too_short" in r["reason"]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    led = conn.execute("SELECT state FROM web_backlog WHERE url=?", ("https://example.com/teaser",)).fetchone()
    assert led["state"] == "held"


def test_a_resolvable_byline_beats_the_hostname(settings, conn):
    """A guest post is written by the guest; resolving by hostname would credit the host."""
    authors.upsert_identity(conn, "org:a16z", "a16z", kind="org")
    authors.add_alias(conn, "a16z.com", "org:a16z", confidence="curated")
    authors.upsert_identity(conn, "person:jane-guest", "Jane Guest")
    authors.add_alias(conn, "jane guest", "person:jane-guest", confidence="curated")

    identity, byline = d.author_for(conn, "https://a16z.com/post", "Jane Guest")
    assert identity == "person:jane-guest" and byline == "Jane Guest"


def test_an_unknown_byline_falls_back_to_the_hostname(settings, conn):
    authors.upsert_identity(conn, "org:a16z", "a16z", kind="org")
    authors.add_alias(conn, "a16z.com", "org:a16z", confidence="curated")

    identity, _ = d.author_for(conn, "https://a16z.com/post", "Someone Unknown")
    assert identity == "org:a16z"


def test_an_unmapped_page_still_derives_but_unmapped(settings, conn):
    row = _archive(settings, conn, "https://nobody.example/post", _article())
    r = d.derive_one(settings, conn, row)
    assert r["state"] == "ingested" and r["identity_id"] is None


def test_derive_counts_outcomes(settings, conn):
    _archive(settings, conn, "https://a.com/good", _article())
    _archive(settings, conn, "https://a.com/teaser", _article(paragraphs=1, words=10))

    counts = d.derive(settings, conn)
    assert counts["ingested"] == 1 and counts["held"] == 1


# ── trailing site furniture ───────────────────────────────────────────────────
def test_a_subscribe_pitch_is_trimmed_from_the_tail():
    """A marketing line becomes a CLAIM attributed to the author otherwise: '9,000+
    investors read Commoncog' would look like an insight they had."""
    body = "\n".join(f"Real paragraph {i} with genuine argument and a conclusion." for i in range(30))
    text = body + (
        "\nOriginally published , last updated ."
        "\nThis article is part of the Operations topic cluster."
        "\nThe thought of business school make you go 'eww'?"
        "\nYou're in good company."
        "\n9,000+ investors and operators read Commoncog to sharpen their business acumen."
        "\nSign up for our newsletter and get a weekly dose of good business thinking:"
    )
    out, removed = ex.trim_promotional_tail(text)

    assert removed > 0
    assert "Sign up for our newsletter" not in out
    assert "9,000+ investors" not in out
    assert "Originally published" not in out
    assert out.strip().endswith("Real paragraph 29 with genuine argument and a conclusion.")


def test_the_article_body_is_never_touched():
    body = "\n".join(f"Real paragraph {i}." for i in range(30))
    out, removed = ex.trim_promotional_tail(body)
    assert (out, removed) == (body, 0)


def test_a_long_closing_section_is_left_alone():
    """Cutting a genuine conclusion is worse than leaving a footer — nothing downstream
    can tell that something went missing."""
    body = "\n".join(f"Paragraph {i}." for i in range(30))
    long_tail = "\n".join(
        f"Subscribe to this idea is discussed at length here in sentence {i} of a real section."
        for i in range(20)
    )
    out, removed = ex.trim_promotional_tail(body + "\n" + long_tail)
    assert removed == 0 and out.endswith(long_tail)


def test_promo_words_deep_in_the_article_are_not_furniture():
    """Only the trailing window is considered; mid-article these are ordinary prose."""
    text = "Readers subscribe to newsletters for many reasons.\n" + "\n".join(
        f"Paragraph {i} continues the argument at length." for i in range(40)
    )
    out, removed = ex.trim_promotional_tail(text)
    assert removed == 0 and out == text
