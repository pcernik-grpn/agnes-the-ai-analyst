"""Non-JSON error bodies must not crash CLI error paths.

Regression for `agnes admin config-surface --json` dying with a
JSONDecodeError when the server (or a proxy in front of it) answered an
error with an HTML page instead of FastAPI's ``{"detail": ...}`` JSON.
Every CLI error path now goes through ``cli.client.error_detail`` /
``error_detail_object``, which never raise.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from cli.client import error_detail, error_detail_object
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path / "local"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-cli-tests")
    (tmp_path / "config").mkdir()
    (tmp_path / "local").mkdir()
    (tmp_path / "data").mkdir()
    yield tmp_path

_HTML_403 = "<html><head><title>403 Forbidden</title></head><body>nginx</body></html>"


def _resp(status_code: int, *, content: bytes | str = b"", json_body=None) -> httpx.Response:
    if json_body is not None:
        return httpx.Response(status_code, json=json_body)
    if isinstance(content, str):
        content = content.encode()
    return httpx.Response(status_code, content=content)


class TestErrorDetail:
    def test_json_detail_string(self):
        assert error_detail(_resp(403, json_body={"detail": "admin only"})) == "admin only"

    def test_json_detail_dict_is_serialized(self):
        out = error_detail(_resp(409, json_body={"detail": {"error": "conflict", "expected": 2}}))
        assert json.loads(out) == {"error": "conflict", "expected": 2}

    def test_non_json_body_falls_back_to_text_and_status(self):
        out = error_detail(_resp(403, content=_HTML_403))
        assert out.startswith("HTTP 403")
        assert "403 Forbidden" in out

    def test_empty_body(self):
        assert error_detail(_resp(502)) == "HTTP 502"

    def test_json_without_detail_key(self):
        out = error_detail(_resp(500, json_body={"error": "boom"}))
        assert out.startswith("HTTP 500")
        assert "boom" in out

    def test_json_non_dict_body(self):
        # A bare JSON list/string has no .get() — must not raise.
        out = error_detail(_resp(500, json_body=["boom"]))
        assert out.startswith("HTTP 500")

    def test_long_body_is_truncated(self):
        out = error_detail(_resp(502, content="x" * 5000))
        assert len(out) < 400

    def test_never_raises_on_undecodable_bytes(self):
        out = error_detail(_resp(500, content=b"\xff\xfe\x00garbage"))
        assert out.startswith("HTTP 500")


class TestErrorDetailObject:
    def test_dict_detail_passthrough(self):
        detail = {"error": "version_conflict", "expected": 3, "actual": 4}
        assert error_detail_object(_resp(409, json_body={"detail": detail})) == detail

    def test_string_detail_passthrough(self):
        assert error_detail_object(_resp(409, json_body={"detail": "no_draft"})) == "no_draft"

    def test_non_json_body_returns_none(self):
        assert error_detail_object(_resp(502, content=_HTML_403)) is None

    def test_json_non_dict_body_returns_none(self):
        assert error_detail_object(_resp(409, json_body=["x"])) is None

    def test_missing_detail_key_returns_none(self):
        assert error_detail_object(_resp(500, json_body={"error": "boom"})) is None


class TestConfigSurfaceNonJsonError:
    """The originally reported crash: HTML 403 from the config-surface endpoint."""

    def test_config_surface_html_error_exits_cleanly(self):
        with patch("cli.commands.admin.api_get", return_value=_resp(403, content=_HTML_403)):
            result = runner.invoke(app, ["admin", "config-surface", "--json"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
        assert "Failed: HTTP 403" in combined
        assert "Traceback" not in combined

    def test_config_surface_json_error_still_shows_detail(self):
        with patch(
            "cli.commands.admin.api_get",
            return_value=_resp(403, json_body={"detail": "admin only"}),
        ):
            result = runner.invoke(app, ["admin", "config-surface", "--json"])
        assert result.exit_code == 1
        combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
        assert "Failed: admin only" in combined


class TestOtherCommandsNonJsonError:
    """Spot-check the audited call sites in other command modules."""

    def test_glossary_search_html_error(self):
        with patch("cli.commands.glossary.api_get", return_value=_resp(502, content=_HTML_403)):
            result = runner.invoke(app, ["glossary", "search", "revenue"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_tokens_create_html_error(self):
        with patch("cli.commands.tokens.api_post", return_value=_resp(502, content=_HTML_403)):
            result = runner.invoke(app, ["auth", "token", "create", "--name", "t"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_admin_news_publish_non_json_409(self):
        with patch("cli.commands.admin_news.api_post", return_value=_resp(409, content=_HTML_403)):
            result = runner.invoke(app, ["admin", "news", "publish"])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)


class TestMagicMockCompatibility:
    """Existing tests hand api_* a MagicMock whose .json() returns a dict —
    error_detail must keep working with those, not only httpx.Response."""

    def test_magicmock_response(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_resp.json.return_value = {"detail": "Internal Server Error"}
        assert error_detail(mock_resp) == "Internal Server Error"
