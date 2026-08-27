"""End-to-end: a Databricks `connection`-kind semantic source syncs through
the standard pipeline — the same one Snowflake/Keboola/git/upload sources
use — landing a stored Ossie document in ``semantic_models`` and a
warehouse-only metric in ``metric_definitions``, stamped
``source='ossie_connection'``.

This is Track D6's whole point: Databricks metric views no longer have a
private write path. See ``connectors/databricks/semantic_ossie.py`` for the
adapter and ``connectors/databricks/semantic_layer.py`` for connection
resolution + the legacy-provenance cutover.
"""

from __future__ import annotations

from unittest.mock import patch

from connectors.databricks.semantic_ossie import DatabricksSemanticAdapter
from tests.test_databricks_semantic_ossie import _SETTINGS, FakeStatementClient


def _register_source(source_id: str = "ss_dbx_test") -> str:
    from src.repositories import semantic_source_repo

    semantic_source_repo().create(
        id=source_id,
        kind="connection",
        name="Databricks metric views",
        adapter="databricks_semantic",
        config={},
    )
    return source_id


def test_databricks_source_syncs_into_semantic_models_and_metric_definitions(e2e_env):
    from src.repositories import metric_repo, semantic_model_repo
    from src.semantic.transports import import_source

    source_id = _register_source()

    with (
        patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=_SETTINGS),
        patch.object(DatabricksSemanticAdapter, "_client", lambda self, settings: FakeStatementClient()),
    ):
        report = import_source(source_id)

    # 1. The document itself is stored whole.
    model = semantic_model_repo().get_by_slug("main.sales.orders_metrics")
    assert model is not None
    assert model["source"] == "ossie_connection"
    assert model["source_ref"] == source_id
    assert model["status"] == "valid"

    # 2. It projects into metric_definitions, warehouse-only.
    assert report.projection is not None
    assert report.projection.metrics_written == 2
    assert {w["name"] for w in report.projection.warehouse_only} == {"Order Count", "Total Revenue"}
    for w in report.projection.warehouse_only:
        assert w["dialect"] == "DATABRICKS"

    row = metric_repo().get(f"ossie_connection/{source_id}/main.sales.orders_metrics/Total Revenue")
    assert row is not None
    assert row["source"] == "ossie_connection"
    assert row["source_ref"] == source_id
    assert row["sql"] == "SELECT MEASURE(`Total Revenue`) FROM `main`.`sales`.`orders_metrics`"
    assert row["table_name"] is None
    assert any("DATABRICKS" in n and "not locally runnable" in n for n in row["notes"])


def test_second_sync_of_an_unchanged_workspace_is_a_no_op(e2e_env):
    from src.semantic.transports import import_source

    source_id = _register_source()
    with (
        patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=_SETTINGS),
        patch.object(DatabricksSemanticAdapter, "_client", lambda self, settings: FakeStatementClient()),
    ):
        import_source(source_id)
        second = import_source(source_id)

    assert second.models_written == 0
    assert second.models_unchanged == 1


def test_sync_prunes_a_measure_removed_upstream(e2e_env):
    """A measure dropped from an otherwise-still-present metric view prunes
    its own metric row — `project_document`'s scoped prune, exercised
    end-to-end through the semantic-source pipeline."""
    from src.repositories import metric_repo
    from src.semantic.transports import import_source

    source_id = _register_source()
    with (
        patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=_SETTINGS),
        patch.object(DatabricksSemanticAdapter, "_client", lambda self, settings: FakeStatementClient()),
    ):
        import_source(source_id)
        assert metric_repo().get(f"ossie_connection/{source_id}/main.sales.orders_metrics/Order Count") is not None

        one_measure_yaml = "version: 1.1\nsource: t\nmeasures:\n  - name: Total Revenue\n    expr: SUM(x)\n"
        shrunk_client = FakeStatementClient(yaml_by_view={"orders_metrics": one_measure_yaml})
        with patch.object(DatabricksSemanticAdapter, "_client", lambda self, settings: shrunk_client):
            import_source(source_id)

    assert metric_repo().get(f"ossie_connection/{source_id}/main.sales.orders_metrics/Order Count") is None
    assert metric_repo().get(f"ossie_connection/{source_id}/main.sales.orders_metrics/Total Revenue") is not None
