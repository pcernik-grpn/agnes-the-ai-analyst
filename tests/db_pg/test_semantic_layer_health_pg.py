"""Logic of ``compute_semantic_layer_health`` and its REST surface.

PG-side by necessity: the mute overlay reads ``semantic_health_mutes``, a
Postgres-only table (A3 PG-first ratchet), so Postgres is the only backend
on which the full roll-up can be exercised. The DuckDB side's contract — the
admin gate, then a typed ``501 requires_postgres_backend`` — is pinned in
``tests/test_semantic_layer_health_endpoint.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_HEALTH = "/api/admin/semantic-layer/health"


@pytest.fixture
def pg_state(pg_engine, tmp_path, monkeypatch):
    """Postgres app-state backend, migrated to head, repos routed to it."""
    import importlib

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    import src.repositories

    importlib.reload(src.repositories)
    return pg_engine


def _source(source_id: str, *, name: str = "s", status: str | None = None, error: str | None = None) -> None:
    from src.repositories import semantic_source_repo

    repo = semantic_source_repo()
    repo.create(id=source_id, kind="upload", name=name, adapter="native", config={})
    if status is not None:
        repo.record_sync(source_id, status=status, error=error)


def _model(
    model_id: str,
    *,
    slug: str,
    source: str = "manual",
    source_ref: str | None = None,
    status: str = "valid",
    document_json: dict | None = None,
) -> None:
    from src.repositories import semantic_model_repo

    semantic_model_repo().upsert(
        id=model_id,
        slug=slug,
        name=slug,
        description=None,
        document="{}",
        document_json=document_json,
        spec_version="0.2.0",
        content_hash=f"hash-{model_id}",
        source=source,
        source_ref=source_ref,
        status=status,
        validation_errors=None,
        validated_at=None,
    )


def _metric(
    metric_id: str,
    *,
    name: str,
    sql: str,
    description: str | None = None,
    table_name: str | None = None,
    tables: list[str] | None = None,
) -> None:
    from src.repositories import metric_repo

    # Registry-fed source: hand-authored sources are deliberately excluded
    # from the orphan finder, so seed the provenance it actually inspects.
    metric_repo().create(
        id=metric_id,
        name=name,
        display_name=name,
        category="revenue",
        sql=sql,
        description=description,
        table_name=table_name,
        tables=tables,
        source="keboola_metastore",
    )


def _table(table_id: str, *, name: str) -> None:
    from src.repositories import table_registry_repo

    table_registry_repo().register(id=table_id, name=name, source_type="local", query_mode="local")


def _column(table_id: str, column_name: str) -> None:
    from src.repositories import column_metadata_repo

    column_metadata_repo().save(table_id=table_id, column_name=column_name, basetype="STRING")


class TestSyncStatus:
    def test_reports_every_source_verbatim(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _source("src-a", name="Warehouse", status="error", error="Metastore timed out")
        _source("src-b", name="Files")

        health = compute_semantic_layer_health()
        by_id = {s["source_id"]: s for s in health["sources"]}
        assert by_id["src-a"]["last_sync_status"] == "error"
        assert by_id["src-a"]["last_sync_error"] == "Metastore timed out"
        assert by_id["src-b"]["last_sync_status"] is None


class TestOrphanedModels:
    def test_a_model_whose_source_was_deleted_is_flagged(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _model("m1", slug="revenue", source="git", source_ref="src-gone")

        health = compute_semantic_layer_health()
        assert [m["model_id"] for m in health["orphaned_models"]] == ["m1"]

    def test_a_model_whose_source_still_exists_is_not_flagged(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _source("src-a")
        _model("m1", slug="revenue", source="git", source_ref="src-a")

        health = compute_semantic_layer_health()
        assert health["orphaned_models"] == []

    def test_a_hand_authored_model_with_no_source_ref_is_never_flagged(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _model("m1", slug="revenue", source="manual", source_ref=None)

        health = compute_semantic_layer_health()
        assert health["orphaned_models"] == []

    def test_a_keboola_model_with_a_live_connection_is_not_flagged(self, pg_state):
        """Keboola metastore sync stamps ``source_ref`` with the
        ``source_connections.id`` it came from, never a ``semantic_sources``
        row — checking it against ``semantic_sources`` always misses."""
        from src.repositories import source_connections_repo
        from src.semantic.coverage import compute_semantic_layer_health

        source_connections_repo().create(
            id="conn-kbc", name="Keboola", source_type="keboola", config={}, is_default=False, created_by="test"
        )
        _model("m1", slug="revenue", source="keboola_metastore", source_ref="conn-kbc")

        health = compute_semantic_layer_health()
        assert health["orphaned_models"] == []

    def test_a_keboola_model_whose_connection_was_deleted_is_flagged(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _model("m1", slug="revenue", source="keboola_metastore", source_ref="conn-gone")

        health = compute_semantic_layer_health()
        assert [m["model_id"] for m in health["orphaned_models"]] == ["m1"]

    def test_a_databricks_model_with_a_live_semantic_source_is_not_flagged(self, pg_state):
        """Since the semantic-layer Phase 1 cutover, a Databricks model
        flows through the standard connection-kind pipeline like every other
        source — ``source='ossie_connection'`` + ``source_ref=<semantic
        source id>`` (the fixed ``databricks_default`` id,
        :func:`connectors.databricks.semantic_layer.ensure_semantic_source`)
        — so it needs no dispatch entry of its own: the generic
        ``source_ref in known_source_ids`` check already asks the right
        question. Pre-cutover this stamped ``source='databricks_metrics'`` +
        ``source_ref=<workspace host>`` instead, which is what the retired
        per-host dispatch this test used to exercise was for."""
        from src.semantic.coverage import compute_semantic_layer_health

        _source("databricks_default", name="Databricks metric views")
        _model("m1", slug="revenue", source="ossie_connection", source_ref="databricks_default")

        health = compute_semantic_layer_health()
        assert health["orphaned_models"] == []

    def test_a_databricks_model_whose_semantic_source_row_is_gone_is_flagged(self, pg_state):
        """The fixed ``databricks_default`` row missing means Databricks was
        never (re)synced since the row was deleted or the instance never
        configured it — same shape as any other connection-kind source
        going stale."""
        from src.semantic.coverage import compute_semantic_layer_health

        _model("m1", slug="revenue", source="ossie_connection", source_ref="databricks_default")

        health = compute_semantic_layer_health()
        assert [m["model_id"] for m in health["orphaned_models"]] == ["m1"]


class TestOrphanedTableBindings:
    """Block 5 of #1707 — a delete (or a rename with no cascade) left a
    metric or a profiled column bound to a table that no longer exists."""

    def test_a_clean_instance_reports_no_orphans(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        health = compute_semantic_layer_health()
        assert health["orphaned_table_bindings"] == []

    def test_a_metric_bound_to_a_deleted_table_is_flagged(self, pg_state):
        from src.repositories import table_registry_repo
        from src.semantic.coverage import compute_semantic_layer_health

        _table("orders", name="orders")
        _metric("met1", name="revenue", sql="SELECT SUM(amount) FROM orders", table_name="orders")
        table_registry_repo().unregister("orders")

        health = compute_semantic_layer_health()
        assert health["orphaned_table_bindings"] == [
            {"binding": "metric", "metric_id": "met1", "name": "revenue", "missing_tables": ["orders"]}
        ]

    def test_a_metric_bound_to_a_live_table_is_not_flagged(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _table("orders", name="orders")
        _metric("met1", name="revenue", sql="SELECT SUM(amount) FROM orders", table_name="orders")

        health = compute_semantic_layer_health()
        assert health["orphaned_table_bindings"] == []

    def test_columns_for_a_deleted_table_are_flagged_with_a_count(self, pg_state):
        from src.repositories import table_registry_repo
        from src.semantic.coverage import compute_semantic_layer_health

        _table("orders", name="orders")
        _column("orders", "id")
        _column("orders", "total")
        table_registry_repo().unregister("orders")

        health = compute_semantic_layer_health()
        assert health["orphaned_table_bindings"] == [{"binding": "column", "table_id": "orders", "column_count": 2}]


class TestInvalidModels:
    def test_an_invalid_model_is_flagged_with_its_errors(self, pg_state):
        from src.repositories import semantic_model_repo
        from src.semantic.coverage import compute_semantic_layer_health

        semantic_model_repo().upsert(
            id="m1",
            slug="broken",
            name="broken",
            description=None,
            document="{}",
            document_json=None,
            spec_version="0.2.0",
            content_hash="h1",
            source="manual",
            source_ref=None,
            status="invalid",
            validation_errors=["missing required field 'datasets'"],
            validated_at=None,
        )

        health = compute_semantic_layer_health()
        assert health["invalid_models"] == [
            {"model_id": "m1", "slug": "broken", "validation_errors": ["missing required field 'datasets'"]}
        ]


class TestMetricsMissingDescription:
    def test_a_metric_with_no_description_is_flagged(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _metric("met1", name="revenue", sql="SELECT 1", description=None)
        _metric("met2", name="churn", sql="SELECT 2", description="Customers lost this period.")

        health = compute_semantic_layer_health()
        assert [m["name"] for m in health["metrics_missing_description"]] == ["revenue"]

    def test_whitespace_only_description_still_counts_as_missing(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _metric("met1", name="revenue", sql="SELECT 1", description="   ")

        health = compute_semantic_layer_health()
        assert [m["name"] for m in health["metrics_missing_description"]] == ["revenue"]


class TestDuplicateMetricNames:
    def test_same_name_different_sql_is_flagged(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _metric("met1", name="revenue", sql="SELECT SUM(gross) FROM sales", description="gross")
        _metric("met2", name="revenue", sql="SELECT SUM(net) FROM sales", description="net")

        health = compute_semantic_layer_health()
        assert len(health["duplicate_metric_names"]) == 1
        finding = health["duplicate_metric_names"][0]
        assert finding["name"] == "revenue"
        assert len(finding["expressions"]) == 2

    def test_same_name_same_sql_is_not_flagged(self, pg_state):
        """Two rows for one metric imported twice by two syncs is normal,
        not a conflict — only a DIFFERENT formula under the same name is."""
        from src.semantic.coverage import compute_semantic_layer_health

        _metric("met1", name="revenue", sql="SELECT SUM(gross) FROM sales", description="gross")
        _metric("met2", name="revenue", sql="SELECT SUM(gross) FROM sales", description="gross")

        health = compute_semantic_layer_health()
        assert health["duplicate_metric_names"] == []

    def test_a_unique_name_is_not_flagged(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _metric("met1", name="revenue", sql="SELECT 1", description="d")

        health = compute_semantic_layer_health()
        assert health["duplicate_metric_names"] == []


class TestMetricsMissingRelationships:
    _DOC_WITH_RELATIONSHIP = {
        "semantic_model": [
            {
                "datasets": [{"name": "orders"}, {"name": "customers"}],
                "relationships": [{"name": "o2c", "from": "orders", "to": "customers"}],
                "metrics": [
                    {
                        "name": "revenue_per_customer",
                        "expression": {
                            "dialects": [
                                {
                                    "dialect": "ANSI_SQL",
                                    "expression": "SUM(orders.amount) / COUNT(customers.id)",
                                }
                            ]
                        },
                    }
                ],
            }
        ]
    }

    _DOC_WITHOUT_RELATIONSHIP = {
        "semantic_model": [
            {
                "datasets": [{"name": "orders"}, {"name": "shipments"}],
                "relationships": [],
                "metrics": [
                    {
                        "name": "orders_per_shipment",
                        "expression": {
                            "dialects": [
                                {
                                    "dialect": "ANSI_SQL",
                                    "expression": "COUNT(orders.id) / COUNT(shipments.id)",
                                }
                            ]
                        },
                    }
                ],
            }
        ]
    }

    def test_a_cross_dataset_metric_with_a_declared_relationship_is_not_flagged(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _model("m1", slug="sales", source="git", document_json=self._DOC_WITH_RELATIONSHIP)

        health = compute_semantic_layer_health()
        assert health["metrics_missing_relationships"] == []

    def test_a_cross_dataset_metric_with_no_declared_relationship_is_flagged(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        _model("m1", slug="shipping", source="git", document_json=self._DOC_WITHOUT_RELATIONSHIP)

        health = compute_semantic_layer_health()
        findings = health["metrics_missing_relationships"]
        assert len(findings) == 1
        assert findings[0]["metric_name"] == "orders_per_shipment"
        assert set(findings[0]["datasets"]) == {"orders", "shipments"}

    def test_a_single_dataset_metric_is_never_flagged(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        doc = {
            "semantic_model": [
                {
                    "datasets": [{"name": "orders"}],
                    "relationships": [],
                    "metrics": [
                        {
                            "name": "order_count",
                            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "COUNT(orders.id)"}]},
                        }
                    ],
                }
            ]
        }
        _model("m1", slug="orders-only", source="git", document_json=doc)

        health = compute_semantic_layer_health()
        assert health["metrics_missing_relationships"] == []

    def test_an_invalid_model_is_skipped_entirely(self, pg_state):
        """A document that failed validation may have any shape at all — it
        is already flagged by ``invalid_models``, and parsing it further for
        a second, unrelated finding is not this check's job."""
        from src.repositories import semantic_model_repo
        from src.semantic.coverage import compute_semantic_layer_health

        semantic_model_repo().upsert(
            id="m1",
            slug="broken",
            name="broken",
            description=None,
            document="{}",
            document_json=self._DOC_WITHOUT_RELATIONSHIP,
            spec_version="0.2.0",
            content_hash="h1",
            source="git",
            source_ref=None,
            status="invalid",
            validation_errors=["nope"],
            validated_at=None,
        )

        health = compute_semantic_layer_health()
        assert health["metrics_missing_relationships"] == []


class TestCoverageSummary:
    def test_counts_missing_and_partial_cells_across_sources(self, pg_state):
        from src.repositories import source_connections_repo
        from src.semantic.coverage import compute_semantic_layer_health

        source_connections_repo().create(
            id="conn-a", name="A", source_type="bigquery", config={}, is_default=False, created_by="test"
        )

        health = compute_semantic_layer_health()
        summary = health["coverage_summary"]
        assert summary["missing_count"] > 0
        assert "partial_count" in summary


class TestMuteOverlay:
    def test_active_mutes_are_included(self, pg_state):
        from src.repositories import semantic_health_mutes_repo
        from src.semantic.coverage import compute_semantic_layer_health

        semantic_health_mutes_repo().create(scope="domain:metrics", muted_by="admin@x.com", reason="known gap")

        health = compute_semantic_layer_health()
        assert len(health["mutes"]) == 1
        assert health["mutes"][0]["scope"] == "domain:metrics"
        assert health["mutes"][0]["muted_by"] == "admin@x.com"

    def test_no_mutes_is_an_empty_list_not_an_error(self, pg_state):
        from src.semantic.coverage import compute_semantic_layer_health

        health = compute_semantic_layer_health()
        assert health["mutes"] == []


class TestTheEndpointOnPostgres:
    def test_health_answers_200_with_the_full_shape(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("DuckDB side is pinned in tests/test_semantic_layer_health_endpoint.py")

        resp = seeded_app_both["client"].get(
            _HEALTH, headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for key in (
            "sources",
            "orphaned_models",
            "orphaned_table_bindings",
            "invalid_models",
            "metrics_missing_description",
            "duplicate_metric_names",
            "metrics_missing_relationships",
            "coverage_summary",
            "mutes",
        ):
            assert key in body, f"missing {key!r} in {sorted(body)}"

    def test_health_reflects_a_mute_made_through_the_mutes_endpoint(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only surface")
        client = seeded_app_both["client"]
        auth = {"Authorization": f"Bearer {seeded_app_both['admin_token']}"}

        client.post(
            "/api/admin/semantic-layer/mutes", json={"scope": "domain:glossary", "reason": "tracked"}, headers=auth
        )

        resp = client.get(_HEALTH, headers=auth)
        assert resp.status_code == 200, resp.text
        assert [m["scope"] for m in resp.json()["mutes"]] == ["domain:glossary"]
