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
- **access mode** — mirrored ACLs vs. open ([`RBAC.md`](RBAC.md) and the
  permission-zone notes in [`architecture.md`](architecture.md)).

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
- **Split one large site across several connections**, each with its own
  crawl and facts jobs so they run in parallel instead of one connection's
  worth of concurrency working through the whole site sequentially:
  1. `POST /api/admin/sharepoint/connections/{id}/clone` or `agnes admin
     sharepoint connection clone <connection_id> --name <name>` — a sibling
     connection wired to the SAME tenant/client identity and certificate/
     client-secret (a vault-stored one is copied verbatim, never decrypted;
     an env-var-sourced one resolves on its own — either way the clone is
     immediately ready to crawl, no re-upload) and site/host discovery
     bookkeeping (notably `manual_sites` — required under `Sites.Selected`,
     where `/sites` enumeration is 403-forbidden and a bookmarked site is
     the only way to resolve it at all), with zero scopes.
  2. `POST …/scopes/bulk` or `agnes admin sharepoint scope bulk-add
     <connection_id> --path "Folder A" --path "Folder B/Sub" [--drive-id
     <id>]` (or `--paths-file split.json`, a JSON list or `{"paths":
     [...]}`) — confirms every path as a scope in one call, reporting
     created/skipped/already-failed paths independently rather than
     all-or-nothing. By default each path still mints its own collection —
     add `--collection-id <id>` (an existing, live collection) or
     `--collection-name <name>` (mint one) to route every scope THIS call
     creates to ONE shared collection instead, so the split site still
     reads, shares and selects in chat as a single collection rather than
     one per scope.
  3. Repeat 1-2 per clone, splitting the site's top-level folders across
     however many connections the crawl needs to parallelize over — reuse
     the SAME `--collection-id` across connections to keep the whole site
     in one collection.

  **Already split without the shared-collection option?** `POST
  …/connections/{id}/collections/consolidate` or `agnes admin sharepoint
  collections consolidate <connection_id> --target-name <name> |
  --target-collection-id <id> [--execute]` folds a connection's per-scope
  collections into one target after the fact — a dry-run preview (the
  default) lists what would be folded and how many files, `--execute`
  performs the real merge (files/chunks/claims re-pointed, grants unioned,
  emptied sources soft-deleted). Also reachable from the source card's
  overflow menu (**Consolidate collections…**). PG-only (A3 ratchet).

  reports it. Per-run override in the Run-now options. Editable in
  `/admin/server-config` → *Extraction* → *crawler*; this is the extraction
  worker's **memory lever** (every file in flight is a converter child
  process holding that document — six in flight has exceeded a 12 GiB
  container on large decks, two held it under 4 GiB), and the worker reads
  it at the start of each run, so a save applies to the next run with no
  restart.
     however many connections the crawl needs to parallelize over.
- `extraction.crawl.min_modified` — a per-connection age filter for a
  backfill run: crawl only files modified on/after a cutoff date instead of
  re-walking a whole multi-year corpus. `PATCH …/extraction/crawl-config`
  (`agnes admin sharepoint crawl-config <connection_id> --min-modified
  YYYY-MM-DD` / `--clear`) sets or clears it; an item with no modified
  timestamp is always kept.
- **Or let Agnes do the split for you.** `GET /api/admin/sharepoint
  /connections/{id}/split-plan?n=<n>[&min_modified=YYYY-MM-DD][&drive_id=<id>]`
  (`agnes admin sharepoint split-plan <connection_id> --n <n> [--min-modified
  YYYY-MM-DD] [--json]`) previews a greedy-packed split of the drive root's
  top-level folders into `n` groups of roughly equal document count (a live
  Graph Search count per folder — never a delta walk, which throttles under
  repetition and biases its own first pages), and reports any file sitting
  directly at the drive root (`loose_root_files`) that a folder-based split
  — this one, and the manual clone + `scopes/bulk` recipe above — can never
  cover. A folder whose count could not be read is still assigned to a
  group, at `documents: 0`, never dropped from the plan. The SharePoint
  connection card's own **Split this site…** control (Actions menu, or the
  same-named button on the card body) previews and applies this from the
  browser. `POST …/splits` (`agnes admin sharepoint split <connection_id>
  --n <n> [--min-modified YYYY-MM-DD] [--transport sync|batch] [--retry-mode
  off|on_gate_fail|always] [--start]`) then creates all `n` clones AND their
  scopes in one call — the same `clone` + `scopes/bulk` primitives above,
  run automatically — named `"<source name> — part i/n"`; `409 split_exists`
  if a split under those names already exists, so a repeat call never
  double-creates. `--min-modified` lands on each clone's own
  `config.extraction.crawl.min_modified` above; `--transport`/`--retry-mode`
  land on each clone's `config.extraction.facts`, the same keys
  `facts-config` writes. `--start` enqueues each clone's crawl immediately
  after creating it, in creation order, skipped silently (never a failed
  apply) when extraction readiness is not currently satisfied.
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

## Watching several connections at once

Running crawl + facts over more than one connection (several tenants, or
several scopes split into separate connections) is one screen:
`/admin/extraction` (the **All connections** button in any SharePoint
source card's Run row on `/admin/data-sources`) — one row per connection with its phase, files
done/seen, a derived files/min, the facts pass's own done/pending counts,
token spend and estimated cost, and how old its last checkpoint is (flagged
once it passes 10 minutes on a run still marked running — "stuck?", not an
outcome, just a prompt to go look). Defaults to connections with a run
active right now (`?active=1`); `?all=1` broadens to every connection, idle
ones included. `agnes admin sharepoint runs [--all] [--json] [--watch]` is
the same view from a terminal — `--watch` refreshes every 10s, for an
operator watching an overnight run over SSH with no browser open. Both read
`GET /api/admin/sharepoint/extraction/runs`, PG-only like the rest of run
observability (see the troubleshooting row below).

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
