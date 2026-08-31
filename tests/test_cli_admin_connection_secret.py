"""Tests for `agnes admin connection secret` (Task 4 of the semantic-layer
source-connections work).

The token is NEVER accepted as an argv option — it must come from a hidden
interactive prompt (`typer.prompt("Token", hide_input=True)`), mirroring the
security rule enforced elsewhere in the CLI (`.claude/skills/agnes-conventions/
references/security.md`: never put secrets on argv). `--remove` clears the
secret instead, with `--kind` selecting which vault secret (`storage` or
`master`) the PUT/DELETE targets — see Task 3's server-side contract
(`PUT/DELETE /api/admin/source-connections/{id}/secret`).
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


class TestSetSecret:
    def test_set_master_secret_via_hidden_prompt(self):
        with patch(
            "cli.commands.admin_connection.api_put",
            return_value=_resp(200),
        ) as put:
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--kind", "master"],
                input="tok\n",
            )
        assert result.exit_code == 0, result.output
        put.assert_called_once_with(
            "/api/admin/source-connections/CONN/secret",
            json={"value": "tok", "kind": "master"},
        )
        assert "master" in result.output

    def test_set_defaults_to_storage_kind(self):
        with patch(
            "cli.commands.admin_connection.api_put",
            return_value=_resp(200),
        ) as put:
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN"],
                input="tok\n",
            )
        assert result.exit_code == 0, result.output
        put.assert_called_once_with(
            "/api/admin/source-connections/CONN/secret",
            json={"value": "tok", "kind": "storage"},
        )

    def test_token_never_accepted_as_argv_option(self):
        """The token must not be settable via a CLI flag — no `--token` on
        this command. Passing one should fail argument parsing, not be
        silently accepted."""
        with patch("cli.commands.admin_connection.api_put", return_value=_resp(200)):
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--kind", "master", "--token", "tok"],
            )
        assert result.exit_code != 0

    def test_invalid_kind_rejected(self):
        with patch("cli.commands.admin_connection.api_put") as put:
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--kind", "bogus"],
                input="tok\n",
            )
        assert result.exit_code != 0
        put.assert_not_called()

    def test_error_response_surfaces_and_exits_nonzero(self):
        with patch(
            "cli.commands.admin_connection.api_put",
            return_value=_resp(400, {"detail": "bad kind"}),
        ):
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--kind", "master"],
                input="tok\n",
            )
        assert result.exit_code != 0
        assert "bad kind" in result.output


class TestRemoveSecret:
    def test_remove_master_secret(self):
        with patch(
            "cli.commands.admin_connection.api_delete",
            return_value=_resp(204),
        ) as delete:
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--remove", "--kind", "master"],
            )
        assert result.exit_code == 0, result.output
        delete.assert_called_once_with(
            "/api/admin/source-connections/CONN/secret",
            params={"kind": "master"},
        )
        assert "master" in result.output

    def test_remove_defaults_to_storage_kind(self):
        with patch(
            "cli.commands.admin_connection.api_delete",
            return_value=_resp(204),
        ) as delete:
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--remove"],
            )
        assert result.exit_code == 0, result.output
        delete.assert_called_once_with(
            "/api/admin/source-connections/CONN/secret",
            params={"kind": "storage"},
        )

    def test_remove_error_response_surfaces_and_exits_nonzero(self):
        with patch(
            "cli.commands.admin_connection.api_delete",
            return_value=_resp(404, {"detail": "not found"}),
        ):
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--remove", "--kind", "master"],
            )
        assert result.exit_code != 0
        assert "not found" in result.output


class TestFromFile:
    """`--from-file` exists for multiline secrets — a SharePoint combined
    cert+key PEM cannot travel through the single-line hidden prompt. A file
    PATH on argv is fine (the security rule bans the secret VALUE on argv)."""

    def test_from_file_reads_the_file_and_skips_the_prompt(self, tmp_path):
        pem = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n-----BEGIN PRIVATE KEY-----\nxyz\n-----END PRIVATE KEY-----\n"
        f = tmp_path / "combined.pem"
        f.write_text(pem)
        with patch(
            "cli.commands.admin_connection.api_put",
            return_value=_resp(204),
        ) as put:
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--from-file", str(f)],
            )
        assert result.exit_code == 0, result.output
        put.assert_called_once_with(
            "/api/admin/source-connections/CONN/secret",
            json={"value": pem.strip(), "kind": "storage"},
        )

    def test_from_file_dash_reads_stdin(self):
        with patch(
            "cli.commands.admin_connection.api_put",
            return_value=_resp(204),
        ) as put:
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--from-file", "-"],
                input="line1\nline2\n",
            )
        assert result.exit_code == 0, result.output
        put.assert_called_once_with(
            "/api/admin/source-connections/CONN/secret",
            json={"value": "line1\nline2", "kind": "storage"},
        )

    def test_missing_file_fails_cleanly_naming_the_path(self, tmp_path):
        with patch("cli.commands.admin_connection.api_put") as put:
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--from-file", str(tmp_path / "nope.pem")],
            )
        assert result.exit_code != 0
        assert "nope.pem" in result.output
        put.assert_not_called()

    def test_empty_file_is_refused(self, tmp_path):
        f = tmp_path / "empty.pem"
        f.write_text("   \n")
        with patch("cli.commands.admin_connection.api_put") as put:
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--from-file", str(f)],
            )
        assert result.exit_code != 0
        put.assert_not_called()

    def test_from_file_with_remove_is_refused(self, tmp_path):
        f = tmp_path / "combined.pem"
        f.write_text("x")
        with patch("cli.commands.admin_connection.api_delete") as delete:
            result = runner.invoke(
                app,
                ["admin", "connection", "secret", "CONN", "--remove", "--from-file", str(f)],
            )
        assert result.exit_code != 0
        delete.assert_not_called()
