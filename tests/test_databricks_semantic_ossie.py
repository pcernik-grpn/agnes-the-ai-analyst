"""Databricks Unity Catalog metric views -> Ossie adapter.

The composer (`compose_document`) is a pure function over one metric view's
parsed YAML, so most mapping assertions run without a Databricks workspace.
The adapter's fetch half is covered with a fake statement client — the same
shape `tests/test_databricks_wire_e2e.py` exercises for real, over the wire.
"""

from __future__ import annotations

import json

import pytest

from connectors.databricks.client import DatabricksApiError
from connectors.databricks.semantic_ossie import (
    DatabricksMetricViewAdapter,
    _list_metric_views,
    compose_document,
    extract_yaml_from_create,
)
from src.semantic.dialect import resolve_expression
from src.semantic.document_validation import validate_document

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


class FakeStatementClient:
    """Routes the two query shapes the adapter issues: metric-view discovery
    (information_schema) and SHOW CREATE TABLE per view."""

    def __init__(self, views=None, yaml_by_view=None, fail_with=None, fail_views=None):
        # views: list of (catalog, schema, name, comment)
        self.views = views if views is not None else [("main", "sales", "orders_metrics", "Sales KPIs")]
        self.yaml_by_view = yaml_by_view or {}
        self.fail_with = fail_with
        # Views whose SHOW CREATE TABLE (only) raises fail_with — discovery
        # still succeeds, unlike fail_with alone (which fails every call).
        self.fail_views = fail_views or set()
        self.statements = []

    def execute_rows(self, statement, **_kwargs):
        self.statements.append(statement)
        if "information_schema" in statement:
            if self.fail_with is not None and not self.fail_views:
                raise self.fail_with
            return (
                ["table_catalog", "table_schema", "table_name", "comment"],
                [list(v) for v in self.views],
            )
        if statement.startswith("SHOW CREATE TABLE"):
            view_name = statement.rsplit(".", 1)[-1].strip("`")
            if view_name in self.fail_views:
                raise self.fail_with
            body = self.yaml_by_view.get(view_name, _YAML)
            if body is None:
                return (["createtab_stmt"], [["CREATE VIEW broken AS SELECT 1"]])
            return (
                ["createtab_stmt"],
                [[f"CREATE VIEW x WITH METRICS\nLANGUAGE YAML\nAS $$\n{body}\n$$"]],
            )
        raise AssertionError(f"unexpected statement: {statement}")


def _model(text):
    result = validate_document(text)
    assert result.ok, result.errors
    return result.parsed["semantic_model"][0]


# ---------------------------------------------------------------------------
# compose_document — pure
# ---------------------------------------------------------------------------


def test_composed_document_is_schema_valid():
    text = compose_document("main", "sales", "orders_metrics", "Sales KPIs", _YAML)
    assert validate_document(text).ok


def test_model_name_is_fully_qualified_so_same_named_views_do_not_collide():
    a = _model(compose_document("main", "sales", "orders_metrics", "", _YAML))
    b = _model(compose_document("main", "finance", "orders_metrics", "", _YAML))
    assert a["name"] == "main.sales.orders_metrics"
    assert b["name"] == "main.finance.orders_metrics"


def test_measures_become_metrics_carrying_the_composed_measure_statement():
    metrics = {m["name"]: m for m in _model(compose_document("main", "sales", "orders_metrics", "", _YAML))["metrics"]}
    assert set(metrics) == {"Order Count", "Total Revenue"}
    dialects = metrics["Total Revenue"]["expression"]["dialects"]
    # MEASURE() only evaluates against its own metric view — the dialect
    # expression is the full runnable statement, not the bare `expr`
    # fragment, which by itself is not something an agent could run anywhere.
    assert dialects == [
        {
            "dialect": "DATABRICKS",
            "expression": "SELECT MEASURE(`Total Revenue`) FROM `main`.`sales`.`orders_metrics`",
        }
    ]
    assert metrics["Total Revenue"]["description"] == "Gross revenue before refunds"


def test_measure_description_falls_back_to_view_comment():
    metrics = {
        m["name"]: m
        for m in _model(compose_document("main", "sales", "orders_metrics", "Sales KPIs", _YAML))["metrics"]
    }
    assert metrics["Order Count"]["description"] == "Sales KPIs"


def test_raw_measure_expr_is_carried_in_custom_extensions():
    metrics = {m["name"]: m for m in _model(compose_document("main", "sales", "orders_metrics", "", _YAML))["metrics"]}
    payload = json.loads(metrics["Total Revenue"]["custom_extensions"][0]["data"])
    assert payload["measure_expr"] == "SUM(o_totalprice)"


def test_metrics_are_refused_for_local_execution():
    metrics = {m["name"]: m for m in _model(compose_document("main", "sales", "orders_metrics", "", _YAML))["metrics"]}
    sql, reason = resolve_expression(metrics["Total Revenue"]["expression"])
    assert sql is None and "DATABRICKS" in reason


def test_dimensions_become_fields_on_the_single_dataset():
    datasets = _model(compose_document("main", "sales", "orders_metrics", "", _YAML))["datasets"]
    assert len(datasets) == 1
    dataset = datasets[0]
    assert dataset["name"] == "orders_metrics"
    assert dataset["source"] == "SELECT * FROM main.sales.orders"
    fields = {f["name"]: f for f in dataset["fields"]}
    assert set(fields) == {"order_date", "country"}
    assert fields["order_date"]["expression"]["dialects"] == [{"dialect": "DATABRICKS", "expression": "o_orderdate"}]


def test_dataset_source_falls_back_to_the_fqn_when_yaml_declares_none():
    yaml_text = "version: 1.1\nmeasures:\n  - name: n\n    expr: COUNT(1)\n"
    dataset = _model(compose_document("main", "sales", "v", "", yaml_text))["datasets"][0]
    assert dataset["source"] == "main.sales.v"


def test_view_without_measures_is_skipped_not_emitted_invalid():
    yaml_text = "version: 1.1\nsource: t\ndimensions:\n  - name: d\n    expr: d\n"
    assert compose_document("main", "sales", "v", "", yaml_text) is None


def test_non_mapping_yaml_is_skipped():
    assert compose_document("main", "sales", "v", "", "- just\n- a list\n") is None


def test_invalid_yaml_is_skipped():
    assert compose_document("main", "sales", "v", "", "measures: [") is None


def test_measure_names_with_backticks_are_escaped_not_broken():
    yaml_text = "measures:\n  - name: 'bad`tick'\n    expr: COUNT(1)\n"
    model = _model(compose_document("c", "s", "v", "", yaml_text))
    metric = model["metrics"][0]
    expr = metric["expression"]["dialects"][0]["expression"]
    assert expr == "SELECT MEASURE(`bad``tick`) FROM `c`.`s`.`v`"


def test_view_comment_becomes_model_description():
    model = _model(compose_document("main", "sales", "orders_metrics", "Sales KPIs", _YAML))
    assert model["description"] == "Sales KPIs"
    payload = json.loads(model["custom_extensions"][0]["data"])
    assert payload["metric_view"] == "main.sales.orders_metrics"


# ---------------------------------------------------------------------------
# extract_yaml_from_create — pure
# ---------------------------------------------------------------------------


def test_extract_yaml_from_create_pulls_the_dollar_delimited_body():
    stmt = "CREATE VIEW x WITH METRICS\nLANGUAGE YAML\nAS $$\nmeasures: []\n$$"
    assert extract_yaml_from_create(stmt) == "measures: []"


def test_extract_yaml_from_create_returns_none_without_delimiters():
    assert extract_yaml_from_create("CREATE VIEW x AS SELECT 1") is None


def test_extract_yaml_from_create_returns_none_for_empty_input():
    assert extract_yaml_from_create("") is None


# ---------------------------------------------------------------------------
# DatabricksMetricViewAdapter — fetch half
# ---------------------------------------------------------------------------


_SETTINGS = {
    "host": "https://dbc-test.cloud.databricks.com",
    "warehouse_id": "wh-1",
    "catalog": "main",
    "catalogs": ["main"],
    "token": "tok",
}


def _extract(config=None, *, settings=_SETTINGS, client=None):
    adapter = DatabricksMetricViewAdapter()
    if client is not None:
        adapter._client = lambda settings: client  # type: ignore[method-assign]
    import unittest.mock as mock

    with mock.patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=settings):
        return adapter.extract(config or {})


class TestAdapterExtract:
    def test_creates_one_document_per_metric_view(self):
        documents = _extract(client=FakeStatementClient())
        assert len(documents) == 1
        model = _model(documents[0])
        assert model["name"] == "main.sales.orders_metrics"

    def test_config_catalogs_overrides_the_connection_default(self):
        client = FakeStatementClient()
        _extract({"catalogs": "other"}, client=client)
        assert any("`other`.information_schema.tables" in s for s in client.statements)

    def test_zero_views_returns_empty_list(self):
        assert _extract(client=FakeStatementClient(views=[])) == []

    def test_unparseable_view_is_skipped_not_fatal(self):
        assert _extract(client=FakeStatementClient(yaml_by_view={"orders_metrics": None})) == []

    def test_per_view_show_create_failure_is_skipped_not_fatal(self):
        client = FakeStatementClient(
            views=[
                ("main", "sales", "orders_metrics", ""),
                ("main", "sales", "broken_metrics", ""),
            ],
            fail_with=DatabricksApiError("nope", status=403),
            fail_views={"broken_metrics"},
        )
        documents = _extract(client=client)
        # The one view whose SHOW CREATE TABLE failed is skipped; the other
        # still composes — one bad view never sinks the whole sync.
        assert len(documents) == 1

    def test_discovery_failure_is_fatal(self):
        client = FakeStatementClient(fail_with=DatabricksApiError("nope", status=403))
        with pytest.raises(DatabricksApiError):
            _extract(client=client)

    def test_raises_when_not_configured(self):
        with pytest.raises(RuntimeError, match="not configured"):
            _extract(settings=None, client=FakeStatementClient())

    def test_raises_when_no_catalog_configured(self):
        with pytest.raises(RuntimeError, match="catalog"):
            _extract(settings={**_SETTINGS, "catalogs": []}, client=FakeStatementClient())

    def test_adapter_is_registered_under_databricks_metric_views(self):
        from src.semantic.adapters import get_adapter

        assert isinstance(get_adapter("databricks_metric_views"), DatabricksMetricViewAdapter)


def test_list_metric_views_reads_the_fake_client():
    rows = _list_metric_views(FakeStatementClient(), "main")
    assert rows == [("main", "sales", "orders_metrics", "Sales KPIs")]
