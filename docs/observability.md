# Observability — logs, cost, and metrics

## Audit & activity trails (retention status)

Agnes keeps seven distinct records of "who did what": `audit_log` (admin/API
actions), chat transcripts (`chat_messages` — flag-controlled, see below),
CLI session JSONLs (viewer: `/admin/sessions`), usage rollups
(`usage_events`, viewer: `/admin/telemetry`), `sync_history`, `llm_usage`
(per-call agent token accounting), and agent-runtime forensics
(`agent_scope_snapshots`).

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

`agnes_turns` (token usage per assistant turn, including prompt-cache reads
and writes, across Claude Code and every chat surface) exists **only on
Postgres-backed instances** — its source table has no DuckDB counterpart. On a
DuckDB-backed instance the id is not registered at all: it never appears in
`agnes catalog`, and a `SELECT` against it says the table is unavailable here
rather than pointing at a package grant that could not surface it.

They are server-side only — `agnes pull` never downloads them — and they are
**members of a seeded data package with the slug `agnes-usage`**, so who may
query usage data at all is an admin decision like any other table grant.
Grant the package to a group (`/admin/access`, or `agnes admin grant create
<group> data_package <pkg-id>`) and its members can read the tables, still
filtered to their own rows; without the grant the tables are absent from
`agnes catalog` and a `SELECT` against them returns 403 naming the package.
Admins are unaffected (god-mode) and keep the unscoped view. Agents and
co-sessions also keep access without the grant — their authority is already
owner grants ∩ scope. Full model:
[`RBAC.md`](RBAC.md#internal-usage-tables-the-agnes-usage-package).

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
GCE-hosted deployments the Terraform module wires it up; see
[`gcp-logging.md`](gcp-logging.md).

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
AGNES_OTEL_CAPTURE_CONTENT=1                                 # optional — see below
```

On a VM built by the `customer-instance` Terraform module the three
variables come from the per-instance `otlp_endpoint`, `otlp_headers_secret`
(a Secret Manager secret name — the value is fetched at boot, never stored
in state) and `otlp_capture_content` fields; the module also writes
`AGNES_DEPLOYMENT_ENV` for every VM (the VM's name unless `deployment_env`
says otherwise). Like everything the startup script renders, they reach a
running VM only through a recreate.

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
  (`src/observability/llm_tracing.py`: summaries, extraction, the semantic
  layer) — the same tracer, the same table.

| attribute | on | carries |
|---|---|---|
| `gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.model` | both | provider (`anthropic`, `gcp.vertex_ai`) and models |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | both | uncached input and output |
| `gen_ai.usage.cache_read_input_tokens`, `gen_ai.usage.cache_creation_input_tokens` | broker | prompt-cache reads and writes, **separately** — folding them into `input_tokens` undercounts an agentic run by orders of magnitude (see *Chat cost*) |
| `gen_ai.response.finish_reasons` | broker | the stop reason |
| `agnes.session_id`, `agnes.user_email`, `agnes.user_id`, `agnes.agent_id`, `agnes.ticket_scope` | broker | which session, who ran it, under which agent; `llm` is the embedded turn engine, `main` the native sandbox |
| `agnes.upstream`, `agnes.stream`, `http.response.status_code`, `error.type` | broker | where the call went and how it ended |
| `agnes.prompt_chars`, `agnes.completion_chars` | generation | sizes, never text |

The resource on every span is `service.name=agnes`, `service.version`,
`deployment.environment` and `service.instance.id` (`hostname:pid`) —
`deployment.environment` is the same `AGNES_DEPLOYMENT_ENV` /
`RELEASE_CHANNEL` label the logs carry, so one instance is one value in
both signals. `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` override
any of them.

### Content

Prompt and completion text is **not** exported by default, for the same
reason the logs never carry it: in this product it routinely holds customer
data. `AGNES_OTEL_CAPTURE_CONTENT=1` adds `gen_ai.input.messages` (system
prompt and conversation, tool calls and tool results included, binary
blocks reduced to their type) and `gen_ai.output.messages` (the answer,
re-assembled from the stream) in the OpenTelemetry GenAI message shape.
Each attribute is capped (`MAX_CONTENT_CHARS`, 256 KiB) and a cut is flagged
as `agnes.content_truncated`. Turn it on only where the collector is
allowed to hold that data.

### What is not exported

HTTP request spans, database calls and the sandbox's own per-tool spans. The
broker sees a completion, not the agent loop around it; an engine that
traces its own turns needs its host to broker an `otlp` egress scope for
that, which this route does not yet do.

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
