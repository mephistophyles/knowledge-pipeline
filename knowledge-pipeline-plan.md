# Personal Knowledge Pipeline — End-to-End System Plan (v3)

A batch-oriented ingestion → distillation → Obsidian vault system with provenance, delta tracking, per-stage evals, explicit swap points, and a database-backed control plane. Runs on AWS (EC2 + S3 + spot GPU bursts) with full local-mode parity. Designed for single-operator use, open-sourceable, and structured for handoff to Claude Code as a build spec.

**v3 changes:** §14 infrastructure — two-tier compute (always-on control box + manifest-based spot GPU bursts), S3 blob store behind a storage interface, Litestream-backed SQLite, SES inbound email, nginx/TLS dashboard exposure, Terraform replication module, cost envelope.

**v2 changes:** Celery/Redis replaced with a SQLite state-machine orchestrator; explicit control plane (pause/hold/throttle/hand-walk); sibling-note annotation workflow; claim dedup with attestations and human-only endorsement; per-source attribution modes; embeddings split into write-time dedup (day one) vs. retrieval (deferred, tripwire-gated).

---

## 1. Architecture Overview

```
 SOURCES                INGESTION            RAW STORE           PROCESSING              DERIVATION             VAULT
┌────────────┐      ┌──────────────┐     ┌─────────────┐     ┌───────────────┐      ┌───────────────┐     ┌──────────────┐
│ Email inbox │────▶│ imap poller  │────▶│             │     │ transcription │      │ extraction    │     │ corpus/      │
│ Podcast RSS │────▶│ feed poller  │────▶│ content-    │────▶│ diarization   │─────▶│ claim dedup / │────▶│ personal/    │
│ YouTube URL │────▶│ yt-dlp       │────▶│ addressed   │     │ speaker map   │      │ attestation   │     │ hubs/        │
│ Web article │────▶│ trafilatura  │────▶│ blob store  │     │ OCR / layout  │      │ entities/hubs │     │ (git repo)   │
│ Paste/notes │────▶│ CLI drop     │────▶│ + manifest  │     │ html→md       │      │ personal notes│     │              │
└────────────┘      └──────────────┘     └─────────────┘     └───────────────┘      └───────────────┘     └──────────────┘
                                                                     │                      │                    │
                                                     ┌───────────────┴──────────────────────┴──────────┐
                                                     │  SQLite state machine (jobs + controls tables)   │
                                                     │  = queue, control plane, and dashboard backend   │
                                                     └──────────────────────────────────────────────────┘
```

**Core invariants (non-negotiable, everything else is swappable):**

1. Every artifact lands in the raw store before any processing. Ingestors do nothing else. This includes **your own notes**, which are artifacts like any other.
2. Every derived note carries a full provenance manifest in frontmatter.
3. Derivation is stateless and idempotent: `raw + prompt_version + model → note`. Any note can be regenerated.
4. Human commentary lives in sibling notes; generated notes are never hand-edited (or are flipped to `locked: true` and abandoned by the pipeline).
5. The vault is a git repo. Deltas are commits.
6. Every stage transition persists its intermediate artifact **before** advancing state. A paused pipeline is inspectable at exactly the point it stopped.
7. Endorsement is human-only. The pipeline records attestations and computes corroboration; it never sets or modifies your endorsement of a claim.

---

## 2. Repository & Storage Layout

```
knowledge-pipeline/           # the open-source repo
  pipeline/
    orchestrator/             # state machine, workers, scheduler, controls
    ingestors/                # email, rss, youtube, web, paste, annotate
    processors/               # transcribe, diarize, speaker_map, ocr, html2md
    derivers/                 # source_note, claims, dedup, entities, hubs, personal
    vault/                    # note writers, templates, frontmatter schema
    evals/                    # eval runners per stage
  config/
    pipeline.yaml             # model registry, task routing, thresholds, source config
    feeds.yaml                # per-source registry: attribution mode, cadence, limits
    prompts/                  # versioned prompt files: extract_claims_v3.md
  evals/
    golden/                   # golden sets per stage
  dashboard/                  # FastAPI + HTMX control plane + review queue

data/                         # NOT in the repo (gitignored / separate disk)
  pipeline.db                 # SQLite: jobs, controls, runs, cost ledger, dedup index
  raw/
    sha256/ab/cd/abcd1234.../
      artifact.(mp3|html|pdf|eml|md)
      manifest.json           # source url, type, fetch date, ingestor version, hash,
                              # annotates: <hash>  (present when artifact is your notes)
  intermediate/
    abcd1234.../transcript.json, diarization.json, speakers.json, article.md ...

vault/                        # separate git repo — the Obsidian vault
  corpus/
    sources/                  # one note per artifact — ALWAYS written, even at 0 new claims
    claims/                   # atomic insight notes with attestation lists
    entities/                 # people, tools, companies, shows
  personal/
    commentary/               # sibling notes (verbatim or structured)
    experiences/
  hubs/                       # topic maps + changelog digest
```

**Why two repos:** the pipeline is open-sourceable; the vault and raw store are private. Raw blobs live outside git; manifests give integrity via hashes.

---

## 3. Orchestration: SQLite State Machine (replaces Celery/Redis)

**Rationale for the change:** the control-plane requirements — pause a stage, hold one artifact, hand-walk an item, throttle a source, inspect intermediates — are awkward in broker-based queues where jobs are in-flight messages. Making job state *rows in a database* makes every one of those operations a flag flip, and the dashboard becomes a thin UI over the same tables.

### Schema (core tables)

```sql
jobs (
  artifact_hash TEXT, stage TEXT, status TEXT,   -- pending|ready|running|done|failed|held
  attempts INT, claimed_by TEXT, error TEXT,
  input_path TEXT, output_path TEXT,             -- intermediate artifact locations
  created_at, updated_at,
  PRIMARY KEY (artifact_hash, stage)
)
controls (
  scope TEXT,        -- global | stage | source_type | artifact
  key TEXT,          -- e.g. 'transcription', 'podcast', '<hash>', '*'
  state TEXT,        -- running | paused
  batch_limit INT,   -- max items this scope may process per run (NULL = unlimited)
  updated_at, note TEXT
)
runs (run_id, started_at, finished_at, stats_json)      -- batch audit trail
costs (artifact_hash, stage, model, tokens_in, tokens_out, usd, at)
```

### Workers

Plain Python processes, one per resource class (`io`, `gpu`, `llm`, `cheap`). Loop: check `controls` (global → stage → source_type → artifact, most specific wins) → claim one `ready` job atomically (`BEGIN IMMEDIATE`) → execute stage → persist intermediate → mark `done` and set the next stage `ready` → repeat. Failures increment `attempts` with backoff; exhausted attempts land in `failed` (the dead-letter view is just `WHERE status='failed'`).

Scheduler: APScheduler in a supervisor process (or plain cron) triggers `poll` jobs for email/RSS on daily cadence and drains backfill at low priority.

### Control plane operations (CLI + dashboard, same tables)

```
pipeline pause  --stage transcription            # stage-wide breakpoint
pipeline pause  --source podcast                 # source-type breakpoint
pipeline hold    <ref>                           # freeze one artifact mid-pipeline
pipeline release <ref>
pipeline retry   <ref> [--stage X]               # requeue a failed/held job
pipeline throttle --source newsletter --limit 25 # per-run batch cap
pipeline walk    <ref>                           # hand-walk mode: run one stage,
                                                 #   print intermediate path, wait for
                                                 #   confirmation before next stage
pipeline step    <ref> <stage>                   # run exactly one stage, then stop
pipeline status  [<ref>]                         # pipeline-wide or per-artifact view
```

`walk` is both the onboarding tool for each new source type (hand-walk the first item, inspect every intermediate) and the debugging tool when something breaks.

### Per-source independence

Every ingestor is a self-contained module registered in `feeds.yaml` / `pipeline.yaml` with `enabled`, `batch_limit`, `priority`, and cadence. Adding a source type = adding one module + one config entry; nothing else changes. Sources can be brought online one at a time (recommended: email first, one podcast feed second) with tight batch limits until their golden sets and review results look right.

| | Decision |
|---|---|
| **Fork/build** | Build (~300–400 lines for the state machine + workers). This is the piece where owning the code pays off most. |
| **Swap points** | Celery/Redis or Prefect if this ever spans machines — stage contracts don't change, only the claiming mechanism. Postgres if SQLite write contention ever appears (it won't at this scale). |
| **Eval fitness** | Operational: failed-job count, stale `running` jobs (crashed workers), stage latency, cost per stage — all dashboard panels reading these tables. |

---

## 4. Ingestion Connectors

Each ingestor's contract: **fetch → hash → write blob + manifest.json → insert first job row**. Manifest records `source_url, source_type, fetched_at, ingestor_version, content_hash`, plus `annotates: <hash>` when the artifact is personal notes about another artifact.

### 4.1 Email / newsletters (spine)
- **v1:** `imap-tools` against a dedicated inbox/label. Store raw `.eml` (headers = provenance).
- **Fork/build:** build thin (~100 lines).
- **Swap:** JMAP; Gmail API if IMAP quota bites.
- **Eval:** completeness — poller vs. inbox reconciliation on dashboard. Failure mode is silent misses, not bad quality.

### 4.2 Podcasts (spine)
- **v1:** `feedparser` + `podcastparser`, download enclosures, per-feed state (last seen GUID). Attribution mode read from `feeds.yaml` (see §6.0).
- **Fork/build:** build thin. Don't fork a podcatcher.
- **Swap:** none needed — RSS is the stable layer; private Patreon-style feeds are still RSS with a token URL.
- **Eval:** feed-level reconciliation.

### 4.3 YouTube (ad hoc)
- **v1:** `pipeline add youtube <url> [--notes file.md]`. Pull official captions via yt-dlp when present (skips transcription entirely); fall back to audio + Whisper.
- **Fork/build:** use yt-dlp as-is. Pin version, auto-`--update` on extraction failure, then `failed` status.
- **Eval:** operational — failed-job rate.

### 4.4 Web articles (ad hoc)
- **v1:** fetch + `trafilatura`; store raw HTML snapshot AND extracted markdown. Archive.org fallback.
- **Swap:** readability-lxml, Mozilla readability, LLM-extraction for stubborn pages. Raw HTML stored → whole archive re-extractable on swap.
- **Eval:** golden set of ~15 saved pages with approved extractions; overlap score + manual diff on swap.

### 4.5 Paste / ad hoc text (X threads, LinkedIn, book notes)
- **v1:** `pipeline add paste` (stdin/file/clipboard) with `--url`/`--type` for provenance. Sidesteps scraping ToS.

### 4.6 Papers / PDFs
- **v1:** `docling` or `marker` behind a `PdfProcessor` interface.
- **Swap:** MinerU, nougat — fast-moving space, hence the interface.
- **Eval:** golden set of 5–10 PDFs, manual fidelity rubric on swap.

### 4.7 Personal notes / annotation (NEW)
- **v1:** two entry points, one mechanism:
  - `pipeline add <source> --notes mynotes.md` — notes captured at ingestion time.
  - `pipeline annotate <ref> [--notes file.md | stdin]` — attach notes to anything already ingested (also a dashboard button on any source/claim note).
- Notes are stored as their own raw artifact (`source_type: personal_note`, manifest `annotates: <source_hash>`). They are **invisible to the corpus derivation chain** — the source is always processed as if no notes exist, so LLM output is never contaminated by your framing.
- A separate **personal deriver** produces `personal/commentary/<source-id>.md`:
  - **Default: verbatim.** Your words are the artifact; no LLM touches them. The note gets `authority: personal`, links to the source note, and inherits the provenance chain (including timestamps/anchors if you wrote any).
  - **Opt-in `--structured`:** an LLM pass splits your notes into atomic personal claim notes, each linked to the corpus claims they address. This is also the channel through which endorsements flow (see §6.3): a structured personal note can carry `endorses: [claim-id]` / `disputes: [claim-id]`.
- **Fork/build:** build. **Eval:** verbatim path needs none; structured path shares the derivation eval protocol (§6.4).

---

## 5. Processing Stages

### 5.1 Transcription
- **v1:** `faster-whisper` (large-v3 / distil-large-v3), GPU preferred, CPU acceptable (no freshness pressure). Behind a `Transcriber` interface returning normalized transcript JSON (segments, timestamps, confidence).
- **Swap:** WhisperX (adds alignment + integrated diarization), NVIDIA Parakeet/Canary, future OSS. Adapters to the JSON schema.
- **Eval:** **WER on a golden set** of ~10 five-minute clips spanning your real content mix (solo show, panel, conference talk, accented speakers). `pipeline eval transcription --engine X`. Cleanest objective swap gate in the system.

### 5.2 Diarization + speaker mapping
- **Gating is attribution-driven (see §6.0):** sources with `attribution: show` skip this stage entirely. `guest` and `hybrid` sources run it.
- **v1:** `pyannote.audio` 3.x (or WhisperX's integrated path). Adds `speaker` per segment.
- **Speaker mapping (new sub-stage):** resolve `SPEAKER_00/01` to entity notes using episode metadata (guest names from RSS/show notes) + the intro segment, via a cheap LLM call. Output `speakers.json`: diarization label → entity. Host(s) declared per-feed in `feeds.yaml` so they're mapped deterministically.
- **Swap:** NVIDIA NeMo MSDD, future entrants.
- **Eval:** **DER** on golden clips + downstream attribution spot-check (does the derived claim credit the right person on crosstalk-heavy shows?). Speaker mapping gets its own micro-eval: 5 episodes with hand-verified mappings.

### 5.3 HTML/email → Markdown normalization
- **v1:** trafilatura (articles) / `mailparser` + html2text (newsletters). Eval covered by §4.4.

---

## 6. Derivation (LLM stages) — the opinionated core

**Build from scratch.** llmwiki and Hyper-Extract are inspiration, not forks: steal llmwiki's wiki-page framing and Hyper-Extract's n-ary relations (implemented as relation-notes linking 3+ participants), write your own chain.

### 6.0 Attribution modes (per-source processing guidance)

Declared in `feeds.yaml` per source (feed, channel, blog), overridable per-artifact:

```yaml
feeds:
  - id: podcast:acquired
    attribution: show          # insights attributed to the show/episode only
    diarize: false             # derived from attribution
  - id: podcast:invest-like-the-best
    attribution: guest         # insights attributed to speaker entity + episode
    hosts: [Patrick O'Shaughnessy]
    diarize: true
  - id: podcast:some-panel-show
    attribution: hybrid        # host segments → show; guest segments → guest entity
  - id: blog:stratechery
    attribution: show          # single-author blog: author == source, no split needed
```

- **show** = source-neutral: claims link to the show/episode entity. Cheapest path (no diarization).
- **guest** = each claim carries both the episode link AND the speaking entity as author.
- **hybrid** = per-segment routing using the speaker map.
- Optional for blogs/YouTube channels (multi-author blogs → per-post author attribution).

### 6.1 Stage chain per artifact
1. **Source note** — metadata, abstract, structure map, link to raw, attribution mode applied. **Always written**, even when the artifact yields zero new claims — the note records `claims_added: 0, claims_matched: n`, turning "one-note IP" sources into coverage data instead of silence, and marking the artifact as processed for reconciliation.
2. **Claim extraction** — candidate atomic claims, each with supporting quote + timestamp/paragraph anchor + author entity (per attribution mode). Cheap model, batched.
3. **Claim dedup / attestation (NEW)** — for each candidate: embed → shortlist nearest existing claims (sqlite-vec) → cheap LLM confirms same-claim vs. distinct → on match, **append an attestation** to the existing claim note instead of creating a duplicate; on miss, create a new claim note. This is the rolling-linkage mechanism: a point made on a podcast and repeated in a newsletter accrues to one claim with two attestations.
4. **Entity extraction & linking** — people/tools/companies/shows; resolve against existing entities before creating (embedding + string match, LLM tiebreak).
5. **Personal deriver** — commentary siblings (§4.7), runs after corpus derivation so links resolve.
6. **Synthesis passes** — hub updates, cross-source connection surfacing, conflict detection against `personal/`. Frontier model, weekly batch.

### 6.2 Claim note anatomy: attestation vs. corroboration vs. endorsement

Three strictly separate concepts, because repetition ≠ validity:

```yaml
---
type: claim
title: "Generation cost is low; taste/selection is the differentiator"
attestations:                      # AUTOMATIC — provenance, appended by pipeline
  - {source: sources/podcast-xyz-e412, anchor: "00:41:22", author: entities/jane-doe, date: 2026-06-01}
  - {source: sources/newsletter-abc-2026-06-15, anchor: "¶14", author: entities/jane-doe, date: 2026-06-15}
independent_sources: 1             # COMPUTED — count of DISTINCT author entities,
                                   #   not distinct artifacts (same person twice = 1)
endorsement: null                  # HUMAN-ONLY — null | endorsed | disputed
endorsed_via: null                 # link to your commentary note when set
---
```

- `attestations` grow automatically and are pure provenance.
- `independent_sources` is computed corroboration — descriptive, never treated as truth. Same-author repetition across media does **not** increase it.
- `endorsement` is set only by you — via dashboard button on the claim, or via a structured personal note carrying `endorses:`/`disputes:`. The pipeline never writes this field. Retrieval ranks endorsement above corroboration count, corroboration above bare attestation.

### 6.3 Model routing

All LLM calls through **LiteLLM** with a task→model registry:

```yaml
models:
  extract_claims:   {model: claude-haiku-4-5,  fallback: gemma-local, batch: true}
  dedup_confirm:    {model: claude-haiku-4-5,  batch: true}
  speaker_map:      {model: claude-haiku-4-5,  batch: true}
  structure_notes:  {model: claude-sonnet-4-6, batch: true}
  synthesize_hub:   {model: claude-sonnet-4-6, batch: false}
prompts:
  extract_claims: prompts/extract_claims_v3.md
embeddings:
  dedup: {model: nomic-embed-text-v1.5, local: true}   # write-time, day one
```

Registry = swap point: Haiku ↔ local Gemma/Qwen is a config change. Use the Anthropic **Batch API** for all non-interactive derivation (50% discount; zero freshness pressure).

### 6.4 Eval
- **Golden set:** 10 artifacts across types with hand-approved derived notes.
- **Rubric + LLM-as-judge** (claim atomicity, quote faithfulness, no hallucinated claims, entity precision, correct attribution mode) — for *relative* comparison between model/prompt versions only.
- **Dedup gets its own micro-eval:** ~30 claim pairs hand-labeled same/distinct; report precision/recall. False merges are worse than false splits (a wrongly merged claim pollutes attestations); tune the confirm threshold conservative.
- **Spot-check protocol:** every prompt/model change → personal review of N=10 outputs via the review queue before promotion. Flagged notes feed golden sets — review compounds into better swap decisions.
- `pipeline eval derivation --model X --prompt extract_claims_v4` → side-by-side diff report.

---

## 7. Vault Writer & Note Conventions

**Build from scratch** (templates + `python-frontmatter`). Frontmatter manifest on every derived note:

```yaml
---
type: claim            # source | claim | entity | hub | relation | commentary
authority: derived     # derived | personal
source_hash: abcd1234
source_url: https://...
anchor: "00:41:22"
attribution_mode: guest
pipeline_version: 0.4.0
prompt_version: extract_claims_v3
model: claude-haiku-4-5
derived_at: 2026-07-09
locked: false          # true = human-owned, pipeline hands off
---
```

**Sibling commentary convention:** `personal/commentary/<note-id>.md`, `authority: personal`, embeds/links the derived note, shares `source_hash`. Retrieval ranks `personal` above `derived` and surfaces conflicts explicitly.

---

## 8. Delta Tracking & Reprocessing

- Vault is git. Pipeline commits per batch (`[ingest] 12 sources`, `[rederive] extract_claims v3→v4, 214 notes`, `[attest] 9 claims gained attestations`).
- **Source deltas:** re-fetch → hash differs → re-derive → git diff is the delta report. Weekly `hubs/changelog.md` digest generated from git log — deltas visible inside Obsidian.
- **Derivation deltas:** `pipeline rederive --prompt extract_claims_v4 --scope corpus/claims` regenerates in a **branch**; review sampled diff; merge. Model upgrades are reversible.
- `locked: true` notes skipped by rederive, listed in digest. Endorsements and attestation lists are preserved across rederivation (they're merged forward, not regenerated).

---

## 9. Dashboard = Control Plane + Review Queue

**v1:** FastAPI + HTMX over `pipeline.db` directly. Panels:

1. **Control plane:** per-stage and per-source pause/resume toggles, batch-limit sliders, per-artifact hold/release/retry — writing the `controls`/`jobs` tables the workers read. Pausing `transcription` while you eval a new Whisper build is one click; nothing downstream corrupts because upstream state just accumulates in `ready`.
2. **Pipeline state:** queue depths per stage, failed jobs with error + retry button, stale `running` detection, per-artifact stage timeline (click any stage → view its intermediate artifact).
3. **Reconciliation:** per-source feed-count vs. ingested-count; sources with `claims_added: 0` trendlines (coverage view).
4. **Cost ledger:** tokens/$ per stage per week from the `costs` table.
5. **Review queue:** newly derived notes for spot-check — approve / flag / annotate (annotate creates a sibling note, §4.7) / endorse-dispute (writes endorsement, §6.2). Flagged items promotable to golden sets in one click.

**Fork/build:** build the app; it's thin because the DB is the API. No Flower needed anymore — the state machine made it redundant.
**Swap:** Streamlit if you want faster/uglier; Grafana only if multi-user ever happens.

---

## 10. Agent Interface

- **v1:** **MCP server over the vault** — `search_notes`, `get_note`, `get_source_chain` (note → source → raw manifest), `get_claim_attestations`, `list_conflicts`, `recent_deltas`. Plugs into Hermes/OpenClaw and Claude Code alike.
- **Fork/build:** fork an existing Obsidian MCP server for transport plumbing if convenient; the retrieval semantics (authority ranking, endorsement > corroboration > attestation, provenance chains, conflict surfacing) are your ontology — build those.
- **Retrieval v1:** lexical — ripgrep + frontmatter filters + backlink traversal over well-titled atomic notes. 
- **Embeddings, two distinct uses:**
  - **Write-time dedup (§6.1 step 3): in from day one.** Local model + sqlite-vec, tiny scope (claim titles + bodies), negligible cost. Required by the rolling attestation mechanism.
  - **Query-time retrieval: deferred behind a tripwire.** Add semantic search only when the golden-query eval says lexical fails — concretely: recall@5 < 80% on the golden query set for two consecutive evals. By then the embedding infra already exists via dedup; adding a retrieval index is incremental. Record `embedding_model` + version in index metadata; re-embedding the whole vault on model swap is minutes at this scale.
- **Eval:** golden query set (~20 real questions) with expected supporting notes; **recall@k** rerun on any retrieval change. The eval that matters most — the whole system exists to serve this interface.

---

## 11. Versioning & Swap-Point Registry (summary)

| Stage | Interface contract | v1 component | Swap candidates | Eval gate for swapping |
|---|---|---|---|---|
| Orchestration | jobs/controls schema; task = 1 stage × 1 artifact | SQLite state machine (custom) | Celery/Redis, Prefect (multi-machine); Postgres | operational metrics |
| Email ingest | blob + manifest | imap-tools | JMAP, Gmail API | reconciliation counts |
| Podcast ingest | blob + manifest | feedparser | — | reconciliation counts |
| YouTube | blob + manifest | yt-dlp | — | failed-job rate |
| Article extract | raw HTML + md | trafilatura | readability, LLM-extract | golden HTML set, overlap |
| PDF | structured md | docling / marker | MinerU, nougat | golden PDF rubric |
| Transcription | transcript JSON schema | faster-whisper | WhisperX, Parakeet | **WER on golden clips** |
| Diarization | speaker field in JSON | pyannote 3.x | NeMo, WhisperX path | **DER + attribution spot-check** |
| Speaker mapping | speakers.json | Haiku + feed metadata | any cheap model | 5-episode hand-verified set |
| Claim extraction | prompt files + registry | Haiku via LiteLLM batch | local Gemma/Qwen | golden set + judge + N=10 review |
| Claim dedup | embed + confirm contract | nomic-embed + Haiku confirm | any embed model / local LLM | **precision/recall on 30 labeled pairs** |
| Synthesis | prompt files + registry | Sonnet | frontier of the day | human review |
| Vault writer | frontmatter schema | custom | — | schema validation in CI |
| Retrieval | MCP tool contracts | ripgrep + frontmatter | + sqlite-vec semantic index | **recall@k on golden queries** |

Version everything that affects output: `pipeline_version` (repo tag), `prompt_version` (file), `model` (registry), `ingestor_version` (per connector), `embedding_model` (index metadata). All in frontmatter/metadata → any note fully explainable and reproducible.

---

## 12. Build Sequence (Claude Code handoff order)

1. **Skeleton:** repo layout, `pipeline.yaml`/`feeds.yaml`, **blob-store interface (local dir ↔ S3, config-selected)** + manifest writer, SQLite schema (jobs/controls/costs), `pipeline add paste`, `pipeline annotate`, vault writer + frontmatter schema, git commit hook. **Deploy to the always-on EC2 box at the end of this step** (docker compose up; nginx subdomain; Litestream). *(Capture — including your own notes — starts day one, on the box.)*
2. **State machine:** workers, claiming, control checks, `pause/hold/retry/throttle/walk/step/status` CLI over SSH. Test by hand-walking pasted artifacts.
3. **Spine ingestion:** email poller (IMAP for backlog) + SES inbound address for go-forward newsletters + podcast poller (with `feeds.yaml` attribution modes) + APScheduler cadence.
4. **Audio chain:** faster-whisper + pyannote + speaker mapping behind interfaces; transcription/diarization golden sets + eval commands. CPU path first on the main box; **manifest-based spot GPU burst worker** (`pipeline burst`) once the CPU path is validated.
5. **Derivation v1:** source-note + claim-extraction prompts via LiteLLM batch; **dedup with sqlite-vec + confirm step**; attestation append logic; personal deriver (verbatim path).
6. **Dashboard:** control plane + state + reconciliation + cost + review queue (with endorse/dispute and annotate actions).
7. **Backfill:** checkpointed backfill with per-source throttles; start with the inbox + 2–3 podcast catalogs (validate ontology and claim granularity on a bounded corpus before opening the floodgates).
8. **Entities + hubs + MCP server;** retrieval golden queries; structured personal notes path.
9. **Rederive machinery + changelog digest** (with attestation/endorsement merge-forward). Then iterate ontology with confidence.

---

## 13. Open questions to settle during the build

- Claim-note granularity threshold — decide empirically at step 7, not in advance. The `claims_added: 0` coverage view will inform this.
- Dedup confirm threshold — tune conservative (false splits over false merges) against the 30-pair labeled set.
- Whether `hybrid` attribution needs per-segment review UI or the speaker map is trustworthy enough — decide after the 5-episode eval.
- Retrieval embeddings tripwire is set (recall@5 < 80% twice consecutively) — revisit the threshold once the golden query set exists.
- Licensing (leaning MIT) and copyright-clean public examples — deferred until the system runs.

---

## 14. Infrastructure (AWS / EC2)

**Deployment philosophy:** no local-first phase and no cloud-only lock-in. The system is Python + SQLite + a blob store; the *only* cloud-specific abstraction is the blob-store interface (`BlobStore: local | s3`, selected in `pipeline.yaml`). Deploy the skeleton to EC2 at the end of build step 1 and develop against the box via SSH thereafter. Local mode remains fully functional and is part of the open-source replication story ("run it on a laptop or a $25/mo instance").

### 14.1 Compute: two-tier split

**Tier 1 — always-on control box** (t4g.medium or t3a.medium, ~$25–30/mo):
- Runs: orchestrator + all workers except heavy audio (LLM stages are network-bound API calls, not compute), ingestors/pollers, dashboard, vault git, Litestream.
- **Single writer to SQLite** — this is what lets SQLite stay. No Postgres, no broker, no shared-database-across-machines problem.
- Docker Compose stack: `orchestrator`, `worker-io`, `worker-llm`, `worker-cheap`, `dashboard`, `litestream`. One `docker compose up`.

**Tier 2 — ephemeral spot GPU burst worker** (g4dn.xlarge spot, ~$0.16–0.21/hr):
- **Manifest pattern — the burst worker never touches the database:**
  1. Orchestrator selects queued audio jobs, writes a work manifest (artifact hashes + S3 input paths + engine config) to S3.
  2. Launches spot instance via boto3 with a user-data script: pull CUDA worker image, fetch manifest, transcribe/diarize, write results to S3 under each artifact's key, write a completion marker, **self-terminate**.
  3. Main box polls for completion markers, ingests results into `intermediate/`, advances job state.
- Spot interruption = missing completion marker = jobs stay `running` until stale-detection requeues them. Nothing corrupts; you re-run pennies of work.
- **v1 trigger is manual:** `pipeline burst --stage transcription [--instance g4dn.xlarge]`. Queue-depth-triggered auto-burst is a later nicety, not a v1 requirement.
- **Cost rationale:** a 200-hour audio backlog ≈ $25–35 and 35–50 wall-hours on CPU vs. **$2–4 and 10–20 hours on spot GPU** at 10–20× realtime. Steady-state weekly audio (~10–20 hrs) is a sub-$1 burst or can just run on the main box's CPU overnight — validate the CPU path first (build step 4), add burst second.
- The manifest pattern generalizes: any stage that becomes a bottleneck can be burst to a bigger box the same way. This is the throughput-management answer.

### 14.2 Storage

| Data | Location | Notes |
|---|---|---|
| Raw blobs | **S3** (Standard; lifecycle → IA at 90d) | Content-addressed keys mirror local layout (`raw/sha256/ab/cd/...`). Durable, cheap, and what makes burst workers natural. |
| Intermediates | EBS gp3 on main box | Regenerable; 50–100 GB. Optionally mirrored to S3 for burst-worker handoff. |
| `pipeline.db` | EBS + **Litestream → S3** | Continuous streaming replication = point-in-time backup for ~$0. Restore = `litestream restore`. |
| Vault | git, canonical on main box | Push mirror to private GitHub. **Laptop runs the obsidian-git plugin with auto-pull** — the vault appears as a normal local Obsidian vault that a remote machine keeps updating; your commentary edits push back through the same channel. Commit-race protection: pipeline commits on a branch or pulls-rebase before its batch commit. |
| Models (Whisper, pyannote, embeddings) | Baked into worker images or cached on EBS/S3 | Avoids HuggingFace pulls on every burst boot. |

### 14.3 Network & access

- **Dashboard:** subdomain on the existing nginx reverse proxy (e.g. `pipeline.<domain>`), wildcard TLS already in place. **Auth floor: basic auth over TLS + fail2ban** — acceptable for v1 but note the dashboard mutates pipeline state; upgrade path is Authelia/Cloudflare Access/Tailscale. Never expose the dashboard port directly; nginx only.
- **CLI:** SSH onto the box (key-only, security group locked to your IP or Tailscale). The CLI is the same package the workers use — no separate client/server protocol to build.
- **Burst instances:** public subnet + locked-down SG (no inbound; egress only) to avoid NAT gateway cost; S3 gateway endpoint for free S3 traffic; IAM role scoped to the manifest/result prefixes only.
- **Secrets:** SSM Parameter Store (API keys, IMAP creds, pyannote token); S3/EC2 access via **IAM instance profiles — no AWS keys on disk**.
- **Email, AWS-native option:** point MX for a subdomain (e.g. `in.<domain>`) at **SES inbound receiving → S3**. Newsletters arrive as raw MIME objects in the raw store's staging prefix with zero polling, zero OAuth, zero provider quirks — the most stable and most replicable ingestion path. Keep IMAP for the existing inbox backlog; re-subscribe or auto-forward go-forward traffic to the SES address.

### 14.4 Replicability (the open-source story)

- **One Terraform module:** VPC-lite (or default VPC), main instance + EBS, S3 bucket + lifecycle, IAM roles/instance profiles, SSM parameters (names only), SES receipt rule (optional flag), security groups. `terraform apply` + a bootstrap script (`cloud-init` → docker compose) = running system.
- Everything above degrades gracefully to local mode: `BlobStore: local`, no SES (IMAP only), no burst (CPU transcription), dashboard on localhost. CI runs the eval suite in local mode.
- **Fork/build:** build the Terraform module and burst launcher (thin, boto3); use Litestream, nginx, compose as-is.

### 14.5 Cost envelope (steady state)

| Item | ~$/mo |
|---|---|
| Always-on instance (t4g.medium) | 25–30 |
| EBS 100 GB gp3 | 8 |
| S3 (first ~50 GB raw + Litestream) | 2–4 |
| Route53 / misc | 1–2 |
| Spot GPU bursts (weekly audio) | 1–5 |
| LLM batch API (Haiku-dominant) | 5–15 |
| **Total** | **~$45–65** |

Backfill months add one-time burst + batch-API spend (tens of dollars for hundreds of hours of audio), not a new baseline.

### 14.6 Infra eval/swap points

| Concern | v1 | Swap/upgrade | Trigger |
|---|---|---|---|
| DB | SQLite (single writer) | Postgres container | second *persistent* machine ever needs write access (burst workers don't count — manifest pattern) |
| Burst trigger | manual `pipeline burst` | queue-depth auto-scaler | you notice yourself running the command on a schedule |
| Dashboard auth | basic auth + TLS | Authelia / Tailscale / CF Access | opening access to anyone but yourself |
| Instance size | t4g.medium | t4g.large / c7g | sustained CPU saturation on dashboard latency or poller lag |
| Region/replication | single region | S3 CRR | only if the vault becomes irreplaceable beyond git mirrors (it shouldn't — raw store is the system of record) |
