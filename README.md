# knowledge-pipeline

Batch-oriented ingestion → distillation → Obsidian vault pipeline with a
SQLite control plane. See [`knowledge-pipeline-plan.md`](knowledge-pipeline-plan.md)
for the full design.

## Status — build steps 1–2 + §14.1 compose stack

This is the **skeleton + state machine** slice. What works today:

- **Blob store** interface (`local` ↔ `s3`, config-selected) + content-addressed
  raw store with provenance manifests.
- **SQLite control plane**: `jobs`, `controls`, `runs`, `costs` tables; the DB is
  the queue, the control plane, and the dashboard backend.
- **Ingestors**: `pipeline add paste` and `pipeline annotate` (the two that need
  no network).
- **State machine**: resource-class workers with atomic (`BEGIN IMMEDIATE`)
  claiming, per-scope pause/hold/throttle, and hand-walk / single-step tools.
- **Vault writer**: frontmatter notes committed to a git repo; a real
  `source_note` stage and a verbatim `personal` deriver.
- **Compose stack**: `orchestrator`, `worker-io`, `worker-llm`, `worker-cheap`,
  `dashboard`, `litestream` — one `docker compose up` runs the tier-1 box locally.

**Stubbed for later steps** (persist an intermediate and advance, so the state
machine is exercisable): `extract_claims`, `dedup`, `entities` (LLM derivation is
build step 5). Not yet present: email/RSS/YouTube/web ingestors (step 3), audio
chain (step 4), GPU burst worker, real dashboard control plane (step 6), EC2/SES
provisioning + Terraform (§14).

## Quick start (local, no Docker)

```bash
uv sync
uv run pipeline init                       # create data/pipeline.db + vault git repo

echo "Taste is the differentiator; generation is cheap." \
  | uv run pipeline add paste --url https://example.com/post --type paste

uv run pipeline status                     # see the source_note job go ready
```

Drain the pipeline with workers, or hand-walk it:

```bash
uv run pipeline worker cheap --once        # runs source_note
uv run pipeline worker llm --once          # runs extract_claims, entities (stubs)
# or, step by step:
uv run pipeline walk <hash-prefix>
```

Annotate an existing artifact (notes are stored separately, invisible to corpus
derivation):

```bash
echo "I disagree — distribution still wins." \
  | uv run pipeline annotate <hash-prefix>
```

## Control plane

```bash
pipeline pause --stage extract_claims      # accumulate upstream in `ready`
pipeline pause --source podcast
pipeline resume --stage extract_claims
pipeline hold <ref> / release <ref>        # freeze one artifact
pipeline retry <ref> [--stage X]           # requeue failed/held
pipeline throttle --source newsletter --limit 25
pipeline step <ref> <stage>                # run exactly one stage
pipeline walk <ref>                        # hand-walk with confirmation per stage
pipeline status [<ref>]
```

## Compose stack (local tier-1 box)

```bash
docker compose up --build
# dashboard → http://localhost:8000
# run ingestion against the same ./data volume from the host CLI or:
docker compose run --rm init add paste --text "hello" --url https://x.example
```

`litestream` replicates `data/pipeline.db` to a local file replica out of the
box; swap `docker/litestream.yml` to an S3 replica for the EC2 deployment.

## Config

`config/pipeline.yaml` selects the blob store (`local` | `s3`), paths, the model
registry, and worker settings. `config/feeds.yaml` holds the per-source
attribution registry (used by the spine ingestors in build step 3).

## Layout

```
pipeline/
  config.py            resolved runtime settings
  db/                  schema, connection, jobs, controls, costs
  storage/             blob store interface + manifest writer
  orchestrator/        stage registry, executor, handlers, worker loop
  ingestors/           paste, annotate
  vault/               frontmatter schema + git-backed note writer
dashboard/             read-only FastAPI state view
docker/                Dockerfile + litestream config
config/                pipeline.yaml, feeds.yaml, prompts/
```
