# Local migration playbook

How to move the pipeline, **and the corpus it has already built**, off the MacBook and
onto local hardware. There is no cloud. Rewritten 2026-11-10 from the 2026-08-06 AWS
playbook. The inventory and checks carry over; the destination changes.

| Machine | Role |
|---|---|
| **MacBook Pro** | Old home. Holds the only copy of the state until step 1 lands. |
| **Mac Studio** (`studio`, M5 Max, 64 GB) | Dev machine. Holds the canonical state *temporarily* during calibration (phase A). |
| **halo** (`ssh halo`, 192.168.1.158, Strix Halo, 128 GB, ~94 GiB GPU-addressable) | Always-on box. Runs inference now (phase A); runs everything later (phase B). |

**One writer at a time.** `pipeline.db` and `vault/` have exactly one live copy at any
moment. Copies elsewhere are backups, not working sets. Two writers means a split brain
that no merge can undo.

---

## What actually has to move

Counts are from 2026-08-06. Re-take them from the live corpus before step 1.

| Thing | Size | Rebuildable? | Notes |
|---|---|---|---|
| `vault/` | 36 MB, 611 commits | **No** | The product. **Has no git remote.** |
| `data/pipeline.db` | 84 MB | **No** | Jobs, claims, identities, ledgers, costs, verdicts. |
| `data/raw/` | 361 MB | **No** | Only copy of 3,448 archived `.eml` + web archives. The mailbox has no cursor. |
| `data/intermediate/` | 4.6 MB | Yes | Keyed stage outputs. Copy anyway; it's small. |
| `evals/` | small | Partly | Bake-off reports and labelled sets. Gitignored (third-party text). |
| `.env` | tiny | Re-creatable | Provider keys + IMAP app password. |
| `config/identities.yaml` | tiny | **No** | Tracked in git, but the dashboard writes it, so the MacBook may hold unpushed edits. |

Easy to miss: the **verdict cache** (`dedup_verdicts`, 20,462 paid judgments) lives
inside `pipeline.db`. It makes every embedder or threshold calibration cost zero LLM
calls. Never "start clean".

---

## Phase A: MacBook → Studio; halo serves inference

### 0. On the MacBook, before copying

- [ ] Stop everything that writes: workers, dashboard, any batch (`uv run pipeline stop`).
      `uv run pipeline status` shows nothing running.
- [ ] `git status`. Commit and push anything that isn't data, including `config/identities.yaml` edits.
- [ ] Fold the WAL into the main file so the copy is self-contained:
      `sqlite3 data/pipeline.db "PRAGMA wal_checkpoint(TRUNCATE);"`
- [ ] **Give the vault a remote.** This is the single biggest gap.
      ```bash
      git -C vault remote add origin git@github.com:mephistophyles/<private-vault-repo>.git
      git -C vault push -u origin main
      ```
- [ ] If `terraform apply` was ever run: `cd infra && terraform destroy` **from the
      MacBook's checkout**. That is where the state file lives, and `infra/` is gone from `main`.

### 1. Copy (run on the MacBook)

```bash
cd ~/Dev/knowledge-pipeline
for d in data vault evals; do rsync -av "$d/" "studio:Dev/knowledge-pipeline/$d/"; done
rsync -av .env "studio:Dev/knowledge-pipeline/"
rsync -av config/identities.yaml "studio:Dev/knowledge-pipeline/config/"
```

Trailing slashes are deliberate. If `vault` is a symlink into Dropbox, `vault/` copies
the contents rather than the link. Add `-n` for a dry run first.

### 2. Check on the Studio

```bash
sqlite3 data/pipeline.db "PRAGMA integrity_check;"                               # ok
sqlite3 data/pipeline.db "select count(*) from claims where merged_into is null;" # == live notes
sqlite3 data/pipeline.db "select count(*) from dedup_verdicts;"                   # ~20,462
find data/raw -name '*.eml' | wc -l                                                # ~3,448
git -C vault log --oneline | wc -l && git -C vault status --short                  # ≥611, clean
ls vault/corpus/claims/*.md | wc -l                                                # == live claim rows
git diff config/identities.yaml                                                    # commit any MacBook edits
```

Don't delete the MacBook copies until halo has run a full week (phase B).

### 3. Reach halo's inference from the Studio

Ollama on halo listens on `127.0.0.1:11434` only. Tunnel it rather than exposing it:

```bash
ssh -N -L 11434:localhost:11434 halo     # leave running
```

The `ollama` provider (`http://localhost:11434/v1`) then works unchanged, and it is the
same URL once the pipeline itself runs on halo. Embedding and model bake-offs run here.

---

## Phase B: Studio → halo (after calibration settles)

1. Stop on the Studio, checkpoint the WAL, then `rsync` `data/`, `.env`, `evals/` to halo.
   The vault travels by `git clone` from its new remote.
2. Run under **uv + systemd as `phil`**, not docker. That avoids the root-owned vault
   bind mount ("dubious ownership" for host git) and the container-localhost problem.
3. Litestream with an **off-box** replica (sftp → Studio; see `docker/litestream.yml`).
   A replica on the same NVMe isn't a backup.
4. Dashboard on the LAN only. It's a write surface (authors form, stop button).
5. Cron the weekly feed (Saturday, after the week closes):
   ```cron
   0 6 * * 6  cd ~/knowledge-pipeline && uv run pipeline feed pull --catch-up
   0 7 * * 6  cd ~/knowledge-pipeline && uv run pipeline worker llm --once
   ```
6. The Studio becomes a client: Obsidian pulls the vault, and code deploys by `git pull`.

---

## Open items carried over

- **Inference contention:** halo also serves an unrelated `office-*` model. Two big
  models plus parallel slots must fit ~94 GiB GTT together. Check `ollama ps` before
  long runs.
- **`entities.max_distance: 0.79`** is a placeholder scaled from the dedup move, not measured.
- **246 `entities` jobs queued** (as of 08-06). They run on first worker start, on
  whatever `models.entities` points at.
- **Backlog:** 2,163 process-triaged emails undrained. This is the first real always-on workload.
- **Web overlap acceptance test** (`web-ingestion-plan.md` step 6) is still outstanding.
