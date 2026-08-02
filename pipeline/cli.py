"""`pipeline` CLI — ingestion + the control plane over the SQLite state machine.

Every control operation is a row write the workers read (plan §3): pause/resume,
hold/release, retry, throttle, plus the hand-walk / single-step debugging tools.
"""
from __future__ import annotations

import sys
import time
from typing import Optional

import typer
from dotenv import load_dotenv

# Local dev reads provider keys (OPENAI_API_KEY, …) from .env; on the box they
# come from the environment (SSM-injected), where this is a harmless no-op.
load_dotenv()

from pipeline.config import Settings
from pipeline.db import backlog as bl
from pipeline.db import bootstrap, connect
from pipeline.db import controls as ctl
from pipeline.db import jobs
from pipeline.orchestrator import stages
from pipeline.orchestrator.executor import run_stage
from pipeline.vault import VaultWriter

app = typer.Typer(help="Personal knowledge pipeline — control plane + ingestion.", no_args_is_help=True)
add_app = typer.Typer(help="Ingest a new artifact into the raw store.", no_args_is_help=True)
app.add_typer(add_app, name="add")
ingest_app = typer.Typer(help="Bulk / feed ingestion (email, …).", no_args_is_help=True)
app.add_typer(ingest_app, name="ingest")
eval_app = typer.Typer(help="Eval-compare a stage across model/provider variants.", no_args_is_help=True)
app.add_typer(eval_app, name="eval")
registry_app = typer.Typer(help="Artifact registry (dashboard backlog metadata).", no_args_is_help=True)
app.add_typer(registry_app, name="registry")
backlog_app = typer.Typer(help="Pre-cutoff email backlog: scan once, then batch locally.", no_args_is_help=True)
app.add_typer(backlog_app, name="backlog")


def _settings() -> Settings:
    return Settings.load()


def _conn(settings: Settings):
    """Bootstrap the DB (idempotent) and return a connection."""
    return bootstrap(settings.db_path)


def _resolve(conn, ref: str) -> str:
    h = jobs.resolve_ref(conn, ref)
    if h is None:
        typer.secho(f"error: unknown or ambiguous artifact ref {ref!r}", fg="red", err=True)
        raise typer.Exit(1)
    return h


# ── init ────────────────────────────────────────────────────────────────────
@app.command()
def init() -> None:
    """Create the SQLite DB + vault git repo (idempotent)."""
    settings = _settings()
    _conn(settings)
    VaultWriter(settings.vault_dir).ensure_layout()
    typer.echo(f"db:    {settings.db_path}")
    typer.echo(f"vault: {settings.vault_dir}")
    typer.secho("initialised.", fg="green")


# ── ingestion ─────────────────────────────────────────────────────────────────
@add_app.command("paste")
def add_paste(
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Read text from a file."),
    text: Optional[str] = typer.Option(None, "--text", "-t", help="Inline text."),
    url: Optional[str] = typer.Option(None, "--url", help="Provenance URL."),
    type_: str = typer.Option("paste", "--type", help="source_type for provenance."),
) -> None:
    """Ingest pasted text (stdin by default)."""
    from pipeline.ingestors.paste import add_paste as _add

    if file:
        content = open(file, encoding="utf-8").read()
    elif text is not None:
        content = text
    else:
        content = sys.stdin.read()
    if not content.strip():
        typer.secho("error: no text provided", fg="red", err=True)
        raise typer.Exit(1)
    settings = _settings()
    conn = _conn(settings)
    VaultWriter(settings.vault_dir).ensure_layout()
    h = _add(settings, conn, content, source_type=type_, source_url=url)
    typer.secho(f"ingested {h[:12]}  (stage: {stages.first_stage(type_)} ready)", fg="green")


@add_app.command("web")
def add_web(
    url: str = typer.Option(..., "--url", help="Canonical article URL (provenance)."),
    author: Optional[str] = typer.Option(
        None, "--author", help="Stable author id for this site (email/handle/name). Defaults to the hostname."
    ),
    title: Optional[str] = typer.Option(None, "--title", help="Article title (defaults to the first line)."),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Read body text from a file."),
) -> None:
    """Ingest a web article you've pasted the body of (stdin by default).

    v1 is deliberately manual: you select the article text, so nav, teasers and
    footers never reach the extractor and no quote can be attributed to a
    'related posts' blurb.
    """
    from pipeline.ingestors.web import add_web as _add_web

    content = open(file, encoding="utf-8").read() if file else sys.stdin.read()
    if not content.strip():
        typer.secho("error: no text provided", fg="red", err=True)
        raise typer.Exit(1)
    settings = _settings()
    conn = _conn(settings)
    VaultWriter(settings.vault_dir).ensure_layout()
    h = _add_web(settings, conn, content, url=url, author=author, title=title)
    if h is None:
        typer.secho("error: body was empty after normalisation", fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"ingested {h[:12]}  (stage: {stages.first_stage('web')} ready)", fg="green")


@app.command()
def annotate(
    ref: str = typer.Argument(..., help="Artifact hash (prefix ok) to annotate."),
    notes: Optional[str] = typer.Option(None, "--notes", "-n", help="Notes file (stdin if omitted)."),
    url: Optional[str] = typer.Option(None, "--url", help="Provenance URL for the notes."),
) -> None:
    """Attach personal notes to an existing artifact (invisible to corpus derivation)."""
    from pipeline.ingestors.annotate import annotate as _annotate

    content = open(notes, encoding="utf-8").read() if notes else sys.stdin.read()
    if not content.strip():
        typer.secho("error: no notes provided", fg="red", err=True)
        raise typer.Exit(1)
    settings = _settings()
    conn = _conn(settings)
    VaultWriter(settings.vault_dir).ensure_layout()
    h = _annotate(settings, conn, ref, content, source_url=url)
    typer.secho(f"annotated → personal_note {h[:12]}", fg="green")


@ingest_app.command("email")
def ingest_email(
    label: str = typer.Option(..., "--label", help="Gmail label / IMAP folder to pull from."),
    limit: int = typer.Option(50, "--limit", help="Max messages to fetch (newest first)."),
) -> None:
    """Read-only IMAP pull of a label's emails → one clean-markdown artifact each."""
    from pipeline.ingestors.email import fetch_and_ingest

    settings = _settings()
    conn = _conn(settings)
    VaultWriter(settings.vault_dir).ensure_layout()
    try:
        hashes = fetch_and_ingest(settings, conn, label=label, limit=limit)
    except RuntimeError as e:
        typer.secho(f"error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"ingested {len(hashes)} email(s) from {label!r}", fg="green")


# ── backlog (pre-cutoff seed corpus) ─────────────────────────────────────────
@backlog_app.command("scan")
def backlog_scan(
    label: str = typer.Option(..., "--label", help="Gmail label / IMAP folder to scan."),
    before: str = typer.Option(..., "--before", help="Cutoff date YYYY-MM-DD; only mail sent BEFORE this."),
) -> None:
    """One-time read of the mailbox → raw .eml archive + ledger rows. Ingests nothing.

    Read-only and resumable: re-running skips what's already archived.
    """
    from pipeline.ingestors.email import scan_backlog

    settings = _settings()
    conn = _conn(settings)
    def _tick(c):
        typer.echo(f"  … {c['seen']} seen ({c['added']} new, {c['known']} known, {c['duplicate']} dup)")
    try:
        counts = scan_backlog(settings, conn, label=label, before=before, progress=_tick)
    except RuntimeError as e:
        typer.secho(f"error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(
        f"archived {counts['added']} new ({counts['known']} already known, "
        f"{counts['duplicate']} duplicate) from {counts['seen']} message(s)", fg="green",
    )


@backlog_app.command("batches")
def backlog_batches(
    assign: bool = typer.Option(False, "--assign", help="Assign batch ids to unbatched rows first."),
    all_triage: bool = typer.Option(False, "--all", help="With --assign, batch regardless of triage decision."),
) -> None:
    """List batches (one per author) with progress."""
    settings = _settings()
    conn = _conn(settings)
    if assign:
        n = bl.assign_batches(conn, only_triage=None if all_triage else "process")
        typer.secho(f"assigned {n} row(s) to batches", fg="yellow")
    rows = bl.batches(conn)
    if not rows:
        typer.echo("no batches yet — run `pipeline backlog batches --assign`")
        return
    typer.secho(f"{'batch':<44}{'total':>7}{'done':>7}{'todo':>7}  range", bold=True)
    for r in rows:
        span = f"{(r['first_sent'] or '')[:10]} → {(r['last_sent'] or '')[:10]}"
        typer.echo(f"{(r['batch_id'] or '')[:43]:<44}{r['total']:>7}{r['ingested'] or 0:>7}{r['pending'] or 0:>7}  {span}")


@backlog_app.command("run")
def backlog_run(
    batch: Optional[str] = typer.Option(None, "--batch", help="Batch id (author) to ingest; default = any."),
    limit: int = typer.Option(25, "--limit", help="Max messages to ingest this run."),
    reprocess: bool = typer.Option(False, "--reprocess", help="Re-derive even if the chain already completed."),
) -> None:
    """Derive normalized artifacts for archived messages and queue their chains.

    Queues work only — run `pipeline worker …` to execute it.
    """
    from pipeline.ingestors.email import ingest_from_eml

    settings = _settings()
    conn = _conn(settings)
    VaultWriter(settings.vault_dir).ensure_layout()
    rows = bl.pending(conn, batch_id=batch, limit=limit)
    if not rows:
        typer.echo("nothing pending" + (f" in batch {batch!r}" if batch else ""))
        return
    queued = skipped = 0
    for row in rows:
        h = ingest_from_eml(settings, conn, row["eml_hash"], reprocess=reprocess)
        if h:
            queued += 1
        else:
            skipped += 1
            typer.secho(f"  skipped (empty body): {(row['subject'] or '')[:60]}", fg="yellow")
    typer.secho(f"queued {queued} artifact(s)" + (f", skipped {skipped}" if skipped else ""), fg="green")


@backlog_app.command("failures")
def backlog_failures(
    batch: Optional[str] = typer.Option(None, "--batch", help="Restrict to one batch."),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List backlog messages whose chain failed, with the stage and error."""
    settings = _settings()
    conn = _conn(settings)
    rows = bl.failures(conn, batch_id=batch, limit=limit)
    if not rows:
        typer.secho("no failed stages in the backlog", fg="green")
        return
    for r in rows:
        typer.secho(f"{r['eml_hash'][:12]}  {r['stage']:<15} attempts={r['attempts']}", fg="red")
        typer.echo(f"    {(r['author'] or '?')}  {(r['subject'] or '')[:70]}")
        typer.echo(f"    {(r['error'] or '')[:110]}")
    typer.echo(f"\n{len(rows)} failed stage(s) — `pipeline backlog retry` to requeue")


@backlog_app.command("retry")
def backlog_retry(
    batch: Optional[str] = typer.Option(None, "--batch", help="Restrict to one batch."),
    stage: Optional[str] = typer.Option(None, "--stage", help="Restrict to one stage."),
) -> None:
    """Requeue only the failed stages — not the whole batch."""
    settings = _settings()
    conn = _conn(settings)
    n = bl.requeue_failed(conn, batch_id=batch, stage=stage)
    typer.secho(f"requeued {n} failed stage(s)", fg="yellow" if n else "green")


@backlog_app.command("status")
def backlog_status() -> None:
    """Ledger summary: archive, triage, and ingest progress."""
    settings = _settings()
    conn = _conn(settings)
    s = bl.summary(conn)
    if not s["total"]:
        typer.echo("backlog empty — run `pipeline backlog scan --label … --before …`")
        return
    typer.secho(f"{s['total']} message(s) from {s['authors']} author(s)", bold=True)
    typer.echo(f"  archived {s['archived']}   ingested {s['ingested']}   duplicate {s['duplicate']}   skipped {s['skipped']}")
    typer.echo(f"  triage: process {s['to_process']}   drop {s['to_drop']}   untriaged {s['untriaged']}")
    rows = bl.progress(conn)
    if rows:
        typer.secho("\nchain progress (ledger rows only):", bold=True)
        for r in rows:
            colour = "red" if r["status"] == "failed" else None
            typer.secho(f"  {r['stage']:<16}{r['status']:<9}{r['n']}", fg=colour)


@app.command("groom")
def groom(
    author: Optional[str] = typer.Option(None, "--author", help="Limit to one author's claims."),
    max_distance: Optional[float] = typer.Option(
        None, "--max-distance", help="Override the config threshold for this pass."
    ),
    apply: bool = typer.Option(False, "--apply", help="Apply the merges (default: dry run)."),
    out: Optional[str] = typer.Option(None, "--out", help="Save the plan to a JSON file."),
    plan_file: Optional[str] = typer.Option(
        None, "--plan", help="Apply a saved plan instead of re-running the (slow) pass."
    ),
    workers: int = typer.Option(8, "--workers", help="Parallel confirm calls."),
    prompt_version: Optional[str] = typer.Option(
        None, "--prompt-version", help="Override the dedup_confirm prompt version (e.g. v3)."
    ),
) -> None:
    """Retroactively dedup claims ALREADY in the vault. Dry run unless --apply.

    Nothing is deleted: the survivor gains the absorbed claim's wording and
    attestations, and the absorbed note moves to corpus/claims/merged/ marked
    `merged_into`, so every merge is reversible.
    """
    from pipeline import corpus_dedup

    settings = _settings()
    conn = _conn(settings)

    if plan_file:  # review already happened — apply what was reviewed, don't re-plan
        plan = corpus_dedup.Plan.load(plan_file)
        typer.secho(f"loaded {len(plan.pairs)} merge(s) from {plan_file}", bold=True)
        if not apply:
            typer.secho("pass --apply to commit them.", fg="yellow")
            return
        n = corpus_dedup.apply_merges(settings, conn, plan)
        typer.secho(f"merged {n} claim(s); absorbed notes kept under {corpus_dedup.MERGED_DIR}/", fg="green")
        return

    thr = settings.dedup_config["max_distance"] if max_distance is None else max_distance
    if thr < 0:
        typer.secho(
            f"dedup max_distance is {thr} (OFF) — nothing can match. Pass --max-distance 0.72",
            fg="red", err=True,
        )
        raise typer.Exit(1)

    # Confirm calls are independent, so they run in parallel; the greedy planner that
    # consumes them is order-dependent and stays sequential (off cache, so it's fast).
    warm = corpus_dedup.warm_cache(
        settings, conn, max_distance=max_distance, author=author, workers=workers,
        prompt_version=prompt_version,
        progress=lambda s: typer.echo(f"  … confirmed {s['done']}/{s['pairs']} pair(s)"),
    )
    if warm["pairs"]:
        typer.secho(
            f"warmed {warm['done']}/{warm['pairs']} verdict(s)"
            + (f", {warm['errors']} error(s)" if warm["errors"] else ""), fg="cyan",
        )

    def _tick(p):
        typer.echo(f"  … examined {p.examined}, {len(p.pairs)} merge(s) found")

    plan = corpus_dedup.plan_merges(
        settings, conn, max_distance=max_distance, author=author,
        prompt_version=prompt_version, progress=_tick,
    )
    if out:  # save BEFORE printing: planning is the hour, applying is instant
        typer.secho(f"plan saved → {plan.save(out)}", fg="cyan")
    typer.secho(
        f"\nexamined {plan.examined} claim(s) at max_distance={thr} — "
        f"{len(plan.pairs)} merge(s), {plan.confirms} confirm call(s)", bold=True,
    )
    for survivor, absorbed, dist in plan.pairs[:25]:
        s = conn.execute("SELECT text FROM claims WHERE claim_id=?", (survivor,)).fetchone()
        a = conn.execute("SELECT text FROM claims WHERE claim_id=?", (absorbed,)).fetchone()
        typer.echo(f"\n  [{dist:.3f}] keep {survivor}\n    ✓ {(s['text'] or '')[:100]}")
        typer.echo(f"          absorb {absorbed}\n    ↳ {(a['text'] or '')[:100]}")
    if len(plan.pairs) > 25:
        typer.echo(f"\n  … and {len(plan.pairs) - 25} more")

    if not apply:
        typer.secho("\ndry run — nothing written. re-run with --apply to commit.", fg="yellow")
        return
    n = corpus_dedup.apply_merges(settings, conn, plan)
    typer.secho(f"merged {n} claim(s); absorbed notes kept under corpus/claims/merged/", fg="green")


# ── workers / scheduler ───────────────────────────────────────────────────────
@app.command()
def worker(
    resource_class: str = typer.Argument(..., help=f"One of {stages.RESOURCE_CLASSES}."),
    once: bool = typer.Option(False, "--once", help="Drain ready jobs then exit."),
) -> None:
    """Run a worker process for one resource class."""
    from pipeline.orchestrator.worker import run_worker

    settings = _settings()
    bootstrap(settings.db_path)
    run_worker(settings, resource_class, once=once)


@app.command()
def scheduler() -> None:
    """Supervisor/scheduler process (orchestrator compose service).

    Placeholder for the APScheduler poll cadence added in build step 3; idles now
    so the compose stack has a stable orchestrator service to run.
    """
    settings = _settings()
    bootstrap(settings.db_path)
    typer.echo("[scheduler] idle (poll cadence lands in build step 3). Ctrl-C to stop.")
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        pass


# ── control plane ─────────────────────────────────────────────────────────────
@app.command()
def pause(
    stage: Optional[str] = typer.Option(None, "--stage", help="Pause a stage."),
    source: Optional[str] = typer.Option(None, "--source", help="Pause a source_type."),
    all_: bool = typer.Option(False, "--all", help="Pause the whole pipeline (global)."),
    note: Optional[str] = typer.Option(None, "--note"),
) -> None:
    """Pause a stage, a source_type, or everything. Work accumulates in `ready`."""
    settings = _settings()
    conn = _conn(settings)
    scope, key = _scope_from_opts(stage, source, all_)
    ctl.set_control(conn, scope, key, state="paused", note=note)
    typer.secho(f"paused {scope}:{key}", fg="yellow")


@app.command()
def resume(
    stage: Optional[str] = typer.Option(None, "--stage"),
    source: Optional[str] = typer.Option(None, "--source"),
    all_: bool = typer.Option(False, "--all"),
) -> None:
    """Resume a paused stage / source / global scope."""
    settings = _settings()
    conn = _conn(settings)
    scope, key = _scope_from_opts(stage, source, all_)
    ctl.set_control(conn, scope, key, state="running")
    typer.secho(f"resumed {scope}:{key}", fg="green")


@app.command()
def throttle(
    limit: int = typer.Option(..., "--limit", help="Max items per worker run for this scope."),
    stage: Optional[str] = typer.Option(None, "--stage"),
    source: Optional[str] = typer.Option(None, "--source"),
) -> None:
    """Cap per-run throughput for a stage or source_type."""
    settings = _settings()
    conn = _conn(settings)
    scope, key = _scope_from_opts(stage, source, False)
    ctl.set_control(conn, scope, key, batch_limit=limit)
    typer.secho(f"throttled {scope}:{key} → {limit}/run", fg="yellow")


@app.command()
def hold(ref: str = typer.Argument(..., help="Freeze one artifact mid-pipeline.")) -> None:
    """Freeze an artifact: its ready stage becomes `held` and won't advance."""
    settings = _settings()
    conn = _conn(settings)
    h = _resolve(conn, ref)
    n = jobs.hold_artifact(conn, h)
    typer.secho(f"held {h[:12]} ({n} stage row(s) frozen)", fg="yellow")


@app.command()
def release(ref: str = typer.Argument(..., help="Release a held artifact.")) -> None:
    """Un-freeze a held artifact back to `ready`."""
    settings = _settings()
    conn = _conn(settings)
    h = _resolve(conn, ref)
    n = jobs.release_artifact(conn, h)
    typer.secho(f"released {h[:12]} ({n} stage row(s))", fg="green")


@app.command("recount")
def recount(
    apply_: bool = typer.Option(False, "--apply", help="Write the corrected counts."),
) -> None:
    """Recompute corroboration as the number of DISTINCT VOICES per claim.

    Grooming counted attestation ENTRIES, so one writer restating a point across editions
    read as several independent sources. Attestation entries are left alone — they are
    provenance and the merges were correct — only the number is corrected.
    """
    from pipeline import corpus_dedup

    settings = _settings()
    conn = _conn(settings)
    changed = corpus_dedup.recount_attestations(settings, conn, dry_run=not apply_)

    if not changed:
        typer.secho("every claim's corroboration already counts distinct voices", fg="green")
        return
    inflated = sum(1 for _, b, a in changed if a < b)
    typer.secho(f"{len(changed)} claim(s) change; {inflated} were inflated", fg="yellow", bold=True)
    for claim_id, before, after in sorted(changed, key=lambda c: c[1] - c[2], reverse=True)[:25]:
        typer.echo(f"  {claim_id:<28} {before} → {after}")
    if not apply_:
        typer.secho("\ndry run — re-run with --apply to write these", fg="cyan")


identity_app = typer.Typer(help="Author identity: map channels to the people who write them.")
app.add_typer(identity_app, name="identity")


@identity_app.command("harvest")
def identity_harvest(
    apply_: bool = typer.Option(False, "--apply", help="Write proposals to the table (unconfirmed)."),
) -> None:
    """Propose identities from the archived .eml display names. Dry run unless --apply."""
    from pipeline import identity_seed

    settings = _settings()
    conn = _conn(settings)
    props = identity_seed.harvest(settings, conn)
    people = [p for p in props if p.person]
    unknown = [p for p in props if not p.person]

    # Grouped by identity, not by alias: one writer with three publications is ONE author,
    # and a flat per-alias listing reads as duplication when it is the opposite.
    from collections import defaultdict

    grouped: dict[str, list] = defaultdict(list)
    for p in people:
        grouped[p.identity_id].append(p)
    typer.secho(
        f"\n{len(people)} channel(s) → {len(grouped)} person identit(ies):", bold=True, fg="green"
    )
    for ident, ps in sorted(grouped.items(), key=lambda kv: -sum(p.editions for p in kv[1])):
        typer.echo(f"  {ps[0].person:<28} {ident}")
        for p in sorted(ps, key=lambda x: -x.editions):
            typer.echo(f"       {p.alias:<46} ({p.editions:>4}) {p.reason}")
    typer.secho(f"\n{len(unknown)} need(s) a human — the writer is not in the header:", bold=True, fg="yellow")
    for p in sorted(unknown, key=lambda x: -x.editions):
        typer.echo(f"  {(p.display or '—'):<28} {p.alias:<44} ({p.editions:>4}) {p.reason}")

    if apply_:
        n = identity_seed.apply(conn, props)
        typer.secho(f"\nwrote {n} proposed alias(es); confirm with `pipeline identity confirm`", fg="green")
    else:
        typer.secho("\ndry run — re-run with --apply to write these as proposals", fg="cyan")


@identity_app.command("sync")
def identity_sync(
    path: Optional[str] = typer.Option(None, "--file", help="Curation file (default config/identities.yaml)."),
) -> None:
    """Apply config/identities.yaml — the canonical record — as curated mappings."""
    from pipeline import identity_seed

    settings = _settings()
    conn = _conn(settings)
    n_id, n_alias, conflicts = identity_seed.sync(settings, conn, path)
    typer.secho(f"synced {n_id} identities, {n_alias} aliases (curated)", fg="green")
    for c in conflicts:
        typer.secho(f"  CONFLICT (skipped): {c}", fg="red")


@identity_app.command("unmapped")
def identity_unmapped() -> None:
    """Channel keys in the backlog with no curated identity — what still needs a decision."""
    settings = _settings()
    conn = _conn(settings)
    rows = conn.execute(
        "SELECT b.author, COUNT(*) n FROM backlog b WHERE b.author IS NOT NULL AND b.author NOT IN "
        "(SELECT alias FROM identity_aliases WHERE confidence='curated') GROUP BY b.author ORDER BY n DESC"
    ).fetchall()
    if not rows:
        typer.secho("every backlog channel resolves to a curated identity", fg="green")
        return
    typer.secho(f"{len(rows)} unmapped channel(s):", fg="yellow", bold=True)
    for r in rows:
        typer.echo(f"  {r['author']:<48} ({r['n']})")


@identity_app.command("list")
def identity_list(
    proposed: bool = typer.Option(False, "--proposed", help="Only unconfirmed rows."),
) -> None:
    """Show the identity table."""
    settings = _settings()
    conn = _conn(settings)
    sql = (
        "SELECT a.alias, a.confidence, i.identity_id, i.display_name, i.kind "
        "FROM identity_aliases a JOIN identities i USING(identity_id)"
    )
    if proposed:
        sql += " WHERE a.confidence='proposed'"
    rows = conn.execute(sql + " ORDER BY i.display_name").fetchall()
    if not rows:
        typer.echo("no identities yet — run `pipeline identity harvest --apply`")
        return
    for r in rows:
        mark = "✓" if r["confidence"] == "curated" else "?"
        typer.echo(f" {mark} {r['display_name']:<28} {r['kind']:<7} {r['alias']:<44} {r['identity_id']}")


@identity_app.command("set")
def identity_set(
    alias: str = typer.Argument(..., help="Channel key: an email, hostname, or byline."),
    name: str = typer.Argument(..., help="The writer's name (or the organisation's)."),
    kind: str = typer.Option("person", "--kind", help="person | org"),
    identity_id: Optional[str] = typer.Option(None, "--id", help="Attach to an existing identity id."),
) -> None:
    """Map a channel to a person (or org) and mark it curated.

    Use the same `--id` for every channel one writer publishes through — that is what
    stops their email and their site reading as two independent sources.
    """
    from pipeline import authors as A

    settings = _settings()
    conn = _conn(settings)
    ident = identity_id or f"{kind}:{A.slug(name)}"
    A.upsert_identity(conn, ident, name, kind=kind)
    A.add_alias(conn, alias, ident, confidence="curated", source="manual")
    conn.commit()
    typer.secho(f"{alias} → {ident} ({name}) [curated]", fg="green")


@identity_app.command("confirm")
def identity_confirm(
    alias: Optional[str] = typer.Argument(None, help="Alias to confirm; omit with --all."),
    all_: bool = typer.Option(False, "--all", help="Confirm every proposal for a person (never orgs)."),
) -> None:
    """Promote proposals to curated, so they can carry corroboration."""
    from pipeline import authors as A

    settings = _settings()
    conn = _conn(settings)
    if all_:
        rows = conn.execute(
            "SELECT a.alias FROM identity_aliases a JOIN identities i USING(identity_id) "
            "WHERE a.confidence='proposed' AND i.kind='person'"
        ).fetchall()
        for r in rows:
            A.confirm(conn, r["alias"])
        conn.commit()
        typer.secho(f"confirmed {len(rows)} person alias(es); orgs left for review", fg="green")
        return
    if not alias:
        typer.secho("give an alias, or --all", fg="red")
        raise typer.Exit(1)
    ok = A.confirm(conn, alias)
    conn.commit()
    typer.secho(f"confirmed {alias}" if ok else f"unknown alias {alias}", fg="green" if ok else "red")


@app.command("retract")
def retract_cmd(
    ref: str = typer.Argument(..., help="Artifact whose claims should be withdrawn."),
    rederive: bool = typer.Option(
        False, "--rederive", help="After retracting, requeue extract_claims so the chain re-runs."
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Withdraw an artifact's claims from the corpus so it can be re-derived cleanly.

    Claim ids encode extraction position, so a second pass committed on top of a first
    reassigns them. Retract is the inverse of what `dedup` commits: it drops this
    artifact's claims, detaches the attestations it left on other notes, and unpicks any
    merges it took part in. Nothing is lost — the vault is a git repo.
    """
    from pipeline import retract as retract_mod

    settings = _settings()
    conn = _conn(settings)
    h = _resolve(conn, ref)
    n = conn.execute("SELECT COUNT(*) FROM claims WHERE artifact_hash=?", (h,)).fetchone()[0]
    if not n:
        typer.secho(f"{h[:12]} has no committed claims — nothing to retract", fg="yellow")
    else:
        if not yes:
            typer.confirm(f"retract {n} claim(s) from {h[:12]}?", abort=True)
        report = retract_mod.retract(settings, conn, h)
        typer.secho(f"retracted {h[:12]}: {report.summary()}", fg="green")
    if rederive:
        conn.execute(
            "UPDATE jobs SET status='ready', attempts=0, error=NULL, updated_at=datetime('now') "
            "WHERE artifact_hash=? AND stage='extract_claims'",
            (h,),
        )
        conn.commit()
        typer.secho("requeued extract_claims", fg="green")


@app.command()
def retry(
    ref: str = typer.Argument(..., help="Requeue a failed/held job."),
    stage: Optional[str] = typer.Option(None, "--stage", help="Specific stage (default: all failed)."),
) -> None:
    """Requeue failed (or a specific) stage for an artifact."""
    settings = _settings()
    conn = _conn(settings)
    h = _resolve(conn, ref)
    if stage:
        cur = conn.execute(
            "UPDATE jobs SET status='ready', attempts=0, error=NULL, updated_at=datetime('now') "
            "WHERE artifact_hash=? AND stage=?",
            (h, stage),
        )
    else:
        cur = conn.execute(
            "UPDATE jobs SET status='ready', attempts=0, error=NULL, updated_at=datetime('now') "
            "WHERE artifact_hash=? AND status='failed'",
            (h,),
        )
    typer.secho(f"requeued {h[:12]} ({cur.rowcount} stage row(s))", fg="green")


# ── eval-compare ──────────────────────────────────────────────────────────────
@eval_app.command("run")
def eval_run(
    ref: str = typer.Argument(...),
    stage: str = typer.Argument(..., help="Producer stage to eval (e.g. extract_claims)."),
) -> None:
    """Run a stage under every configured variant, hold the artifact, print a report."""
    from pipeline import eval_compare

    settings = _settings()
    conn = _conn(settings)
    h = _resolve(conn, ref)
    try:
        manifest = eval_compare.run_eval(settings, conn, h, stage)
    except ValueError as e:
        typer.secho(f"error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(eval_compare.render_report(manifest))
    typer.secho(f"artifact held for review — approve with: pipeline eval approve {h[:12]} {stage} <#>", fg="yellow")


@eval_app.command("approve")
def eval_approve(
    ref: str = typer.Argument(...),
    stage: str = typer.Argument(...),
    index: int = typer.Argument(..., help="Variant number from the eval report."),
) -> None:
    """Commit the chosen variant's output and resume the chain."""
    from pipeline import eval_compare

    settings = _settings()
    conn = _conn(settings)
    h = _resolve(conn, ref)
    try:
        result = eval_compare.approve_eval(settings, conn, h, stage, index)
    except ValueError as e:
        typer.secho(f"error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"approved variant {index} for {h[:12]}/{stage}", fg="green")
    typer.echo(f"  next stage: {result['next_stage'] or '∅ (chain complete)'}")


# ── hand-walk / single-step ───────────────────────────────────────────────────
@app.command()
def step(
    ref: str = typer.Argument(...),
    stage: str = typer.Argument(..., help="Exact stage to run, then stop."),
) -> None:
    """Run exactly one stage for an artifact, then stop."""
    settings = _settings()
    conn = _conn(settings)
    h = _resolve(conn, ref)
    outcome = run_stage(settings, conn, h, stage)
    typer.echo(f"ran {h[:12]}/{stage}")
    typer.echo(f"  intermediate: {outcome.output_path}")
    typer.echo(f"  next stage:   {outcome.next_stage or '∅ (chain complete)'}")


@app.command()
def walk(ref: str = typer.Argument(..., help="Hand-walk an artifact stage by stage.")) -> None:
    """Run one stage at a time, printing each intermediate and pausing for confirmation."""
    settings = _settings()
    conn = _conn(settings)
    h = _resolve(conn, ref)
    while True:
        stage = _pending_stage(conn, h)
        if stage is None:
            typer.secho("chain complete.", fg="green")
            break
        typer.echo(f"→ running {h[:12]}/{stage} …")
        outcome = run_stage(settings, conn, h, stage)
        typer.echo(f"  intermediate: {outcome.output_path}")
        if outcome.next_stage is None:
            typer.secho("chain complete.", fg="green")
            break
        if not typer.confirm(f"  advance to {outcome.next_stage}?", default=True):
            typer.echo("paused. re-run `pipeline walk` to continue.")
            break


# ── status ────────────────────────────────────────────────────────────────────
@app.command()
def status(ref: Optional[str] = typer.Argument(None, help="Per-artifact view if given.")) -> None:
    """Pipeline-wide summary, or a single artifact's stage timeline."""
    settings = _settings()
    conn = _conn(settings)
    if ref:
        _status_artifact(conn, _resolve(conn, ref))
    else:
        _status_summary(conn)


# ── helpers ───────────────────────────────────────────────────────────────────
def _scope_from_opts(stage, source, all_) -> tuple[str, str]:
    picked = [bool(stage), bool(source), bool(all_)]
    if sum(picked) != 1:
        typer.secho("error: pass exactly one of --stage / --source / --all", fg="red", err=True)
        raise typer.Exit(1)
    if stage:
        return "stage", stage
    if source:
        return "source_type", source
    return "global", "*"


def _pending_stage(conn, artifact_hash: str) -> Optional[str]:
    row = conn.execute(
        "SELECT source_type FROM jobs WHERE artifact_hash=? LIMIT 1", (artifact_hash,)
    ).fetchone()
    source_type = row["source_type"] if row else None
    for stage in stages.chain_for(source_type):
        job = jobs.get_job(conn, artifact_hash, stage)
        if job is None or job["status"] != "done":
            return stage
    return None


def _status_summary(conn) -> None:
    rows = conn.execute(
        "SELECT stage, status, COUNT(*) n FROM jobs GROUP BY stage, status ORDER BY stage, status"
    ).fetchall()
    typer.secho("jobs by stage/status:", bold=True)
    if not rows:
        typer.echo("  (empty)")
    for r in rows:
        typer.echo(f"  {r['stage']:<16} {r['status']:<8} {r['n']}")

    failed = conn.execute(
        "SELECT artifact_hash, stage, error FROM jobs WHERE status='failed'"
    ).fetchall()
    if failed:
        typer.secho("\nfailed (dead-letter):", bold=True, fg="red")
        for r in failed:
            typer.echo(f"  {r['artifact_hash'][:12]}/{r['stage']}: {r['error']}")

    stale = conn.execute(
        "SELECT artifact_hash, stage, claimed_by FROM jobs WHERE status='running' "
        "AND updated_at < datetime('now','-15 minutes')"
    ).fetchall()
    if stale:
        typer.secho("\nstale running (crashed worker?):", bold=True, fg="yellow")
        for r in stale:
            typer.echo(f"  {r['artifact_hash'][:12]}/{r['stage']} (by {r['claimed_by']})")

    controls = ctl.list_controls(conn)
    if controls:
        typer.secho("\ncontrols:", bold=True)
        for c in controls:
            lim = f" limit={c['batch_limit']}" if c["batch_limit"] is not None else ""
            typer.echo(f"  {c['scope']}:{c['key']} → {c['state']}{lim}")


def _status_artifact(conn, artifact_hash: str) -> None:
    rows = conn.execute(
        "SELECT source_type FROM jobs WHERE artifact_hash=? LIMIT 1", (artifact_hash,)
    ).fetchone()
    source_type = rows["source_type"] if rows else None
    typer.secho(f"{artifact_hash}  ({source_type})", bold=True)
    for stage in stages.chain_for(source_type):
        job = jobs.get_job(conn, artifact_hash, stage)
        if job is None:
            typer.echo(f"  {stage:<16} —")
        else:
            out = f"  → {job['output_path']}" if job["output_path"] else ""
            typer.echo(f"  {stage:<16} {job['status']:<8} attempts={job['attempts']}{out}")


if __name__ == "__main__":
    app()
