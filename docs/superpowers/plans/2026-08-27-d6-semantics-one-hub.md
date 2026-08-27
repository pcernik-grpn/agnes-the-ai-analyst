# D6 — Semantics on one hub

Source: `docs/superpowers/plans/2026-08-24-agnes-remediation-program.md`
Track D, item D6. Three independent-ish parts sharing one goal: every
semantic-layer metric — however it was imported — ends up in
`metric_definitions`, and there is exactly one writer of that table.

## The three parts

**A — dialect projection fix (this change).** A metric whose expression
offers only a warehouse-specific dialect (`SNOWFLAKE`, later `DATABRICKS`)
is silently dropped at `src/semantic/projection.py::project_document` — it
never reaches `metric_repo().create`, so it never appears in
`metric_definitions` or the metrics catalog, even though the document itself
(`semantic_models`) stores it fine. Fixed by adding a fallback resolution
path in `src/semantic/dialect.py` and using it in `projection.py` to project
the metric anyway: the raw warehouse expression rides as the metric's `sql`,
and a `notes` entry records which dialect it is and that it needs server-side
execution (remote/materialized), not a local DuckDB run.

**B — Databricks adapter port (depends on A).** `connectors/databricks/
semantic_layer.py::build_metric_rows` is a second, direct writer of
`metric_definitions` — it composes rows by hand from Unity Catalog metric-view
YAML instead of going through the Ossie document → `project_document` path
every other source uses. Porting it onto the same semantic-source adapter
contract (`extract(config) -> list[str]`, like `connectors/snowflake/
semantic_ossie.py`) means its metrics start arriving as Ossie documents
tagging expressions `DATABRICKS` — and without Part A, `resolve_expression`
would drop every one of them exactly as it dropped Snowflake's before this
fix. A must land first; B is a follow-up task, not implemented here.

**C — delete the orphaned OpenMetadata export (independent).**
`src/catalog_export.py` predates the semantic layer and has no live callers
in the sync/admin flow it originally served; deleting it (plus its advertised
`config/instance.yaml` block) is unrelated to A/B and can happen on its own
schedule. Not implemented here.

## Design decision: a notes marker, not a schema column

`metric_definitions` has no `query_mode`/`remote`/"locally runnable" column,
and `metric_repo().create` silently swallows unrecognized kwargs — so a new
column would need a migration-ladder bump (DuckDB `_vN_to_v(N+1)` +
Postgres Alembic revision) for what is, functionally, a one-line human-
readable annotation. `connectors/databricks/semantic_layer.py::build_metric_rows`
already established the precedent for exactly this case: it stashes "run this
server-side" guidance in the metric's `notes` list rather than a dedicated
column. Part A follows the same precedent — `notes` gets one entry naming the
warehouse dialect and pointing at remote/materialized execution — so a reader
of `agnes catalog --metrics --show <id>` sees the caveat without a schema
change, and Part B (Databricks-via-Ossie) can reuse the identical mechanism
with zero new plumbing.

## The B3 "N metrics skipped" badge, reconciled

`app/web/semantic_layer_view.py::dialect_skipped_count` (wired to the
`/semantic-layer/<slug>` model-detail page, added under remediation-program
Track B3) originally recomputed independently via `resolve_expression` and
reported a warehouse-only metric as "skipped (unsupported dialect)". Once
Part A made that metric project, the wording became false — the metric no
longer vanishes, it just isn't locally runnable — so Part A folds the fix in:
`count_dialect_skipped_metrics` → `count_warehouse_only_metrics` (now built
on `resolve_expression_any`, the same resolution path the projector itself
uses, rather than pattern-matching `resolve_expression`'s reason string);
the view helper `dialect_skipped_count` → `warehouse_only_metric_count`; the
badge text → "N metric(s) run server-side only (warehouse dialect)".
