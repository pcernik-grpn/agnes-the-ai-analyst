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
    with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, _health_body())):
        result = runner.invoke(app, ["semantic-model", "health"])
    assert result.exit_code == 0
    assert "No sync failures, disconnected models, or invalid documents." in result.output


def test_an_orphaned_metric_binding_is_reported_with_the_missing_table_name():
    body = _health_body(
        orphaned_table_bindings=[
            {"binding": "metric", "metric_id": "met1", "name": "revenue", "missing_tables": ["orders_gone"]}
        ]
    )
    with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
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
    with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
        result = runner.invoke(app, ["semantic-model", "health"])
    assert result.exit_code == 0
    assert "orders_gone" in result.output
    assert "12" in result.output


def test_json_flag_passes_the_key_through_verbatim():
    body = _health_body(
        orphaned_table_bindings=[{"binding": "column", "table_id": "orders_gone", "column_count": 3}]
    )
    with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
        result = runner.invoke(app, ["semantic-model", "health", "--json"])
    assert result.exit_code == 0
    assert "orphaned_table_bindings" in result.output
    assert "orders_gone" in result.output
