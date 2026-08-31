"""Post-cutover regression for the semantic-layer flat-table cutover (wave 2 of
`docs/superpowers/plans/2026-08-16-semantic-layer-parity-sequencing.md`).

Since the cutover, `sync_semantic_layer` is a pure document pipeline: it stores
each Keboola project's Metastore as an Ossie document under
`source='keboola_metastore'` and projects that document — via
`src.semantic.projection.project_document` — into the flat query tables
(`metric_definitions` / `glossary_terms` / `column_metadata`). The projector is
the SOLE writer of those tables; the legacy flat composer that used to write
`source='keboola_semantic_layer'` rows in `_sync_one_source` is gone.

This test runs one fixture Metastore project through that single writer and
pins the fields a query actually depends on — `sql`, `table_name`, `validation`,
the composed JOIN — as ABSOLUTE expected values (not derived from any composer,
so it survives as a regression). It also asserts the two things the cutover is
responsible for:

  - no `source='keboola_semantic_layer'` row survives a sync (the retired
    source is purged once the projection writes rows);
  - the intended differences the wave-1 N4 decision made on purpose (id shape,
    dropped `grain`/`dimensions`) hold, so the cutover cannot quietly
    reintroduce the misattribution waves 0/1 removed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _register_keboola_table(bucket: str, source_table: str, name: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=name,
            name=name,
            source_type="keboola",
            bucket=bucket,
            source_table=source_table,
            query_mode="local",
        )
    finally:
        conn.close()


def _model_item(uuid: str, name: str) -> dict:
    return {"type": "semantic-model", "id": uuid, "attributes": {"name": name}}


def _dataset_item(table_id: str, model_uuid: str, *, grain: str | None = None, primary_key=None) -> dict:
    attrs: dict = {"name": table_id.rpartition(".")[2], "tableId": table_id, "modelUUID": model_uuid}
    if grain:
        attrs["grain"] = grain
    if primary_key:
        attrs["primaryKey"] = list(primary_key)
    return {"type": "semantic-dataset", "id": f"ds-{table_id}", "attributes": attrs}


def _metric_item(name: str, sql: str, dataset: str, model_uuid: str) -> dict:
    return {
        "type": "semantic-metric",
        "id": f"m-{name}",
        "attributes": {"name": name, "sql": sql, "dataset": dataset, "modelUUID": model_uuid},
    }


def _constraint_item(name: str, rule: str, metrics, severity: str = "error") -> dict:
    return {
        "type": "semantic-constraint",
        "id": f"c-{name}",
        "attributes": {
            "name": name,
            "constraintType": "range",
            "rule": rule,
            "metrics": list(metrics),
            "severity": severity,
        },
    }


def _relationship_item(name: str, from_id: str, to_id: str, on: str) -> dict:
    # type="left" and the metric's dataset on the `to` side — the one
    # live-verified case `resolve_relationship` accepts.
    return {
        "type": "semantic-relationship",
        "id": f"r-{name}",
        "attributes": {"name": name, "from": from_id, "to": to_id, "type": "left", "on": on},
    }


def _seed_columns(table_name: str, columns) -> None:
    from src.db import get_system_db
    from src.repositories.column_metadata import ColumnMetadataRepository

    conn = get_system_db()
    try:
        repo = ColumnMetadataRepository(conn)
        for col in columns:
            repo.save(table_id=table_name, column_name=col, basetype="VARCHAR", description=None, source="test")
    finally:
        conn.close()


# One fixture, exercising the cases the plan (§ wave 2) calls out: a plain
# bound metric, a metric carrying a constraint, a metric on a table the
# instance never registered (skipped), and a foreign-alias metric that a
# relationship resolves into a LEFT JOIN.
_ITEMS = {
    "semantic-model": [_model_item("model-1", "core")],
    "semantic-dataset": [
        _dataset_item("in.c-shop.orders", "model-1", grain="monthly", primary_key=["order_id"]),
        _dataset_item("in.c-shop.customers", "model-1"),
        _dataset_item("in.c-nowhere.ghosts", "model-1"),
    ],
    "semantic-metric": [
        _metric_item("total_revenue", 'SUM("amount")', "in.c-shop.orders", "model-1"),
        _metric_item("order_count", "COUNT(*)", "in.c-shop.orders", "model-1"),
        _metric_item("ghost_metric", "COUNT(*)", "in.c-nowhere.ghosts", "model-1"),
        # A foreign-alias metric on `orders` reaching a column on the joined
        # `customers` table. With the relationship below, the projector composes
        # a LEFT JOIN — the parity case the JOIN port closed.
        _metric_item("distinct_regions", 'COUNT(DISTINCT c."region")', "in.c-shop.orders", "model-1"),
    ],
    "semantic-constraint": [_constraint_item("non_negative", "value >= 0", ["total_revenue"])],
    "semantic-relationship": [
        # Distinct join columns on each side so the alias resolution is
        # unambiguous: customers."customer_id" = orders."cust_ref".
        _relationship_item(
            "orders_customers", "in.c-shop.customers", "in.c-shop.orders", 'c."customer_id" = o."cust_ref"'
        ),
    ],
    "semantic-glossary": [],
}


def _by_name(rows: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in rows}


def _norm_validation(v):
    return json.loads(v) if isinstance(v, str) else v


@pytest.fixture
def synced(e2e_env):
    """Register the shop tables, seed a stale legacy row, run the sync against
    the fixture, and return (metastore_rows_by_name, all_metric_rows)."""
    from connectors.keboola.semantic_layer import sync_semantic_layer
    from src.repositories import metric_repo

    _register_keboola_table("in.c-shop", "orders", "shop_orders")
    _register_keboola_table("in.c-shop", "customers", "shop_customers")
    # Both the simple bind and the JOIN resolve alias sides against real column
    # metadata (populated by the profiler in prod). Distinct join columns keep
    # the resolution unambiguous.
    _seed_columns("shop_orders", ["order_id", "amount", "cust_ref"])
    _seed_columns("shop_customers", ["customer_id", "region"])

    # A row from the retired writer. The sync must purge it once the projection
    # writes rows — nothing under source='keboola_semantic_layer' may survive.
    metric_repo().create(
        id="keboola/old-model/legacy_only",
        name="legacy_only",
        display_name="legacy_only",
        category="keboola",
        sql='SELECT 1 FROM "shop_orders" AS t',
        table_name="shop_orders",
        source="keboola_semantic_layer",
        source_ref=None,
    )

    fake_storage = MagicMock()
    fake_storage.verify_token.return_value = {"isMasterToken": True}
    fake_metastore = MagicMock()
    fake_metastore.list_items.side_effect = lambda item_type, model_uuid=None: _ITEMS[item_type]

    with (
        patch("connectors.keboola.storage_api.KeboolaStorageClient", return_value=fake_storage),
        patch("connectors.keboola.metastore_client.MetastoreClient", return_value=fake_metastore),
    ):
        sync_semantic_layer(keboola_url="https://connection.keboola.com", keboola_token="master-tok")

    all_rows = metric_repo().list()
    metastore = [m for m in all_rows if m.get("source") == "keboola_metastore"]
    return _by_name(metastore), all_rows


class TestSingleWriter:
    """The projector is the only writer; the retired source leaves no rows."""

    def test_only_the_bound_metrics_are_written(self, synced):
        metastore, _ = synced
        # ghost_metric (unregistered table) is skipped; distinct_regions
        # (foreign alias) is composed via the relationship.
        assert set(metastore) == {"total_revenue", "order_count", "distinct_regions"}

    def test_no_legacy_source_rows_survive(self, synced):
        _, all_rows = synced
        assert [m for m in all_rows if m.get("source") == "keboola_semantic_layer"] == []
        assert not any(m["name"] == "legacy_only" for m in all_rows)


class TestLoadBearingFields:
    """The fields a query actually depends on, pinned as absolute values."""

    def test_simple_bound_sql(self, synced):
        metastore, _ = synced
        assert metastore["total_revenue"]["sql"] == 'SELECT SUM("amount") FROM "shop_orders" AS t'
        assert metastore["order_count"]["sql"] == 'SELECT COUNT(*) FROM "shop_orders" AS t'

    def test_table_binding(self, synced):
        metastore, _ = synced
        for name in ("total_revenue", "order_count"):
            assert metastore[name]["table_name"] == "shop_orders"

    def test_join_sql(self, synced):
        metastore, _ = synced
        sql = metastore["distinct_regions"]["sql"]
        assert sql.startswith("SELECT ")
        assert "LEFT JOIN" in sql
        # The foreign alias `c.` is rewritten to the joined-table alias.
        assert 'c."region"' not in sql
        assert set(metastore["distinct_regions"]["tables"]) == {"shop_orders", "shop_customers"}
        assert metastore["distinct_regions"]["table_name"] == "shop_orders"

    def test_constraint_becomes_validation(self, synced):
        metastore, _ = synced
        pv = _norm_validation(metastore["total_revenue"]["validation"])
        assert pv is not None
        assert [r["name"] for r in pv["rules"]] == ["non_negative"]
        assert pv["rules"][0]["rule"] == "value >= 0"

    def test_a_metric_without_constraints_has_no_validation(self, synced):
        metastore, _ = synced
        assert _norm_validation(metastore["order_count"]["validation"]) is None

    def test_expression_preserves_the_raw_fragment(self, synced):
        """`expression` is the bare aggregation fragment `resolve_expression`
        returns, captured BEFORE `_bind_metric` composes it into `sql` — the
        legacy composer stored the same fragment alongside its composed sql,
        and the All-metrics tab of `semantic_layer_list.html` gates its "Expression" block on it."""
        metastore, _ = synced
        assert metastore["total_revenue"]["expression"] == 'SUM("amount")'
        assert metastore["order_count"]["expression"] == "COUNT(*)"
        # The foreign-alias JOIN case: `expression` is the RAW fragment, still
        # aliased `c.`, not the `sql` column's rewritten-to-`j.` composed JOIN.
        assert metastore["distinct_regions"]["expression"] == 'COUNT(DISTINCT c."region")'


class TestIntendedDifferences:
    """Differences the wave-1 N4 decision made on purpose — pinned so the
    cutover cannot quietly reintroduce the misattribution wave 0/1 removed."""

    def test_id_shape(self, synced):
        metastore, _ = synced
        # source/source_ref/model-key/name — source_ref is None for the
        # explicit single-source path, so the ref segment is `_`. The model
        # segment is the model's STABLE upstream id (the Metastore UUID the
        # adapter carries), not its display name: names are neither unique nor
        # stable, and two like-named models keyed on the name collapse onto
        # one id, the later silently overwriting the earlier.
        assert metastore["total_revenue"]["id"] == "keboola_metastore/_/model-1/total_revenue"

    def test_grain_is_dropped(self, synced):
        metastore, _ = synced
        # The dataset's grain is a fact about the dataset, not the metric; it is
        # never stamped onto `metric_definitions.grain`.
        assert metastore["total_revenue"]["grain"] is None

    def test_dataset_grain_survives_as_a_note(self, synced):
        metastore, _ = synced
        assert any("monthly" in n for n in (metastore["total_revenue"]["notes"] or []))

    def test_dimensions_from_primary_key_are_dropped(self, synced):
        metastore, _ = synced
        # A dataset's primary key is not a metric's dimension set.
        assert not metastore["total_revenue"]["dimensions"]
