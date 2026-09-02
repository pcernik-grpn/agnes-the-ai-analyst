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


class TestExtract:
    """`agnes admin sharepoint extract` — CLI counterpart to
    `POST /api/admin/sharepoint/connections/{connection_id}/extract`, the
    manual crawl trigger with its per-run options. Until this command
    existed the run options (`resync`, `force_reprocess`) were reachable
    only from the source card or a hand-written curl."""

    def test_bare_call_posts_no_body(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "e1", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1"])
        assert result.exit_code == 0, result.output
        assert "e1" in result.output
        args, kwargs = mock_post.call_args
        assert args[0] == "/api/admin/sharepoint/connections/conn1/extract"
        assert kwargs.get("json") is None

    def test_concurrency_and_timeout_ride_the_payload(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "e2", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(
                app,
                ["admin", "sharepoint", "extract", "conn1", "--concurrency", "2", "--timeout-s", "600"],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"concurrency": 2, "timeout_s": 600}

    def test_force_reprocess_rides_the_payload_as_true(self):
        """The "re-read every file in scope, ignoring the change cursor"
        option — the same key the source card's checkbox sends, so the two
        surfaces can never drift on what the run does."""
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "e3", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1", "--force-reprocess"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"force_reprocess": True}

    def test_resync_rides_the_payload_as_true(self):
        with patch(
            "cli.commands.admin_sharepoint.api_post", return_value=_resp(202, {"job_id": "e4", "status": "queued"})
        ) as mock_post:
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1", "--resync"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"resync": True}

    def test_json_output(self):
        body = {"job_id": "e5", "status": "queued"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(202, body)):
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == body

    def test_already_running_is_reported_and_exits_nonzero(self):
        detail = {"error": "extraction_already_running", "job_id": "e0"}
        with patch("cli.commands.admin_sharepoint.api_post", return_value=_resp(409, {"detail": detail})):
            result = runner.invoke(app, ["admin", "sharepoint", "extract", "conn1"])
        assert result.exit_code == 1
        assert "extraction_already_running" in result.output
