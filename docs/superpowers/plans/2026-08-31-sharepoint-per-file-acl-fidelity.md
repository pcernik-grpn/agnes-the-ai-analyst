# SharePoint Per-File ACL Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Agnes itself enforce, end-to-end, that a user who cannot read a
file in SharePoint (via its folder-inheritance chain) can never reach that
file's content — document, chunks, claims, quotes — through Agnes, with
revocation windows measured in hours.

**Architecture:** Four moves on top of the existing ACL-mirroring machinery
(`connectors/sharepoint/acl_sync.py`): (1) a **server-side ingest gate** so the
exclusion list is enforced by Agnes at upload/ingest time instead of trusted to
the external producer; (2) a **retroactive cleanup** pass in the subtree sweep
that purges already-ingested content under newly excluded subtrees/files;
(3) **permission zones** — a broken-inheritance folder subtree is auto-promoted
to its own collection with its own mirrored ACL (opt-in switch), instead of
being invisible to everyone; (4) **hours-scale cadence** — ACL sync every N
hours (default 4) and the subtree sweep daily, both switch-tunable. Plus one
correctness fix found during research: Graph `/children` listings read only the
first page today, so large folders are partially swept.

**Tech Stack:** Python 3.11, FastAPI, httpx (MockTransport in tests), DuckDB +
Postgres dual-backend repos (pre-A3 pairs) with PG-only `corpus_file_sources`,
Microsoft Graph app-only API.

**Spec:** `docs/superpowers/specs/2026-08-28-sharepoint-acl-mirroring-design.md`
— this plan implements the MUST NOT branch (§1.2). The Q7 fork is now
**ratified as MUST NOT by the project owner (2026-08-31)** with an
hours-scale violation budget; `acl_sync.guarantee_mode` stays the runtime
switch (default `must_not`).

## Global Constraints

- Everything remains behind `acl_mirroring.enabled` (default `false`); zones
  additionally behind the new `acl_zones` switch (default `false`).
- **No new tables, no Alembic migration** — zone rows and exclusion entries
  live in `source_connections.config` (JSON), counters in audit + config; the
  DuckDB ladder is frozen (A3) and stays untouched.
- All sync writes stay sentinel-segregated: `ACL_SYNC_SENTINEL =
  "system:sharepoint-acl-sync"` on `user_groups.created_by` /
  `resource_grants.assigned_by`, `ACL_SYNC_SOURCE = "sharepoint_sync"` on
  memberships (`connectors/sharepoint/acl_sync.py:89-96`).
- Every new audit action: append-only entry in `src/audit_events.py` CATALOG
  (after line 1222, own comment block), emitted via `log_safe` only; every new
  route/job declares posture in `src/audit_posture.py`.
- Every task's PR adds a `## [Unreleased]` CHANGELOG bullet; never bump
  `pyproject.toml`; no AI attribution in commits.
- Vendor-agnostic: no customer names, tenants, or internal hostnames anywhere.
- Worker config writes go through `source_connections_repo().config_patch`,
  never wholesale `update` (race contract, `acl_sync.py:366-372`).
- The scheduler sidecar never imports `app.switches` — cadence helpers read
  `os.environ` then `app.instance_config.get_value` (pattern:
  `services/scheduler/__main__.py:355-390` `_extraction_schedule`).
- PG-only surfaces (`corpus_file_sources`) must fail soft on a DuckDB-backend
  instance: catch `RequiresPostgresBackend` and degrade (pattern:
  `app/api/collections.py:700`), never a raw 500.
- Graph mocking in tests: monkeypatch module-qualified `graph_client.*`
  functions and `graph_client._http_client` (idioms:
  `tests/test_sharepoint_acl_sync.py:27-193`,
  `tests/test_sharepoint_graph_client.py:120`, `:633` `_FakeGraphBatch`).
- Run per task: `python3 scripts/verify_syncmap.py --base HEAD`, then
  `.venv/bin/pytest tests/ connectors/ --lane impacted --tb=short -n auto -q`.
  Full lanes are CI's job (draft PR after first commit).

## Verified anchors used throughout (do not re-derive)

| thing | anchor |
|---|---|
| sweep walk + exclusion write | `connectors/sharepoint/acl_sync.py:755-828`, persist `:924-955` |
| scope sync (read→classify→resolve→diff→write) | `acl_sync.py:461-570` |
| grant reconciliation (adds before removes, sentinel-scoped) | `acl_sync.py:595-640` |
| staleness suspension | `acl_sync.py:643-697` |
| server-written config keys tuple | `acl_sync.py:98-124` + hand carry-forward `app/api/admin_source_connections.py:1157-1169` |
| scope rows written by wizard | `app/api/admin_sharepoint.py:977-983` (keys: `source_scope_id, display_path, anonymize, collection_id, access_mode, drive_id, include_excluded_subtrees[, audience_classes]`) |
| collection create | `app/api/admin_sharepoint.py:500-522` → `file_corpora_repo().create(name=…, slug=…, description=…, created_by=…)` returns `col_<hex>` |
| upload endpoint + per-file source metadata | `app/api/collections.py:1142` (`paths[]` = drive-relative, `source_stable_ids[]` = `graph:<driveItem-id>`) |
| upload preflight (pre-storage, read-only) | `app/api/collections.py:1065` `_preflight_source_anchored_batch` |
| facts ingest gate stack | `app/api/facts.py:676-678` (3 gates before `facts_repo().ingest_batch`) |
| file purge machinery | `app/api/collections.py:620` `_purge_file_row`, `:667` `_sweep_facts_orphans_after_delete`, `:700` `_purge_facts_claims_for_replaced_file` |
| corpus files repo | `src/repositories/corpus_files_pg.py`: `list_for_corpus(corpus_id)` `:134`, `get_by_path` `:111`, `delete` `:227` (DuckDB twin `src/repositories/corpus_files.py`) |
| source anchors repo (PG-only) | `src/repositories/corpus_file_sources_pg.py`: `resolve(corpus_id, stable_id)` `:31`, `get(corpus_file_id)` `:47` |
| grants repo | `src/repositories/resource_grants.py`: `ensure_grant` `:233`, `delete` `:272`, `delete_by_resource` `:280`, `list_all(resource_type=…)` `:38` |
| corpus map | `connectors/sharepoint/corpus_map.py:48-73` `_map_key`, `:76` `producer_corpus_map(scopes)` |
| producer env handoff | `app/worker/kinds.py:1365` `_excluded_subtree_scope_map`, `:1768-1770` env write |
| scheduler rows | `services/scheduler/__main__.py:522` `_ENQUEUE_BODIES`, `:718-725` acl row (`daily 06:00`), `:742-749` sweep row (`cron 0 7 * * 1`) |
| switch example (int) | `app/switches.py:802-816` `acl_max_stale_hours`; `switch_value` `:847` |
| job-kind registration | `app/worker/kinds.py:2089-2113`; exact-set pins `tests/test_worker_kinds.py:80-140` + `tests/test_ducklake_maintenance.py:137-153` |
| audit catalog block | `src/audit_events.py:1165-1222`; postures `src/audit_posture.py` (`POSTURE` `:57`, SharePoint block `:141-165`, `JOB_POSTURE` `:1367`) |
| admin trigger precedent | `app/api/admin_sharepoint.py:1261-1302` (acl-sync now) |
| route obligations | PG smoke `tests/db_pg/test_endpoints_smoke.py` (`COVERED_ROUTES` per class, SharePoint block `:3252-3316`); triple-surface `tests/test_documentation_api_triple_surface.py` (`_EXEMPT` `:804`, acl precedent `:1230-1234`); `docs/api-reference.md` path-verbatim (`tests/test_api_docs_coverage.py:61`); `make update-openapi-snapshot` → `tests/snapshots/openapi.json` |
| flag docs trio | `app/switches.py` description + `app/api/admin.py:1030-1053` `_KNOWN_FIELDS["acl_sync"]` + `docs/feature-flags.md:232-235` |
| scheduler pins | `tests/test_scheduler_sidecar.py:34` (sweep cron), `:551-603` (bodies/names) |
| feature-flag name sets | `tests/test_feature_flags.py:85` and `:120-124`, `:334-338` |

**Known behavioral facts the design leans on (verified):**
- `remove_scope` deletes only the bookkeeping row — collections and grants
  (including sentinel-owned) are orphaned (`admin_sharepoint.py:1062-1081`).
- `DELETE /api/collections/{id}` never deletes `resource_grants` rows
  (`collections.py:591-612`); `delete_by_resource` exists and must be called
  explicitly.
- Cross-collection "re-homing" via stable-id upsert is a **copy, not a move**
  — `corpus_file_sources.resolve` is corpus-scoped, so the old collection's
  row + chunks + claims survive untouched. Cleanup must delete parent-side
  copies explicitly.
- Exclusion entries today carry `{item_id, path, detected_at}` where `path`
  is display-style (breadcrumb-derived); upload `paths[]` are drive-relative.
  This plan adds `rel_path` (drive-relative) to every new exclusion entry —
  the gate matches only on `rel_path` and stable ids, never display paths.
- `producer_corpus_map` keys are `<site>` / `<site>/<folder-path-minus-library>`
  and the external resolver's longest-prefix behavior is **not** guaranteed —
  the ingest gate (Task 5) is what makes zone mis-routing fail closed.

---

### Task 1: Graph client — paged children listings (correctness fix)

**Files:**
- Modify: `connectors/sharepoint/graph_client.py:345-385`
- Test: `tests/test_sharepoint_graph_client_acl.py`

**Interfaces:**
- Consumes: existing `_graph_get_all_pages(access_token, path, *, params)` (`graph_client.py:706`).
- Produces: `list_root_children` / `list_item_children` with identical signatures and item shape (`{id, name, is_folder, child_count}`) but complete across `@odata.nextLink` pages. Every later task assumes children listings are complete.

Graph pages `/children` (default ~200 items). Today both listing functions
read a single page (`_graph_get` + `_map_children(body)`), so the subtree
sweep silently skips children past the first page — a broken-inheritance
folder there is never probed and never excluded. That violates MUST NOT on
any folder with >200 children.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sharepoint_graph_client_acl.py`, following the existing
URL-fragment dict-transport idiom in that file (`:18-41`):

```python
def test_list_item_children_follows_next_link(monkeypatch):
    pages = {
        "/items/root-1/children": {
            "value": [{"id": "f1", "name": "A", "folder": {"childCount": 0}}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/drives/d1/items/root-1/children?$skiptoken=p2",
        },
        "$skiptoken=p2": {
            "value": [{"id": "f2", "name": "B", "file": {}}],
        },
    }
    _install_pages(monkeypatch, pages)  # the module's existing fragment-matching transport helper
    items = asyncio.run(gc.list_item_children("tok", "d1", "root-1"))
    assert [i["id"] for i in items] == ["f1", "f2"]
    assert items[0]["is_folder"] is True and items[1]["is_folder"] is False
```

(If the file's helper has a different name, reuse it verbatim — do not add a
second transport shim. Mirror the same test for `list_root_children`.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_sharepoint_graph_client_acl.py -v -k next_link`
Expected: FAIL — second page's `f2` missing.

- [ ] **Step 3: Implement paging**

In `graph_client.py`, add a row-level mapper next to `_map_children` and
switch both listing functions to `_graph_get_all_pages` with `$top=999`:

```python
def _map_child_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Row-level twin of _map_children for callers that already collected
    pages — same {id, name, is_folder, child_count} shape."""
    return _map_children({"value": rows})


async def list_item_children(access_token: str, drive_id: str, item_id: str) -> List[Dict[str, Any]]:
    rows = await _graph_get_all_pages(
        access_token,
        f"/drives/{drive_id}/items/{item_id}/children",
        params={"$select": "id,name,folder,file", "$top": "999"},
    )
    return _map_child_rows(rows)
```

(`list_root_children` gets the identical change on the `/root/children` path.
`_graph_get_all_pages` is defined later in the module — Python resolves at
call time; keep docstrings, add one line noting completeness matters to the
sweep.)

- [ ] **Step 4: Run the module's tests**

Run: `.venv/bin/pytest tests/test_sharepoint_graph_client_acl.py tests/test_sharepoint_graph_client.py tests/test_sharepoint_subtree_sweep.py -q`
Expected: PASS (existing single-page fakes still satisfy paging — no nextLink → one page).

- [ ] **Step 5: CHANGELOG + commit**

Add under `## [Unreleased]` → `### Fixed`:
`- SharePoint tree listings now follow Graph paging — folders with more than ~200 children were only partially browsed and, in the ACL subtree sweep, only partially probed for broken permission inheritance.`

```bash
git add connectors/sharepoint/graph_client.py tests/test_sharepoint_graph_client_acl.py CHANGELOG.md
git commit -m "fix(sharepoint): page /children listings — large folders were partially swept"
```

---

### Task 2: Switches + scheduler — hours-scale cadence

**Files:**
- Modify: `app/switches.py` (registry, after `:816`), `app/api/admin.py:1030-1053` (`_KNOWN_FIELDS["acl_sync"]`), `services/scheduler/__main__.py` (`:718-749`), `docs/feature-flags.md:232-235`, `docs/jobs-classification.md`
- Test: `tests/test_feature_flags.py`, `tests/test_switches.py`, `tests/test_scheduler_sidecar.py`, `tests/test_admin_configure_api.py`

**Interfaces:**
- Produces: `switch_value("acl_sync_interval_hours") -> int` (default 4), `switch_value("acl_zones") -> bool` (default False), `switch_value("acl_sweep_interval_days")` default changed 7 → 1. Tasks 3–7 read `acl_zones`; nothing else changes callers.

- [ ] **Step 1: Failing tests first**

In `tests/test_feature_flags.py`, add `"acl_sync_interval_hours"` and
`"acl_zones"` to the exact-name-set assertions (`:85` block and the sets at
`:120-124` / `:334-338`). In `tests/test_scheduler_sidecar.py`, change the
pinned expectations: sweep schedule from `"cron 0 7 * * 1"` to
`"daily 07:00"` (`:34`) and the acl row from `"daily 06:00"` to
`"every 4h"`, plus a new test:

```python
def test_acl_sync_schedule_reads_interval(monkeypatch):
    monkeypatch.setenv("AGNES_ACL_SYNC_INTERVAL_HOURS", "2")
    jobs = _build_jobs_by_name()   # the file's existing helper for build_jobs()
    assert jobs["sharepoint-acl"] == "every 2h"
```

Run: `.venv/bin/pytest tests/test_feature_flags.py tests/test_scheduler_sidecar.py -q` — expected FAIL.

- [ ] **Step 2: Register the switches**

Append after `acl_max_stale_hours` (`app/switches.py:816`), before
`acl_sweep_interval_days`:

```python
Switch(
    name="acl_sync_interval_hours",
    config_keys=("acl_sync", "interval_hours"),
    env_var="AGNES_ACL_SYNC_INTERVAL_HOURS",
    kind="int",
    default=4,
    effect="restart",
    category="product",
    editable=True,
    description=(
        "Hours between SharePoint ACL sync runs (scope + zone root permission "
        "re-reads). The source-side revocation window is at most this long. "
        "Read by the scheduler sidecar at startup."
    ),
),
Switch(
    name="acl_zones",
    config_keys=("acl_sync", "zones_enabled"),
    env_var="AGNES_ACL_ZONES_ENABLED",
    kind="bool",
    default=False,
    effect="live",
    category="product",
    editable=True,
    description=(
        "Promote broken-inheritance SharePoint subtrees to their own "
        "collections with their own mirrored ACLs (permission zones) instead "
        "of excluding them from the crawl entirely. Requires acl_mirroring."
    ),
),
```

Change `acl_sweep_interval_days` default `7` → `1` and extend its description
with: `"Daily by default so a newly broken-inheritance subtree is detected "
"within ~a day (MUST NOT posture); raise it if the Graph probe budget on a "
"very large tenant becomes a problem."`

- [ ] **Step 3: Admin panel fields**

In `app/api/admin.py` `_KNOWN_FIELDS["acl_sync"]` (`:1030-1053`) add hints for
`interval_hours`, `zones_enabled` (declare `{"kind": "bool"}` per the
`test_admin_configure_api.py:1169` bool-declaration ratchet), **and** the
already-missing `sweep_interval_days` (research found it editable but
panel-less — fix that inconsistency here).

- [ ] **Step 4: Scheduler helper + rows**

In `services/scheduler/__main__.py`, add next to `_extraction_schedule`
(`:355`):

```python
def _acl_sync_schedule() -> str:
    """every-Nh row for sharepoint-acl-sync. Env > instance.yaml > default 4.
    Mirrors the acl_sync_interval_hours switch without importing app.switches
    (the sidecar reads env + get_value only). Deliberately NOT in the
    smallest-tick min() — hour-grain needs no tick constraint."""
    from app.instance_config import get_value

    raw = os.environ.get("AGNES_ACL_SYNC_INTERVAL_HOURS") or get_value("acl_sync", "interval_hours", default=4)
    try:
        hours = max(1, int(raw))
    except (TypeError, ValueError):
        hours = 4
    schedule = f"every {hours}h"
    return schedule if is_valid_schedule(schedule) else "every 4h"
```

Replace the acl row's `"daily 06:00"` with `_acl_sync_schedule()` (`:718-725`,
keep the enqueue body) and the sweep row's `"cron 0 7 * * 1"` with
`"daily 07:00"` (`:742-749`); rewrite both rationale comments (the drift
window is now the interval; the sweep's per-connection
`acl_sweep_interval_days` due-guard still prevents restart-refire).

- [ ] **Step 5: Docs + run**

`docs/feature-flags.md`: two new rows next to `:232-235`, update the
`acl_sweep_interval_days` default cell to `1`. `docs/jobs-classification.md`:
update the two SharePoint rows' cadence text.

Run: `.venv/bin/pytest tests/test_feature_flags.py tests/test_switches.py tests/test_scheduler_sidecar.py tests/test_admin_configure_api.py -q`
Expected: PASS.

- [ ] **Step 6: CHANGELOG + commit**

`### Changed` bullet:
`- SharePoint ACL sync now runs every N hours (acl_sync.interval_hours, default 4) and the broken-inheritance sweep daily (acl_sync.sweep_interval_days default lowered 7 → 1) — source-side revocation and detection windows are hours-scale. New acl_sync.zones_enabled switch (default off) prepares permission zones.`

```bash
git add app/switches.py app/api/admin.py services/scheduler/__main__.py docs/feature-flags.md docs/jobs-classification.md tests/test_feature_flags.py tests/test_scheduler_sidecar.py CHANGELOG.md
git commit -m "feat(sharepoint): hours-scale ACL cadence switches + daily sweep"
```

---

### Task 3: Sweep v2 — file probes, drive-relative paths, permission zones

**Files:**
- Modify: `connectors/sharepoint/acl_sync.py` (walk `:755-828`, `_sweep_scope` `:831-868`, `_sweep_connection` `:871-957`, config-keys tuple `:98-124`), `app/api/admin_source_connections.py:1157-1169` (carry-forward), `src/audit_events.py` (append after `:1222`), `src/audit_posture.py` (no new keys — job postures exist)
- Test: `tests/test_sharepoint_subtree_sweep.py`

**Interfaces:**
- Consumes: Task 1's complete children listings; `switch_value("acl_zones")`.
- Produces:
  - exclusion entries now `{item_id, path, rel_path, kind, detected_at}` with `kind ∈ {"folder","file"}` (legacy entries without `kind`/`rel_path` are read as folders with no rel-path match — treat-missing-as-legacy everywhere);
  - `config["acl_zones"]: list[dict]` — `{zone_item_id, parent_scope_id, drive_id, name, display_path, rel_path, collection_id, detected_at, status}` with `status ∈ {"active","dissolved"}`;
  - helper `zone_rows(connection) -> list[dict]` and `active_zone_rows(connection) -> list[dict]` exported from `acl_sync.py` (used by Tasks 4, 5, 6, 7);
  - helper `scope_rel_root(display_path: str) -> str` returning the scope root's drive-relative prefix (`""` for a site/drive/library-root scope).

**Semantics locked here:**
- The walk now probes **files as well as folders** in the same `$batch` flow.
- `flag is None` (unknown) stays **excluded** (fail-closed), never zoned.
- A folder with `flag is True`: if `acl_zones` is on → becomes/refreshes an
  **active zone** and the walk **descends into it** (nested breaks become
  further zones); if off → excluded, no descend (today's behavior).
- A file with `flag is True` → excluded entry `kind="file"`, always (no
  single-file zones — YAGNI; counted, visible, fail-closed).
- A known active zone whose root now probes `False` → `status="dissolved"`
  (Task 6 purges it). Probe `None` on a known zone root → stays active
  (unknown never dissolves).
- Zone collection creation is idempotent on `zone_item_id`; name/slug derive
  from the parent scope's collection plus the folder name; grants start empty
  (invisible to everyone until Task 4's sync mirrors the zone ACL — that
  ordering is what keeps zone creation MUST-NOT-safe).
- Zones are written to the **top-level `acl_zones` key via `config_patch`**,
  never into `scopes` — the sweep must not become a second automated writer
  of `scopes` rows (race contract at `acl_sync.py:909-921`; the per-scope
  `excluded_subtrees` write inside `scopes` already exists and stays).

- [ ] **Step 1: Failing tests**

Extend `tests/test_sharepoint_subtree_sweep.py` (reuse `sweep_env` `:22` and
the tree-fake idiom `:125`); new tests:

```python
def test_file_with_unique_permissions_is_excluded(sweep_env, monkeypatch):
    # tree: root -> [folder A (inherits), file F (unique)]
    # expect: excluded_subtrees contains {"item_id": "F", "kind": "file",
    #         "rel_path": "F.docx", ...}; folder A still descended.

def test_folder_break_becomes_zone_when_switch_on(sweep_env, monkeypatch):
    monkeypatch.setenv("AGNES_ACL_ZONES_ENABLED", "true")
    # tree: root -> folder Z (unique) -> child folder (inherits)
    # expect: no excluded entry for Z; config["acl_zones"] has one active row
    #         with a fresh col_ id; file_corpora_repo().get(collection_id) exists;
    #         the walk DID descend into Z (fake asserts child listed);
    #         audit rows: exactly one sharepoint_acl.zone_created.

def test_zone_dissolves_when_inheritance_relinks(sweep_env, monkeypatch):
    # pre-seed an active zone row for Z in config; probe now returns False for Z
    # expect: status flipped to "dissolved"; one sharepoint_acl.zone_dissolved.

def test_unknown_probe_never_creates_zone(sweep_env, monkeypatch):
    monkeypatch.setenv("AGNES_ACL_ZONES_ENABLED", "true")
    # probe returns None for folder U -> excluded (kind "folder"), no zone row.

def test_rel_path_is_drive_relative(sweep_env, monkeypatch):
    # scope display_path "Site/Documents/Team", excluded subtree at walk path
    # "Sub" -> rel_path == "Team/Sub" (library segment dropped, root prefix kept).
```

Run: `.venv/bin/pytest tests/test_sharepoint_subtree_sweep.py -q` — expected FAIL.

- [ ] **Step 2: `scope_rel_root` helper**

In `acl_sync.py` (near the naming helpers `:127-139`):

```python
def scope_rel_root(display_path: str) -> str:
    """Drive-relative prefix of a scope root, derived from the wizard
    breadcrumb the same way corpus_map._map_key does: segments are
    slash-split and stripped; segment[0] is the site, segment[1] the
    document library (both absent from drive-relative paths). "Site" or
    "Site/Documents" -> ""; "Site/Documents/Team/Sub" -> "Team/Sub"."""
    segments = [s.strip() for s in (display_path or "").split("/") if s.strip()]
    return "/".join(segments[2:])
```

- [ ] **Step 3: Walk changes**

Rework `_walk_subtree_sweep` (`:755-828`):
- signature gains the flags it needs:
  `_walk_subtree_sweep(token, drive_id, root_item_id, root_path, *, rel_root, zones_enabled, known_zone_ids)` returning
  `{"excluded_subtrees", "zone_candidates", "relinked_zone_ids", "requests", "unknown_probes", "truncated"}`;
- queue entries become `(item_id, path, rel_path)`;
- probe `ids` now include **file ids too**; per item:
  - file + (`True`/`None`) → excluded `{"item_id", "path", "rel_path", "kind": "file", "detected_at"}` (files are never descended anyway);
  - folder + `None` → excluded `kind="folder"`, no descend;
  - folder + `True` and `zones_enabled` → append to `zone_candidates`
    (`{"zone_item_id": id, "name", "path", "rel_path"}`) **and enqueue its
    children** (descend);
  - folder + `True` and not `zones_enabled` → excluded `kind="folder"`, no
    descend;
  - folder + `False` → descend; **and** if `item_id in known_zone_ids`,
    append to `relinked_zone_ids`;
- request accounting unchanged (`$batch` chunk math already matches
  `probe_unique_permissions`).

- [ ] **Step 4: Zone reconciliation in `_sweep_connection`**

After per-scope walks, before the `config_patch`:

```python
def _reconcile_zones(connection_id, scope, walk, existing_zones, now_iso) -> tuple[list[dict], list[dict]]:
    """Merge one scope's walk output into the connection's zone list.
    Returns (updated_zone_rows_for_this_scope, newly_dissolved_rows).
    Idempotent on zone_item_id; collection created once, reused forever."""
```

- for each candidate not in `existing_zones` (by `zone_item_id`): create the
  collection —

```python
from src.repositories import file_corpora_repo
parent_name = scope.get("display_path") or scope.get("source_scope_id")
name = f"{parent_name}/{candidate['name']}"
slug_base = _slugify(name)  # reuse the same slugify admin_sharepoint._create_scope_collection uses (import it or mirror its 2-attempt unique-suffix retry)
collection_id = _create_zone_collection(name=name, slug=slug_base, zone_item_id=candidate["zone_item_id"])
```

  then append the zone row (`status="active"`, `parent_scope_id =
  scope["source_scope_id"]`, `drive_id = scope["drive_id"]`, `display_path =
  f"{scope['display_path']}/{candidate['path']}"`, `rel_path` from the walk)
  and `log_safe(action="sharepoint_acl.zone_created",
  resource=f"source_connection:{connection_id}",
  params={"zone_item_id": …, "collection_id": …, "scope": …},
  result="success", client_kind="scheduler")`;
- for each `relinked_zone_ids` member whose row is active: set
  `status="dissolved"` + `log_safe("sharepoint_acl.zone_dissolved", …)`;
- an existing active zone re-seen as candidate: refresh `detected_at` only,
  no audit (transition-only auditing, same as membership).

Collection-creation collision handling mirrors
`admin_sharepoint.py:516-522` (one retry with `sha256(zone_item_id)[:8]`
suffix). `_create_zone_collection` lives in `acl_sync.py`;
`created_by=ACL_SYNC_SENTINEL`.

Persist: add `"acl_zones": all_zone_rows` to the existing `config_patch`
dict (`:931-952`) and extend exclusion entries with `rel_path`/`kind`.
Export `zone_rows(connection)` / `active_zone_rows(connection)` helpers
(plain config readers, tolerant of the key being absent).

- [ ] **Step 5: Config-key bookkeeping**

Add `"acl_zones"` to `ACL_SYNC_SERVER_WRITTEN_CONFIG_KEYS` (`:98`) and to the
hand-maintained carry-forward in `app/api/admin_source_connections.py:1157-1169`
(same comment style as the four existing keys).

- [ ] **Step 6: Audit catalog**

Append in `src/audit_events.py` after `:1222`:

```python
# -- SharePoint per-file ACL fidelity (2026-08-31 plan) --------------------
"sharepoint_acl.zone_created": AuditEvent(
    "sharepoint_acl.zone_created", "mutation",
    "Broken-inheritance subtree promoted to a permission zone with its own collection.",
),
"sharepoint_acl.zone_dissolved": AuditEvent(
    "sharepoint_acl.zone_dissolved", "mutation",
    "Permission zone dissolved after its folder re-linked inheritance; content re-homes to the parent scope.",
),
```

- [ ] **Step 7: Run + commit**

Run: `.venv/bin/pytest tests/test_sharepoint_subtree_sweep.py tests/test_sharepoint_acl_sync.py tests/test_audit_catalog.py tests/test_sharepoint_config_carry_forward_ratchet.py -q`
Expected: PASS.

CHANGELOG `### Added` bullet:
`- SharePoint permission zones (acl_sync.zones_enabled, default off): a broken-inheritance subtree becomes its own collection with its own mirrored ACL instead of being excluded outright; the sweep now also detects single files with unique permissions and excludes them fail-closed, and records drive-relative paths for server-side enforcement.`

```bash
git add connectors/sharepoint/acl_sync.py app/api/admin_source_connections.py src/audit_events.py tests/test_sharepoint_subtree_sweep.py CHANGELOG.md
git commit -m "feat(sharepoint): sweep v2 — file probes, rel paths, permission zones"
```

---

### Task 4: ACL sync v2 — mirror zone ACLs, suspend zone grants

**Files:**
- Modify: `connectors/sharepoint/acl_sync.py` (`_sync_connection` `:362-458`, `_suspend_connection_grants` `:683-697`)
- Test: `tests/test_sharepoint_acl_sync.py`

**Interfaces:**
- Consumes: Task 3's `active_zone_rows(connection)`; existing `_sync_scope` (unchanged signature).
- Produces: every active zone's collection carries sentinel grants mirroring the zone root's ACL; `sp-direct:<zone_item_id>` groups for direct assignments on zone roots; staleness suspension covers zone collections.

- [ ] **Step 1: Failing tests**

Extend `tests/test_sharepoint_acl_sync.py` (reuse `acl_env`, `_perms_fake`,
`_members_fake`, `_group_perm`):

```python
def test_zone_acl_is_mirrored(acl_env, monkeypatch):
    # connection with one mirrored scope + one active zone row (seeded in config
    # per the Task 3 shape); perms fake keyed {(drive, zone_item_id): [_group_perm("g-9")]}
    # expect: group entra:g-9 exists; grant on the ZONE collection with
    #         assigned_by == ACL_SYNC_SENTINEL; parent collection grants unchanged.

def test_zone_revocation_removes_grant(acl_env, monkeypatch):
    # run once with the group, once with empty perms for the zone root
    # expect: zone grant deleted; sharepoint_acl.grant_removed audited once.

def test_suspension_covers_zone_collections(acl_env, monkeypatch):
    monkeypatch.setenv("AGNES_ACL_GUARANTEE_MODE", "must_not")
    # failed run + stale last_success -> sentinel grants on BOTH the scope
    # collection and the zone collection are deleted.
```

Run — expected FAIL.

- [ ] **Step 2: Sync zones as pseudo-scopes**

In `_sync_connection`, after the `scopes` loop (`:392-404`):

```python
for zone in active_zone_rows(connection):
    pseudo = {
        "source_scope_id": zone["zone_item_id"],
        "collection_id": zone["collection_id"],
        "drive_id": zone["drive_id"],
    }
    zone_report = await _sync_scope(connection_id, pseudo, token)
    # fold matched/unmatched/unhonored/stale/grant_deltas into the same
    # accumulators, tagging unhonored entries with zone rel_path for the card
```

`_sync_scope` needs no change — `direct_group_name(zone_item_id)` already
yields a unique `sp-direct:<zone_item_id>` group and `_reconcile_grants`
already scopes to the passed collection. Count zones into the run report
(`"zones": len(active_zone_rows(connection))` in `acl_sync_last_run`).

- [ ] **Step 3: Suspension covers zones**

In `_suspend_connection_grants` (`:688`), extend the collection set:

```python
collection_ids = {s.get("collection_id") for s in _mirrored_scopes(connection) if s.get("collection_id")}
collection_ids |= {z.get("collection_id") for z in active_zone_rows(connection) if z.get("collection_id")}
```

- [ ] **Step 4: Run + commit**

Run: `.venv/bin/pytest tests/test_sharepoint_acl_sync.py -q` — PASS.

CHANGELOG `### Added` (extend Task 3's zone bullet with):
`Zone roots are re-read by every ACL sync run, so zone-level revocations land within the same window as scope-level ones; must_not staleness suspension covers zone collections.`

```bash
git add connectors/sharepoint/acl_sync.py tests/test_sharepoint_acl_sync.py CHANGELOG.md
git commit -m "feat(sharepoint): mirror zone ACLs in the sync + suspend zone grants"
```

---

### Task 5: Ingest gate — Agnes-side enforcement of exclusions and zone routing

**Files:**
- Create: `connectors/sharepoint/ingest_gate.py`
- Modify: `app/api/collections.py` (upload preflight, around `:1065`/`:1254`), `app/api/facts.py` (gate stack `:676-678`), `src/audit_events.py`
- Test: `tests/test_sharepoint_ingest_gate.py` (new), plus one test each appended to `tests/test_collections_upload*.py` and `tests/test_facts_api*.py` (use the module that already exercises `POST /api/collections/{id}/files` and `POST /api/facts/ingest`; locate by grepping for those route strings under `tests/`)

**Interfaces:**
- Consumes: Task 3's exclusion-entry shape (`rel_path`, `kind`) and `active_zone_rows`; `source_connections_repo().list(source_type="sharepoint")`.
- Produces:

```python
@dataclass(frozen=True)
class SourceAclIndex:
    excluded_stable_ids: frozenset[str]     # "graph:<item-id>" for kind=file entries
    excluded_rel_prefixes: tuple[str, ...]  # kind=folder rel_paths (non-empty only)
    foreign_zone_prefixes: tuple[str, ...]  # rel_paths of ACTIVE zones whose collection_id != this one, within the same scope/drive

def source_acl_index_for_collection(collection_id: str) -> Optional[SourceAclIndex]
def source_acl_refusal(index: SourceAclIndex, *, path: Optional[str], stable_id: Optional[str]) -> Optional[str]
    # returns "source_acl_excluded" | "source_acl_zone_mismatch" | None
```

**Semantics locked here:**
- Matching is fail-closed but precise: stable-id **exact** match against
  `excluded_stable_ids`; path matching is component-safe prefix
  (`path == p or path.startswith(p + "/")`) against `excluded_rel_prefixes`
  and `foreign_zone_prefixes`. Legacy exclusion entries with no `rel_path`
  cannot be path-matched (their subtrees were never crawled, so nothing
  arrives for them) — stable-id matching still applies when `kind=="file"`.
- The gate fires only when `acl_mirroring` is enabled AND the collection
  belongs to a SharePoint scope/zone; every other collection: `index is None`
  → no-op, zero overhead beyond one config scan.
- A zone's own collection accepts its subtree; the **parent** collection
  refuses paths under any active zone (`foreign_zone_prefixes`) — this is
  what makes producer mis-routing (unknown longest-prefix behavior) fail
  closed instead of leaking zone content at parent visibility.
- Refusal is whole-request and loud: HTTP 403, typed body listing offenders,
  one `sharepoint_acl.ingest_rejected` audit row per refused request. A
  producer that honors the exclusion env never hits it.

- [ ] **Step 1: Failing unit tests (`tests/test_sharepoint_ingest_gate.py`)**

Seed a connection row (direct repo writes per the `acl_env` idiom — copy the
fixture) with one mirrored scope `{collection_id: "col_parent", drive_id:
"d1", display_path: "Site/Documents", …}` carrying
`excluded_subtrees=[{"item_id": "X", "rel_path": "Secret", "kind": "folder"},
{"item_id": "F", "rel_path": "open/f.docx", "kind": "file"}]` and one active
zone `{zone_item_id: "Z", collection_id: "col_zone", rel_path: "Legal"}`:

```python
def test_folder_exclusion_blocks_descendants(gate_env):
    idx = source_acl_index_for_collection("col_parent")
    assert source_acl_refusal(idx, path="Secret/inner/doc.docx", stable_id="graph:other") == "source_acl_excluded"
    assert source_acl_refusal(idx, path="SecretishName/doc.docx", stable_id=None) is None  # component-safe

def test_file_exclusion_blocks_by_stable_id(gate_env):
    idx = source_acl_index_for_collection("col_parent")
    assert source_acl_refusal(idx, path="open/f.docx", stable_id="graph:F") == "source_acl_excluded"

def test_zone_content_refused_in_parent_but_allowed_in_zone(gate_env):
    parent = source_acl_index_for_collection("col_parent")
    zone = source_acl_index_for_collection("col_zone")
    assert source_acl_refusal(parent, path="Legal/contract.docx", stable_id=None) == "source_acl_zone_mismatch"
    assert source_acl_refusal(zone, path="Legal/contract.docx", stable_id=None) is None

def test_non_sharepoint_collection_is_untouched(gate_env):
    assert source_acl_index_for_collection("col_unrelated") is None
```

Run — expected FAIL (module missing).

- [ ] **Step 2: Implement `ingest_gate.py`**

Module docstring states the MUST NOT rationale and the trust boundary ("the
producer env list is advice; this module is enforcement"). Implementation
notes: iterate `source_connections_repo().list(source_type="sharepoint")`;
find the scope or zone owning `collection_id`; build the index from that
scope's exclusions + sibling active zones (a zone's index gets the zone's own
deeper exclusions, i.e. entries whose `rel_path` sits under the zone's, and
`foreign_zone_prefixes` from *nested* active zones); feature-flag check via
`feature_enabled("acl_mirroring", "enabled",
env_var="AGNES_ACL_MIRRORING_ENABLED", default=False)` — return `None` when
off. Pure functions, no caching (config reads are one repo call; both call
sites are request-scoped).

- [ ] **Step 3: Wire the upload endpoint**

In `app/api/collections.py`, inside the upload handler right after the
pairing validations (`:1254-1269`) and before bytes are stored (alongside
`_preflight_source_anchored_batch`'s call site):

```python
from connectors.sharepoint.ingest_gate import source_acl_index_for_collection, source_acl_refusal

acl_index = source_acl_index_for_collection(collection_id)
if acl_index is not None:
    offenders = []
    for i in range(len(files)):
        reason = source_acl_refusal(
            acl_index,
            path=(paths[i] if paths else None),
            stable_id=(source_stable_ids[i] if source_stable_ids else None),
        )
        if reason:
            offenders.append({"index": i, "path": paths[i] if paths else None, "reason": reason})
    if offenders:
        log_safe(action="sharepoint_acl.ingest_rejected", resource=f"file_corpus:{collection_id}",
                 params={"count": len(offenders)}, result="error", client_kind="api")
        raise HTTPException(status_code=403, detail={"error": "source_acl_excluded_paths", "items": offenders})
```

- [ ] **Step 4: Wire facts ingest**

In `app/api/facts.py`, a fourth gate function next to
`_refuse_producer_out_of_scope_documents` (`:438`), called in the gate stack
(`:676-678`):

```python
def _refuse_source_acl_excluded_documents(body: FactsIngestRequest) -> None:
    """MUST NOT enforcement (plan 2026-08-31): a document under an excluded
    subtree/file or under another collection's active zone is refused even
    when the producer's claims batch is otherwise in scope."""
    by_corpus: dict[str, Any] = {}
    offenders = []
    for doc in body.documents or []:
        corpus_id = doc.get("corpus_id")
        if not corpus_id:
            continue
        if corpus_id not in by_corpus:
            by_corpus[corpus_id] = source_acl_index_for_collection(corpus_id)
        idx = by_corpus[corpus_id]
        if idx is None:
            continue
        reason = source_acl_refusal(idx, path=doc.get("path"), stable_id=doc.get("stable_id"))
        if reason:
            offenders.append({"doc_id": doc.get("doc_id"), "reason": reason})
    if offenders:
        log_safe(action="sharepoint_acl.ingest_rejected", resource="facts:ingest",
                 params={"count": len(offenders)}, result="error", client_kind="api")
        raise HTTPException(status_code=403, detail={"error": "source_acl_excluded_documents", "items": offenders})
```

- [ ] **Step 5: Endpoint-level tests**

In the existing upload test module: one test seeding the gate config, POSTing
a two-file batch where one file sits under `Secret/` → expect 403,
`error == "source_acl_excluded_paths"`, nothing stored (`list_for_corpus`
empty), one audit row. In the facts test module: `documents[]` with an
offending `path` → 403 `source_acl_excluded_documents`. Both under the
config-shim flag idiom (`tests/test_admin_sharepoint.py:1262`).

- [ ] **Step 6: Audit catalog + run**

Catalog append (same Task 3 comment block):

```python
"sharepoint_acl.ingest_rejected": AuditEvent(
    "sharepoint_acl.ingest_rejected", "mutation",
    "Upload/ingest refused documents under a source-ACL-excluded subtree or a mis-routed permission zone (fail closed).",
),
```

Run: `.venv/bin/pytest tests/test_sharepoint_ingest_gate.py tests/test_audit_catalog.py -q` plus the two touched endpoint modules — PASS. Then `make update-openapi-snapshot` **only if** response models changed (plain HTTPException bodies don't change the schema — skip if no diff).

- [ ] **Step 7: CHANGELOG + commit**

`### Added`:
`- SharePoint source-ACL ingest gate: Agnes now refuses (403) any uploaded document or fact batch whose source path/item falls under an excluded subtree, an excluded unique-permission file, or another collection's permission zone — enforcement no longer relies on the external producer honoring the exclusion list.`

```bash
git add connectors/sharepoint/ingest_gate.py app/api/collections.py app/api/facts.py src/audit_events.py tests/test_sharepoint_ingest_gate.py CHANGELOG.md
git commit -m "feat(sharepoint): server-side source-ACL ingest gate"
```

---

### Task 6: Retroactive cleanup — purge content that lost its right to exist

**Files:**
- Modify: `connectors/sharepoint/acl_sync.py` (`_sweep_connection`), `src/audit_events.py`
- Test: `tests/test_sharepoint_subtree_sweep.py` (DuckDB path-matching), `tests/db_pg/test_sharepoint_acl_phase_pg.py` (stable-id matching, PG)

**Interfaces:**
- Consumes: Task 3's exclusion/zone shapes; `file_corpora_repo`, `corpus_files` repo (`list_for_corpus`), `corpus_file_sources_repo` (PG-only), purge machinery from `app.api.collections` (`_purge_file_row`, `_sweep_facts_orphans_after_delete`), `resource_grants_repo().delete_by_resource`, `corpus_file_events` repo `record`.
- Produces: `_cleanup_connection_content(connection, exclusions_by_scope, zone_rows_all) -> dict` returning `{"removed_files": int, "dissolved_zones": int}`, called at the end of `_sweep_connection` (after `config_patch`), results folded into `acl_sweep_last_run`.

**Semantics locked here:**
- For every mirrored scope's collection, delete each `corpus_files` row whose
  `path` sits under an excluded folder `rel_path`, or under an **active
  zone's** `rel_path` (parent-side copies — re-homing is a copy, the parent
  copy must die), or whose source anchor's `source_stable_id` equals
  `graph:<item-id>` of an excluded file.
- For every zone flipped to `dissolved` this run: purge **all** files in the
  zone collection (parent re-crawl re-ingests them under the parent),
  delete the zone collection's sentinel grants, then
  `resource_grants_repo().delete_by_resource(ResourceType.COLLECTION.value,
  zone_collection_id)` (closes the verified dangling-grants gap), then
  `file_corpora_repo().soft_delete(zone_collection_id)`.
- Deletion uses the exact machinery `DELETE /files/{id}` uses:
  `_purge_file_row(collection_id, row)` per row, one
  `_sweep_facts_orphans_after_delete(trigger="sharepoint-acl-cleanup")` per
  connection at the end, one `corpus_file_events` `record(change="deleted",
  source_stable_id=…)` per row. Claims die by FK cascade; chunks via the
  purge helper.
- On a DuckDB-backend instance `corpus_file_sources_repo()` raises
  `RequiresPostgresBackend` — catch it, skip stable-id matching, keep path
  matching (facts don't exist on DuckDB instances; content still gets
  removed).
- Audit: one `sharepoint_acl.content_purged` per collection with a non-zero
  removal count, params `{collection_id, removed, trigger}` — never a
  per-file row (volume).

- [ ] **Step 1: Failing tests**

DuckDB (`tests/test_sharepoint_subtree_sweep.py`): seed via real repos —
`file_corpora_repo().create`, `corpus_files` repo `add` rows with paths
`Secret/a.docx`, `open/keep.docx` in the scope collection; sweep fake probes
`Secret` folder as `True` (zones off) → expect `keep.docx` survives,
`a.docx` gone, one `content_purged` audit row, one `corpus_file_events`
deleted row. A second test: pre-seeded active zone + probe `False` →
dissolved zone's collection is emptied + soft-deleted and
`resource_grants` rows for it are gone.

PG (`tests/db_pg/test_sharepoint_acl_phase_pg.py` idiom): seed a
`corpus_file_sources` anchor `graph:F` for a file, sweep probes file `F`
unique → row deleted by stable-id even when its path is outside any excluded
folder.

Run — expected FAIL.

- [ ] **Step 2: Implement `_cleanup_connection_content`**

```python
def _cleanup_connection_content(connection, exclusions_by_scope, zone_rows_all) -> Dict[str, int]:
    from app.api.collections import _purge_file_row, _sweep_facts_orphans_after_delete
    from src.repositories import corpus_files_repo, resource_grants_repo, file_corpora_repo
    ...
```

- build per-collection matcher inputs (mirror the Task 5 index shapes — do
  NOT import `ingest_gate`'s collection scan here; the sweep already holds
  the connection row, so build matchers directly from
  `exclusions_by_scope` + `zone_rows_all`);
- stable-id lookup: `corpus_file_sources_repo().get(corpus_file_id)` per
  candidate row inside a `try/except RequiresPostgresBackend` that flips a
  `stable_ids_available = False` once;
- deletion loop per collection: `for row in corpus_files_repo().list_for_corpus(cid): if _matches(row): _purge_file_row(cid, row); events.record(...); removed += 1`;
- one orphan sweep per connection when `removed > 0`;
- dissolved zones per the semantics above (order: purge files → sentinel
  grant delete via `_reconcile_grants(zone_cid, [], zone_item_id)` → 
  `delete_by_resource` → `soft_delete`);
- audit + fold counts into the `acl_sweep_last_run` block (extend the dict in
  `_sweep_connection` with `"removed_files"` and `"dissolved_zones"`).

Catalog append:

```python
"sharepoint_acl.content_purged": AuditEvent(
    "sharepoint_acl.content_purged", "mutation",
    "Already-ingested content removed because its source subtree/file lost inheritance or a permission zone dissolved.",
),
```

- [ ] **Step 3: Run + commit**

Run: `.venv/bin/pytest tests/test_sharepoint_subtree_sweep.py tests/test_audit_catalog.py -q` and the PG file via the db_pg lane if a local PG is available (CI runs it regardless).

CHANGELOG `### Added`:
`- SharePoint sweep now retroactively purges already-ingested content (files, chunks, claims) under newly detected broken-inheritance subtrees and unique-permission files, and fully retires dissolved permission zones including their grants.`

```bash
git add connectors/sharepoint/acl_sync.py src/audit_events.py tests/test_sharepoint_subtree_sweep.py tests/db_pg/test_sharepoint_acl_phase_pg.py CHANGELOG.md
git commit -m "feat(sharepoint): retroactive cleanup of excluded/zone content"
```

---

### Task 7: Producer handoff — zone corpus map + file exclusions

**Files:**
- Modify: `connectors/sharepoint/corpus_map.py`, `app/worker/kinds.py` (`_excluded_subtree_scope_map` `:1365-1400`, corpus-map env build `:1746-1755`), `app/api/admin_sharepoint.py` (corpus-map endpoint `:1084-1131`), `docs/api-reference.md`
- Test: the existing corpus-map test module (grep `producer_corpus_map` under `tests/` and extend it), `tests/test_worker_kinds.py` or the module already testing `_excluded_subtree_scope_map` (grep `AGNES_SP_EXCLUDED_SUBTREE_IDS` under `tests/`)

**Interfaces:**
- Consumes: Task 3's zone rows (`active_zone_rows`), exclusion `kind` field.
- Produces: `producer_corpus_map(scopes, zones=())` — zone entries become additional map keys (site-form of the zone's `display_path`, exactly `_map_key`'s folder branch) → zone `collection_id`; `_excluded_subtree_scope_map` includes `kind="file"` item ids and **omits** zoned subtrees when `acl_zones` is on (they are crawled now); dissolved zones are neither excluded nor mapped.

- [ ] **Step 1: Failing tests**

```python
def test_zone_rows_produce_nested_keys():
    scopes = [{"source_scope_id": "root-1", "display_path": "Site/Documents/Team", "collection_id": "col_p"}]
    zones = [{"zone_item_id": "Z", "display_path": "Site/Documents/Team/Legal", "collection_id": "col_z", "status": "active", "parent_scope_id": "root-1"}]
    m = producer_corpus_map(scopes, zones)
    assert m["Site/Team"] == "col_p" and m["Site/Team/Legal"] == "col_z"

def test_dissolved_zone_not_mapped():  # status "dissolved" -> absent

def test_excluded_map_includes_file_ids_and_skips_zones(monkeypatch):
    # connection config: excluded [{item_id "F", kind "file"}, {item_id "X", kind "folder"}],
    # active zone Z; AGNES_ACL_ZONES_ENABLED=true
    # expect map == {"root-1": ["F", "X"]}  (zone Z NOT in the list)
    # and with zones disabled, legacy behavior: Z's folder-break entry (which
    # Task 3 would then have written as excluded) appears as before.
```

Run — expected FAIL.

- [ ] **Step 2: Implement**

`producer_corpus_map(scopes: list, zones: Sequence[dict] = ()) -> dict[str, str]`:
after the scope loop, for each zone with `status == "active"` build a
pseudo-scope `{"source_scope_id": zone["zone_item_id"], "display_path":
zone["display_path"], "collection_id": zone["collection_id"]}` and run it
through the same `_map_key` + ambiguity accounting (zone ids are plain Graph
folder item ids — no comma, no `b!` — so the folder branch fires; a zone
whose `display_path` yields fewer than 3 segments raises `CorpusMapError`
rather than silently collapsing). Update the module docstring: nested keys
now exist; the producer resolver **must** resolve longest-prefix-first, and
the Task 5 ingest gate is the server-side backstop when it does not.

Callers: `kinds.py:1752` and the admin corpus-map endpoint pass
`active_zone_rows(connection)`. `_excluded_subtree_scope_map`: include every
entry's `item_id` regardless of `kind` (files ride the same env list — the
env var name stays for producer compat, docstring updated), and document
that zoned subtrees are intentionally absent.

- [ ] **Step 3: Docs + run + commit**

`docs/api-reference.md` corpus-map section: document zone keys + the
longest-prefix producer requirement + that `AGNES_SP_EXCLUDED_SUBTREE_IDS`
values may now be file item ids.

Run: `.venv/bin/pytest tests/ -k "corpus_map or excluded_subtree" -q` — PASS.

CHANGELOG `### Changed`:
`- SharePoint producer handoff: the corpus map now carries permission-zone keys (nested under their parent scope; resolver must match longest-prefix-first) and the exclusion env list may contain unique-permission file ids.`

```bash
git add connectors/sharepoint/corpus_map.py app/worker/kinds.py app/api/admin_sharepoint.py docs/api-reference.md CHANGELOG.md tests/
git commit -m "feat(sharepoint): zone-aware corpus map + file-level exclusion handoff"
```

---

### Task 8: Admin surface — sweep trigger, zone visibility, grant-hygiene fixes

**Files:**
- Modify: `app/api/admin_sharepoint.py` (new route after `:1302`; `_scope_out` `:460-476`; `remove_scope` `:1062-1081`), `src/audit_posture.py` (`POSTURE` SharePoint block `:141-165`), `src/audit_events.py`, `docs/api-reference.md`
- Test: `tests/test_admin_sharepoint.py`, `tests/db_pg/test_endpoints_smoke.py` (SharePoint block `:3252-3316`), `tests/test_documentation_api_triple_surface.py` (`_EXEMPT`), snapshot via `make update-openapi-snapshot`

**Interfaces:**
- Consumes: existing acl-sync trigger precedent (`admin_sharepoint.py:1261-1302`), Task 3 zone rows.
- Produces: `POST /api/admin/sharepoint/connections/{connection_id}/subtree-sweep` (admin, 202, idempotency key `sharepoint-subtree-sweep:<connection_id>`, payload `{"connection_id": …}` — the explicit-connection payload already bypasses the due-guard, giving admins "re-check now"); connection detail exposes `zones` (list of `{zone_item_id, display_path, collection_id, status, detected_at}`) and per-scope `excluded_file_count`; `remove_scope` deletes the scope collection's sentinel-owned grants.

- [ ] **Step 1: Failing tests**

`tests/test_admin_sharepoint.py`:

```python
def test_sweep_now_enqueues_job(...):        # mirror the acl-sync trigger tests: 202, jobs_repo kind
                                             # "sharepoint-subtree-sweep", payload {"connection_id": cid},
                                             # 409-dedup second call, 404 unknown connection, 403 analyst,
                                             # flag-off behavior identical to the acl-sync trigger's
def test_scope_out_reports_zones_and_file_exclusions(...):
def test_remove_scope_deletes_sentinel_grants(...):
    # seed sentinel grant on the scope collection; DELETE scope; grant gone;
    # an admin-assigned grant on the same collection survives.
```

- [ ] **Step 2: Implement the route**

Copy the acl-sync trigger handler shape (`:1261-1302`) with kind
`sharepoint-subtree-sweep`, idempotency key helper
`_sweep_idempotency_key(connection_id)`, audit action
`sharepoint_acl.sweep_triggered` (catalog append + `POSTURE` entry
`"POST /api/admin/sharepoint/connections/{connection_id}/subtree-sweep":
"sharepoint_acl.sweep_triggered"` under the existing module header).

- [ ] **Step 3: `_scope_out` + connection detail**

`_scope_out` gains `"excluded_file_count": sum(1 for e in
scope.get("excluded_subtrees") or [] if e.get("kind") == "file")` and passes
`rel_path`/`kind` through in the existing `excluded_subtrees` projection
(`:470-472`). The connection serializer gains `"zones":
[_zone_out(z) for z in zone_rows(row)]`.

- [ ] **Step 4: `remove_scope` hygiene**

Before rewriting `config["scopes"]`, when the removed scope has a
`collection_id`: delete sentinel-owned COLLECTION grants for it (the exact
loop `confirm_scope` uses for the `mirrored -> manual` transition,
`:1044-1050`). Docstring updated — collections stay, mirrored grants no
longer dangle.

- [ ] **Step 5: Route obligations**

- `tests/db_pg/test_endpoints_smoke.py`: add the new route string to the
  SharePoint class's `COVERED_ROUTES` (with the smoke test exercising it) —
  or `KNOWN_UNTESTED` with a reason only if the class pattern genuinely
  cannot reach it (prefer covered; the acl-sync line at `:3315` is the
  template).
- `tests/test_documentation_api_triple_surface.py` `_EXEMPT`: add the route
  with the same rationale wording as the acl-sync exemption (`:1230-1234` —
  admin-gated operational trigger, REST+UI only, deliberate).
- `docs/api-reference.md`: document the route path verbatim.
- `make update-openapi-snapshot`.

- [ ] **Step 6: Run + commit**

Run: `.venv/bin/pytest tests/test_admin_sharepoint.py tests/test_audit_route_posture.py tests/test_audit_catalog.py tests/test_openapi_snapshot.py tests/test_api_docs_coverage.py tests/test_documentation_api_triple_surface.py -q` — PASS. Then `python3 scripts/verify_syncmap.py --base HEAD`.

CHANGELOG `### Added` + `### Fixed`:
`- Admin "Re-check subtrees now" trigger (POST .../subtree-sweep) and zone/excluded-file visibility on the SharePoint connection detail.`
`- Removing a SharePoint scope now deletes the ACL sync's own mirrored grants for its collection instead of leaving them dangling.`

```bash
git add app/api/admin_sharepoint.py src/audit_posture.py src/audit_events.py docs/api-reference.md tests/ CHANGELOG.md
git commit -m "feat(sharepoint): sweep trigger endpoint, zone visibility, grant hygiene"
```

---

## Task dependency graph (for the decomposer)

```
T1 (paging)  ──► T3 (sweep v2) ──► T4 (zone ACL sync)
T2 (switches/cadence)   │            
                        ├─────────► T5 (ingest gate)
                        ├─────────► T6 (cleanup)
                        └─────────► T7 (producer handoff)
T3 ───────────────────────────────► T8 (admin surface)
```

T2 is independent. T4–T8 each depend only on T3's data shapes (and T5/T6
share the matcher semantics — keep them consistent; T6 deliberately builds
matchers locally rather than importing T5's module). No migration task
exists; nothing needs migration serialization.

## Residual risks — stated, not hidden (for the PR body and customer material)

1. Windows are hours-scale, not zero: rights change on a known root ≤
   `acl_sync.interval_hours` (default 4 h); newly broken inheritance ≤
   `acl_sweep_interval_days` (default 1 day) + sweep duration. Between
   detection and cleanup, previously crawled content under a *newly* broken
   subtree is the spec's §8.3(3) residual — now bounded by a day instead of
   a week, and erased retroactively.
2. Permission-only changes never appear in Graph delta; the sweep re-probe
   is the only detector. Webhook-accelerated sweeps remain future work
   (spec QD).
3. The external producer must still route zone content to zone collections
   (longest-prefix resolution) — but mis-routing now fails closed at the
   ingest gate instead of leaking.
4. File-grain fidelity = fail-closed exclusion (the file is invisible to
   everyone in Agnes), not per-file mirroring — deliberate (§3, cost).
