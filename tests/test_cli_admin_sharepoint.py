"""CLI tests for `agnes admin sharepoint` subcommands."""

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


class TestFactsExtract:
    """`agnes admin sharepoint facts-extract` — CLI counterpart to
    `POST /api/admin/sharepoint/connections/{connection_id}/facts-extract`."""

    def test_bare_call_posts_no_body(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "j1", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "facts-extract", "conn1"])
        assert result.exit_code == 0, result.output
        assert "j1" in result.output
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/facts-extract"
        assert kwargs.get("json") is None

    def test_doc_id_is_repeatable_and_rides_the_payload(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "j2", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(
                app,
                [
                    "admin",
                    "sharepoint",
                    "facts-extract",
                    "conn1",
                    "--doc-id",
                    "d1",
                    "--doc-id",
                    "d2",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"doc_ids": ["d1", "d2"]}

    def test_timeout_s_rides_the_payload(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "j3", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "facts-extract", "conn1", "--timeout-s", "120"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"timeout_s": 120}

    def test_json_output(self):
        body = {"job_id": "j4", "status": "queued"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(202, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-extract", "conn1", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_typed_error_is_reported_and_exits_nonzero(self):
        detail = {"error": "facts_extraction_disabled", "message": "extraction.facts.enabled is off"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(409, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-extract", "conn1"])
        assert result.exit_code == 1
        assert "extraction.facts.enabled is off" in result.output

    def test_a_plain_string_error_is_reported(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post",
            return_value=_resp(404, {"detail": "connection_not_found"}),
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-extract", "does-not-exist"])
        assert result.exit_code == 1
        assert "connection_not_found" in result.output


class TestFactsConfig:
    """`agnes admin sharepoint facts-config` — CLI counterpart to
    `PATCH /api/admin/sharepoint/connections/{connection_id}/extraction/facts-config`."""

    def test_retry_mode_patches_the_connection(self):
        body = {"connection_id": "conn1", "retry_mode": {"value": "always", "source": "connection"}}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch:
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--retry-mode", "always"])
        assert result.exit_code == 0, result.output
        assert "always" in result.output and "connection" in result.output
        args, kwargs = mock_patch.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/extraction/facts-config"
        assert kwargs["json"] == {"retry_mode": "always"}

    def test_clear_sends_a_null_retry_mode(self):
        body = {"connection_id": "conn1", "retry_mode": {"value": "on_gate_fail", "source": "instance"}}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)) as mock_patch:
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--clear"])
        assert result.exit_code == 0, result.output
        assert "instance" in result.output
        _, kwargs = mock_patch.call_args
        assert kwargs["json"] == {"retry_mode": None}

    def test_neither_flag_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1"])
        assert result.exit_code == 1
        assert "--retry-mode or --clear" in result.output

    def test_both_flags_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--retry-mode", "off", "--clear"])
        assert result.exit_code == 1
        assert "not both" in result.output

    def test_an_invalid_retry_mode_is_a_usage_error(self):
        result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--retry-mode", "sometimes"])
        assert result.exit_code == 1
        assert "must be one of" in result.output

    def test_json_output(self):
        body = {"connection_id": "conn1", "retry_mode": {"value": "off", "source": "connection"}}
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(200, body)):
            result = runner.invoke(
                app, ["admin", "sharepoint", "facts-config", "conn1", "--retry-mode", "off", "--json"]
            )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_a_server_side_422_is_reported(self):
        """The CLI validates locally against `_RETRY_MODES` before calling
        out, but the server's own 422 (a different value than the CLI
        would ever send today) must still surface cleanly rather than a
        bare traceback or a silent success."""
        detail = "retry_mode must be one of ['always', 'on_gate_fail', 'off'] or null (to clear the override)"
        with patch("cli.commands.admin_sharepoint.api_patch", return_value=_resp(422, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "facts-config", "conn1", "--retry-mode", "off"])
        assert result.exit_code == 1
        assert "must be one of" in result.output

    def test_a_404_is_reported(self):
        with patch(
            "cli.commands.admin_sharepoint.api_patch",
            return_value=_resp(404, {"detail": "connection_not_found"}),
        ):
            result = runner.invoke(
                app, ["admin", "sharepoint", "facts-config", "does-not-exist", "--retry-mode", "off"]
            )
        assert result.exit_code == 1
        assert "connection_not_found" in result.output
