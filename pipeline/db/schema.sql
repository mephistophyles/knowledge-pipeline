-- Knowledge pipeline control-plane schema (plan §3).
-- The database IS the queue, the control plane, and the dashboard backend.

CREATE TABLE IF NOT EXISTS jobs (
  artifact_hash TEXT    NOT NULL,
  stage         TEXT    NOT NULL,
  status        TEXT    NOT NULL DEFAULT 'ready',  -- pending|ready|running|done|failed|held
  attempts      INTEGER NOT NULL DEFAULT 0,
  claimed_by    TEXT,
  error         TEXT,
  input_path    TEXT,                              -- intermediate artifact feeding this stage
  output_path   TEXT,                              -- intermediate artifact this stage produced
  source_type   TEXT,                              -- denormalized for control checks / throttle
  created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (artifact_hash, stage)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_stage_status ON jobs(stage, status);

CREATE TABLE IF NOT EXISTS controls (
  scope       TEXT    NOT NULL,                    -- global | stage | source_type | artifact
  key         TEXT    NOT NULL,                    -- '*' | stage | source_type | <hash>
  state       TEXT    NOT NULL DEFAULT 'running',  -- running | paused
  batch_limit INTEGER,                             -- max items this scope may process per run (NULL = unlimited)
  note        TEXT,
  updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  started_at  TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  stats_json  TEXT
);

CREATE TABLE IF NOT EXISTS costs (
  artifact_hash TEXT,
  stage         TEXT,
  provider      TEXT,                              -- which provider served the call
  model         TEXT,
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  usd           REAL,
  latency_ms    INTEGER,                           -- wall-clock of the provider call
  at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_costs_stage ON costs(stage);
