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


_FLEET_BODY = {
    "connections": [
        {
            "connection_id": "conn_a",
            "connection_name": "corp-sharepoint",
            "run": {
                "outcome": "running",
                "phase": "facts",
                "files_done": 900,
                "files_seen": 900,
                "error": None,
                "usage": {"facts": {"input_tokens": 1000, "output_tokens": 200, "estimated_cost_usd": 0.5}},
            },
            "files_per_min": 12.5,
            "checkpoint_age_s": 45.0,
            "stuck": False,
            "facts": {"phase_active": True, "docs_done": 12, "docs_total": 340},
            "estimated_cost_usd": 0.5,
        }
    ],
    "totals": {
        "connections": 1,
        "active": 1,
        "stuck": 0,
        "files_done": 900,
        "files_seen": 900,
        "files_per_min": 12.5,
        "facts_docs_done": 12,
        "facts_docs_total": 340,
        "estimated_cost_usd": 0.5,
    },
    "as_of": "2026-09-02T12:00:00+00:00",
}


class TestRuns:
    """`agnes admin sharepoint runs` — CLI counterpart to
    `GET /api/admin/sharepoint/extraction/runs`."""

    def test_bare_call_uses_the_active_scope(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, _FLEET_BODY)) as mock_get:
            result = runner.invoke(app, ["admin", "sharepoint", "runs"])
        assert result.exit_code == 0, result.output
        mock_get.assert_called_once_with("/api/admin/sharepoint/extraction/runs?active=1")
        assert "corp-sharepoint" in result.output
        assert "Totals" in result.output

    def test_all_flag_broadens_the_scope(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, _FLEET_BODY)) as mock_get:
            result = runner.invoke(app, ["admin", "sharepoint", "runs", "--all"])
        assert result.exit_code == 0, result.output
        mock_get.assert_called_once_with("/api/admin/sharepoint/extraction/runs?all=1")

    def test_json_output_is_the_raw_body(self):
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, _FLEET_BODY)):
            result = runner.invoke(app, ["admin", "sharepoint", "runs", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == _FLEET_BODY

    def test_a_typed_501_is_reported_and_exits_nonzero(self):
        body = {"detail": "extraction_runs requires postgres", "error": "requires_postgres_backend"}
        with patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(501, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "runs"])
        assert result.exit_code == 1
        assert "requires postgres" in result.output

    def test_watch_stops_cleanly_on_keyboard_interrupt(self):
        """`--watch` loops until Ctrl-C — the second sleep() call raises to
        simulate the interrupt, and the command must exit 0, not crash."""
        with (
            patch("cli.commands.admin_sharepoint.api_get", return_value=_resp(200, _FLEET_BODY)) as mock_get,
            patch("cli.commands.admin_sharepoint.time.sleep", side_effect=[None, KeyboardInterrupt()]),
        ):
            result = runner.invoke(app, ["admin", "sharepoint", "runs", "--watch"])
        assert result.exit_code == 0, result.output
        assert mock_get.call_count == 2
