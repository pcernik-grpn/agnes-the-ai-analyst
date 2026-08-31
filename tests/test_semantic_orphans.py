"""Orphaned semantic bindings — Block 5 of #1707.

Deleting (or renaming to a new id) a registered table leaves no cascade for
two projections: ``metric_definitions`` rows bound to it by name
(``table_name`` + the multi-table ``tables[]`` array) and ``column_metadata``
rows bound to it by id (``table_id``). This is DETECTION only — nothing in
``src/semantic/orphans.py`` deletes, prunes, or blocks anything.

Backend-agnostic on purpose: every repo it touches
(``table_registry_repo``, ``metric_repo``, ``column_metadata_repo``) exists
on both DuckDB and Postgres, so these tests run against the plain DuckDB
``e2e_env`` fixture rather than needing a Postgres backend.
"""

from __future__ import annotations

import pytest

from src.semantic.orphans import (
    find_orphaned_columns,
    find_orphaned_metrics,
    find_orphaned_table_bindings,
)


@pytest.fixture
def system_db(e2e_env):
    return e2e_env


def _register_table(id_, name):
    from src.repositories import table_registry_repo

    table_registry_repo().register(
        id=id_,
        name=name,
        source_type="local",
        query_mode="local",
    )


def _create_metric(id_, name, *, table_name=None, tables=None, source="keboola_metastore"):
    from src.repositories import metric_repo

    # Registry-fed source by default: hand-authored sources (manual /
    # yaml_import / web_upload) are deliberately excluded from the orphan
    # finder, so tests seed the provenance the finder actually inspects.
    metric_repo().create(
        id=id_,
        name=name,
        display_name=name,
        category="revenue",
        sql="SELECT 1",
        table_name=table_name,
        tables=tables,
        source=source,
    )


def _save_column(table_id, column_name):
    from src.repositories import column_metadata_repo

    column_metadata_repo().save(table_id=table_id, column_name=column_name, basetype="STRING")


# ---------------------------------------------------------------------------
# pure functions — no repos, just data in / data out
# ---------------------------------------------------------------------------


class TestFindOrphanedMetrics:
    def test_a_metric_bound_to_a_missing_table_name_is_flagged(self):
        metrics = [{"id": "m1", "name": "revenue", "source": "keboola_metastore", "table_name": "orders_gone", "tables": None}]
        orphans = find_orphaned_metrics(metrics, known_table_names={"customers"})
        assert orphans == [{"metric_id": "m1", "name": "revenue", "missing_tables": ["orders_gone"]}]

    def test_a_metric_bound_to_a_live_table_name_is_not_flagged(self):
        metrics = [{"id": "m1", "name": "revenue", "source": "keboola_metastore", "table_name": "orders", "tables": None}]
        orphans = find_orphaned_metrics(metrics, known_table_names={"orders"})
        assert orphans == []

    def test_a_join_metric_reports_every_missing_table_in_the_tables_array(self):
        metrics = [{"id": "m1", "name": "arpu", "source": "keboola_metastore", "table_name": "orders", "tables": ["orders", "customers_gone"]}]
        orphans = find_orphaned_metrics(metrics, known_table_names={"orders"})
        assert orphans == [{"metric_id": "m1", "name": "arpu", "missing_tables": ["customers_gone"]}]

    def test_a_metric_with_no_table_binding_at_all_is_never_flagged(self):
        metrics = [{"id": "m1", "name": "constant", "table_name": None, "tables": None}]
        orphans = find_orphaned_metrics(metrics, known_table_names=set())
        assert orphans == []

    def test_empty_metric_list_returns_empty_list(self):
        assert find_orphaned_metrics([], known_table_names={"orders"}) == []


class TestFindOrphanedColumns:
    def test_columns_for_a_missing_table_id_are_grouped_into_one_finding(self):
        columns = [
            {"table_id": "orders_gone", "column_name": "id"},
            {"table_id": "orders_gone", "column_name": "total"},
        ]
        orphans = find_orphaned_columns(columns, known_table_ids={"customers"})
        assert orphans == [{"table_id": "orders_gone", "column_count": 2}]

    def test_columns_for_a_live_table_id_are_not_flagged(self):
        columns = [{"table_id": "orders", "column_name": "id"}]
        orphans = find_orphaned_columns(columns, known_table_ids={"orders"})
        assert orphans == []

    def test_empty_column_list_returns_empty_list(self):
        assert find_orphaned_columns([], known_table_ids={"orders"}) == []


# ---------------------------------------------------------------------------
# find_orphaned_table_bindings() — the repo-backed orchestrator
# ---------------------------------------------------------------------------


class TestFindOrphanedTableBindings:
    def test_a_clean_instance_returns_empty_lists(self, system_db):
        result = find_orphaned_table_bindings()
        assert result == {"orphaned_metrics": [], "orphaned_columns": []}

    def test_a_metric_bound_to_a_still_registered_table_is_not_flagged(self, system_db):
        _register_table("orders", "orders")
        _create_metric("revenue", "revenue", table_name="orders")

        result = find_orphaned_table_bindings()
        assert result["orphaned_metrics"] == []

    def test_a_metric_bound_to_a_deleted_table_is_flagged(self, system_db):
        _register_table("orders", "orders")
        _create_metric("revenue", "revenue", table_name="orders")

        from src.repositories import table_registry_repo

        table_registry_repo().unregister("orders")

        result = find_orphaned_table_bindings()
        assert result["orphaned_metrics"] == [
            {"metric_id": "revenue", "name": "revenue", "missing_tables": ["orders"]}
        ]

    def test_columns_for_a_deleted_table_are_flagged(self, system_db):
        _register_table("orders", "orders")
        _save_column("orders", "id")
        _save_column("orders", "total")

        from src.repositories import table_registry_repo

        table_registry_repo().unregister("orders")

        result = find_orphaned_table_bindings()
        assert result["orphaned_columns"] == [{"table_id": "orders", "column_count": 2}]

    def test_columns_for_a_still_registered_table_are_not_flagged(self, system_db):
        _register_table("orders", "orders")
        _save_column("orders", "id")

        result = find_orphaned_table_bindings()
        assert result["orphaned_columns"] == []


def test_hand_authored_metrics_are_never_flagged():
    """yaml_import / web_upload / manual metrics carry free-text table names
    that never came from table_registry — a miss there is not a deleted
    table (mirrors _orphaned_models' source='manual' exclusion)."""
    from src.semantic.orphans import find_orphaned_metrics

    metrics = [
        {"id": "m1", "name": "starter", "source": "yaml_import", "table_name": "never_registered"},
        {"id": "m2", "name": "uploaded", "source": "web_upload", "table_name": "never_registered"},
        {"id": "m3", "name": "handmade", "source": "manual", "table_name": "never_registered"},
        {"id": "m4", "name": "projected", "source": "keboola_metastore", "table_name": "never_registered"},
    ]
    orphans = find_orphaned_metrics(metrics, known_table_names={"orders"})
    assert [o["metric_id"] for o in orphans] == ["m4"]
