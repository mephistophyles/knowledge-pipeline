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
  **OpenAI, OpenRouter, NVIDIA, and Ollama**; each derivation stage independently
  picks its provider + model + params. Every call logs tokens, **cost, and latency**.
- **Real derivation chain** — `source_note` → `extract_claims` (LLM) → `dedup`
  (embed + sqlite-vec nearest + LLM confirm → attest-or-create) → `entities`
  (extract + resolve → link-or-create). Claims and entities that recur across
  sources collapse into one note with multiple **attestations** / **mentions**.
- **Author identity** — attribution is anchored to the *person*, not the channel.
  One writer's newsletter, site, and guest posts resolve to a single identity, so
  the same essay arriving twice cannot corroborate itself. Corroboration counts
  distinct **voices**, never attestation entries. Unmapped channels attest
  *provisionally* and are queued for review in the dashboard rather than blocking
  ingestion.
- **Web ingestion** — fetch (robots-respecting, rate-limited) or import a page you
  saved from the browser; both land in one content-addressed archive, extract via
  trafilatura, and pass a quality gate that holds teasers, index pages, and
  non-English rather than deriving them.
- **Re-derivation is explicit** — `pipeline retract` withdraws an artifact's claims
  before a re-run, because claim ids encode extraction position and writing a second
  pass over a first would silently reassign them.
- **Eval-compare** — run a stage under several `{provider, model, params}`
  variants over one source and compare outputs + cost + latency before approving
  one into the vault (see [Common operations](#common-operations)).
- **Git-backed vault** — frontmatter+markdown notes committed per batch; every
  derived note carries its full generating key.
- **State machine** — resource-class workers with atomic (`BEGIN IMMEDIATE`)
  claiming, per-scope pause/hold/throttle, and hand-walk / single-step tools.
- **Compose stack** — one `docker compose up` runs the tier-1 box locally.

**Not yet built:** the promote workflow (designed — see
[Planned workflows](#planned-workflows)), email/RSS/YouTube/web ingestors, the
audio chain + GPU burst, claim↔entity linking, and the dashboard control plane.
AWS provisioning lives in [`infra/`](infra/) (Terraform).

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

| Provider   | `.env` key            | Notes                                     |
|------------|-----------------------|-------------------------------------------|
| OpenAI     | `OPENAI_API_KEY`      | Batch API available (50% off, later)      |
| OpenRouter | `OPENROUTER_API_KEY`  | Many models via one key                   |
| NVIDIA     | `NVIDIA_API_KEY`      | build.nvidia.com; extraction + embeddings |
| Ollama     | *(none)*              | Local, free — just run `ollama serve`     |

The default config uses no local models: extraction and entities run on NVIDIA,
dedup-confirm on OpenRouter, embeddings on NVIDIA. That is deliberate — a t3.small
cannot host Ollama, so any local dependency blocks deployment.

**To run fully local / free**, point stages at Ollama and pull the models:

```bash
ollama pull gemma4:latest        # extraction + dedup-confirm (or your pick)
ollama pull nomic-embed-text     # dedup embeddings
```

then set the stage in `config/pipeline.yaml`:

```yaml
models:
  extract_claims: {provider: ollama, model: gemma4:latest, params: {temperature: 0}, prompt_version: v1}
embeddings:
  provider: ollama
  model: nomic-embed-text
```

**Changing the embedder is a migration, not a setting.** `max_distance` is
embedder-specific — L2 distances are not comparable across models, so an inherited
threshold is meaningless — and a `vec0` index is created at a fixed width, so a new
dimensionality cannot be written into the old one. After any change to
`embeddings.model`:

```bash
uv run pipeline reembed --apply   # rebuild claims_vec + entities_vec
```

Vectors are derived data (`claims.text` is stored), so this is always reversible.

Note `embeddings.input_type`. Asymmetric embedders (NVIDIA, Cohere, Voyage) bake a
usage mode into the vector, and claim-to-claim dedup is *symmetric*, so both sides
are embedded as `passage`. Omitting it is not a safe default: on
`nemotron-3-embed-1b` the unconditioned vector is nearly orthogonal to both real
modes, and dedup ranking fell to **worse than chance**. Providers that don't know
the field ignore it.

## Common operations

### Ingest

Web pages — fetched where robots.txt permits, or imported from a page you saved in
the browser (no server request, so robots has nothing to say about it):

```bash
uv run pipeline web audit commoncog.com          # robots, sitemaps, feeds, terms
uv run pipeline web scan --file backlog.md       # or pass URLs directly
uv run pipeline web import saved.html            # or a directory of saves
uv run pipeline web derive                       # extract → artifacts → chain
uv run pipeline web held                         # what the quality gate distrusted
```

Weekly email batches are Saturday→Friday windows, where the window *is* the cursor:
pulling a given week is the same operation whenever it runs, and re-running costs
only the re-read because archiving keys on content hash.

```bash
uv run pipeline feed weeks                       # windows + what each pulled
uv run pipeline feed pull --catch-up             # every complete week
```


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

### Author identity

Attribution is anchored to the person who wrote something, not the address or host
it arrived through. `config/identities.yaml` is the canonical record — one id per
author, however many publications they write through:

```bash
uv run pipeline identity harvest --apply   # propose identities from archived headers
uv run pipeline identity sync              # apply config/identities.yaml as curated
uv run pipeline identity unmapped          # channels still needing a decision
uv run pipeline identity apply --apply     # upgrade provisional attestations after mapping
```

Day to day this lives in the dashboard at `/authors`, which lists every unmapped
channel with a one-line form to attach it to an existing author or name a new one.
Saving writes through to the YAML and takes effect on the corpus immediately.
Shared platforms are listed per writer (`medium.com/@stewart`), never per host —
mapping a whole platform to one identity would merge distinct people.

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

### Eval-compare a stage across models

Benchmark a producer stage (e.g. `extract_claims`) across several
`{provider, model, params}` variants over one source before committing anything.
List the variants under `evals.<stage>` in `config/pipeline.yaml`, then:

```bash
uv run pipeline step <ref> source_note        # get extract_claims ready
uv run pipeline eval run <ref> extract_claims # runs every variant; holds the artifact
# → side-by-side report of outputs + tokens + cost + latency per variant
uv run pipeline eval approve <ref> extract_claims 0   # commit variant 0; chain resumes
```

Each variant's output is a separate keyed intermediate (no vault write); approve
picks the winner and the committer stages consume it. Only producer stages are
eval-able.

### Groom the corpus retroactively

The per-source `dedup` stage only sees the corpus as it stood at the time. Grooming
gives everything a second look under the current threshold:

```bash
uv run pipeline groom --workers 8 --out plan.json   # dry run, saves the plan
uv run pipeline groom --plan plan.json --apply      # apply without re-judging
uv run pipeline recount --apply                     # corroboration = distinct voices
```

Merges are additive and reversible: the absorbed claim's wording is kept on the
survivor under `alternate_phrasings`, its note moves to `corpus/claims/merged/`
rather than being deleted, and the vault is a git repo. Corroboration counts
distinct **voices** — one writer restating a point across editions is emphasis, not
support.

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

### Uploading a local run (promote)

Validate a source fully on your laptop (any provider/model), then publish the
finished notes to the production vault with **zero recompute**: `promote` pushes
the vault notes (git), the raw blob (→ S3, so the note stays regenerable there),
and a minimal `done` record. Intermediates don't travel — they regenerate on
demand from `raw + generating key`.

## Config

`config/pipeline.yaml` — blob store (`local` | `s3`), paths, `providers:`,
per-stage `models:`, `embeddings:`, `dedup:` tuning, and worker settings.

### Long-running batches

Anything expected to run more than ~15 minutes reports its estimated duration and
cost before starting, heartbeats to the `batches` table, and appears on the
dashboard overview with task-level and workflow-level time remaining. To interrupt
one cleanly — it finishes the current item and keeps committed progress:

```bash
uv run pipeline stop            # --clear to cancel the request
```
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
