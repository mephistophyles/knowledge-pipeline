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

-- Claim index for dedup (plan §6.3). One row per committed claim note; the
-- companion `claims_vec` (sqlite-vec virtual table) holds the embedding and is
-- created lazily with the model's dimension the first time a claim is indexed.
CREATE TABLE IF NOT EXISTS claims (
  claim_id      TEXT PRIMARY KEY,
  artifact_hash TEXT,                                -- source that first asserted it
  text          TEXT,
  source_url    TEXT,
  model         TEXT,
  attestations  INTEGER NOT NULL DEFAULT 1,          -- corroborating sources (incl. origin)
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Entity index for entity resolution (plan §6.4). Companion `entities_vec`
-- (sqlite-vec) holds the embedding; string match keys on (name, entity_type).
CREATE TABLE IF NOT EXISTS entities (
  entity_id   TEXT PRIMARY KEY,
  name        TEXT,
  entity_type TEXT,
  mentions    INTEGER NOT NULL DEFAULT 1,            -- sources that mention it (incl. origin)
  created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name, entity_type);

-- Artifact registry (dashboard backlog browsing). One row per ingested artifact,
-- populated at ingest, so thousands of items can be filtered by facet without
-- reading every manifest. `jobs` holds progress; this holds what a thing IS.
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_hash TEXT PRIMARY KEY,
  source_type   TEXT,                                -- email | paste | personal_note | …
  author        TEXT,                                -- writer / sender / host
  source        TEXT,                                -- feed / publication / provenance URL
  media         TEXT,                                -- text | audio | video
  title         TEXT,                                -- subject / post / episode title
  word_count    INTEGER,                             -- body length after cleaning (thin-content signal)
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_artifacts_source_type ON artifacts(source_type);
CREATE INDEX IF NOT EXISTS idx_artifacts_author ON artifacts(author);
CREATE INDEX IF NOT EXISTS idx_artifacts_source ON artifacts(source);
CREATE INDEX IF NOT EXISTS idx_artifacts_media ON artifacts(media);
