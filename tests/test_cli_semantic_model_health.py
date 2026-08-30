"""CLI tests for `agnes semantic-model health`.

Scoped to the ``orphaned_table_bindings`` section (Block 5 of #1707): the
CLI hardcodes one rendering block per health-report key
(``cli/commands/semantic_model.py::health``), so a new key added to the
server's JSON is silently swallowed unless the CLI also learns to print it —
this file pins that it does not.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


def _health_body(**overrides) -> dict:
    body = {
        "sources": [],
        "orphaned_models": [],
        "orphaned_table_bindings": [],
        "invalid_models": [],
        "metrics_missing_description": [],
        "duplicate_metric_names": [],
        "metrics_missing_relationships": [],
        "coverage_summary": {"missing_count": 0, "partial_count": 0},
        "mutes": [],
    }
    body.update(overrides)
    return body


def test_a_clean_report_says_nothing_is_wrong():
    with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, _health_body())):
        result = runner.invoke(app, ["semantic-model", "health"])
    assert result.exit_code == 0
    assert "No sync failures, disconnected models, or invalid documents." in result.output


def test_an_orphaned_metric_binding_is_reported_with_the_missing_table_name():
    body = _health_body(
        orphaned_table_bindings=[
            {"binding": "metric", "metric_id": "met1", "name": "revenue", "missing_tables": ["orders_gone"]}
        ]
    )
    with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, body)):
        result = runner.invoke(app, ["semantic-model", "health"])
    assert result.exit_code == 0
    assert "revenue" in result.output
    assert "orders_gone" in result.output
    # A clean report's headline must not also print for a genuinely dirty one.
    assert "No sync failures, disconnected models, or invalid documents." not in result.output


def test_orphaned_columns_are_reported_with_the_table_id_and_count():
    body = _health_body(
        orphaned_table_bindings=[{"binding": "column", "table_id": "orders_gone", "column_count": 12}]
    )
    with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, body)):
        result = runner.invoke(app, ["semantic-model", "health"])
    assert result.exit_code == 0
    assert "orders_gone" in result.output
    assert "12" in result.output


def test_json_flag_passes_the_key_through_verbatim():
    body = _health_body(
        orphaned_table_bindings=[{"binding": "column", "table_id": "orders_gone", "column_count": 3}]
    )
    with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, body)):
        result = runner.invoke(app, ["semantic-model", "health", "--json"])
    assert result.exit_code == 0
    assert "orphaned_table_bindings" in result.output
    assert "orders_gone" in result.output


class TestSourcesThatSyncedButImportedNothing:
    """#1707: the report already carried `owned_model_count`, but this
    renderer filtered `sources` on `last_sync_status == "error"` alone — so a
    source that synced fine and imported nothing produced no section at all
    AND still got the reassuring "No sync failures…" line."""

    @staticmethod
    def _source(source_id: str, *, status: str | None, owned: int | None) -> dict:
        return {
            "source_id": source_id,
            "name": source_id.title(),
            "last_sync_status": status,
            "last_sync_at": None,
            "last_sync_error": None,
            "owned_model_count": owned,
        }

    def _run(self, sources: list[dict]):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, _health_body(sources=sources))):
            return runner.invoke(app, ["semantic-model", "health"])

    def test_a_synced_source_owning_nothing_is_reported(self):
        result = self._run([self._source("warehouse", status="ok", owned=0)])
        assert result.exit_code == 0
        assert "Sources that synced but imported nothing (1):" in result.output
        assert "Warehouse" in result.output

    def test_it_suppresses_the_nothing_is_wrong_line(self):
        result = self._run([self._source("warehouse", status="ok", owned=0)])
        assert "No sync failures, disconnected models, or invalid documents." not in result.output

    def test_it_is_not_styled_as_an_error(self):
        """Nothing failed — the fetch worked. It must not read as a failure."""
        result = self._run([self._source("warehouse", status="ok", owned=0)])
        assert "Sync failures" not in result.output

    def test_a_source_owning_models_is_not_reported(self):
        result = self._run([self._source("warehouse", status="ok", owned=3)])
        assert "synced but imported nothing" not in result.output
        assert "No sync failures, disconnected models, or invalid documents." in result.output

    def test_a_never_synced_source_is_not_reported(self):
        result = self._run([self._source("fresh", status=None, owned=0)])
        assert "synced but imported nothing" not in result.output

    def test_a_failed_source_stays_in_the_sync_failures_section_only(self):
        result = self._run([self._source("broken", status="error", owned=0)])
        assert "Sync failures (1):" in result.output
        assert "synced but imported nothing" not in result.output

    def test_an_unknown_count_is_never_reported_as_empty(self):
        """`null` means "cannot say", not "owns nothing"."""
        result = self._run([self._source("murky", status="ok", owned=None)])
        assert "synced but imported nothing" not in result.output


class TestTheScanScopeOnTheEmptySourceFinding:
    """Finding A17 on #1707: the finding says a source imported nothing; the
    scope says where it looked, which is the half the admin can act on."""

    @staticmethod
    def _source(source_id: str, *, scope: str | None) -> dict:
        return {
            "source_id": source_id,
            "name": source_id.title(),
            "last_sync_status": "ok",
            "last_sync_at": None,
            "last_sync_error": None,
            "owned_model_count": 0,
            "scan_scope": scope,
        }

    def _run(self, sources: list[dict]):
        with patch("cli.commands.admin_semantic.api_get", return_value=_resp(200, _health_body(sources=sources))):
            return runner.invoke(app, ["semantic-model", "health"])

    def test_the_finding_names_what_was_scanned(self):
        result = self._run([self._source("warehouse", scope="ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE")])
        assert result.exit_code == 0
        assert "scanned ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE" in result.output

    def test_it_keeps_the_forward_hint(self):
        result = self._run([self._source("warehouse", scope="ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE")])
        assert "check this source's scope/config" in result.output

    def test_a_source_with_no_derivable_scope_still_reports_the_finding(self):
        result = self._run([self._source("warehouse", scope=None)])
        assert "Sources that synced but imported nothing (1):" in result.output
        assert "scanned" not in result.output
