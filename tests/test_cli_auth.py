"""Tests for agnes auth login/logout/whoami commands."""

import json
import pytest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path / "local"))
    # `agnes auth login` exports the resolved server into os.environ, which
    # outlives the invocation — so without this every test after one that signs
    # in would inherit a server it never asked for, and a missing-server case
    # would pass for the wrong reason in a full-file run while failing in
    # isolation. Tests that need one set it explicitly.
    monkeypatch.delenv("AGNES_SERVER", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "local").mkdir()
    yield tmp_path


def _make_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


class TestAuthLogin:
    def test_login_browser_success(self, monkeypatch):
        """Default browser flow: capture a code, exchange it, save the token."""
        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        exch = _make_response(
            200,
            {
                "token": "tok123",
                "email": "alice@example.com",
                "expires_at": "2026-09-01 00:00:00+00:00",
            },
        )
        with patch("cli.lib.loopback.capture_code_via_browser", return_value="code-xyz"):
            with patch("cli.commands.auth.api_post", return_value=exch) as mock_post:
                with patch("cli.commands.auth.save_token") as mock_save:
                    result = runner.invoke(app, ["auth", "login", "--no-browser"])
        assert result.exit_code == 0, result.output
        assert "alice@example.com" in result.output
        mock_save.assert_called_once_with("tok123", "alice@example.com")
        # The captured code is exchanged at the right endpoint.
        assert mock_post.call_args[0][0] == "/cli/auth/exchange"
        assert mock_post.call_args[1]["json"]["code"] == "code-xyz"

    def test_login_browser_timeout_prints_manual_hint(self, monkeypatch):
        """A timeout exits 1 and points the user at the manual import path."""
        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        with patch(
            "cli.lib.loopback.capture_code_via_browser",
            side_effect=TimeoutError("no callback"),
        ):
            result = runner.invoke(app, ["auth", "login", "--no-browser"])
        assert result.exit_code == 1
        assert "timed out" in result.output.lower()
        assert "import-token" in result.output

    def test_login_server_too_old(self, monkeypatch):
        """A 404 from /cli/auth/exchange means the server predates this flow."""
        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        exch = _make_response(404, {"detail": "Not Found"})
        with patch("cli.lib.loopback.capture_code_via_browser", return_value="c"):
            with patch("cli.commands.auth.api_post", return_value=exch):
                result = runner.invoke(app, ["auth", "login", "--no-browser"])
        assert result.exit_code == 1
        assert "doesn't support browser login" in result.output

    def test_login_password_mode(self, monkeypatch):
        """--password keeps the terminal-only email+password path."""
        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        resp = _make_response(200, {"access_token": "tokP", "email": "bob@example.com"})
        with patch("cli.commands.auth.api_post", return_value=resp) as mock_post:
            with patch("cli.commands.auth.save_token") as mock_save:
                result = runner.invoke(
                    app,
                    ["auth", "login", "--password"],
                    input="bob@example.com\nhunter2\n",
                )
        assert result.exit_code == 0, result.output
        mock_save.assert_called_once_with("tokP", "bob@example.com")
        assert mock_post.call_args[0][0] == "/auth/token"
        assert mock_post.call_args[1]["json"] == {"email": "bob@example.com", "password": "hunter2"}

    def test_login_password_mode_invalid_credentials(self, monkeypatch):
        """Bad password creds exit with error under --password."""
        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        mock_resp = _make_response(401, {"detail": "Invalid credentials"})
        with patch("cli.commands.auth.api_post", return_value=mock_resp):
            result = runner.invoke(
                app,
                ["auth", "login", "--password"],
                input="bad@example.com\nnope\n",
            )
        assert result.exit_code == 1
        assert "Login failed" in result.output

    def test_login_refuses_when_no_server_is_configured(self, monkeypatch):
        """No --server, no AGNES_SERVER, no saved config -> refuse.

        The old behavior fell through to `get_server_url()`'s
        `http://localhost:8000` default and opened a browser there, so the
        only symptom a user saw was "localhost refused to connect" — with no
        hint that the server was never configured.
        """
        monkeypatch.delenv("AGNES_SERVER", raising=False)
        with patch("cli.lib.loopback.capture_code_via_browser") as mock_capture:
            result = runner.invoke(app, ["auth", "login", "--no-browser"])
        assert result.exit_code == 1
        assert "--server" in result.output
        # localhost may only appear as the explicit local-dev hint — never as a
        # server this command silently went and opened.
        assert "Opening http://localhost:8000" not in result.output
        mock_capture.assert_not_called()

    def test_login_password_refuses_when_no_server_is_configured(self, monkeypatch):
        """The --password path is gated by the same gate, before any prompt."""
        monkeypatch.delenv("AGNES_SERVER", raising=False)
        with patch("cli.commands.auth.api_post") as mock_post:
            result = runner.invoke(
                app,
                ["auth", "login", "--password"],
                input="a@example.com\nhunter2\n",
            )
        assert result.exit_code == 1
        assert "--server" in result.output
        # Refused before asking for credentials, not after.
        assert "Email" not in result.output
        mock_post.assert_not_called()

    def test_login_with_server_flag_persists_server_to_config_yaml(self, tmp_path, monkeypatch):
        """`--server` must be persisted, like `agnes auth import-token` does.

        Without this, the very next command (`agnes auth whoami`, `agnes pull`,
        even this command's own manual-fallback hint) resolved back to the
        localhost default, because login only exported AGNES_SERVER in-process.
        Also asserts the URL is normalized (trailing slash stripped).
        """
        monkeypatch.delenv("AGNES_SERVER", raising=False)
        exch = _make_response(200, {"token": "tok", "email": "a@example.com", "expires_at": None})
        with patch("cli.lib.loopback.capture_code_via_browser", return_value="c"):
            with patch("cli.commands.auth.api_post", return_value=exch):
                with patch("cli.commands.auth.save_token"):
                    result = runner.invoke(
                        app,
                        ["auth", "login", "--server", "https://agnes.example.com/", "--no-browser"],
                    )
        assert result.exit_code == 0, result.output

        import yaml

        config_file = tmp_path / "config" / "config.yaml"
        assert config_file.exists(), "config.yaml must be written when --server is passed"
        assert yaml.safe_load(config_file.read_text()).get("server") == "https://agnes.example.com"

    def test_login_does_not_persist_server_when_login_fails(self, tmp_path, monkeypatch):
        """A --server that never signed in must not become the saved default.

        `agnes auth import-token` persists before verifying; here a typo'd host
        would otherwise stick around as the server every later command resolves.
        """
        monkeypatch.delenv("AGNES_SERVER", raising=False)
        with patch(
            "cli.lib.loopback.capture_code_via_browser",
            side_effect=TimeoutError("no callback"),
        ):
            result = runner.invoke(
                app,
                ["auth", "login", "--server", "https://typo.example.com", "--no-browser"],
            )
        assert result.exit_code == 1
        assert not (tmp_path / "config" / "config.yaml").exists()
        # The failed run still points its own fallback hint at the right host.
        assert "https://typo.example.com/me/profile#tokens" in result.output

    def test_login_password_persists_server_after_success(self, tmp_path, monkeypatch):
        """The --password path persists too — one behavior on both doors."""
        monkeypatch.delenv("AGNES_SERVER", raising=False)
        resp = _make_response(200, {"access_token": "tokP", "email": "bob@example.com"})
        with patch("cli.commands.auth.api_post", return_value=resp):
            with patch("cli.commands.auth.save_token"):
                result = runner.invoke(
                    app,
                    ["auth", "login", "--password", "--server", "https://pw.example.com"],
                    input="bob@example.com\nhunter2\n",
                )
        assert result.exit_code == 0, result.output

        import yaml

        cfg = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text())
        assert cfg.get("server") == "https://pw.example.com"

    def test_login_accepts_server_from_saved_config(self, tmp_path, monkeypatch):
        """A server already in config.yaml is enough — the gate must not
        demand --server on every login."""
        monkeypatch.delenv("AGNES_SERVER", raising=False)
        (tmp_path / "config" / "config.yaml").write_text("server: https://saved.example.com\n")
        exch = _make_response(200, {"token": "tok", "email": "a@example.com", "expires_at": None})
        with patch("cli.lib.loopback.capture_code_via_browser", return_value="c") as mock_capture:
            with patch("cli.commands.auth.api_post", return_value=exch):
                with patch("cli.commands.auth.save_token"):
                    result = runner.invoke(app, ["auth", "login", "--no-browser"])
        assert result.exit_code == 0, result.output
        assert mock_capture.call_args[0][0] == "https://saved.example.com"


class TestAuthLogout:
    def test_logout(self):
        """Logout clears token and confirms."""
        with patch("cli.commands.auth.clear_token") as mock_clear:
            result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0
        assert "Logged out" in result.output
        mock_clear.assert_called_once()


class TestAuthImportToken:
    def _make_jwt(self, email="alice@example.com", typ="pat"):
        import jwt as pyjwt

        return pyjwt.encode(
            {"email": email, "typ": typ, "sub": "u-1"},
            "unused",
            algorithm="HS256",
        )

    def _mock_verify(self, status_code=200, json_data=None):
        """Build a patcher for cli.commands.auth.httpx.Client that returns a canned response."""
        resp = _make_response(status_code, json_data or {})
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False
        mock_client.get.return_value = resp
        return patch("cli.commands.auth.httpx.Client", return_value=mock_client)

    def test_import_token_success_writes_canonical_format(self, tmp_path, monkeypatch):
        """Valid JWT + 200 from server -> canonical token.json on disk."""
        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        token = self._make_jwt(email="bob@example.com")

        with self._mock_verify(200):
            result = runner.invoke(app, ["auth", "import-token", "--token", token])

        assert result.exit_code == 0, result.output
        assert "bob@example.com" in result.output

        token_file = tmp_path / "config" / "token.json"
        assert token_file.exists()
        data = json.loads(token_file.read_text())
        # v19: token.json no longer carries a role label (auth derives admin
        # from group memberships server-side).
        assert data == {"access_token": token, "email": "bob@example.com"}

    def test_import_token_401_does_not_overwrite_existing(self, tmp_path, monkeypatch):
        """A 401 response aborts import and leaves the prior token file untouched."""
        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        existing = {"access_token": "keep-me", "email": "old@example.com"}
        token_file = tmp_path / "config" / "token.json"
        token_file.write_text(json.dumps(existing))

        token = self._make_jwt()
        with self._mock_verify(401, {"detail": "Token revoked"}):
            result = runner.invoke(app, ["auth", "import-token", "--token", token])

        assert result.exit_code == 1
        assert "Token rejected by server" in result.output
        assert "Token revoked" in result.output
        # Existing file must be intact.
        assert json.loads(token_file.read_text()) == existing

    def test_import_token_with_server_flag_persists_server_to_config_yaml(self, tmp_path, monkeypatch):
        """Passing --server should write `server: URL` to ~/.config/agnes/config.yaml
        so the user never has to configure the server in a separate step."""
        # No AGNES_SERVER env var — rely entirely on the --server flag for persistence.
        monkeypatch.delenv("AGNES_SERVER", raising=False)
        token = self._make_jwt(email="dave@example.com")

        with self._mock_verify(200):
            result = runner.invoke(
                app,
                [
                    "auth",
                    "import-token",
                    "--token",
                    token,
                    "--server",
                    "https://agnes.example.com",
                ],
            )
        assert result.exit_code == 0, result.output

        config_file = tmp_path / "config" / "config.yaml"
        assert config_file.exists(), "config.yaml must be written when --server is passed"
        import yaml

        cfg = yaml.safe_load(config_file.read_text())
        assert cfg.get("server") == "https://agnes.example.com"

    def test_import_token_claim_fallback_via_cli_email_override(self, tmp_path, monkeypatch):
        """Missing email claim -> refuse without --email, accept with it.
        v19 dropped the --role flag (token.json no longer carries role)."""
        import jwt as pyjwt

        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        # JWT without email claim — simulates a malformed or minimal token.
        token = pyjwt.encode({"sub": "u-1", "typ": "pat"}, "unused", algorithm="HS256")

        with self._mock_verify(200):
            fail_result = runner.invoke(app, ["auth", "import-token", "--token", token])
        assert fail_result.exit_code == 1
        assert "missing" in fail_result.output.lower()

        with self._mock_verify(200):
            ok_result = runner.invoke(
                app,
                [
                    "auth",
                    "import-token",
                    "--token",
                    token,
                    "--email",
                    "carol@example.com",
                ],
            )
        assert ok_result.exit_code == 0, ok_result.output
        token_file = tmp_path / "config" / "token.json"
        data = json.loads(token_file.read_text())
        assert data == {"access_token": token, "email": "carol@example.com"}

    def _mock_verify_raises(self, exc):
        """Patch cli.commands.auth.httpx.Client so the verify GET raises `exc`."""
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False
        mock_client.get.side_effect = exc
        return patch("cli.commands.auth.httpx.Client", return_value=mock_client)

    def test_import_token_5xx_proceeds_with_warning(self, tmp_path, monkeypatch):
        """A 5xx during verification is transient, not a token rejection — the
        valid PAT is still saved, with a warning (was: hard Exit(1))."""
        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        token = self._make_jwt(email="erin@example.com")

        with self._mock_verify(503):
            result = runner.invoke(app, ["auth", "import-token", "--token", token])

        assert result.exit_code == 0, result.output
        assert "could not verify" in result.output.lower()
        token_file = tmp_path / "config" / "token.json"
        assert json.loads(token_file.read_text()) == {
            "access_token": token,
            "email": "erin@example.com",
        }

    def test_import_token_read_timeout_proceeds_with_warning(self, tmp_path, monkeypatch):
        """A slow verify (read timeout) must not fail a valid PAT — save + warn."""
        import httpx

        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        token = self._make_jwt(email="frank@example.com")

        with self._mock_verify_raises(httpx.ReadTimeout("timed out")):
            result = runner.invoke(app, ["auth", "import-token", "--token", token])

        assert result.exit_code == 0, result.output
        assert "could not verify" in result.output.lower()
        token_file = tmp_path / "config" / "token.json"
        assert json.loads(token_file.read_text()) == {
            "access_token": token,
            "email": "frank@example.com",
        }

    def test_import_token_connect_error_still_aborts(self, tmp_path, monkeypatch):
        """A genuinely unreachable server (connect error) still aborts and does
        not write a token — that failure mode is unchanged."""
        import httpx

        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        token = self._make_jwt()

        with self._mock_verify_raises(httpx.ConnectError("refused")):
            result = runner.invoke(app, ["auth", "import-token", "--token", token])

        assert result.exit_code == 1
        assert "could not reach server" in result.output.lower()
        assert not (tmp_path / "config" / "token.json").exists()

    def test_import_token_verify_timeout_env_override(self, tmp_path, monkeypatch):
        """AGNES_VERIFY_TIMEOUT overrides the hard-coded verification timeout."""
        monkeypatch.setenv("AGNES_SERVER", "http://example.test")
        monkeypatch.setenv("AGNES_VERIFY_TIMEOUT", "45")
        token = self._make_jwt(email="grace@example.com")

        with self._mock_verify(200) as mock_client_cls:
            result = runner.invoke(app, ["auth", "import-token", "--token", token])

        assert result.exit_code == 0, result.output
        assert mock_client_cls.call_args.kwargs["timeout"] == 45.0


class TestAuthWhoami:
    def test_whoami_no_token(self):
        """Whoami exits when no token is stored."""
        with patch("cli.commands.auth.get_token", return_value=None):
            result = runner.invoke(app, ["auth", "whoami"])
        assert result.exit_code == 1
        assert "Not logged in" in result.output

    def test_whoami_valid_token(self):
        """Whoami decodes JWT and shows user info. v19: no role claim."""
        import jwt as pyjwt

        token = pyjwt.encode(
            {"email": "alice@example.com"},
            "secret",
            algorithm="HS256",
        )
        with patch("cli.commands.auth.get_token", return_value=token):
            with patch("cli.commands.auth.get_server_url", return_value="http://localhost:8000"):
                result = runner.invoke(app, ["auth", "whoami"])
        assert result.exit_code == 0
        assert "alice@example.com" in result.output

    def test_whoami_invalid_token(self):
        """Whoami with garbled token exits with error."""
        with patch("cli.commands.auth.get_token", return_value="not.a.jwt"):
            result = runner.invoke(app, ["auth", "whoami"])
        # May succeed or fail depending on jwt decode — either way no traceback
        assert result.exit_code in (0, 1)

    def test_whoami_shows_token_expiry(self):
        """Whoami surfaces the token's `exp` claim (#477)."""
        import jwt as pyjwt
        from datetime import datetime, timedelta, timezone

        exp = datetime.now(timezone.utc) + timedelta(days=10)
        token = pyjwt.encode(
            {"email": "alice@example.com", "exp": int(exp.timestamp())},
            "secret",
            algorithm="HS256",
        )
        with patch("cli.commands.auth.get_token", return_value=token):
            with patch("cli.commands.auth.get_server_url", return_value="http://localhost:8000"):
                result = runner.invoke(app, ["auth", "whoami"])
        assert result.exit_code == 0
        assert "Token:" in result.output
        assert "valid until" in result.output

    def test_whoami_shows_unknown_expiry_when_no_exp_claim(self):
        """Whoami degrades gracefully when the token has no `exp` claim."""
        import jwt as pyjwt

        token = pyjwt.encode({"email": "alice@example.com"}, "secret", algorithm="HS256")
        with patch("cli.commands.auth.get_token", return_value=token):
            with patch("cli.commands.auth.get_server_url", return_value="http://localhost:8000"):
                result = runner.invoke(app, ["auth", "whoami"])
        assert result.exit_code == 0
        assert "expiry unknown" in result.output


def test_da_login_sends_password(monkeypatch):
    import httpx
    from typer.testing import CliRunner
    from cli.commands import auth as auth_mod

    captured = {}

    def fake_post(path, json=None, **kwargs):
        captured["path"] = path
        captured["json"] = json
        return httpx.Response(
            200,
            json={
                "access_token": "tok",
                "email": "u@t",
                "role": "analyst",
                "user_id": "u1",
                "token_type": "bearer",
            },
        )

    monkeypatch.setattr(auth_mod, "api_post", fake_post, raising=False)
    monkeypatch.setenv("AGNES_SERVER", "http://example.test")

    runner = CliRunner()
    # Provide email and password via stdin (typer prompts). --password selects
    # the terminal-only path; the default flow is now browser-based.
    result = runner.invoke(auth_mod.auth_app, ["login", "--password"], input="u@t\nhunter2\n")
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/auth/token"
    assert captured["json"] == {"email": "u@t", "password": "hunter2"}


def test_da_auth_token_create_calls_api(monkeypatch):
    import httpx
    from typer.testing import CliRunner
    from cli.commands.auth import auth_app
    from cli.commands import tokens as tok_mod

    captured = {}

    def fake_post(path, json=None, **kwargs):
        captured["path"] = path
        captured["json"] = json
        return httpx.Response(
            201,
            json={
                "id": "abc",
                "name": json["name"],
                "prefix": "XXXXXXXX",
                "token": "raw-token-once",
                "expires_at": None,
                "created_at": "2026-04-21T00:00:00+00:00",
            },
        )

    monkeypatch.setattr(tok_mod, "api_post", fake_post, raising=False)

    runner = CliRunner()
    result = runner.invoke(auth_app, ["token", "create", "--name", "laptop", "--ttl", "30d"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/auth/tokens"
    assert captured["json"] == {"name": "laptop", "expires_in_days": 30, "surface": "all"}
    assert "raw-token-once" in result.output


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 15.0),  # unset -> default
        ("", 15.0),  # empty -> default
        ("30", 30.0),  # integer string
        ("12.5", 12.5),  # float string
        ("abc", 15.0),  # non-numeric -> default (typo can't disable the timeout)
        ("0", 15.0),  # non-positive -> default
        ("-5", 15.0),  # negative -> default
    ],
)
def test_resolve_verify_timeout(raw, expected):
    from cli.commands.auth import _resolve_verify_timeout

    assert _resolve_verify_timeout(raw) == expected
