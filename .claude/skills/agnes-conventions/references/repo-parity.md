# Playbook: repository / method — PG-first (A3 ratchet)

**The DuckDB app-state backend is frozen** (see `CLAUDE.md` → "Dual-backend
discipline"). A brand-new app-state repository is **Postgres-only** — no
DuckDB module, no DuckDB `_REGISTRY` entry. Existing DuckDB↔PG pairs stay
maintained (a method added to one side still needs its sibling), but no new
pair may be created. Reach every repo via the `*_repo()` factory, never
instantiate a repo class directly.

## New repository (the default case) — PG-only

1. `src/repositories/<name>_pg.py` — Postgres impl: `class <Name>PgRepository`
   taking a SQLAlchemy `Engine`; `sa.text(...)` with `:named` binds; reads under
   `with self._engine.connect()`, writes under `with self._engine.begin()`. Shape:
   `src/repositories/sync_state_pg.py:17`. **No `src/repositories/<name>.py`
   DuckDB module.**
2. `src/repositories/__init__.py` — THREE edits:
   - add `"<name>_repo"` to `__all__`;
   - add a `_REGISTRY` entry with **only** the `PG` backend:
     `"<name>": {PG: ("src.repositories.<name>_pg", "<Name>PgRepository")}`;
   - add the factory fn `def <name>_repo() -> Any: return _build("<name>")`.
3. `tests/db_pg/test_<name>_pg.py` — exercise the PG repo directly (CRUD,
   constraints, cluster-specific invariants). There is no DuckDB half to
   parametrize against.
4. A route that can reach this repo while the active backend is DuckDB
   raises `src.repositories.RequiresPostgresBackend` automatically (the
   factory does this for any PG-only entry) — the app-wide handler in
   `app/main.py` turns it into a `501`. If the route is swept by
   `tests/db_pg/test_get_status_parity_sweep.py` /
   `test_mutation_status_parity_sweep.py`, add `"METHOD path": "<one-line
   reason>"` to that file's `_PG_ONLY_ROUTE_EXEMPTIONS` (a `dict[str, str]`
   — an empty/missing reason fails its own guard).

Full recipe with worked examples: `docs/migrations.md` → "Adding a PG-only
feature".

## Extending an EXISTING DuckDB↔PG pair (no schema change)

Only applies to a repo that already has both `src/repositories/<name>.py`
and `src/repositories/<name>_pg.py` — e.g. a new query method over columns
that already exist. A genuine schema change (new column/table) on an
existing pair is **still PG-only** (see above) — the freeze doesn't care
whether the table predates the ratchet.

1. `src/repositories/<name>.py` — add the method to the DuckDB impl.
2. `src/repositories/<name>_pg.py` — add the matching method (identical
   parameter names; PG may add defaulted params, never drop one).
3. `tests/db_pg/test_<name>_contract.py` — extend with the new behavior,
   parametrized `["duckdb", "pg"]`. Model it on
   `tests/db_pg/test_mcp_sources_contract.py`.

## Guards that fail if you skip a step

| Skipped | Failing test |
|---|---|
| Registering a NEW repo with a DuckDB backend (either full pair or DuckDB-only) | `tests/test_repository_registry.py::test_registry_backends_are_symmetric` + `tests/test_repository_registry_pg_first_ratchet.py` |
| A NEW plain (non-`_pg.py`) module under `src/repositories/` | `tests/db_pg/test_repo_module_pg_first_ratchet.py` |
| `_REGISTRY` entry / asymmetric backends on an existing pair | `tests/test_repository_registry.py::test_registry_backends_are_symmetric` |
| `__all__` / factory fn | `tests/test_repository_registry.py::test_every_public_factory_has_a_registry_entry` |
| Existing-pair PG side missing a public method the DuckDB side has | `tests/db_pg/test_repo_method_parity.py` |
| Direct `XRepository(conn)` instead of factory | `tests/test_backend_split_guard.py` |
| `get_system_db()` in a handler | `tests/test_backend_split_guard.py` |
| PG-only route not failing clean (raw 500) on a DuckDB-backed instance | `tests/db_pg/_parity_sweep_util.py::assert_pg_only_exemptions_fail_clean` |
| Semantic drift (e.g. JSON dict vs str) | your `tests/db_pg/test_<name>_contract.py` / `test_<name>_pg.py` |

## Steps

1. TDD: write the PG-only contract test first (it fails — no repo).
2. Write the PG repo. (Existing-pair method addition: write both sides.)
3. Make the `__init__.py` edits — PG-only `_REGISTRY` shape for a new repo.
4. Green the contract test + registry/parity guards.

## Anchors

- factory `_build` / `_REGISTRY` / `_ARG_PROVIDERS` / `RequiresPostgresBackend`: `src/repositories/__init__.py`
- frozen-list ratchets: `tests/test_repository_registry_pg_first_ratchet.py`, `tests/db_pg/test_repo_module_pg_first_ratchet.py`
- PG-only-route exemption mechanism: `tests/db_pg/_parity_sweep_util.py`, proven in `tests/db_pg/test_pg_only_route_exemption_mechanism.py`
- paired example (frozen pre-A3 pair): `src/repositories/sync_state.py` + `src/repositories/sync_state_pg.py`
- contract example: `tests/db_pg/test_mcp_sources_contract.py`
