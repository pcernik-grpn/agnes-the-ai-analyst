# Playbook: schema migration — PG-first (A3 ratchet)

**The DuckDB migration ladder in `src/db.py` is frozen** (see `CLAUDE.md` →
"Dual-backend discipline"). Every new schema change — new table, new column,
new index — is **Alembic-only**. There is no matching `_vN_to_v(N+1)` step to
write, and `SCHEMA_VERSION` must not move.

## Alembic side (`migrations/versions/`) — the only ladder that advances

1. Create `migrations/versions/00NN_<desc>.py`, `down_revision` chained to
   the current head (no `_v<N>` suffix tied to a DuckDB version — there is
   no longer a corresponding one):

   ```python
   revision = "00NN_<desc>"
   down_revision = "<previous revision id>"

   def upgrade() -> None: ...   # op.create_table / op.add_column
   def downgrade() -> None: ...  # exact inverse
   ```

2. Update `src/db_pg.py` `Base.metadata` (the SQLAlchemy models) to match
   the new structural change — same as always.
3. Build the repository **PG-only**: `src/repositories/<name>_pg.py`, no
   DuckDB sibling. See `references/repo-parity.md`.

## DuckDB side (`src/db.py`) — do not touch

- `SCHEMA_VERSION` stays at `FROZEN_DUCKDB_SCHEMA_VERSION` (currently 124).
  Do not write a new `_vN_to_v(N+1)` function; do not bump `SCHEMA_VERSION`.
- `tests/test_db_schema_version_frozen.py` is the gate — it fails if
  `SCHEMA_VERSION` moves past the frozen constant.
- The DuckDB ladder for *existing* tables (pre-A3) is unaffected and stays
  exactly as it was — this freeze only stops it from growing further, it
  does not touch what is already there.

## Integration gates

- `tests/test_db_schema_version_frozen.py` — `SCHEMA_VERSION` must equal
  `FROZEN_DUCKDB_SCHEMA_VERSION`; fails if a new `_vN_to_v(N+1)` step (or a
  bare version bump) appears.
- `tests/test_db_schema_version.py` — unaffected by the freeze and still the
  integration gate for the frozen ladder's existing (pre-A3) steps: it drives
  old DuckDB files up through them and asserts they land at `SCHEMA_VERSION`.
  A bugfix to one of those existing `_vN_to_v(N+1)` functions still needs a
  green run here; only a NEW step is disallowed (that's
  `test_db_schema_version_frozen.py`'s job).
- `tests/db_pg/test_alembic_roundtrip.py` — upgrade/downgrade roundtrips +
  `test_no_model_migration_drift` (autogenerate diff vs `Base.metadata` must be
  empty → this is why you update `src/db_pg.py`).
- `tests/test_repository_registry_pg_first_ratchet.py` +
  `tests/db_pg/test_repo_module_pg_first_ratchet.py` — no new DuckDB-backed
  `_REGISTRY` entry or repo module (the repo-side half of the same freeze;
  see `references/repo-parity.md`).

## Steps

1. TDD: add a test for the new table/column (PG-only contract test — see
   `references/repo-parity.md`).
2. Alembic: new revision (up + down), chained to head.
3. Update `src/db_pg.py` `Base.metadata`.
4. Do NOT touch `src/db.py` / `SCHEMA_VERSION`.
5. Green `test_db_schema_version_frozen.py` + `test_alembic_roundtrip.py`.

## Anchors

- `SCHEMA_VERSION` / `FROZEN_DUCKDB_SCHEMA_VERSION`: `src/db.py`
- gates: `tests/test_db_schema_version_frozen.py` (freeze ceiling),
  `tests/test_db_schema_version.py` (existing pre-A3 ladder steps),
  `tests/db_pg/test_alembic_roundtrip.py`
- full developer recipe with worked examples: `docs/migrations.md` → "Adding
  a PG-only feature"
