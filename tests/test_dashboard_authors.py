"""The unmapped-authors queue: where human judgement on attribution actually happens.

A queue, not a gate — unmapped sources still ingest, their attestations are just
provisional. So it can sit for a fortnight without blocking anything."""
import yaml
from fastapi.testclient import TestClient

from dashboard import app as dash
from pipeline import authors, identity_seed
from pipeline.web.fetch import Fetched
from pipeline.web.ledger import archive


def _client(settings, monkeypatch):
    monkeypatch.setattr(dash.Settings, "load", staticmethod(lambda: settings))
    return TestClient(dash.app)


def _page(settings, conn, url, title="A Post"):
    archive(settings, conn, Fetched(url=url, requested_url=url, body=b"<html>x</html>",
                                    content_type="text/html", http_status=200, title=title))


def test_unmapped_hosts_appear_in_the_queue(settings, conn):
    _page(settings, conn, "https://andrewchen.com/psychd-funnel")
    rows = dash.unmapped_authors(conn)
    assert [r["key"] for r in rows] == ["andrewchen.com"]
    assert rows[0]["n"] == 1


def test_a_curated_host_leaves_the_queue(settings, conn):
    _page(settings, conn, "https://andrewchen.com/x")
    authors.upsert_identity(conn, "person:andrew-chen", "Andrew Chen")
    authors.add_alias(conn, "andrewchen.com", "person:andrew-chen", confidence="curated")
    assert dash.unmapped_authors(conn) == []


def test_a_proposed_alias_does_not_count_as_mapped(settings, conn):
    """Only human-confirmed rows clear the queue; a harvest guess is what needs review."""
    _page(settings, conn, "https://andrewchen.com/x")
    authors.upsert_identity(conn, "person:andrew-chen", "Andrew Chen")
    authors.add_alias(conn, "andrewchen.com", "person:andrew-chen", confidence="proposed")
    assert [r["key"] for r in dash.unmapped_authors(conn)] == ["andrewchen.com"]


def test_name_suggestion_is_a_starting_point(settings):
    assert dash._suggest_name("andrewchen.com") == "Andrewchen"
    assert dash._suggest_name("newsletter.pragmaticengineer.com") == "Pragmaticengineer"
    assert dash._suggest_name("jason@asmartbear.com") == "Asmartbear"


# ── the form ──────────────────────────────────────────────────────────────────
def test_saving_a_new_author_writes_db_and_the_canonical_file(settings, conn, monkeypatch, tmp_path):
    _page(settings, conn, "https://andrewchen.com/x")
    client = _client(settings, monkeypatch)

    r = client.post("/authors/map",
                    data={"alias": "andrewchen.com", "identity_id": "", "name": "Andrew Chen",
                          "kind": "person"}, follow_redirects=False)
    assert r.status_code == 303

    assert authors.identity_of(conn, "andrewchen.com") == "person:andrew-chen"
    entries = yaml.safe_load((settings.root / "config/identities.yaml").read_text())["identities"]
    entry = next(e for e in entries if e["id"] == "person:andrew-chen")
    assert "andrewchen.com" in entry["aliases"]


def test_attaching_a_second_source_keeps_one_author(settings, conn, monkeypatch):
    """The Andrew Chen case: one writer on his own site, a firm's blog, and as a guest."""
    _page(settings, conn, "https://andrewchen.com/x")
    _page(settings, conn, "https://a16z.com/y")
    client = _client(settings, monkeypatch)

    client.post("/authors/map", data={"alias": "andrewchen.com", "identity_id": "",
                                      "name": "Andrew Chen", "kind": "person"})
    client.post("/authors/map", data={"alias": "a16z.com",
                                      "identity_id": "person:andrew-chen", "name": "", "kind": "person"})

    assert authors.identity_of(conn, "a16z.com") == "person:andrew-chen"
    entries = yaml.safe_load((settings.root / "config/identities.yaml").read_text())["identities"]
    ids = [e["id"] for e in entries]
    assert ids.count("person:andrew-chen") == 1          # one entry, two aliases
    entry = next(e for e in entries if e["id"] == "person:andrew-chen")
    assert set(entry["aliases"]) == {"andrewchen.com", "a16z.com"}


def test_saving_backfills_rows_already_in_the_ledger(settings, conn, monkeypatch):
    """Otherwise the backlog that prompted the decision stays unmapped, which reads as the
    form not having worked."""
    _page(settings, conn, "https://andrewchen.com/x")
    client = _client(settings, monkeypatch)

    client.post("/authors/map", data={"alias": "andrewchen.com", "identity_id": "",
                                      "name": "Andrew Chen", "kind": "person"})

    row = conn.execute("SELECT identity_id FROM web_backlog WHERE site='andrewchen.com'").fetchone()
    assert row["identity_id"] == "person:andrew-chen"
    assert dash.unmapped_authors(conn) == []


def test_moving_an_alias_removes_it_from_the_previous_owner(settings, conn, monkeypatch):
    """An alias claimed twice would make attribution depend on row order."""
    _page(settings, conn, "https://shared.example/x")
    client = _client(settings, monkeypatch)
    client.post("/authors/map", data={"alias": "shared.example", "identity_id": "",
                                      "name": "First Guess", "kind": "person"})
    client.post("/authors/map", data={"alias": "shared.example", "identity_id": "",
                                      "name": "Correct Author", "kind": "person"})

    entries = yaml.safe_load((settings.root / "config/identities.yaml").read_text())["identities"]
    owners = [e["id"] for e in entries if "shared.example" in (e.get("aliases") or [])]
    assert owners == ["person:correct-author"]
    assert authors.identity_of(conn, "shared.example") == "person:correct-author"


def test_the_file_header_survives_a_write(settings, conn, monkeypatch):
    """The header states the rules the file is curated by; a yaml round-trip would drop it."""
    p = settings.root / "config/identities.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Canonical author identities.\n# ONE author = ONE id.\nidentities: []\n")
    client = _client(settings, monkeypatch)
    client.post("/authors/map", data={"alias": "x.com", "identity_id": "", "name": "X", "kind": "person"})
    assert p.read_text().startswith("# Canonical author identities.\n# ONE author = ONE id.")


def test_the_page_renders_and_says_when_there_is_nothing_to_do(settings, conn, monkeypatch):
    client = _client(settings, monkeypatch)
    assert "Nothing to decide" in client.get("/authors").text
    _page(settings, conn, "https://andrewchen.com/x")
    body = client.get("/authors").text
    assert "andrewchen.com" in body and "never blocks" in body
