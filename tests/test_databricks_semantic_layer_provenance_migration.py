"""``connectors/databricks/semantic_layer.py``: the settings/identity helpers
Track D6 added — ``ensure_semantic_source`` (idempotent get-or-create for the
`connection`-kind semantic source) and ``purge_legacy_metric_rows`` (the
one-time cutover away from the retired direct writer's provenance).
"""

from __future__ import annotations

from connectors.databricks.semantic_layer import (
    DATABRICKS_SEMANTIC_SOURCE_ID,
    LEGACY_METRIC_SOURCE,
    ensure_semantic_source,
    purge_legacy_metric_rows,
)


class TestEnsureSemanticSource:
    def test_creates_a_connection_kind_source_on_first_call(self, e2e_env):
        from src.repositories import semantic_source_repo

        source_id = ensure_semantic_source()
        assert source_id == DATABRICKS_SEMANTIC_SOURCE_ID == "databricks_default"

        row = semantic_source_repo().get(source_id)
        assert row is not None
        assert row["kind"] == "connection"
        assert row["adapter"] == "databricks_semantic"

    def test_is_idempotent_and_does_not_overwrite_an_existing_row(self, e2e_env):
        from src.repositories import semantic_source_repo

        first = ensure_semantic_source()
        repo = semantic_source_repo()
        # An admin disabling the source (or renaming it) must survive a
        # second scheduled run — get-or-create, never get-or-replace.
        repo.update(first, enabled=False, name="My renamed source")

        second = ensure_semantic_source()
        assert second == first
        row = repo.get(second)
        assert row["enabled"] is False
        assert row["name"] == "My renamed source"


class TestPurgeLegacyMetricRows:
    def test_removes_only_the_legacy_source_rows(self, e2e_env):
        from src.repositories import metric_repo

        repo = metric_repo()
        repo.create(
            id="databricks/main.sales.orders/revenue",
            name="revenue",
            display_name="revenue",
            category="databricks",
            sql="SELECT MEASURE(`revenue`) FROM `main`.`sales`.`orders`",
            source=LEGACY_METRIC_SOURCE,
            source_ref="dbc-old.cloud.databricks.com",
        )
        repo.create(
            id="ossie_connection/databricks_default/main.sales.orders/revenue",
            name="revenue (new)",
            display_name="revenue (new)",
            category="main.sales.orders",
            sql="SELECT MEASURE(`revenue`) FROM `main`.`sales`.`orders`",
            source="ossie_connection",
            source_ref="databricks_default",
        )
        repo.create(
            id="manual/revenue",
            name="manual revenue",
            display_name="manual revenue",
            category="finance",
            sql="SELECT SUM(amount) FROM orders",
            source="manual",
        )

        purged = purge_legacy_metric_rows()

        assert purged == 1
        assert repo.get("databricks/main.sales.orders/revenue") is None
        assert repo.get("ossie_connection/databricks_default/main.sales.orders/revenue") is not None
        assert repo.get("manual/revenue") is not None

    def test_is_idempotent(self, e2e_env):
        from src.repositories import metric_repo

        repo = metric_repo()
        repo.create(
            id="databricks/main.sales.orders/revenue",
            name="revenue",
            display_name="revenue",
            category="databricks",
            sql="SELECT MEASURE(`revenue`) FROM `main`.`sales`.`orders`",
            source=LEGACY_METRIC_SOURCE,
            source_ref="dbc-old.cloud.databricks.com",
        )

        assert purge_legacy_metric_rows() == 1
        assert purge_legacy_metric_rows() == 0

    def test_no_legacy_rows_is_a_clean_no_op(self, e2e_env):
        assert purge_legacy_metric_rows() == 0
