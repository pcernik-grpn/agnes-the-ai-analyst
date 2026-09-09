# Scheduler job classification (wave-2B)

Every row the scheduler sidecar (`services/scheduler/__main__.py::build_jobs()`)
fires falls into one of two buckets:

- **`queued`** — the scheduler POSTs a fire-and-forget enqueue request to
  `POST /api/jobs` (`app/api/jobs.py`) and returns immediately; a worker
  process (`app/worker/runtime.py`, kinds registered in `app/worker/kinds.py`)
  claims the row and does the actual work out-of-band. This is where the
  wave-2B durable job queue (spec §3.3) lands the heaviest/most
  contention-prone work.
- **`stays-HTTP`** — the scheduler still calls the endpoint synchronously
  and waits for the response, exactly as before. Appropriate for cheap,
  sub-second work where queueing overhead isn't worth it, or for jobs not
  yet migrated (see "Explicitly deferred" below).

`jira-refresh` is a `queued` job kind (registered in `app/worker/kinds.py`)
but has no scheduler row of its own — it is enqueued from the Jira webhook
path (`connectors/jira/service.py::trigger_incremental_transform`), not on a
cadence, so it doesn't appear in `build_jobs()`.

## All scheduler rows

| name | current target | classification | why |
|---|---|---|---|
| `data-refresh` | `POST /api/jobs` (`kind=data-refresh`) | queued | Keboola/BigQuery extractor run + orchestrator rebuild — long-running, HEAVY lane. |
| `health-check` | `GET /api/health` | stays-HTTP | Sub-second liveness poke; the scheduler needs the response synchronously to log status. |
| `script-runner` | `POST /api/scripts/run-due` | stays-HTTP | Not yet migrated — admin script execution is out of wave-2B scope. |
| `marketplaces` | `POST /api/jobs` (`kind=marketplaces-sync`) | queued | Git clone + RBAC-filtered re-aggregation across all registered marketplaces — bulk I/O, LIGHT lane. |
| `initial-workspace` (optional, admin-configurable) | `POST /api/admin/initial-workspace/sync-if-configured` | stays-HTTP | Self-gating no-op on instances without an IWT repo; not yet migrated. |
| `session-collector` | `POST /api/jobs` (`kind=session-collector`) | queued | Filesystem walk + parquet write over all analyst sessions — LIGHT lane, cadence-sensitive so queueing avoids blocking the health-check thread. |
| `session-processor:verification` | `POST /api/admin/run-session-processor?processor=verification` | stays-HTTP | LLM-heavy, but deferred to a later workstream — the session-processor family isn't part of this wave's migrated set. |
| `session-processor:usage` | `POST /api/admin/run-session-processor?processor=usage` | stays-HTTP | Same session-processor family as verification; deferred to a later workstream. |
| `corporate-memory` | `POST /api/jobs` (`kind=corporate-memory`) | queued | LLM-driven corporate-memory collection pass — LIGHT lane, cadence-sensitive. |
| `jira-org-refresh` | `POST /api/jobs` (`kind=jira-org-refresh`) | queued | One organization-API request per organization (minutes on a large site) — LIGHT lane, `daily 05:00`. No caller needs the result synchronously, so it never warranted a REST endpoint of its own. |
| `store-blocked-purge` | `POST /api/admin/run-blocked-purge` | stays-HTTP | Cheap `rmtree` + one UPDATE; sub-second, not worth queueing overhead. |
| `store-reap-stuck-reviews` | `POST /api/admin/run-reap-stuck-reviews` | stays-HTTP | One indexed SELECT + a handful of small UPDATEs; sub-second reaper. |
| `store-lint-audit` | `POST /api/admin/store/lint-audit` | stays-HTTP | Fingerprint-gated (zero-cost when nothing changed) weekly audit; not yet migrated. |
| `bq-metadata-refresh` | `POST /api/admin/run-bq-metadata-refresh` | stays-HTTP | Long interval (4h default), not cadence-sensitive; not yet migrated. |
| `semantic-sources-refresh` | `POST /api/admin/run-semantic-sources-refresh` | stays-HTTP | Long interval (6h default), low request volume; not yet migrated. Replaced the per-connector `keboola-semantic-layer-refresh` / `databricks-semantic-layer-refresh` rows (#1707 Block 3). |
| `usage-prune` | `POST /api/admin/usage/prune` | stays-HTTP | Daily retention prune, short-circuits when disabled; not yet migrated. |
| `jira-sla-poll` | `POST /api/admin/run-jira-sla-poll` | stays-HTTP | Short-circuits when Jira isn't configured; not yet migrated. |
| `jira-consistency-check` | `POST /api/admin/run-jira-consistency-check` | stays-HTTP | Short-circuits when Jira isn't configured; not yet migrated. |
| `jira-refresh` | enqueued from the Jira webhook path (no scheduler row) | queued | HEAVY lane orchestrator rebuild, previously called inline from the webhook's incremental-transform path; now a durable job so a slow rebuild can't block the webhook response. |
| `knowledge-packaging` | `POST /api/admin/run-knowledge-packaging` (`kind=knowledge-packaging`) | queued | TCRD-296 synthesis C.15: an inline, synchronous packaging pass behind this endpoint used to be bounded only by the scheduler's own 600s client timeout, and a slower pass let the NEXT tick fire a second, overlapping call that raced the first hard enough to OOM the app. The endpoint is now a thin enqueue (idempotency-keyed, same shape as `analytics-migrate`'s bespoke admin endpoint below) of the `knowledge-packaging` worker job kind (LIGHT lane; `app/worker/kinds.py::_run_knowledge_packaging`), which supplies a belt-and-braces PG advisory lock and a 20-minute wall-clock budget (`run_packaging_pass`'s `deadline`, checkpointed per collection so an interrupted run resumes cleanly). |
| `knowledge-digests` | `POST /api/admin/run-knowledge-digests` | stays-HTTP | Fingerprint-gated (K4, #799); not yet migrated. |
| `ducklake-maintenance` | `POST /api/jobs` (`kind=ducklake-maintenance`) | queued | DuckLake `merge_adjacent_files` → `ducklake_expire_snapshots` → `ducklake_cleanup_old_files` → catalog VACUUM pass (wave-2G Task 5) — LIGHT lane. Enqueued daily regardless of the configured analytics backend; the handler (`app/worker/kinds.py::_run_ducklake_maintenance`) no-ops when `analytics.backend != "ducklake"`, so this row is harmless on a legacy-backend instance. |
| `analytics-migrate` | `POST /api/admin/analytics/migrate` (`kind=analytics-migrate`) | queued | Wave-2G Task 6 migration command — an explicit-target full rebuild (`SyncOrchestrator.migrate_to_backend`) into `ducklake` or back to `legacy`, from the on-disk extracts tree, regardless of the currently configured `analytics.backend`. HEAVY lane (same cost class as `data-refresh`). No scheduler row — admin-triggered only, via `agnes admin analytics migrate --to <target>` / the `admin_analytics_migrate` MCP tool. |
| `agents:run-due` | `POST /api/v1/agents/run-due` | stays-HTTP | Agent-schedules design (2026-08-17) — same shape as `script-runner`: the sweep itself is cheap (walk + per-row optimistic claim), and the actual work it dispatches goes through the existing `agent_response` job kind (already `queued`, HEAVY/LIGHT-lane per its own registration) rather than running inline. `every 1m`, gated on `SCHEDULER_AGENT_SCHEDULES` (default on). |
| `extraction-run-due` (optional, admin-configurable) | `POST /api/admin/sharepoint/extraction/run-due` | stays-HTTP | TCRD-226 — same shape as `agents:run-due`: the sweep is cheap (walk every SharePoint connection + per-row `is_table_due` check), and the actual work it dispatches goes through the existing `corpus-extraction` job kind (already `queued`, its own EXTRACTION lane). Registered only when `extraction.schedule` is configured (`_extraction_schedule()` -> `None` omits the row) — off by default, mirroring `extraction.enabled`'s own posture. |
| `sharepoint-subscriptions-renew` (only when `sharepoint.enabled`) | `POST /api/admin/sharepoint/subscriptions/run-due` | stays-HTTP | Renews the Microsoft Graph drive change-notification subscriptions that make near-real-time crawling work at all (`connectors/sharepoint/subscriptions.py::renew_due_subscriptions`). Stays-HTTP for the same reason as `extraction-run-due`: the due-check is purely local (each connection's recorded expiries against a 72h window) and the only work is a handful of Graph `PATCH`es for the connections actually due — nothing worth a worker lease. Registered whenever the receiver flag is on; `daily 04:30`, re-timed by `SCHEDULER_SUBSCRIPTION_RENEWAL_SCHEDULE`. With the connector off a stale row gets the admin router's typed 409 (`feature_disabled`) — still harmless, refused one layer earlier than the module's own no-op. Unlike the extraction sweep the CADENCE has a default: subscriptions expire on Graph's clock (25 days requested against its 30-day ceiling), so "not configured" must not mean "never renewed". |
| `sharepoint-acl` | `POST /api/jobs` (`kind=sharepoint-acl-sync`) | queued | 2026-08-30 plan, Task 4 — mirrors SharePoint scope-root permissions into Agnes groups/memberships/collection grants (`connectors/sharepoint/acl_sync.py::run_acl_sync`) — network-bound Graph paging + a few hundred repo writes, LIGHT lane. 2026-08-31 plan, Task 2 moved the cadence from a fixed `daily 06:00` to an interval (`every Nh`, default 4h) driven by the `acl_sync.interval_hours` switch (`services/scheduler/__main__.py::_acl_sync_schedule`) — the source-side revocation window this bounds is hours-scale policy (Q7 ratified MUST NOT). Enqueued unconditionally; the handler no-ops when the `sharepoint` switch is false, so this row is harmless on an instance that hasn't turned the feature on. |
| `sharepoint-subtree-sweep` | `POST /api/jobs` (`kind=sharepoint-subtree-sweep`) | queued | 2026-08-30 plan, Task 7 — walks each mirrored scope's folder tree to detect and exclude broken-inheritance subtrees (`connectors/sharepoint/acl_sync.py::run_subtree_sweep`) — a full probe pass is multi-hour on a large library (spec §6.2), LIGHT lane, no automatic retry. Its lease (`AGNES_SP_SWEEP_LEASE_S`) defaults to 300s, the same heartbeat-protected default every other long-running kind uses (`app/worker/kinds.py`'s lease/retry tuning note) — NOT sized to the sweep's own multi-hour duration; the worker's heartbeat, not the lease's own size, is what keeps a genuinely running sweep's lease alive, so a worker that dies mid-sweep is reclaimed within minutes rather than up to the old 4h default. 2026-08-31 plan, Task 2 moved the cadence from weekly (`cron 0 7 * * 1`) to `daily 07:00` — the broken-inheritance detection window this bounds is now hours-scale policy (MUST NOT posture) rather than a week-long blind spot. Enqueued unconditionally; the handler no-ops when the `sharepoint` switch is false, and each connection additionally self-guards against a restart-refire via its own `acl_sweep_last_full`/`acl_sync.sweep_interval_days` check (default lowered 7 → 1 day in the same task). |
| `conversation-export` (optional, admin-configurable) | `POST /api/jobs` (`kind=conversation-export`) | queued | Design 2026-09-08 §3.12, Task 11 — the conversation-corpus export's PUSH sink: POSTs newline-delimited JSON batches of completed conversations to `observability.conversation_export.endpoint`, advancing a Postgres-persisted watermark (`export_watermarks`) only after a 2xx. LIGHT lane, idempotency-keyed (`idempotency_key=conversation-export`) plus a belt-and-braces PG advisory lock (`src.db_pg.conversation_export_lease`), same two-layer shape as `knowledge-packaging` above. Registered ONLY when `observability.conversation_export.endpoint` is configured (`_conversation_export_schedule()` -> `None` omits the row) — off by default, mirroring `extraction-run-due`'s posture; `interval_minutes` (default 60) sets the cadence. The handler (`app/worker/kinds_conversation_export.py`) additionally no-ops, cleanly, when the content-export policy excludes workload `chat` or on a DuckDB-backed instance (swallows `RequiresPostgresBackend`). |

## Explicitly deferred (not in scope for this wave)

- **Collections/corpus ingest + admin register-table conversion** — deferred
  to the DuckLake workstream.
- **`telegram_bot` absorption into the gateway role** (three-plane spec
  §3.5) — not delivered in wave-2F; the spec's companion item (`ws_gateway`
  absorption) shipped there, Telegram did not. The standalone
  `services/telegram_bot` process keeps its own Compose service entry. Safe
  today: Compose runs it as a singleton, and under
  `coordination.backend: redis` the Telegram long-poll loop is additionally
  guarded by a leader lease, so even an accidental second replica cannot
  double-poll. Folding the consumer into the `gateway` role (and removing
  the Compose entry) is deferred to a later wave.
- **LISTEN/NOTIFY worker wakeup** — polling suffices for v1; the worker loop
  polls the `jobs` table on its own cadence instead of being pushed a
  wakeup notification.
- **Request-id correlation** across the scheduler → `/api/jobs` → worker →
  handler chain — deferred to the observability workstream.
- **Scheduler catch-up semantics** — the scheduler still keeps in-memory
  last_run; per-job catch-up (spec §3.3) is deferred to a later wave.
- ~~**Role-split /api/sync/status** — the api process's in-process lock is
  not held on split topologies; the auto-upgrade sync-defer probe rewrite to
  a job-queue query is deferred to WS I (ops tooling).~~ Done in wave-2E:
  `scripts/ops/agnes-auto-upgrade.sh`'s `sync_or_refresh_busy` now also
  queries `GET /api/jobs?kind=data-refresh&status=running` (authenticated
  with `SCHEDULER_API_TOKEN`), so the defer probe correctly sees a sync
  running in a separate worker container, alongside the original
  `/api/sync/status` check.
