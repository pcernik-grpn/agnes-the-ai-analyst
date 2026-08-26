"""Source-agnostic semantic-layer coverage check (semantic-phase5, wave 1,
Task 1).

``resolve_dataset_table()`` (``src/semantic/projection.py``) is the single,
source-agnostic dataset -> ``table_registry.id`` resolver: a Keboola-sourced
document's ``dataset.source`` is the raw Keboola tableId and must go through
``resolve_table_name()``'s bucket-split lookup, while every other source's
``dataset.source``/``.name`` is matched literally against
``table_registry.id``/``.name``. ``tables_without_semantic_coverage()``
(``src/semantic_coverage.py``) builds directly on it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.semantic.projection import resolve_dataset_table
from src.semantic_coverage import tables_without_semantic_coverage


@pytest.fixture
def system_db(e2e_env):
    return e2e_env


def _register(id_, name, *, source_type="local", bucket=None, source_table=None):
    from src.repositories import table_registry_repo

    table_registry_repo().register(
        id=id_,
        name=name,
        source_type=source_type,
        bucket=bucket,
        source_table=source_table,
        query_mode="local",
    )


def _upsert_model(*, slug, source, source_ref, document, status="valid"):
    from src.repositories import semantic_model_repo

    semantic_model_repo().upsert(
        id=f"{source}/{source_ref or '_'}/{slug}",
        slug=slug,
        name=slug,
        description=None,
        document="",
        document_json=document,
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source=source,
        source_ref=source_ref,
        status=status,
        validation_errors=None,
        validated_at=datetime.now(timezone.utc),
    )


class TestResolveDatasetTable:
    def test_native_source_matches_table_registry_id(self, system_db):
        _register("orders_view", "orders_view")
        assert resolve_dataset_table({"source": "orders_view"}, "git") == "orders_view"

    def test_native_source_matches_table_registry_name_when_id_differs(self, system_db):
        _register("orders_tbl", "Orders Table")
        assert resolve_dataset_table({"source": "Orders Table"}, "git") == "orders_tbl"

    def test_native_source_falls_back_to_dataset_name(self, system_db):
        _register("orders_view", "orders_view")
        assert resolve_dataset_table({"name": "orders_view"}, "manual") == "orders_view"

    def test_unresolvable_dataset_returns_none(self, system_db):
        assert resolve_dataset_table({"source": "does-not-exist"}, "git") is None

    def test_empty_dataset_returns_none(self, system_db):
        assert resolve_dataset_table({}, "git") is None

    def test_keboola_dataset_resolves_via_bucket_table_lookup(self, system_db):
        """Critical regression: a Keboola dataset's ``source`` is the raw
        Keboola tableId (``bucket.table``), never the registered Agnes
        name — a naive text match against table_registry.id/.name would
        never find it; it must go through resolve_table_name()'s
        bucket-split registry lookup instead.
        """
        _register("shop_orders", "shop_orders", source_type="keboola", bucket="in.c-shop", source_table="orders")
        resolved = resolve_dataset_table({"source": "in.c-shop.orders"}, "keboola_metastore")
        assert resolved == "shop_orders"

    def test_keboola_naive_text_match_would_have_missed_it(self, system_db):
        """Documents exactly why ``source`` must gate the resolution
        strategy: the raw ``dataset.source`` string never equals a
        registered id/name for a Keboola-bound dataset."""
        _register("shop_orders", "shop_orders", source_type="keboola", bucket="in.c-shop", source_table="orders")
        from src.repositories import table_registry_repo

        naive = table_registry_repo().get("in.c-shop.orders") or table_registry_repo().get_by_name("in.c-shop.orders")
        assert naive is None

    def test_keboola_dataset_with_no_matching_registration_returns_none(self, system_db):
        assert resolve_dataset_table({"source": "in.c-ghost.nowhere"}, "keboola_metastore") is None


class TestTablesWithoutSemanticCoverage:
    def test_table_with_native_model_dataset_is_covered(self, system_db):
        _register("orders", "orders")
        _register("uncovered", "uncovered")
        _upsert_model(
            slug="retail",
            source="manual",
            source_ref=None,
            document={
                "semantic_model": [
                    {"name": "retail", "datasets": [{"name": "orders", "source": "orders", "fields": []}]}
                ]
            },
        )
        ids = {r["id"] for r in tables_without_semantic_coverage()}
        assert "orders" not in ids
        assert "uncovered" in ids

    def test_table_with_keboola_bound_model_dataset_is_covered(self, system_db):
        """The critical case this task exists to prevent: a Keboola table
        bound via its raw tableId must be recognized as covered."""
        _register("shop_orders", "shop_orders", source_type="keboola", bucket="in.c-shop", source_table="orders")
        _upsert_model(
            slug="core",
            source="keboola_metastore",
            source_ref=None,
            document={
                "semantic_model": [
                    {"name": "core", "datasets": [{"name": "orders", "source": "in.c-shop.orders", "fields": []}]}
                ]
            },
        )
        ids = {r["id"] for r in tables_without_semantic_coverage()}
        assert "shop_orders" not in ids

    def test_table_with_no_model_at_all_is_uncovered(self, system_db):
        _register("lonely", "lonely")
        ids = {r["id"] for r in tables_without_semantic_coverage()}
        assert "lonely" in ids

    def test_dataset_with_zero_metrics_still_covers_its_table(self, system_db):
        """The coverage check reads datasets directly, not through
        metric_definitions.table_name — a dataset with no metrics at all
        must still count as covering its table."""
        _register("orders", "orders")
        _upsert_model(
            slug="retail",
            source="manual",
            source_ref=None,
            document={
                "semantic_model": [
                    {
                        "name": "retail",
                        "datasets": [{"name": "orders", "source": "orders", "fields": []}],
                        "metrics": [],
                    }
                ]
            },
        )
        ids = {r["id"] for r in tables_without_semantic_coverage()}
        assert "orders" not in ids

    def test_invalid_status_model_does_not_grant_coverage(self, system_db):
        _register("orders", "orders")
        _upsert_model(
            slug="retail",
            source="manual",
            source_ref=None,
            status="invalid",
            document={"semantic_model": [{"name": "retail", "datasets": [{"name": "orders", "source": "orders"}]}]},
        )
        ids = {r["id"] for r in tables_without_semantic_coverage()}
        assert "orders" in ids

    def test_returns_full_table_registry_rows(self, system_db):
        _register("lonely", "lonely")
        rows = tables_without_semantic_coverage()
        assert rows and rows[0]["name"] == "lonely"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestSemanticCoverageEndpoint:
    def test_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        r = c.get("/api/admin/semantic-coverage", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_returns_uncovered_tables(self, seeded_app):
        _register("lonely", "lonely")
        c = seeded_app["client"]
        r = c.get("/api/admin/semantic-coverage", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        body = r.json()
        ids = {row["id"] for row in body["tables"]}
        assert "lonely" in ids
