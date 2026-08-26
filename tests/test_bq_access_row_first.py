"""``get_bq_access()`` row-first resolution + cache invalidation (D2.2): the
default bigquery ``source_connections`` row wins over
``data_source.bigquery.*`` instance config, and the process cache is keyed
on the resolved projects so a row UPDATE is visible on the very next call —
no explicit `cache_clear()` needed."""

from __future__ import annotations

import pytest


@pytest.fixture
def bq_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
    from connectors.bigquery.access import get_bq_access

    get_bq_access.cache_clear()
    yield
    get_bq_access.cache_clear()


class TestRowWinsOverInstanceConfig:
    def test_row_values_override_instance_config(self, bq_env, monkeypatch):
        """Failing-first per the plan: a row with DIFFERENT values than
        instance config must win — the resolver returns the ROW's values."""

        def fake_get_value(*keys, default=""):
            return {
                ("data_source", "bigquery", "project"): "yaml-project",
                ("data_source", "bigquery", "billing_project"): "yaml-billing",
            }.get(keys, default)

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)

        from src.repositories import source_connections_repo

        source_connections_repo().create(
            id="bq-row",
            name="bigquery",
            source_type="bigquery",
            config={"project": "row-project", "billing_project": "row-billing"},
            is_default=True,
        )

        from connectors.bigquery.access import get_bq_access

        bq = get_bq_access()
        assert bq.projects.data == "row-project"
        assert bq.projects.billing == "row-billing"

    def test_row_without_billing_project_falls_back_to_project(self, bq_env):
        from src.repositories import source_connections_repo

        source_connections_repo().create(
            id="bq-row2",
            name="bigquery",
            source_type="bigquery",
            config={"project": "solo-project"},
            is_default=True,
        )

        from connectors.bigquery.access import get_bq_access

        bq = get_bq_access()
        assert bq.projects.data == "solo-project"
        assert bq.projects.billing == "solo-project"


class TestZeroArgFallbackIsByteCompatible:
    def test_no_row_falls_back_to_instance_config(self, bq_env, monkeypatch):
        def fake_get_value(*keys, default=""):
            return {
                ("data_source", "bigquery", "project"): "yaml-only-project",
            }.get(keys, default)

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)

        from connectors.bigquery.access import get_bq_access

        bq = get_bq_access()
        assert bq.projects.data == "yaml-only-project"


class TestCacheInvalidatesOnRowUpdate:
    def test_updating_the_row_is_visible_on_next_call_without_explicit_clear(self, bq_env):
        from src.repositories import source_connections_repo
        from connectors.bigquery.access import get_bq_access

        repo = source_connections_repo()
        repo.create(
            id="bq-row3",
            name="bigquery",
            source_type="bigquery",
            config={"project": "before-project"},
            is_default=True,
        )
        bq1 = get_bq_access()
        assert bq1.projects.data == "before-project"

        repo.update("bq-row3", config={"project": "after-project"})

        bq2 = get_bq_access()
        assert bq2.projects.data == "after-project", (
            "get_bq_access must pick up a saved connection row on the very next "
            "call — this is what makes an admin save live across role-split "
            "processes without a restart"
        )
        assert bq2 is not bq1

    def test_repeat_calls_with_no_change_return_the_same_instance(self, bq_env):
        from src.repositories import source_connections_repo
        from connectors.bigquery.access import get_bq_access

        source_connections_repo().create(
            id="bq-row4",
            name="bigquery",
            source_type="bigquery",
            config={"project": "stable-project"},
            is_default=True,
        )
        a = get_bq_access()
        b = get_bq_access()
        assert a is b
