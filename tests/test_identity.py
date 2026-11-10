"""Author identity: claims are anchored to the literal PERSON, not the channel they
arrived through. See web-ingestion-plan.md Part 1."""
from pipeline import authors
from pipeline.ingestors.email import ingest_message
from pipeline.ingestors.web import add_web
from pipeline.orchestrator.executor import run_stage
from pipeline.vault.writer import read_note

from .conftest import FakeMsg


def _walk(settings, conn, h):
    for stage in ("source_note", "extract_claims", "dedup"):
        run_stage(settings, conn, h, stage)


def _person(conn, display, *keys, kind="person", confidence="curated"):
    ident = f"{kind}:{authors.slug(display)}"
    authors.upsert_identity(conn, ident, display, kind=kind)
    for k in keys:
        authors.add_alias(conn, k, ident, confidence=confidence, source="test")
    return ident


# ── resolution ────────────────────────────────────────────────────────────────
def test_many_channels_resolve_to_one_person(conn):
    ident = _person(conn, "Ben Thompson", "email@stratechery.com", "stratechery.com")
    assert authors.identity_of(conn, "email@stratechery.com") == ident
    assert authors.identity_of(conn, "stratechery.com") == ident


def test_unmapped_channel_resolves_to_nothing(conn):
    assert authors.identity_of(conn, "nobody@example.com") is None


def test_proposed_aliases_are_not_trusted_until_confirmed(conn):
    """A harvest suggestion must not silently fuse two writers into one voice."""
    ident = _person(conn, "Ben Thompson", "email@stratechery.com", confidence="proposed")
    assert authors.identity_of(conn, "email@stratechery.com") is None
    assert authors.identity_of(conn, "email@stratechery.com", confirmed_only=False) == ident

    assert authors.confirm(conn, "email@stratechery.com")
    assert authors.identity_of(conn, "email@stratechery.com") == ident


def test_confirmation_survives_a_later_harvest(conn):
    """Re-running the seeder must not undo human curation."""
    ident = _person(conn, "Ben Thompson", "email@stratechery.com")
    authors.add_alias(conn, "email@stratechery.com", "person:someone-else", confidence="proposed")
    assert authors.identity_of(conn, "email@stratechery.com") == ident


def test_slug_handles_real_names(conn):
    assert authors.slug("Anne-Laure Le Cunff") == "anne-laure-le-cunff"
    assert authors.slug("CJ Gustafson") == "cj-gustafson"


# ── the reason this exists ────────────────────────────────────────────────────
def test_one_writer_on_two_channels_does_not_corroborate_themselves(settings, conn, fake_claims):
    """THE acceptance test: the same essay by email and by web is ONE voice.

    Without the identity layer `email@stratechery.com` and `stratechery.com` are two
    authors, so a single writer's piece arriving on both channels would read as two
    independent sources agreeing — manufacturing the exact signal attestation exists to
    protect."""
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    _person(conn, "Ben Thompson", "email@stratechery.com", "stratechery.com")

    fake_claims["text"] = '[{"claim": "Aggregators win by owning demand.", "quote": "q email"}]'
    a = ingest_message(
        settings, conn, FakeMsg(text="the edition", from_="Ben Thompson <email@stratechery.com>", subject="Aggregation")
    )
    _walk(settings, conn, a)

    fake_claims["text"] = '[{"claim": "Aggregators win by owning demand.", "quote": "q web"}]'
    fake_claims["same"] = True
    b = add_web(settings, conn, "the same essay on the site", url="https://stratechery.com/aggregation")
    _walk(settings, conn, b)

    claim_id = f"claim-{a[:8]}-00"
    post = read_note(settings.vault_dir / f"corpus/claims/{claim_id}.md")
    assert len(post.metadata["attestations"]) == 1
    row = conn.execute("SELECT attestations FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
    assert row["attestations"] == 1


def test_two_writers_at_one_venue_do_corroborate(settings, conn, fake_claims):
    """The inverse must still hold, or the fix would flatten a publication into one voice."""
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    _person(conn, "Writer One", "one@venue.com")
    _person(conn, "Writer Two", "two@venue.com")

    fake_claims["text"] = '[{"claim": "Distribution beats product.", "quote": "q1"}]'
    a = ingest_message(settings, conn, FakeMsg(text="ed one", from_="one@venue.com", subject="A"))
    _walk(settings, conn, a)

    fake_claims["text"] = '[{"claim": "Distribution beats product.", "quote": "q2"}]'
    fake_claims["same"] = True
    b = ingest_message(settings, conn, FakeMsg(text="ed two", from_="two@venue.com", subject="B"))
    _walk(settings, conn, b)

    row = conn.execute(
        "SELECT attestations FROM claims WHERE claim_id=?", (f"claim-{a[:8]}-00",)
    ).fetchone()
    assert row["attestations"] == 2


# ── provisional attestations ──────────────────────────────────────────────────
def test_unmapped_channel_attests_but_cannot_corroborate(settings, conn, fake_claims):
    """Non-blocking by design: the claim commits and the quote is visible, but an unknown
    channel cannot vouch — otherwise ingesting an unmapped source is indistinguishable
    from genuine independent support."""
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    _person(conn, "Known Writer", "known@example.com")

    fake_claims["text"] = '[{"claim": "Taste differentiates software.", "quote": "q known"}]'
    a = ingest_message(settings, conn, FakeMsg(text="ed", from_="known@example.com", subject="A"))
    _walk(settings, conn, a)

    fake_claims["text"] = '[{"claim": "Taste sets software apart.", "quote": "q unknown"}]'
    fake_claims["same"] = True
    b = ingest_message(settings, conn, FakeMsg(text="ed2", from_="stranger@example.com", subject="B"))
    _walk(settings, conn, b)

    claim_id = f"claim-{a[:8]}-00"
    post = read_note(settings.vault_dir / f"corpus/claims/{claim_id}.md")
    atts = post.metadata["attestations"]
    assert len(atts) == 2                    # recorded and visible
    assert "q unknown" in post.content
    assert atts[1]["provisional"] is True
    assert "(provisional)" in post.content
    row = conn.execute("SELECT attestations FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
    assert row["attestations"] == 1          # but it does not count as support


def test_known_channel_records_the_identity_on_the_attestation(settings, conn, fake_claims):
    ident = _person(conn, "Known Writer", "known@example.com")
    fake_claims["text"] = '[{"claim": "One claim.", "quote": "q"}]'
    h = ingest_message(settings, conn, FakeMsg(text="ed", from_="known@example.com", subject="A"))
    _walk(settings, conn, h)

    post = read_note(settings.vault_dir / f"corpus/claims/claim-{h[:8]}-00.md")
    att = post.metadata["attestations"][0]
    assert att["identity"] == ident
    assert att["author"] == "known@example.com"  # the channel is still recorded
    assert not att.get("provisional")
    assert ident in post.content                 # the person is what the note shows


def test_legacy_attestations_resolve_through_the_table(settings, conn, fake_claims):
    """Notes written before identities existed store only a channel key. Resolving the
    stored value at comparison time means curating an alias corrects every note that
    mentions it, with no migration."""
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    fake_claims["text"] = '[{"claim": "Taste differentiates software.", "quote": "q1"}]'
    a = ingest_message(settings, conn, FakeMsg(text="ed", from_="ben@stratechery.com", subject="A"))
    _walk(settings, conn, a)  # committed with NO identity — legacy shape

    _person(conn, "Ben Thompson", "ben@stratechery.com", "stratechery.com")

    fake_claims["text"] = '[{"claim": "Taste sets software apart.", "quote": "q2"}]'
    fake_claims["same"] = True
    b = add_web(settings, conn, "same essay", url="https://stratechery.com/post")
    _walk(settings, conn, b)

    post = read_note(settings.vault_dir / f"corpus/claims/claim-{a[:8]}-00.md")
    assert len(post.metadata["attestations"]) == 1  # recognised as the same writer
