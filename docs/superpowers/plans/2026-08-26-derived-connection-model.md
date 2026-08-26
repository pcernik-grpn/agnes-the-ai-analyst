# Derived Connection Model (D2, slice 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Snowflake, BigQuery and Databricks each get ONE managed
`source_connections` row as their live source of truth — killing the
mid-wizard "restart the instance" demand and the instance-config/-UI
config split for these sources — while multi-connection-per-type stays an
explicit follow-up boundary.

**Architecture:** Revive the tested-but-dead generic resolver
(`src/connection_resolver.py`), give each per-source settings resolver an
optional connection argument (row-first, instance-config fallback so every
existing test mock and un-migrated instance keeps working), seed rows from
instance config the way `connections_seed.py` already does for
keboola+bigquery, move the wizard's SF/DBX saves from the server-config
yaml overlay onto the connection row (DB rows are read live by every
process — the cross-process staleness that forced the restart disappears),
and relocate the connection-identity/repoint guard onto the row.

**Tech Stack:** existing `source_connections` + `connection_secrets`
schema (v79, generic — **zero migrations needed**), `src/connection_specs.py`,
`src/connection_resolver.py`, FastAPI, pytest.

**Spec:** `docs/superpowers/plans/2026-08-24-agnes-remediation-program.md`
§Track D/D2. Ground truth: 2026-08-26 scout against main@670c00064.

## Ground truth this plan stands on (main@670c00064)

- `POST /api/admin/source-connections` already accepts any `source_type`
  (`app/api/admin_source_connections.py:421-453`) but validates nothing;
  `src/connection_specs.py` has specs for keboola/bigquery/databricks —
  snowflake spec missing; only `kind="master"` secrets are keboola-gated,
  `kind="storage"` works for any row (:744-765).
- `src/connection_resolver.py` (`resolve_connection` + `resolve_token`)
  exists, is tested, and has ZERO production callers.
- `connections_seed.py:27` already seeds keboola AND bigquery rows from
  instance config on first boot — **every BQ instance likely already has
  an unused default connection row**. `get_bq_access()`
  (`connectors/bigquery/access.py:870`, `functools.cache`) still reads
  instance config.
- SF/DBX resolvers (`connectors/snowflake/settings.py:29`,
  `connectors/databricks/semantic_layer.py:87`) are zero-arg, read
  `data_source.<type>.*` live in-process; the restart demand
  (`admin_data_sources.html:3720-3723`, `:3279-3284`) exists because of
  CROSS-PROCESS `_instance_config` staleness under role-split
  (`app/api/admin.py:580` says so verbatim), BQ's `functools.cache`, and
  on-disk `_remote_attach`/view-body snapshots (which a restart doesn't
  fix anyway; only BQ self-heals, `src/orchestrator.py:377-424`).
- `app/connection_identity.py` + the 409 repoint guard
  (`app/api/admin.py:2004-2081`) formalize "instance config IS the
  connection identity" — D2 relocates that subject onto the row.
- The Keboola #1530 per-connection pattern (`_resolve_keboola_credentials`,
  rows grouped by `connection_id`) is the reuse template.
- Wizard: Snowflake has a discovery picker (#1490); **BigQuery is still
  free-text rows** (`admin_data_sources.html:3608-3632`).

## Explicit slice boundary — OUT of D2 (issues #1462/#1375 follow-up)

Multi-connection-per-type and everything it needs: `SF_ALIAS="sf"` baked
into view bodies (`connectors/snowflake/attach.py:35`,
`extract_init.py:75`), one extract dir per source type
(`app/api/sync.py:312-315`), `_remote_attach` first-writer-wins, BQ
single-project lock (`access.py:30-34`), per-connection repoint guards,
`kind="master"` secrets for non-keboola. One row per type is the contract
of this slice; the plan must not silently half-enable a second row.

## Global Constraints

Inherits the master program's Global Constraints. No schema change → no
migration work (A3-compatible by vacuity). NOT breaking: instance-config
fallback keeps un-migrated instances and existing env-based deploys
working; deprecation warnings only (the seeder's existing pattern).
Reserved files from parallel Track C work (`app/api/agents*.py`,
`app/api/chat.py`, `app/api/agent_*`, `src/access_policy.py`,
`app/api/broker*.py`) must not be touched. Branch:
`zs/d2-connection-model` off origin/main; expected 3 PRs (tasks D2.1,
D2.2+D2.3, D2.4).

---

### Task D2.1: specs, validation, seeding (1 PR)

**Files:** `src/connection_specs.py` (add the snowflake spec: config keys
`account,user,database,warehouse,role,auth_type`, secret refs
`token_env`/`private_key_env`+passphrase — mirror
`resolve_snowflake_settings`'s read set exactly);
`app/api/admin_source_connections.py` POST/PUT wire
`validate_connection_config` (reject unknown source_type or malformed
config with a 400 naming the field); `app/connections_seed.py` extend to
snowflake + databricks rows (mapping per the scout: SF
`config={account,user,database,warehouse,role,auth_type}`, DBX
`config={host,warehouse_id,catalog}` + `token_env`), keeping the existing
deprecation-warning behavior; docs (`docs/DATA_SOURCES.md` +
`docs/CONFIGURATION.md` ownership map rows for these sources).
**Tests:** extend `tests/test_connection_specs.py` +
`tests/test_connections_seed.py` (seed matrix ×4 source types,
idempotence, no-op when a row exists); new
`tests/test_source_connection_validation.py` (POST rejects garbage,
accepts each valid spec — failing-first against the unvalidated POST).

### Task D2.2: live row-first resolution (with D2.3 in 1 PR)

**Files:** revive `src/connection_resolver.py` as the single entry:
`resolve_source_connection(source_type)` returns the type's default row
(or None). `resolve_snowflake_settings(connection=None)` /
`resolve_databricks_settings(connection=None)`: when a row exists (passed
or looked up), settings come from `row.config` + vault-first secrets via
`resolve_token`; fallback = today's instance-config path, byte-compatible
— zero-arg calls keep working so the existing monkeypatch-based tests
(`tests/test_databricks_remote_query.py` etc.) survive unchanged. Thread
the row through the per-pass call sites (`app/api/sync.py:570-618`
memoization becomes per-connection-id), extract-init builders, v2
schema/scan, discovery, semantic syncs, card probes. **BigQuery:**
`get_bq_access()` reads the seeded row first (instance-config fallback);
replace the bare `functools.cache` with a cache keyed on the row's
`updated_at` (or config hash) so an admin save invalidates across
processes on next read — this is the piece that makes the existing seeded
row finally load-bearing.
**Tests:** per-source contract: row-set vs fallback resolution
(failing-first: create a row with different values than instance config →
resolver must return the ROW's values); BQ cache invalidation on row
update; the #1530-style "wrong credential impossible" assertion for SF
materialized pass.

### Task D2.3: kill the restart demand (same PR as D2.2)

**Files:** wizard SF/DBX save paths (`admin_data_sources.html` +
`app/api/admin_source_connections.py`): saves write the CONNECTION ROW +
vault (not the server-config `data_source.<type>` overlay); remove the
restart warning copy; `app/connection_identity.py` + the repoint 409
guard (`app/api/admin.py:2004-2081`) re-keyed to the row (changing the
row's identity leaves with registered tables → same 409 contract);
`app/api/admin.py:580`'s `data_source` restart classification narrowed
accordingly; `tests/test_server_config_restart_effect.py` +
`tests/test_admin_data_sources_page.py` +
`tests/test_admin_server_config_connection_guard.py` updated to the new
contract (each update must re-pin, not delete).
**Acceptance (the headline):** an admin configures Snowflake in the
wizard and registers+syncs a table in the same session with NO restart —
pinned by an end-to-end test that saves a connection via the API and
immediately resolves settings from a *different* TestClient app instance
(simulating the second process reading the DB live).

### Task D2.4: connection identity on the catalog (1 small PR)

**Files:** `app/api/v2_catalog.py` row shape gains `connection`
({id,name} or null) — additive; `agnes catalog` table adds the column
when any row carries it; MCP catalog tool parity
(`app/api/mcp/foundation_tools.py`) + `tests/test_mcp_tool_parity.py`;
OpenAPI snapshot.
**Tests:** catalog rows carry connection identity for attributed rows,
null for legacy; CLI render; MCP parity — failing-first on the missing
field.

## Execution notes

- Order: D2.1 → D2.2+D2.3 → D2.4; independent of A3/C merges (no schema,
  no reserved files) — safe to run parallel to Track C.
- Reviewer asks: rbac on D2.1/D2.3 (vault write gates, repoint guard
  relocation), architecture on D2.2 (per-pass memoization, on-disk
  snapshot healing unchanged — explicitly NOT extended to SF/DBX in this
  slice, note it), rules everywhere.
- The known follow-up boundary (multi-connection) is restated in each
  PR body so reviewers don't flag the single-row contract as a gap.
