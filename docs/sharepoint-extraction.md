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
  every file.
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
  reports it. Per-run override in the Run-now options.
- **Split one large site across several connections**, each with its own
  crawl and facts jobs so they run in parallel instead of one connection's
  worth of concurrency working through the whole site sequentially:
  1. `POST /api/admin/sharepoint/connections/{id}/clone` or `agnes admin
     sharepoint connection clone <connection_id> --name <name>` — a sibling
     connection wired to the SAME tenant/client identity, certificate/
     client-secret reference (never a copied secret value) and site/host
     discovery bookkeeping (notably `manual_sites` — required under
     `Sites.Selected`, where `/sites` enumeration is 403-forbidden and a
     bookmarked site is the only way to resolve it at all), with zero
     scopes.
  2. `POST …/scopes/bulk` or `agnes admin sharepoint scope bulk-add
     <connection_id> --path "Folder A" --path "Folder B/Sub" [--drive-id
     <id>]` (or `--paths-file split.json`, a JSON list or `{"paths":
     [...]}`) — confirms every path as a scope in one call, reporting
     created/skipped/already-failed paths independently rather than
     all-or-nothing.
  3. Repeat 1-2 per clone, splitting the site's top-level folders across
     however many connections the crawl needs to parallelize over.
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
  double-creates. `--min-modified` lands on each clone's
  `config.extraction.crawl.min_modified` (bookkeeping today — no admin-facing
  crawl date filter reads that key yet); `--transport`/`--retry-mode` land on
  each clone's `config.extraction.facts`, the same keys `facts-config`
  writes. `--start` enqueues each clone's crawl immediately after creating
  it, in creation order, skipped silently (never a failed apply) when
  extraction readiness is not currently satisfied.
- Webhooks for near-real-time updates: mint the secret
  (`POST …/webhook`), then `POST …/subscriptions/ensure` — Agnes owns the
  Graph subscription lifecycle including renewals
  ([`api-reference.md`](api-reference.md) → *Graph subscription
  lifecycle*). Requires a public HTTPS origin (`AGNES_BASE_URL`).

## 6. Optional LLM stages (each a cost switch, default off)

| Stage | Switch | What it buys | Cost order |
|---|---|---|---|
| LLM name detection | `extraction.anonymization.detector: "llm"` | recall on names regex can't pattern-match | ~$5 / 1 000 docs (Haiku) |
| Scan OCR | `extraction.scan_ocr.enabled` | text from image-only PDFs | ~$0.006 / page |
| Facts extraction | `extraction.facts.enabled` (+ `facts.enabled`) | knowledge-graph facts with verbatim evidence | ~$0.05 / doc (Haiku), measured live — see `config/instance.yaml.example`'s `facts` block |

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
`extraction.timeout_s`): `POST /api/admin/sharepoint/connections/{id}
/facts-extract` or `agnes admin sharepoint facts-extract <connection_id>`
(`--doc-id` narrows it to one document, e.g. to test a prompt change
cheaply; `--timeout-s` overrides the budget for that one run).

## Watching several connections at once

Running crawl + facts over more than one connection (several tenants, or
several scopes split into separate connections) is one screen:
`/admin/extraction` — one row per connection with its phase, files
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
