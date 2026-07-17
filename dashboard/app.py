"""Dashboard service — read-only local monitor (plan §9).

A window over the same SQLite tables so you can watch the backlog while it runs:
where each artifact is in the chain, per-stage counts, cost/latency, and — on the
detail page — the actual intermediate output of any step, so you know when to
inspect. Opens the DB read-only; the workers remain the single writer.

The full control-plane + review-queue dashboard is build step 6; this stays a
read-only view. Query helpers take a connection so they're unit-testable; the
endpoints wire the connection and render.
"""
from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from pipeline.config import Settings

app = FastAPI(title="knowledge-pipeline dashboard")

# Chain order for sorting stages (frontier detection + timeline order).
STAGE_ORDER = [
    "source_note", "extract_claims", "dedup", "entities", "personal",
    "transcribe", "diarize", "speaker_map",
]
STATUS_COLS = ["ready", "running", "held", "done", "failed", "pending"]


def _order(stage: str) -> int:
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 99


def _ro_conn(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ── query helpers (pure; take a connection) ───────────────────────────────────
def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM jobs GROUP BY status"
    )}


def stage_matrix(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    m: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT stage, status, COUNT(*) n FROM jobs GROUP BY stage, status"):
        m.setdefault(r["stage"], {})[r["status"]] = r["n"]
    return m


def cost_summary(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT stage, COUNT(*) calls, COALESCE(SUM(usd),0) usd, "
        "COALESCE(AVG(latency_ms),0) avg_latency FROM costs GROUP BY stage ORDER BY stage"
    )]


def artifacts(conn: sqlite3.Connection) -> list[dict]:
    """One row per artifact: progress + its current (frontier) stage/status."""
    by_art: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT artifact_hash, stage, status, source_type FROM jobs"):
        by_art.setdefault(r["artifact_hash"], []).append(r)
    out = []
    for h, rows in by_art.items():
        done = sum(1 for r in rows if r["status"] == "done")
        non_done = sorted((r for r in rows if r["status"] != "done"), key=lambda r: _order(r["stage"]))
        active = non_done[0] if non_done else None
        out.append({
            "artifact_hash": h,
            "source_type": rows[0]["source_type"],
            "done": done,
            "total": len(rows),
            "active_stage": active["stage"] if active else None,
            "active_status": active["status"] if active else "done",
        })
    # Surface things needing attention first: failed, then held, then the rest.
    prio = {"failed": 0, "held": 1}
    return sorted(out, key=lambda a: (prio.get(a["active_status"], 2), a["artifact_hash"]))


def artifact_detail(conn: sqlite3.Connection, artifact_hash: str) -> dict:
    rows = sorted(
        conn.execute(
            "SELECT stage, status, attempts, error, input_path, output_path, updated_at, source_type "
            "FROM jobs WHERE artifact_hash=?",
            (artifact_hash,),
        ).fetchall(),
        key=lambda r: _order(r["stage"]),
    )
    return {
        "artifact_hash": artifact_hash,
        "source_type": rows[0]["source_type"] if rows else None,
        "stages": [dict(r) for r in rows],
    }


# ── endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/summary")
def summary() -> JSONResponse:
    try:
        conn = _ro_conn(Settings.load())
    except sqlite3.OperationalError:
        return JSONResponse({"error": "db not initialised"}, status_code=503)
    with conn:
        return JSONResponse({
            "status_counts": status_counts(conn),
            "stage_matrix": stage_matrix(conn),
            "cost_summary": cost_summary(conn),
            "artifacts": artifacts(conn),
        })


@app.get("/api/artifact/{artifact_hash}")
def api_artifact(artifact_hash: str) -> JSONResponse:
    conn = _ro_conn(Settings.load())
    with conn:
        return JSONResponse(artifact_detail(conn, artifact_hash))


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    try:
        conn = _ro_conn(Settings.load())
    except sqlite3.OperationalError:
        return _page("<p>db not initialised — run <code>pipeline init</code></p>")
    with conn:
        return _page(_render_overview(status_counts(conn), stage_matrix(conn), cost_summary(conn), artifacts(conn)))


@app.get("/artifact/{artifact_hash}", response_class=HTMLResponse)
def artifact_page(artifact_hash: str) -> str:
    conn = _ro_conn(Settings.load())
    with conn:
        detail = artifact_detail(conn, artifact_hash)
    return _page(_render_artifact(detail))


# ── rendering ─────────────────────────────────────────────────────────────────
def _render_overview(counts: dict, matrix: dict, costs: list[dict], arts: list[dict]) -> str:
    badges = " ".join(
        f"<span class=badge>{html.escape(s)}: <b>{n}</b></span>" for s, n in sorted(counts.items())
    ) or "no jobs yet"

    stages = sorted(matrix, key=_order)
    mrows = "".join(
        "<tr><td>" + html.escape(s) + "</td>"
        + "".join(f"<td>{matrix[s].get(c, '') or ''}</td>" for c in STATUS_COLS)
        + "</tr>"
        for s in stages
    ) or f"<tr><td colspan={len(STATUS_COLS) + 1}>no jobs</td></tr>"

    crows = "".join(
        f"<tr><td>{html.escape(c['stage'])}</td><td>{c['calls']}</td>"
        f"<td>${c['usd']:.4f}</td><td>{c['avg_latency']:.0f}</td></tr>" for c in costs
    ) or "<tr><td colspan=4>no LLM calls yet</td></tr>"

    arows = ""
    for a in arts:
        cls = a["active_status"] if a["active_status"] in ("failed", "held") else ""
        active = f"{a['active_stage']} · {a['active_status']}" if a["active_stage"] else "✓ complete"
        h = a["artifact_hash"]
        arows += (
            f"<tr class={cls}><td><a href='/artifact/{h}'>{h[:12]}</a></td>"
            f"<td>{html.escape(a['source_type'] or '—')}</td>"
            f"<td>{a['done']}/{a['total']}</td><td>{html.escape(active)}</td></tr>"
        )
    arows = arows or "<tr><td colspan=4>no artifacts — ingest something</td></tr>"

    return f"""<h1>knowledge-pipeline <span class=live>● live</span></h1>
<p>{badges}</p>
<h2>backlog by stage</h2>
<table><tr><th>stage</th>{''.join(f'<th>{c}</th>' for c in STATUS_COLS)}</tr>{mrows}</table>
<h2>cost &amp; latency</h2>
<table><tr><th>stage</th><th>calls</th><th>usd</th><th>avg ms</th></tr>{crows}</table>
<h2>artifacts</h2>
<table><tr><th>artifact</th><th>source</th><th>progress</th><th>current step</th></tr>{arows}</table>"""


def _render_artifact(detail: dict) -> str:
    h = detail["artifact_hash"]
    rows = ""
    for s in detail["stages"]:
        out = _read_output(s.get("output_path"))
        inspect = f"<details><summary>output</summary><pre>{html.escape(out)}</pre></details>" if out else "—"
        err = f"<br><span class=failed>{html.escape(s['error'])}</span>" if s.get("error") else ""
        rows += (
            f"<tr class={s['status'] if s['status'] in ('failed', 'held') else ''}>"
            f"<td>{html.escape(s['stage'])}</td><td>{html.escape(s['status'])}{err}</td>"
            f"<td>{html.escape(str(s.get('updated_at') or ''))}</td><td>{inspect}</td></tr>"
        )
    return f"""<p><a href='/'>← overview</a></p>
<h1>{h[:12]}</h1>
<p>source_type: <b>{html.escape(detail.get('source_type') or '—')}</b> · <code>{h}</code></p>
<table><tr><th>stage</th><th>status</th><th>updated</th><th>inspect output</th></tr>{rows}</table>"""


def _read_output(path: str | None, limit: int = 6000) -> str:
    """Read a stage's intermediate for inline inspection; pretty-print JSON."""
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        text = json.dumps(json.loads(p.read_text()), indent=2)
    except Exception:
        text = p.read_text(errors="replace")
    return text[:limit] + ("\n… (truncated)" if len(text) > limit else "")


def _page(body: str) -> str:
    return f"""<!doctype html><meta charset=utf-8>
<meta http-equiv=refresh content=3>
<title>knowledge-pipeline</title>
<style>body{{font:14px/1.5 system-ui;margin:2rem;max-width:900px}}
h2{{margin-top:1.5rem;font-size:1rem;color:#555}}
table{{border-collapse:collapse;width:100%;margin:.3rem 0}}
td,th{{border:1px solid #ccc;padding:.3rem .6rem;text-align:left}}th{{background:#f4f4f4}}
.badge{{display:inline-block;background:#eef;padding:.1rem .5rem;border-radius:4px;margin:.1rem}}
tr.failed{{background:#fdd}}tr.held{{background:#ffd}}
.failed{{color:#b00}}.live{{color:#0a0;font-size:.7rem;vertical-align:middle}}
pre{{background:#f7f7f7;padding:.5rem;overflow:auto;max-height:24rem}}
a{{color:#06c}}</style>
{body}
<hr><p><a href=/api/summary>/api/summary</a> · <a href=/health>/health</a> · refreshes every 3s</p>"""
