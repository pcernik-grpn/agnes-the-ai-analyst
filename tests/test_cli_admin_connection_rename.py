"""Tests for `agnes admin connection rename` (source-card redesign §8, CLI
counterpart to the overflow menu's `Rename…` item).

`rename` is a thin `PUT /api/admin/source-connections/{id} {"name": ...}` —
the update endpoint already carries forward config/scopes/extraction state
when the request omits those keys, so a rename body carries `name` alone.
"""

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


class TestRename:
    def test_renames_via_a_name_only_put(self):
        with patch(
            "cli.commands.admin_connection.api_put",
            return_value=_resp(200, {"id": "CONN", "name": "New Name"}),
        ) as put:
            result = runner.invoke(app, ["admin", "connection", "rename", "CONN", "New Name"])
        assert result.exit_code == 0, result.output
        put.assert_called_once_with(
            "/api/admin/source-connections/CONN",
            json={"name": "New Name"},
        )
        assert "New Name" in result.output

    def test_name_taken_by_another_connection_is_409(self):
        with patch(
            "cli.commands.admin_connection.api_put",
            return_value=_resp(409, {"detail": "name_taken"}),
        ):
            result = runner.invoke(app, ["admin", "connection", "rename", "CONN", "Taken"])
        assert result.exit_code == 1
        assert "name_taken" in result.output

    def test_unknown_connection_is_reported_and_exits_nonzero(self):
        with patch(
            "cli.commands.admin_connection.api_put",
            return_value=_resp(404, {"detail": "connection_not_found"}),
        ):
            result = runner.invoke(app, ["admin", "connection", "rename", "does-not-exist", "New Name"])
        assert result.exit_code == 1
        assert "connection_not_found" in result.output

    def test_missing_name_argument_fails_argument_parsing(self):
        result = runner.invoke(app, ["admin", "connection", "rename", "CONN"])
        assert result.exit_code != 0
