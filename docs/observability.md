# Observability — logs, cost, and metrics

## Audit & activity trails (retention status)

Agnes keeps eight distinct records of "who did what": `audit_log` (admin/API
actions), chat transcripts (`chat_messages` — flag-controlled, see below),
CLI session JSONLs (viewer: `/admin/sessions`), usage rollups
(`usage_events`, viewer: `/admin/telemetry`), `sync_history`, `llm_usage`
(per-call agent token accounting), agent-runtime forensics
(`agent_scope_snapshots`), and `llm_calls` (the LLM observability ledger —
one priced row per LLM call across every workload, see *LLM call ledger*
below).

Chat transcripts used to have no admin viewer at all (a deliberate privacy
decision). Since the audit-full-coverage plan's F4 bridge, a chat session is
materialized into the SAME jsonl shape and `SESSION_DATA_DIR` layout a
CLI/analyst session already uses (`app/chat/session_export.py`), on session
end (archive/delete/idle-reaper kill) and via a bounded pipeline sweep for
still-active sessions — so it becomes visible at `/admin/sessions` and feeds
the usage rollups exactly like a CLI session. Controlled by
`sessions.include_chat` (`config/instance.yaml.example`, env override
`AGNES_SESSIONS_INCLUDE_CHAT`) — **on by default**; set it `false` to
restore the old no-materialized-copy behavior (`chat_messages` stays the
only copy, no admin transcript viewer).

`/admin/activity` (`GET /api/admin/activity`, `agnes admin activity`, and the
`activity` MCP tool) is a **unified, read-side projection** over the first
four of those — `audit_log` + `sync_history` + `llm_usage` +
`agent_scope_snapshots` — UNION'd on read (no new table, no migration) into
one chronological timeline. Each row carries a `trail` field naming its
source table (`audit` | `sync` | `llm` | `agent_scope`) so an admin can still
narrow to one trail (`?trail=audit`); the projection reuses the SAME
filter/facet/cursor machinery `audit_log`-only queries always used
(`AUDIT_SOURCE_CASE_SQL`, `RESULT_CLASS_CASE_SQL`), implemented in lockstep
on both backends (`AuditRepository.query_unified` /
`AuditPgRepository.query_unified` in `src/repositories/audit.py` /
`audit_pg.py`). The page's KPI cards and facet dropdowns
(`GET /api/admin/observability/kpis` + `/facets`, backed by
`AuditRepository.kpis`/`facets`) read the SAME union and accept the SAME
`trail=` filter as the timeline — the cards, dropdowns and table can never
tell different stories for one filter state. **`chat_messages` is never
part of this union** — it is not one of the four SELECTs the projection
UNIONs, and that is unaffected by the F4 export above: an exported chat
transcript is browsable transcript-by-transcript at `/admin/sessions` (the
same surface CLI sessions use), never folded into this KPI/facet timeline.

Retention, per trail:

| Trail | Config key | Default | Scheduler job |
|---|---|---|---|
| `audit_log` | `audit.retention_days` | 365 days | daily `audit-prune` |
| `sync_history` | `retention.sync_history_days` | 0 = forever | daily `retention-prune` |
| `llm_usage` | `retention.llm_usage_days` | 0 = forever | daily `retention-prune` |
| `llm_calls` | `retention.llm_calls_days` | 0 = forever | daily `retention-prune` |
| `agent_scope_snapshots` | `retention.agent_scope_snapshots_days` | 0 = forever | daily `retention-prune` |
| `usage_events` | `retention.usage_events_days` (or `USAGE_EVENTS_RETENTION_DAYS` env, which wins when set) | 0 = forever | own job, `POST /api/admin/usage/prune` (`agnes admin usage prune`) |
| `chat_messages` | — | no policy | — (privacy decision, deliberately out of scope) |
| CLI session JSONLs | — | no policy | — (filesystem, out of scope for DB retention) |
| Exported chat-session JSONLs | `sessions.include_chat` (default: on) | no policy, same as CLI session JSONLs above | — (filesystem, out of scope for DB retention; viewer: `/admin/sessions`) |

`sync_state` (current per-table sync status) and the live `agents` table are
never touched by any prune above — only the trail tables themselves
(`sync_history`, `agent_scope_snapshots`) age out. Every non-`audit_log`
window defaults to `0` = keep forever, so a freshly-installed instance prunes
nothing until an admin opts in. See `src/audit_retention.py` for the
dispatcher and `config/instance.yaml.example` for the full `retention:`
block.

### Self-service usage data — the `agnes-usage` package

Those trails are also queryable as ordinary tables, so a user can analyze
their own usage with `agnes query` instead of an admin page:

| Table | Source | Row scope |
|---|---|---|
| `agnes_sessions` | `usage_session_summary` | own rows (non-admin) / all (admin) |
| `agnes_telemetry` | `usage_events` | own rows (non-admin) / all (admin) |
| `agnes_audit` | `audit_log` | own rows (non-admin) / all (admin) |
| `agnes_turns` | `usage_turns` | own rows (non-admin) / all (admin) |
| `agnes_llm_calls` | `llm_calls` | own rows (non-admin) / all (admin) |
| `agnes_extraction_runs` | `extraction_runs` | **admin-only** — zero rows for any non-admin |
| `agnes_facts_ingest_runs` | `facts_ingest_runs` | **admin-only** — zero rows for any non-admin |

`agnes_turns` (token usage per assistant turn, including prompt-cache reads
and writes, across Claude Code and every chat surface), `agnes_llm_calls`
(one row per LLM call across every workload — chat, agent API, builders,
extraction, corporate memory and the rest — priced at write time, see *LLM
call ledger* below), `agnes_extraction_runs` (one row per built-in
extraction/crawl run) and `agnes_facts_ingest_runs` (one row per
fact-graph ingest batch) exist **only on Postgres-backed instances** — their
source tables have no DuckDB counterpart. On a DuckDB-backed instance none of
the four ids is registered at all: they never appear in `agnes catalog`, and
a `SELECT` against any of them says the table is unavailable here rather
than pointing at a package grant that could not surface it.

The first five tables are filtered to the caller's own rows (admins see
everyone). `agnes_extraction_runs` and `agnes_facts_ingest_runs` are
**admin/operator data, not per-user data**: a crawl run belongs to a
data-source connection and an ingest batch belongs to a set of collections —
neither belongs to a person, so there is no "own rows" for a non-admin to
see. Granting the package still decides whether the TABLE is visible
(`agnes catalog` lists it, a query gets past the 403), but every non-admin
caller's query against either one succeeds with zero rows, never someone
else's data and never a wider grant than the four own-rows tables get. This
is what makes the real LLM spend ledger (`agnes_extraction_runs.usage`,
`agnes_facts_ingest_runs.llm_usage`) safely queryable at all: an admin can
ask `agnes query "SELECT json_extract(llm_usage, '$.input_tokens') FROM
agnes_facts_ingest_runs"` instead of trusting one dashboard's arithmetic,
without opening that ledger to every grantee of the package.

They are server-side only — `agnes pull` never downloads them — and they are
**members of a seeded data package with the slug `agnes-usage`**, so who may
query usage data at all is an admin decision like any other table grant.
Grant the package to a group (`/admin/access`, or `agnes admin grant create
<group> data_package <pkg-id>`) and its members can read the five own-rows
tables, still filtered to their own rows; without the grant every table in
the package is absent from `agnes catalog` and a `SELECT` against any of them
returns 403 naming the package. Admins are unaffected (god-mode) and keep the
unscoped view on all seven tables. Agents and co-sessions also keep access
without the grant — their authority is already owner grants ∩ scope. Full
model: [`RBAC.md`](RBAC.md#internal-usage-tables-the-agnes-usage-package).

**Operator step after upgrading** to the release that introduced this
(previously every authenticated user had implicit access): grant
`agnes-usage` to the groups that should keep it — granting it to `Everyone`
restores the previous behaviour exactly, since the per-row filter was and
remains what separates one user's rows from another's.

## Chat cost — measured, not modelled

```bash
agnes admin usage chat-cost                      # last 7 days
agnes admin usage chat-cost --window 30d --json
agnes admin usage chat-cost --user someone@example.com
```

Mirrors `GET /api/admin/telemetry/chat-cost` (admin-only). One row per
`(session, model)` with uncached input, output, cache reads and cache writes
reported **separately**, priced by `src/llm_pricing.py` at the rates of the
model that session actually ran on.

Why it is split that way: prompt caching is the largest single lever on the
cost of a long agent session. A cached read costs ~0.1x the input rate and a
cache write ~1.25x, so a workload built around one large stable prefix and
many short turns — which is what every agent surface here is — is cheap in
reality and looks expensive in any cost model that charges those re-reads at
the full input rate. That error is roughly 10x on the dominant term, which is
more than enough to reverse a conclusion. Read the numbers instead:
`cached_input_share` says how much of everything the model read came from
cache.

Two honesty markers, both load-bearing:

- **`cache_accounting`** per row is `recorded`, `partial`, or `unavailable`.
  Rows written before the prompt-cache columns existed carry no figures at
  all; their cached tokens are *unknown*, not zero, and their `cost_usd` is
  a floor. A cache-blind zero read as a measurement is exactly the mistake
  this endpoint exists to prevent.
- **`priced_as`** per row states the four rates used, so any figure here can
  be re-derived rather than taken on trust.

The prompt-cache columns are Postgres-only (`migrations/versions/0092_*`, A3
freeze), so on the frozen DuckDB app-state backend this route answers a typed
`501 requires_postgres_backend` rather than serving zeros.

Note the separate, deliberately coarser surface: the daily spend cap
(`chat.daily_anthropic_spend_usd`) prices the day's tokens at the most
expensive general-purpose tier because it reads a two-bucket counter with no
model attached — a guardrail that must guess should guess in the direction
that stops sooner. It is a soft guardrail, not a billing ledger; this
endpoint is the ledger.

This endpoint stays the per-session chat view. For the cross-workload one —
chat AND every server-side generation, priced the same way — see `llm-cost`
in *LLM call ledger* below.

## LLM call ledger — every call, priced once

`llm_calls` (Postgres-only, migration `0115_llm_observability`, A3 ratchet —
see *Dual-backend discipline* in `CLAUDE.md`) is the single place "an LLM
call happened" is recorded. The chat broker writes one row per forwarded
completion, and `trace_generation` (`src/observability/llm_tracing.py`)
writes one per server-side generation — the same call sites the OTel spans
above come from, wrapped at every one of: the five builder endpoints
(`entity_builder`, `agent_builder`, `mcp_builder`, `package_builder`,
`semantic_model_builder`), document fact extraction (including one row per
document result fetched back from a batch job), OCR, vision captioning, the
NER anonymizer, auto-title, chat readiness probes, corporate memory,
knowledge digests, table autodoc, ontology, the memory-curator profile,
session verification and the store guardrails — not only the two
`connectors/llm/` providers the older `llm_usage` ledger saw.

Each row carries the call's **context**
(`src/observability/llm_context.py`) — `workload` (the coarse kind of work:
`chat`, `agent_api`, `builder`, `extraction`, `corporate_memory`,
`knowledge`, `semantic_layer`, `anonymization`, `ocr`, `vision`,
`auto_title`, `readiness`, `store_guardrails`, `verification`, `admin_ask`),
`purpose` (a finer call-site label, e.g. `entity_builder_turn`,
`facts_extraction`, `digest`), `session_id`/`turn_id` for a chat call,
`user_id`/`agent_id` for identity, `job_id` when the call ran inside a
worker job, and `subject_id` — what the call is *about* (an entity id for a
builder, a document id for extraction, a session id for auto-title) — plus
the four token kinds, `cost_usd`, and `priced_as`: the rates the row was priced at
(`src/llm_pricing.py`), stored beside the figure so any row can be
re-derived rather than taken on trust. An unknown model prices at the same
`DEFAULT_PRICE` every other surface uses, and `priced_as` says so.

### Reading it

```bash
agnes admin usage llm-cost                          # last 7 days, by workload
agnes admin usage llm-cost --by model --window 30d --json
agnes admin usage llm-calls --turn-id <turn-id>      # every call one turn made
agnes admin usage feedback --verdict down            # the thumbs-down queue
```

- `GET /api/admin/telemetry/llm-cost?window=1d|7d|30d|all&by=workload|agent|user|model|purpose`
  (`agnes admin usage llm-cost`) — grouped totals: calls, the four token
  kinds, `cost_usd`, `cached_input_share`, and `priced_models` (which
  model(s) the group's rows were actually priced at, so a figure is never
  taken on trust).
- `GET /api/admin/telemetry/llm-calls?session_id=|turn_id=|job_id=|user_id=&limit=&before=&before_id=`
  (`agnes admin usage llm-calls`) — the detail rows for one turn, session,
  job or user, newest first, cursor by `created_at`. At least one of the
  four ids is required — this is a drill-down into one unit of work, never
  an unbounded dump of every call the instance ever made.
- `GET /api/admin/telemetry/feedback?window=&verdict=up|down` (`agnes admin
  usage feedback`) — the chat-turn thumbs queue, see *Feedback* below.
- `/admin/telemetry` has an "LLM cost" section reading `llm-cost` by
  workload with a window selector, and the `agnes_llm_calls` table in the
  `agnes-usage` data package (own rows for a non-admin grantee, everything
  for an admin) reads the same ledger as an ordinary `agnes query` table.

All three read routes are admin-gated and resolve the ledger repository as a
FastAPI dependency, so a DuckDB-backed instance answers the typed `501
requires_postgres_backend` before any query parameter is even validated.

### One turn id, three tables

`chat_messages.turn_id`, `usage_turns.turn_uuid` and `llm_calls.turn_id`
carry the SAME id — the one ChatManager mints per delivered user message
(see *OpenTelemetry export* → *What is exported* below) — so the transcript,
the per-turn token table and the call ledger always agree on what one turn
was.

### Feedback

`POST /api/chat/sessions/{chat_id}/feedback` (`{turn_id, verdict: "up"|
"down", comment?}`, gated like the session's other routes — owner or a live
participant — and only for a turn the session's own messages carry, so a
caller cannot key feedback on another session's turn) writes one
`chat_message_feedback` row per `(turn_id,
user_id)` — a second submit updates it rather than piling up a second
opinion. Audited as `chat.feedback` (session, turn, verdict — never the
comment). The web chat renders thumbs on every completed assistant bubble
(the frame carries `turn_id`) and a short optional comment on thumbs-down;
read the queue with `agnes admin usage feedback` (never prints the comment
text in its table — use `--json` for that).

### Extraction provenance

"Why did extraction pull the wrong facts" is answered by the `llm_calls`
rows with `workload=extraction`, `purpose=facts_extraction|facts_retry|
facts_batch`, `subject_id` (the document's own `corpus_files.id`) and
`job_id` — the worker job id `app/worker/runtime.py` binds onto the
context for the whole job, the same id `jobs.id` and `extraction_runs.
job_id` carry. Join on `extraction_runs`, not `facts_ingest_runs` — the
latter has no `job_id` column at all (`facts_ingest_runs` is keyed on the
ingest run's own id, a different unit of work than the worker job that
drove it). One `llm_calls` row per document call, so a wrong fact traces
back to the exact call, its model, its cost and, under policy, its prompt.

### Memory provenance

`agent_memories` gains `source_turn_id`/`source_message_id` (Postgres-only;
the DuckDB sibling accepts and drops them, the same pattern the
`chat_messages` cache-token columns already established) — `remember` fills
them from the live turn record at write time, so "why did this memory get
written" traces back to the exact turn and message that caused it.

The turn record `chat:turn:{session_id}` is never deleted at turn end, only
re-published with `ended_at` set, so it always answers with the session's
LAST turn — open or closed, however old. A write that happens outside any
live turn (an owner note through the API, a curator job) must not borrow
that last turn's identity just because one is still there to read:
`remember` stamps provenance only from a record that is BOTH still open
(`ended_at is None`) AND not newer than the write itself (the same
`started_at` freshness rule the broker applies to a late completion — see
*What is exported* below). A closed turn, a turn that started after the
write began, or a legacy record published before `ended_at` existed (no
such key at all, read as "unknown", never as "open") all get no provenance
rather than a wrong one.

### DuckDB-backed instances

`llm_calls` and `chat_message_feedback` are Postgres-only by construction
(A3 ratchet): on the frozen DuckDB app-state backend the three read routes
above and the feedback endpoint answer a typed `501
requires_postgres_backend`, spans are unaffected, and the ledger WRITES from
the broker and `trace_generation` are silent no-ops rather than a crash — a
measurement must never cost the call it observes.

### Retention

`retention.llm_calls_days` (default `0` = forever), pruned by the same
daily `retention-prune` job as the other trails above.

## Knowledge packaging — worker job, single-run, checkpointed

```bash
agnes admin knowledge packaging run                # enqueue a pass
agnes admin knowledge packaging status              # last run, running?, next due
agnes admin knowledge packaging status --json
```

Per-collection `knowledge.duckdb` artifacts (K3, #798) are rebuilt by the
`knowledge-packaging` worker job kind (LIGHT lane,
`app/worker/kinds.py::_run_knowledge_packaging`), not inline inside an HTTP
request. TCRD-296 synthesis C.15: it used to run synchronously behind
`POST /api/admin/run-knowledge-packaging`, bounded only by the scheduler's
own client timeout — a pass slower than that timeout let the next scheduler
tick fire a second, overlapping call, and two overlapping in-process runs
raced hard enough to OOM the app.

**Single-run.** The scheduler's tick (`SCHEDULER_KNOWLEDGE_PACKAGING_INTERVAL`,
default 15 min) still calls `POST /api/admin/run-knowledge-packaging`, but the
endpoint is now a thin, idempotency-keyed enqueue: a second tick while one run
is still `queued`/`running` gets back the SAME job id as a `409` (expected
under a fast cadence, not an error to page on) instead of starting a redundant
run. Belt-and-braces on top of that dedupe, the job handler also takes a
non-blocking Postgres advisory lock (`src.db_pg.knowledge_packaging_lease`,
no-op on the frozen DuckDB app-state backend, which is single-process by
construction) before running — a stray manual `POST /api/jobs` enqueue with a
different idempotency key skips cleanly instead of racing the in-flight run.

**Bounded and resumable.** One run is capped at a 20-minute wall-clock budget
(`_DEFAULT_KNOWLEDGE_PACKAGING_TIMEOUT_S`, a plain constant — the incident was
an *unbounded* run, not a mistuned number). `run_packaging_pass` checkpoints
`state.json` after every collection it finishes, so hitting the deadline
mid-sweep loses progress on at most the ONE collection in flight; the result
carries `interrupted_reason: "timeout"` and the next scheduled run picks up
where it left off (an already-recorded, unchanged fingerprint is a skip, not a
rebuild). Reads are bounded too: `build_artifact`/`corpus_fingerprint` page
through a corpus's chunks (`CorpusChunksRepository.list_for_corpus_batch`,
keyset-paginated by id) rather than materializing the whole corpus's rows —
including every 384-dim embedding — in one call.

**No worker role, no silent black hole.** `POST /api/admin/run-knowledge-packaging`
checks `role_enabled(Role.WORKER)` before enqueueing — a process/instance with
no worker role has no loop that will ever claim the job, and enqueueing anyway
would leave it `queued` forever with no visible error. That case answers a
typed `501` (`{"error": "requires_worker_role"}`) instead.

`GET /api/admin/knowledge-packaging/status` (`agnes admin knowledge packaging
status`) reports the last run's outcome — including
`built`/`skipped`/`pruned`/`errors`/`interrupted_reason`/`duration_s`/
`collections_total`/`collections_processed` — whether one is running right
now, and a best-effort `next_due` estimate read from the scheduler's own
durable last-run marker.

## Audit log volume — how much does audit logging cost you

The audit-coverage work (wave 1 + wave 2 of the audit-full-coverage plan)
moved `audit_log` from "whatever handlers happened to write" to "every
mutating route writes its declared action, and every sensitive read does
too" — a route with no explicit `log_safe()` call gets a row from
`AuditFallbackMiddleware` instead of writing nothing. That is a real
increase in write volume on a busy instance, so two things exist to make it
measurable and controllable rather than assumed:

- **`scripts/audit_volume_estimate.py`** — reads the last N days of
  `audit_log` (default 7) and reports rows/day overall, the top actions by
  volume, a projection of row count (and an approximate size, from sampled
  row byte sizes) at the configured `audit.retention_days`, and flags any
  single action responsible for more than 25% of rows. Run it against any
  instance's own `DATA_DIR` (or `DATABASE_URL` for a Postgres-backed one):

  ```bash
  DATA_DIR=/path/to/data .venv/bin/python -m scripts.audit_volume_estimate
  DATA_DIR=/path/to/data .venv/bin/python -m scripts.audit_volume_estimate --days 30 --limit 10 --json
  ```

- **`audit.sampling`** (`config/instance.yaml.example`) + `src/audit_helpers
  .should_sample(action)` — an opt-in, per-action sampling ratio. Nothing is
  sampled by default; an operator who sees one action dominating the report
  above can configure e.g. `audit.sampling: {chat.tool_call: 0.1}` to keep 1
  in 10 rows for that action only. Sampling is **deterministic** (a
  per-action call counter, not `random`) so "1 in 10" is exactly true even
  on a low-traffic instance, and a security-relevant action (auth,
  RBAC/grant changes, secret rotation, admin configuration) must never be
  listed there — see the caveat next to the worked example in
  `config/instance.yaml.example`.

**Measured example.** The numbers below are a real run of
`scripts.audit_volume_estimate`, not an estimate written by hand — but the
source is a **local, synthetic seed**, not production traffic. Instance
shape: a fresh local DuckDB `system.duckdb`, seeded with a 14-day, ~150
rows/day mix modeling a small (~10-person) analyst team already covered by
wave 1's declared-action middleware — chat/MCP tool calls weighted
heaviest, catalog/query reads next, scheduler ticks and admin mutations
rarest (`retention_days` left at the default 365):

```
Audit volume report — backend=duckdb window=7d since=2026-08-22T20:34:08Z
  events_total=1054  rows_per_day=150.6
  retention_days=365  projected_rows_at_retention=54969
  projected size at retention: 17.0 MB (avg 323.4 bytes/row, sampled)
  ! dominant action: chat.tool_call = 27.9% of rows (>25%)

  action                                        count      pct
  chat.tool_call                                  294    27.9%
  mcp.tool_call                                   189    17.9%
  chat.question_answer                            147    14.0%
  query.local                                     112    10.6%
  catalog.list                                     77     7.3%
  activity.read                                    63     6.0%
  catalog.sample                                   42     4.0%
  catalog.schema                                   35     3.3%
  login_success                                    28     2.7%
  data.download                                    21     2.0%
  (10 more, each < 1%)
```

At this rate a year of retention projects to ~55k rows / ~17 MB — trivial
for either backend. What actually drives the total is chat/MCP tool-call
volume, not the wave-1/2 declared-action or read-ratchet middleware itself:
those add at most one row per otherwise-unaudited request, and most
instances' request volume is dominated by chat/agent tool calls that were
already audited before this work. A real production instance's actual
numbers will differ with its real traffic mix — run the script against your
own `DATA_DIR` rather than relying on the figures above; they exist to show
the methodology and a plausible order of magnitude, not to stand in for a
measurement of your instance.

Agnes writes one JSON object per log line and nothing else. There is no
telemetry sink to configure, no key to set, and no data leaving the host on
Agnes's account: whatever already collects the container's stdout — a
platform log service, a sidecar collector, `docker logs` — is the whole
pipeline.

## Structured logs

In production (`DEBUG` unset) every process installs a JSON formatter
(`app/logging_config.py`). One record per line:

| field | what it carries |
|---|---|
| `severity` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `message` | the formatted log message |
| `time` | UTC ISO-8601 |
| `logger` | the module's logger name |
| `service` | `app`, `scheduler`, … — which process wrote it |
| `env` | `AGNES_DEPLOYMENT_ENV`, else `RELEASE_CHANNEL`, else `unknown` |
| `replica` | `hostname:pid`, for correlating across replicas |
| `request_id` | present when the line was written inside a request |
| `exc` | the formatted traceback, when the record carries one |

The names are the ones a collector looks for when deciding whether a line is
a structured entry or just text. Under this project's older `lvl`/`msg`/`ts`
every line arrived at its collector's default severity with the payload as
one opaque string — filterable by substring only, which is the same as
having no levels at all.

Anything a caller passes as `extra={...}` is promoted to a real field
alongside these, so numbers stay filterable instead of being formatted into
the message. Core fields win a name collision: an `extra` cannot relabel its
own line's `severity` or `service`.

In development (`DEBUG=1`) the same records render through `rich` with
colour and tracebacks instead.

### What is emitted, and from where

- **Unhandled exceptions** — the FastAPI 500 handler logs the exception with
  the request's method, path and `request_id`. Rebuild failures
  (`src/orchestrator.py`) and HTTP-job failures (`services/scheduler/`) log
  as `event: component_error` with the component named.
- **LLM calls** — every Anthropic / OpenAI-compat generation emits one
  `event: llm_generation` record (`src/observability/llm_tracing.py`) with
  `provider`, `model`, `latency_ms`, `input_tokens`, `output_tokens`,
  `prompt_chars`, `completion_chars` and `is_error`. Prompts and completions
  are **never** recorded — in this product they routinely carry customer
  data, and a log pipeline is the wrong place to hold it. Their sizes are,
  because a size is the part that explains a cost or a latency. For per-turn
  cost as a measurement rather than a sample, use *Chat cost* above.

### Splitting traffic by environment

Set `AGNES_DEPLOYMENT_ENV` per deployment (`production`, `staging`, a
developer's name) and filter on `env`. It falls back to `RELEASE_CHANNEL`
and finally to the literal `unknown` — never absent, so `env != "production"`
keeps matching a deployment that forgot to label itself. The same variable
is what the host-side operator scripts (`agnes-watchdog.sh`,
`agnes-db-backup.sh`) read to tag their alerts.

### Shipping the logs somewhere

Nothing in Agnes decides this — it writes to stdout and stops. On the
GCE-hosted deployments the Terraform module wires it up, to one of two
destinations (a VM has exactly one, because Docker allows one log driver per
container): Google Cloud Logging, see [`gcp-logging.md`](gcp-logging.md), or
Datadog, see [`datadog-logging.md`](datadog-logging.md).

### Verifying the wiring

With `DEBUG=1`, `GET /api/debug/throw?kind=ValueError&msg=hello` raises after
authentication resolves, so you can confirm a real unhandled exception
reaches your collector with the request context attached. It returns 404
whenever `DEBUG` is unset.


## Prometheus `/metrics`

Every role process (api/gateway/worker; `all` in a single-process deployment)
exposes its own `GET /metrics` (`app/observability/metrics.py`) in the
standard Prometheus text exposition format. **Unauthenticated, internal-scrape-only** —
the same posture as `/healthz`/`/readyz` (see `app/api/health_probes.py`):
no auth dependency, deliberately outside `/api/*`. **Operators must not
expose this endpoint publicly** — put it behind the same
TLS-terminating-reverse-proxy / firewall boundary that keeps `/healthz` and
`/readyz` internal, and scrape it from inside that boundary only.

Every series carries `role` (the process's active `AGNES_ROLE`, or `all`)
and `replica` (`hostname:pid`) labels so a scrape can attribute a sample to
the process that emitted it. Whether it is then correct to `sum()` a metric
across replicas or take its `max()` depends on whether the underlying value
is genuinely per-replica state or a global value every replica happens to
sample identically — see the table below.

### Key series

| Series | Kind | Labels (beyond `role`/`replica`) | Scope | Correct cross-replica aggregation |
|---|---|---|---|---|
| `agnes_http_requests_total` | Counter | `method`, `path_template`, `status` | Per-replica | `sum() by (...)` — additive. |
| `agnes_http_request_duration_seconds` | Histogram | `method`, `path_template` | Per-replica | `sum() by (...)` (rate/histogram_quantile as usual) — additive. |
| `agnes_jobs_queued` | Gauge | `kind` | **Global.** Sampled at scrape time from the shared job queue — every replica reports the same value each scrape. | `max() by (kind)` — `sum()` N-counts the true depth by however many replicas were scraped. |
| `agnes_jobs_queued_capped` | Gauge | — | **Global.** 1 if the last scrape hit the bounded-scan cap (see the metric's own help text), else 0. | `max() by (...)` — same reasoning as above. |
| `agnes_jobs_oldest_queued_age_seconds` | Gauge | `kind` | **Global.** `now - min(created_at)` by kind, sampled at scrape time from the SAME bounded scan `agnes_jobs_queued` uses (no second DB hit) — every replica reports the same value each scrape. When `agnes_jobs_queued_capped=1`, the scan (ordered `created_at DESC`) may have scanned the true oldest row(s) out of its window, so this value can UNDERSTATE the real oldest-queued age — uncapped, it's exact. Absent for a kind with no queued jobs (same as `agnes_jobs_queued`). | `max() by (kind)` — same reasoning as `agnes_jobs_queued`; never `sum()`, which is meaningless for an age value and N-counts the same reading by however many replicas were scraped. |
| `agnes_jobs_running` | Gauge | `kind`, `lane` | **Per-replica.** In-process count of that replica's own currently-executing jobs. | `sum() by (kind)` — genuinely additive across the fleet. |
| `agnes_job_duration_seconds` | Histogram | `kind`, `outcome` | Per-replica | `sum() by (...)` — additive. Explicit buckets from 1s to 4h — Agnes jobs range from sub-second housekeeping to multi-hour BigQuery materializations, so the `prometheus_client` default buckets (top bucket ~10s) would collapse almost everything into `+Inf`. |
| `agnes_job_claims_total` | Counter | `kind` | Per-replica | `sum() by (kind)` — additive. |
| `agnes_job_failures_total` | Counter | `kind`, `reason` | Per-replica | `sum() by (kind)` — additive. |
| `agnes_worker_lane_active` | Gauge | `lane` | **Per-replica.** In-process count of busy concurrency slots in a lane. | `sum() by (lane)` — genuinely additive across the fleet. |
| `agnes_coordination_up` | Gauge | — | **Per-replica.** 1 if that replica's `coordination().ping()` succeeded at scrape time, else 0. | Don't sum — a fleet-wide health view wants `min()` (any replica down flags the fleet) or per-`replica` alerting, not a total. |
| `agnes_coordination_backend_info` | Info | `backend` (`memory`\|`redis`) | Per-replica (identical across a healthy fleet) | Informational only — join against other series by label, don't aggregate. |
| `agnes_readiness` | Gauge | — | **Per-replica.** Same value that replica's own `/readyz` reports. | `min()` for fleet-wide readiness (any not-ready replica should surface), or alert per-`replica`. |
| `agnes_metrics_collector_errors_total` | Counter | `collector` | Per-replica | `sum() by (collector)` — additive; nonzero means a scrape-time collector (`jobs_queued`, `coordination`, `coordination_backend`, `readiness`) swallowed an exception instead of reporting — investigate, don't ignore. |

Request-id → job log correlation (not a Prometheus series, but part of the
same observability wave): `app/job_correlation.py` stamps the originating
HTTP request's `request_id` onto a job payload at enqueue time
(`POST /api/jobs`, the sync-trigger endpoint, and the Jira webhook's
incremental-transform follow-up) and re-binds it into
`app.logging_config.request_id_var` for the duration of the worker's
handler invocation — every JSON log line emitted while that job runs
carries the `request_id` of the request that enqueued it, so a support
ticket's request id greps straight through to the async job that serviced
it, not just the synchronous response.

### Scrape config

The `mtier` Compose profile (`docker-compose.mtier.yml`) ships a
`prometheus` service (`deploy/prometheus/prometheus.yml`, 15s scrape
interval) that polls all four role containers by Compose DNS name
(`api1`/`api2`/`gateway`/`worker:8000/metrics`) plus a `cadvisor` service
(`gcr.io/cadvisor/cadvisor`, container-level cpu/mem/network metrics,
`cadvisor:8080`) — see [`DEPLOYMENT.md`](DEPLOYMENT.md) → *Multi-process* →
*Metrics (Prometheus)* for how to bring it up and the macOS Docker Desktop
caveat on cAdvisor's fidelity. A production deployment that doesn't use
this profile should scrape the same `/metrics` path on whatever ports each
role's `/healthz`/`/readyz` already answer on, at a similar interval.

## OpenTelemetry export — opt-in, one span per LLM completion

Agnes can ship traces to any OTLP/HTTP collector. It is off until the
standard variables are set on the process (every process: the app, the
scheduler, a collector — they share one logging entrypoint and that is where
the exporter is installed):

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://<collector>/<base-path>   # the SDK appends /v1/traces
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<token>    # whatever the collector wants
# Prompt/completion TEXT export is a separate decision — see *Content policy*
# below. It is governed by `observability.content_export` in instance.yaml,
# never by an environment variable; AGNES_OTEL_CAPTURE_CONTENT is a
# deprecated alias that no longer enables anything on its own.
```

On a VM built by the `customer-instance` Terraform module the endpoint and
headers come from the per-instance `otlp_endpoint` and `otlp_headers_secret`
fields (a Secret Manager secret name — the value is fetched at boot, never
stored in state); the module also writes `AGNES_DEPLOYMENT_ENV` for every VM
(the VM's name unless `deployment_env` says otherwise). The module's
`otlp_capture_content` boolean no longer enables content export on its own —
carrying the `observability.content_export` policy record instead is a
separate module change, out of scope here. Like everything the startup
script renders, these reach a running VM only through a recreate.

An unset endpoint leaves the OpenTelemetry API's no-op tracer in place: no
exporter, no background thread, a dictionary lookup per call. The log line
`otel: OTLP trace export enabled` at startup says it is on; a collector
that refuses the batches shows up as the SDK's own
`Failed to export span batch` warnings.

### What is exported

- **One span per LLM completion that transits the chat broker**
  (`app/api/broker.py`) — every chat surface and every engine, because all of
  a session's LLM traffic goes through that one route. Named `chat <model>`,
  kind `CLIENT`, with the duration of the upstream call.
- **One span per server-side generation** wrapped in `trace_generation`
  (`src/observability/llm_tracing.py`) — every call site in *LLM call
  ledger* above (builders, extraction, OCR, vision, anonymization,
  auto-title, readiness, corporate memory, digests, autodoc, ontology,
  verification, store guardrails) — the same tracer, the same table.
- **One `agnes.chat.turn` span per delivered user message** (kind
  `INTERNAL`, opened by ChatManager) — `agnes.session_id`, `agnes.turn_id`,
  `agnes.user_id`, `agnes.agent_id`, `agnes.surface`, `agnes.workload`
  (`chat` or `agent_api`) — no message text. Closed with `agnes.tool_calls`
  and the turn's drained token totals and `agnes.cost_usd`. One
  `agnes.chat.tool <tool>` child span per tool call under it (`agnes.tool`,
  `agnes.args_hash` — the same digest the `chat.tool_call` audit row
  carries, `agnes.is_error`), ended by the matching `tool_result` — never
  the arguments or the result.
- **Completion spans open as children of the turn.** The broker reads
  `chat:turn:{session_id}` — a coordination record with a 24 h TTL that is
  never deleted at turn end, only re-published (with `ended_at` set) when
  the turn closes and overwritten outright by the next turn — and opens its
  completion span as a child of the stored context, even when the turn and
  the completion run in different replicas (the collector stitches the two
  on `trace_id`). A record whose own `started_at` is AFTER this completion
  began is refused rather than attributed — a co-driver's message can start
  turn N+1 before turn N's completion returns, and the session's last turn
  is not necessarily the turn that made this call. With the coordination
  backend unavailable, or with no usable record, the completion span is a
  root span instead, `turn_id` is null on it, and everything else records
  as usual — degrade, never fail.
- **One `agnes.chat.feedback` span per thumbs submission**, carrying one
  event `agnes.feedback` (`agnes.verdict`, `agnes.has_comment`), parented
  under the turn's own span context when it is still the turn the feedback
  is about.

| attribute | on | carries |
|---|---|---|
| `gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.model` | both | provider (`anthropic`, `gcp.vertex_ai`) and models |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | both | uncached input and output |
| `gen_ai.usage.cache_read_input_tokens`, `gen_ai.usage.cache_creation_input_tokens` | both | prompt-cache reads and writes, **separately** — folding them into `input_tokens` undercounts an agentic run by orders of magnitude (see *Chat cost*); generation spans carry these now too, not only the broker's |
| `gen_ai.response.finish_reasons` | broker | the stop reason |
| `agnes.session_id`, `agnes.user_id`, `agnes.agent_id`, `agnes.ticket_scope` | broker | which session, who ran it, under which agent; `llm` is the embedded turn engine, `main` the native sandbox |
| `agnes.workload`, `agnes.purpose` | both | the call's context (`src/observability/llm_context.py`) — coarse kind of work and a finer call-site label, so a builder turn, a corporate-memory extraction and an auto-title stop looking identical |
| `agnes.turn_id`, `agnes.job_id`, `agnes.subject_id` | both | the chat turn, the worker job, and what the call is *about*, when the context carries them |
| `agnes.upstream`, `agnes.stream`, `http.response.status_code`, `error.type` | broker | where the call went and how it ended |
| `agnes.response_bytes`, `agnes.stream_complete` | broker | how much of the response came back, and for a stream whether the model reached its stop reason — a client that walks away mid-turn leaves a span with no answer and no final usage, and this is what tells it apart from a lost export |
| `agnes.response_truncated` | broker | the usage was recovered from the stream's head/tail edges because the full mirror overflowed — tokens, cost and stop reason are still exact; only the content SUMMARY (never exported unless the policy allows it) was cut |
| `agnes.prompt_chars`, `agnes.completion_chars` | both | sizes of the exchange, never text |
| `agnes.cost_usd` | both | the price `src/llm_pricing.py` computed for the call, at the same rates the matching `llm_calls` row was priced at — a collector never has to re-implement the price table |
| `agnes.kind` | both | `completion` (the broker), `generation` (a server-side call), `turn`, `tool`, or `feedback` |
| `agnes.tool_calls` | turn | how many tool calls the turn made, set when the turn span closes |

**Identity minimisation.** `agnes.user_email` is not exported on any span —
only `agnes.user_id` rides the span, which has a stable join value
off-instance the email never had. Fetching the email used to be the only
reason the broker read the session row for the native sandbox's `main`
scope on the span path, and it no longer does. A collector-side query that
already references the attribute is unaffected: a missing-key read comes
back `NULL` rather than an error, so the column simply empties.

The resource on every span is `service.name=agnes`, `service.version`,
`deployment.environment` and `service.instance.id` (`hostname:pid`) —
`deployment.environment` is the same `AGNES_DEPLOYMENT_ENV` /
`RELEASE_CHANNEL` label the logs carry, so one instance is one value in
both signals. `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` override
any of them.

### Content policy — placement and consent

Prompt and completion TEXT leaves the instance only under a policy somebody
recorded — the same reasoning the logs follow (never carry it), extended
with WHO agreed and WHERE it goes. Three placement tiers, named in the
policy and in this doc:

- **T0, on-instance.** `chat_messages`, session JSONL, `llm_calls`. Always,
  under the customer's own contract.
- **T1, operator-controlled collector.** An OTLP endpoint the instance's own
  owner runs.
- **T2, third-party collector.** An endpoint someone else runs (the vendor,
  a SaaS). The code cannot verify which tier an endpoint actually is — the
  policy record makes the operator say it.

`config/instance.yaml`:

```yaml
observability:
  content_export:
    mode: off            # off | pseudonymized | full
    placement: operator  # operator | third_party
    basis: ""            # free text: contract clause, DPA reference, "internal dev instance"
    approved_by: ""      # a person, never blank unless off
    approved_at: ""      # ISO date
    workloads: []        # allowlist; empty = every workload once mode != off
```

Rules, enforced in `src/observability/otel.py` and
`src/observability/content_policy.py`:

- **A mode without a basis is `off`.** `capture_content_enabled()` (and the
  relay's `content_export_mode()`) return true only when `mode != off` AND
  `basis`, `approved_by`, `placement` are ALL non-empty; an unfinished
  record is logged at WARNING at startup ("content export requested
  without a recorded basis; exporting sizes only") and treated as `off`.
- **`AGNES_OTEL_CAPTURE_CONTENT` is a deprecated alias.** It used to be the
  whole switch. Set while no policy exists, it is now ignored with the
  same warning. **BREAKING**: a deployment that relied on the variable
  alone must add the policy record above to keep exporting content.
- **The effective policy is logged once at startup** (mode, placement,
  approved_by, workloads) and written to the audit log as
  `observability.content_export` (a system action, content-free — the
  basis TEXT stays in the config file, only `basis_recorded: true/false`
  enters `params`), so the decision is in the trail rather than in
  somebody's memory.
- **`pseudonymized` runs every exported text through the instance
  anonymizer** (`src.anonymization.anonymize_markdown`, this instance's own
  pseudonym key and `rules_from_config()`) before it is exported — stable
  `EMAIL_<hmac>`/`PERSON_<hmac>`… tokens, so a collector can still tell two
  mentions apart without ever holding the value. `full` exports as today,
  unchanged. A pseudonymisation that cannot run (a missing key, an
  anonymizer failure) withholds the text — `export_text` fails **closed**,
  never falls back to the raw exchange.

Text rides two span **events** — never span attributes — in the
OpenTelemetry GenAI message shape (`[{role, parts}]` as JSON):

| event | attribute | carries |
|---|---|---|
| `gen_ai.content.prompt` | `gen_ai.prompt` | system prompt and conversation, tool calls and tool results included, binary blocks reduced to their type |
| `gen_ai.content.completion` | `gen_ai.completion` | the answer, re-assembled from the stream |

Events rather than attributes on purpose: a collector stores a span's
attributes as one JSON object with keys in alphabetical order, and an
agent turn's prompt runs to hundreds of KiB, so anything sorting after
`gen_ai.input…` — the answer, the usage — fell past every preview or size
cap downstream. With the text on events the attribute object stays small
and parseable however long the conversation is, and each side of the
exchange is its own record the collector can map, cap or drop
independently (in a Data-Streams style sink that means mapping the
`events` field to a column). Each event's text is capped
(`MAX_CONTENT_CHARS`, 256 KiB) and a cut is flagged on the span as
`agnes.content_truncated`; the sizes (`agnes.prompt_chars` /
`agnes.completion_chars`) are on the span whether capture is on or not.

**Content classes differ by workload**, and `workloads` (empty by default,
meaning every workload once `mode` is not `off`) lets the policy say so: an
operator can open `builder`/`corporate_memory` content for quality work
while keeping `chat` at `off`, because the two carry very different
content:

| workload | the prompt is | the completion is |
|---|---|---|
| `chat` / `agent_api` | the customer's conversation and data | the customer's conversation |
| `extraction` (facts, OCR, vision, NER) | the customer's document | a derived artifact (facts, proposals) |
| `builder` | an admin-authored draft plus candidate ids | a config patch |
| `corporate_memory` / `knowledge` / `semantic_layer` | employee notes, catalog text | derived notes |

Every content producer passes its own workload to the gate
(`capture_content_enabled(workload=...)` in `src/observability/otel.py`,
which reads `content_export_mode(workload=...)` in
`src/observability/content_policy.py`): a completion span's is the chat
turn's (or `agent_api` for an agent-bound call), a generation span's is the
ambient `llm_context`'s, and the engine's telemetry relay (below) is `chat`
by definition. A workload name the vocabulary does not know is dropped
from the allowlist and warned about at startup rather than kept, so a typo
cannot silently expand "only these workloads" into "everything". The
other direction is closed too: a `workloads` value that names nothing valid
(a typo-only list, a mapping) disables content export with a warning instead
of widening it, and a bare string (`workloads: chat`) is read as a one-entry
list.

### The embedded engine's own spans

The broker sees a completion, not the agent loop around it. The embedded
turn engine's sandbox traces that loop itself — one span per turn, per
model step and per tool call, properly nested — and exports it through its
in-sandbox relay's `otlp` scope to `POST /api/broker/otlp/v1/{signal}` on
this instance, which swaps the per-turn `kai_otlp` ticket for the same
collector credential the app's own export uses and forwards the batch. The
scope is minted by `/api/kai/tickets` exactly when `OTEL_EXPORTER_OTLP_ENDPOINT`
is set, so the sandbox's traces land wherever the broker's do, and nowhere
when the instance exports nothing.

Two halves, in this order: the app version carrying the route first, the
engine's `HOST_BROKER_OTLP_URL` second. The URL is what makes the sandbox
initialize OTel at all, and it also makes `otlp` an *active* relay scope
that every turn needs a ticket for — set it against an app that does not
mint one and every turn fails before the prompt is sent. Rolling back is
the reverse: clear the URL, then the app.

Not exported by anything: HTTP request spans and database calls.

#### The relay under the same policy

The sandbox's own spans carry the same content classes the broker's
completion spans do — the prompt, the tool calls, the answer — so
`otlp_proxy` (`app/api/broker.py`) decodes the protobuf batch it is about
to forward and applies the SAME `observability.content_export` policy, at
workload `chat` by definition (the relay carries one turn's own prompt and
answer):

- **`off`** (the default) — the content attributes (`gen_ai.prompt`,
  `gen_ai.completion`, `gen_ai.input.messages`, `gen_ai.output.messages`)
  are stripped from every span and span event, and
  `agnes.content_stripped=true` is added wherever something was actually
  removed; the structural turn/step/tool spans still flow — refusing the
  whole batch would lose what answers "what did this turn do" to protect
  something a removal already protects. A logs batch is accepted and its
  bodies dropped, so the exporter sees a 2xx rather than retrying a
  decision the operator made.
- **`pseudonymized`** — every content attribute and log body is rewritten
  through the instance anonymizer, and the batch is re-serialised
  uncompressed (the caller drops the `content-encoding` header it was
  about to forward).
- **`full`** — the bytes are forwarded byte-for-byte, compression header
  and all — no decode, no re-serialise.

Metrics (`/v1/metrics`) are always forwarded as sent — counts, never
content. A gzip-encoded batch is decompressed for scrubbing and re-sent
uncompressed; a batch that cannot be decoded (bad gzip, not a valid
protobuf of the declared signal, or a decompression past the relay's own
ceiling) is refused with `400 otlp_batch_undecodable` rather than
forwarded unstripped — a relay that cannot read a batch cannot claim the
batch is free of content, so it fails **closed** under `off`/`pseudonymized`,
the same way `export_text` fails closed for the app's own spans.

## Conversation corpus export — for evaluation, under the content policy

Telemetry (above) is one span/row per LLM call, content capped, for "what did
this cost and where does it burn". The corpus export is a different product:
one COMPLETE record per chat session, every surface (web, Slack, Telegram,
agent API), built on-instance from what the instance already keeps
(`chat_sessions`, `chat_messages`, `llm_calls`, `chat_message_feedback`,
`agent_memories`) — for "why are the answers bad, at scale".

### Shape

One record per session:

| field | source |
|---|---|
| `thread_id` | `chat_sessions.id` |
| `source` | literal `agnes` |
| `surface`, `agent_id`, `user_id` | the session — **never the email** |
| `deployment_environment` | the instance label the logs and spans carry |
| `conversation_start`, `conversation_end`, `duration_seconds` | first and last message timestamps |
| `turn_count`, `message_count`, `tool_call_count`, `tool_calls_sequence` | derived from messages and parts |
| `llm_run_count`, `total_prompt_tokens`, `total_completion_tokens`, `llm_cache_read_tokens`, `llm_cache_creation_tokens`, `total_cost`, `primary_model`, `provider` | summed from the session's `llm_calls` rows; fallback to `chat_messages` token columns when no `llm_calls` row exists |
| `cost_status` | `ledger` (measured from `llm_calls`), `transcript` (priced from `chat_messages` token columns), or `unavailable` (zeros, never a silent zero) |
| `messages_json` | `[{role, content, turn_id, created_at, parts}]` — complete, tool_use and tool_result blocks included, in order |
| `tool_calls_json` | `[{turn_id, tool_name, input, output, is_error, started_at}]` |
| `first_user_message`, `last_message_role`, `final_assistant_message_complete` | derived |
| `last_run_status`, `has_error`, `error_types` | the session's `llm_calls` statuses and error frames — a call is `ok`, `error` (the upstream refused or was unreachable) or `incomplete` (a stream that returned HTTP 200 but never reached its stop reason, i.e. a half-delivered answer), and `has_error` counts everything that is not `ok` |
| `feedback_json` | `[{turn_id, user_id, verdict, comment, created_at}]` |
| `memory_writes_json` | `[{memory_id, turn_id, status, content_length}]` — the memory's own content is never included, only its length |
| `content_mode` | `full` or `pseudonymized` — what this record's text went through |
| `exported_at` | |

### Under the content policy

Both the field table's content-bearing fields and the export as a whole are
gated by the same `observability.content_export` policy the OTel export
above obeys (mode/placement/basis/approved_by/workloads — see *Content
policy — placement and consent* above). A pull refuses
`403 content_export_disabled` — with a `reason` of `mode_off`, `no_basis`
or `workload_excluded` — when the policy is off, has no recorded basis, or
its `workloads` allowlist excludes `chat`. Under `mode: pseudonymized`,
`messages_json`, `tool_calls_json` and
`first_user_message` go through the same instance anonymizer the OTel path
uses, once at export time (never per span, unlike the telemetry above);
under `full` they export verbatim. `content_mode` on every record says
which happened, so a downstream consumer never has to guess. No new
retention: the export reads what `chat_messages` already keeps.

### Pulling it

`GET /api/admin/conversations/corpus?since=&until=&surface=&agent_id=&format=jsonl|json&limit=&cursor=`
— admin-only, Postgres-only (a DuckDB-backed instance answers a typed
`501`), newline-delimited JSON by default, `limit` at most 500, a keyset
cursor on `(last_message_at, id)` returned as both the `X-Next-Cursor`
header and `next_cursor` in the JSON body (`format=json`). `since` is
required — a bare call is a `400 since_required`, not an unbounded scan of
every conversation the instance has ever held.

```bash
curl -s -H "Authorization: Bearer $PAT" \
  "$SERVER/api/admin/conversations/corpus?since=2026-01-01&limit=200" \
  | tee conversations.jsonl
```

`agnes admin conversations export --since 2026-01-01 --out conversations.jsonl`
mirrors it from the terminal, following the cursor across pages until
exhausted (`--json` writes one array instead). A data platform pulls this
with a generic HTTP extractor and a personal access token — the format is
deliberately transport-neutral, no vendor-specific client required.

Every pull writes one `conversations.export` audit row (`since`, `until`,
`surface`, `agent_id`, `count`, `content_mode`, `placement`, `delivery:
"pull"` — never the exported content itself).

### Pushing it

Set `observability.conversation_export.{endpoint, headers_secret_env,
interval_minutes, surfaces}` in `instance.yaml` to turn on a scheduled push.
A `conversation-export` worker job (LIGHT lane, one at a time — an
idempotency key plus a Postgres advisory lease acquired only once the config
and content-export-policy gates below pass, so an unconfigured or
policy-off instance never opens a database connection for it) reads a
Postgres-persisted watermark (`export_watermarks`, Postgres-only — a
`(last_message_at, id)` keyset position, not a bare timestamp, so the
trailing conversation of a run is never re-sent on the next tick, keyed by
a hash of `endpoint` + `surfaces` rather than one fixed row — repointing
`endpoint` or widening/narrowing `surfaces` therefore re-delivers the
whole corpus under the new configuration from a fresh cursor, so the
destination collector must upsert by `thread_id`), walks every
conversation completed since it, and POSTs newline-delimited JSON
batches (at most 200 records or 8 MiB per request — a single conversation
whose own line already exceeds 8 MiB is still offered to the destination,
since the export is "complete, never truncated" and there is nothing
smaller to send; if the destination refuses it the run steps over that one
record rather than stopping, counting it as `oversized_skipped` in the
audit row and naming it in the log, so one outsized transcript cannot block
every conversation behind it, and the pull endpoint — which has no batch
cap — still serves it whole) to `endpoint`, with the auth headers parsed
`OTEL_EXPORTER_OTLP_HEADERS`-style from the environment variable named by
`headers_secret_env` — the header value itself never sits in
`instance.yaml`. Because that request carries a secret and customer
conversations, `endpoint` is held to a narrow shape — an `https://` URL to
a named host (plain `http://` only to the loopback host, for a local relay)
with no credentials in it — and, when `AGNES_REMOTE_ATTACH_HOST_ALLOWLIST`
is set, its host must be on that list, the same egress control the
credentialed remote `ATTACH` uses; anything else leaves the sink off with a
warning rather than posting anywhere. `surfaces` narrows the underlying query itself, not just
what gets POSTed, so an excluded surface is never fetched at all. A
conversation is exported once its last message is at least 5 minutes old;
a thread that continues later is re-exported with the fuller transcript,
so the destination upserts by `thread_id`. Delivery is retried three
times (four attempts total, exponential backoff: 1s, 2s, 4s) on a 5xx or a
connection error; a 4xx (including 429) is never retried within a run and
leaves the watermark exactly where the last successful batch left it, to
be retried on the next tick rather than skipped or resent from scratch.
`interval_minutes` (default 60) sets the scheduler cadence; with `endpoint`
unset the scheduler has no such row at all. The same content-export policy
gates it: when the policy is `off`, has no recorded basis, or excludes the
`chat` workload, the job sends nothing and leaves the watermark alone
(warned once per process, not once per tick). Audited as
`conversations.export` with `delivery: "push"`, `count`, `refreshed` and
`endpoint_host` — never headers, never content. A run that fails
unexpectedly mid-walk is caught, logged, and audited with
`result: "failed"` and the exception's class name (never its message) —
the job itself never raises. On a DuckDB-backed instance the job is a
clean no-op.

The main watermark only ever advances past a `chat_sessions` row once, so a
thumbs-up/down (or a comment edit) filed on a turn AFTER its conversation
was already delivered would otherwise sit in `chat_message_feedback`
forever with nothing that ever revisits it — the delivered record's
`feedback_json` would stay empty for good. After the main walk, the same
run additionally sweeps for the session ids whose feedback changed since
the last sweep and re-delivers just those records through the identical
builder/batching/retry path (`refreshed` in the audit row, separate from
`count`) — the destination already upserts by `thread_id`, so a
re-delivered record simply replaces the stale one. This aux sweep tracks
its own, coarser watermark (same `export_watermarks` table, one row per
delivery configuration, suffixed `:feedback`) and is capped at 500 session
ids per run rather than paginated, so a burst of feedback wider than that
in one window leaves stragglers for the window after. A session the main
walk already delivered in the same run is never swept twice — its feedback
at query time already rode along in that record. Memory-status changes
(`agent_memories`) are NOT covered by this sweep: that table has no single
"last changed" timestamp column to sweep on, only separate
`created_at`/`activated_at`/`archived_at` markers for each lifecycle step.

## No telemetry vendor

Agnes sends nothing to a third-party analytics or error-tracking service, and
has no key for one. The OTLP export above goes only where the operator
points it, with the operator's credential, and is off until they do. An
optional integration with a hosted product-analytics vendor existed until
0.96 and was removed (see `CHANGELOG.md`): it was off on every deployment,
it never saw the failures that mattered — a handled error is not a 500, so
it was never captured — and it asked operators to ship prompts and session
replays off-host to get numbers the log pipeline already carries. What it
did well, LLM call metadata and an environment label, is above, in logs
and, opt-in, in traces.
