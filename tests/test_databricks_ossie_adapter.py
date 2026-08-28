"""DatabricksMetricViewAdapter — composes one Ossie document per Unity
Catalog metric view (connectors/databricks/semantic_ossie.py).

Uses the same fake statement client pattern as
tests/test_databricks_semantic_layer_sync.py (routes the two query shapes
the discovery issues: information_schema + SHOW CREATE TABLE per view).
"""

from __future__ import annotations

import yaml

from connectors.databricks.semantic_ossie import (
    DATABRICKS_DIALECT,
    DatabricksMetricViewAdapter,
    compose_document,
    extract_documents,
)
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
    def __init__(self, views=None, yaml_by_view=None, fail_with=None):
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


class TestComposeDocument:
    def test_composes_one_document_valid_against_the_schema(self):
        text, skip_reason = compose_document("main", "sales", "orders_metrics", "Sales KPIs", _YAML)
        assert skip_reason is None
        assert text is not None
        result = validate_document(text)
        assert result.ok, result.errors

    def test_model_name_is_the_fully_qualified_view_name(self):
        text, _ = compose_document("main", "sales", "orders_metrics", "", _YAML)
        parsed = yaml.safe_load(text)
        model = parsed["semantic_model"][0]
        assert model["name"] == "main.sales.orders_metrics"

    def test_measures_become_dialect_tagged_metrics(self):
        text, _ = compose_document("main", "sales", "orders_metrics", "", _YAML)
        parsed = yaml.safe_load(text)
        metrics = {m["name"]: m for m in parsed["semantic_model"][0]["metrics"]}
        assert set(metrics) == {"Order Count", "Total Revenue"}

        revenue = metrics["Total Revenue"]
        dialects = revenue["expression"]["dialects"]
        assert len(dialects) == 1
        assert dialects[0]["dialect"] == DATABRICKS_DIALECT
        assert dialects[0]["expression"] == "SELECT MEASURE(`Total Revenue`) FROM `main`.`sales`.`orders_metrics`"
        assert "Gross revenue before refunds" in revenue["description"]
        assert "SQL warehouse" in revenue["description"]

    def test_dimensions_become_dataset_fields(self):
        text, _ = compose_document("main", "sales", "orders_metrics", "", _YAML)
        parsed = yaml.safe_load(text)
        dataset = parsed["semantic_model"][0]["datasets"][0]
        assert dataset["source"] == "SELECT * FROM main.sales.orders"
        field_names = {f["name"] for f in dataset["fields"]}
        assert field_names == {"order_date", "country"}
        order_date = next(f for f in dataset["fields"] if f["name"] == "order_date")
        assert order_date["expression"]["dialects"][0]["dialect"] == DATABRICKS_DIALECT
        assert order_date["expression"]["dialects"][0]["expression"] == "o_orderdate"

    def test_no_measures_is_skipped(self):
        text, reason = compose_document("main", "sales", "v", "", "version: 1.1\nsource: t\n")
        assert text is None
        assert reason == "no_measures"

    def test_non_mapping_yaml_is_skipped(self):
        text, reason = compose_document("main", "sales", "v", "", "- just\n- a list\n")
        assert text is None
        assert reason == "yaml_not_a_mapping"

    def test_yaml_error_is_skipped(self):
        text, reason = compose_document("main", "sales", "v", "", "measures: [\n")
        assert text is None
        assert reason.startswith("yaml_error")

    def test_measures_with_no_name_are_dropped_not_fatal(self):
        yaml_text = "measures:\n  - expr: COUNT(1)\n  - name: valid\n    expr: COUNT(2)\n"
        text, reason = compose_document("main", "sales", "v", "", yaml_text)
        assert reason is None
        parsed = yaml.safe_load(text)
        names = {m["name"] for m in parsed["semantic_model"][0]["metrics"]}
        assert names == {"valid"}


class TestExtractDocuments:
    def test_returns_one_document_per_view_and_counts_views(self):
        docs, counters = extract_documents(FakeStatementClient(), ["main"])
        assert len(docs) == 1
        assert counters["metric_views_seen"] == 1
        assert counters["skipped_unparseable"] == 0

    def test_unparseable_view_is_counted_not_fatal(self):
        docs, counters = extract_documents(FakeStatementClient(yaml_by_view={"orders_metrics": None}), ["main"])
        assert docs == []
        assert counters["skipped_unparseable"] == 1

    def test_no_views_returns_empty(self):
        docs, counters = extract_documents(FakeStatementClient(views=[]), ["main"])
        assert docs == []
        assert counters["metric_views_seen"] == 0


_SETTINGS = {
    "host": "https://dbc-test.cloud.databricks.com",
    "warehouse_id": "wh-1",
    "catalog": "main",
    "catalogs": ["main"],
    "token": "tok",
}


def _settings(monkeypatch, value=None):
    """Point the adapter's credential resolution at fixed settings.

    The adapter never reads credentials out of its own config (see the class
    docstring), so every extract test has to say what the instance's
    Databricks connection resolves to — including the tests that say it
    resolves to nothing.
    """
    monkeypatch.setattr(
        "connectors.databricks.semantic_layer.resolve_databricks_settings",
        lambda connection=None: value,
    )


class TestAdapterExtract:
    """The adapter's config carries SCOPE only.

    Regression boundary: the connect wizard's "Also sync semantic views"
    opt-in creates this source with an empty config and syncs it immediately,
    so an adapter that demanded `config['host'/'warehouse_id'/'token']` could
    never succeed from that flow — it raised before issuing a statement, and
    the wizard swallowed the failure as non-fatal. Credentials resolve from
    the connection instead, the way the Snowflake adapter next door already
    did.
    """

    def test_an_empty_config_syncs_off_the_instance_connection(self, monkeypatch):
        """The wizard's exact payload: `config: {}`, nothing else."""
        _settings(monkeypatch, _SETTINGS)
        client = FakeStatementClient()
        monkeypatch.setattr(
            "connectors.databricks.client.DatabricksStatementClient",
            lambda **_kwargs: client,
        )

        docs = DatabricksMetricViewAdapter().extract({})

        assert len(docs) == 1
        assert validate_document(docs[0]).ok

    def test_the_resolved_credentials_reach_the_statement_client(self, monkeypatch):
        _settings(monkeypatch, _SETTINGS)
        seen = {}

        def _client(**kwargs):
            seen.update(kwargs)
            return FakeStatementClient()

        monkeypatch.setattr("connectors.databricks.client.DatabricksStatementClient", _client)

        DatabricksMetricViewAdapter().extract({})

        assert seen == {
            "host": _SETTINGS["host"],
            "token": _SETTINGS["token"],
            "warehouse_id": _SETTINGS["warehouse_id"],
        }

    def test_extract_returns_documents_only_discarding_counters(self, monkeypatch):
        _settings(monkeypatch, _SETTINGS)
        adapter = DatabricksMetricViewAdapter()
        docs = adapter.extract({"catalogs": ["main"], "client": FakeStatementClient()})
        assert len(docs) == 1
        assert validate_document(docs[0]).ok

    def test_accepts_a_single_catalog_key(self, monkeypatch):
        _settings(monkeypatch, _SETTINGS)
        adapter = DatabricksMetricViewAdapter()
        docs = adapter.extract({"catalog": "main", "client": FakeStatementClient()})
        assert len(docs) == 1

    def test_a_config_catalog_scope_narrows_the_connection_default(self, monkeypatch):
        """Scope IS the adapter's config; it wins over the connection's own
        catalog list, which is the whole point of having it."""
        _settings(monkeypatch, {**_SETTINGS, "catalogs": ["main", "other"]})
        client = FakeStatementClient()

        DatabricksMetricViewAdapter().extract({"catalogs": ["other"], "client": client})

        assert any("`other`.information_schema" in s for s in client.statements)
        assert not any("`main`.information_schema" in s for s in client.statements)

    def test_credentials_in_the_config_are_ignored_not_honored(self, monkeypatch):
        """A semantic source row must never become a second place a workspace
        token is stored — same rule as the Snowflake adapter."""
        _settings(monkeypatch, _SETTINGS)
        seen = {}

        def _client(**kwargs):
            seen.update(kwargs)
            return FakeStatementClient()

        monkeypatch.setattr("connectors.databricks.client.DatabricksStatementClient", _client)

        DatabricksMetricViewAdapter().extract(
            {"host": "https://evil.example.com", "token": "leaked", "warehouse_id": "wh-other"}
        )

        assert seen["host"] == _SETTINGS["host"]
        assert seen["token"] == _SETTINGS["token"]
        assert seen["warehouse_id"] == _SETTINGS["warehouse_id"]

    def test_unconfigured_databricks_raises(self, monkeypatch):
        import pytest

        _settings(monkeypatch, None)
        with pytest.raises(RuntimeError, match="not configured"):
            DatabricksMetricViewAdapter().extract({})

    def test_missing_catalogs_raises(self, monkeypatch):
        import pytest

        _settings(monkeypatch, {**_SETTINGS, "catalog": "", "catalogs": []})
        with pytest.raises(ValueError, match="catalog"):
            DatabricksMetricViewAdapter().extract({"client": FakeStatementClient()})

    def test_a_pinned_connection_id_is_the_one_resolved(self, monkeypatch):
        seen = {}

        def _resolve_connection(source_type, connection_id):
            seen["args"] = (source_type, connection_id)
            return {"id": connection_id, "source_type": "databricks", "config": {}}

        monkeypatch.setattr("src.connection_resolver.resolve_connection", _resolve_connection)
        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda connection=None: {**_SETTINGS} if connection else None,
        )

        docs = DatabricksMetricViewAdapter().extract({"connection_id": "conn-dbx", "client": FakeStatementClient()})

        assert seen["args"] == ("databricks", "conn-dbx")
        assert len(docs) == 1

    def test_a_pinned_connection_of_another_type_is_refused_by_name(self, monkeypatch):
        """`resolve_connection` looks an explicit id up by id alone, so it will
        happily hand back a Snowflake row. Reading its config would report
        "Databricks is not configured" about a connection that exists."""
        import pytest

        monkeypatch.setattr(
            "src.connection_resolver.resolve_connection",
            lambda *_a, **_k: {"id": "conn-sf", "source_type": "snowflake", "config": {}},
        )
        _settings(monkeypatch, _SETTINGS)

        with pytest.raises(RuntimeError, match="not a Databricks one"):
            DatabricksMetricViewAdapter().extract({"connection_id": "conn-sf"})

    def test_a_pinned_connection_that_is_gone_raises_instead_of_falling_back(self, monkeypatch):
        """Falling through to the default connection would sync a DIFFERENT
        workspace's metric views under this source's provenance, silently."""
        import pytest

        monkeypatch.setattr("src.connection_resolver.resolve_connection", lambda *_a, **_k: None)
        _settings(monkeypatch, _SETTINGS)

        with pytest.raises(RuntimeError, match="no longer exists"):
            DatabricksMetricViewAdapter().extract({"connection_id": "conn-gone"})
