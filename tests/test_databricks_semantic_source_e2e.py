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

import pytest

from connectors.databricks.semantic_ossie import DatabricksMetricViewAdapter
from tests.test_databricks_semantic_ossie import _SETTINGS, FakeStatementClient


def _register_source(source_id: str = "ss_dbx_test") -> str:
    from src.repositories import semantic_source_repo

    semantic_source_repo().create(
        id=source_id,
        kind="connection",
        name="Databricks metric views",
        adapter="databricks_metric_views",
        config={},
    )
    return source_id


def test_databricks_source_syncs_into_semantic_models_and_metric_definitions(e2e_env):
    from src.repositories import metric_repo, semantic_model_repo
    from src.semantic.transports import import_source

    source_id = _register_source()

    with (
        patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=_SETTINGS),
        patch.object(DatabricksMetricViewAdapter, "_client", lambda self, settings: FakeStatementClient()),
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
        patch.object(DatabricksMetricViewAdapter, "_client", lambda self, settings: FakeStatementClient()),
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
        patch.object(DatabricksMetricViewAdapter, "_client", lambda self, settings: FakeStatementClient()),
    ):
        import_source(source_id)
        assert metric_repo().get(f"ossie_connection/{source_id}/main.sales.orders_metrics/Order Count") is not None

        one_measure_yaml = "version: 1.1\nsource: t\nmeasures:\n  - name: Total Revenue\n    expr: SUM(x)\n"
        shrunk_client = FakeStatementClient(yaml_by_view={"orders_metrics": one_measure_yaml})
        with patch.object(DatabricksMetricViewAdapter, "_client", lambda self, settings: shrunk_client):
            import_source(source_id)

    assert metric_repo().get(f"ossie_connection/{source_id}/main.sales.orders_metrics/Order Count") is None
    assert metric_repo().get(f"ossie_connection/{source_id}/main.sales.orders_metrics/Total Revenue") is not None


# ---------------------------------------------------------------------------
# The full-wipe guard on the source the sweep auto-registers
# ---------------------------------------------------------------------------


def _ensured_source() -> str:
    """The Databricks source as the sweep's auto-migration registers it —
    the config under test, never a hand-written stand-in."""
    from connectors.databricks.semantic_layer import ensure_semantic_source

    with patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=_SETTINGS):
        source_id = ensure_semantic_source()
    assert source_id is not None
    return source_id


def test_the_ensured_source_carries_the_full_wipe_guard(e2e_env):
    """``safe_prune`` is the same valve the migrated Keboola source carries,
    and for the same reason: a discovery that succeeds while every view's
    ``SHOW CREATE TABLE`` fails is indistinguishable from "the workspace has
    no metric views any more"."""
    from src.repositories import semantic_source_repo

    source_id = _ensured_source()
    assert semantic_source_repo().get(source_id)["config"]["safe_prune"] is True


def test_a_successful_but_empty_discovery_does_not_wipe_the_workspaces_rows(e2e_env):
    """Every view's ``SHOW CREATE TABLE`` failing is a SUCCESSFUL sync that
    returns nothing (the adapter skips a view it cannot read rather than
    sinking the whole run). Without the guard that empty result prunes every
    row this source owns."""
    from connectors.databricks.client import DatabricksApiError
    from src.repositories import metric_repo, semantic_model_repo, semantic_source_repo
    from src.semantic.transports import import_source

    source_id = _ensured_source()
    with (
        patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=_SETTINGS),
        patch.object(DatabricksMetricViewAdapter, "_client", lambda self, settings: FakeStatementClient()),
    ):
        import_source(source_id)
    assert len(metric_repo().list()) == 2

    blinded = FakeStatementClient(
        fail_with=DatabricksApiError("transient", status=503),
        fail_views={"orders_metrics"},
    )
    with (
        patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=_SETTINGS),
        patch.object(DatabricksMetricViewAdapter, "_client", lambda self, settings: blinded),
    ):
        report = import_source(source_id)

    assert not report.models_pruned
    assert len(metric_repo().list()) == 2
    assert len(semantic_model_repo().list_all()) == 1
    # The sync itself is still recorded as the success it was.
    assert semantic_source_repo().get(source_id)["last_sync_status"] == "ok"


def test_a_transient_discovery_failure_raises_and_imports_nothing(e2e_env):
    """The other half of the same protection: when the workspace cannot be
    enumerated at all, the adapter must RAISE rather than return an empty
    document list — an error recorded on the row, and not one row touched."""
    from connectors.databricks.client import DatabricksApiError
    from src.repositories import metric_repo, semantic_source_repo
    from src.semantic.transports import import_source

    source_id = _ensured_source()
    with (
        patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=_SETTINGS),
        patch.object(DatabricksMetricViewAdapter, "_client", lambda self, settings: FakeStatementClient()),
    ):
        import_source(source_id)
    assert len(metric_repo().list()) == 2

    down = FakeStatementClient(fail_with=DatabricksApiError("warehouse unavailable", status=503))
    with (
        patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=_SETTINGS),
        patch.object(DatabricksMetricViewAdapter, "_client", lambda self, settings: down),
        pytest.raises(DatabricksApiError),
    ):
        import_source(source_id)

    assert len(metric_repo().list()) == 2
    row = semantic_source_repo().get(source_id)
    assert row["last_sync_status"] == "error"
    assert "warehouse unavailable" in (row["last_sync_error"] or "")
