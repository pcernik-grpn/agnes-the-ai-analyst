"""Golden regression for the Databricks semantic-layer cutover (semantic-layer
Phase 1 — "STRUKTURA DO OSSIE").

Mirrors `tests/test_semantic_layer_cutover_parity.py` (the Keboola cutover's
own golden regression): the retired writer
(`connectors/databricks/semantic_layer.py::build_metric_rows`, deleted by
this cutover) composed exactly `SELECT MEASURE(<name>) FROM <fqn>` per
measure, under `source='databricks_semantic_layer'`, with no dialect concept
at all. This file pins that the new Ossie/projection path:

  - composes the BYTE-IDENTICAL SQL text for the same input (the one fact a
    cutover must never silently change — it is what a human or an agent would
    actually run on the warehouse);
  - tags it with a dialect the query validator recognizes as not locally
    executable (the new, deliberate behavior the legacy path never had —
    every row it wrote looked exactly as "runnable" as a Keboola metric,
    which was never true for MEASURE());
  - purges any surviving `source='databricks_semantic_layer'` row once the
    new path has stored real output, so the two writers never coexist for
    the same workspace.

Values are pinned as ABSOLUTE expected constants (not derived from either
composer), so this survives as a regression after `build_metric_rows` is
long gone — same discipline as the Keboola golden test.
"""

from __future__ import annotations

from unittest.mock import patch

from src.semantic_validation import validate_query

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
measures:
  - name: Total Revenue
    expr: SUM(o_totalprice)
    description: Gross revenue before refunds
"""

# The retired flat composer's exact output shape for this fixture — pinned,
# not recomputed. Verified against `build_metric_rows` before its deletion:
# `sql = f"SELECT MEASURE({_quote_dbx_ident(name)}) FROM {quoted_fqn}"`.
_EXPECTED_SQL = "SELECT MEASURE(`Total Revenue`) FROM `main`.`sales`.`orders_metrics`"


class _FakeStatementClient:
    def __init__(self):
        self.views = [("main", "sales", "orders_metrics", "Sales KPIs")]

    def execute_rows(self, statement, **_kwargs):
        if "information_schema" in statement:
            return (
                ["table_catalog", "table_schema", "table_name", "comment"],
                [list(v) for v in self.views],
            )
        if statement.startswith("SHOW CREATE TABLE"):
            return (["createtab_stmt"], [[f"CREATE VIEW x WITH METRICS\nLANGUAGE YAML\nAS $$\n{_YAML}\n$$"]])
        raise AssertionError(f"unexpected statement: {statement}")


def _sync():
    from connectors.databricks.semantic_layer import sync_semantic_layer

    with patch(
        "connectors.databricks.semantic_layer.resolve_databricks_settings",
        return_value=_SETTINGS,
    ):
        return sync_semantic_layer(client=_FakeStatementClient())


class TestDatabricksCutoverParity:
    def test_composed_sql_is_byte_identical_to_the_retired_flat_composer(self, e2e_env):
        from src.repositories import semantic_model_repo

        result = _sync()
        assert result["status"] == "ok"

        row = semantic_model_repo().get("databricks_metrics/dbc-test.cloud.databricks.com/main.sales.orders_metrics")
        model = row["document_json"]["semantic_model"][0]
        metric = next(m for m in model["metrics"] if m["name"] == "Total Revenue")
        dialects = metric["expression"]["dialects"]
        assert len(dialects) == 1
        assert dialects[0]["expression"] == _EXPECTED_SQL

    def test_validate_semantic_query_reports_not_locally_executable(self, e2e_env):
        """The one behavior the legacy path never had: item 7 of the cutover
        — query validation now correctly flags a query using this metric as
        not runnable on the local DuckDB engine."""
        from src.repositories import semantic_model_repo

        _sync()
        row = semantic_model_repo().get("databricks_metrics/dbc-test.cloud.databricks.com/main.sales.orders_metrics")
        document = row["document_json"]["semantic_model"][0]

        result = validate_query(_EXPECTED_SQL, [document], target_engine="duckdb")
        assert "Total Revenue" in result["used_metrics"]
        assert result["locally_executable"] is False
        assert "DATABRICKS" in result["sql_dialects"]

    def test_legacy_flat_rows_do_not_survive_a_cutover_sync(self, e2e_env):
        from src.repositories import metric_repo

        metric_repo().create(
            id="databricks/main.sales.orders_metrics/Total Revenue",
            name="Total Revenue",
            display_name="Total Revenue",
            category="databricks",
            sql=_EXPECTED_SQL,
            source="databricks_semantic_layer",
            source_ref="dbc-test.cloud.databricks.com",
        )
        _sync()
        assert metric_repo().get("databricks/main.sales.orders_metrics/Total Revenue") is None
        # And the new writer never recreates a metric_definitions row under
        # any id for this measure — see sync_semantic_layer's own docstring.
        assert metric_repo().find_by_name("Total Revenue") is None
