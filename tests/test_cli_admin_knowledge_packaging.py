"""CLI tests for `agnes admin knowledge packaging run|status` (TCRD-296 synthesis C.15)."""

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


class TestRun:
    def test_success_prints_status_and_job(self):
        body = {"status": "queued", "job_id": "job_abc123"}
        with patch("cli.commands.admin_knowledge_packaging.api_post", return_value=_resp(202, body)) as mock_post:
            result = runner.invoke(app, ["admin", "knowledge", "packaging", "run"])
        assert result.exit_code == 0, result.output
        assert "Status: queued" in result.output
        assert "Job:    job_abc123" in result.output
        mock_post.assert_called_once_with("/api/admin/run-knowledge-packaging")

    def test_success_json_output(self):
        body = {"status": "queued", "job_id": "job_xyz"}
        with patch("cli.commands.admin_knowledge_packaging.api_post", return_value=_resp(202, body)):
            result = runner.invoke(app, ["admin", "knowledge", "packaging", "run", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["job_id"] == "job_xyz"

    def test_already_in_progress_exits_1(self):
        with patch(
            "cli.commands.admin_knowledge_packaging.api_post",
            return_value=_resp(
                409,
                {"detail": {"error": "knowledge_packaging_already_in_progress", "job_id": "job_existing"}},
            ),
        ):
            result = runner.invoke(app, ["admin", "knowledge", "packaging", "run"])
        assert result.exit_code == 1
        assert "job_existing" in result.output

    def test_no_worker_role_typed_501_prints_and_exits_1(self):
        with patch(
            "cli.commands.admin_knowledge_packaging.api_post",
            return_value=_resp(
                501,
                {"detail": {"error": "requires_worker_role", "message": "knowledge packaging needs the worker role"}},
            ),
        ):
            result = runner.invoke(app, ["admin", "knowledge", "packaging", "run"])
        assert result.exit_code == 1
        assert "requires_worker_role" in result.output


class TestStatus:
    def test_no_runs_yet(self):
        body = {"last_run": None, "running": False, "next_due": None}
        with patch("cli.commands.admin_knowledge_packaging.api_get", return_value=_resp(200, body)) as mock_get:
            result = runner.invoke(app, ["admin", "knowledge", "packaging", "status"])
        assert result.exit_code == 0, result.output
        assert "Running:  False" in result.output
        assert "Last run: (none yet)" in result.output
        mock_get.assert_called_once_with("/api/admin/knowledge-packaging/status")

    def test_reports_last_run_summary(self):
        body = {
            "last_run": {
                "job_id": "job_1",
                "status": "completed",
                "created_at": "2026-09-01T00:00:00Z",
                "finished_at": "2026-09-01T00:05:00Z",
                "result": {
                    "built": ["col_a"],
                    "skipped": ["col_b"],
                    "pruned": [],
                    "errors": [],
                    "interrupted_reason": None,
                },
            },
            "running": False,
            "next_due": "2026-09-01T00:15:00Z",
        }
        with patch("cli.commands.admin_knowledge_packaging.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "knowledge", "packaging", "status"])
        assert result.exit_code == 0, result.output
        assert "job_1" in result.output
        assert "built=1 skipped=1 pruned=0 errors=0" in result.output
        assert "2026-09-01T00:15:00Z" in result.output

    def test_json_output(self):
        body = {"last_run": None, "running": True, "next_due": None}
        with patch("cli.commands.admin_knowledge_packaging.api_get", return_value=_resp(200, body)):
            result = runner.invoke(app, ["admin", "knowledge", "packaging", "status", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["running"] is True
