# Observability — PostHog integration

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

Optional integration that wires four signals into a single PostHog project:

1. **Backend exceptions** — every unhandled FastAPI exception, plus rebuild
   failures from `src/orchestrator.py` and HTTP-job failures from
   `services/scheduler/`.
2. **LLM tracing** — every Anthropic / OpenAI-compat call emits a
   `$ai_generation` event with provider, model, latency, and token counts.
3. **Frontend errors + pageviews** — `window.error` /
   `unhandledrejection` forwarded via `posthog.captureException`; automatic
   `$pageview` and `$pageleave`.
4. **Session replay (masked) + feature flags** — both gated behind the same
   single `POSTHOG_API_KEY`.

The integration ships **off by default**. Setting one environment variable
turns everything on.

## Enabling the integration

```bash
# Required — the only switch that controls on/off.
# Use a PROJECT key (publishable phc_…), never a personal API key.
POSTHOG_API_KEY=phc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

That's the entire minimum. Defaults will:

- Send to `https://eu.i.posthog.com` (override with `POSTHOG_HOST`).
- Identify logged-in users by id + email (override with `POSTHOG_IDENTIFY_PII`).
- Record session replay with all inputs and known data surfaces masked
  (override with `POSTHOG_REPLAY=false` or
  `POSTHOG_REPLAY_MASK_SELECTOR=…`).
- Skip prompt / completion bodies in LLM events; emit token counts + latency
  only (override with `POSTHOG_LLM_PAYLOADS=1` if you accept the privacy
  trade-off — LLM prompts in this product routinely include customer SQL
  and data).

## All knobs

| Variable | Default | Notes |
|---|---|---|
| `POSTHOG_API_KEY` | unset | **The on/off switch.** Unset = integration is fully off. Project key only. |
| `POSTHOG_HOST` | `https://eu.i.posthog.com` | Full URL. Use `https://us.i.posthog.com` for the US region or your own host. |
| `POSTHOG_IDENTIFY_PII` | `email` | `none` / `id` / `email` / `full`. |
| `POSTHOG_REPLAY` | `true` | Disable replay only, keeping errors / events / flags. |
| `POSTHOG_REPLAY_MASK_SELECTOR` | empty | CSS selector appended to the default mask list. |
| `POSTHOG_LLM_PAYLOADS` | `0` | `1` adds `$ai_input` + `$ai_output_choices` to LLM events. Off by default. |
| `POSTHOG_ENVIRONMENT` | auto | Tagged on every event as the `environment` super-property. Auto-resolves to `local` when `LOCAL_DEV_MODE=1`, else `RELEASE_CHANNEL`, else `AGNES_DEPLOYMENT_ENV`, else `unknown`. |

## Splitting traffic by environment

Every captured event — backend exceptions, `$ai_generation`, browser
`$pageview`, JS errors, custom events — is tagged with two super
properties so PostHog dashboards can slice cleanly:

- `environment` — resolved at startup (see table above). Operators
  typically set this to `local`, `staging`, or `production` explicitly,
  or rely on the auto-resolver.
- `release` — the running `AGNES_VERSION`, falling back to
  `RELEASE_CHANNEL`. Useful for "is this error new in this release?"
  cohorting.

Both apply to backend events via the SDK's `super_properties` and to
browser events via `posthog.register({...})` in the loaded callback, so
filtering by `environment = production` in PostHog hides every event
generated from a developer laptop, CI, or staging.

## Privacy posture

- The PostHog **project key** is publishable — it's safe in browser HTML.
  PostHog uses a separate **personal API key** for admin operations. This
  integration only ever exposes the project key. Treat the personal key like
  any other secret and never set it as `POSTHOG_API_KEY`.
- Session replay defaults: `maskAllInputs: true`, plus a CSS-selector mask
  for known data-bearing classes (`.data-cell`, `.query-result`,
  `.sql-output`, plain `<code>` and `<pre>`, and any element marked
  `data-sensitive`). Add your own with `POSTHOG_REPLAY_MASK_SELECTOR`.
- LLM payloads are **off by default** because the prompts and completions
  in this product include customer SQL, query results, and table samples.
  Token counts and latency are always sent (no payload contents in them).
- `person_profiles: 'identified_only'` — anonymous visits do not create
  person records.

## Where the events come from

| Event | Code path |
|---|---|
| `$exception` (unhandled 500) | `app/main.py:_unhandled_exception_handler` |
| `$exception` (orchestrator rebuild) | `src/orchestrator.py:_capture_orchestrator_exception` |
| `$exception` (scheduler job) | `services/scheduler/__main__.py:_call_api` |
| `$exception` (CLI uncaught) | `cli/main.py:main` |
| `$ai_generation` | `src/observability/llm_tracing.py:trace_generation` wrapped at `connectors/llm/anthropic_provider.py:_attempt_extraction` and `connectors/llm/openai_compat.py` |
| `$pageview`, `$pageleave`, JS errors | injected into every `text/html` response by `app/middleware/posthog_inject.py` |

## CLI coverage

The `da` CLI (`cli/main.py:main`) catches every uncaught exception from a
command, forwards it to PostHog with `component=cli` and the invoked
command name, then flushes the client before re-raising for Typer's
default error printer. Normal Typer / Click exits, `SystemExit`, and
`KeyboardInterrupt` are intentionally skipped.

Operators must surface `POSTHOG_API_KEY` (and any other `POSTHOG_*` knob)
into the shell that runs `da` — typically by sourcing the same `.env` the
server uses, or by setting the variable in their shell profile. The CLI
respects exactly the same env-var contract as the server.

LLM calls made by CLI commands (`da query`, `da explore`, etc.) flow
through the provider wrappers in `connectors/llm/` and therefore emit
`$ai_generation` events via the same tracing path the server uses.

## Testing the integration

Boot the app with the key set, hit `/`, then provoke a 500 (e.g. via a
debug-only route). One **Errors** event should arrive within seconds along
with one `$pageview` per page load. Open **Session replay** and pick the
session — every `<input>` should show as a masked rectangle.

The unit tests in `tests/test_posthog_*.py` cover the disabled and enabled
configurations; `tests/test_llm_tracing.py` exercises the success and error
variants of the LLM event.

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

## Self-hosting note

PostHog is itself open source — operators with a self-hosted PostHog instance
just point `POSTHOG_HOST` at their endpoint. No code changes required.
