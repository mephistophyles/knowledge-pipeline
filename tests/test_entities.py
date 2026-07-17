"""Entity extraction + resolution (plan §6.4), via a fake extract/confirm/embed
provider and the real sqlite-vec index: same-named entities link across sources,
near names are settled by the tiebreak, distinct entities get their own notes."""
from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator.executor import run_stage
from pipeline.vault.writer import read_note


def _walk(settings, conn, h):
    for stage in ("source_note", "extract_claims", "dedup", "entities"):
        run_stage(settings, conn, h, stage)


def test_entity_extracted_to_note_with_key(settings, conn, fake_claims):
    fake_claims["entities"] = '[{"name": "OpenAI", "type": "company"}]'
    h = add_paste(settings, conn, "OpenAI shipped a model.", source_url="https://a")
    _walk(settings, conn, h)

    post = read_note(settings.vault_dir / "corpus/entities/openai.md")
    assert post["type"] == "entity"
    assert post["name"] == "OpenAI"
    assert post["entity_type"] == "company"
    assert post["provider"] == "fake" and post["model"] == "fake-ent" and post["prompt_version"] == "v1"
    assert len(post["mentions"]) == 1
    assert "*company*" in post.content


def test_same_name_links_across_sources(settings, conn, fake_claims):
    fake_claims["entities"] = '[{"name": "OpenAI", "type": "company"}]'
    a = add_paste(settings, conn, "A", source_url="https://a")
    _walk(settings, conn, a)
    b = add_paste(settings, conn, "B", source_url="https://b")
    _walk(settings, conn, b)  # exact (name, type) match → link, no new note

    post = read_note(settings.vault_dir / "corpus/entities/openai.md")
    assert len(post["mentions"]) == 2
    assert conn.execute("SELECT mentions FROM entities WHERE entity_id='openai'").fetchone()["mentions"] == 2
    assert [p.name for p in (settings.vault_dir / "corpus/entities").glob("*.md")] == ["openai.md"]


def test_near_name_confirmed_same_links(settings, conn, fake_claims):
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]  # everything embeds near
    fake_claims["entities"] = '[{"name": "OpenAI", "type": "company"}]'
    a = add_paste(settings, conn, "A")
    _walk(settings, conn, a)

    fake_claims["entities"] = '[{"name": "Open A.I.", "type": "company"}]'  # different string, near
    fake_claims["same"] = True  # tiebreak: same entity
    b = add_paste(settings, conn, "B")
    _walk(settings, conn, b)

    assert len(read_note(settings.vault_dir / "corpus/entities/openai.md")["mentions"]) == 2
    assert not (settings.vault_dir / "corpus/entities/open-a-i.md").exists()


def test_distinct_entity_gets_new_note(settings, conn, fake_claims):
    fake_claims["vector"] = [1, 0, 0, 0, 0, 0, 0, 0]
    fake_claims["same"] = False  # tiebreak: distinct

    fake_claims["entities"] = '[{"name": "Alpha", "type": "tool"}]'
    a = add_paste(settings, conn, "A")
    _walk(settings, conn, a)

    fake_claims["entities"] = '[{"name": "Beta", "type": "tool"}]'
    b = add_paste(settings, conn, "B")
    _walk(settings, conn, b)

    assert (settings.vault_dir / "corpus/entities/alpha.md").exists()
    assert (settings.vault_dir / "corpus/entities/beta.md").exists()


def test_entities_records_extract_cost(settings, conn, fake_claims):
    h = add_paste(settings, conn, "text")
    _walk(settings, conn, h)
    row = conn.execute("SELECT provider FROM costs WHERE stage='entities:extract'").fetchone()
    assert row is not None and row["provider"] == "fake"
