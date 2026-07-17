# knowledge-pipeline

Batch-oriented ingestion → distillation → Obsidian vault pipeline with a
provider-agnostic LLM layer and a SQLite control plane. See
[`knowledge-pipeline-plan.md`](knowledge-pipeline-plan.md) for the full design.

The pipeline turns raw sources (pasted text today; email/podcasts later) into a
git-backed vault of atomic, cross-linked **claim** notes — each one reproducible
from its source and the exact model that produced it, and corroborated by every
source that repeats it.

## What works today

- **Content-addressed raw store** with provenance manifests (`local` ↔ `s3`,
  config-selected — the only cloud-specific abstraction).
- **SQLite control plane** — the DB *is* the queue, control plane, and dashboard
  backend (`jobs`, `controls`, `runs`, `costs`, `claims` tables).
- **Provider-agnostic LLM layer** — one OpenAI-compatible adapter serves
  **OpenAI, OpenRouter, and Ollama**; each derivation stage independently picks
  its provider + model + params. Every call logs tokens, **cost, and latency**.
- **Real derivation chain** — `source_note` → `extract_claims` (LLM) → `dedup`
  (embed + sqlite-vec nearest + LLM confirm → attest-or-create) → `entities`
  (stub). Claims that recur across sources collapse into one note with multiple
  **attestations**.
- **Git-backed vault** — frontmatter+markdown notes committed per batch; every
  derived note carries its full generating key.
- **State machine** — resource-class workers with atomic (`BEGIN IMMEDIATE`)
  claiming, per-scope pause/hold/throttle, and hand-walk / single-step tools.
- **Compose stack** — one `docker compose up` runs the tier-1 box locally.

**Not yet built:** `entities` derivation (stub), eval-compare and promote
workflows (designed — see [Planned workflows](#planned-workflows)), email/RSS/
YouTube/web ingestors, the audio chain + GPU burst, and the dashboard control
plane. AWS provisioning lives in [`infra/`](infra/) (Terraform).

## How it works

```
  ingest            derivation chain (per source_type)             vault (git repo)
 ┌────────┐   ┌────────────┬───────────────┬────────┬──────────┐  ┌──────────────┐
 │ paste  │──▶│ source_note│ extract_claims│ dedup  │ entities │─▶│ corpus/…     │
 │ annotate│  │ (no LLM)   │ (LLM producer)│(embed +│ (stub)   │  │ personal/…   │
 └────────┘   └────────────┴───────────────┴─LLM)───┴──────────┘  └──────────────┘
       │              every stage = a job row in SQLite; workers          │
       ▼              claim → run → advance. Controls gate claiming.      ▼
  raw store (content-addressed blobs + manifest)              intermediates keyed by
                                                              the generating key
```

Two ideas do most of the work:

- **The generating key** — `(provider, model, params, prompt_version, input_hash)`
  is stamped into every note's frontmatter and used to name each stage's
  intermediate. Any note is reproducible; two configs over the same source never
  clobber each other.
- **Produce ≠ commit** — producer stages (`extract_claims`) write only a keyed
  intermediate; committer stages (`dedup`) write the vault. That separation is
  what makes eval-compare, offline runs, and promote clean to add.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and (for local LLM runs)
[Ollama](https://ollama.com/).

```bash
uv sync
cp .env.example .env          # add provider keys you'll use (see below)
uv run pipeline init          # create data/pipeline.db + the vault git repo
```

### Providers

Declared once in `config/pipeline.yaml` under `providers:`, referenced per stage
under `models:`. Keys are read from the environment (via `.env` locally; from the
instance environment on the box).

| Provider   | `.env` key            | Notes                               |
|------------|-----------------------|-------------------------------------|
| OpenAI     | `OPENAI_API_KEY`      | Batch API available (50% off, later)|
| OpenRouter | `OPENROUTER_API_KEY`  | Many models via one key             |
| Ollama     | *(none)*              | Local, free — just run `ollama serve`|

**To run fully local / free**, point stages at Ollama and pull the models:

```bash
ollama pull gemma4:latest        # extraction + dedup-confirm (or your pick)
ollama pull nomic-embed-text     # dedup embeddings
```

then set the stage in `config/pipeline.yaml`:

```yaml
models:
  extract_claims: {provider: ollama, model: gemma4:latest, params: {temperature: 0}, prompt_version: v1}
```

`embeddings:` (dedup) and `models.dedup:` (the same-claim confirm model) already
default to Ollama.

## Common operations

### Ingest

```bash
echo "Taste is the differentiator; generation is cheap." \
  | uv run pipeline add paste --url https://example.com/post
uv run pipeline add paste --text "..." --url https://x        # or inline
uv run pipeline add paste --file notes.md                     # or from a file

# Personal notes about an artifact — stored separately, invisible to corpus derivation:
echo "I disagree — distribution still wins." | uv run pipeline annotate <ref>
```

### Run the pipeline

```bash
uv run pipeline worker cheap --once     # runs source_note, dedup
uv run pipeline worker llm --once       # runs extract_claims
# or drive it by hand:
uv run pipeline step <ref> extract_claims   # exactly one stage
uv run pipeline walk <ref>                  # one stage at a time, with confirmation
uv run pipeline status [<ref>]              # pipeline summary or one artifact's timeline
```

Derived claim notes land in `vault/corpus/claims/`; each records its generating
key and an `## Attestations` section listing every source that asserted it.

### Control plane

```bash
pipeline pause --stage extract_claims     # accumulate upstream in `ready`
pipeline pause --source podcast
pipeline resume --stage extract_claims
pipeline hold <ref> / release <ref>       # freeze one artifact mid-pipeline
pipeline retry <ref> [--stage X]          # requeue failed/held
pipeline throttle --source newsletter --limit 25
```

### Swap models / providers per stage

The `models:` map in `config/pipeline.yaml` is the swap point — change a stage's
`provider`/`model`/`params` and rerun. Because intermediates are keyed by the
generating key, re-running a source under a new model produces a *new* keyed
output rather than overwriting the old one.

### Inspect cost & latency

Every LLM call writes to the `costs` table — the substrate for benchmarking
quality/cost/latency per step and model:

```bash
uv run python -c "from pipeline.db import bootstrap; \
  [print(dict(r)) for r in bootstrap('data/pipeline.db').execute(\
  'SELECT stage,provider,model,tokens_in,tokens_out,usd,latency_ms FROM costs ORDER BY at DESC LIMIT 20')]"
```

### Dashboard / compose stack

```bash
docker compose up --build        # dashboard → http://localhost:8000
```

`litestream` replicates `data/pipeline.db` to a local file replica out of the
box; swap `docker/litestream.yml` to an S3 replica for the EC2 deployment.

## Planned workflows

Designed against the produce/commit boundary above, **not yet implemented** —
documented here as the intended UX and the spec for their slices.

### Evals (eval-compare)

Mark a source for evaluation and run a stage under two `{provider, model, params}`
configs; both outputs are held (as separate keyed intermediates) for side-by-side
comparison of quality, cost, and latency before you approve one into the vault.
Your first real runs are effectively the baseline eval. *(Intermediates are
already keyed by the generating key; the compare/approve control flow is the
remaining work.)*

### Uploading a local run (promote)

Validate a source fully on your laptop (any provider/model), then publish the
finished notes to the production vault with **zero recompute**: `promote` pushes
the vault notes (git), the raw blob (→ S3, so the note stays regenerable there),
and a minimal `done` record. Intermediates don't travel — they regenerate on
demand from `raw + generating key`.

## Config

`config/pipeline.yaml` — blob store (`local` | `s3`), paths, `providers:`,
per-stage `models:`, `embeddings:`, `dedup:` tuning, and worker settings.
`config/prompts/` — versioned prompt files (`<stage>_<version>.md`).
`config/feeds.yaml` — per-source attribution registry (spine ingestors, later).

## Layout

```
pipeline/
  config.py            resolved runtime settings + per-stage model resolution
  llm/                 provider protocol, OpenAI-compatible adapter, registry, prompts
  db/                  schema, connection, jobs, controls, costs, claims_index (sqlite-vec)
  storage/             blob store interface + manifest writer
  orchestrator/        stage registry, executor, handlers (derivers), worker loop
  ingestors/           paste, annotate
  vault/               frontmatter schema + git-backed note writer
dashboard/             read-only FastAPI state view
docker/                Dockerfile + litestream config
infra/                 AWS foundation (Terraform)
config/                pipeline.yaml, feeds.yaml, prompts/
```
