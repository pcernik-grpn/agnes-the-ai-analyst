# Extraction observability — admin UI design

**Status:** design only, nothing implemented. Written against the built-in
document pipeline landing today (`extraction.producer.mode: builtin`).
**Scope:** what an admin can see about the pipeline — running, cost,
configuration, per-document outcome — and where each of those lives on a page
that already exists.

Related: [fact-graph over Collections](2026-08-27-fact-graph-over-collections-design.md)
§13.2 (Surfaces), [anonymization](../../anonymization.md),
[empty/blocked/forbidden vocabulary](2026-08-29-empty-blocked-forbidden-vocabulary-design.md).

---

## 1. Problem

Until today the `corpus-extraction` job kind shelled out to an
operator-supplied producer: Agnes resolved credentials, ran a subprocess, and
learned exactly two things — the exit code and, later, whatever the producer
chose to POST to `/api/facts/ingest`
(`app/worker/kinds.py:1497-1733`). Every honest limitation on the source card
today follows from that boundary:

- the crawl schedule row is *static text* — "Crawl runs externally — this line
  is a label, not live state" (`app/web/router.py:8446-8449`);
- the "Queue cost" cell in the pipeline strip is a placeholder constant,
  `0.02 × rejected/deferred items`, with a comment saying so
  (`app/web/router.py:8250-8258`);
- the error badges are "a deliberate, narrower simplification" of the spec's
  categories, because the per-document error log "lives in the CRAWLER's own
  … which Agnes never receives" (`app/web/router.py:8342-8356`);
- anonymization is *declared*, never verified, because the anonymizer ran
  outside (`docs/anonymization.md`, "Current limits").

With `mode: builtin` all four causes disappear at once. The crawl runs
in-process in the extraction worker
(`app/worker/kinds.py::_run_builtin_corpus_extraction` →
`connectors/sharepoint/crawler.py::run_builtin_crawl`), the converter reports
which engine produced each document (`connectors/sharepoint/convert.py:145-155`),
the anonymizer returns a substitution count
(`src/anonymization.py:90-99`), the optional LLM detector reports its own token
usage (`src/anonymization_ner.py:576-608`), and the crawl emits a 30-field run
report (`connectors/sharepoint/crawler.py:274-312`).

**None of that reaches a screen.** The report is written into a JSON state file
and returned as the job result; the card still renders the external-producer
story. The gap this document closes is not "collect more data" — it is
*surface the data the pipeline already produces, and label the parts we still
cannot see as unknown rather than healthy.*

Four questions, one per surface:

1. **What is running right now?** phase, progress, elapsed, ETA, throttling,
   and the last N runs with outcomes.
2. **What does it cost?** per run and per month, in $, with the price's source
   named — plus a projection *before* a large run.
3. **How is it configured?** every effective value with its origin, and which
   values an admin may change where.
4. **What happened to my document?** engine, characters, anonymization,
   ingest status, or the exact reason it was skipped.

---

## 2. Design principles (all inherited, none new)

| # | Principle | Where it comes from |
|---|---|---|
| P1 | **Zero new navigation.** No new page, no new nav item, no new grant type. "A capability that needed five new pages would be a signal the model is wrong." | spec §13.2 (`…fact-graph-over-collections-design.md:1209-1217`) |
| P2 | **The card stays a verdict; itemized detail lives one click below in a drawer** filtered to one category. | spec §13.2 "Source card"; implemented as `toggleFileSourceDrawer` (`app/web/templates/admin_data_sources.html:2394`) |
| P3 | **State, not settings** — a collection detail shows what happened; the *change* affordance is a named batch task, never a toggle. | spec §13.2 "Collection detail"; quoted in `app/api/admin_sharepoint.py:348-355` |
| P4 | **Requested ≠ declared ≠ verified.** Never collapse an intent, a self-report and an observation into one word. | `docs/anonymization.md` "Badge semantics"; `_scope_out`'s `anonymization_declared` (`app/api/admin_sharepoint.py:431-437`) |
| P5 | **FAILED never degrades into EMPTY.** Four states — NOTHING_FOUND / EMPTY / BLOCKED / FAILED — with severity precedence, one shared macro. | `2026-08-29-empty-blocked-forbidden-vocabulary-design.md` §2/§4; `app/web/templates/macros/_state.html` |
| P6 | **Refuse before you run, and name the exit.** A trigger that cannot succeed returns a typed 409 naming the fix, never a job that dies 30 minutes later. | `_extraction_readiness` (`app/api/admin_sharepoint.py:514-544`), `trigger_extraction` (`:1111-1158`) |
| P7 | **Estimate first.** A potentially expensive operation is priced before it is run, and the estimate names its basis. | `agnes snapshot create … --estimate`; `bq_cost_estimate_usd` from a configurable price (`app/api/v2_scan.py:453-455`) |
| P8 | **Tokens only, one shell, shared components.** `--ds-*` tokens, `base_page.html`/`base_ds.html`, `macros/_detail.html` for any detail page. | `.claude/skills/agnes-conventions/references/design-system.md:41-157`, `references/web-page.md:1-31` |
| P9 | **Degrade per block, never 500 the page.** Every sub-block of the source card already carries its own `try/except`. | `_sharepoint_pipeline_cell` (`app/web/router.py:8358-8362`) |

**The one principle this document adds by extension, not invention (P10):
every number on these surfaces carries its own freshness and its own source.**
A value read from a completed run says *when* that run ended; a value read from
a live run says *as of* its last checkpoint; a value that could not be read
renders as FAILED (P5), never as `0`, `—`, or the previous value. Today's card
violates this in a small but instructive way: `SOURCE_PIPELINES` is
server-rendered once into the template
(`app/web/templates/admin_data_sources.html:1829`, fed by
`app/web/router.py:7862`), while `loadConnections()` re-fetches the connection
list around it — so counts silently age while the page looks live.

---

## 3. Ground truth — what the pipeline emits today

Everything below is a field that exists in code as of this branch. **The design
uses only these; §7 lists separately the small set of additions it requires.**

### 3.1 Crawl run report — `CrawlStats.report()`

`connectors/sharepoint/crawler.py:274-312`, persisted to
`state["last_run"]` and returned as the job result.

| Group | Fields |
|---|---|
| Identity | `mode` (`"builtin"`), `started_at`, `finished_at`, `duration_s`, `interrupted`, `connection_id`, `scope_errors[]` |
| Volume | `scopes`, `drives`, `new`, `changed`, `unchanged`, `deleted`, `downloads`, `bytes_downloaded` (+ `_human`), `files_per_s` |
| Transport | `requests`, `retries`, `http_429`, `throttle_wait_s`, `retry_wait_s`, `token_refreshes`, `delta_resyncs` |
| Refusals | `errors`, `convert_failed`, `anonymize_failed`, `excluded_subtree_skips`, `permission_skips`, `skipped_oversize {files, bytes, largest[≤20]}`, `max_file_mb` |

Two properties matter for the UI. First, **an interrupted run still reports**:
the `except BaseException` path writes the same report with
`interrupted: true` before re-raising (`crawler.py:1168-1174`) — so "the worker
was killed" is a *rendered outcome*, not a missing row. Second, **every skip is
counted**: "an unindexed document must never be invisible in the report"
(`crawler.py:236-238`), and `note_oversize` keeps the 20 largest offenders with
their paths (`crawler.py:266-272`).

### 3.2 Per-document seams

| Signal | Shape | Source |
|---|---|---|
| Conversion | `ConvertResult{markdown, engine}`, `engine ∈ {markitdown, pypdfium2, passthrough, empty}` | `connectors/sharepoint/convert.py:100-103,145-155` |
| PDF structure | `DocumentMarkdown.stats = {pages, headings, tables, fallbacks, degraded_pages}` | `connectors/sharepoint/pdf_structure.py:123,748-757` |
| Anonymization | `AnonymizeResult{text, replaced}` — occurrences, never the entities | `src/anonymization.py:90-99,756-822` |
| LLM NER usage | `.last_usage` (per document) / `.total_usage` (per detector) = `{calls, chunks, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, entities, dropped_not_verbatim, model}` | `src/anonymization_ner.py:558-568,576-608` |
| Ingest outcome | `corpus_files.processing_status ∈ {pending, processing, indexed, needs_review, rejected}` + `processing_detail` (free text) | `src/db.py:1591-1605` |
| Source anchor | `corpus_file_sources{corpus_file_id, corpus_id, source_stable_id, source_doc_id, source_sha256, source_url}` (PG-only) | `migrations/versions/0075_corpus_file_sources.py:33-50` |
| Job lifecycle | `jobs{status, attempts, error, created_at, started_at, finished_at}` + `payload_json.result` (the run report, merged by `complete()`) | `migrations/versions/0041_jobs_v94.py:56-74`; `src/repositories/jobs_pg.py:345-400` |

### 3.3 What is *not* emitted (and therefore must not be rendered)

- **Per-file rows for the pipeline's decisions.** `_process_item`
  (`crawler.py:861-969`) increments counters and returns; nothing durable
  records *which* file used which engine, how many substitutions it took, or
  why it was dropped. Oversize skips keep 20 paths in the report; convert and
  anonymize failures keep none.
- **NER usage from a crawl.** The crawler's anonymize seam calls
  `anonymize_markdown(text, key=…)` with no `detector` argument
  (`crawler.py:614-624`), so the regex detector is used and `LLMDetector` is
  never constructed. **No tokens are spent, and none are reported, until that
  is wired** — §7.2.
- **Live progress.** `save_state` runs once per delta page
  (`crawler.py:1053-1060`), but writes only `delta_links`/`ctags`; the report
  exists only at the end.
- **A price for anything.** There is no model-price table anywhere in the
  repository. `api.scan.bq_cost_per_tb_usd` (`app/api/v2_scan.py:453`) is the
  only priced surface and it reads its rate from config with a documented
  default.
- **Per-scope scan transcription.** Vision transcription today is
  instance-level and env-configured (`src/ingest/vision.py:24`,
  `AGNES_VISION_MODEL`); the spec's per-scope control (§7.1, `:587-594`) does
  not exist.

---

## 4. Surface 1 — "What is running right now"

### 4.1 Where it lives

The existing SharePoint `.ds-src` card on `/admin/data-sources`
(`app/web/router.py:7821`, rendered by
`app/web/templates/admin_data_sources.html`). Three changes, no new page:

1. The **pipeline strip's first cell** (`Crawl`) becomes live while a run is
   in flight: phase + progress instead of a document total.
2. A new **`Run` fact row** in the card body, immediately above the existing
   `In-Agnes extraction` row (`admin_data_sources.html:2270-2277`) — it owns
   the "Run extraction now" button, which moves up from the schedule row so the
   action sits beside the state it changes.
3. The existing **drawer** (`ds-filesource-drawer`, `:2310`; `toggleFileSourceDrawer`, `:2394`) gains a `runs`
   segment alongside today's four error categories — same `toggleFileSourceDrawer`
   mechanism, same segmented switch the spec asks for (§13.2 "Source card").

### 4.2 Wireframe — card, run in flight

```
┌─ SP  Corp SharePoint ─────────────────────── ● running ──── ⌄ ─┐
│ tenant 8f2c1d0a… · 3 scopes · Finance                          │
├────────────────────────────────────────────────────────────────┤
│ ┌──────────────┬───────────────┬──────────────┬──────────────┐ │
│ │ Crawl        │ Extraction    │ Facts → graph│ Run cost     │ │
│ │ convert      │ 812 indexed   │ 4 210 · 1 903│ $0.71 so far │ │
│ │ 812/1 240    │               │              │ (est. $1.08) │ │
│ └──────────────┴───────────────┴──────────────┴──────────────┘ │
│                                                                │
│ Run          ▶ running · convert · 812/1 240 files · 2.1 f/s   │
│              started 14:02 · 6m 31s elapsed · ~3m left         │
│              ⚠ throttled — 4× HTTP 429, 38s waited             │
│              [ Stop after this file ]      [ Run history (8) ] │
│                                                                │
│ In-Agnes     builtin producer · last run 14:02 · next 15:00    │
│ extraction   [ Run extraction now ]                            │
│ …                                                              │
│ Last run     Rejected quotes 3   Deferred 0   Protocol 0       │
└────────────────────────────────────────────────────────────────┘
        as of 14:08:41 · refreshed 3s ago            ⟳
```

Rules the wireframe encodes:

- **Phase** is one of `crawl · convert · anonymize · ingest`, derived from the
  checkpoint the worker last wrote (§7.1). It is a *label of the last observed
  phase*, not a claim about this instant — the caption line says `as of`.
- **Progress** is `files_done / files_seen`. `files_seen` is the delta
  enumeration total *so far*, which grows during the crawl phase; while it is
  still growing the denominator renders as `812/1 240+` with a tooltip
  ("enumeration still running — the total can still rise"). A percentage bar is
  deliberately **not** drawn until enumeration completes, because a bar over a
  moving denominator is the archetypal number that looks checked and is not
  (P10).
- **ETA** is `(files_seen − files_done) / files_per_s`, shown only when
  enumeration is complete AND `files_per_s > 0`; otherwise the slot reads
  `ETA unknown`. Never a spinner standing in for a number.
- **Throttle indicator** appears only when `http_429 > 0`, and states both
  counts (`4× HTTP 429, 38s waited`) — `throttle_wait_s` is real wall-clock the
  operator is paying for and is the single most common cause of "why is this
  taking so long".
- **`Stop after this file`** is honest about its semantics: the worker has no
  hard-kill path; it sets a cooperative cancel flag the crawl checks between
  items, and the run then finalizes with `interrupted: true` (§7.1). Label it
  what it does. (If the flag is not built in v1, the button is not drawn —
  never a control that cannot succeed, per the empty-state doc's "never a CTA
  that can't succeed".)

### 4.3 Wireframe — run history drawer

Opened by `Run history (8)`; the same drawer element, `segment=runs`.

```
┌ Run history — Corp SharePoint ──────────────────────── [ × ] ┐
│ ( runs ) ( rejected quotes 3 ) ( deferred ) ( protocol )     │
├──────────────────────────────────────────────────────────────┤
│ ● 31 Aug 14:02   running   6m 31s   812/1 240   convert      │
│ ✓ 31 Aug 09:00   done      11m 04s  1 240 files · 3.9 GB     │
│                  1 108 new · 132 changed · 0 deleted         │
│                  skipped: 7 oversize (1.4 GB) · 2 convert    │
│ ⚠ 30 Aug 21:00   interrupted 4m 12s  318/1 240               │
│                  resumed by the 31 Aug 09:00 run — 0 files   │
│                  re-downloaded (cTag match)                  │
│ ✗ 30 Aug 15:00   failed    0m 02s                            │
│                  corpus-extraction: extraction.enabled is    │
│                  false — refusing to run                     │
│ …                                              5 more runs ⌄ │
└──────────────────────────────────────────────────────────────┘
```

- Four outcome glyphs, matching the job lifecycle exactly: `running`, `done`,
  `interrupted` (`report.interrupted == true`), `failed` (`jobs.status`).
  `interrupted` is its own outcome, not a flavour of failure — the run
  *ingested what it ingested*, and the next run resumes from the persisted
  cTags without re-downloading (`crawler.py:894-897,966-969`). Saying so on the
  row is what stops an operator from re-running a 4-hour crawl out of doubt.
- `failed` rows show `jobs.error` verbatim. The refusal strings are already
  written for a human (`kinds.py:1583-1590`).
- The drawer is capped and paged like the existing rejection drawer; the count
  in the button is the *total*, so "5 more runs" is never a silent truncation.

### 4.4 Refresh mechanics — polling, and why not SSE

**Decision: poll `GET …/extraction/status`. 3 s while a run is active, 30 s
when idle, paused when `document.visibilityState !== "visible"`.**

Precedent for the cadence: `/admin/sync` polls every 3 s while a sync holds the
lock and 5 s otherwise (`app/web/templates/admin_sync.html:365-367,421`). The
idle interval is stretched to 30 s here because this card's payload is heavier
than a lock flag.

SSE exists in this codebase — `/api/admin/cache-warmup/stream`, consumed with a
polling fallback (`app/web/templates/admin_tables.html:6509-6530`) — and is
**the wrong tool here**, for a structural reason worth stating so it is not
re-litigated: that stream reads a module-global `WARMUP_STATE` in the process
that owns the work (`app/api/cache_warmup.py:255-276`). A `corpus-extraction`
job runs in a *different process* — the `extraction-worker` role-split
container (`docker-compose.yml:219-255`, `AGNES_WORKER_LANES=extraction`). An
in-memory subscriber list in the API process can never see it. Any live view
must therefore read a **shared store**, and once the state is in a shared store
the difference between SSE and a 3-second poll is a fan-out optimisation for a
single-admin page. Poll.

Failure honesty in the poll loop (P5/P10):

- a failed poll switches the caption to `couldn't refresh — last read 14:08:41`
  in `--ds-accent-danger-*` and keeps the stale numbers **visibly marked
  stale**; it never blanks them and never re-renders them as current;
- three consecutive failures collapse the block to `state.panel(FAILED)` with a
  Retry, per `macros/_state.html`;
- a `501 requires_postgres_backend` (§7.1) renders once as an explanatory
  EMPTY-tone panel — "run history needs a Postgres backend" — and *stops
  polling*, exactly as the card already words its PG-only degradation
  (`admin_data_sources.html:2126-2131`).

---

## 5. Surface 2 — "What it costs"

### 5.1 The honesty problem, first

Agnes has no price list. Adding one to a vendor-agnostic public repository is a
maintenance trap (prices change; a stale constant silently misreports money)
and a vendor entanglement. The existing answer in this codebase is
`api.scan.bq_cost_per_tb_usd` — **a rate read from config with a documented
default, applied to a measured quantity** (`app/api/v2_scan.py:453-455`).

This design mirrors it exactly:

```yaml
extraction:
  pricing:
    # Per MILLION tokens, in USD, per model id. No defaults ship: a price
    # nobody set renders as "not priced", never as $0.00.
    "claude-haiku-4-5":
      input: 1.00
      output: 5.00
      cache_write: 1.25
      cache_read: 0.10
    source: "vendor public price list, checked 2026-08-31"   # free text, shown in the UI
```

Consequences, all deliberate:

- **An unpriced instance sees tokens, not dollars.** The cell reads
  `41.2k tokens · not priced` and links to where the rate is set. `$0.00` for
  "we don't know" is exactly the healthy-looking unverified value P10 forbids.
- **The price's provenance is displayed**, not just its value: hovering the
  cost shows `1.2M in + 84k out × extraction.pricing["claude-haiku-4-5"]
  (source: "…", set 2026-08-31)`.
- Bytes downloaded are reported as **bytes**, never converted to money. Egress
  pricing depends on the tenant's own agreement; Agnes does not know it.

### 5.2 Where it lives

- **Pipeline strip, cell 4** — replaces the placeholder
  `_FILE_SOURCE_QUEUE_COST_PLACEHOLDER_PER_ITEM` cost
  (`app/web/router.py:8250-8258,8430-8434`) with the real figure. The constant
  and its "not a real cost model" comment are deleted in the same change; a
  placeholder that outlives its cause is how a card starts lying.
- **A `Cost` fact row** in the card body: this run, this month, and the input
  quantities.
- **The estimate** appears inside the existing `Run extraction now` flow
  (`runSpExtraction`, `admin_data_sources.html:2737-2773`) as a confirm step —
  not a new dialog family, not a new page.

### 5.3 Wireframe — cost row and pre-run estimate

```
│ Cost         This run  $0.71   1.19M in · 84k out · 312k cached│
│              This month $12.40 across 34 runs (since 1 Aug UTC)│
│              Downloaded 3.9 GB this run · 61 GB this month     │
│              Priced at extraction.pricing["claude-haiku-4-5"]  │
│              source: "vendor list, checked 2026-08-31"         │
```

```
┌ Run extraction now — Corp SharePoint ──────────────── [ × ] ┐
│                                                             │
│  Estimated for this run                                     │
│  ───────────────────────────────────────────────────────── │
│  Files to process        ~1 240   (delta since 09:00: 132)  │
│  Download                ~3.9 GB                            │
│  LLM tokens (NER)        ~1.3M in · ~90k out                │
│  Estimated cost          ~$0.74                             │
│                                                             │
│  Basis: the last completed run (31 Aug 09:00) averaged       │
│  1 052 input tokens/document. Enumeration is exact; token    │
│  and cost figures are projections from that average and can  │
│  be wrong for a scope with different documents.              │
│                                                             │
│  ⚠ 7 files over the 50 MB cap will be skipped (1.4 GB).      │
│                                                             │
│             [ Cancel ]            [ Start run ]             │
└─────────────────────────────────────────────────────────────┘
```

Design notes:

- **The estimate enumerates; it never downloads.** `POST …/extract/estimate`
  walks the delta the same way the crawl does but stops at metadata: it knows
  file counts, byte totals and oversize skips *exactly*, from Graph's own
  `size` field. Only the token/$ line is a projection — and it names its basis,
  the way `--estimate`'s output names its dry-run source.
- **With no completed run to average, there is no $ line at all.** The dialog
  then says "First run — no basis for a token estimate yet" and shows only the
  exact columns. A first-run projection from a hardcoded tokens-per-document
  guess is a fabricated number.
- **The estimate is shown, not enforced.** No threshold blocks a run; the spec
  asks for visibility, and a cap here would duplicate the real guardrail
  (`extraction.crawler.max_file_mb`) at a worse layer.
- Month-to-date is labeled **"across N runs (since 1 Aug UTC)"** — it is a sum
  over this instance's own extraction runs, not an LLM bill. The distinction
  matters the first time it disagrees with an invoice.

---

## 6. Surface 3 — "How it is configured"

### 6.1 Where it lives

A new `Configuration` fact row on the same card, opening the same drawer with
`segment=config`. It is a **read-out with origins**, plus deep links to the one
place each value can be changed. It is not an editor: the whole `extraction`
block is deploy-time today (§6.3), and inventing a second write path for it
would be a new settings surface — precisely what P1 forbids.

### 6.2 Wireframe

```
┌ Configuration — Corp SharePoint ────────────────────── [ × ] ┐
│ ( runs ) ( config ) ( rejected quotes 3 ) ( deferred ) …     │
├──────────────────────────────────────────────────────────────┤
│ Producer        builtin              instance.yaml  🔒 deploy │
│                 extraction.producer.mode                     │
│ Enabled         true                 env AGNES_EXTRACTION_… 🔒│
│ Schedule        every 1h             instance.yaml  🔒 deploy │
│                 next 15:00 · last 14:02                      │
│ Timeout         3600s                instance.yaml  🔒 deploy │
│ Max file size   50 MB                instance.yaml  🔒 deploy │
│                 7 files skipped last run (1.4 GB) — these    │
│                 documents are NOT in the collection          │
│ Checkpoint      every 200 delta rows  built in — not settable │
│ NER detector    off (regex only)     — no LLM tokens spent    │
│ NER model       claude-haiku-4-5     extraction.model 🔒      │
│ Anonymization   key from env AGNES_ANONYMIZATION_HMAC_KEY     │
│                 (name only — the value is never displayed)    │
│ Scan transcript instance-wide, env AGNES_VISION_MODEL         │
│                 ⓘ not per-scope — every scope gets the same   │
├─ Per scope ──────────────────────────────────────────────────┤
│ Finance/Contracts    anonymize ✓ requested · declared ✓       │
│                      audience: Legal ▸ Finance ▸ All          │
│ Finance/Reports      anonymize —                              │
│ HR/Handbook          anonymize ✓ requested · ⚠ not declared   │
└──────────────────────────────────────────────────────────────┘
```

- **Origin badge on every row** — `instance.yaml` / `env <NAME>` /
  `per-scope` / `built in`. The data already exists in the shape
  `GET /api/admin/config-surface` returns (`{key, env_var, yaml_path, default,
  current_value, source}`, `app/api/config_surface.py:1-20`); this drawer is
  the extraction slice of the same idea, scoped to one connection.
- **`🔒 deploy` marks a value an admin cannot change from the UI**, with the
  reason on hover, taken from the switch's own `lock_reason`
  (`app/switches.py:705-732`). A locked row is *outlined* and carries no
  chevron; an editable row is outlined *with* a chevron linking to
  `/admin/server-config` — the form/affordance rule from the design system
  ("'you can change this' is a FORM signal, not a hue",
  `design-system.md:97-112`).
- **Oversize skips are explained where the cap is shown**, not only in the run
  report: a document over the cap is never downloaded, never converted, and
  never appears in the collection — the one configuration value whose
  misunderstanding produces a silently incomplete corpus.
- **Per-scope rows reuse `_scope_out` verbatim** (`app/api/admin_sharepoint.py:417-475`)
  — `anonymize`, `anonymization_declared`, `audience_classes`, `tiered`,
  `no_group_warning` — the same projection the wizard's step-3 preview and the
  card's scope list already render, so a fourth copy cannot drift (P4).

### 6.3 Which keys belong in `/admin/server-config` — and why none do yet

The precedent is explicit. `extraction.enabled` is registered with
`editable=False` and a `lock_reason` naming three unmet prerequisites — a
configured producer, a worker actually polling the `extraction` lane, and the
multi-process requirements that a role split imposes
(`app/switches.py:705-732`). Two structural facts extend that verdict to the
whole block:

1. **`editable=True` on one switch makes the entire section admin-writable.**
   `POST /api/admin/server-config` validates the *section* name and then
   deep-merges the patch — stated in the `Switch` docstring precisely so this
   is never a silent side effect (`app/switches.py:56-70`;
   `app/api/admin.py:529-546`).
2. **The `extraction` section contains `producer.command`** — a command line
   the server executes as a subprocess. Making the section writable turns the
   admin settings form into an arbitrary-command surface. That is a security
   change, not a UX one, and it does not become acceptable merely because
   `mode: builtin` no longer *uses* the command.

**Recommendation for v1: `extraction` stays out of `_EDITABLE_SECTIONS`;
every row in §6.2 renders read-only with its origin.** Two follow-ups, sized
and named rather than smuggled in:

- If a runtime-editable knob is wanted (the plausible candidates are
  `crawler.max_file_mb`, `schedule`, and `pricing`), it must live in a section
  that contains no executable, e.g. a new `extraction_runtime:` block — which
  requires one entry in `_SECTION_BASELINE_EFFECT` (`app/api/admin.py:576-645`)
  classified `live`, since every one of those values is read per run
  (`_max_file_mb`, `crawler.py:1077-1084`).
- `extraction.enabled`'s own `lock_reason` needs a factual update in builtin
  mode: "needs a configured `extraction.producer` command" is no longer true
  when `mode: builtin`. The worker-lane and multi-process halves still are.

---

## 7. Data the surfaces need — the smallest additions that work

Four surfaces, three additions. Each is justified against an existing
precedent, and each is PG-only per the A3 ratchet (new app-state = a `_pg.py`
repo plus an Alembic revision, no DuckDB sibling, no `src/db.py` ladder step).
The card already degrades honestly on a DuckDB instance
(`admin_data_sources.html:2126-2131`), so this adds no new class of failure.

### 7.1 `extraction_runs` (new table, revision `0087`)

Modelled on `facts_ingest_runs` (`migrations/versions/0078_facts_ingest_runs.py`),
for the same reason it exists: "so the source card has something to read …
without an admin having to have been watching the live response".

```
extraction_runs
  id                text  pk          job_id            text  (jobs.id)
  connection_id     text  idx         status            text  running|done|interrupted|failed
  started_at        timestamptz       finished_at       timestamptz  null
  phase             text              checkpoint_at     timestamptz
  files_seen        int               files_done        int
  enumeration_done  bool              report            jsonb  (CrawlStats.report(), final)
  progress          jsonb  (live counters at last checkpoint)
  usage             jsonb  ({model, calls, input_tokens, output_tokens,
                             cache_creation_input_tokens, cache_read_input_tokens})
  skips             jsonb  (capped list: {path, reason, detail}, ≤200 + a total)
  error             text  null
```

- **Written by the crawl at its existing checkpoint cadence** — the same place
  `save_state` already runs, once per delta page of 200 rows
  (`crawler.py:1053-1060,126`). No new write loop, no new frequency; the
  checkpoint that already exists gains a second destination. `checkpoint_at` is
  what the UI's `as of` caption prints.
- **Why not the `jobs` table.** `jobs` already carries lifecycle and the final
  report (`payload_json.result`), and `GET /api/jobs?kind=corpus-extraction`
  already lists them (`app/api/jobs.py:140-150`). It cannot carry *progress*:
  adding a mutable progress column would mean touching a frozen DuckDB↔PG repo
  pair on both sides plus its contract test, and it has no per-connection
  index. `jobs` stays the lifecycle owner; `extraction_runs.job_id` joins them.
- **Why not the crawl state file.** `state_path()` is under `DATA_DIR`
  (`crawler.py:169-184`), which the compose topology happens to share between
  `app` and `extraction-worker` (`docker-compose.yml:225-226`) — but that is a
  deployment coincidence, not a contract, and it breaks the moment a worker
  runs on another host. A shared store the API already speaks is the honest
  choice.
- Retention follows the existing `retention.*` pattern
  (`app/instance_config.py:1809-1816`); default keep-forever, rows are small.

### 7.2 Wire the NER detector, or report zero honestly

Today's crawl spends no LLM tokens (§3.3). The cost surface is therefore
*correct but empty* until `LLMDetector` is passed into `anonymize_markdown`
through the crawler's seam (`crawler.py:614-624`), one detector per run so
`total_usage` accumulates across documents — the module's own stated intent
("a crawler logs the first per document and the second in its run report",
`src/anonymization_ner.py:582-585`).

Until that lands, the UI must say `NER detector: off (regex only) — no LLM
tokens spent`, not `$0.00`. The two are different claims and P10 is the whole
reason to care.

### 7.3 Per-document provenance — additive columns on `corpus_file_sources`

For documents that *became* files, four nullable columns on an existing PG-only
table (`migrations/versions/0075_corpus_file_sources.py`), written by
`_Ingestor.ingest` which already holds every value:

`convert_engine`, `converted_chars`, `anonymize_replaced`, `crawled_at`.

`anonymize_replaced` is a **count of substitutions**, never the entities — the
same discipline `AnonymizeResult` itself keeps (`src/anonymization.py:95-99`).
Rendering the replaced strings would rebuild the identifiers anonymization
exists to remove.

**Documents that never became files** (oversize, unconvertible, empty
conversion, fail-closed anonymization) have no row to hang anything on. They
live in `extraction_runs.skips` — run-scoped, capped, searchable by path in the
drawer. This is a real limitation and §10 names it: such a document is findable
through *the run*, not through *the collection*.

---

## 8. Surface 4 — "What happened to my document"

### 8.1 Where it lives

The existing per-file detail page, `/library/{slug}/files/{file_id}`
(`app/web/router.py:4596-4651`, `app/web/templates/library_file_detail.html`)
— which already renders on the shared detail scaffold with a rail of facts. Per
the scaffold contract, an entity-specific block goes *where it belongs in the
rail's fixed order*, not in a new position: the About prose, then the metadata
read-out, then this new **Extraction** block, then Sharing
(`design-system.md:118-151`).

**No new endpoint.** The route is server-rendered; the provenance join is added
to its context. This is the cheapest possible answer to P1.

### 8.2 Wireframe — file detail rail

```
   ┌ Extraction ─────────────────────────┐
   │ Source     Finance/Contracts/…/msa  │
   │            .pdf  ↗ open in source   │
   │ Crawled    31 Aug 09:04             │
   │ Converted  pypdfium2 · 48 210 chars │
   │            12 pages · 7 headings ·  │
   │            2 tables · 1 fallback    │
   │ Anonymized 31 Aug 09:04 · 214       │
   │            substitutions            │
   │            (entities are not stored)│
   │ Indexed    ✓ indexed · 31 Aug 09:05 │
   └─────────────────────────────────────┘
```

For a file whose ingest failed, the same block carries the `processing_detail`
text verbatim under a danger-toned `Indexed ✗ rejected` row — the field already
exists (`src/db.py:1602`) and is already the free-text reason.

### 8.3 Wireframe — the document that is *not* there

The harder half. An admin looking for a document that never arrived is on the
collection page, which has no row to click. The answer is a **filter, not a
page**: the collection detail's file list gains a `not indexed` filter and,
beneath it, a one-line pointer into the run that refused the document.

```
┌ Files in Finance/Contracts ─────────── 1 233 files ─┐
│ [ search files            ]  ( all ) ( not indexed )│
├─────────────────────────────────────────────────────┤
│ ⚠ 9 documents from the 31 Aug 09:00 run are not in  │
│   this collection: 7 over the 50 MB size cap,       │
│   2 could not be converted.   [ See the run → ]     │
├─────────────────────────────────────────────────────┤
│  msa-2024.pdf            indexed     31 Aug 09:05   │
│  …                                                  │
└─────────────────────────────────────────────────────┘
```

The banner is drawn **only when the last run recorded skips for this
collection**, and it names counts by reason. `See the run →` opens the source
card's run drawer scrolled to that run's skip list, where each entry is
`path · reason · detail`. One click, no new page, and the question "where is my
contract?" has a terminating answer rather than a silence.

---

## 9. Proposed API additions

All gated `Depends(require_admin)` — the module's whole surface already is
(`app/api/admin_sharepoint.py:4`). Read postures use the closed
`EXEMPT_REASONS` vocabulary (`src/audit_posture.py:603-611`); every new
cataloged action needs an entry in `src/audit_events.py`'s `CATALOG`.

| # | Method + path | Gate | Audit posture | Response (abridged) |
|---|---|---|---|---|
| A1 | `GET /api/admin/sharepoint/connections/{connection_id}/extraction/status` | `require_admin` | `READ_POSTURE: "exempt:noise"` — 3 s poll, no content (same class as `GET /api/jobs`, `audit_posture.py:827`) | `{running: {run_id, job_id, phase, files_done, files_seen, enumeration_done, files_per_s, started_at, checkpoint_at, http_429, throttle_wait_s, eta_s\|null}\|null, last_run: {…report summary…}\|null, month_to_date: {runs, input_tokens, output_tokens, cache_read, cache_write, bytes_downloaded, cost_usd\|null, unpriced_reason?}, priced_by: {model, source, set_at}\|null, as_of}` |
| A2 | `GET …/{connection_id}/extraction/runs?limit=10&cursor=` | `require_admin` | `READ_POSTURE: "exempt:ui_support"` | `{runs: [{id, job_id, status, started_at, finished_at, duration_s, interrupted, files_done, files_seen, bytes_downloaded, new, changed, deleted, skips_total, error}], next_cursor}` |
| A3 | `GET …/{connection_id}/extraction/runs/{run_id}` | `require_admin` | `READ_POSTURE: "exempt:ui_support"` | the full stored `report` + `usage` + `skips` (capped list plus `skips_total`, so truncation is visible), + `cost: {usd\|null, basis}` |
| A4 | `POST …/{connection_id}/extract/estimate` | `require_admin` | `POSTURE: "exempt:debug_dry_run_no_write"` — the reason already in use at `audit_posture.py:566` | `{files: int, bytes: int, oversize: {files, bytes}, delta_since: ts\|null, tokens: {input, output}\|null, cost_usd: null\|float, basis: "last_completed_run:<id>"\|"no_basis", refusal?: {error, message}}` |
| A5 | `GET …/{connection_id}/extraction/config` | `require_admin` | new cataloged action `sharepoint_connection.extraction_config_read` (`kind: "read"`) — sibling of `scopes_read` / `certificate_read` (`audit_events.py:955-975`), because it discloses credential **env-var names** and per-scope audience mapping | `{effective: [{key, value, origin: "yaml"\|"env"\|"builtin"\|"default", env_name?, editable: bool, lock_reason?}], scopes: [_scope_out(...)], vision: {model, origin, per_scope: false}}` |

**One behavioural fix, not an addition.** `_extraction_readiness()` refuses
with `extraction_producer_not_configured` whenever
`_extraction_producer_argv()` is `None` (`app/api/admin_sharepoint.py:533-542`)
— but in `mode: builtin` there is no argv by design, and the handler
deliberately does not consult one
(`kinds.py::_run_builtin_corpus_extraction` docstring). As written, **the
admin trigger and the scheduled sweep both refuse every builtin-mode
connection.** The readiness check must branch on
`_extraction_producer_mode()`, and in builtin mode assert what builtin actually
requires: at least one confirmed scope (the crawl's own precondition,
`crawler.py:1115-1119`). A4 reuses the same function, so the estimate dialog
inherits the same typed refusals with no second copy of the rules (P6).

**Deliberately not added:** any endpoint for surface 4. The file detail route
and the collection detail route render server-side and gain context, not APIs.

---

## 10. Config keys

| Key | Origin today | Read where | Editable where | Notes |
|---|---|---|---|---|
| `extraction.enabled` | `instance.yaml` / `AGNES_EXTRACTION_ENABLED` | per request (`feature_enabled`) | **nowhere** — `Switch(editable=False)` (`app/switches.py:705-732`) | `lock_reason` needs updating for builtin mode (§6.3) |
| `extraction.producer.mode` | `instance.yaml` | per job (`kinds.py::_extraction_producer_mode`) | nowhere (deploy) | `builtin` \| anything else = `external`; default keeps every existing instance unchanged |
| `extraction.producer.command` / `.module` | `instance.yaml` | per job | nowhere (deploy) | executable — the reason the section must stay unwritable (§6.3) |
| `extraction.schedule` | `instance.yaml` / `SCHEDULER_EXTRACTION_SCHEDULE` | per sweep (`admin_sharepoint.py:582-592`) | nowhere (deploy) | one instance-wide cadence applied per connection |
| `extraction.timeout_s` | `instance.yaml` | per job | nowhere (deploy) | external mode only — the builtin crawl is not a subprocess and is not killed by it; the config drawer must say so |
| `extraction.crawler.max_file_mb` | `instance.yaml`, default 50 | **per run** (`crawler.py:1077-1084`) | nowhere today; the best candidate for `extraction_runtime` (§6.3) | `0` = unlimited; drives the oversize skips |
| `extraction.model` / `corporate_memory.extraction.model` | `instance.yaml` | per detector (`anonymization_ner.py:430-454`) | `corporate_memory` **is** editable (`app/api/admin.py:529-543`) — the top-level `extraction.model` is not | the two resolve in a documented order; the drawer must show *which one won* |
| `extraction.anonymization.hmac_key_env` | `instance.yaml`, allowlisted | at run start | nowhere (deploy) | **name only** on screen, never the value (`docs/anonymization.md`) |
| `extraction.pricing.*` | **proposed** (§5.1) | per render | with the section, if `extraction_runtime` lands | absent ⇒ "not priced", never `$0.00` |
| `AGNES_VISION_MODEL` | env only (`src/ingest/vision.py:24`) | per transcription | nowhere | instance-wide; the spec's per-scope transcription control does not exist |
| per-scope `anonymize`, `audience_classes`, `access_mode` | `source_connections.config.scopes[]` | per run + per read | **the connect wizard** (`POST …/scopes`) | server-written; the generic connection editor preserves them (`SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS`, `admin_sharepoint.py:252`) |
| checkpoint granularity (200 delta rows) | code constant (`crawler.py:126`) | — | not settable | shown as `built in` so nobody hunts for the knob |

---

## 11. Implementation notes

**Templates touched (all existing):**

| File | Change |
|---|---|
| `app/web/templates/admin_data_sources.html` | `_sharepointPipelineStripHtml` (`:1890`) — live crawl cell + real cost cell; `_sharepointFactsHtml` (`:2122`) — `Run`, `Cost`, `Configuration` rows; the drawer (`:2310`) gains `runs` and `config` segments; a poll loop |
| `app/web/router.py` | `_sharepoint_pipeline_cell` (`:8324`) — read `extraction_runs` for the strip; delete `_FILE_SOURCE_QUEUE_COST_PLACEHOLDER_PER_ITEM` (`:8258`); `library_file_detail` (`:4596`) — provenance in ctx; `library_detail` (`:4661`) — skip banner + `not indexed` filter |
| `app/web/templates/library_file_detail.html` | one `detail.side_open('Extraction')` block after the metadata read-out (`:176-187`) |
| `app/api/admin_sharepoint.py` | A1–A5; mode-aware `_extraction_readiness` |
| `connectors/sharepoint/crawler.py` | checkpoint writes to `extraction_runs`; per-file provenance into `_Ingestor.ingest`; skip rows |
| `src/audit_posture.py`, `src/audit_events.py` | one `READ_POSTURE`/`POSTURE` entry per route; one new CATALOG action |

**Design-system components used — nothing hand-rolled:**

- `.ds-src` card, `.ds-pipe` strip, `.ds-src__fact` rows, `.ds-badge`,
  `.ds-filesource-drawer` — all already in `admin_data_sources.html`; the new
  rows are instances, not variants.
- `ds.segmented_strip` (`_components.html:432`) for the drawer's category
  switch — the spec's "segmented switch" (§13.2), replacing the current
  badge-as-filter buttons.
- `state.panel()` (`macros/_state.html`) for every EMPTY / FAILED case: never
  polled, never run, poll failed, PG-only refusal.
- `detail.side_open` / `detail.side_rows` / `detail.status`
  (`macros/_detail.html`) for the file-detail block.
- Status colour only from `--ds-accent-{info,warn,success,danger}-{bg,ink,line}`;
  the running pulse from `--ds-motion-*` honouring `prefers-reduced-motion`
  (`design-system.md:66-83,159-161`). No raw hex, no `var(--primary)` — both
  are contract-tested (`tests/test_design_system_contract.py`).
- The card is reached from `/admin/data-sources`, already in the admin nav
  inventory (`app/web/admin_nav.py`); no nav change, so no
  `tests/test_web_admin_nav.py` entry is needed.

**Poll vs SSE:** poll, for the cross-process reason in §4.4. One
`setInterval`, one in-flight request at a time, `visibilitychange`-gated,
3 s active / 30 s idle, exponential backoff to 60 s on error, and a hard stop
after a typed `501`.

**Guards to extend:** `tests/test_design_system_contract.py` runs on the
touched templates automatically; add route tests for A1–A5 (200 + shape),
a posture test is enforced automatically by
`tests/test_audit_route_posture.py` / `test_audit_read_posture.py` /
`test_audit_catalog.py`, and one regression test pinning that
`_extraction_readiness` accepts builtin mode with confirmed scopes and no
producer command.

---

## 12. Non-goals

1. **No monitoring page.** No `/admin/extraction`, no run-detail route, no
   dashboard. Everything is the source card, its drawer, and two existing
   Library pages (P1).
2. **No live log stream.** The producer's stdout is `DEVNULL` by deliberate
   design in external mode (`kinds.py:1695-1707`); builtin mode logs through
   the ordinary logger. Streaming logs to the browser is a different product.
3. **No cost enforcement.** No budget, no threshold, no auto-abort. The
   estimate informs; `max_file_mb` is the only guardrail, and it already exists.
4. **No verification of anonymization content.** Builtin mode lets Agnes report
   *observed* substitution counts, which is strictly more than a producer's
   self-declaration — but it is still not a check that the text is clean.
   `docs/anonymization.md`'s "Current limits" section stands and must be
   updated, not deleted, when this ships.
5. **No per-scope scan transcription control.** Named as absent (§3.3); adding
   it is its own change with its own cost surface.
6. **No cross-connection roll-up.** Cost and history are per connection. An
   instance-wide "what did extraction cost this month" view is plausible later,
   on `/admin/usage`, and is not designed here.
7. **No entity display, ever.** Anonymization surfaces counts. The replaced
   strings are not stored, not returned, not rendered.
8. **No DuckDB app-state support.** PG-only, typed `501`, consistent with the
   card's existing degradation.

---

## 13. Build order (½-day steps)

Each step is independently shippable and leaves the card honest.

| # | Step | ½-days |
|---|---|---|
| 1 | **Fix `_extraction_readiness` for builtin mode** + regression test. Without it nothing below can be triggered. | 1 |
| 2 | Alembic `0087_extraction_runs` + `src/repositories/extraction_runs_pg.py` + registry entry (PG-only) + contract test. | 1 |
| 3 | Crawler writes `extraction_runs` at its existing checkpoint and finalizes on done/interrupted/failed. Tests: a killed run leaves a row with `status=interrupted` and the counters it reached. | 1 |
| 4 | **A1 `…/extraction/status`** + posture entry + route test. | 1 |
| 5 | Card: live crawl cell, `Run` fact row, poll loop with the stale/FAILED/501 behaviours from §4.4. | 2 |
| 6 | **A2/A3 runs endpoints** + the `runs` drawer segment (history rows, outcome glyphs, skip list). | 2 |
| 7 | Per-document provenance: 4 columns on `corpus_file_sources`, written by `_Ingestor.ingest`; skip rows into `extraction_runs.skips`. | 1 |
| 8 | File-detail `Extraction` rail block + collection-detail skip banner and `not indexed` filter. | 2 |
| 9 | **A5 `…/extraction/config`** + the `config` drawer segment with origin/lock badges. | 2 |
| 10 | Wire `LLMDetector` into the crawl seam, one detector per run, `total_usage` into `extraction_runs.usage`. | 1 |
| 11 | `extraction.pricing` config + cost cell + `Cost` fact row + month-to-date, with the unpriced path rendered first. Delete the placeholder constant. | 2 |
| 12 | **A4 estimate** + the confirm step in `runSpExtraction`, including the no-basis first-run path. | 2 |
| 13 | Docs: update `docs/anonymization.md` (observed vs declared in builtin mode), `config/instance.yaml.example` (`pricing`, `producer.mode`), CHANGELOG. | 1 |

Total ≈ 19 half-days. Steps 1–5 alone answer "what is running right now" and
are the smallest useful slice.

---

## 14. Open questions

1. **Is `interrupted` resumable in practice, and does the UI get to say so?**
   The cTag map makes a resumed run skip already-ingested documents
   (`crawler.py:894-897`), and the cTag is written only *after* durable ingest
   (`:966-969`) — so the claim is sound per document. It is *not* verified
   across a `deltaLink` that expired mid-run (410 → full resync,
   `crawler.py:1004-1016`). Either we test that path and let the row say
   "resumed, 0 re-downloaded", or the row says "resumed" without the count.
2. **Whose price, and for how long?** `extraction.pricing` is per model id, but
   the detector may run against a direct API or through a managed
   cloud endpoint (`anonymization_ner.py:476-503`), where SKUs and rates differ,
   and cache reads/writes are priced separately again. One flat per-model rate
   may be too coarse to be trusted — in which case the honest fallback is
   tokens-only, and the `$` the spec asks for (§13.2) does not ship.
3. **Where does a refused document live?** A file that was skipped has no
   `corpus_files` row, so it is discoverable only through a run
   (§7.3/§8.3). The alternative — materialising a `rejected` row in the
   collection — makes the document findable where people look for it, at the
   cost of putting non-documents in a collection listing, in the counts, and
   in every grant-scoped query that touches it. This is the one decision in
   this design that a wrong choice makes expensive to reverse.
