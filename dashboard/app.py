"""Dashboard service — read-only for now (plan §9).

The full control plane + review queue is build step 6. This v0 is a thin,
read-only window over the same tables so the compose stack has a real dashboard
service and you can eyeball queue state in a browser. It opens the DB read-only;
the workers remain the single writer (plan §14.1).
"""
from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from pipeline.config import Settings

app = FastAPI(title="knowledge-pipeline dashboard")


def _ro_conn() -> sqlite3.Connection:
    settings = Settings.load()
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/summary")
def summary() -> JSONResponse:
    try:
        conn = _ro_conn()
    except sqlite3.OperationalError:
        return JSONResponse({"error": "db not initialised"}, status_code=503)
    with conn:
        jobs = [dict(r) for r in conn.execute(
            "SELECT stage, status, COUNT(*) n FROM jobs GROUP BY stage, status ORDER BY stage, status"
        )]
        controls = [dict(r) for r in conn.execute(
            "SELECT scope, key, state, batch_limit FROM controls ORDER BY scope, key"
        )]
        failed = [dict(r) for r in conn.execute(
            "SELECT artifact_hash, stage, error FROM jobs WHERE status='failed'"
        )]
    return JSONResponse({"jobs": jobs, "controls": controls, "failed": failed})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    try:
        conn = _ro_conn()
        with conn:
            rows = conn.execute(
                "SELECT stage, status, COUNT(*) n FROM jobs "
                "GROUP BY stage, status ORDER BY stage, status"
            ).fetchall()
        table = "".join(
            f"<tr><td>{r['stage']}</td><td>{r['status']}</td><td>{r['n']}</td></tr>" for r in rows
        ) or "<tr><td colspan=3>no jobs yet</td></tr>"
    except sqlite3.OperationalError:
        table = "<tr><td colspan=3>db not initialised — run <code>pipeline init</code></td></tr>"
    return f"""<!doctype html><meta charset=utf-8>
<title>knowledge-pipeline</title>
<style>body{{font:14px/1.5 system-ui;margin:2rem;max-width:640px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:.3rem .6rem;text-align:left}}
th{{background:#f4f4f4}}</style>
<h1>knowledge-pipeline</h1>
<p>Read-only state view (build step 1–2). Full control plane + review queue: build step 6.</p>
<table><tr><th>stage</th><th>status</th><th>count</th></tr>{table}</table>
<p><a href=/api/summary>/api/summary</a> · <a href=/health>/health</a></p>"""
