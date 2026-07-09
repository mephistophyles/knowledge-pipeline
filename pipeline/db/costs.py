"""Cost ledger writes (plan §3). LLM stages call `record` in build step 5;
present now so the schema contract is stable."""
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
) -> None:
    conn.execute(
        "INSERT INTO costs(artifact_hash, stage, model, tokens_in, tokens_out, usd) "
        "VALUES(?,?,?,?,?,?)",
        (artifact_hash, stage, model, tokens_in, tokens_out, usd),
    )
