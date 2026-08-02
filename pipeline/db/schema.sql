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
  merged_into   TEXT,                                -- set when groomed into another claim;
                                                     -- the row is KEPT so a merge is reversible
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

-- Pre-cutoff email backlog ledger. The mailbox is read ONCE into `.eml` archives;
-- everything after that pages through this table, never IMAP. Identity is the sha256
-- of the RAW RFC822 bytes, so it survives changes to the normalizer (which rewrite
-- `artifact_hash` but not `eml_hash`) and re-derivation never needs the mailbox.
CREATE TABLE IF NOT EXISTS backlog (
  eml_hash      TEXT PRIMARY KEY,                    -- sha256 of raw message bytes: stable identity
  eml_key       TEXT    NOT NULL,                    -- blobstore key of the archived .eml
  message_id    TEXT,                                -- absent/duplicated in the wild → NOT unique
  artifact_hash TEXT,                                -- normalized artifact currently derived from it
  author        TEXT,                                -- normalized author key (pipeline.authors)
  sent_at       TEXT,                                -- INTERNALDATE, ISO8601 — the cutoff is applied to this
  subject       TEXT,
  triage        TEXT,                                -- process | drop | review | NULL (not yet triaged)
  batch_id      TEXT,                                -- assigned batch (author-derived); NULL = unassigned
  state         TEXT    NOT NULL DEFAULT 'archived', -- archived | ingested | duplicate | skipped
  created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_backlog_message_id ON backlog(message_id);
CREATE INDEX IF NOT EXISTS idx_backlog_author ON backlog(author);
CREATE INDEX IF NOT EXISTS idx_backlog_batch ON backlog(batch_id, state);
CREATE INDEX IF NOT EXISTS idx_backlog_state ON backlog(state);

-- Cached dedup confirm verdicts. Grooming re-runs (different threshold, same prompt)
-- otherwise re-ask the model identical questions: a threshold sweep costs one paid
-- pass per PROMPT instead of one per (prompt, threshold) pair. Keyed by prompt+model
-- because a verdict is only reusable under the exact judge that produced it.
CREATE TABLE IF NOT EXISTS dedup_verdicts (
  prompt_version TEXT NOT NULL,
  model          TEXT NOT NULL,
  claim_a        TEXT NOT NULL,
  claim_b        TEXT NOT NULL,
  same           INTEGER NOT NULL,
  distance       REAL,
  at             TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (prompt_version, model, claim_a, claim_b)
);

-- ── Author identity (web-ingestion-plan.md Part 1) ───────────────────────────
-- `pipeline.authors.author_key` normalizes a From header to a bare email, which is a
-- CHANNEL, not a person: `email@stratechery.com` and `stratechery.com` are the same
-- writer, and the same essay arriving on both would otherwise read as two independent
-- sources corroborating each other. Identity is anchored to the literal person so that
-- guest posts, syndication, and multi-author venues attribute to whoever made the claim
-- rather than to the pipe it arrived through.
CREATE TABLE IF NOT EXISTS identities (
  identity_id  TEXT PRIMARY KEY,                     -- 'person:ben-thompson' | 'org:corporate-rebels'
  display_name TEXT NOT NULL,
  kind         TEXT NOT NULL DEFAULT 'person',       -- person | org
  note         TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Many channel keys resolve to one identity. `confidence` gates use: a 'proposed' row is
-- a harvest suggestion and is NOT trusted for corroboration until confirmed, because a
-- wrong merge fuses two real writers into one voice.
CREATE TABLE IF NOT EXISTS identity_aliases (
  alias       TEXT PRIMARY KEY,                      -- normalized key: email, hostname, or byline
  identity_id TEXT NOT NULL REFERENCES identities(identity_id),
  kind        TEXT,                                  -- email | host | byline
  confidence  TEXT NOT NULL DEFAULT 'proposed',      -- proposed | curated
  source      TEXT,                                  -- how we learned it (e.g. 'eml-display-name')
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_alias_identity ON identity_aliases(identity_id);
CREATE INDEX IF NOT EXISTS idx_alias_confidence ON identity_aliases(confidence);

-- ── Web backlog ledger (web-ingestion-plan.md Part 2) ────────────────────────
-- Same inversion as the email ledger: fetch ONCE into a content-addressed archive, then
-- page this table forever. Identity is the sha256 of the RAW response bytes, never the
-- extracted body — the extractor is the component most likely to change, and keying on
-- its output would turn every extractor tweak into a corpus-wide identity reset.
CREATE TABLE IF NOT EXISTS web_backlog (
  fetch_hash    TEXT PRIMARY KEY,                   -- sha256 of raw response bytes
  url           TEXT NOT NULL,                      -- canonical URL
  requested_url TEXT,                               -- what we were given, canonicalized
  fetch_key     TEXT,                               -- blobstore key of the archived response
  site          TEXT,                               -- hostname; the publication facet
  title         TEXT,
  identity_id   TEXT,                               -- resolved author, NULL = unmapped
  content_type  TEXT,
  http_status   INTEGER,
  syndicated_from TEXT,                             -- set when the page declares a cross-site canonical
  artifact_hash TEXT,                               -- normalized artifact, once extracted
  triage        TEXT,                               -- process | drop | review | NULL
  state         TEXT NOT NULL DEFAULT 'archived',   -- archived | ingested | duplicate | escalated
  escalation    TEXT,                               -- cause, when state='escalated'
  fetched_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_web_url ON web_backlog(url);
CREATE INDEX IF NOT EXISTS idx_web_state ON web_backlog(state);
CREATE INDEX IF NOT EXISTS idx_web_site ON web_backlog(site);
CREATE INDEX IF NOT EXISTS idx_web_identity ON web_backlog(identity_id);

-- Every exit through the escalation seam, by cause. This table IS the evidence for the
-- deferred browser-vs-form decision: ~3/month keeps manual paste, ~30/month justifies
-- building something. Rows are kept even when a URL is later fetched successfully, so the
-- rate reflects what actually happened rather than what survived.
CREATE TABLE IF NOT EXISTS web_escalations (
  url        TEXT NOT NULL,
  cause      TEXT NOT NULL,
  detail     TEXT,
  at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_escalation_cause ON web_escalations(cause);
