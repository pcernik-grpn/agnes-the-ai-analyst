# Data Sources

## Overview

Agnes uses a connector system where each connector produces an `extract.duckdb` following a standard contract. The SyncOrchestrator auto-discovers and ATTACHes these into the master `analytics.duckdb`.

Configure the data source type in `config/instance.yaml`:

```yaml
data_source:
  type: "keboola"  # Options: keboola | bigquery | local
```

| Value | What it means |
|---|---|
| `keboola` | Pulls tables from the Keboola Storage API (configure `stack_url` + token). |
| `bigquery` | Queries BigQuery remotely via the DuckDB BQ extension (configure the `bigquery` block). |
| `local` | No external source — CSV/parquet placed in the data directory, plus the tables that file uploads under `/library` create. This is the value for an instance with no warehouse behind it. |

`csv` is accepted as an alias for `local`; `local` is the canonical name. There
is no separate CSV *connector* — nothing under `connectors/` handles it — so
choosing `csv` expecting a Keboola-style pull configures an instance with no
external source at all.

Table definitions are stored in the DuckDB `table_registry` table (not in config files). Register tables via the admin API, CLI, or web UI.

## Connection ownership: `source_connections` vs `instance.yaml`

Keboola, BigQuery, Snowflake and Databricks each get one managed row in the
`source_connections` table, reachable via `POST/GET/PUT/DELETE
/api/admin/source-connections` (`agnes admin connection …`). A row's `config`
is validated and normalized at write time by `src/connection_specs.py` — an
unknown `source_type` or a malformed config (e.g. a non-`https://`
`stack_url`) is rejected with a 400 naming the field. On first boot,
`app/connections_seed.py` seeds a row for each type from whatever
`instance.yaml` / env vars are already configured — a one-time, no-op-if-a-row-
already-exists copy, not a live sync; editing `instance.yaml` afterwards logs a
deprecation warning and is otherwise ignored.

The row is the single live source of truth per source type, resolved fresh on
every call (`resolve_snowflake_settings()` / `resolve_databricks_settings()` /
`get_bq_access()`), with `instance.yaml` as a fallback only for an
un-migrated instance that has no row yet:

| Source | Config actually used at query time TODAY | Row seeded on first boot |
|---|---|---|
| `keboola` | the `source_connections` row (multi-project, #1530) | yes |
| `bigquery` | the `source_connections` row; `instance.yaml` (`data_source.bigquery`) only when no row exists | yes |
| `snowflake` | the `source_connections` row; `instance.yaml` (`data_source.snowflake`) only when no row exists | yes |
| `databricks` | the `source_connections` row; `instance.yaml` (`data_source.databricks`) only when no row exists | yes |

Every type is fully migrated: the row is load-bearing everywhere, an admin
edit is visible on the very next call with no restart, and the "Add data
source" wizard's Snowflake/Databricks panes save onto the row (and its own
vault slot) directly rather than the `data_source.<type>` yaml overlay. A
hand-edited `data_source.<type>.*` yaml block is IGNORED (not merely stale)
once a row of that type exists — `app/connections_seed.py`'s deprecation
warning names the field. Multi-connection-per-type (more than one Snowflake/
Databricks/BigQuery connection per instance) is an explicit follow-up — see
`docs/superpowers/plans/2026-08-26-derived-connection-model.md`.

## Query Modes

Each table has a `query_mode` that determines how data is accessed:

- **`local`**: Data is downloaded to parquet files on the Agnes server. Suitable for tables that fit in local storage.
- **`remote`**: Data stays in the external source; DuckDB extension ATTACHes at query time. Suitable for large tables where only query results are transferred.

## Keboola Connector

Syncs tables from Keboola Storage API using the DuckDB Keboola extension.

### Requirements

- Keboola Storage API token with read access
- DuckDB Keboola extension (auto-installed)

### Configuration

In `.env`:
```
KEBOOLA_STORAGE_TOKEN=your-token-here
KEBOOLA_STACK_URL=https://connection.your-region.keboola.com
KEBOOLA_PROJECT_ID=12345
```

Or configure via the admin UI (`/admin/tables`) or CLI:
```bash
agnes admin register-table --source-type keboola --bucket "in.c-crm" --table "company" --query-mode local
```

### How it works

1. The extractor (`connectors/keboola/extractor.py`) uses the DuckDB Keboola extension to download data
2. Produces `extract.duckdb` with `_meta` table + parquet files in `/data/extracts/keboola/data/`
3. The SyncOrchestrator ATTACHes `extract.duckdb` into `analytics.duckdb` and creates views

### Identifier validation

All Keboola table names, bucket names, and source table identifiers are validated against `_SAFE_QUOTED_IDENTIFIER` regex before use. Invalid identifiers are skipped with error logging.

### Semantic-layer sync (metrics & glossary)

Separately from table sync, Agnes can import Keboola's business-semantic layer — metric definitions and glossary terms — into `metric_definitions` and `glossary_terms` (the `keboola-semantic-layer-refresh` job, `POST /api/admin/run-keboola-semantic-layer-refresh`).

- **Master token requirement.** This sync calls Keboola's Metastore API, which rejects any token that isn't a master (owner) Storage API token — a regular read-scoped token 400s with an opaque error. Because of this, the master token is a *separate* vault slot from the plain storage token used for table pulls.
- **Where to set it.** Either the "Master token (semantic layer)" control on a Keboola connection's card at `/admin/data-sources`, or `agnes admin connection secret <connection_id> --kind master` (prompts for the token; never pass it on the command line). Saving runs a live `verify_token` preflight and rejects a non-master token immediately rather than failing later during sync.
- **Multi-project behavior.** Every Keboola connection with a master token configured syncs on its own — each connection's metric/glossary rows are stamped with that connection's `source_ref` for provenance, and each sync's prune (removing rows no longer present upstream) only touches rows carrying its own `source_ref`. One connection's sync failure or token removal never affects another connection's rows.
- **Orphaned rows.** Removing a connection's master token (or the connection itself) stops that project from syncing, but its previously-imported rows are left in place rather than deleted — they show up as "orphaned" (a `source_ref` that no longer matches any connected source) on `GET /admin/semantic-layer`, which lists per-connection sync status/counts alongside the orphaned set so an admin can decide whether to leave them or clean them up.

## BigQuery Connector

Queries BigQuery tables on-demand using the DuckDB BigQuery extension (remote attach).

### Requirements

- Google Cloud project with BigQuery access
- Application Default Credentials (ADC) configured

### Configuration

In `config/instance.yaml`:
```yaml
bigquery:
  project_id: "your-gcp-project"
```

## BigQuery Adapter

Registers BigQuery tables and views as remote DuckDB views (no data download). Queries
issued through the master `analytics.duckdb` are forwarded to BigQuery via the DuckDB
BigQuery extension. See also `agnes snapshot create` for the analytical workflow that materializes
filtered subsets locally.

### Requirements

- DuckDB BigQuery extension (auto-installed by the extractor on first run).
- A GCP service account with `bigquery.metadata.get` on the dataset and
  `bigquery.data.viewer` (or finer) on the table; `bigquery.jobs.create` on the
  billing project for views and `agnes snapshot create` queries.
- Credentials resolution: GCE metadata server first, then Application Default
  Credentials (`gcloud auth application-default login` or
  `GOOGLE_APPLICATION_CREDENTIALS`). See `connectors/bigquery/auth.py`.

### Configuration

In `config/instance.yaml`:

```yaml
data_source:
  type: bigquery
  bigquery:
    project: my-data-project              # data + default billing project
    billing_project: my-billing-project   # optional override; needed when SA
                                          # lacks serviceusage.services.use on
                                          # the data project
    location: us
```

### Registering BigQuery tables

Two ways, both API-first (no manual `table_registry` SQL).

**Web UI** — go to `/admin/tables`. With `data_source.type: bigquery` the page
swaps the discovery panel for a "Register BigQuery table" button that opens a
manual-entry modal: dataset, source table, view name, description, folder,
optional sync schedule. Submit runs `/api/admin/register-table/precheck` first
(round-trips `bigquery.Client.get_table` to confirm the table exists and the SA
can see it), surfaces the row count + size + column count, then commits.

**CLI** — `agnes admin register-table`:

```bash
# Dry-run: validate + check the source exists, no DB write.
agnes admin register-table orders \
    --source-type bigquery \
    --bucket analytics \
    --source-table orders \
    --dry-run

# Commit
agnes admin register-table orders \
    --source-type bigquery \
    --bucket analytics \
    --source-table orders \
    --description "Order data from BQ"
```

The server forces `query_mode=remote` and `profile_after_sync=false` for BQ
rows. Sync schedule (`--sync-schedule`) is accepted and stored but not yet
evaluated by the scheduler — see issue #79; addressed in Milestone 3 of the
admin-BQ-registration epic (#108).

### Wildcard / sharded tables

Not supported in M1. The register endpoint rejects any `source_table` containing
`*`. Tracked in #108 M3+.

### Hybrid Queries

Server-side only. Admins can POST `{sql, register_bq: {alias: bq_sql}}` to
`/api/query/hybrid` (`app/api/query_hybrid.py`); the BigQuery sub-queries
run server-side, where BQ credentials live, and the join runs against the
server's local parquet views in a single DuckDB session.

Analysts who need to combine a local table with a remote one should
`agnes snapshot create` a filtered slice of the remote table and join it
locally, or run the join server-side via `agnes query --remote`. The
earlier `agnes query --register-bq` flag (which ran in-process on the
caller's machine) was removed because it required local BigQuery
credentials that analysts don't have.

## Jira Connector

Real-time webhook-based connector that updates parquet files incrementally.

### How it works

1. Jira webhooks hit `/api/jira/webhook` endpoint
2. The connector (`connectors/jira/`) processes webhook events and updates parquet files
3. Produces `extract.duckdb` with `_meta` table + incremental parquet data

## Databricks Connector

Talks to a Databricks SQL warehouse over the Statement Execution API (Arrow
result stream — no Databricks SDK), in two modes, and syncs the workspace's
Unity Catalog **metric views** (Databricks's semantic layer) into Agnes's
`metric_definitions` registry.

| Mode | What happens | Use it for |
|---|---|---|
| `materialized` | The scheduler runs the row's SQL on the warehouse and writes a parquet, distributed like any other materialized row. | Anything queried repeatedly. Cheap, cached, `agnes pull`-able, joinable with local data. |
| `remote` | Nothing syncs. Each analyst statement ships to the warehouse and the rows come back. | Ad-hoc questions, tables too large to materialize, and `MEASURE()` over a metric view — which only evaluates on Databricks compute. |

### Requirements

- A SQL warehouse (its ID is under *Connection details*).
- A workspace PAT or OAuth M2M access token in the `DATABRICKS_TOKEN` env
  var (or the vault) — never in YAML.

### Configuration

```yaml
data_source:
  databricks:
    host: "${DATABRICKS_HOST}"                  # https://dbc-....cloud.databricks.com
    warehouse_id: "${DATABRICKS_WAREHOUSE_ID}"
    catalog: "main"                             # default Unity Catalog catalog
    # max_bytes_per_materialize: 10737418240    # result-size cap, 0 disables
    # statement_timeout_seconds: 900            # client-side deadline, 0 disables
    # max_bytes_per_remote_query: 1073741824    # `--remote` result cap, 0 disables
    # remote_query_timeout_seconds: 120         # `--remote` deadline
    # attach_enabled: false                     # EXPERIMENTAL Unity Catalog ATTACH
```

Works as a secondary source next to any primary `data_source.type` — the
block's presence is what enables `source_type=databricks` registrations
(same rule as any secondary source).

### Registering tables

`query_mode` may be `materialized` or `remote`; `local` is rejected at
register time (no extractor subprocess would ever populate it). Materialized
rows come in two shapes:

```bash
# Custom SQL — this is where semantic-layer queries live:
agnes admin register-table --name revenue_by_day --source-type databricks \
    --query-mode materialized \
    --source-query 'SELECT order_date, MEASURE(`Total Revenue`) FROM `main`.`sales`.`kpis` GROUP BY order_date'

# Full-table dump — server generates SELECT * FROM `catalog`.`schema`.`table`:
agnes admin register-table --name orders --source-type databricks \
    --query-mode materialized --bucket sales --source-table orders
```

`bucket` is the schema inside the configured default catalog; a dotted
bucket (`other_catalog.sales`) overrides the catalog per row. The scheduler
materializes due rows on their `sync_schedule`, writes
`extracts/databricks/data/<name>.parquet`, and the row rides the normal
manifest + `agnes pull` distribution.

**Cost guardrail:** the Statement Execution API has no dry-run primitive
(unlike BigQuery), so `max_bytes_per_materialize` caps the statement
**result** size via the API's `byte_limit`. A result truncated at the cap is
rejected with a structured error — never written as data.

### Remote rows (`query_mode='remote'`)

```bash
agnes admin register-table --name orders --source-type databricks \
    --query-mode remote --bucket sales --source-table orders_raw
```

No `--source-query`: the statement that runs is the analyst's.
`bucket`+`source_table` are what a bare reference to the row gets rewritten
into.

```bash
agnes query --remote "SELECT country, COUNT(*) FROM orders GROUP BY 1"
agnes query --remote "SELECT o_date, MEASURE(\`Total Revenue\`) FROM revenue_kpis GROUP BY o_date"
```

The statement is sent to the warehouse in **Databricks SQL**, not DuckDB
flavor and not transpiled — a query that works in the Databricks UI works
here. Agnes rewrites only table identifiers: a registered bare name, or a
direct `dbx."<catalog>.<schema>"."<table>"` path, becomes
`` `catalog`.`schema`.`table` ``.

**Every table the statement names must be registered and granted.** The query
runs under the workspace PAT, which can typically read the whole workspace, so
Agnes parses the statement (sqlglot, `databricks` dialect) and checks each
table reference against the registry and the caller's grants. A path Agnes does
not recognise — `` `main`.`hr`.`payroll` ``, a bare `hr.payroll`, an
unregistered name — is refused (`databricks_table_not_registered`), even when
it rides along with a legitimate one in the same JOIN, and even for an admin:
the admin bypass covers *grants*, never registration. CTEs defined in the
statement are of course fine. A statement Agnes cannot parse is refused
(`databricks_sql_unparseable`) rather than forwarded — an unparseable statement
is precisely the one whose references cannot be checked.

**Cost guardrail, and how it differs from BigQuery's.** BigQuery prices a
statement before running it, so Agnes can refuse an over-cap query having
spent nothing. Databricks has no dry-run. What it has is `byte_limit`, so
`max_bytes_per_remote_query` (default 1 GiB) caps **the bytes the warehouse
may return** — not the bytes it scanned to produce them. Hitting the cap
raises `remote_scan_too_large`; the result is never returned short, because a
plausible number that is quietly missing rows is the worst possible answer.
Warehouse compute is bounded by `remote_query_timeout_seconds` (default 120).
`bytes_scanned` in the query response therefore means *returned* bytes for
Databricks rows and *scanned* bytes for BigQuery ones.

**What a remote row cannot do.** The statement runs entirely on the
warehouse, so it cannot see anything that only exists on this server:

- Joining a remote Databricks table with a local/materialized table is
  refused (`remote_cross_source_unsupported`) unless the Unity Catalog ATTACH
  below is enabled. Materialize the Databricks side and join locally.
- A statement naming remote tables on two engines (Databricks + BigQuery) is
  refused (`remote_cross_engine_unsupported`). There is no join layer between
  them.

### Snapshots of a remote Databricks row

`agnes snapshot create` works on a remote Databricks row, in both shapes —
the `table_id` form (`--select` / `--where` / `--limit` / `--order-by`) and
`--from-query`. The predicate is written in **Databricks SQL**, the flavor
`agnes schema` advertises for the row.

```bash
agnes snapshot create orders --select country,n \
    --where "country = 'CZ' AND o_date >= DATE_SUB(CURRENT_DATE(), 30)" --estimate
agnes snapshot create orders --select country,n --where "country = 'CZ'" --as cz_orders
agnes query "SELECT country, COUNT(*) FROM cz_orders GROUP BY 1"
```

**`--estimate` reports differently here, on purpose.** Databricks has no
dry-run, so `estimated_scan_bytes` is `n/a`, not `0` — `0` already means
"served locally, nothing billable" on this response, and printing it for a
warehouse scan would read as *free*. What the estimate does give you is a
**real row count**: a `COUNT(*)` carrying your own predicate, which the
warehouse answers as an aggregate without shipping rows. The `engine` field
names which engine answered.

Size is bounded by `api.scan.max_result_bytes` (default 2 GiB) rather than the
interactive `max_bytes_per_remote_query`, and the statement timeout by
`data_source.databricks.scan_timeout_seconds` (default 900, `0` disables) — a
snapshot is a materialize, not an answer somebody is waiting on. A result that
hits the byte cap raises `remote_scan_too_large` rather than arriving short.

### Access policies on a remote row

A table carrying an [access policy](table-access-policies.md) **is** enforced
on Databricks. The policy body is transpiled to Databricks SQL, substituted
into the caller's statement, and its identity values are bound through the
Statement Execution API's `parameters` field — never spliced into SQL text.
sqlglot renders a policy's `$user_email` as `:user_email`, which is exactly
the API's named-parameter marker, so one authored policy body keeps that
guarantee on all three engines (DuckDB `$name`, BigQuery `@name`, Databricks
`:name`).

Two details worth knowing:

- **`$user_groups` is expanded, not interpolated.** The API binds scalar
  parameters only, so the array marker is rewritten to `ARRAY(:p0, :p1, …)`
  over generated scalar markers. The group names still travel as request
  fields; only the *number* of them is visible in the statement. A caller in
  no groups gets a typed empty array and matches nothing.
- **The registry gate runs twice.** The first pass sees the caller's SQL and
  enforces *their* grants. The second sees the statement *after* substitution,
  because a policy body can name tables the caller never wrote — §15's
  `policy_mapping` join. That second pass checks **registration only**: a table
  the caller may not read is fine inside a policy body — that is the whole
  point of the idiom — but a table Agnes cannot resolve denies (`policy_error`)
  rather than shipping a bare name to resolve against whatever the default
  catalog holds.

  **A mapping table joined by a policy on a remote row must itself be a
  `query_mode='remote'` Databricks row.** The statement runs entirely on the
  warehouse, which cannot read a local parquet, so a mapping table registered
  `local` or `materialized` denies. The caller's error is deliberately
  table-scoped (naming the policy body's tables would leak its contents), so
  the specific reason is written to the server log instead.
- **A duplicate output column denies.** A masking policy written
  `SELECT * EXCEPT (national_id), md5(email) AS email` still emits the
  plaintext `email` from the star alongside the masked one. DuckDB reads are
  checked before execution and BigQuery rejects such a result itself, but
  Spark permits duplicate column names, so the returned column list is checked
  and the rows are refused rather than handed over.

`/api/v2/scan` is the one surface that still refuses a policied Databricks
table (`policy_unsupported_on_scan_engine`). It has no caller-authored
statement to substitute a policy into — it builds one from `table_id` +
`select` + `where` — so it points you at `agnes query --remote`, which does
enforce, or at a materialized row whose scan reads a local parquet.

### Unity Catalog ATTACH (experimental, off by default)

```yaml
data_source:
  databricks:
    attach_enabled: true   # default false
```

Installs the `uc_catalog` + `delta` DuckDB community extensions and ATTACHes
the workspace's Unity Catalog under the `dbx` alias, giving each
`query_mode='remote'` row a local master view. That buys the one thing the
warehouse path cannot do — JOINing a Databricks table against local parquets
— at the cost of much weaker pushdown (predicates the warehouse resolves in
seconds become Delta file scans). All-Databricks statements keep using the
warehouse path even when this is on.

Off by default for three reasons worth knowing before turning it on:

1. It installs community extensions from the DuckDB community repository at
   rebuild time.
2. The ATTACH sends a live workspace PAT to the endpoint. Pin it with
   `AGNES_REMOTE_ATTACH_HOST_ALLOWLIST` — the same control that governs every
   other credentialed ATTACH, and it applies here.
3. **The ATTACH mechanism itself has not been verified against a live
   Databricks workspace.** The `_remote_attach` contract, the view DDL, the
   opt-in gate, the identifier refusals, and the credential-egress allowlist
   are covered by tests; whether `uc_catalog` installs, authenticates, and
   returns rows against a real workspace is not. Treat the first enablement
   as a trial. The SQL-warehouse paths this connector otherwise relies on —
   `materialized` rows (`materialize_query`), `remote` rows
   (`execute_select`/`execute_scan_to_arrow`), and semantic-layer metric-view
   discovery (`_list_metric_views` + the `SHOW CREATE TABLE ... $$<yaml>$$`
   parse) — ARE covered against a real workspace, by
   `tests/test_live_databricks.py`: `pytest tests/test_live_databricks.py -m
   live -v` with `DATABRICKS_HOST`, `DATABRICKS_TOKEN`,
   `DATABRICKS_WAREHOUSE_ID` (and optionally `DATABRICKS_CATALOG`, to also
   exercise the metric-view discovery test) exported. Dev-local only — not
   wired into CI, same as the BigQuery/Keboola/Jira `-m live` siblings.

With it off — the default — remote rows still work; every statement simply
runs on the SQL warehouse.

### Semantic-layer sync (Unity Catalog metric views)

Unity Catalog metric views import into the [semantic layer](semantic-layer.md)
through the `databricks_metric_views` adapter
(`connectors/databricks/semantic_ossie.py`), the same
git/upload/connection pipeline every other semantic source uses — not a
direct `metric_definitions` writer. `POST
/api/admin/run-databricks-semantic-layer-refresh` (scheduler default: every
6 h, `SCHEDULER_DATABRICKS_SEMANTIC_LAYER_REFRESH_INTERVAL`) registers the
workspace as a `connection`-kind semantic source the first time it runs
(fixed id `databricks_default`) and syncs it:

- enumerates metric views per configured catalog
  (`information_schema.tables`, `table_type='METRIC_VIEW'`), reads each
  YAML definition (`SHOW CREATE TABLE`), and composes one Apache Ossie
  document per view — measures become metrics, dimensions become dataset
  fields;
- every expression is tagged dialect `DATABRICKS`: readable in the catalog,
  not spliceable into a local DuckDB query
  (`src/semantic/dialect.py`). A metric's expression is the full,
  actionable statement (`SELECT MEASURE(...) FROM <metric view>`), because
  `MEASURE()` only evaluates against its owning view — an agent runs it
  server-side (a materialized row, or `agnes query --remote`);
- rows land in `metric_definitions` stamped `source='ossie_connection'` +
  `source_ref='databricks_default'`, projected by the same
  `src/semantic/projection.py` every source is projected through — a name
  already owned by another writer is skipped, never shadowed;
- pre-cutover rows (`source='databricks_semantic_layer'`, from the retired
  direct writer) are reconciled once per run — idempotent, a no-op once
  they are gone.

`agnes catalog --metrics` then surfaces the definitions to analysts and
agents like any other metric. Extra catalogs can be enumerated via
`data_source.databricks.semantic_layer_catalogs`, or per-sync via the
semantic source's `config.catalogs`.

## Writing a Custom Connector

Create a new connector in `connectors/<name>/extractor.py` that produces the `extract.duckdb` contract:

```
/data/extracts/{source_name}/
├── extract.duckdb          ← _meta table + views
└── data/                   ← parquet files (local sources only)
```

### Required: `_meta` table

```sql
CREATE TABLE _meta (
    table_name   VARCHAR NOT NULL,
    description  VARCHAR,
    rows         BIGINT,      -- 0 for remote
    size_bytes   BIGINT,      -- 0 for remote
    extracted_at TIMESTAMP,
    query_mode   VARCHAR      -- 'local' | 'remote' | 'materialized'
);
```

### Optional: `_remote_attach` table (for remote sources)

```sql
CREATE TABLE _remote_attach (
    alias     VARCHAR,  -- DuckDB alias used in views
    extension VARCHAR,  -- Extension name
    url       VARCHAR,  -- Connection URL
    token_env VARCHAR   -- Env-var name holding the auth token (NOT the token itself)
);
```

### Identifier validation

Import shared validators from `src/identifier_validation.py`:

```python
from src.identifier_validation import validate_identifier, validate_quoted_identifier
```

Use `validate_identifier()` for strict names (alphanumeric + underscore) and `validate_quoted_identifier()` for names that may contain dots/hyphens (e.g., Keboola-style `in.c-crm.orders`).

The SyncOrchestrator auto-discovers connectors by scanning `/data/extracts/*/extract.duckdb` — no registration step needed beyond producing the correct output format.

See `connectors/keboola/` for a complete batch-pull reference implementation, or `connectors/bigquery/` for a remote-attach example.
