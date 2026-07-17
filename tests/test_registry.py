"""Artifact registry: register/query/filter/facets, ingestor population, backfill."""
from pipeline.db import registry
from pipeline.ingestors.paste import add_paste


def test_register_get_count(conn):
    registry.register(conn, "h1", source_type="email", author="Ann", source="list-a", title="Issue 1")
    registry.register(conn, "h2", source_type="paste", source="https://x", title="A paste")
    assert registry.count(conn) == 2
    assert registry.get(conn, "h1")["author"] == "Ann"
    assert registry.get(conn, "h1")["media"] == "text"  # default


def test_register_is_upsert(conn):
    registry.register(conn, "h1", source_type="email", title="v1")
    registry.register(conn, "h1", source_type="email", title="v2")
    assert registry.count(conn) == 1
    assert registry.get(conn, "h1")["title"] == "v2"


def test_query_filter_and_search(conn):
    registry.register(conn, "h1", source_type="email", author="Ann", title="Alpha")
    registry.register(conn, "h2", source_type="paste", author="Bob", title="Beta")
    assert [r["artifact_hash"] for r in registry.query(conn, filters={"source_type": "email"})] == ["h1"]
    assert [r["artifact_hash"] for r in registry.query(conn, filters={"search": "Bet"})] == ["h2"]
    assert registry.count(conn, filters={"author": "Ann"}) == 1


def test_facet_values(conn):
    registry.register(conn, "h1", source_type="email", author="Ann")
    registry.register(conn, "h2", source_type="paste", author="Bob")
    f = registry.facet_values(conn)
    assert set(f["source_type"]) == {"email", "paste"}
    assert set(f["author"]) == {"Ann", "Bob"}


def test_paste_ingest_registers(settings, conn):
    h = add_paste(settings, conn, "First line here.\nsecond line", source_url="https://post")
    r = registry.get(conn, h)
    assert r["source_type"] == "paste"
    assert r["title"] == "First line here."  # first non-empty line
    assert r["source"] == "https://post"


def test_backfill_registers_unregistered(settings, conn):
    h = add_paste(settings, conn, "some text", source_url="https://y")
    conn.execute("DELETE FROM artifacts WHERE artifact_hash=?", (h,))  # simulate pre-registry artifact
    assert registry.unregistered_count(conn) == 1
    assert registry.backfill(settings, conn) == 1
    assert registry.get(conn, h) is not None
    assert registry.unregistered_count(conn) == 0
