"""A `query_mode='materialized'` row whose connector is unconfigured (missing
Snowflake/Databricks settings, or a Keboola connection credential error) must
still be visible in `sync_state` — not just in the in-memory `_run_sync`
summary.

Before this fix, `_run_materialized_pass`'s three "not configured" branches
(Keboola credential error, Databricks client init, Snowflake settings)
appended to `summary["errors"]` and `continue`d WITHOUT calling
`state.set_error(...)`, unlike every other failure branch in the same loop
(`MaterializeBudgetError`, the generic `except Exception`). A row stuck in
this state had no `sync_state` row at all, so `GET /api/admin/registry` /
`agnes admin list-tables` reported it as merely "never synced" with no
explanation, and an admin had no way to discover *why* a table registered as
`materialized` never produced data.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_state():
    class _State:
        def __init__(self):
            self.set_error_calls = []

        def get_last_sync(self, _id):
            return None

        def set_error(self, table_id, msg):
            self.set_error_calls.append((table_id, msg))

        def set_skipped(self, table_id, reason):
            pass

        def update_sync(self, **kw):
            pass

    return _State()


def _fake_registry(rows):
    class _Repo:
        def __init__(self, conn):
            pass

        def list_all(self):
            return rows

    return _Repo


def _row(source_type, **extra):
    return {
        "id": f"gold_{source_type}",
        "name": f"gold_{source_type}",
        "query_mode": "materialized",
        "source_type": source_type,
        "source_query": "SELECT 1",
        "sync_schedule": None,
        **extra,
    }


def test_snowflake_not_configured_records_sync_state_error(monkeypatch, fake_state, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    row = _row("snowflake")
    monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: _fake_registry([row])(None))
    monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: fake_state)

    from app.api.sync import _run_materialized_pass

    with patch("connectors.snowflake.settings.resolve_snowflake_settings", return_value=None):
        summary = _run_materialized_pass(MagicMock(), MagicMock())

    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["table"] == "gold_snowflake"
    assert fake_state.set_error_calls, "sync_state.set_error must record the unconfigured-connector failure"
    assert fake_state.set_error_calls[0][0] == "gold_snowflake"
    assert "Snowflake not configured" in fake_state.set_error_calls[0][1]


def test_databricks_not_configured_records_sync_state_error(monkeypatch, fake_state, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    row = _row("databricks")
    monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: _fake_registry([row])(None))
    monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: fake_state)

    from app.api.sync import _run_materialized_pass

    with patch("connectors.databricks.semantic_layer.resolve_databricks_settings", return_value=None):
        summary = _run_materialized_pass(MagicMock(), MagicMock())

    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["table"] == "gold_databricks"
    assert fake_state.set_error_calls, "sync_state.set_error must record the unconfigured-connector failure"
    assert fake_state.set_error_calls[0][0] == "gold_databricks"
    assert "Databricks not configured" in fake_state.set_error_calls[0][1]


def test_keboola_credential_error_records_sync_state_error(monkeypatch, fake_state, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    row = _row("keboola", connection_id="missing_conn", bucket="in.c-crm", source_table="orders")
    monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: _fake_registry([row])(None))
    monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: fake_state)

    from app.api.sync import _KeboolaCredentialError, _run_materialized_pass

    with patch(
        "app.api.sync._resolve_keboola_credentials",
        side_effect=_KeboolaCredentialError("connection_id 'missing_conn' not found in source_connections"),
    ):
        summary = _run_materialized_pass(MagicMock(), MagicMock())

    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["table"] == "gold_keboola"
    assert fake_state.set_error_calls, "sync_state.set_error must record the unconfigured-connector failure"
    assert fake_state.set_error_calls[0][0] == "gold_keboola"
    assert "not found in source_connections" in fake_state.set_error_calls[0][1]
