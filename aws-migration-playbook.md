# AWS migration playbook

Moving the pipeline — **and the corpus it has already built** — from the laptop to the
always-on box. Written 2026-08-06, when the last local model dependency was removed.

The pipeline was designed for this: `storage.blobstore` is the only cloud-specific
abstraction, and every stage is a pure function of `(input, prompt_version, model config)`.
The hard part is not the code. It is the **state**, because most of it looks disposable and
is not.

---

## What actually has to move

| Thing | Size | Rebuildable? | Notes |
|---|---|---|---|
| `vault/` | 36 MB, 611 commits | **No** | The product. Notes, claims, attestations. |
| `data/pipeline.db` | 84 MB | **No** | Jobs, claims, identities, ledgers, costs, verdicts. |
| `data/raw/` | 361 MB | **No** | Content-addressed originals: 3,448 `.eml` + web archives. |
| `data/intermediate/` | 4.6 MB | Yes | Keyed stage outputs; re-derivable from raw. |
| `dedup_verdicts` (in the DB) | 20,462 rows | Technically | See below — treat as irreplaceable. |

Current corpus: **1,491 live claims**, 153 entities, 521 artifacts, 3,448 backlog rows,
24 web rows, 67 curated identities.

### Three things that look disposable and are not

1. **The verdict cache (20,462 rows).** Every row is a paid LLM judgment about whether two
   claims are the same. It is what made the last groom cost *zero* confirm calls and what
   makes threshold sweeps affordable. Rebuilding it is roughly 20k calls. It lives inside
   `pipeline.db`, so it travels if the DB does — but do not "start clean" on the box.
2. **`config/identities.yaml` (67 identities).** Hand-curated attribution, unrecoverable by
   any automated pass. It is in git, so it travels with the repo — verify it arrived.
3. **`data/raw/`.** Content-addressed, so it *looks* like a cache. It is the only copy of
   3,448 archived emails, and the mailbox has no usable cursor to re-scan from.

---

## Order of operations

Do these in order. Each step has a check; do not proceed past a failing check.

### 0. Prerequisites (before touching anything)

- [ ] **Give the vault a git remote — it has none.** This is the single biggest gap: 611
      commits of irreplaceable output exist only on this laptop and in Dropbox.
      ```bash
      # private repo, separate from the code repo
      git -C vault remote add origin git@github.com:<you>/mimir-vault.git
      git -C vault push -u origin main
      ```
      Check: `git -C vault ls-remote origin` lists the branch.
- [ ] Confirm nothing is mid-flight: `uv run pipeline status`, and no rows in `batches`
      with `state='running'`.
- [ ] Tag the pre-migration state so there is a known-good point to come back to:
      ```bash
      git tag pre-aws-migration && git push --tags
      git -C vault tag pre-aws-migration && git -C vault push --tags
      ```

### 1. Secrets into SSM

Four are needed now — `NVIDIA_API_KEY` is new since the infra was written, and the pipeline
will not run without it (extraction, entities, and embeddings all use it).

```bash
for k in NVIDIA_API_KEY OPENROUTER_API_KEY OPENAI_API_KEY IMAP_PASSWORD; do
  aws ssm put-parameter --name "/mimir/$k" --type SecureString \
    --value "$(grep "^$k=" .env | cut -d= -f2-)" --overwrite
done
```

Check: `aws ssm get-parameters-by-path --path /mimir --with-decryption` returns four.
The instance role in `infra/iam.tf` already grants read on the project's SSM path.

### 2. Apply the Terraform

`infra/` already defines the S3 raw store, IAM instance profile, security group, Ubuntu
EC2, Elastic IP, and the Route 53 record.

```bash
cd infra && terraform plan   # read it; then
terraform apply
terraform output              # note s3_bucket and the public IP
```

Check: the bucket exists and the instance is reachable over SSH.

**Instance sizing:** t3.small is now viable *because* nothing runs locally any more — no
Ollama, no GPU. If that ever changes, this decision reverses.

### 3. Move the raw store to S3

```bash
aws s3 sync data/raw/ s3://<bucket>/raw/ --size-only
```
Then flip the config:
```yaml
storage:
  blobstore: s3
  s3: { bucket: <bucket>, prefix: raw, region: us-east-1 }
```

Check — object counts must match, and this is worth doing rather than trusting `sync`:
```bash
find data/raw -type f | wc -l
aws s3 ls s3://<bucket>/raw/ --recursive --summarize | tail -2
```

### 4. Move the database

SQLite is a single file; the risk is copying it mid-write.

```bash
sqlite3 data/pipeline.db ".backup /tmp/pipeline-migrate.db"   # safe hot copy
scp /tmp/pipeline-migrate.db ubuntu@<ip>:/opt/mimir/data/pipeline.db
```

Check on the box, before anything else runs:
```bash
sqlite3 data/pipeline.db "select count(*) from claims where merged_into is null;"  # 1491
sqlite3 data/pipeline.db "select count(*) from dedup_verdicts;"                    # 20462
sqlite3 data/pipeline.db "select count(*) from identities;"                        # 67
```

**Durability:** the box is now the only writer. Set up Litestream to the same bucket
(`infra/outputs.tf` already anticipates it) *before* the first real run, or a lost instance
means a lost corpus.

### 5. Vault on the box

The vault is a git repo, so it travels by clone — not by copy.

```bash
git clone git@github.com:<you>/mimir-vault.git /opt/mimir/vault
```

**Known issue, fix it now rather than later:** the container writes the vault as root via a
bind mount, so host-side git hits "dubious ownership" and obsidian-git cannot operate on
it. Run the container as the `ubuntu` UID (or `chown` after each write). The band-aid
(`git config --global --add safe.directory`) makes inspection work but leaves the
permissions wrong.

Check: as `ubuntu`, `git -C /opt/mimir/vault status` works without a safe.directory flag.

### 6. Bring the pipeline up

```bash
docker compose up -d          # or the systemd unit
uv run pipeline status
```

Smoke test on ONE artifact before any batch — this proves the whole cloud path
(S3 read, SSM secret, NVIDIA call, S3 write) in a single command:
```bash
uv run pipeline step <ref> extract_claims
```

Check: a new row in `costs` with `provider='nvidia'` and a non-zero latency.

### 7. Dashboard access

**The dashboard is now a write surface** — the authors form edits attribution and writes
`config/identities.yaml`, and the stop button halts batches. It must not be exposed
unauthenticated.

Decision already taken: **Tailscale**, because device identity beats an IP allowlist for
someone who roams. Consequence to accept deliberately: the dashboard is then *not* publicly
reachable, and `pipeline.madebyphil.com` + Caddy TLS only makes sense if you later want
public access with real auth in front.

Check: reachable over the tailnet, refused from the open internet.

### 8. Switch the ingestion cadence

The weekly feed is the ongoing half. On the box it becomes a cron rather than a thing you
remember:

```cron
# Saturdays, after the week closes
0 6 * * 6  cd /opt/mimir && uv run pipeline feed pull --catch-up
0 7 * * 6  cd /opt/mimir && uv run pipeline worker llm --once
```

Check: `pipeline feed weeks` shows the new week archived after the first firing.

---

## What changes conceptually once this lands

- **`pipeline stop` becomes an operator control, not a safety net.** Everything built to
  survive a closing laptop — the stop file, staleness warnings, the estimate-before-start
  rule — was scaffolding around work running on a machine that moves. It stays useful; it
  stops being load-bearing.
- **Batch estimates matter less.** A 4-hour backlog drain is fine unattended. The 15-minute
  flagging threshold is about *your* attention, so keep it for interactive work and relax
  it for scheduled runs.
- **The laptop becomes a client.** Obsidian pulls the vault; the box writes it.

---

## Rollback

Nothing here is one-way:

- Config: `storage.blobstore: local` and the old paths; the code is identical either way.
- Data: `git checkout pre-aws-migration` in both repos; `data/raw` and the DB are still on
  the laptop until you delete them — **do not delete them until the box has run a full
  week successfully.**
- Terraform: `terraform destroy` removes the infra but **not** the S3 bucket contents if
  versioning/retention is on. Check before assuming a clean teardown.

---

## Open items, deliberately not solved here

- **Entity threshold (`entities.max_distance: 0.79`)** is a considered placeholder scaled
  from the dedup move, not a measured value. Needs labelled entity pairs.
- **246 `entities` jobs are queued** and will run on first worker start — expect NVIDIA
  calls immediately after step 6.
- **Backlog remains large**: 2,163 process-triaged emails undrained. That is the first
  real always-on workload, and the reason for doing this.
- **Web overlap acceptance test** (step 6 of `web-ingestion-plan.md`) still needs
  deliberate email/web duplicates.
