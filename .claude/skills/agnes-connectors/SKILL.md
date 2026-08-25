---
name: agnes-connectors
description: Rules for the extract.duckdb contract every data source must produce — the _meta table, the _remote_attach mechanism for remote-mode tables, parquet layout, and the pattern for adding a new connector. Use when adding a new data source or modifying an existing extractor in connectors/.
---

# Agnes connectors — the extract.duckdb contract

Every data source produces the same output:

    /data/extracts/{source_name}/
    ├── extract.duckdb          ← _meta table + views
    └── data/                   ← parquet files (local sources only)

See `CLAUDE.md § Architecture: extract.duckdb Contract` and
`docs/architecture.md`.

## Required `_meta` table

Every `extract.duckdb` MUST contain a `_meta` table with these columns:

| column | type | meaning |
|---|---|---|
| `table_name` | VARCHAR | name used in views |
| `description` | VARCHAR | human-readable description |
| `rows` | BIGINT | row count at extraction time |
| `size_bytes` | BIGINT | parquet size for local mode, 0 for remote |
| `extracted_at` | TIMESTAMP | extraction time |
| `query_mode` | VARCHAR | one of `local`, `remote`, `materialized` |

If `_meta` is missing or malformed, `SyncOrchestrator.rebuild()` skips the
source with an error logged. Tests for new connectors MUST assert `_meta` is
well-formed.

## Four connector shapes

- **Batch pull** (Keboola, `query_mode='local'`) — DuckDB extension downloads
  data to parquet, scheduled. Extractor in
  `connectors/<name>/extractor.py`.
- **Remote attach** (BigQuery, `query_mode='remote'`) — DuckDB BQ extension,
  no download. Queries hit the upstream at query time. Requires `_remote_attach`.
- **Materialized SQL** (`query_mode='materialized'`) — scheduler runs
  admin-registered SQL through DuckDB and writes the result to a parquet under
  `/data/extracts/<source>/data/`. Distributed via the same manifest +
  `agnes pull` flow as `local`. BigQuery cost guardrail:
  `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB; `0` disables).
- **Real-time push** (Jira) — webhooks update parquets incrementally; the
  webhook handler triggers `rebuild_source('jira')`.

## `_remote_attach` table (remote mode only)

For each remote-mode table in `_meta`, the extractor writes a row in
`_remote_attach` with `alias`, `extension`, `url`, `token_env`. See the
`agnes-orchestrator` skill for how the orchestrator consumes it.

## Adding a new connector — checklist

1. Create `connectors/<name>/extractor.py` that emits `extract.duckdb` (+
   `data/*.parquet` if local) into `/data/extracts/<name>/`.
2. Populate `_meta` with one row per table.
3. If any table is `query_mode='remote'`, populate `_remote_attach`.
4. Register the connector type in the catalog (search for existing
   `source_type` values to follow the pattern).
5. Add a fixture-based test that runs the extractor against a fixture
   upstream and asserts `_meta` is complete.
6. CHANGELOG bullet under `Added` per `agnes-release-process`.

## Stable infrastructure — do NOT modify

`connectors/jira/file_lock.py`. (`connectors/jira/transform.py` was
previously off-limits but as of 0.54.19 is no longer; it remains
sensitive — touch only with end-to-end understanding of the
JSON-overlay / parquet-rewrite pipeline.)
