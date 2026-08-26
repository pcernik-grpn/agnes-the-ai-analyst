"""sync_semantic_layer() for Databricks — Ossie document path, post-cutover.

Since the semantic-phase-1 cutover, `sync_semantic_layer` is a pure document
pipeline: it composes one Ossie document per Unity Catalog metric view
(`connectors.databricks.semantic_ossie`), stores each under
`source='databricks_metrics'` in `semantic_models`, and runs them through
`src.semantic.projection.project_document`.

Every measure is composed with ONLY the `DATABRICKS` Ossie dialect (never
`DUCKDB`/`ANSI_SQL` — `MEASURE()` isn't valid DuckDB syntax at all), which is
the same choice `connectors/snowflake/semantic_ossie.py` already made for its
own warehouse-only metrics: `src.semantic.dialect.resolve_expression` skips
composing a `metric_definitions` row for every one of them, exactly as it
does for Snowflake. So this sync's `created_or_updated`/`pruned` counters
describe DOCUMENT-level work (metric views synced into `semantic_models`),
not `metric_definitions` rows — see `sync_semantic_layer`'s own docstring.
`tests/test_databricks_ossie_adapter.py` covers document composition itself;
this file exercises the sync entrypoint's contract: counters, error codes,
prune scoping, legacy-row retirement.

Fake statement client, real test DuckDB via the e2e_env fixture.
"""

from __future__ import annotations

from unittest.mock import patch


from connectors.databricks.client import DatabricksApiError

_SETTINGS = {
    "host": "https://dbc-test.cloud.databricks.com",
    "warehouse_id": "wh-1",
    "catalog": "main",
    "catalogs": ["main"],
    "token": "tok",
}

_YAML = """
version: 1.1
source: SELECT * FROM main.sales.orders
dimensions:
  - name: order_date
    expr: o_orderdate
  - name: country
    expr: c_country
measures:
  - name: Order Count
    expr: COUNT(o_orderkey)
  - name: Total Revenue
    expr: SUM(o_totalprice)
    description: Gross revenue before refunds
"""

_DOC_ID = "databricks_metrics/dbc-test.cloud.databricks.com/main.sales.orders_metrics"

# A second metric view over the SAME underlying table (`source:` matches
# `_YAML`'s) but declaring a dimension `_YAML` does not — `region` — used to
# exercise `column_metadata` pruning when two metric views share a table_id.
_YAML_V2 = """
version: 1.1
source: SELECT * FROM main.sales.orders
dimensions:
  - name: order_date
    expr: o_orderdate
  - name: region
    expr: c_region
measures:
  - name: Order Count
    expr: COUNT(o_orderkey)
"""
_DOC_ID_V2 = "databricks_metrics/dbc-test.cloud.databricks.com/main.sales.orders_metrics_v2"


class FakeStatementClient:
    """Routes the two query shapes the sync issues: metric-view discovery
    (information_schema) and SHOW CREATE TABLE per view."""

    def __init__(self, views=None, yaml_by_view=None, fail_with=None):
        # views: list of (catalog, schema, name, comment)
        self.views = views if views is not None else [("main", "sales", "orders_metrics", "Sales KPIs")]
        self.yaml_by_view = yaml_by_view or {}
        self.fail_with = fail_with
        self.statements = []

    def execute_rows(self, statement, **_kwargs):
        self.statements.append(statement)
        if self.fail_with is not None:
            raise self.fail_with
        if "information_schema" in statement:
            return (
                ["table_catalog", "table_schema", "table_name", "comment"],
                [list(v) for v in self.views],
            )
        if statement.startswith("SHOW CREATE TABLE"):
            view_name = statement.rsplit(".", 1)[-1].strip("`")
            body = self.yaml_by_view.get(view_name, _YAML)
            if body is None:
                return (["createtab_stmt"], [["CREATE VIEW broken AS SELECT 1"]])
            return (
                ["createtab_stmt"],
                [[f"CREATE VIEW x WITH METRICS\nLANGUAGE YAML\nAS $$\n{body}\n$$"]],
            )
        raise AssertionError(f"unexpected statement: {statement}")


def _sync(client):
    from connectors.databricks.semantic_layer import sync_semantic_layer

    with patch(
        "connectors.databricks.semantic_layer.resolve_databricks_settings",
        return_value=_SETTINGS,
    ):
        return sync_semantic_layer(client=client)


class TestSyncSemanticLayer:
    def test_stores_one_document_per_metric_view(self, e2e_env):
        from src.repositories import semantic_model_repo

        result = _sync(FakeStatementClient())
        assert result["status"] == "ok"
        assert result["metric_views_seen"] == 1
        assert result["created_or_updated"] == 1
        assert result["source_ref"] == "dbc-test.cloud.databricks.com"

        row = semantic_model_repo().get(_DOC_ID)
        assert row is not None
        assert row["source"] == "databricks_metrics"
        assert row["source_ref"] == "dbc-test.cloud.databricks.com"
        assert row["status"] == "valid"
        model = row["document_json"]["semantic_model"][0]
        assert model["name"] == "main.sales.orders_metrics"
        metric_names = {m["name"] for m in model["metrics"]}
        assert metric_names == {"Order Count", "Total Revenue"}

    def test_measures_never_reach_metric_definitions(self, e2e_env):
        """The MEASURE()-tagged, DATABRICKS-only expression is not
        DuckDB-runnable — `resolve_expression` skips composing a
        `metric_definitions` row for it, same as it does for every Snowflake
        semantic-view metric. Discoverable via the document, not the flat
        metrics listing (`agnes catalog --metrics`)."""
        from src.repositories import metric_repo

        _sync(FakeStatementClient())
        assert metric_repo().find_by_name("Total Revenue") is None
        assert metric_repo().find_by_name("Order Count") is None

    def test_unchanged_document_is_not_recounted(self, e2e_env):
        _sync(FakeStatementClient())
        result = _sync(FakeStatementClient())
        assert result["created_or_updated"] == 0
        assert result["pruned"] == 0

    def test_prunes_documents_actually_removed_upstream(self, e2e_env):
        """When the upstream fetch itself is non-empty, a view genuinely
        dropped from one pass to the next is still pruned — only a
        `documents == []` pass is guarded (see
        test_zero_fetch_does_not_wipe_existing_documents)."""
        from src.repositories import semantic_model_repo

        two_views = [
            ("main", "sales", "orders_metrics", "Sales KPIs"),
            ("main", "sales", "customers_metrics", "Customer KPIs"),
        ]
        other_doc_id = "databricks_metrics/dbc-test.cloud.databricks.com/main.sales.customers_metrics"
        _sync(FakeStatementClient(views=two_views))
        assert semantic_model_repo().get(_DOC_ID) is not None
        assert semantic_model_repo().get(other_doc_id) is not None

        result = _sync(FakeStatementClient(views=[two_views[0]]))
        assert result["pruned"] == 1
        assert semantic_model_repo().get(_DOC_ID) is not None
        assert semantic_model_repo().get(other_doc_id) is None

    def test_zero_fetch_does_not_wipe_existing_documents(self, e2e_env):
        """A successful-but-empty metric-view fetch (`documents == []` —
        either zero views found, or every view's `SHOW CREATE TABLE` call
        failing transiently, see
        connectors/databricks/semantic_ossie.py::extract_documents) must NOT
        prune every previously-stored `semantic_models` document for this
        workspace — mirrors Keboola's own `if not models` guard
        (connectors/keboola/semantic_layer.py::_sync_one_source,
        tests/test_keboola_semantic_layer_sync.py
        ::test_empty_metrics_does_not_wipe_existing_rows)."""
        from src.repositories import semantic_model_repo

        _sync(FakeStatementClient())
        assert semantic_model_repo().get(_DOC_ID) is not None

        result = _sync(FakeStatementClient(views=[]))
        assert result["status"] == "ok"
        assert result["pruned"] == 0
        assert semantic_model_repo().get(_DOC_ID) is not None

    def test_partial_composition_does_not_prune_the_dropped_views_column_metadata(self, e2e_env):
        """A single composed document failing `validate_document` is
        logged-and-dropped before reaching `project_document` — so the
        merged model list handed to it that pass is a PARTIAL view of what
        `extract_documents` actually composed. Two metric views sharing an
        underlying table (`dataset.source`, `column_metadata`'s `table_id`)
        make this observable: dropping one must not prune the OTHER's
        already-projected columns, which upstream never asked to have
        removed. Mirrors
        tests/test_keboola_semantic_layer_sync.py
        ::test_partial_composition_does_not_prune_the_dropped_models_rows."""
        from src.repositories import column_metadata_repo, semantic_model_repo
        from src.semantic import document_validation

        table_id = "SELECT * FROM main.sales.orders"
        two_views = [
            ("main", "sales", "orders_metrics", "Sales KPIs"),
            ("main", "sales", "orders_metrics_v2", "Sales KPIs v2"),
        ]
        client = FakeStatementClient(
            views=two_views,
            yaml_by_view={"orders_metrics": _YAML, "orders_metrics_v2": _YAML_V2},
        )

        _sync(client)
        assert semantic_model_repo().get(_DOC_ID) is not None
        assert semantic_model_repo().get(_DOC_ID_V2) is not None
        # `region` is declared only by the v2 view — its presence confirms
        # both views' fields were projected onto the shared table_id.
        assert column_metadata_repo().get(table_id, "country") is not None
        assert column_metadata_repo().get(table_id, "region") is not None

        real_validate = document_validation.validate_document

        def _fail_v2(text):
            if "name: main.sales.orders_metrics_v2" in text:
                return document_validation.ValidationResult(ok=False, errors=["forced failure for test"])
            return real_validate(text)

        with patch("src.semantic.document_validation.validate_document", side_effect=_fail_v2):
            result = _sync(FakeStatementClient(views=two_views, yaml_by_view={"orders_metrics": _YAML, "orders_metrics_v2": _YAML_V2}))

        assert result["status"] == "ok"
        # "orders_metrics" still composes and projects fine.
        assert column_metadata_repo().get(table_id, "country") is not None
        # v2's document failed validation and was dropped — its
        # PREVIOUSLY-WRITTEN column rows must survive this pass, not be
        # pruned as if upstream had genuinely removed the field.
        assert column_metadata_repo().get(table_id, "region") is not None

    def test_extract_stage_drop_does_not_prune_the_dropped_views_rows(self, e2e_env):
        """Unlike the validate_document-stage drop above, a view can also be
        dropped earlier, inside `extract_documents` itself — a persistent
        `SHOW CREATE TABLE` failure or unparseable YAML — before it ever
        reaches `documents`. That never shows up as a gap between
        `parsed_documents` and `documents` (both already exclude it), so the
        `len(parsed_documents) < len(documents)` check alone can't see it;
        `discovery_counters['skipped_unparseable']` is the only signal. Both
        the previously-stored document AND its column_metadata rows must
        survive this pass — a transient per-view failure, not a real
        upstream removal."""
        from src.repositories import column_metadata_repo, semantic_model_repo

        table_id = "SELECT * FROM main.sales.orders"
        two_views = [
            ("main", "sales", "orders_metrics", "Sales KPIs"),
            ("main", "sales", "orders_metrics_v2", "Sales KPIs v2"),
        ]
        client = FakeStatementClient(
            views=two_views,
            yaml_by_view={"orders_metrics": _YAML, "orders_metrics_v2": _YAML_V2},
        )
        _sync(client)
        assert semantic_model_repo().get(_DOC_ID) is not None
        assert semantic_model_repo().get(_DOC_ID_V2) is not None
        assert column_metadata_repo().get(table_id, "country") is not None
        assert column_metadata_repo().get(table_id, "region") is not None

        # Re-sync: orders_metrics_v2's SHOW CREATE TABLE now yields no
        # parseable YAML body (persistent permission issue, say) —
        # extract_documents drops it before `documents` is even returned;
        # orders_metrics itself still discovers/composes/validates fine.
        result = _sync(
            FakeStatementClient(views=two_views, yaml_by_view={"orders_metrics": _YAML, "orders_metrics_v2": None})
        )

        assert result["status"] == "ok"
        assert result["skipped_unparseable"] == 1
        # orders_metrics is unaffected.
        assert semantic_model_repo().get(_DOC_ID) is not None
        assert column_metadata_repo().get(table_id, "country") is not None
        # orders_metrics_v2's OWN previously-stored rows must survive a
        # transient per-view failure, not be deleted as if upstream had
        # genuinely removed the view.
        assert semantic_model_repo().get(_DOC_ID_V2) is not None
        assert column_metadata_repo().get(table_id, "region") is not None

    def test_unparseable_view_is_counted_not_fatal(self, e2e_env):
        result = _sync(FakeStatementClient(yaml_by_view={"orders_metrics": None}))
        assert result["status"] == "ok"
        assert result["skipped_unparseable"] == 1
        assert result["created_or_updated"] == 0

    def test_unconfigured_instance_reports_error_code(self, e2e_env):
        from connectors.databricks.semantic_layer import sync_semantic_layer

        with patch(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            return_value=None,
        ):
            result = sync_semantic_layer()
        assert result["status"] == "error"
        assert result["code"] == "credentials_not_configured"

    def test_upstream_4xx_maps_to_client_error_code(self, e2e_env):
        result = _sync(FakeStatementClient(fail_with=DatabricksApiError("denied", status=403)))
        assert result["status"] == "error"
        assert result["code"] == "upstream_client_error"

    def test_upstream_5xx_maps_to_upstream_error_code(self, e2e_env):
        result = _sync(FakeStatementClient(fail_with=DatabricksApiError("boom", status=503)))
        assert result["status"] == "error"
        assert result["code"] == "upstream_error"


class TestLegacySourceRetirement:
    """One-time purge of rows still stamped with the retired
    `source='databricks_semantic_layer'` label, scoped to this workspace —
    mirrors `connectors/keboola/semantic_layer.py::_sync_one_source`'s own
    legacy-retirement guard."""

    def test_legacy_rows_are_purged_once_the_document_is_stored(self, e2e_env):
        from src.repositories import metric_repo

        metric_repo().create(
            id="databricks/main.sales.orders_metrics/Total Revenue",
            name="Total Revenue",
            display_name="Total Revenue",
            category="databricks",
            sql="SELECT MEASURE(`Total Revenue`) FROM `main`.`sales`.`orders_metrics`",
            source="databricks_semantic_layer",
            source_ref="dbc-test.cloud.databricks.com",
        )
        result = _sync(FakeStatementClient())
        assert result["status"] == "ok"
        assert metric_repo().get("databricks/main.sales.orders_metrics/Total Revenue") is None
        assert result["pruned"] >= 1

    def test_legacy_row_of_a_different_workspace_is_untouched(self, e2e_env):
        from src.repositories import metric_repo

        metric_repo().create(
            id="databricks/other.workspace/x",
            name="Other Metric",
            display_name="Other Metric",
            category="databricks",
            sql="SELECT MEASURE(`x`) FROM `other`",
            source="databricks_semantic_layer",
            source_ref="some-other-workspace.cloud.databricks.com",
        )
        _sync(FakeStatementClient())
        assert metric_repo().get("databricks/other.workspace/x") is not None

    def test_legacy_rows_are_not_purged_on_a_zero_write_pass(self, e2e_env):
        """An empty/failed upstream fetch (0 documents stored) must never
        delete the last good copy of a legacy row."""
        from src.repositories import metric_repo

        metric_repo().create(
            id="databricks/main.sales.orders_metrics/Total Revenue",
            name="Total Revenue",
            display_name="Total Revenue",
            category="databricks",
            sql="SELECT MEASURE(`Total Revenue`) FROM `main`.`sales`.`orders_metrics`",
            source="databricks_semantic_layer",
            source_ref="dbc-test.cloud.databricks.com",
        )
        result = _sync(FakeStatementClient(views=[]))
        assert result["status"] == "ok"
        assert metric_repo().get("databricks/main.sales.orders_metrics/Total Revenue") is not None
