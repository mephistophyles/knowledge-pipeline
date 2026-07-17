"""End-to-end coverage of the steps 1–2 slice: storage round-trip, control-plane
precedence, atomic claiming, and the paste → walk / annotate → derive flows."""
from pipeline.db import controls as ctl
from pipeline.db import jobs
from pipeline.ingestors.annotate import annotate
from pipeline.ingestors.paste import add_paste
from pipeline.orchestrator import stages
from pipeline.orchestrator.executor import run_stage
from pipeline.storage.manifest import load_artifact, load_manifest, write_artifact


# ── storage ───────────────────────────────────────────────────────────────────
def test_manifest_round_trip(settings):
    store = settings.blobstore
    h, m = write_artifact(store, b"hello world", source_type="paste", ingestor_version="t", ext="md")
    assert m.content_hash == h
    assert load_artifact(store, load_manifest(store, h)) == b"hello world"


def test_content_addressing_is_idempotent(settings):
    store = settings.blobstore
    h1, _ = write_artifact(store, b"same", source_type="paste", ingestor_version="t", ext="md")
    h2, _ = write_artifact(store, b"same", source_type="paste", ingestor_version="t", ext="md")
    assert h1 == h2


# ── control-plane precedence ──────────────────────────────────────────────────
def test_control_precedence_most_specific_wins(conn):
    ctl.set_control(conn, "global", "*", state="paused")
    assert ctl.effective_state(conn, "extract_claims", "paste", "abc") == "paused"
    # A more specific `running` un-pauses the broader global pause.
    ctl.set_control(conn, "stage", "extract_claims", state="running")
    assert ctl.effective_state(conn, "extract_claims", "paste", "abc") == "running"
    # Artifact scope is more specific still.
    ctl.set_control(conn, "artifact", "abc", state="paused")
    assert ctl.effective_state(conn, "extract_claims", "paste", "abc") == "paused"


# ── claiming + advance ────────────────────────────────────────────────────────
def test_paste_ingest_and_walk(settings, conn, fake_claims):
    h = add_paste(settings, conn, "Taste is the differentiator.", source_url="https://x")
    assert jobs.get_job(conn, h, "source_note")["status"] == "ready"

    for stage in stages.chain_for("paste"):
        run_stage(settings, conn, h, stage)
        assert jobs.get_job(conn, h, stage)["status"] == "done"

    note = settings.vault_dir / f"corpus/sources/paste-{h[:8]}.md"
    assert note.exists()


def test_claim_respects_pause(settings, conn):
    h = add_paste(settings, conn, "x")
    ctl.set_control(conn, "stage", "source_note", state="paused")
    assert jobs.claim_next(conn, "w1", ["source_note"]) is None
    ctl.set_control(conn, "stage", "source_note", state="running")
    claimed = jobs.claim_next(conn, "w1", ["source_note"])
    assert claimed["artifact_hash"] == h
    assert jobs.get_job(conn, h, "source_note")["status"] == "running"


def test_claim_is_exclusive(settings, conn):
    add_paste(settings, conn, "only one")
    first = jobs.claim_next(conn, "w1", ["source_note"])
    second = jobs.claim_next(conn, "w2", ["source_note"])
    assert first is not None and second is None  # can't double-claim


def test_batch_limit_throttles(settings, conn):
    add_paste(settings, conn, "a")
    add_paste(settings, conn, "b")
    ctl.set_control(conn, "source_type", "paste", batch_limit=1)
    processed: dict = {}
    j1 = jobs.claim_next(conn, "w", ["source_note"], processed)
    jobs.count_processed(processed, j1)
    j2 = jobs.claim_next(conn, "w", ["source_note"], processed)
    assert j1 is not None and j2 is None  # second is over the per-run limit


def test_hold_freezes_and_blocks_advance(settings, conn):
    h = add_paste(settings, conn, "held one")
    jobs.hold_artifact(conn, h)
    assert jobs.get_job(conn, h, "source_note")["status"] == "held"
    assert jobs.claim_next(conn, "w", ["source_note"]) is None  # frozen: not claimable
    # Even a deliberate force-run must not thaw the freeze — next stage stays held.
    run_stage(settings, conn, h, "source_note")
    assert jobs.get_job(conn, h, "extract_claims")["status"] == "held"
    # Release thaws the whole artifact.
    jobs.release_artifact(conn, h)
    assert jobs.get_job(conn, h, "extract_claims")["status"] == "ready"


# ── annotate / personal deriver ───────────────────────────────────────────────
def test_annotate_creates_invisible_personal_note(settings, conn):
    target = add_paste(settings, conn, "corpus source")
    note_hash = annotate(settings, conn, target, "my private take")
    m = load_manifest(settings.blobstore, note_hash)
    assert m.source_type == "personal_note"
    assert m.annotates == target
    # Personal chain, not the corpus chain.
    assert jobs.get_job(conn, note_hash, "personal")["status"] == "ready"

    run_stage(settings, conn, note_hash, "personal")
    commentary = settings.vault_dir / f"personal/commentary/personal-{note_hash[:8]}.md"
    assert commentary.exists()
    assert "my private take" in commentary.read_text()


def test_failure_exhausts_to_dead_letter(conn):
    jobs.insert_job(conn, "z" * 64, "source_note", "paste")
    statuses = [jobs.record_attempt_failure(conn, "z" * 64, "source_note", "boom", 3) for _ in range(3)]
    assert statuses == ["ready", "ready", "failed"]
