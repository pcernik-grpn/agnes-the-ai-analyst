"""CLI tests for `agnes semantic-model coverage` — the source-agnostic
semantic-layer coverage check (semantic-phase5, wave 1, Task 1)."""

from __future__ import annotations

import json
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


def test_no_uncovered_tables():
    with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, {"tables": []})):
        result = runner.invoke(app, ["semantic-model", "coverage"])
    assert result.exit_code == 0
    assert "semantic-layer coverage" in result.output


def test_lists_uncovered_tables():
    body = {"tables": [{"id": "lonely", "name": "lonely"}, {"id": "orphan", "name": "Orphan Table"}]}
    with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
        result = runner.invoke(app, ["semantic-model", "coverage"])
    assert result.exit_code == 0
    assert "lonely" in result.output
    assert "orphan" in result.output


def test_limit_truncates_and_says_so():
    body = {"tables": [{"id": f"t{i}", "name": f"t{i}"} for i in range(5)]}
    with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
        result = runner.invoke(app, ["semantic-model", "coverage", "--limit", "2"])
    assert result.exit_code == 0
    assert "t0" in result.output
    assert "t1" in result.output
    assert "t4" not in result.output
    assert "3 more" in result.output


def test_json_output():
    body = {"tables": [{"id": "lonely", "name": "lonely"}]}
    with patch("cli.commands.semantic_model.api_get", return_value=_resp(200, body)):
        result = runner.invoke(app, ["semantic-model", "coverage", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == body


def test_admin_only_error_surfaces():
    with patch(
        "cli.commands.semantic_model.api_get",
        return_value=_resp(403, {"detail": "Admin access required"}, text="Forbidden"),
    ):
        result = runner.invoke(app, ["semantic-model", "coverage"])
    assert result.exit_code == 1
    assert "Admin access required" in result.output
