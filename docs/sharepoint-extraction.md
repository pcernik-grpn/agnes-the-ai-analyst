# SharePoint extraction: from zero to the first crawl

The one place that walks an operator end to end: enable the connector,
connect a tenant, run the first crawl, verify what happened, then turn on
the optional LLM stages. Every step below links the reference doc that owns
its details — this page is the ORDER and the checklist, not a second copy of
those details.

The pipeline this activates (all in-process, no external producer):

```
Graph delta crawl → convert to markdown → anonymize (per scope) → ingest
into collections → (optional) facts extraction into the knowledge graph
```

## 0. Prerequisites

- **A deployment that can run the extraction worker lane.** On a Terraform
  VM: `extraction_worker_enabled = true` (plus its multi-process
  prerequisites — Postgres app-state, Redis coordination; the module
  renders all of it). Details: [`DEPLOYMENT.md`](DEPLOYMENT.md) →
  *extraction lane*. On plain Compose: the `extraction-worker` profile.
- **The standard app image** — since 2026-09 it already carries the
  converter backends (`extraction` extra). Bare-metal pip installs need
  `pip install 'agnes[extraction]'`; a missing extra is refused up front
  with `409 extraction_dependencies_missing`, never a crawl that fails on
  every file. Legacy Office/OpenDocument files (`.doc`/`.rtf`/`.odt`,
  `.ppt`/`.odp`, `.xls`/`.ods`) are pre-converted through headless
  LibreOffice before the markitdown route — the standard image bundles
  `libreoffice-core`/`-writer`/`-calc`/`-impress`; a bare-metal install
  additionally needs the `soffice` binary on `PATH`, or those specific
  suffixes fail conversion (`MissingConversionDependency`) while every other
  format keeps working.
- **An Entra app registration** for the tenant: certificate (default) or
  client secret, with admin consent granted. This is the one step Agnes
  cannot do for you.
- Nothing to prepare for the anonymization key: it provisions itself from
  the server vault on first use ([`anonymization.md`](anonymization.md) →
  *The key: nothing to configure*).

## 1. Enable the connector

One switch: `sharepoint.enabled: true` in `/admin/server-config` (or
`AGNES_SHAREPOINT_ENABLED=1`; the Terraform flag sets it for you). It gates
the wizard, every `/api/admin/sharepoint/*` route, crawling, webhooks and
ACL mirroring together — there are no other flags to find
([`feature-flags.md`](feature-flags.md), migration note).

## 2. Connect the tenant

`/admin/data-sources` → add SharePoint → the connect wizard: tenant id,
client id, certificate or client secret, then pick scopes (sites / document
libraries / folders). Per scope, two decisions that matter later:

- **anonymize** — this scope's documents are pseudonymized BEFORE anything
  is stored, fail-closed ([`anonymization.md`](anonymization.md)).
- **access mode** — mirrored ACLs vs. open ([`RBAC.md`](RBAC.md) → "SharePoint
  ACL mirroring" for exactly what a mirrored scope honors, and the
  permission-zone notes in [`architecture.md`](architecture.md)). Mirroring
  honors WHO has access, never WHAT they may do with it: Agnes is a
  read-only consumer of SharePoint content, so a Graph role (`read`/`write`/
  `owner`) is never read — every honored principal simply gets read access.
  Flip `access_mode` on many already-confirmed scopes at once with
  `agnes admin sharepoint scope set-mode <id> --all|--scope <source_scope_id>
  --mode manual|mirrored` — the fast path for turning mirroring on across a
  large site split into hundreds of bulk-added scopes.
- **site groups** (Owners/Members/Visitors, or a custom one) are not
  enumerable through the app-only Graph surface the connector uses, so a
  mirrored scope granting one honors nobody by default — map it to one or
  more existing Agnes groups with `agnes admin sharepoint acl
  map-site-group <id> --site-group "<name>" --group <agnes_group_id>` (or the
  source card's **Map site group (ACL)…** action), see
  [`RBAC.md`](RBAC.md) → "SharePoint ACL mirroring".

## 3. Sanity-check the anonymization on YOUR documents

Before any crawl: source card → **View configuration** → **Preview
redaction**. Paste a sample or upload one real file — the panel shows the
converted markdown and the redacted version side by side, with per-kind
counts. Nothing you preview is stored. If your corpus has vocabulary of its
own (codenames, partners), add `extraction.anonymization.custom_terms`
first and preview again.

## 4. First crawl — one folder, small limits

Source card → **Run extraction now** → the options row: start with a small
per-run time limit and default concurrency. The run appears live on the
card; the drawer shows counters (new / changed / unchanged / skipped with
reasons), per-stage token usage, and — on interruption — whether the state
is resumable (`timeout` and `throttled` resume from persisted state; the
next run picks up where this one stopped).

Verify: the scope's collection holds the documents, skips are explainable
(oversize, unconvertible, lock files), and for an anonymize-marked scope a
spot-check shows pseudonyms, not names.

## 5. Scale up

- `extraction.schedule` — the instance-wide recurring sweep (empty =
  manual only).
- `extraction.crawler.concurrency` (default 6) — files pipelined per delta
  page; the crawl backs off on tenant throttling by itself (AIMD) and
  reports it. Per-run override in the Run-now options. No separate knob to
  raise: if the container's own memory limit cannot sustain the resolved
  cap — several crawl jobs can share one worker's EXTRACTION lane, each
  with its own convert pool — the run's effective cap is clamped down
  automatically (never up) from the container's cgroup limit and the
  worker's own lane count, at roughly 2 GB reserved per in-flight file. The
  run's `concurrency.source` reports `"memory_budget"` when this fired, next
  to `"config"`/`"payload"`/`"adaptive"`.
- **Large sites shard themselves automatically** (2026-09-03
  auto-parallel-crawl design) — an admin never has to split a site by hand
  and merge the results back afterwards. When a connection's estimated
  document total is over `extraction.crawler.shard_target_docs` (default
  5000; `0` disables sharding entirely — every site crawls sequentially,
  today's pre-2026-09 behaviour, and is what a DuckDB-backed instance
  always does — this feature is PG-only, A3 ratchet), the very next
  `POST …/extract` becomes a short PLANNER instead of crawling itself: it
  packs the site into K shards (top-level folders grouped by a live Graph
  Search document count, one held back as a "remainder" shard for loose
  root files and anything created after planning), opens ONE parent
  `extraction_runs` row, and enqueues K `corpus-extraction-shard` child
  jobs — each with its OWN convert pool, its OWN per-delta-unit crawl
  state, and its OWN run row — that write into the connection's EXISTING
  collection. No clone, no consolidate, one connection to watch.

  Preview what a trigger would plan right now, without triggering
  anything: `GET /api/admin/sharepoint/connections/{id}/shard-plan
  [?min_modified=YYYY-MM-DD]` (`agnes admin sharepoint shard-plan
  <connection_id> [--min-modified YYYY-MM-DD] [--json]`), or the source
  card's **Parallel crawl — preview shards…** control. Response:
  `{mode: "inline"|"sharded", target_docs, signal, shards: [{drive_id,
  index, label, expected, targets_count}], loose_root_files}` —
  `expected` is a live count, always shown "≈", never exact.

  **Per-site operator controls fan out to every shard unchanged:**
  - `resync` drops every shard's cursor AND the persisted plan itself, so
    the next trigger both re-enumerates from scratch and re-plans fresh.
  - `force_reprocess` / `retry_failed` / `retry_empty` / `concurrency` /
    `timeout_s` on `POST …/extract` fan out to every child as-is —
    `retry_failed`/`retry_empty` replay each shard's OWN backlog (scoped
    by its own delta-unit keys, never another shard's), and `timeout_s`
    bounds each CHILD independently, not the run as a whole.
  - `POST …/extraction/stop` sets ONE cooperative flag; every child stops
    at its own next checkpoint boundary.
  - `shards: [i, ...]` (1-based) on `POST …/extract` re-runs only the
    named shard indices from the connection's LAST persisted plan —
    opening a fresh parent run scoped to just those shards, never
    re-planning — the supported replacement for "re-run one clone" below.
  - `409 extraction_already_running` while the site's TOP-LEVEL run row is
    still `running` — a sharded site's run is not "done" until its LAST
    child finishes, even though the triggering job (the planner) itself
    finished the moment it enqueued the children.

  **Observability**: the fleet dashboard and the source card's Run row
  both gain a "k/K shards" badge/line for a sharded site, and a per-shard
  breakdown (label, outcome, absolute files done/seen, the live "≈"
  expected count, checkpoint age, a stuck flag, and any error) — a shard
  CHILD never appears as its own row anywhere; only the parent (planner)
  run does.

  **Migrating an instance with manual split connections** (the pre-2026-09
  workflow below): nothing breaks on upgrade — each clone auto-shards on
  its own if it is itself large, and the original connection's whole-drive
  cursor seeds its own remainder shard, so re-ingesting nothing is a no-op.
  To fold everything back onto one connection: delete the clone
  connections (their scopes go with them), `POST …/collections/consolidate`
  on the original to fold the now-orphaned per-folder collections into its
  own, then `POST …/extract` — the planner shards it automatically and
  already-ingested documents upsert to a no-op on `(collection,
  stable_id)`. To keep every part's crawl/facts progress instead of
  re-downloading, fold with `POST …/connections/{id}/splits/merge` (below)
  FIRST — it unions each sibling's scopes and progress onto the target —
  then trigger the target.

- **Manual multi-connection split (deprecated).** `GET …/split-plan`
  (`agnes admin sharepoint split-plan`) / `POST …/splits` (`agnes admin
  sharepoint split`) — the pre-2026-09 way to parallelize a big crawl by
  hand: clone the connection N times (`POST …/clone` / `agnes admin
  sharepoint connection clone <connection_id> --name <name>` — a sibling
  wired to the SAME tenant/client identity and certificate/client-secret,
  with zero scopes) and bulk-confirm each clone's slice of the site's
  top-level folders as scopes (`POST …/scopes/bulk` or `agnes admin
  sharepoint scope bulk-add <connection_id> --path "Folder A" --path
  "Folder B/Sub" [--drive-id <id>]`, or `--paths-file split.json`), all
  routed to ONE shared collection by default (`--collection-id <id>` /
  `--collection-name <name>` for an explicit target, mutually exclusive
  with each other and with `--per-folder-collections`, which restores the
  OLD one-collection-per-folder default). Superseded by automatic sharding
  above for its original purpose (parallelizing one big crawl) — kept as a
  migration-window escape hatch only: `POST …/splits` answers with a
  `Deprecation: true` response header, and both endpoints are slated for
  removal after one release. `GET …/split-plan`'s response gains one
  ADDITIVE field, `mode` (the SAME verdict `shard-plan` would give this
  connection right now — an informational hint, `null` if it could not be
  computed). `409 split_exists` refuses a repeat `POST …/splits` under
  names that already exist. The SharePoint connection card's legacy split
  panel (**Legacy: create N connections manually (deprecated)…**, behind
  the **Parallel crawl — preview shards…** control) still previews and
  applies it from the browser.

  **Already split without the shared-collection option?** `POST
  …/connections/{id}/collections/consolidate` or `agnes admin sharepoint
  collections consolidate <connection_id> --target-name <name> |
  --target-collection-id <id> [--execute]` folds a connection's per-scope
  collections into one target after the fact — a dry-run preview (the
  default) lists what would be folded and how many files, `--execute`
  performs the real merge (files/chunks/claims re-pointed, grants unioned,
  emptied sources soft-deleted). Also reachable from the source card's
  overflow menu (**Consolidate collections…**, an inline drawer row).
  `--site` (`include_split_siblings: true`) widens the fold to every OTHER
  connection FROM THE SAME `POST …/splits` call — one call instead of
  repeating this once per part with the same target; refused with `409
  sibling_crawl_running` if a family member's crawl is currently queued or
  running. PG-only (A3 ratchet).

  **Merging the parts back into one CONNECTION** (a step further than
  folding collections above — this also unions the crawl/facts progress,
  so the merged connection resumes incrementally instead of re-downloading
  the site). Once a split site no longer needs to run in parallel — or a
  site was split by hand into several sibling connections and it is time
  to fold it back — `POST …/connections/{id}/splits/merge` or `agnes admin
  sharepoint split-merge <target_id> --sibling <id>... | --all-siblings
  --target-collection-id <id> | --target-name <name> [--execute]` (source
  card overflow menu → **Merge split parts back into this source…**) is
  the REVERSE of the split above: it moves every sibling's scopes onto the
  target (deduped by `(source_scope_id, drive_id)`), unions each sibling's
  crawl state (delta-link cursors, cTags, the failed/empty-document
  backlogs) and facts state (the extraction ledger) onto the target's own,
  folds every involved scope collection into one target collection (the
  SAME repository `collections/consolidate` uses — never reimplemented),
  and re-points each sibling's run history onto the target. `--all-siblings`
  folds in every OTHER connection named like this one's own split family
  (the `"<source name> — part i/n"` convention `POST …/splits` already
  establishes); `--sibling <id>` (repeatable) names siblings explicitly —
  the only form that works for a site split by hand under different
  names. Refused (`409`, nothing touched) while any involved connection
  has a running crawl/facts job, while a sibling carries ACL-mirroring
  permission zones or a mirrored-scope audience mapping different from the
  target's own, or while a scope collection being folded is still
  referenced by a connection OUTSIDE the merge group. Siblings are marked
  merged-away (their scopes cleared) rather than deleted — their
  credentials are left untouched; remove a merged-away sibling later with
  the ordinary `DELETE /api/admin/source-connections/{id}` if it is no
  longer needed. Dry-run by default. PG-only (A3 ratchet).
- `extraction.crawl.min_modified` — a per-connection age filter for a
  backfill run: crawl only files modified on/after a cutoff date instead of
  re-walking a whole multi-year corpus. `PATCH …/extraction/crawl-config`
  (`agnes admin sharepoint crawl-config <connection_id> --min-modified
  YYYY-MM-DD` / `--clear`) sets or clears it; an item with no modified
  timestamp is always kept. On the source card, the same control ("Crawl
  filter", next to "Facts policy") sets it directly — widening the date
  later needs a "Re-enumerate from scratch" run afterwards, since the
  delta cursor has already moved past whatever the old cutoff skipped.
- Webhooks for near-real-time updates: mint the secret
  (`POST …/webhook`), then `POST …/subscriptions/ensure` — Agnes owns the
  Graph subscription lifecycle including renewals
  ([`api-reference.md`](api-reference.md) → *Graph subscription
  lifecycle*). Requires a public HTTPS origin (`AGNES_BASE_URL`).

## 6. Optional LLM stages (each a cost switch, default off)

| Stage | Switch | What it buys | Cost order |
|---|---|---|---|
| LLM name detection | `extraction.anonymization.detector: "llm"` | recall on names regex can't pattern-match | ~$5 / 1 000 docs (Haiku) |
| Scan OCR | `extraction.scan_ocr.enabled` | text from image-only PDFs | ~$0.006 / page (triaged — see below) |
| Facts extraction | `extraction.facts.enabled` (+ `facts.enabled`) | knowledge-graph facts with verbatim evidence | ~$0.05 / doc (Haiku), measured live — see `config/instance.yaml.example`'s `facts` block |

**Scan OCR triages a document before paying to transcribe all of it**
(`extraction.scan_ocr.triage`, on by default once `scan_ocr.enabled` is —
`triage.enabled: false` restores the old all-or-nothing behaviour, byte for
byte). Two stages, cheapest first:

1. **Metadata rules, no model call.** `skip_path_patterns` /
   `full_path_patterns` (case-insensitive substring or glob against the
   document's path — e.g. `"Tax Returns"` to skip, `"Data Room/*Contract*"`
   to always transcribe in full; a `full_path_patterns` match always wins),
   `max_size_mb` / `max_pages_for_preview` (skip a document too large or too
   long), and `min_pages` (a document this short just gets transcribed in
   full — triaging it costs about the same as skipping it).
2. **A `preview_pages`-page preview (default 5) + one classification call**,
   for anything the rules above left undecided: the preview pages transcribe
   exactly like any other page, then ONE extra text-only call (never the
   page images again) returns `{doc_type, language, scan_quality, continue,
   reason}` via a strict tool-use schema. `continue: true` is the only thing
   that pays for the rest of the document (up to `max_pages`, appended to
   the preview); a malformed or missing verdict is always treated as
   `continue: false` ("triage_unparseable"), never a guessed yes.

A document that stops after the preview still keeps those pages and a
one-line marker (`<!-- scan_ocr: preview N of M pages; triage: <doc_type>;
continue=<bool>; reason=… -->`) so it stays searchable and identifiable —
unless `preview_pages: 0`, which reproduces the pre-triage "empty" outcome
(no text at all) for a document a rule already decided to skip. **Cost
model**: a `skip`/`triage`-stopped document costs `preview_pages` page-calls
(≈5 × the per-page price above) instead of up to `max_pages`; only a
`continue: true` verdict (or an explicit `full_path_patterns` match) pays
the full bill. The run report's new `scan_ocr` block —
`{previewed, continued, stopped, pages_transcribed, stop_reasons: {…}}` —
is what an operator reads to see the split; `ocr_usage` (tokens/cost) keeps
working unchanged alongside it.

**A document that converted with no text is not a permanent dead end.**
Every `convert_empty` outcome (typically an unreadable scan, most often
because scan OCR was off when it was crawled) is recorded in the
connection's persisted crawl state as it is encountered; turning
`extraction.scan_ocr.enabled` on does nothing for documents already crawled,
since Graph's delta feed never re-offers an unchanged item on its own.
`POST …/connections/{id}/extraction/retry-empty`
(`agnes admin sharepoint retry-empty <connection_id>`) re-queues exactly
that backlog for another conversion pass, before the connection's ordinary
incremental crawl — see [`api-reference.md`](api-reference.md) for the exact
contract.

**Every reprocessing action an operator needed the shell for is a button**
(TCRD-296 synthesis). Besides the empty-conversion backlog above, a
connection also keeps a `failed_items` backlog — every convert-stage
failure (a conversion crash, a transient download error), including ones
already given up on after repeated attempts — replayed by `POST
…/connections/{id}/extract` with `{"retry_failed": true}`
(`agnes admin sharepoint extract <connection_id> --retry-failed`) BEFORE
the run's ordinary incremental delta walk. The source card's Run row (and
the fleet table at `/admin/extraction`, one screen down) show **"Retry
failed (N)"** and **"Retry empty (N)"** next to each connection, `N` read
from `GET …/extraction/status`'s `failed_items_count`/`empty_items_count`
— the persisted backlog sizes, never a client-side guess — and disabled
while a run for that connection is live. A connection whose most recent
run ended `failed`/`interrupted` also gets a plain **"Re-run"** button
(`POST …/extract` with no body — the same trigger a scheduled sweep or
`agnes admin sharepoint extract <connection_id>` would use). Every button
is a thin wrapper over the routes documented here and in
[`api-reference.md`](api-reference.md) — nothing new is introduced at the
protocol level, only a door that does not require a terminal.

All three can run against a self-hosted OpenAI-compatible endpoint instead
of the Anthropic API — globally (`extraction.llm`) or per stage, e.g. the
NER detector local while facts stay hosted:
[`self-hosted-llm.md`](self-hosted-llm.md).

Facts extraction normally runs as the tail of a crawl (`corpus-extraction`),
so a document only gets a model call once it has been crawled and ingested
in the SAME run. To (re)build the graph over a corpus that is already
indexed — after turning `extraction.facts.enabled` on for the first time
over an existing connection, or after a prompt/ontology change — trigger it
on its own, with its own wall-clock budget
(`extraction.facts.run_timeout_s`, independent of the crawl's own
`extraction.timeout_s`): source card → **Extract facts now** (next to
**Run extraction now**; disabled, with the reason, while either switch is
off), `POST /api/admin/sharepoint/connections/{id}/facts-extract`, or
`agnes admin sharepoint facts-extract <connection_id>` (`--doc-id` narrows
it to one document, e.g. to test a prompt change cheaply; `--timeout-s`
overrides the budget for that one run). The pass is incremental — indexed
documents without up-to-date facts — so it is safe to repeat, and it runs
alongside a crawl; while one is queued or running the card's Run row says
so and the button is locked.

**A pass continues itself until the ledger is done.** A single pass over a
large backlog can exceed `run_timeout_s` — it stops between documents
(`interrupted: timeout`), never mid-document, and whatever it already
extracted is shipped either way. When it stops that way with documents
still pending, the worker automatically enqueues the next pass on the same
connection (same idempotency key, so a manual "Extract facts now" click
never races it), 30 seconds out, carrying over the same `--doc-id`/
`--timeout-s` the stopped pass ran with. It never continues after a stop,
a permanent provider error, or once the backlog is actually empty, and it
caps at 48 consecutive continuations per connection (an operator needs to
re-trigger by hand past that, which is itself a sign something upstream —
throughput, quota, a stuck document — needs a look). The source card's
facts line and `agnes admin sharepoint runs` both show how many documents
are still pending and whether a pass is currently chasing them ("N pending
· continuing" vs. "N pending · not running") — the second phrase is the
one that means an operator should intervene.

### Provider limits (TCRD-296 synthesis F.25)

A live incident hit two shapes of "the provider will refuse EVERY call, not
just this one": an Anthropic workspace exhausting its on-demand usage limit
(a 400 whose message names a reset date), and a Vertex Claude quota bucket
with NO allocation at all for a region×model pair (a 429 that retrying the
SAME region reproduces identically). Before this classification existed,
both looked identical to any other model-call failure and FAILED THE JOB,
while the crawl's `extraction.facts.stream_every` trigger kept enqueueing a
fresh pass every threshold — 161 failed job rows overnight in the incident
this closes, with no single place saying "facts are paused because the
provider refuses."

**Classification.** `connectors.sharepoint.facts_extraction.
classify_provider_limit_error` recognizes a closed set of three reasons —
`workspace_limit`, `quota_exceeded`, `billing_disabled` — by message content
(neither provider exposes a distinct exception type or status code for
"the account is out of usage" versus "this one request was malformed").
This is deliberately DISTINCT from an ordinary transient 429/5xx, which the
existing AIMD/backoff retry already absorbs: a non-retryable-shaped error
(the Anthropic 400 case) classifies immediately, before any retry is
attempted; a retryable-shaped 429 (the Vertex quota case) is classified only
once every retry attempt has failed identically — telling a structural
zero-allocation bucket apart from an ordinary saturated-but-recoverable rate
limit.

**A hit ends the pass CLEANLY, not with a failed job.** The pass reports
`interrupted: true, interrupted_reason: "provider_limit"` and completes
normally — same posture as a `timeout` interruption — rather than raising
and failing the job. It also persists a fleet-level condition
(`extraction_conditions_repo()`, Postgres-only — see `docs/migrations.md`):
`{reason, provider, model, region, message, first_seen, last_seen,
retry_after_s}`. Fleet-level, not per-connection: the underlying refusal is
account/workspace/region-scoped, never tied to one SharePoint connection.

**The crawl's own streamed trigger backs off while a condition is active.**
`extraction.facts.stream_every`'s enqueue
(`crawler._enqueue_streamed_facts_pass`) checks
`streamed_pass_suppressed_by_provider_limit()` before enqueueing and skips
while a condition is still within its cooldown — the provider's own
`Retry-After` when given, else a 30-minute default. The self-continuation
chain (`maybe_continue_pass`, above) needs no separate check: it only ever
continues on `interrupted_reason == "timeout"`, and a provider-limit stop
always reports `"provider_limit"` instead, so it simply resets and stops on
its own. **The manual trigger (`POST …/facts-extract`,
`agnes admin sharepoint facts-extract`) is deliberately NEVER suppressed** —
an operator who just fixed the underlying limit should not have to wait out
the cooldown to prove it, and a pass for that provider completing WITHOUT
hitting a refusal is exactly what clears the condition for everyone else.

**Where it surfaces.** `GET /api/admin/sharepoint/extraction/runs` (the
fleet endpoint) gains a top-level `conditions[]` array — the `/admin/
extraction` fleet page renders it as a banner ("Facts extraction paused:
`<provider>` `<model>` in `<region>` — `<message>`; retrying after
`<time>`"), and `agnes admin sharepoint runs` prints the same line in the
terminal. Each connection's own `GET …/extraction/status` gains
`provider_limit` (the active condition, if any, matching THAT connection's
resolved facts provider) — the source card's facts line renders it as
"paused: provider limit" in place of the ordinary "N pending · continuing/
not running" wording.

**Region×model matrix validation** (live finding (b) — Vertex quotas are
per REGION and per MODEL: a project running Sonnet outside `global` answers
429 on every call, even a 5-token one, because it has no regional bucket at
all; Haiku's region buckets can also saturate at peak, a genuine capacity
limit rather than a missing bucket). `VERTEX_REGION_MODEL_MATRIX` in
`connectors/sharepoint/facts_extraction.py` documents the known-good
pairings (`haiku: global, us-east5, europe-west1`; `sonnet: global`); both
`POST /api/admin/server-config` (the `extraction.facts.vertex_region`
instance-level setting) and `PATCH …/extraction/facts-config` (a per-
connection override) refuse an undocumented region×model pairing with a
`422` naming the mismatch, instead of letting every pass discover it live.
A model tier the matrix does not name (`opus`, or a future tier) is treated
as unconstrained — a known-bad-combination guardrail, not a closed
allowlist.

Each document's request is kept under a token budget
(`extraction.facts.max_prompt_tokens`, default 150 000, hard-ceilinged at
190 000 regardless of what is configured) on top of the flat character
pre-cap (`extraction.facts.max_doc_chars`, default 120 000): a dense
document (a converted spreadsheet, CSV, or EDI-shaped export) is truncated
further and counted in `docs_truncated`; a binary/decode-garbage document
(a failed conversion) is skipped outright and counted in
`docs_skipped_garbled_text`; a dense document too large even at the token
budget is skipped rather than shipping a meaningless head, counted in
`docs_skipped_too_large_tabular`. A model call that fails PERMANENTLY for
one document's own request (most commonly a 400 "prompt is too long") is
counted in `facts_failed`/`facts_failed_reasons` and the pass continues
with the next document — it never aborts the whole run.

**What a facts-ledger `"done"` entry means, and what it does not (TCRD-296
gap #62).** Each document's per-connection facts state (`docs_state`,
keyed by `corpus_files.id`) is written `status: "done"` once the pass
extracted its facts — never once its evidence actually landed in the fact
graph, which happens in a LATER step (the batch's ingest call, possibly
several documents later). A `"done"` entry is what `is_up_to_date` treats
as current forever, so a document whose batch was refused, or whose
citation the ingest gate rejected/deferred, previously stayed `"done"`
with zero claims — invisible to every later pass, since nothing revisits
an already-`"done"` entry. Two things now keep this honest:

- **Cache citation.** The response cache (lever B above) keys on the
  CONVERTED markdown's content hash, while the model cites `doc_id` (the
  SOURCE document's identity) in its evidence. Two documents that convert
  to byte-identical markdown but differ in their source bytes share a
  cache row, and a naive replay would still cite the FIRST document. Every
  evidence entry is now rewritten to the document actually being
  processed right after the reply is parsed — the run report's
  `facts_evidence_doc_id_rewritten` counts how often this fired, whether
  the mis-citation came from a cache hit or the model itself.
- **Ledger correction.** The pass now reconciles each document's ledger
  entry against what its batch's ingest call actually reported: a refused
  batch downgrades its documents' entries to a bounded-retry status
  (`ingest_refused`) rather than leaving them `"done"`; a document whose
  own evidence contributed zero claims despite extracting facts is marked
  `no_claims` the same way; either status is retried automatically by the
  next pass (cache-served, so the retry costs no extra model call), up to
  a small bounded number of attempts before giving up with a terminal
  `"failed"` entry. A TCRD-241 duplicate copy — a byte-identical file whose
  claims all land on a SIBLING `corpus_files` row (the loader's
  deterministic winner-pick) — is the one legitimate zero-claims case: it
  stays `"done"`, with a `claims_on_file_id` marker pointing at the winner,
  so a coverage report can tell "duplicate" from "genuinely missing".

**Recovering the historical backlog.** The two fixes above only prevent
this from happening on a FRESH pass. A document whose ledger entry an
OLDER, pre-fix pass already wrote `"done"` with zero claims needs a
one-time reset: `POST /api/admin/sharepoint/connections/{id}
/facts/reset-no-claims` (`agnes admin sharepoint facts reset --no-claims
<connection_id> [--dry-run]`) checks every such candidate against the real
fact graph and either leaves it alone (already has a claim), backfills
`claims_on_file_id` (a duplicate copy whose sibling carries the claim), or
removes the ledger entry so the next pass re-extracts it (genuinely
missing). `--dry-run` computes and reports the same counts without writing
anything. Refuses with `409 facts_extraction_running` while a
facts-extraction pass — chained or standalone — holds the connection's
facts-pass lock, since that pass upserts the whole ledger payload on its
own schedule.

## Verifying completeness

"Did we really get everything?" is a live Graph Search count compared
against the corpus, not a guess: **Completeness** in the source card's
extraction drawer (or `/admin/extraction`'s own per-row button) shows, per
confirmed scope — and, for a connection with exactly ONE whole-drive scope,
per top-level folder under it — `expected` (Graph Search's own document
count, narrowed to convertible formats and to the crawl's own
`min_modified` cutoff), `indexed`/`rejected` (from the corpus), and the
crawl's own recorded reasons for anything missing: `failed`, `empty`,
`skipped_unsupported`, `oversize`. `gap = expected - indexed - failed -
empty - skipped_unsupported - oversize`, and each row's `status` is:

- **complete** — indexed already covers expected, nothing to explain.
- **accounted** — some documents are missing from the index, but every one
  of them has a recorded reason (failed, converted empty, an unsupported
  type, or over the size cap).
- **missing** — an unexplained gap remains after every known reason is
  applied. This is the row worth investigating first.
- **unknown** — `expected` itself could not be resolved (a scope that spans
  a whole SharePoint SITE across several drives has no single count to
  compare against) — never rendered as 0, which would read as "everything
  is missing" when the truth is "unmeasured".

Rows are sortable by `gap` (click the column header, or in the CLI they are
sorted descending by default) so the worst-looking scope/folder is always
the first thing an admin sees. The check fans out one Graph Search call per
scope/folder, so its answer is cached for 10 minutes — a **Recount** button
(`?refresh=true`) bypasses the cache for a fresh read. Running it while a
crawl is active still answers, just labeled `provisional: true` — a
snapshot mid-crawl, not a settled number. `agnes admin sharepoint
completeness <connection_id> [--min-modified YYYY-MM-DD] [--refresh]
[--json]` is the same check from a terminal.

Answers on both app-state backends (crawl state, `corpus_files` and the job
queue are all backend-agnostic — unlike run history above, this does NOT
need Postgres). See `connectors/sharepoint/completeness.py`'s module
docstring for the exact attribution rules behind each reason count on a
multi-scope connection.

## Watching several connections at once

Running crawl + facts over more than one connection (several tenants, or
several scopes split into separate connections) is one screen:
`/admin/extraction` (the **All connections** button in any SharePoint
source card's Run row on `/admin/data-sources`) — one row per connection with its phase, files
done/seen, a derived files/min, the facts pass's own done/pending counts,
token spend and estimated cost, and how old its last checkpoint is. A run
still marked `running` whose checkpoint has gone stale past
`extraction.stall_after_s` (default 900s/15min, admin-editable in
`/admin/server-config` → Extraction → Stall threshold) is reported as
`outcome: "stalled"` — the SAME word and the SAME threshold the fleet row's
"Stuck?" badge uses, so the two can never disagree. Defaults to connections
with a run active right now (`?active=1`); `?all=1` broadens to every
connection, idle ones included. `agnes admin sharepoint runs [--all]
[--json] [--watch]` is the same view from a terminal — `--watch` refreshes
every 10s, for an operator watching an overnight run over SSH with no
browser open. Both read `GET /api/admin/sharepoint/extraction/runs`,
PG-only like the rest of run observability (see the troubleshooting row
below). A small strip above the table — printed as a `Jobs — …` line from
the CLI — shows queued-vs-running counts per worker lane
(`corpus-extraction`, `sharepoint-facts-extraction`), independent of the
`active`/`all` scope: a lane with jobs queued and NONE running is flagged
(every worker slot busy elsewhere, or none configured for it) — the one
signal a connection stuck at "queued" forever has no `extraction_runs` row
to show any other way. Each row also carries its own "Retry failed (N)"/
"Retry empty (N)"/"Re-run" buttons, same rules as the source card's Run row
above.

### Cancelling a run Stop can't reach

A crawl loop that is genuinely stuck (not merely slow) never notices the
cooperative Stop button either — nothing yields, so the flag it sets is
never polled. For exactly that case, both the fleet table (every
`running`/`stalled` row) and the SharePoint source card's Run row (once it
reads `stalled`) offer a **Cancel run** button behind a confirm dialog —
`agnes admin sharepoint runs cancel <run_id>` does the same from a
terminal. Unlike Stop, cancel does not wait on the crawl: it force-finalizes
the owning job (`status: failed`, `error: cancelled_by_admin`, its lease
released — this is also what stops the worker's own lease-renewal loop) and
closes the run row `interrupted` immediately, while still setting the same
cooperative flag Stop does in case the loop is merely slow and can still
exit cleanly on its own. Whatever the run had ingested up to its last
checkpoint is kept and counted resumable, exactly like a normal stop.

## Troubleshooting quick table

| Symptom | Meaning | Fix |
|---|---|---|
| `409 feature_disabled` on admin routes | connector off | step 1 |
| `409 extraction_dependencies_missing` | converter backends missing (bare install) | `pip install 'agnes[extraction]'` |
| run ends `interrupted / timeout` | hit its time ceiling | fine — rerun resumes; raise `timeout_s` |
| run ends `interrupted / throttled` | tenant 429 budget exhausted | rerun later; lower concurrency |
| documents in `anonymize_failed` | fail-closed drop | check key status + detector availability; preview the file (step 3) |
| run history says "needs a Postgres backend" | `extraction_runs` is PG-only | run state needs Postgres app-state; config/preview still work |
| a facts pass fails with "prompt is too long" | one document's request exceeded the model's context window | fixed automatically going forward (token-safe bound + per-document failure); a still-oversized/garbled document is skipped and counted, never retried |
| run shows `outcome: "stalled"` and Stop doesn't help | crawl loop stuck, never polling the stop flag | **Cancel run** on the fleet table / source card (or `agnes admin sharepoint runs cancel <run_id>`) force-closes it |
