# Postgres migrations playbook

Agnes's app state lives in Postgres (via SQLAlchemy 2.0 + Alembic). Analytics
stays on DuckDB. This document is the operator + developer playbook for the
PG side; the DuckDB inline ladder in `src/db.py` is **frozen** (PG-first
ratchet, A3 — see `CLAUDE.md` → "Dual-backend discipline"): existing
`src/repositories/*.py` ↔ `*_pg.py` pairs and DuckDB schema steps stay
maintained, but no new one may be added. `SCHEMA_VERSION` is pinned at
`src/db.py::FROZEN_DUCKDB_SCHEMA_VERSION`; every new migration from here on
is Alembic-only. See "Adding a PG-only feature" below for the developer
recipe, and "Extending an EXISTING (frozen pre-A3) pair" for the no-schema-
change case on a repo that already has a DuckDB half.

## Module layout

```
alembic.ini                              ; Alembic config, no DB URL
migrations/                              ; revision chain
  env.py                                 ; reads DATABASE_URL / AGNES_DB_URL (legacy alias)
  script.py.mako
  shipped_revision_ids.txt               ; append-only ratchet — every id ever shipped (issue #2086)
  versions/
    0001_baseline.py                     ; empty anchor
    0002_audit_log.py
    0003_rbac.py                         ; users + user_groups + members + grants
    0004_ops_triad.py                    ; table_registry + sync_state + sync_history
    0005_config_and_pat.py               ; metric_definitions + instance_templates + PATs
    0006_lookup.py                       ; view_ownership + column_metadata + bq_cache + sync_settings

src/db_pg.py                             ; engine + session singleton
src/models/                              ; SQLAlchemy 2.0 models (one file per cluster)
src/repositories/*_pg.py                 ; Postgres impls (mirror DuckDB ones)

tests/db_pg/                             ; PG fixtures + tests
  conftest.py                            ; pg_engine via testcontainers/embedded/pgserver
  snapshot.py                            ; schema snapshot for round-trip tests
  test_alembic_skeleton.py
  test_alembic_roundtrip.py              ; round-trip + drift discipline
  test_audit_contract.py                 ; cross-engine contract test pattern
  test_*_pg.py                           ; per-cluster PG-side tests

scripts/migrate_duckdb_to_pg/__init__.py ; data migration framework
scripts/migrate_duckdb_to_pg/__main__.py ; CLI: python -m scripts.migrate_duckdb_to_pg
```

## URL resolution

```bash
# Production
export DATABASE_URL="postgresql+psycopg://agnes:***@10.x.y.z:5432/agnes"

# Dev (defaults to a local pgserver-bundled binary; no install needed)
unset DATABASE_URL AGNES_DB_URL  # tests pick up automatically
```

Resolution order: explicit `cfg.attributes["sqlalchemy.url"]` (used by tests) →
`DATABASE_URL` env → `AGNES_DB_URL` env (legacy alias, emits deprecation warning at
runtime). No silent default to SQLite or file paths.

## Local dev via Docker Compose

The repo ships `docker-compose.postgres.yml` as a backward-compatible
**overlay**: existing `docker compose up` continues to run the
DuckDB-only path; adding the overlay brings up a Postgres service plus
a one-shot migrate service.

```bash
# Bring up app + Postgres, run migrations, then start uvicorn
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up

# Or set once in your shell so plain `docker compose up` picks it up
export COMPOSE_FILE=docker-compose.yml:docker-compose.postgres.yml
docker compose up
```

What the overlay does:

- Adds a `postgres:16-alpine` service with a named volume (`postgres_data`).
- Adds a one-shot `migrate` service that runs `alembic upgrade head`
  against the Postgres above, then exits.
- Wires `DATABASE_URL=postgresql+psycopg://agnes:${POSTGRES_PASSWORD}@postgres:5432/agnes`
  into both `migrate` and `app`. The `app` service waits for `migrate`
  to exit 0 before booting uvicorn, so a botched upgrade blocks the
  whole stack with a clear log trail.
- Binds Postgres to `127.0.0.1:5432` on the host so a misconfigured
  firewall can't expose it to the public internet.

`POSTGRES_PASSWORD` is read from `.env`; defaults to `agnes` for
zero-config local dev (change before any multi-user / exposed
deployment).

## Production wiring

For managed Postgres (Cloud SQL, RDS, Azure DB), **do NOT use the
postgres overlay** — point `DATABASE_URL` at the managed instance and
run the migration step in your deploy pipeline.

### Cloud SQL — TCP from private IP

```bash
# In .env or Secret Manager → injected into the systemd unit's
# EnvironmentFile= (or container env)
DATABASE_URL=postgresql+psycopg://agnes:${PG_PW}@10.x.y.z:5432/agnes
```

### Cloud SQL — Unix socket (Cloud SQL Auth Proxy or private IP)

```bash
# Note the URL-encoded socket path: %2F = /
DATABASE_URL=postgresql+psycopg://agnes:${PG_PW}@/agnes?host=/cloudsql/${PROJ}:${REGION}:${INST}
```

The `cfg.attributes["sqlalchemy.url"]` trick in `migrations/env.py`
exists specifically so the `%`-encoded socket path doesn't break
configparser's interpolation.

### Migration step in CI/CD

Run `alembic upgrade head` against the production DB **before** the new
image starts serving traffic — this is the standard expand-then-deploy
pattern. The migration is idempotent (re-running on a head DB is a
no-op), so a re-deploy after a failed image pull is safe.

```bash
# Recommended pattern: a one-shot pre-deploy job in your pipeline
docker run --rm \
  -e DATABASE_URL="${DATABASE_URL}" \
  ghcr.io/keboola/agnes-the-ai-analyst:${IMAGE_TAG} \
  alembic upgrade head

# Then deploy the app image. The image already contains alembic + the
# migrations dir — no extra build step needed.
```

> **Startup self-migration (#636).** On a Postgres backend the app brings the
> schema to the image's Alembic head **itself at boot**, mirroring the DuckDB
> ladder's self-migration on connect: when the DB is behind (or never
> stamped), the pending migrations are applied in-process under a Postgres
> advisory lock, so concurrent replicas serialize and the late starter
> no-ops. Set `AGNES_PG_AUTO_MIGRATE=0` to disable this and **fail closed at
> boot** instead — for deployments whose pipeline owns migrations via the
> pre-deploy `alembic upgrade head` above (the error names the current and
> head revisions). Two cases always fail closed regardless of the flag: a DB
> *ahead* of the image (app rollback after a newer image migrated — roll the
> image forward, never auto-downgrade), and a failed upgrade (never serve on
> a half-migrated schema). For emergency recovery only, set
> `AGNES_SKIP_PG_REVISION_CHECK=1` to boot past all of it (you will hit
> schema errors until you migrate). DuckDB backends self-migrate on connect
> and are unaffected.

For rollback discipline: every Alembic revision in this repo has a
real `downgrade()` body, validated by `test_full_chain_roundtrip` and
`test_pairwise_roundtrip` on every PR. To roll back one revision in
production:

```bash
docker run --rm -e DATABASE_URL=... ghcr.io/keboola/agnes-the-ai-analyst:${IMAGE_TAG} \
  alembic downgrade -1
```

### Connection-pool tuning

`src/db_pg.py` defaults to `pool_size=5, max_overflow=10, pool_timeout=30s`
— i.e. up to 15 concurrent connections per app process, each request
waiting up to 30s for one before failing. Cloud SQL's per-instance
connection cap (default 100, configurable up to thousands depending on
tier) is the binding constraint. For a 3-VM MIG running 1 uvicorn
worker each, 3 × 15 = 45 connections — comfortably inside the default
cap. Scale `pool_size` proportionally if you increase uvicorn workers
per VM.

Override with `AGNES_PG_POOL_SIZE` / `AGNES_PG_MAX_OVERFLOW` /
`AGNES_PG_POOL_TIMEOUT_S` (env vars, see `config/.env.template`) — the
defaults above are unchanged unless set. A process running the
`extraction` worker lane (`AGNES_WORKER_LANES=extraction`, see
`app/worker/runtime.py` and the `extraction-worker` compose service)
sizes `pool_size` automatically when `AGNES_PG_POOL_SIZE` is unset:
`extraction.concurrency + extraction.facts.concurrency`, capped at 64 —
a corpus-extraction slot and a facts-extraction pass each hold a
connection for their whole run, not per-statement, so the plain
request-scoped default of 5 starves under concurrent crawls + facts
passes (live finding, 2026-09: the pool exhausted 30×/10min under 2
facts passes + 4 crawls sharing one engine, failing 44 documents'
ingest with `TimeoutError` and forcing 27 facts-LLM-cache lookups to
fall through to a paid model call). Every other role/process is
unaffected. The resolved pool settings are logged once, at engine
creation.

## Running migrations

```bash
# Upgrade to head
DATABASE_URL=... alembic upgrade head

# Downgrade by one revision
DATABASE_URL=... alembic downgrade -1

# Generate offline SQL (for DBA review)
DATABASE_URL=... alembic upgrade head --sql > /tmp/up.sql

# Autogenerate a fresh revision from a model change
DATABASE_URL=... alembic revision --autogenerate -m "your message"
# (then hand-review the file in migrations/versions/)
```

## A database stranded by a renumbered revision

Revision ids are immutable once shipped — see
`migrations/shipped_revision_ids.txt`, the append-only manifest
`tests/test_alembic_revision_ratchet.py` checks on every PR. This section is
what to do when that discipline was violated before the ratchet existed
(issue #2086: `facts_ingest_runs` shipped as `0077_facts_ingest_runs`, then
got renumbered to `0078_facts_ingest_runs` when `0077_ontology_drafts` was
inserted ahead of it).

**What the state is.** A database that had already applied the OLD id is
stamped in `alembic_version` at a string no image's `migrations/versions/`
contains any more — not the current one, not any past or future one, because
the id itself stopped existing the moment it was renamed. `assert_pg_at_head`
cannot tell this apart from a plain app rollback by the stamped string alone,
so it disambiguates using this repo's strict `NNNN_name` numbering: an
unknown id whose leading 4-digit prefix is <= the shipped head's prefix
cannot have come from a newer image (a newer image's head number only goes
up), so it must be a **STRANDED** database rather than one that is merely
**AHEAD** — a distinct `RuntimeError` names the id and says so, instead of
telling the operator to "roll the image forward" (impossible: no image knows
that id) or restore a backup (usually unnecessary: the database's data is
fine, only its migration bookkeeping is confused).

**Why stamping forward without applying the skipped revision is unsafe.** The
stranded id is missing exactly the migration(s) that were inserted ahead of
it when it got renumbered — in the #2086 case, the `ontology_drafts` table.
Simply `UPDATE alembic_version SET version_num = '<the new id>'` would tell
Alembic "you are here" for a database that is NOT actually there: every
later migration that assumes `ontology_drafts` exists (a `add_column` on it,
a foreign key into it, …) now runs against a schema silently missing it, and
future `alembic upgrade head` runs never revisit the gap because, as far as
Alembic is concerned, that step is already behind it.

**The automatic repair.** `src/db_pg.py`'s `RENUMBERED_REVISION_REPAIRS` maps
a stranded id to the revision(s) that were inserted ahead of it and the id
that now occupies its old position in the chain. `ensure_pg_at_head()` (the
self-migrating startup path — see "Adding a PG-only feature" below for how
that path fits together) checks the DB's stamped revision against this map
before its normal behind-head upgrade: on a match it applies the listed
revision(s)' DDL directly, then atomically re-stamps `alembic_version` to the
map's target id (guarded by `WHERE version_num = <the stranded id>` plus a
rowcount check), then falls through to the ordinary upgrade-to-head — so one
boot cycle both repairs the gap and catches the database up to the image's
real head. The repair tolerates a half-applied prior attempt (a crash
between applying the DDL and re-stamping, or an operator having applied the
same DDL by hand already): each listed revision runs its own DDL inside a
savepoint, and a duplicate-object error is treated as "already done" rather
than crashing the boot loop. `assert_pg_at_head()` — the check-only path used
where `ensure_pg_at_head()` doesn't run — never applies this repair itself;
it only names it.

**Generic manual recipe**, for a stranding this map does not (yet) cover, or
for an operator who wants to apply the fix by hand instead of relying on the
next boot's auto-repair:

```bash
# 1. Identify the gap: which revision(s) exist in the CURRENT chain between
#    the stranded id's old position and where it was renumbered to. Read the
#    migration file(s)' upgrade() bodies — do not guess; a botched CREATE TABLE
#    against a database whose real state you're unsure of is the failure mode
#    this whole section exists to avoid.

# 2. Apply exactly that DDL by hand (adapt column/table names — this is
#    illustrative, not literal SQL to paste):
psql "$DATABASE_URL" -c '
    CREATE TABLE <the_skipped_table> (
        id VARCHAR PRIMARY KEY,
        ...
    );
    CREATE INDEX <the_skipped_index> ON <the_skipped_table> (...);
'

# 3. Re-stamp alembic_version — guarded by WHERE + a rowcount check so an
#    unexpected concurrent change is caught rather than silently overwritten:
psql "$DATABASE_URL" -c "
    UPDATE alembic_version
       SET version_num = '<the id that now occupies the old position>'
     WHERE version_num = '<the stranded id>';
"
# Confirm exactly one row changed ("UPDATE 1") before continuing.

# 4. Restart the app. ensure_pg_at_head() (or a manual `alembic upgrade
#    head`) picks up from the freshly-stamped id and migrates the rest of
#    the way normally — the stranded id no longer matches any
#    RENUMBERED_REVISION_REPAIRS key, so this is now an ordinary
#    behind-head upgrade.
```

Whichever path is used, **never renumber a revision id to "fix" this** — that
only moves the strand to whichever id gets reused. Register the repair in
`RENUMBERED_REVISION_REPAIRS` once identified, so the next database (or the
next operator) that hits the same stranded id self-heals on its next boot
instead of needing this recipe again.

## Adding a PG-only feature (post-A3)

**This is the default path for any new app-state table or repository.** The
DuckDB app-state backend is frozen (`CLAUDE.md` → "Dual-backend
discipline") — a new feature never touches `src/db.py` or gets a DuckDB
repo module.

1. **Define the SQLAlchemy model** under `src/models/<cluster>.py`. Import
   `Base` from `src.db_pg`, and add the import to `src/models/__init__.py`.
2. **Generate the Alembic migration** — `alembic revision --autogenerate -m
   "<table_name>"`. Read the generated file; verify `downgrade()` is the
   true inverse of `upgrade()`. Nothing lands in `src/db.py` —
   `SCHEMA_VERSION` does not move.
3. **Run round-trip + drift + pairwise tests**:
   ```bash
   pytest tests/db_pg/test_alembic_skeleton.py tests/db_pg/test_alembic_roundtrip.py
   ```
4. **Build ONLY the PG repository**, `src/repositories/<name>_pg.py`. There
   is no DuckDB sibling to mirror.
5. **Register it PG-only** in `src/repositories/__init__.py` `_REGISTRY`:
   ```python
   "<name>": {PG: ("src.repositories.<name>_pg", "<Name>PgRepository")},
   ```
   No `DUCKDB` key. `tests/test_repository_registry.py::test_registry_backends_are_symmetric`
   accepts this shape; `tests/test_repository_registry_pg_first_ratchet.py`
   and `tests/db_pg/test_repo_module_pg_first_ratchet.py` are the ratchets
   that keep a *new* DuckDB-backed entry or module from sneaking in
   elsewhere.
6. **A route that can reach this repo on a DuckDB-backed instance must fail
   clean, not crash.** Resolving the repo there raises
   `src.repositories.RequiresPostgresBackend` automatically (the factory's
   `_build()` does this for any PG-only entry) — the app-wide handler in
   `app/main.py` turns it into a `501` naming the feature. You do not need a
   bespoke try/except in the handler; just let the exception propagate.
7. **If the route is swept by `tests/db_pg/test_get_status_parity_sweep.py`
   / `test_mutation_status_parity_sweep.py`**, add its `"METHOD path"` key
   (with a one-line reason as the value — `_PG_ONLY_ROUTE_EXEMPTIONS` is a
   `dict[str, str]`) to that file's `_PG_ONLY_ROUTE_EXEMPTIONS` — the sweep's
   `assert_pg_only_exemptions_fail_clean` still requires the DuckDB side to
   answer a TYPED `501` (`body["error"] == "requires_postgres_backend"`),
   not merely some 4xx, so a genuine crash — or an unrelated 403/404 that
   fires before the repo is even reached — is still caught.
8. **Write PG-side tests** under `tests/db_pg/test_<cluster>_pg.py` and a
   PG-only-shaped test in the style of `tests/db_pg/test_mcp_sources_contract.py`
   (there is no DuckDB half to parametrize against — just exercise the PG
   repo directly).

Skip the `scripts/migrate_duckdb_to_pg/__init__.py:TASKS` step entirely — a
brand-new table has no DuckDB-side data to carry over. One registration in
that file still applies: if the table's primary key is anything other than a
plain `id` column, add it to `_PK_COLUMNS` — the PG→PG copy/validate paths
(cloud↔side-car DR) iterate `Base.metadata` and default to `SELECT id`, so
an unregistered non-`id` PK fails them
(`tests/db_pg/test_data_migration.py::test_non_id_pk_tables_are_in_pk_columns_map`
names the miss).

## Extending an EXISTING (frozen pre-A3) pair — no schema change

A new **method** on a repository that already has a DuckDB↔PG pair — a new
query or write path over columns that already exist — is not a schema
change and is unaffected by the freeze:

1. Add the method to the PG repo (`src/repositories/<name>_pg.py`) **and**
   its DuckDB sibling (`src/repositories/<name>.py`) in the same PR — this
   pair is frozen at "maintained", not "abandoned". Mirror signatures
   (`tests/db_pg/test_repo_method_parity.py` checks this statically).
2. Extend `tests/db_pg/test_<cluster>_contract.py` with the new behavior,
   parametrized over both backends.

**A genuine schema change on an existing pair's table (new column, new
index, new constraint) follows "Adding a PG-only feature" above, not this
section** — the DuckDB ladder is frozen regardless of whether the table
itself predates the ratchet. The new column lands in Postgres only; the
DuckDB side of that repo simply does not gain the capability that depends on
it. (`src/db_pg.py` `Base.metadata` still needs the model change either way.)

## Adding an index on a table that may already be huge in production

A migration runs at process start, inside the startup revision repair's own
transaction — `CREATE INDEX CONCURRENTLY` cannot run there (it needs
Alembic's `autocommit_block()` to commit that transaction first; see
`migrations/versions/0098_corpus_chunks_file_id_index.py`'s docstring). A
plain `CREATE INDEX` is fine for a cheap B-tree (that migration does exactly
that, unconditionally) but holds a SHARE lock for as long as the build takes
— seconds for a small/medium table, several MINUTES for a GIN or other
expensive index over a table already at production scale (tens of millions
of rows), stalling every writer for the duration.

The pattern (`migrations/versions/0101_corpus_chunks_fts_index.py`): count
the target table's rows first (`op.get_bind().execute(sa.text("SELECT
COUNT(*) FROM …")).scalar()`), build in place under a threshold comfortably
above what a fresh/lightly-loaded instance has and comfortably below the
scale that made the migration necessary in the first place, and above it
SKIP the build with a `logger.warning(...)` naming the EXACT `CREATE INDEX
CONCURRENTLY IF NOT EXISTS …` statement an operator must run out-of-band,
once, off-peak. Whatever the index accelerates must still WORK without it —
just slower (a sequential scan) — so a deployment that hits the skip branch
degrades, it does not break. Unit-test both branches by faking `alembic.op`
(`op.get_bind()` returning a stub whose `.scalar()` reports a controlled row
count) rather than actually seeding millions of rows — see
`tests/db_pg/test_corpus_chunks_fts_index_migration.py`.

## The four load-bearing tests

These run on every PR and protect against the most dangerous mistakes:

  - `test_baseline_upgrade_creates_only_alembic_version` — baseline must
    never grow user tables.
  - `test_full_chain_roundtrip` — `upgrade head → downgrade base → upgrade head`
    preserves the schema bit-for-bit. Catches a missing or broken
    `downgrade()` body.
  - `test_pairwise_roundtrip` — for every (N, N+1) pair, `upgrade N →
    upgrade N+1 → downgrade N` restores the schema. Catches single-step
    botches that the full-chain test might mask.
  - `test_no_model_migration_drift` — `Base.metadata` matches `head`
    schema, no extras on either side. Catches forgotten migrations and
    model/SQL drift.

## DuckDB → Postgres data migration

`scripts/migrate_duckdb_to_pg/` copies app state from a DuckDB
`system.duckdb` file into Postgres. The migration is **idempotent** —
re-running is safe via `INSERT ... ON CONFLICT (pk) DO NOTHING`.

```bash
# Dry-run first against a snapshot copy
python -m scripts.migrate_duckdb_to_pg \
  --duckdb-path /var/agnes-snapshot/system.duckdb \
  --dry-run --verbose

# Live migration
DATABASE_URL="postgresql+psycopg://..." \
  python -m scripts.migrate_duckdb_to_pg \
  --duckdb-path /var/agnes-snapshot/system.duckdb

# Single-table verification
python -m scripts.migrate_duckdb_to_pg --only users --verbose
```

The CLI returns exit code 1 if any post-copy validation fails (row count
mismatch or PK-set checksum diverged), and exit code 2 when the source
DuckDB file does not exist — a missing source during an operator-driven
cutover means a mis-mounted volume, not a fresh install. The
docker-compose `data-migrate` one-shot passes `--missing-source-ok`,
which turns the missing-source case into a logged no-op (exit 0) so a
brand-new data volume (fresh Postgres-backend deployment, no prior
DuckDB) can boot compose from scratch. The flag cannot be combined with
`--reset-target` (a one-time cutover requires an existing source).

## Backend choice for tests

### Local testing

The PG test suite (`tests/db_pg/`) runs against `pgserver` by default — that
ships a Postgres 16 binary inside its wheel, so no Docker or system PG is
required. To run against a real container instead:

```bash
AGNES_TEST_PG_BACKEND=container .venv/bin/pytest tests/db_pg/  # needs Docker
AGNES_TEST_PG_BACKEND=embedded  .venv/bin/pytest tests/db_pg/  # needs system postgres
```

CI matrices that want higher fidelity should set `container` explicitly.

| `AGNES_TEST_PG_BACKEND=` | Where it gets PG | When to use |
|---|---|---|
| _(unset)_ / `pgserver` | userland-bundled Postgres 16 from the `pgserver` PyPI package | default — works on any dev box, no install needed |
| `container` | testcontainers boots `postgres:16-alpine` via Docker | CI or local fidelity runs with Docker |
| `embedded` | pytest-postgresql + system `postgres` binary | dev laptop with Postgres installed |

## Future improvements

Cheap upgrades that the project research validated but hasn't yet landed:

- **`atlas migrate lint` CI step** — Atlas reads the Alembic-generated
  SQL and surfaces lock risks, missing indexes on FK columns, and unsafe
  column drops *before* a migration ships. Pure linter; doesn't replace
  Alembic. 30-min wire-up.
- **Expand/contract branch discipline (OpenStack pattern)** — Alembic
  supports named branches; the dual-write window during repository
  cutover maps directly. See
  https://docs.openstack.org/neutron/latest/contributor/alembic_migrations.html
  No new tooling required.
- **pgroll** for *post-migration* zero-downtime schema evolution. Defer
  until pgroll hits v1.0 and a Python SDK lands; for the one-time DuckDB
  cutover, the Alembic + OpenStack pattern is sufficient.
