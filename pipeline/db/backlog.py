"""Backlog ledger — the pre-cutoff email corpus as a local table.

The mailbox is scanned ONCE into `.eml` archives (see `pipeline.ingestors.email`);
from then on every batch, re-run and eval reads this table. That inversion is the
point: IMAP has no usable cursor (newest-first, no offset), so anything that pages
the mailbox is unreliable and un-resumable. A table is neither.

Identity is `eml_hash` — the sha256 of the raw RFC822 bytes. `artifact_hash` (the
normalized markdown) is derived and therefore *mutable*: improving the boilerplate
stripper rewrites it for every row. Keying on the raw bytes is what makes
re-derivation a routine operation rather than a corpus-wide identity reset.
"""
from __future__ import annotations

import sqlite3

STATES = ("archived", "ingested", "duplicate", "skipped")


def add(
    conn: sqlite3.Connection,
    *,
    eml_hash: str,
    eml_key: str,
    message_id: str | None,
    author: str | None,
    sent_at: str | None,
    subject: str | None,
    state: str = "archived",
) -> bool:
    """Record one archived message. Returns True if newly added, False if already known.

    Idempotent by `eml_hash`, so an interrupted scan is resumed by re-running it.
    """
    cur = conn.execute(
        "INSERT INTO backlog(eml_hash, eml_key, message_id, author, sent_at, subject, state) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(eml_hash) DO NOTHING",
        (eml_hash, eml_key, message_id, author, sent_at, subject, state),
    )
    conn.commit()
    return cur.rowcount > 0


def message_id_seen(conn: sqlite3.Connection, message_id: str | None, eml_hash: str) -> bool:
    """True if a DIFFERENT .eml already claims this Message-ID.

    The same edition re-delivered can differ byte-for-byte (added headers) while
    carrying one Message-ID. Catching that here stops one edition being counted twice
    without making the header a hard uniqueness constraint — plenty of senders omit or
    reuse it, and a UNIQUE index would abort a 3,000-message scan over one bad sender.
    """
    if not message_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM backlog WHERE message_id=? AND eml_hash<>? LIMIT 1", (message_id, eml_hash)
    ).fetchone()
    return row is not None


def set_state(conn: sqlite3.Connection, eml_hash: str, state: str, *, artifact_hash: str | None = None) -> None:
    conn.execute(
        "UPDATE backlog SET state=?, artifact_hash=COALESCE(?, artifact_hash), "
        "updated_at=datetime('now') WHERE eml_hash=?",
        (state, artifact_hash, eml_hash),
    )
    conn.commit()


def set_triage(conn: sqlite3.Connection, eml_hash: str, decision: str) -> None:
    conn.execute(
        "UPDATE backlog SET triage=?, updated_at=datetime('now') WHERE eml_hash=?", (decision, eml_hash)
    )
    conn.commit()


def assign_batches(conn: sqlite3.Connection, *, only_triage: str | None = "process") -> int:
    """Assign every unbatched row a `batch_id` = its author key.

    Author is the batch unit deliberately: it gives a per-source hit-rate read, a
    natural stopping point, and makes pruning a source a single delete. Rows with no
    author fall into `unknown`. Returns the number of rows assigned.
    """
    sql = "UPDATE backlog SET batch_id=COALESCE(author,'unknown'), updated_at=datetime('now') WHERE batch_id IS NULL"
    params: list = []
    if only_triage:
        sql += " AND triage=?"
        params.append(only_triage)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Batch summary, largest first — the work-list you pick from."""
    return conn.execute(
        "SELECT batch_id, COUNT(*) AS total, "
        "SUM(state='ingested') AS ingested, SUM(state='archived') AS pending, "
        "MIN(sent_at) AS first_sent, MAX(sent_at) AS last_sent "
        "FROM backlog WHERE batch_id IS NOT NULL GROUP BY batch_id ORDER BY total DESC"
    ).fetchall()


def pending(conn: sqlite3.Connection, *, batch_id: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    """Archived-but-not-yet-ingested rows, oldest first.

    Oldest-first so the corpus accumulates in the order it was written — later
    editions meet the earlier ones already in the index, which is what lets
    corroboration build instead of arriving out of order.
    """
    sql = "SELECT * FROM backlog WHERE state='archived'"
    params: list = []
    if batch_id:
        sql += " AND batch_id=?"
        params.append(batch_id)
    sql += " ORDER BY sent_at ASC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def get(conn: sqlite3.Connection, eml_hash: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM backlog WHERE eml_hash=?", (eml_hash,)).fetchone()


def progress(conn: sqlite3.Connection, *, batch_id: str | None = None) -> list[sqlite3.Row]:
    """Per-stage job status rolled up across the ledger.

    The ledger row and the job rows are joined on `artifact_hash`, so every message
    is accounted for by name rather than by count — "3 failed" is only useful if you
    can say which three.
    """
    sql = (
        "SELECT j.stage, j.status, COUNT(*) n FROM backlog b JOIN jobs j "
        "ON j.artifact_hash = b.artifact_hash WHERE b.artifact_hash IS NOT NULL"
    )
    params: list = []
    if batch_id:
        sql += " AND b.batch_id=?"
        params.append(batch_id)
    return conn.execute(sql + " GROUP BY j.stage, j.status ORDER BY j.stage, j.status", params).fetchall()


def failures(conn: sqlite3.Connection, *, batch_id: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
    """Ledger rows whose chain has a failed stage — the retry work-list."""
    sql = (
        "SELECT b.eml_hash, b.author, b.subject, b.sent_at, j.stage, j.attempts, j.error "
        "FROM backlog b JOIN jobs j ON j.artifact_hash = b.artifact_hash "
        "WHERE j.status='failed'"
    )
    params: list = []
    if batch_id:
        sql += " AND b.batch_id=?"
        params.append(batch_id)
    return conn.execute(sql + " ORDER BY b.sent_at LIMIT ?", params + [limit]).fetchall()


def requeue_failed(conn: sqlite3.Connection, *, batch_id: str | None = None, stage: str | None = None) -> int:
    """Reset failed stages back to `ready` for ledger rows, clearing the attempt count.

    Targeted by design: it touches only rows that actually failed, so retrying costs
    exactly the editions that need it rather than re-running a whole batch.
    """
    sql = (
        "UPDATE jobs SET status='ready', attempts=0, error=NULL, updated_at=datetime('now') "
        "WHERE status='failed' AND artifact_hash IN (SELECT artifact_hash FROM backlog "
        "WHERE artifact_hash IS NOT NULL"
    )
    params: list = []
    if batch_id:
        sql += " AND batch_id=?"
        params.append(batch_id)
    sql += ")"
    if stage:
        sql += " AND stage=?"
        params.append(stage)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def summary(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(state='archived') AS archived, SUM(state='ingested') AS ingested, "
        "SUM(state='duplicate') AS duplicate, SUM(state='skipped') AS skipped, "
        "SUM(triage='process') AS to_process, SUM(triage='drop') AS to_drop, "
        "SUM(triage IS NULL) AS untriaged, COUNT(DISTINCT author) AS authors FROM backlog"
    ).fetchone()
    return {k: (row[k] or 0) for k in row.keys()}
