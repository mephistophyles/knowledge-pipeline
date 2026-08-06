# Web ingestion — and the author identity layer it depends on

Planned 2026-08-01. Web/blog ingestion is a new **ingestor** plus a new **backlog
scanner**, not a new pipeline: the derivation chain, control plane, ledger pattern,
triage, grooming, and retract all transfer unchanged.

The prerequisite is author identity, so that is built first.

---

## Part 1 — Author identity

### The problem

`_author_key` normalizes a From header to a bare email. That key is a **channel**, not a
person. Two consequences:

1. `ben@stratechery.com` (email) and `stratechery.com` (web) are two different authors, so
   the same essay arriving on both channels reads as **two independent sources
   corroborating each other** — manufacturing the exact signal author-aware attestation
   exists to protect.
2. The channel usually does not name the person. Measured over the real ledger: only a
   minority of the 57 keys carry a name in the local part (`annelaure@`, `will.bachman@`,
   `jason@`); `email@stratechery.com` → Ben Thompson is not recoverable by parsing.

Display names in the archived `.eml` bytes DO carry the person in most cases, in three
shapes: bare person (`Ben Thompson`), person-plus-venue (`CJ Gustafson from Mostly
Metrics`), and venue-only (`Department of Product`, `Corporate Rebels`) where the person
is simply not in the data. A crude classifier misfires exactly at the margins — it reads
`The Bottleneck` as a person and `Cedric from Commoncog` as a publication — which is why
seeding **proposes** and Phil **confirms**. Nothing is applied unconfirmed.

### The model

Three levels where there are currently two collapsed into one:

- **channel key** — what we observe: `email@stratechery.com`, `stratechery.com`, a byline
- **identity** — the literal person: `person:ben-thompson`
- **venue** — where it appeared: `stratechery.com`. A venue is NOT an author.

```sql
identities(identity_id PK, display_name, kind, note)     -- 'person:…' | 'org:…'
identity_aliases(alias PK, identity_id FK, kind, confidence, source)
```

`identity_of(key) -> identity_id`. Attestation, backlog batching, and hit-rate analytics
switch to the identity. The venue stays recoverable via `source_hash → artifact →
site/list-id`, so no provenance is lost.

Byline wins when present and resolvable; the channel is the fallback. That is what makes a
guest post attribute to the guest — and it only works because the anchor is the person.

### Decisions (locked with Phil)

| Question | Decision |
|---|---|
| Unmapped channel key | **Provisional** — the claim commits, but its attestation is marked provisional and excluded from corroboration until mapped. Non-blocking, and false corroboration becomes structurally impossible rather than merely unlikely. |
| Venues with no identifiable person | **First-class `org:` identities** that can corroborate. Honest about what we know; two different orgs agreeing is genuine corroboration. |
| Co-authors and guest posts | **Primary identity drives corroboration; co-authors stored on the attestation** for provenance. Keeps the "same source?" rule simple and testable while losing no information. |
| Seeding the table | **Harvest display names from the archived `.eml` bytes → proposed rows with confidence marks → Phil confirms.** Reusable for the web sources that follow. |

### Migration

No rewrite needed. Stored attestation authors are resolved through `identity_of()` at
comparison time, so existing notes keep their raw strings and stay valid. A display
backfill can follow later as a groom-style pass.

---

## Part 2 — Web ingestion

### Decisions (locked with Phil)

| Question | Decision |
|---|---|
| Fetch | HTTP-first (`requests` + real UA). ONE **escalation seam** for every failure → `held`. |
| Escalation mechanism | **Deferred until the rate is measured.** Every exit is counted; ~3/month keeps manual paste forever, ~30/month justifies Playwright or a submit form behind the same seam, with no upstream rework. |
| Paywalled articles | Manual paste via the existing v1 `add web` path. No credentials in the pipeline. |
| Unreliable extraction | Heuristic quality gate → the existing `held` queue. |

### Input constraints (decided)

1. **Accepted input** — an http(s) URL, or pasted body text (v1 path, unchanged).
2. **Content type** — `text/html` or plain text/markdown. PDFs are rejected, not held: a
   separate ingestor with different extraction, not a degraded web page.
3. **URL canonicalization, before hashing or dedup** — lowercase host, strip `www.`, drop
   tracking params (`utm_*`, `fbclid`, `gclid`, `ref`, `source`, `mc_cid`, `mc_eid`), drop
   the fragment, normalize the trailing slash. After fetch, prefer `<link rel="canonical">`
   when it points at the same registrable domain; cross-domain canonicals are a syndication
   signal, handled via the identity table.
4. **Ledger identity** = sha256 of the **raw fetched bytes** (`fetch_hash`), never the
   extracted body. Same lesson as `eml_hash`: swapping the body extractor becomes a
   re-derivation rather than a corpus-wide identity reset — and the extractor is the part
   most likely to change.
5. **Length floor: 250 words → `held`.** Pilot editions average ~1,635 words; a teaser or a
   nav-only extraction is under 300. This is also the automatic detector for the deferred
   "ingest the article, not the email stub" problem.
6. **Length ceiling: 15,000 words → `held`.** Not a rejection — a flag that the page is a
   book, an index, or an extraction that swallowed an archive.
7. **Politeness** — honour `robots.txt`, 1 req/s per host, descriptive UA with a contact
   URL. A bulk backlog scan should look like a person, not a crawler.
8. **Language** — English only for now; non-English detected at extraction exits to `held`.

### Body extraction

`trafilatura` (best-in-class precision/recall; also returns author/date/title/sitename).
The defence email did not have: the pilot measures ~96–97% **quote grounding**, so a
claim's quote must appear verbatim in the artifact body — which makes extraction failures
detectable rather than silent.

### Attribution challenges

1. **Email/web identity split — HIGH, imminent.** Resolved by Part 1. The overlap test
   triggers it deliberately.
2. **Article quoting a third party — MEDIUM, not a plumbing problem.** An essay
   block-quoting someone yields a claim attributed to the essay's author that they were
   reporting or disputing. Prompt-level concern; recorded as a known limit.
3. **Syndication and mirrors — MEDIUM.** Cross-domain canonical plus "originally published
   at". Same false-corroboration failure as (1); resolves through the identity table.
4. **Multi-author venues — MEDIUM.** Byline-wins plus channel fallback handles it; the
   alias table is where per-venue policy is expressed.
5. **URL identity — LOW.** Constraint 3.
6. **Updated articles — LOW.** A re-fetch of changed content is a NEW artifact of the same
   canonical URL. Keep both, let dedup merge — safe now that merges are additive and
   retract-then-rederive exists.

---

## Status (2026-08-06)

Steps 1-5 are shipped. Identity is anchored to the person and curated in
`config/identities.yaml` (67 identities), with the unmapped queue living in the
dashboard at `/authors`. Fetch, snapshot import, extraction, and the quality gate are
in place and were validated on a real 22-URL backlog: 21 archived, 1 escalated,
20 extracted, 1 held as a paywall teaser (hbr.org, 59 words).

Two findings worth carrying forward:

- **Substack does NOT disallow us.** The earlier "37% of the corpus is off-limits"
  finding was our bug — `RobotFileParser.read()` fetches robots.txt with urllib's
  default UA, which Cloudflare 403s, and urllib reads a 403 as disallow-everything.
  12 false refusals on the real backlog became 1 once robots.txt was fetched with our
  own UA.
- **Terms can be stricter than robots.txt.** Substack's ToS prohibits crawling even
  where robots.txt permits it, which is why `pipeline web audit` reports the terms
  clause verbatim and renders findings rather than verdicts.

Still open: step 6 (the overlap acceptance test, needs deliberate email/web
duplicates) and step 7 (read the escalation counter to settle browser-vs-form).

## Build sequence

1. **Identity layer** — tables, `identity_of()`, switch attestation/batching/analytics,
   provisional attestations, harvest-and-confirm seeding. MUST precede any web claim.
2. **Canonicalization + fetch + ledger** — `web_backlog`, raw HTML archive keyed by
   `fetch_hash`, robots/rate limiting, escalation seam with counters.
3. **Body extraction** — trafilatura, quality gate, `held` routing.
4. **Web triage** — reuse the stage; adapt the prompt for index pages and link-dumps, the
   web analogue of promo/listicle newsletters.
5. **Registry + facets** — author=identity, source=site, media=text, title=headline.
6. **Overlap end-to-end** — Phil's deliberate email/web duplicates.
7. **Read the escalation counter** → decide browser vs submit form on evidence.

## Evaluation and comparison

Ground truth first: the 57 hand-confirmed keys ARE the ground truth, and also the
deliverable — a rare overlap. No metric is reported from output counts alone.

| # | Measures | Method | Passes when |
|---|---|---|---|
| E1 | Seeding accuracy | Harvested proposals vs Phil's confirmed table; precision/recall per shape (bare person / person-from-venue / venue-only) | Decides whether harvesting beats typing 57 rows; reported honestly either way |
| E2 | Corroboration correction | Re-resolve the **74** existing multi-attestation claims under identities; audit every change by hand | Quantifies how many of today's 1,126 claims are already inflated by one person holding two channels |
| E3 | Overlap acceptance | An essay in BOTH backlogs | Exactly ONE claim, ONE attestation — not two corroborating sources |
| E4 | Extraction quality | Hand-marked body for ~20 pages across site shapes vs trafilatura output; plus quote-grounding rate on the derived claims | Grounding stays at the email corpus's ~96–97%; text-matching validated on samples before any number is reported |
| E5 | Escalation rate | Counter on the seam, per month, by cause | Supplies the evidence for the deferred browser-vs-form decision |

E2 is the one that tells us whether this was a latent bug or an active one.
