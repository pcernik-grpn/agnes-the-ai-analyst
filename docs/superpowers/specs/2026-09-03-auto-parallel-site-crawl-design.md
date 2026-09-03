# Automatic parallel crawl of one SharePoint site — design

**Status:** proposed design, 2026-09-03. Implementation plan:
[`../plans/2026-09-03-auto-parallel-site-crawl-plan.md`](../plans/2026-09-03-auto-parallel-site-crawl-plan.md).

## 1. Problem

The owner's requirement, verbatim intent: *the split into several sources that we did
to crawl a big site in parallel must happen in the background — the admin must not have
to split a site and then merge the results; that is laborious, and we do not know in
advance what should be split how.*

Today a large site is crawled by ONE `corpus-extraction` job on ONE extraction-lane slot,
walking every scope and every drive strictly sequentially (`connectors/sharepoint/crawler.py`
5454-5504, the "Drives stay SEQUENTIAL, deliberately" note at 5476-5483). The only way to
parallelize is the manual split shipped as `GET …/split-plan` + `POST …/splits`
(`app/api/admin_sharepoint.py` 2406-2625, packing in `connectors/sharepoint/site_split.py`
19-50), which clones the connection N times, mints one collection per folder
(2553-2568), and is folded back afterwards by `POST …/collections/consolidate`
(`src/repositories/sharepoint_collection_consolidation_pg.py` 91-268). The live run that
used it hit: N conversion pools multiplied silently (memory), facts passes starved of
lanes (`app/worker/runtime.py` 202-208), one collection per scope, per-connection
consolidation, and no view of "is the whole site done".

## 2. Current state (verified)

- **Scopes.** A scope row is `{source_scope_id, display_path, collection_id, drive_id,
  anonymize, access_mode, include_excluded_subtrees}` (`admin_sharepoint.py` 2559-2568);
  only rows with a `collection_id` are crawlable (`crawler.py` 4817-4824).
- **Delta unit.** `DriveTarget` (`crawler.py` 2034-2052): a drive root or a folder
  subtree, `state_key = "<drive_id>"` or `"<drive_id>:<item_id>"`, delta URL
  `/drives/{id}/root/delta` or `/drives/{id}/items/{item}/delta`. The cursor is already
  per key: `delta_links[target.state_key]` is written only after the page's rows are
  ingested (4791-4806); a 410 drops just that key (4720-4738).
- **State.** ONE JSON blob per connection (`sharepoint_connection_state`, PK
  `(connection_id, kind)`, `kind IN ('crawl','facts')` — `migrations/versions/0096_…py`
  53-61), loaded once and rewritten whole at every page boundary under a process-local
  `_state_lock` (`crawler.py` 639-648, 664-708). Two workers writing it would clobber each
  other — the blocker for any multi-job design.
- **Pool + memory.** One `_ConvertProcessPool` per run, sized to the run's concurrency cap
  (5415-5425); every fork must happen from a single-threaded point — a page boundary once
  the page's thread pool is joined (2919-2925, 4778-4783). `extraction.crawler.concurrency`
  (4958-4980) and the lane count `extraction.concurrency` (`runtime.py` 301-346) multiply
  against one tenant and one container (193-210).
- **Runs.** `extraction_runs` (`0094_extraction_runs.py` 45-94): one row per crawl,
  `job_id`, `status`, `phase`, `checkpoint_at`, `files_seen/done`, `report`, `progress`.
  `_RunRecorder` opens it, checkpoints it, and sweeps any leftover `running` row for the
  connection first (`crawler.py` 763-811, `extraction_runs_pg.py` 306-376). Liveness is
  derived at read time (`app/api/admin_extraction.py` 227-259); the fleet view is one row
  per connection from `list_latest_for_connections` (517-635).
- **Jobs.** `corpus-extraction` on `EXTRACTION_LANE`, no auto-retry, reclaim budget 25
  (`app/worker/kinds.py` 1707-1721, `app/worker/registry.py` 93-96); claim order is
  `priority DESC, created_at ASC` (`src/repositories/jobs_pg.py` 288); per-connection
  idempotency key `corpus-extraction:{id}` (`admin_sharepoint.py` 1048-1055) is what makes
  `POST …/extract` answer 409 while a run is in flight (2940-3024).
- **Facts.** Chained after the crawl inside the same job with the crawl's leftover
  deadline (`crawler.py` 5269-5328 -> `facts_extraction.py` 4187-4233), streamed mid-crawl
  every N ingested files as a standalone job deduped on `sharepoint-facts-extraction:{id}`
  (5131-5238). The pass walks documents by the scopes' collection ids (2557, 1974-1990),
  serialized per connection by an advisory lock (`state_store.py` 211-248).
- **Operator controls** that must keep working per site: `min_modified` (2218, applied
  per item 3863-3883), `resync` (5653-5671), `force_reprocess` (4642-4651),
  `retry_failed` (4418-4512, backlog entries carry their `state_key` 4465-4472),
  `retry_empty` (`admin_sharepoint.py` 3027-3096), cooperative stop on a connection-level
  flag (`crawler.py` 527-620, cleared at run start 5361-5370).

## 3. Options

### A — intra-job fan-out (threads in one job)
Shards as concurrent coroutines/threads inside the one `corpus-extraction` job, one
shared convert pool, one run row.
- (+) one pool, one memory envelope, no new job kind, no state split.
- (−) **Breaks the pool's fork-safety invariant.** Recycle/repair forks are only safe at a
  page boundary when no worker thread is alive (2919-2925, 3004-3018). With K shards
  paging independently there is never such a point; the alternative (`spawn`) was
  rejected in that docstring for test-observability reasons. This alone rules A out.
- (−) One process, one lane, one host: throughput is bounded by one worker's CPU/GIL
  (anonymize + ingest run on parent threads), a crash or OOM takes every shard down, the
  run-wide deadline and 429 budget cannot be per shard, and horizontal workers (the very
  reason `0096` moved state to Postgres) buy nothing.

### B — child jobs (recommended)
The `corpus-extraction` job becomes a short PLANNER: it estimates the site, packs it into
K shards, opens a parent run row, enqueues K `corpus-extraction-shard` jobs and returns.
Each child crawls its shard's delta units into the scope's own collection with its own
convert pool, its own run row (`parent_run_id`) and its own state rows; the LAST child to
finish finalizes the parent (aggregate report, chained facts pass).
- (+) Fits every existing primitive: SKIP-LOCKED claims, heartbeat leases, reclaim budget,
  lanes as the fleet-wide bound, PG-resident state, `fail_for_job` closing a dead child's
  row. Failure isolation and retries are per shard by construction. Any worker on any
  host can take any shard.
- (+) No cloning, no consolidation, one collection, one connection, one idempotency scope.
- (−) Needs per-shard state rows, parent/child columns on `extraction_runs`, a finalize
  step, a new job kind, and a memory clamp so K pools stay inside the container.

### C — automatic hidden clone connections
What `POST …/splits` does, auto-created and auto-consolidated. Worst: it multiplies every
per-connection artefact (facts ledger — a second ledger over the same collection can
double-extract; ACL sync; subscriptions; dispatch bookkeeping; credential references), the
consolidation is an after-the-fact data move that refuses on collisions, "hidden"
connections still appear in every `list(source_type="sharepoint")` walk (fleet view,
run-due sweep, ACL sync), and it leaves the admin with exactly the merge step the owner
asked to remove.

### Evaluation matrix (B chosen)
| Concern | A | B | C |
|---|---|---|---|
| Graph throttling (per app/tenant) | one AIMD governor | one governor per child; in-flight ≤ lanes × cap (§4.6) | N governors, unbounded by anything but N |
| Memory | one pool | lanes × pool, clamped from cgroup (§4.6) | N pools, no clamp (the live incident) |
| Lane accounting | 1 lane | explicit: children hold lanes; facts outranks queued shards | N lanes silently |
| Failure isolation / cursors | shared blob, shared crash | per-shard row, per-shard job | per clone |
| Resume / idempotency | per key, one writer | per key, one writer per row | per clone |
| Facts streaming | one stream | per child, deduped on the connection key | N ledgers (risk) |
| Fleet UX | one row | one row + shard rows | N rows + consolidate |
| Completeness | none | expected vs seen per shard | none |
| Operator controls per site | yes | yes (fan out to children) | per clone |

## 4. Design (option B)

### 4.1 Unit of parallelism and the plan
A **shard** is a scheduling unit: a list of `DriveTarget`s (delta units) plus optional
`exclude_prefixes`. State stays keyed by delta unit (`state_key`), never by shard, so a
re-plan that regroups folders never orphans a cursor.

Planner (in the parent job, `run_builtin_crawl`):
1. Resolve confirmed (or `payload["scopes"]`-selected) scopes to targets as today.
2. One `search_document_count` per drive root (`graph_client.py` 442-497, `min_modified`
   aware). If the site total ≤ `shard_target_docs`, or the backend is DuckDB, or the
   knob is 0 → **inline path, byte-for-byte today's crawl** (golden tests unchanged).
3. For each drive over the target: list root children with `webUrl` (413-439), count
   each folder (8 in flight, as the split planner does at 2336), fold a folder still over
   the target ONE level deeper (new `list_item_children_with_url`), then pack folders
   into `K = min(ceil(total / target), 32)` groups with the existing
   `pack_folders_into_groups`. Add one **remainder shard** per drive: the drive root
   target (`root_item_id=None`, `state_key = drive_id`) with `exclude_prefixes` = every
   folder path owned by a group — it covers loose root files and any top-level folder
   created after planning, via the same `folder_prefixes` skip `_process_item` already
   applies (3859, `_under_prefix` 2202). Its first enumeration pays metadata pages only.
4. Signal fallback: Search unavailable (all counts 0) → `folder.childCount`; both absent
   → one shard per top-level folder (capped at 32). The plan records `signal`.
5. Persist `shard_plan` in the connection's `crawl` state row; re-plan only on `resync`,
   on a scope-set change, or when no plan exists.

### 4.2 Data model (PG-only, one Alembic revision `0103_crawl_shards` — renumbered
from `0102` when a competing `0102_merge_users_kind_fts` landed on the
vehicle first; see the plan's Task 2 note)
- `extraction_runs` += `parent_run_id TEXT NULL` (index `idx_extraction_runs_parent`),
  `shard_key TEXT NULL`, `shard_label TEXT NULL`, `shards_total INT NULL`,
  `shards_done INT NOT NULL DEFAULT 0`. Mirror in `src/models/extraction.py`.
- `sharepoint_connection_state`: relax `ck_sharepoint_connection_state_kind` to
  `kind IN ('crawl','facts') OR kind LIKE 'crawl:%'`. One row per delta unit:
  `kind = 'crawl:<state_key>'`, payload = today's shape (`delta_links` with one key,
  `ctags`, `failed_items`, `empty_items`), written only by the child that owns it.
  The `crawl` row keeps `shard_plan`, `last_run`, and its legacy maps.
- Legacy seed: a child consults the connection row's `ctags` READ-ONLY when its own row
  has no entry, so sharding an already-crawled drive re-enumerates but does not
  re-download unchanged files; the finalizer clears the legacy `ctags` after the first
  fully-done sharded run. Legacy `failed_items`/`empty_items` are re-homed by the
  planner into the shard row whose prefix matches `entry["path"]`.
- `state_store.get/put` accept `crawl:` kinds only on Postgres (`file_state_path`
  already raises on an unknown kind); DuckDB instances never shard.
- `docs/migrations.md`'s append-only `shipped_revision_ids.txt` gets the new id.

### 4.3 Jobs and lifecycle
- New kind `corpus-extraction-shard` (EXTRACTION lane, `retry_in_seconds=None`, reclaim
  budget 25 — same as its parent; added to `_INJECT_JOB_ID_KINDS` and
  `_EXTRACTION_RUN_OWNING_KINDS`). Payload: `connection_id, parent_run_id, shard_index,
  shard{targets, exclude_prefixes, scope_id, expected}` + pass-through of `concurrency`,
  `timeout_s`, `force_reprocess`, `retry_failed`, `retry_empty`. Idempotency key
  `corpus-extraction-shard:{connection_id}:{shard_index}`; **priority −1** so a queued
  facts pass (priority 0) is claimed before a queued shard (`jobs_pg.py` 288).
- Parent job: `_clear_stale_stop` once, plan, `repo.start(phase="plan",
  shards_total=K)`, enqueue children, return `{"mode":"sharded","parent_run_id",…}`.
  It does not wait — no lane is held by a waiting parent.
- Child: the crawl body of `_run_crawl_async` factored into `_crawl_targets(...)`, run
  over the shard's targets with per-target state rows; its own `_RunRecorder` (child rows
  never sweep `abandon_stale_running`; each checkpoint also bumps the parent's
  `checkpoint_at`); never clears the stop flag; `timeout_s` applies per child. On exit it
  finishes its row, then `finish_shard(parent)` (atomic `shards_done + 1 RETURNING`); the
  child that observes `shards_done == shards_total` finalizes.
- Finalize (idempotent, single winner via `UPDATE … SET phase='finalizing' WHERE phase
  <> 'finalizing' RETURNING id`): aggregate child reports (sum counters, concat capped
  lists, `scope_errors`; status = `failed` if any child failed, else `interrupted` if any
  interrupted, else `done`), write `last_run`, run the chained facts pass with a FRESH
  `_Deadline(extraction.facts.run_timeout_s)` against the parent recorder (phase `facts`),
  finish the parent. A parent left `running` with every child terminal is finalized by the
  next `POST …/extract` instead of re-planned.
- Completion = all children terminal. A dead child's job fails through the existing
  reclaim budget and `fail_for_job` closes its row; the parent then finalizes as `failed`
  on the last live child, naming the shard.

### 4.4 Operator semantics per site
- `POST …/extract`: 409 `extraction_already_running` while a top-level run row is
  `running` (the job key no longer covers the shards' lifetime); body gains
  `shards: [int]` to re-run named shards from the persisted plan (replaces "re-run one
  clone"). `resync` drops every `crawl:*` row's cursors and the plan; `force_reprocess`,
  `retry_failed`, `retry_empty`, `concurrency`, `timeout_s`, `min_modified` fan out
  unchanged to every child (`retry_failed`/`retry_empty` replay each shard row's own
  backlog, which `_retry_failed_items` already filters by `state_key`).
- Stop: one flag, every child stops at its next boundary; a child claimed after the flag
  exits immediately as `stopped`. Run-due sweep and scheduler: unchanged.

### 4.5 Facts
Streaming: each child calls `_maybe_stream_facts_extraction` on its own counters; the
connection-keyed dedup collapses K triggers into one queued pass. Chained: once, by the
finalizer (§4.3). The advisory lock and the per-document ledger stay per connection —
one collection, one ledger, no double extraction.

### 4.6 Throttling and memory (no new knobs)
- In flight against the tenant ≤ `extraction.concurrency` × per-job cap; every child keeps
  its own AIMD governor, so tenant pushback still halves each child's target.
- Memory clamp: `cap = floor(cgroup_limit × 0.8 / (lanes × 2 GiB))` (cgroup v2
  `memory.max`, else v1 `memory.limit_in_bytes`; `"max"`/absent → no clamp; Linux only),
  applied in `_resolve_concurrency`, reported as `concurrency.source = "memory_budget"`
  and logged once per run. 2 GiB is the live per-in-flight-file figure the crawler's own
  constants cite (`crawler.py` 152-156).

### 4.7 API, UI, CLI
- New `GET …/shard-plan` (preview: `mode`, `target_docs`, `signal`, `shards[]`, per-shard
  `expected`, `loose_root_files`); `GET …/split-plan` becomes a thin alias and is
  documented as deprecated. `POST …/splits` stays as an escape hatch, marked deprecated
  (CHANGELOG + `Deprecation: true` header), slated for removal after one release.
- Run projections (`_run_out`, fleet, status, history, detail) gain additive keys:
  `mode`, `shards_total`, `shards_done`, `expected_documents`, `seen_documents`, and (fleet
  + detail) `shards[] {index,label,outcome,files_done,files_seen,expected,checkpoint_at,
  error}`. Child rows never appear as top-level rows (`get_running`, `list_latest_…`,
  `list_for_connection`, `count_for_connection`, `last_completed/failed` filter
  `parent_run_id IS NULL`).
- Fleet view: one row per site with a "3/8 shards" badge and a disclosure listing shard
  rows (stuck flag per shard). Source card Run row: "running · 3/8 shards · N files";
  the "Split this site…" control becomes "Parallel crawl — preview shards" (read-only).
  CLI: `agnes admin sharepoint shard-plan` (alias `split-plan`).
- Completeness: per shard, `expected` (plan count) vs `seen = new+changed+unchanged`
  (+`filtered_by_age`), rendered as "≈ expected", never as a percentage; omitted for
  anonymized scopes only where a path prefix would be needed (it is not — counters
  suffice).

### 4.8 Config knobs
Exactly one: `extraction.crawler.shard_target_docs` (default 5000; 0 = never shard, i.e.
today's behaviour). Code constants: `_MAX_SHARDS = 32`, `_SHARD_SPLIT_DEPTH = 2`,
`_MEMORY_RESERVE_PER_ITEM_BYTES = 2 GiB`, `_MEMORY_HEADROOM = 0.8`.

## 5. Migration path for an instance with manual splits
1. Nothing breaks on upgrade: clones stay independent connections; each auto-shards if
   large; the original connection's whole-drive cursor seeds its remainder shard.
2. Recommended fold-back, in this order: delete the clone connections (their scopes go
   with them), `POST …/collections/consolidate` on the original to fold the orphaned
   per-folder collections into its own collection (the endpoint already repoints the
   caller's scopes), then `POST …/extract` — the planner shards; already-ingested
   documents upsert to a no-op on `(collection, stable_id)` (`crawler.py` 2348-2358).
3. The UI's split control is replaced; consolidate stays reachable as the migration aid.

## 6. Risks
- **Item moved between shards mid-run**: shard A's delete can land after shard B's add.
  Mitigation: a folder shard applies a `deleted` row only when the item's cTag is in ITS
  own row; the residual race surfaces in the completeness delta. Documented.
- **Search counts are approximate** (index lag, `IsDocument` vs convertible): the plan
  balances load, it does not promise totals; the UI says "≈".
- **Remainder shard's first enumeration** pages the whole drive once (metadata only).
- **Finalizer dies mid-way**: parent stalls; the next trigger finalizes (§4.3).
- **K pools of spares**: each child pre-forks `2 × cap` idle spares; the clamp bounds
  `cap`, not spares — spares are import-only and were sized for that (`crawler.py`
  269-288).
- **DuckDB backend**: unchanged, sequential; the feature is PG-only by construction.
- **`abandon_stale_running` semantics change** for children; covered by tests in the plan.
