"""Cost ledger writes (plan §3). Every provider call records one row — the
substrate for benchmarking quality/cost/latency per step across models."""
from __future__ import annotations

import sqlite3


def record(
    conn: sqlite3.Connection,
    artifact_hash: str,
    stage: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    usd: float,
    provider: str | None = None,
    latency_ms: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO costs(artifact_hash, stage, provider, model, tokens_in, tokens_out, usd, latency_ms) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (artifact_hash, stage, provider, model, tokens_in, tokens_out, usd, latency_ms),
    )
