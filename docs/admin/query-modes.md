# Query Modes — when to register a table as `local`, `remote`, or `materialized`

Source-agnostic guide to the three `query_mode` values Agnes supports. Pick the right mode at registration time and the analyst-side experience is fast, cost-aware, and predictable. Pick wrong and you'll either burn BQ scan budget on every query or spend hours waiting on syncs that didn't need to happen.

## TL;DR — decision tree

```
Is the table small (< 1 GB) and updated daily-or-slower?
  └─ YES → query_mode: local       (sync to laptop, query offline)

Is the table the result of an aggregate SQL the operator controls?
  └─ YES → query_mode: materialized  (server runs SQL → parquet, distributed)

Otherwise:
  └─ query_mode: remote   (data stays in upstream; analyst queries on demand)
```

## Three modes side-by-side

| Aspect | `local` | `materialized` | `remote` |
|---|---|---|---|
| Where the data lives | Analyst laptop (parquet) | Agnes server filesystem (parquet) | Upstream (BigQuery, Keboola, …) |
| Who runs the query | Analyst's local DuckDB | Analyst's local DuckDB | Upstream engine via DuckDB extension |
| Cost model | Free after sync | Free after each sync | Per-query scan cost on the analyst's first hit |
| Freshness | As fresh as last sync | As fresh as last scheduled run | Live |
| Scan limits | None (laptop disk) | None (server disk) | `bq_max_scan_bytes` cost gate (default 5 GiB) |
| Best for | Stable reference data, daily-updated facts | Aggregates, daily snapshots | Big tables, live data, residency-restricted |

## The second axis: `server_only` — queryable, never distributed

`query_mode` decides **where the query runs**. `server_only` decides **whether the parquet leaves the server**. They are independent: a `local` or `materialized` table with `server_only=true` is synced and kept fresh on the server, stays listed in the catalog, stays subject to the same RBAC — but `agnes pull` never downloads it, so it never lands on an analyst laptop.

Use it when the data may be *queried* by a group of people but must not be *copied* onto their machines: HR tables, salary data, anything under a residency or retention rule, or simply a table too large to be worth distributing.

| | Analyst runs `agnes query` | Analyst runs `agnes query --local` | Analyst runs `agnes query --remote` | Parquet on laptop | Listed in catalog |
|---|---|---|---|---|---|
| `local` | ✅ local view | ✅ local view | ✅ | ✅ | ✅ |
| `local` + `server_only` | ✅ falls back server-side | ❌ "table does not exist" | ✅ | ❌ | ✅ |
| `remote` | ✅ falls back server-side | ❌ "table does not exist" | ✅ | ❌ | ✅ |

The first column is the DEFAULT, `--scope auto`: `agnes query` runs locally
first and transparently re-runs server-side when the table has no local view,
printing a `[scope]` note to stderr saying where it ran. The hard failure is
the `--local` column — `--scope local` is the explicit "do not leave this
machine" mode, and it is the only one that refuses.

**Register via CLI:**

```bash
agnes admin register-table salaries \
    --source-type keboola \
    --bucket in.c-hr \
    --source-table salaries \
    --query-mode local \
    --server-only
```

**Register via API:** set `"server_only": true` on `POST /api/admin/register-table`.

`server_only=true` is rejected with `query_mode: remote` — a remote row has no server-stored parquet to suppress, so the pairing is incoherent. Both the CLI and the API validator refuse it.

An analyst querying a `server_only` table with plain `agnes query` gets their answer: the default `--scope auto` finds no local view and re-runs the query server-side, noting `[scope] '<table>' not found locally — running server-side`. Only `--local` / `--scope local` refuses, with a "table does not exist" error and a hint pointing at `--remote`. `agnes catalog` surfaces the flag as the `server_only` field and in `fetch_via`.

`server_only` is therefore a *distribution* control, not an access control: it stops the parquet reaching the laptop, and RBAC alone decides who may read the data. Do not reach for it to hide a table from someone who has been granted it.

## Per-source-type reference

### BigQuery — `query_mode: remote`

The most common use case for `remote`. Data stays in BQ; analysts query on demand via the Agnes server's service account.

**IAM:** the server's SA must have:
- `roles/bigquery.dataViewer` on the dataset (read access)
- `roles/bigquery.jobUser` on the *billing* project (run jobs)

If `data_source.bigquery.billing_project == data_source.bigquery.project`, set the SA's `serviceusage.services.use` permission too — the BQ extension can otherwise 403 USER_PROJECT_DENIED on the first query. The instance health check (`agnes diagnose`) surfaces this as an `info`-tier entry on `bq_config`.

**Register via UI:** `/admin/tables` → "Add table" → Source type `bigquery` → Mode `remote` → fill `dataset` (your BQ dataset name) + `source_table` (the BQ table id within that dataset).

**Register via CLI:**

```bash
agnes admin register-table sales_2024 \
    --source-type bigquery \
    --bucket dwh_base \
    --source-table sales_2024 \
    --query-mode remote
```

After registration, smoke-test the SA's access:

```bash
agnes query --remote "SELECT COUNT(*) FROM sales_2024"
```

A 403 here means the SA is missing `dataViewer` or `jobUser`; fix in IAM and re-test.

**Cost guardrail:** `bq_max_scan_bytes` (default 5 GiB) refuses queries whose pre-execution scan estimate exceeds the cap. Configurable in `/admin/server-config`. When an analyst hits the cap, the response includes a hint to use `agnes snapshot create --where '<predicate>'` to materialise a scoped subset locally.

### BigQuery — `query_mode: materialized`

The server runs a scheduled SQL aggregate against BigQuery and writes the result to a parquet on the Agnes filesystem. Analysts get the parquet via `agnes pull` like any other local table.

**Register via CLI:**

```bash
agnes admin register-table monthly_kpis \
    --source-type bigquery \
    --bucket dwh_base \
    --source-table monthly_kpis \
    --query-mode materialized \
    --query @path/to/monthly_kpis.sql \
    --sync-schedule "daily 03:00"
```

**Cost guardrail:** `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB; set `0` to disable) refuses materialise runs whose query plan exceeds the cap. Catches a typo'd `WHERE` clause that would otherwise scan a year of data.

**Analyst reads are served from the parquet:** `agnes snapshot create` / `POST /api/v2/scan` (and `/estimate`) on a materialized table read the server-side parquet directly — zero upstream BigQuery scan cost, and `/estimate` reports `estimated_scan_bytes: 0`. The scheduled materialize run is the only thing that touches BigQuery. `--where` predicates may use BigQuery or DuckDB flavor (both are validated and rendered as DuckDB for the local read). Until the first materialize run completes there is no parquet yet, so reads return 404 — they never fall back to scanning the raw upstream table.

### Keboola — `query_mode: local` (the production path)

The Agnes server's Keboola DuckDB extension downloads the table to a parquet on the server filesystem; `agnes pull` distributes it to analyst laptops.

**Setup:** `instance.yaml.data_source.type: keboola` + Storage API token via `KEBOOLA_STORAGE_TOKEN` env var (or whatever `instance.yaml.token_env` points at).

**Register via CLI:**

```bash
agnes admin register-table users \
    --source-type keboola \
    --bucket in.c-crm \
    --source-table users \
    --query-mode local
```

### Keboola — `query_mode: materialized` (full or filtered export)

The server exports the table through the Storage API's `export-async` endpoint and writes a parquet on the Agnes filesystem — same distribution as `local` from there on. Two shapes:

- **Full export** — no `source_query` at all (NULL). The whole table, every scheduled run.
- **Filtered export** — `source_query` holds a **JSON filter spec**, never SQL: `{"where_filters": [{"column": "date", "operator": "ge", "values": ["{{last_3_months}}"]}]}`. Accepted keys are `where_filters`, `columns`, `changed_since`, `changed_until`, `limit` and `file_type`; operators are `eq, ne, gt, ge, lt, le`, and the date placeholders resolve at sync time. A `source_query` starting with `SELECT`/`WITH` is refused at registration and at update time — for DuckDB-SQL pulls use `query_mode: local` (Direct extract) instead.

The export asks for parquet and falls back to CSV automatically on projects whose backend refuses it; either way the columns are retyped from the source schema — see [`docs/DATA_SOURCES.md`](../DATA_SOURCES.md) → *Materialized rows: parquet export, with an automatic CSV fallback*.

**Which UI option yields which mode.** `/admin/tables` offers the same four Keboola access modes in the Register wizard and the Edit modal, and each one names its outcome:

| Option | `query_mode` | What it stores |
|---|---|---|
| Whole table (extension) | `materialized` | full export, no `source_query` |
| Direct extract (Storage API) | `local` | the sync-strategy path (full refresh / incremental / partitioned) with `where_filters` |
| Filtered export (Storage API) | `materialized` | the JSON filter spec above, in `source_query` |
| Live (remote) | `remote` | nothing syncs — see below |

**`query_mode: remote` for Keboola** is supported via the `_remote_attach` mechanism (the orchestrator ATTACHes the Keboola DuckDB extension on demand, the same way it does for BQ) and registrable from both `/admin/tables` modals as **Live (remote)**, but it is **not in wide deployment use today** — expect rougher edges than the BigQuery remote path. `server_only` is rejected with it: a remote row has no server-stored parquet to withhold.

### Jira — `query_mode: local` only

Event-driven: webhooks update parquets incrementally. No `remote` or `materialized` mode for Jira today.

## Worked examples

**1. Big BigQuery fact table you query weekly:** `query_mode: remote`. SA needs `dataViewer` + `jobUser`. Analyst runs `agnes query` for one-off aggregates (the default `--scope auto` routes remote tables server-side automatically; `--remote` is the explicit shorthand) and `agnes snapshot create` for cross-week joins.

**2. Daily Keboola dimension table:** `query_mode: local`. Synced once a day by the scheduler; analyst's `agnes pull` picks it up.

**3. Monthly KPI aggregate from a BQ datawarehouse:** `query_mode: materialized` + `--sync-schedule "0 3 1 * *"` (3:00 on the 1st of each month). The server runs your aggregate SQL once a month; analysts get a parquet of the result.

## See also

- `docs/RBAC.md` — granting analysts access to a registered table.
- `config/instance.yaml.example` — the `data_source` config block.
- `agnes catalog --json` — inspect a registered table's mode + size hints.
- `agnes diagnose` — surface `bq_config` IAM issues and other health entries.
