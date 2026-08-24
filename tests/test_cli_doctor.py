"""CLI tests for `agnes doctor` — the support-bundle command.

The command's contract: ALWAYS produce the artifact (one redacted Markdown
file), degrade loudly instead of partially — a missing server section is
replaced by an explicit reason, never silently omitted — and never let a
token value reach the output.
"""

import json
import re
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()

SENTINEL_TOKEN = "pat_SENTINEL_NEVER_IN_BUNDLE_123456"

SUPPORT_PAYLOAD = {
    "generated_at": "2026-08-23T10:00:00+00:00",
    "build": {
        "status": "ok",
        "version": "1.2.3",
        "package_version": "1.2.3",
        "channel": "stable",
        "image_tag": "stable",
        "commit_sha": "abc1234",
        "deployed_at": "2026-08-20T00:00:00+00:00",
    },
    "schema": {"status": "ok", "backend": "duckdb", "db_schema": "ok", "current": 1, "expected": 1},
    "retrieval": {
        "status": "warning",
        "mode": "lexical_only",
        "detail": "semantic scoring is INACTIVE",
    },
    "sync": {
        "status": "warning",
        "sources": {
            "keboola": {
                "tables": 2,
                "ok": 1,
                "errors": 1,
                "stale": 0,
                "never_synced": 0,
                "last_sync_max": "2026-08-23T01:00:00+00:00",
                "last_errors": [
                    {
                        "table_id": "events",
                        "error": "extract exploded",
                        "last_sync": "2026-08-22T01:00:00+00:00",
                    }
                ],
            }
        },
    },
    "disk": {
        "status": "ok",
        "data_dir": "/data",
        "total_bytes": 100_000_000_000,
        "used_bytes": 40_000_000_000,
        "free_bytes": 60_000_000_000,
        "system_db_bytes": 1_000_000,
        "analytics_db_bytes": 2_000_000,
    },
    "process": {"status": "ok", "state_backend": "duckdb", "roles": ["api"], "python": "3.12.0"},
    "secrets": {"ANTHROPIC_API_KEY": True, "SENDGRID_API_KEY": False},
}


@pytest.fixture(autouse=True)
def tmp_env(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("AGNES_TOKEN", raising=False)
    monkeypatch.delenv("AGNES_SESSION_ID", raising=False)
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    (config_dir / "token.json").write_text(json.dumps({"access_token": SENTINEL_TOKEN, "email": "user@example.com"}))
    yield tmp_path


def _resp(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = json.dumps(json_data or {})
    return r


def _route(support_status=200):
    def side_effect(path, **kwargs):
        if path == "/api/health/detailed":
            return _resp(200, {"status": "healthy", "caller_role": "admin"})
        if path == "/api/admin/doctor/support":
            if support_status == 200:
                return _resp(200, SUPPORT_PAYLOAD)
            return _resp(support_status, {"detail": "Admin access required"})
        return _resp(404, {})

    return side_effect


def _bundle_file(tmp_path):
    files = sorted(tmp_path.glob("agnes-doctor-*.md"))
    assert files, f"no bundle written in {tmp_path}: {list(tmp_path.iterdir())}"
    return files[-1]


class TestBundleFile:
    def test_writes_bundle_with_both_sections(self, tmp_env):
        with patch("cli.commands.doctor.api_get", side_effect=_route()):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        content = _bundle_file(tmp_env).read_text()
        assert "## Client" in content
        assert "## Server" in content
        # Degradations must be loud in the rendered bundle too.
        assert "lexical_only" in content
        assert "extract exploded" in content
        # The command tells the user where the artifact landed.
        assert "agnes-doctor-" in result.output

    def test_output_flag_controls_destination(self, tmp_env):
        target = tmp_env / "bundle.md"
        with patch("cli.commands.doctor.api_get", side_effect=_route()):
            result = runner.invoke(app, ["doctor", "--output", str(target)])
        assert result.exit_code == 0, result.output
        assert target.exists()

    def test_json_mode_prints_structured_bundle(self, tmp_env):
        with patch("cli.commands.doctor.api_get", side_effect=_route()):
            result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "client" in data and "server" in data
        assert data["server"]["retrieval"]["mode"] == "lexical_only"
        # JSON mode prints; it must not also litter the cwd with a file.
        assert not list(tmp_env.glob("agnes-doctor-*.md"))


class TestDegradedServerSection:
    def test_non_admin_gets_explicit_reason_not_silence(self, tmp_env):
        with patch("cli.commands.doctor.api_get", side_effect=_route(support_status=403)):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        content = _bundle_file(tmp_env).read_text()
        assert "## Server" in content
        assert "admin" in content.lower()

    def test_unreachable_server_still_writes_client_section(self, tmp_env):
        with patch(
            "cli.commands.doctor.api_get",
            side_effect=ConnectionError("connection refused"),
        ):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        content = _bundle_file(tmp_env).read_text()
        assert "## Client" in content
        assert "unavailable" in content.lower()
        assert "connection refused" in content

    def test_missing_token_is_reported_not_fatal(self, tmp_env):
        (tmp_env / "config" / "token.json").unlink()
        with patch("cli.commands.doctor.api_get", side_effect=_route()):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        content = _bundle_file(tmp_env).read_text()
        assert "not logged in" in content.lower()


class TestRedaction:
    def test_token_value_never_reaches_the_bundle(self, tmp_env):
        (tmp_env / "config" / "last-error.log").write_text(
            f"Traceback ...\nAuthorization: Bearer {SENTINEL_TOKEN}\nGET /api/pull?token={SENTINEL_TOKEN} failed\n"
        )
        with patch("cli.commands.doctor.api_get", side_effect=_route()):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        content = _bundle_file(tmp_env).read_text()
        assert SENTINEL_TOKEN not in content
        assert "<redacted>" in content
        # The error-log tail itself is still there — only secrets go.
        assert "Traceback" in content

    def test_credentials_in_a_connection_url_are_scrubbed(self, tmp_env):
        """A connector's own error text is the sneakiest way a secret travels.

        Upstream failures routinely quote the connection URL, which carries
        the password in the userinfo position — a shape none of the
        header/query-param patterns match. The bundle exists to be pasted
        into a support channel, so this has to be caught.
        """
        from cli.commands.doctor import _scrub

        scrubbed = _scrub("connection failed: postgres://agnes:s3cr3tpw@10.0.0.1:5432/db", ())
        assert "s3cr3tpw" not in scrubbed
        # The rest of the URL must survive — it is the diagnostic content.
        assert "10.0.0.1:5432" in scrubbed
        assert "agnes" in scrubbed

    def test_server_side_sync_errors_are_scrubbed_before_rendering(self, tmp_env):
        payload = json.loads(json.dumps(SUPPORT_PAYLOAD))
        payload["sync"]["sources"]["keboola"]["last_errors"][0]["error"] = (
            "snowflake://svc:Passw0rd123@acct.snowflakecomputing.com refused"
        )

        def route(path, **kwargs):
            if path == "/api/health/detailed":
                return _resp(200, {"status": "healthy", "caller_role": "admin"})
            if path == "/api/admin/doctor/support":
                return _resp(200, payload)
            return _resp(404, {})

        with patch("cli.commands.doctor.api_get", side_effect=route):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        content = _bundle_file(tmp_env).read_text()
        assert "Passw0rd123" not in content
        assert "acct.snowflakecomputing.com" in content

    def test_json_mode_is_scrubbed_too(self, tmp_env):
        (tmp_env / "config" / "last-error.log").write_text(f"Bearer {SENTINEL_TOKEN}\n")
        with patch("cli.commands.doctor.api_get", side_effect=_route()):
            result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0, result.output
        assert SENTINEL_TOKEN not in result.output


class TestBundleFilename:
    def test_default_name_is_timestamped(self, tmp_env):
        with patch("cli.commands.doctor.api_get", side_effect=_route()):
            runner.invoke(app, ["doctor"])
        name = _bundle_file(tmp_env).name
        assert re.fullmatch(r"agnes-doctor-\d{8}-\d{6}Z\.md", name), name
