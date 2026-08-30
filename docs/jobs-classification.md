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
| `keboola-semantic-layer-refresh` | `POST /api/admin/run-keboola-semantic-layer-refresh` | stays-HTTP | Long interval (6h default), low request volume; not yet migrated. |
| `usage-prune` | `POST /api/admin/usage/prune` | stays-HTTP | Daily retention prune, short-circuits when disabled; not yet migrated. |
| `jira-sla-poll` | `POST /api/admin/run-jira-sla-poll` | stays-HTTP | Short-circuits when Jira isn't configured; not yet migrated. |
| `jira-consistency-check` | `POST /api/admin/run-jira-consistency-check` | stays-HTTP | Short-circuits when Jira isn't configured; not yet migrated. |
| `jira-refresh` | enqueued from the Jira webhook path (no scheduler row) | queued | HEAVY lane orchestrator rebuild, previously called inline from the webhook's incremental-transform path; now a durable job so a slow rebuild can't block the webhook response. |
| `knowledge-packaging` | `POST /api/admin/run-knowledge-packaging` | stays-HTTP | Fingerprint-gated (K3, #798); not yet migrated. |
| `knowledge-digests` | `POST /api/admin/run-knowledge-digests` | stays-HTTP | Fingerprint-gated (K4, #799); not yet migrated. |
| `ducklake-maintenance` | `POST /api/jobs` (`kind=ducklake-maintenance`) | queued | DuckLake `merge_adjacent_files` → `ducklake_expire_snapshots` → `ducklake_cleanup_old_files` → catalog VACUUM pass (wave-2G Task 5) — LIGHT lane. Enqueued daily regardless of the configured analytics backend; the handler (`app/worker/kinds.py::_run_ducklake_maintenance`) no-ops when `analytics.backend != "ducklake"`, so this row is harmless on a legacy-backend instance. |
| `analytics-migrate` | `POST /api/admin/analytics/migrate` (`kind=analytics-migrate`) | queued | Wave-2G Task 6 migration command — an explicit-target full rebuild (`SyncOrchestrator.migrate_to_backend`) into `ducklake` or back to `legacy`, from the on-disk extracts tree, regardless of the currently configured `analytics.backend`. HEAVY lane (same cost class as `data-refresh`). No scheduler row — admin-triggered only, via `agnes admin analytics migrate --to <target>` / the `admin_analytics_migrate` MCP tool. |
| `agents:run-due` | `POST /api/v1/agents/run-due` | stays-HTTP | Agent-schedules design (2026-08-17) — same shape as `script-runner`: the sweep itself is cheap (walk + per-row optimistic claim), and the actual work it dispatches goes through the existing `agent_response` job kind (already `queued`, HEAVY/LIGHT-lane per its own registration) rather than running inline. `every 1m`, gated on `SCHEDULER_AGENT_SCHEDULES` (default on). |
| `extraction-run-due` (optional, admin-configurable) | `POST /api/admin/sharepoint/extraction/run-due` | stays-HTTP | TCRD-226 — same shape as `agents:run-due`: the sweep is cheap (walk every SharePoint connection + per-row `is_table_due` check), and the actual work it dispatches goes through the existing `corpus-extraction` job kind (already `queued`, its own EXTRACTION lane). Registered only when `extraction.schedule` is configured (`_extraction_schedule()` -> `None` omits the row) — off by default, mirroring `extraction.enabled`'s own posture. |
| `sharepoint-acl` | `POST /api/jobs` (`kind=sharepoint-acl-sync`) | queued | 2026-08-30 plan, Task 4 — mirrors SharePoint scope-root permissions into Agnes groups/memberships/collection grants (`connectors/sharepoint/acl_sync.py::run_acl_sync`) — network-bound Graph paging + a few hundred repo writes, LIGHT lane, `daily 06:00`. Enqueued unconditionally; the handler no-ops when `acl_mirroring.enabled` is false, so this row is harmless on an instance that hasn't turned the feature on. |

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
