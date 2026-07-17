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
