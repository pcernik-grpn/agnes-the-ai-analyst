"""Tests for `agnes admin config export` / `agnes admin config apply`
(Track D3) — the CLI wrapper around GET/POST /api/admin/server-config
(-overlay).

``export`` dumps GET /api/admin/server-config/overlay's ``sections`` as
deterministic YAML. ``apply`` reads a YAML file, filters it against the
server's own ``editable_sections``/``secret_key_patterns`` (fetched from GET
/api/admin/server-config — never a client-side copy of the allowlist), and
POSTs the survivors through the SAME validated path the admin UI uses
(POST /api/admin/server-config) — so section allowlisting, deep-merge,
danger-zone confirmation, and audit logging all apply unchanged. The server
is always the final gate; client-side filtering here is defense-in-depth so
one stray key doesn't block an otherwise-valid apply.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml
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


_SERVER_CONFIG_META = {
    "sections": {},
    "editable_sections": ["instance", "theme", "email", "auth", "data_source"],
    "danger_sections": ["auth", "server"],
    "secret_key_patterns": ["secret", "token", "password", "passwd", "api_key", "private", "credential", "dsn"],
}


class TestExport:
    def test_export_dumps_yaml_to_stdout(self):
        overlay = {"sections": {"instance": {"name": "Acme"}}, "editable_sections": ["instance"]}
        with patch("cli.commands.admin_config.api_get", return_value=_resp(200, overlay)) as get:
            result = runner.invoke(app, ["admin", "config", "export"])
        assert result.exit_code == 0, result.output
        get.assert_called_once_with("/api/admin/server-config/overlay")
        loaded = yaml.safe_load(result.output)
        assert loaded == {"instance": {"name": "Acme"}}

    def test_export_json_flag(self):
        overlay = {"sections": {"instance": {"name": "Acme"}}, "editable_sections": ["instance"]}
        with patch("cli.commands.admin_config.api_get", return_value=_resp(200, overlay)):
            result = runner.invoke(app, ["admin", "config", "export", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"instance": {"name": "Acme"}}

    def test_export_writes_to_file(self, tmp_path):
        overlay = {"sections": {"instance": {"name": "Acme"}}, "editable_sections": ["instance"]}
        out_file = tmp_path / "overlay.yaml"
        with patch("cli.commands.admin_config.api_get", return_value=_resp(200, overlay)):
            result = runner.invoke(app, ["admin", "config", "export", "--out", str(out_file)])
        assert result.exit_code == 0, result.output
        assert yaml.safe_load(out_file.read_text()) == {"instance": {"name": "Acme"}}

    def test_export_deterministic_key_order(self):
        # Insertion order deliberately NOT alphabetical.
        overlay = {
            "sections": {
                "theme": {"z_field": 1, "a_field": 2},
                "instance": {"name": "Acme"},
            },
            "editable_sections": ["instance", "theme"],
        }
        with patch("cli.commands.admin_config.api_get", return_value=_resp(200, overlay)):
            first = runner.invoke(app, ["admin", "config", "export"]).output
            second = runner.invoke(app, ["admin", "config", "export"]).output
        assert first == second
        # Top-level sections sorted alphabetically: instance before theme.
        assert first.index("instance:") < first.index("theme:")
        # Nested keys sorted too: a_field before z_field.
        assert first.index("a_field:") < first.index("z_field:")

    def test_export_error_surfaces_server_detail(self):
        with patch(
            "cli.commands.admin_config.api_get",
            return_value=_resp(403, {"detail": "not admin"}),
        ):
            result = runner.invoke(app, ["admin", "config", "export"])
        assert result.exit_code != 0
        assert "not admin" in result.output


class TestApplyFileHandling:
    def test_apply_nonexistent_file_errors_without_network_call(self, tmp_path):
        missing = tmp_path / "does-not-exist.yaml"
        with patch("cli.commands.admin_config.api_get") as get, patch("cli.commands.admin_config.api_post") as post:
            result = runner.invoke(app, ["admin", "config", "apply", str(missing)])
        assert result.exit_code != 0
        get.assert_not_called()
        post.assert_not_called()

    def test_apply_invalid_yaml_errors(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("instance: [unclosed")
        with patch("cli.commands.admin_config.api_get") as get:
            result = runner.invoke(app, ["admin", "config", "apply", str(bad)])
        assert result.exit_code != 0
        get.assert_not_called()

    def test_apply_rejects_non_mapping_top_level(self, tmp_path):
        f = tmp_path / "list.yaml"
        f.write_text(yaml.dump(["not", "a", "mapping"]))
        with patch("cli.commands.admin_config.api_get") as get:
            result = runner.invoke(app, ["admin", "config", "apply", str(f)])
        assert result.exit_code != 0
        get.assert_not_called()


class TestApplyDryRun:
    def test_dry_run_shows_diff_and_writes_nothing(self, tmp_path):
        f = tmp_path / "overlay.yaml"
        f.write_text(yaml.dump({"instance": {"name": "New Name"}}))
        current_overlay = {"sections": {"instance": {"name": "Old Name"}}, "editable_sections": ["instance"]}

        def get_side_effect(path, **kwargs):
            if path == "/api/admin/server-config":
                return _resp(200, _SERVER_CONFIG_META)
            if path == "/api/admin/server-config/overlay":
                return _resp(200, current_overlay)
            raise AssertionError(f"unexpected GET {path}")

        with (
            patch("cli.commands.admin_config.api_get", side_effect=get_side_effect),
            patch("cli.commands.admin_config.api_post") as post,
        ):
            result = runner.invoke(app, ["admin", "config", "apply", str(f), "--dry-run"])
        assert result.exit_code == 0, result.output
        post.assert_not_called()
        assert "Old Name" in result.output
        assert "New Name" in result.output

    def test_dry_run_no_changes(self, tmp_path):
        f = tmp_path / "overlay.yaml"
        f.write_text(yaml.dump({"instance": {"name": "Same"}}))
        current_overlay = {"sections": {"instance": {"name": "Same"}}, "editable_sections": ["instance"]}

        def get_side_effect(path, **kwargs):
            if path == "/api/admin/server-config":
                return _resp(200, _SERVER_CONFIG_META)
            if path == "/api/admin/server-config/overlay":
                return _resp(200, current_overlay)
            raise AssertionError(f"unexpected GET {path}")

        with (
            patch("cli.commands.admin_config.api_get", side_effect=get_side_effect),
            patch("cli.commands.admin_config.api_post") as post,
        ):
            result = runner.invoke(app, ["admin", "config", "apply", str(f), "--dry-run"])
        assert result.exit_code == 0, result.output
        post.assert_not_called()
        assert "no changes" in result.output.lower()

    def test_dry_run_json_output(self, tmp_path):
        f = tmp_path / "overlay.yaml"
        f.write_text(yaml.dump({"instance": {"name": "New Name"}}))
        current_overlay = {"sections": {"instance": {"name": "Old Name"}}, "editable_sections": ["instance"]}

        def get_side_effect(path, **kwargs):
            if path == "/api/admin/server-config":
                return _resp(200, _SERVER_CONFIG_META)
            if path == "/api/admin/server-config/overlay":
                return _resp(200, current_overlay)
            raise AssertionError(f"unexpected GET {path}")

        with (
            patch("cli.commands.admin_config.api_get", side_effect=get_side_effect),
            patch("cli.commands.admin_config.api_post"),
        ):
            result = runner.invoke(app, ["admin", "config", "apply", str(f), "--dry-run", "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert isinstance(rows, list)
        assert any(row.get("before") == "Old Name" and row.get("after") == "New Name" for row in rows)


class TestApplyPosts:
    def test_apply_posts_filtered_sections(self, tmp_path):
        f = tmp_path / "overlay.yaml"
        f.write_text(yaml.dump({"instance": {"name": "New Name"}}))
        current_overlay = {"sections": {}, "editable_sections": ["instance"]}

        def get_side_effect(path, **kwargs):
            if path == "/api/admin/server-config":
                return _resp(200, _SERVER_CONFIG_META)
            if path == "/api/admin/server-config/overlay":
                return _resp(200, current_overlay)
            raise AssertionError(f"unexpected GET {path}")

        with (
            patch("cli.commands.admin_config.api_get", side_effect=get_side_effect),
            patch(
                "cli.commands.admin_config.api_post",
                return_value=_resp(200, {"restart_required": False, "sections_effect": {}}),
            ) as post,
        ):
            result = runner.invoke(app, ["admin", "config", "apply", str(f)])
        assert result.exit_code == 0, result.output
        post.assert_called_once()
        call_args, call_kwargs = post.call_args
        assert call_args[0] == "/api/admin/server-config"
        assert call_kwargs["json"]["sections"] == {"instance": {"name": "New Name"}}

    def test_apply_filters_unknown_section(self, tmp_path):
        f = tmp_path / "overlay.yaml"
        f.write_text(
            yaml.dump(
                {
                    "instance": {"name": "New Name"},
                    "totally_bogus_section": {"foo": "bar"},
                }
            )
        )
        current_overlay = {"sections": {}, "editable_sections": ["instance"]}

        def get_side_effect(path, **kwargs):
            if path == "/api/admin/server-config":
                return _resp(200, _SERVER_CONFIG_META)
            if path == "/api/admin/server-config/overlay":
                return _resp(200, current_overlay)
            raise AssertionError(f"unexpected GET {path}")

        with (
            patch("cli.commands.admin_config.api_get", side_effect=get_side_effect),
            patch(
                "cli.commands.admin_config.api_post",
                return_value=_resp(200, {"restart_required": False, "sections_effect": {}}),
            ) as post,
        ):
            result = runner.invoke(app, ["admin", "config", "apply", str(f)])
        assert result.exit_code == 0, result.output
        assert "totally_bogus_section" in result.output
        posted_sections = post.call_args.kwargs["json"]["sections"]
        assert "totally_bogus_section" not in posted_sections
        assert posted_sections == {"instance": {"name": "New Name"}}

    def test_apply_strips_literal_secret_keeps_env_name_and_env_ref(self, tmp_path):
        f = tmp_path / "overlay.yaml"
        f.write_text(
            yaml.dump(
                {
                    "email": {
                        "smtp_host": "smtp.example.com",
                        "smtp_password": "literal-cleartext-secret",
                        "token_env": "SOME_TOKEN_ENV",
                        "smtp_password_ref": "${SMTP_PASSWORD}",
                    }
                }
            )
        )
        meta = dict(_SERVER_CONFIG_META, editable_sections=["email"])
        current_overlay = {"sections": {}, "editable_sections": ["email"]}

        def get_side_effect(path, **kwargs):
            if path == "/api/admin/server-config":
                return _resp(200, meta)
            if path == "/api/admin/server-config/overlay":
                return _resp(200, current_overlay)
            raise AssertionError(f"unexpected GET {path}")

        with (
            patch("cli.commands.admin_config.api_get", side_effect=get_side_effect),
            patch(
                "cli.commands.admin_config.api_post",
                return_value=_resp(200, {"restart_required": False, "sections_effect": {}}),
            ) as post,
        ):
            result = runner.invoke(app, ["admin", "config", "apply", str(f)])
        assert result.exit_code == 0, result.output
        posted_email = post.call_args.kwargs["json"]["sections"]["email"]
        assert posted_email["smtp_host"] == "smtp.example.com"
        assert "smtp_password" not in posted_email
        assert posted_email["token_env"] == "SOME_TOKEN_ENV"
        assert posted_email["smtp_password_ref"] == "${SMTP_PASSWORD}"
        assert "smtp_password" in result.output  # warned about the stripped field

    def test_apply_keeps_boolean_field_with_secret_shaped_name(self, tmp_path):
        """Regression: `mcp.allow_query_param_token` (and similar switches)
        have "token"/"secret" substrings in their name by naming coincidence
        — a boolean cannot itself be a credential, so it must survive the
        client-side scrub (mirrors the server's own
        `_declared_boolean_fields()` guard, Devin Review on #1183)."""
        f = tmp_path / "overlay.yaml"
        f.write_text(yaml.dump({"mcp": {"allow_query_param_token": True}}))
        meta = dict(_SERVER_CONFIG_META, editable_sections=["mcp"])
        current_overlay = {"sections": {}, "editable_sections": ["mcp"]}

        def get_side_effect(path, **kwargs):
            if path == "/api/admin/server-config":
                return _resp(200, meta)
            if path == "/api/admin/server-config/overlay":
                return _resp(200, current_overlay)
            raise AssertionError(f"unexpected GET {path}")

        with (
            patch("cli.commands.admin_config.api_get", side_effect=get_side_effect),
            patch(
                "cli.commands.admin_config.api_post",
                return_value=_resp(200, {"restart_required": False, "sections_effect": {}}),
            ) as post,
        ):
            result = runner.invoke(app, ["admin", "config", "apply", str(f)])
        assert result.exit_code == 0, result.output
        posted_mcp = post.call_args.kwargs["json"]["sections"]["mcp"]
        assert posted_mcp == {"allow_query_param_token": True}

    def test_apply_danger_section_requires_confirm_danger(self, tmp_path):
        f = tmp_path / "overlay.yaml"
        f.write_text(yaml.dump({"auth": {"allowed_domain": "example.com"}}))
        meta = dict(_SERVER_CONFIG_META, editable_sections=["auth"])
        current_overlay = {"sections": {}, "editable_sections": ["auth"]}

        def get_side_effect(path, **kwargs):
            if path == "/api/admin/server-config":
                return _resp(200, meta)
            if path == "/api/admin/server-config/overlay":
                return _resp(200, current_overlay)
            raise AssertionError(f"unexpected GET {path}")

        with (
            patch("cli.commands.admin_config.api_get", side_effect=get_side_effect),
            patch(
                "cli.commands.admin_config.api_post",
                return_value=_resp(400, {"detail": "section(s) auth require confirm_danger=true"}),
            ) as post,
        ):
            result = runner.invoke(app, ["admin", "config", "apply", str(f)])
        assert result.exit_code != 0
        assert "confirm_danger" in result.output
        assert post.call_args.kwargs["json"]["confirm_danger"] is False

    def test_apply_confirm_danger_flag_passed_through(self, tmp_path):
        f = tmp_path / "overlay.yaml"
        f.write_text(yaml.dump({"auth": {"allowed_domain": "example.com"}}))
        meta = dict(_SERVER_CONFIG_META, editable_sections=["auth"])
        current_overlay = {"sections": {}, "editable_sections": ["auth"]}

        def get_side_effect(path, **kwargs):
            if path == "/api/admin/server-config":
                return _resp(200, meta)
            if path == "/api/admin/server-config/overlay":
                return _resp(200, current_overlay)
            raise AssertionError(f"unexpected GET {path}")

        with (
            patch("cli.commands.admin_config.api_get", side_effect=get_side_effect),
            patch(
                "cli.commands.admin_config.api_post",
                return_value=_resp(200, {"restart_required": True, "sections_effect": {"auth": "restart"}}),
            ) as post,
        ):
            result = runner.invoke(app, ["admin", "config", "apply", str(f), "--confirm-danger"])
        assert result.exit_code == 0, result.output
        assert post.call_args.kwargs["json"]["confirm_danger"] is True
