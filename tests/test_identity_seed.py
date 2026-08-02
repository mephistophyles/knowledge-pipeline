"""Harvesting identity proposals from From-header display names.

Every case below is a real shape from the 57-author backlog. The classifier's job is not
to be always right — it is to be right where the header actually names the writer, and to
ABSTAIN where it doesn't, so the abstentions become a short human review list rather than
a long list of confident errors."""
import pytest

from pipeline import authors, identity_seed as seed


# ── shapes that yield a person ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "display,expected",
    [
        ("Ben Thompson", "Ben Thompson"),
        ("CJ Gustafson from Mostly Metrics", "CJ Gustafson"),
        ("Gregor Ojstersek from Engineering Leadership", "Gregor Ojstersek"),
        ("Anne-Laure Le Cunff", "Anne-Laure Le Cunff"),
        ("Roger L. Martin", "Roger L. Martin"),
        ("Adam Grant, Granted", "Adam Grant"),              # venue appended after a comma
        ("Sebastian Raschka, PhD", "Sebastian Raschka"),     # credential suffix
        ("Release Notes by Jake MccGwire", "Jake MccGwire"),  # venue leads, writer trails
    ],
)
def test_person_shapes(display, expected):
    person, kind, _ = seed.classify(display)
    assert (person, kind) == (expected, "person")


# ── shapes where the writer is genuinely absent ───────────────────────────────
@pytest.mark.parametrize(
    "display",
    [
        "Department of Product",
        "Rationality Newsletter",
        "The Bottleneck",
        "The Founders Corner",
        "The Pragmatic Engineer",
        "Elena's Growth Scoop",   # publication named after its writer — not their name
        "Cedric",                 # single token: first name or brand, unknowable
        "Sangram",
        "",
    ],
)
def test_abstains_when_the_header_names_a_venue(display):
    person, kind, reason = seed.classify(display)
    assert person is None and kind == "org"
    assert reason  # always says why, so the review list is actionable


def test_role_address_as_display_name_is_not_a_person():
    person, _, _ = seed.classify("mephistophyles@gmail.com")
    assert person is None


# ── header decoding ───────────────────────────────────────────────────────────
def test_folded_headers_are_unfolded():
    """RFC 5322 folding arrives as an embedded newline and would otherwise become part
    of the name."""
    assert seed.display_name_of("Sebastian Raschka,\n PhD <s@example.com>") == "Sebastian Raschka, PhD"


def test_display_name_strips_address_and_quotes():
    assert seed.display_name_of('"Ben Thompson" <email@stratechery.com>') == "Ben Thompson"
    assert seed.display_name_of("email@stratechery.com") == "email@stratechery.com"


# ── proposals are never trusted on their own ──────────────────────────────────
def test_applied_proposals_are_unconfirmed_and_cannot_corroborate(conn):
    p = seed.Proposal("email@stratechery.com", "Ben Thompson", "Ben Thompson", "person", 207, "bare person name")
    assert seed.apply(conn, [p]) == 1

    assert authors.identity_of(conn, "email@stratechery.com") is None          # not trusted
    assert authors.identity_of(conn, "email@stratechery.com", confirmed_only=False) == p.identity_id
    assert p.identity_id == "person:ben-thompson"


def test_apply_never_overwrites_a_curated_row(conn):
    """Re-running the harvest must not undo human review."""
    authors.upsert_identity(conn, "person:ben-thompson", "Ben Thompson")
    authors.add_alias(conn, "email@stratechery.com", "person:ben-thompson", confidence="curated")

    wrong = seed.Proposal("email@stratechery.com", "Stratechery", None, "org", 207, "venue")
    seed.apply(conn, [wrong])

    assert authors.identity_of(conn, "email@stratechery.com") == "person:ben-thompson"


# ── the curated record ────────────────────────────────────────────────────────
SHARED_PLATFORMS = {
    "substack.com", "beehiiv.com", "gmail.com", "ghost.io", "mailchimp.com",
    "convertkit.com", "outlook.com", "googlemail.com", "mail.beehiiv.com",
}


def _curation():
    import yaml
    from pathlib import Path
    return yaml.safe_load(Path("config/identities.yaml").read_text())["identities"]


def test_no_alias_is_claimed_by_two_identities():
    """A contested alias would make resolution depend on row order — one writer's claims
    would attribute differently run to run."""
    seen = {}
    for e in _curation():
        for a in e.get("aliases") or []:
            assert a not in seen, f"{a} claimed by {seen.get(a)} and {e['id']}"
            seen[a] = e["id"]


def test_shared_platforms_are_never_aliased():
    """substack.com pointing at one person would make every Substack writer that person."""
    for e in _curation():
        for a in e.get("aliases") or []:
            assert a.lower() not in SHARED_PLATFORMS, f"{e['id']} claims the platform {a}"


def test_every_identity_has_an_id_name_and_kind():
    for e in _curation():
        assert e["id"].startswith(("person:", "org:"))
        assert e["name"] and e["kind"] in {"person", "org"}
        assert e["id"].split(":", 1)[0] == e["kind"]


def test_sync_writes_curated_rows(settings, conn, tmp_path):
    import yaml
    from pipeline import identity_seed as s

    f = tmp_path / "ids.yaml"
    f.write_text(yaml.safe_dump({"identities": [
        {"id": "person:cj-gustafson", "name": "CJ Gustafson", "kind": "person",
         "aliases": ["mostlymetrics@mail.mostlymetrics.com", "lookingforleverage@mail.beehiiv.com",
                     "mostlymetrics.com"]},
    ]}))
    n_id, n_alias, conflicts = s.sync(settings, conn, str(f))

    assert (n_id, n_alias, conflicts) == (1, 3, [])
    # One author, however many publications — the whole point.
    for key in ("mostlymetrics@mail.mostlymetrics.com", "lookingforleverage@mail.beehiiv.com",
                "mostlymetrics.com"):
        assert authors.identity_of(conn, key) == "person:cj-gustafson"


def test_sync_reports_a_contested_alias_instead_of_applying_it(settings, conn, tmp_path):
    import yaml
    from pipeline import identity_seed as s

    f = tmp_path / "ids.yaml"
    f.write_text(yaml.safe_dump({"identities": [
        {"id": "person:a", "name": "A", "kind": "person", "aliases": ["shared@example.com"]},
        {"id": "person:b", "name": "B", "kind": "person", "aliases": ["shared@example.com"]},
    ]}))
    _, _, conflicts = s.sync(settings, conn, str(f))

    assert len(conflicts) == 1 and "shared@example.com" in conflicts[0]
    assert authors.identity_of(conn, "shared@example.com") == "person:a"  # first wins, not last


def test_sync_is_idempotent(settings, conn, tmp_path):
    import yaml
    from pipeline import identity_seed as s

    f = tmp_path / "ids.yaml"
    f.write_text(yaml.safe_dump({"identities": [
        {"id": "org:commoncog", "name": "Commoncog", "kind": "org",
         "aliases": ["newsletter@commoncog.com", "commoncog.com"]},
    ]}))
    assert s.sync(settings, conn, str(f)) == s.sync(settings, conn, str(f))
