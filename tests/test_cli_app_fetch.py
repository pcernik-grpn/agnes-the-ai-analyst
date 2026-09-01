"""CLI tests for `agnes app fetch` — authenticated GET against a hosted app.

The command exists so an agent never handles a credential: the CLI resolves the
token itself, so it appears in no command, no shell history and no transcript.
That only holds if the token can never be aimed anywhere but the app's own
origin — which is what most of this file pins.

Same idiom as `tests/test_cli_data_apps.py`: patch the module-level names Typer
captured at import time, then drive through `CliRunner`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()

APP_ORIGIN = "https://s.apps.example.com/"


def _api(status_code=200, body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body if body is not None else {"slug": "s", "url": APP_ORIGIN, "state": "running"}
    resp.text = str(body)
    return resp


def _upstream(status_code=200, text="hello", content=b"hello"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = content
    return resp


def _run(args, api=None, upstream=None, token="tok-123"):
    with patch("cli.commands.data_apps.api_get", return_value=api or _api()) as g, patch(
        "cli.commands.data_apps.httpx.get", return_value=upstream or _upstream()
    ) as h, patch("cli.commands.data_apps.get_token", return_value=token):
        result = runner.invoke(app, args)
    return result, g, h


# --- the happy path --------------------------------------------------------


def test_fetch_prints_the_body():
    result, _, h = _run(["app", "fetch", "s", "/provenance.json"])
    assert result.exit_code == 0, result.output
    assert "hello" in result.output
    assert h.call_args[0][0] == "https://s.apps.example.com/provenance.json"


def test_fetch_sends_the_token_as_a_bearer():
    _, _, h = _run(["app", "fetch", "s", "/x"])
    assert h.call_args.kwargs["headers"]["Authorization"] == "Bearer tok-123"


def test_fetch_defaults_to_the_app_root():
    _, _, h = _run(["app", "fetch", "s"])
    assert h.call_args[0][0] == "https://s.apps.example.com/"


def test_fetch_writes_to_a_file_with_output(tmp_path):
    dest = tmp_path / "out.json"
    result, _, _ = _run(["app", "fetch", "s", "/x", "--output", str(dest)])
    assert result.exit_code == 0, result.output
    assert dest.read_bytes() == b"hello"


# --- the token must never leave the app's own origin -----------------------
# The whole point of the command is that the credential is handled for you. A
# path argument that can retarget the request would turn that convenience into
# a way to post an Agnes token to an arbitrary host.


def test_absolute_url_as_path_is_refused():
    result, _, h = _run(["app", "fetch", "s", "https://evil.example/steal"])
    assert result.exit_code != 0
    h.assert_not_called()


def test_protocol_relative_path_is_refused():
    result, _, h = _run(["app", "fetch", "s", "//evil.example/steal"])
    assert result.exit_code != 0
    h.assert_not_called()


def test_traversal_is_normalised_onto_the_app_origin():
    """`..` does not escape, and is not refused for show.

    The property being defended is that the token only ever goes to the app's
    own origin — not that the path looks tidy. URL joining normalises the
    traversal, so `/../../etc/passwd` resolves to `/etc/passwd` ON THE APP,
    which the app answers or 404s like any other path. Refusing it would be
    theatre: the identical request is available by typing the normalised path,
    and this is HTTP, not a file read — there is no filesystem behind it to
    walk out of.
    """
    _, _, h = _run(["app", "fetch", "s", "/../../etc/passwd"])
    assert h.call_args[0][0].startswith("https://s.apps.example.com/")
    assert "evil" not in h.call_args[0][0]


# --- deployments that do not serve apps on their own origin ----------------


def test_path_prefix_deployment_is_refused_with_an_explanation():
    """`_app_url` returns a relative `/apps/<slug>/` when no subdomain base is
    configured, and the ingress refuses that form. Say so, rather than let the
    user read a bare 403."""
    api = _api(body={"slug": "s", "url": "/apps/s/", "state": "running"})
    result, _, h = _run(["app", "fetch", "s", "/x"], api=api)
    assert result.exit_code != 0
    h.assert_not_called()
    assert "origin" in result.output.lower()


# --- ordinary failures -----------------------------------------------------


def test_unknown_app_reports_not_found():
    result, _, h = _run(["app", "fetch", "nope", "/x"], api=_api(status_code=404, body={}))
    assert result.exit_code != 0
    h.assert_not_called()


def test_upstream_error_status_is_surfaced():
    result, _, _ = _run(["app", "fetch", "s", "/x"], upstream=_upstream(status_code=403, text="forbidden"))
    assert result.exit_code != 0
    assert "403" in result.output
