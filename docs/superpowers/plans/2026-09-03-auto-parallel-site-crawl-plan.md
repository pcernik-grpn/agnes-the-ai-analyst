# Automatic parallel site crawl — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or
> superpowers:executing-plans, one task at a time; `verify-agnes-change` before claiming any
> task done. Every decision is made in the spec — no "ask the user" step.

**Goal:** One connection, one site: the `corpus-extraction` job plans K shards and fans out
`corpus-extraction-shard` child jobs that write into the scope's own collection; one parent
run row rolls them up; the manual split/consolidate workflow becomes unnecessary.

**Spec:** `docs/superpowers/specs/2026-09-03-auto-parallel-site-crawl-design.md`

## Global constraints
- PG-only feature (A3 ratchet): DuckDB instances keep today's inline crawl byte-for-byte;
  `tests/test_sharepoint_crawler.py` must stay green untouched through Task 4.
- No new knobs beyond `extraction.crawler.shard_target_docs`; everything else is a named
  constant with a docstring stating the live finding it comes from.
- Vendor-agnostic wording in code, docs and tests (no customer names/hostnames).
- CHANGELOG `## [Unreleased]` bullet lands in Task 11; `docs/migrations.md` rules for the
  revision (append `shipped_revision_ids.txt`, models mirror the DDL).
- Per task: run the named tests; final gate `scripts/verify_syncmap.py`.
- **Merge magnets** (other builders are active on them — keep diffs surgical, append new
  sections at file ends, never reflow): `connectors/sharepoint/crawler.py`,
  `app/api/admin_sharepoint.py`, `app/web/static/js/admin/data_sources_page.js`.

## Task order and dependency graph
1 → 2 → 3 → 4 → 5 → 7 → 8 → 9 → 10 → 11, with 6 independent after 1. Runtime (1-7) lands
before any read-side/UI work; a release can ship after Task 7 with the feature dark
(`shard_target_docs: 0`).

---

### Task 1 — Factor the crawl body so a caller can hand it explicit targets
**Files:** `connectors/sharepoint/crawler.py` (magnet — new helpers appended after
`_run_crawl_async`, minimal edits inside it); `tests/test_sharepoint_crawler.py`.
**Steps:**
- Extract lines 5452-5510 of `_run_crawl_async` into `async def _crawl_targets(connection,
  *, targets_by_scope, state_for, stats, recorder, …)` where `state_for(target) -> dict`
  returns the state dict a target crawls with and `save_for(target)` persists it; the
  inline path passes the single connection-level state for every target (today's
  behaviour). Thread a `shard_exclude_prefixes: Sequence[str]` into `_ScopeContext`
  merged into `_ExclusionIndex.folder_prefixes` (respected at 3859 already).
- Add `legacy_ctags: Mapping[str,str]` to `_ScopeContext` and consult it in the
  `already` check at 3888-3889 (`ctags.get(id) or ctx.legacy_ctags.get(id)`); on success
  the item's cTag is written to the active state only (unchanged code).
- `_RunRecorder.__init__` gains `sweep_stale: bool = True`; `_run_crawl_async` gains
  `clear_stale_stop: bool = True`. Defaults preserve today's behaviour.
- Folder-shard delete guard: in `_process_item`'s `deleted` branch, skip when
  `target.root_item_id` is set and `stable_id not in ctags` (spec §6).
**Tests:** existing suite green; new: a run over explicit `[DriveTarget(folder)]` with
`shard_exclude_prefixes` skips the excluded subtree and persists only that key's
deltaLink; legacy cTag fallback counts `unchanged` without downloading.
**Acceptance:** `git diff --stat` on `crawler.py` shows no change to the report contract.

### Task 2 — Per-delta-unit state rows + Alembic revision `0103_crawl_shards`
**Files:** `migrations/versions/0103_crawl_shards.py` (`down_revision =
"0102_merge_users_kind_fts"` — the vehicle's head as of 2026-09-03, after
`origin/main` merged in a competing `0102_merge_users_kind_fts` revision;
re-check `alembic heads` before writing this file, it may have moved again),
`migrations/shipped_revision_ids.txt`,
`src/models/extraction.py`, `src/models/sharepoint_state.py`,
`src/repositories/sharepoint_state_pg.py` (+`list_kinds(connection_id, prefix)`),
`src/repositories/extraction_runs_pg.py` (columns only; methods in Task 4),
`connectors/sharepoint/state_store.py`, `connectors/sharepoint/crawler.py`
(`load_state/save_state(connection_id, shard_key=None)` → kind `crawl:<key>`;
`_apply_resync` iterates `list_kinds(…, "crawl:")`).
**Tests:** `tests/db_pg/` migration up/down round trip; `state_store` refuses a `crawl:`
kind on DuckDB with `StateStoreError`; resync clears every shard row's `delta_links` and
`failed_items` but keeps `ctags`; a pure `rehome_legacy_backlog(legacy, plan)` moves
`failed_items` by path prefix.
**Acceptance:** `alembic heads` shows one head; models match DDL (autogenerate is empty).

### Task 3 — Shard planner
**Files:** new `connectors/sharepoint/shard_plan.py` (pure: `plan_shards(drive_units,
*, target_docs, max_shards=32)` reusing `site_split.pack_folders_into_groups`; Graph half
`compute_shard_plan(transport, auth, scope, targets, *, min_modified)`),
`connectors/sharepoint/graph_client.py` (+`list_item_children_with_url`), new
`tests/test_sharepoint_shard_plan.py`.
**Rules (from spec §4.1):** drive total ≤ target → one whole-drive shard; else folders,
one level deeper for folders > target, K = min(ceil(total/target), 32), plus one
remainder shard per drive with `exclude_prefixes`; signal fallback
`search → child_count → one-per-folder`; `expected` per target and per shard.
**Tests:** every folder appears in exactly one shard; remainder excludes exactly the
grouped paths; a huge folder is split one level and never deeper; zero-count fallback;
K never exceeds 32; plan is JSON-serializable and stable across two calls on same input.
**Acceptance:** no I/O in the pure module; Graph half exercised with `httpx.MockTransport`.

### Task 4 — Child job kind, parent orchestration, finalizer
**Files:** `connectors/sharepoint/crawler.py` (append: `run_shard_crawl(payload)`,
`_plan_or_run_inline`, `_finalize_site_run`, `_aggregate_child_reports`), `app/worker/kinds.py`
(register `corpus-extraction-shard`, `_INJECT_JOB_ID_KINDS`), `app/worker/runtime.py`
(`_EXTRACTION_RUN_OWNING_KINDS`), `app/worker/registry.py` (`JOB_MAX_ATTEMPTS_BY_KIND`),
`src/repositories/extraction_runs_pg.py` (`start(parent_run_id=, shard_key=, shard_label=,
shards_total=)`, `children_for(parent_ids)`, `finish_shard(parent_id) -> (done,total)`,
`bump_parent_checkpoint`, `claim_finalize(parent_id) -> bool`, top-level filters
`parent_run_id IS NULL` on `get_running/list_latest_for_connections/list_for_connection/
count_for_connection/last_completed/last_failed`), `tests/test_sharepoint_crawler.py`,
`tests/test_extraction_runs_pg.py` (or the existing PG repo test file).
**Steps:** `run_builtin_crawl` → estimate → inline (unchanged) or plan + parent row +
enqueue K children (priority −1, idempotency `corpus-extraction-shard:{id}:{i}`, options
pass-through) → return. `run_shard_crawl` → refuse if stop flag set (records `stopped`),
`_crawl_targets` over the shard with per-target state rows and `legacy_ctags`, child
recorder (`sweep_stale=False`, parent bump), `_maybe_stream_facts_extraction` unchanged,
`finish_shard`, last child → `_finalize_site_run` (aggregate, `last_run`, facts pass with
`_Deadline(extraction.facts.run_timeout_s)`, parent finish, clear legacy `ctags` once).
**Tests:** planner path enqueues K jobs and returns without crawling; two children with
disjoint folders each write only their own state row; last-child finalize runs exactly
once under a simulated race (`claim_finalize`); a failed child makes the parent `failed`
naming the shard; stop flag set before claim → child records `stopped`; parent
`checkpoint_at` advances on child checkpoints; `fail_for_job` on a child closes only that
row.
**Acceptance:** inline path golden tests unchanged; new tests green on PG fixtures.

### Task 5 — Trigger and per-site operator semantics
**Files:** `app/api/admin_sharepoint.py` (magnet — touch only `trigger_extraction`,
`retry_empty_extraction`, `ExtractionRunOptions`), `tests/test_admin_data_sources_extraction.py`.
**Steps:** 409 `extraction_already_running` when `extraction_runs_repo().get_running(id)`
(top-level) exists — guarded by `RequiresPostgresBackend` fallback to today's check;
`shards: List[int]` option → payload; document `resync` re-plan and per-child `timeout_s`
in the docstring; `retry-empty` passes through unchanged.
**Tests:** 409 while a parent runs; `shards` narrows to the named indices (planner test);
DuckDB instance path unchanged.

### Task 6 — Memory-budget clamp on per-job concurrency (independent after Task 1)
**Files:** `connectors/sharepoint/crawler.py` (append `_cgroup_memory_limit_bytes()`,
`_memory_budget_cap()`; `_resolve_concurrency` applies it; `CrawlStats.concurrency_source`
may be `"memory_budget"`), `tests/test_sharepoint_crawler.py`.
**Tests:** fake cgroup v2/v1 files via `monkeypatch` + `tmp_path`; `"max"` → no clamp;
`lanes=4, limit=32 GiB` → cap 3; the clamp never raises a configured cap; report names
the source; non-Linux → no-op.

### Task 7 — Lane priority + facts interplay
**Files:** `connectors/sharepoint/crawler.py` (enqueue priority constant), `tests/test_facts_extraction.py`
or `tests/test_sharepoint_crawler.py`.
**Tests:** with one queued shard (−1) and one queued facts job (0), `claim_next` returns
the facts job first (PG fixture); K children streaming collapse onto one deduped facts
job; the chained pass runs once, from the finalizer, with its own deadline.
**Acceptance:** release-able dark: `shard_target_docs: 0` makes Tasks 4-7 unreachable.

### Task 8 — Read side: rollups in `admin_extraction.py`
**Files:** `app/api/admin_extraction.py` (`_run_out` additive keys `mode`, `shards_total`,
`shards_done`, `expected_documents`, `seen_documents`; new `_rollup_children(parent,
children)`; fleet endpoint fetches `children_for(parent_ids)` in ONE query; detail endpoint
includes `shards[]`), `tests/test_admin_extraction.py`, `tests/test_admin_extraction_fleet_view.py`.
**Tests:** parent counters = sum of children; `stuck` per shard and on the row; a child
never appears as a fleet row; inline runs unchanged (snapshot of existing expectations).

### Task 9 — Preview endpoint, deprecations, CLI
**Files:** `app/api/admin_sharepoint.py` (magnet — append `GET …/shard-plan`; `split-plan`
delegates; `POST …/splits` adds `Deprecation: true` header), `cli/commands/admin_sharepoint.py`
(`shard-plan` command; `split-plan` kept as alias), `tests/test_sharepoint_site_split.py`
(+ endpoint tests file used by the split work).
**Acceptance:** `shard-plan` returns spec §4.7 shape; old callers of `split-plan` see an
unchanged response plus `mode`.

### Task 10 — UI: fleet shard rows + source card + retire the split control
**Files:** `app/web/templates/admin_extraction.html` (badge + disclosure row per site,
`renderShardRow`), `app/web/static/js/admin/data_sources_extraction_observability.js`
(`_extRunRowHtml`: "k/K shards" line; run detail lists shards),
`app/web/static/js/admin/data_sources_page.js` (magnet — replace the split control
with "Parallel crawl — preview shards" calling `shard-plan`; drop `applySpSplit`),
`tests/test_admin_extraction_fleet_view.py`, `tests/test_admin_data_sources_extraction.py`.
**Tests (node-extracted, existing pattern):** row shows "3/8 shards"; shard row renders
`≈ expected`, outcome, stuck; no "Create N connections" button remains in the template.

### Task 11 — Docs, config example, CHANGELOG
**Files:** `docs/sharepoint-extraction.md` (replace the "Split one large site" recipe
with the automatic behaviour + migration path §5), `docs/api-reference.md`,
`config/instance.yaml.example` (`shard_target_docs` next to `crawler.concurrency`),
`CHANGELOG.md` (one Added bullet, one Deprecated bullet for `splits`/`split-plan`).
**Acceptance:** `scripts/verify_syncmap.py` clean; no customer-specific wording.

## Out of scope
Cross-shard load rebalancing while running; sharding for non-SharePoint sources; removing
`POST …/splits` (deprecated now, deleted in a later release); DuckDB support.
