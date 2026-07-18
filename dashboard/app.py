"""Dashboard service — read-only local monitor + backlog browser (plan §9).

A window over the same SQLite tables: watch where each artifact is in the chain,
per-stage counts, cost/latency, and — on the detail page — the actual intermediate
output of any step. The artifacts view is driven by the `artifacts` registry so a
large backlog can be filtered by facet (type / author / source / media) + title
search and paged. Opens the DB read-only; the workers remain the single writer.

Still read-only (interactive control is the next phase). Query helpers take a
connection so they're unit-testable; the endpoints wire the connection and render.
"""
from __future__ import annotations

import html
import json
import sqlite3
from math import ceil
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from dashboard import executor
from pipeline.config import Settings
from pipeline.db import bootstrap, jobs, registry

app = FastAPI(title="knowledge-pipeline dashboard")

STAGE_ORDER = [
    "source_note", "extract_claims", "dedup", "entities", "personal",
    "transcribe", "diarize", "speaker_map",
]
STATUS_COLS = ["ready", "running", "held", "done", "failed", "pending"]
PAGE_SIZE = 50


def _order(stage: str) -> int:
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 99


def _ro_conn(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ── query helpers (pure; take a connection) ───────────────────────────────────
def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}


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


def _progress(conn: sqlite3.Connection, artifact_hash: str) -> dict:
    rows = conn.execute("SELECT stage, status FROM jobs WHERE artifact_hash=?", (artifact_hash,)).fetchall()
    if not rows:
        return {"done": 0, "total": 0, "active_stage": None, "active_status": "—"}
    done = sum(1 for r in rows if r["status"] == "done")
    non_done = sorted((r for r in rows if r["status"] != "done"), key=lambda r: _order(r["stage"]))
    active = non_done[0] if non_done else None
    return {
        "done": done,
        "total": len(rows),
        "active_stage": active["stage"] if active else None,
        "active_status": active["status"] if active else "done",
    }


def artifacts(conn: sqlite3.Connection, *, filters: dict | None = None, page: int = 1) -> list[dict]:
    """A page of registry artifacts (filtered) enriched with chain progress."""
    offset = (max(page, 1) - 1) * PAGE_SIZE
    rows = registry.query(conn, filters=filters, limit=PAGE_SIZE, offset=offset)
    return [{**dict(r), **_progress(conn, r["artifact_hash"])} for r in rows]


def artifact_detail(conn: sqlite3.Connection, artifact_hash: str) -> dict:
    rows = sorted(
        conn.execute(
            "SELECT stage, status, attempts, error, input_path, output_path, updated_at, source_type "
            "FROM jobs WHERE artifact_hash=?",
            (artifact_hash,),
        ).fetchall(),
        key=lambda r: _order(r["stage"]),
    )
    meta = registry.get(conn, artifact_hash)
    return {
        "artifact_hash": artifact_hash,
        "meta": dict(meta) if meta else None,
        "source_type": rows[0]["source_type"] if rows else (meta["source_type"] if meta else None),
        "stages": [dict(r) for r in rows],
    }


# ── control + execution actions (write surface; local only — add auth before exposing) ──
CONTROL = {"hold", "release", "retry"}
EXECUTE = {"step", "process"}


def _rw_conn(settings: Settings) -> sqlite3.Connection:
    return bootstrap(settings.db_path)  # writable (WAL) + sqlite-vec loaded


def apply_control(conn: sqlite3.Connection, action: str, hashes: list[str]) -> None:
    for h in hashes:
        if action == "hold":
            jobs.hold_artifact(conn, h)
        elif action == "release":
            jobs.release_artifact(conn, h)
        elif action == "retry":
            conn.execute(
                "UPDATE jobs SET status='ready', attempts=0, error=NULL, updated_at=datetime('now') "
                "WHERE artifact_hash=? AND status IN ('failed','held')",
                (h,),
            )


# Execution (step/process) is serialized + idempotent — see dashboard/executor.py.


# ── endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/action/{action}")
def action(action: str, hash: list[str] = Form(default=[])) -> RedirectResponse:
    """Apply an action to the selected artifacts. Control actions write immediately;
    execution (step/process) is queued on the serial, idempotent executor."""
    settings = Settings.load()
    if action in CONTROL and hash:
        conn = _rw_conn(settings)
        with conn:
            apply_control(conn, action, hash)
    elif action in EXECUTE and hash:
        executor.enqueue(hash, action == "process")
    return RedirectResponse("/", status_code=303)


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
def index(
    source_type: str | None = None,
    author: str | None = None,
    source: str | None = None,
    media: str | None = None,
    q: str | None = None,
    status: str | None = None,
    hide_completed: str | None = None,
    page: int = 1,
) -> str:
    try:
        conn = _ro_conn(Settings.load())
    except sqlite3.OperationalError:
        return _page("<p>db not initialised — run <code>pipeline init</code></p>")
    filters = {
        "source_type": source_type, "author": author, "source": source, "media": media,
        "search": q, "status": status, "hide_completed": "1" if hide_completed else None,
    }
    filters = {k: v for k, v in filters.items() if v}
    with conn:
        ctx = {
            "counts": status_counts(conn),
            "matrix": stage_matrix(conn),
            "costs": cost_summary(conn),
            "arts": artifacts(conn, filters=filters, page=page),
            "facets": registry.facet_values(conn),
            "filters": filters,
            "page": max(page, 1),
            "total": registry.count(conn, filters=filters),
            "unregistered": registry.unregistered_count(conn),
        }
    return _page(_render_overview(ctx))


@app.get("/artifact/{artifact_hash}", response_class=HTMLResponse)
def artifact_page(artifact_hash: str) -> str:
    conn = _ro_conn(Settings.load())
    with conn:
        detail = artifact_detail(conn, artifact_hash)
    return _page(_render_artifact(detail))


# ── rendering ─────────────────────────────────────────────────────────────────
def _qs(filters: dict, **overrides) -> str:
    params = {k: v for k, v in {**filters, **overrides}.items() if v}
    if "search" in params:  # the query param is `q`, the filter key is `search`
        params["q"] = params.pop("search")
    return "?" + urlencode(params) if params else ""


def _filter_bar(facets: dict, filters: dict) -> str:
    def select(name: str, values: list[str]) -> str:
        opts = "<option value=''>any</option>" + "".join(
            f"<option{' selected' if filters.get(name) == v else ''}>{html.escape(v)}</option>" for v in values
        )
        return f"<label>{name}<select name={name}>{opts}</select></label>"

    selects = "".join(select(f, facets.get(f, [])) for f in registry.FACETS)
    selects += select("status", ["ready", "running", "held", "done", "failed"])
    q = html.escape(filters.get("search") or "")
    hide = "checked" if filters.get("hide_completed") else ""
    active = " · <a href='/'>clear</a>" if filters else ""
    return (
        f"<form method=get class=filters>{selects}"
        f"<label>search<input name=q value=\"{q}\" placeholder='title / author'></label>"
        f"<label><input type=checkbox name=hide_completed value=1 {hide}> hide completed</label>"
        f"<button>filter</button>{active}</form>"
    )


def _render_overview(ctx: dict) -> str:
    counts, matrix, costs = ctx["counts"], ctx["matrix"], ctx["costs"]
    arts, filters = ctx["arts"], ctx["filters"]

    badges = " ".join(
        f"<span class=badge>{html.escape(s)}: <b>{n}</b></span>" for s, n in sorted(counts.items())
    ) or "no jobs yet"

    stages = sorted(matrix, key=_order)
    mrows = "".join(
        "<tr><td>" + html.escape(s) + "</td>"
        + "".join(f"<td>{matrix[s].get(c, '') or ''}</td>" for c in STATUS_COLS) + "</tr>"
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
            f"<tr class={cls}><td><input type=checkbox name=hash value='{h}'></td>"
            f"<td><a href='/artifact/{h}'>{html.escape(a.get('title') or h[:12])}</a></td>"
            f"<td>{html.escape(a.get('author') or '—')}</td>"
            f"<td>{html.escape(a.get('source_type') or '—')}</td>"
            f"<td>{html.escape(a.get('media') or '—')}</td>"
            f"<td>{a['done']}/{a['total']}</td><td>{html.escape(active)}</td></tr>"
        )
    arows = arows or "<tr><td colspan=7>no artifacts match — ingest something or clear filters</td></tr>"

    total, page = ctx["total"], ctx["page"]
    pages = max(ceil(total / PAGE_SIZE), 1)
    prev = f"<a href='{_qs(filters, page=page - 1)}'>← prev</a>" if page > 1 else "<span class=dim>← prev</span>"
    nxt = f"<a href='{_qs(filters, page=page + 1)}'>next →</a>" if page < pages else "<span class=dim>next →</span>"
    nudge = (
        f"<p class=nudge>{ctx['unregistered']} artifact(s) not in the registry — run "
        f"<code>pipeline registry backfill</code> to show them.</p>" if ctx["unregistered"] else ""
    )

    return f"""<h1>knowledge-pipeline <span class=live>● live</span></h1>
<p>{badges}</p>
<h2>backlog by stage</h2>
<table><tr><th>stage</th>{''.join(f'<th>{c}</th>' for c in STATUS_COLS)}</tr>{mrows}</table>
<h2>cost &amp; latency</h2>
<table><tr><th>stage</th><th>calls</th><th>usd</th><th>avg ms</th></tr>{crows}</table>
<h2>artifacts <span class=dim>({total})</span></h2>
{_filter_bar(ctx['facets'], filters)}{nudge}
<form method=post>
<div class=actionbar>
<button formaction=/action/hold>⏸ hold</button>
<button formaction=/action/release>▶ release</button>
<button formaction=/action/retry>↻ retry</button>
<button formaction=/action/step>step 1</button>
<button formaction=/action/process>process → done</button>
<span class=dim>— acts on checked rows</span>
</div>
<table><tr><th><input type=checkbox title="select all" onclick="for(const c of this.closest('table').querySelectorAll('input[name=hash]'))c.checked=this.checked"></th><th>title</th><th>author</th><th>type</th><th>media</th><th>progress</th><th>current step</th></tr>{arows}</table>
</form>
<p class=pager>{prev} · page {page}/{pages} · {nxt}</p>"""


def _render_artifact(detail: dict) -> str:
    h = detail["artifact_hash"]
    meta = detail.get("meta") or {}
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
    title = meta.get("title") or h[:12]
    facets = " · ".join(
        f"{k}: <b>{html.escape(str(meta[k]))}</b>" for k in ("source_type", "author", "source", "media") if meta.get(k)
    ) or "—"
    return f"""<p><a href='/'>← overview</a></p>
<h1>{html.escape(title)}</h1>
<p>{facets} · <code>{h}</code></p>
<table><tr><th>stage</th><th>status</th><th>updated</th><th>inspect output</th></tr>{rows}</table>"""


def _read_output(path: str | None, limit: int = 6000) -> str:
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
<title>knowledge-pipeline</title>
<style>body{{font:14px/1.5 system-ui;margin:2rem;max-width:960px}}
h2{{margin-top:1.5rem;font-size:1rem;color:#555}}
table{{border-collapse:collapse;width:100%;margin:.3rem 0}}
td,th{{border:1px solid #ccc;padding:.3rem .6rem;text-align:left}}th{{background:#f4f4f4}}
.badge{{display:inline-block;background:#eef;padding:.1rem .5rem;border-radius:4px;margin:.1rem}}
.filters{{margin:.5rem 0}}.filters label{{margin-right:.8rem;font-size:.85rem;color:#555}}
.filters select,.filters input{{margin-left:.3rem}}
.actionbar{{margin:.5rem 0}}.actionbar button{{margin-right:.3rem;cursor:pointer;padding:.2rem .5rem}}
button.refresh{{position:fixed;top:1rem;right:1rem;padding:.3rem .8rem;cursor:pointer}}
tr.failed{{background:#fdd}}tr.held{{background:#ffd}}
.failed{{color:#b00}}.live{{color:#0a0;font-size:.7rem;vertical-align:middle}}
.dim{{color:#999}}.nudge{{color:#b60;font-size:.85rem}}.pager{{margin:.5rem 0;color:#555}}
pre{{background:#f7f7f7;padding:.5rem;overflow:auto;max-height:24rem}}
a{{color:#06c}}</style>
<button class=refresh onclick="location.reload()">↻ refresh</button>
{body}
<hr><p><a href=/api/summary>/api/summary</a> · <a href=/health>/health</a> · manual refresh</p>"""
