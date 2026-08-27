"""Tests for `agnes pull` Typer wrapper."""

import json
from unittest.mock import patch

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


from cli.commands.pull import pull_app  # noqa: E402

runner = CliRunner()


class _FakePullResult:
    """Minimal duck-typed PullResult so the legacy-hook nudge tests don't
    depend on a live server / real manifest."""

    tables_updated = 0
    parquets_total = 0
    rules_count = 0
    duration_s = 0.0
    errors: list = []
    stack_sync = None
    # Added in #594 (data-package prune): the human-readable pull summary
    # reads `result.tables_removed`, so the fake must carry the field too.
    tables_removed = 0


def _write_legacy_settings(workspace):
    sp = workspace / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {"hooks": [{"type": "command", "command": "python server/scripts/collect_session.py"}]},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )


_NUDGE = "outdated hook layout"


def _run_pull_in(workspace, monkeypatch, args):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    monkeypatch.setenv("AGNES_SERVER", "http://server.test:8000")
    monkeypatch.setenv("AGNES_TOKEN", "tok")
    with patch("cli.commands.pull.run_pull", return_value=_FakePullResult()):
        return runner.invoke(pull_app, args)


def test_pull_nudges_on_legacy_hooks(tmp_path, monkeypatch):
    """A legacy-hook workspace gets exactly one stderr nudge pointing at
    `agnes init`."""
    _write_legacy_settings(tmp_path)
    result = _run_pull_in(tmp_path, monkeypatch, [])
    assert result.exit_code == 0
    err = _clean(result.stderr or "")
    assert _NUDGE in err
    assert "agnes init" in err
    # Emitted exactly once.
    assert err.count(_NUDGE) == 1


def test_pull_no_nudge_on_modern_workspace(tmp_path, monkeypatch):
    """A modern `agnes init` workspace gets no nudge (no double-nudge)."""
    from cli.lib.hooks import install_claude_hooks

    install_claude_hooks(tmp_path)
    result = _run_pull_in(tmp_path, monkeypatch, [])
    assert result.exit_code == 0
    assert _NUDGE not in _clean(result.stderr or "")


def test_pull_nudge_suppressed_under_quiet(tmp_path, monkeypatch):
    """`--quiet` (the SessionStart hook path) stays silent — no nudge."""
    _write_legacy_settings(tmp_path)
    result = _run_pull_in(tmp_path, monkeypatch, ["--quiet"])
    assert result.exit_code == 0
    assert _NUDGE not in _clean(result.stderr or "")


def test_pull_help():
    result = runner.invoke(pull_app, ["--help"])
    assert result.exit_code == 0
    assert "--quiet" in _clean(result.output)
    assert "--json" in _clean(result.output)
    assert "--dry-run" in _clean(result.output)


class _FailingPullResult:
    """Duck-typed PullResult with one per-table failure recorded — mirrors a
    real `run_pull` outcome where a table failed hash verification on every
    attempt (#596). Drives the exit-code assertions below."""

    tables_updated = 0
    tables_removed = 0
    parquets_total = 1
    rules_count = 0
    duration_s = 0.1
    errors = [{"table": "kbc_project", "error": "hash mismatch: expected aaa, got bbb"}]
    stack_sync = None


def _run_failing_pull_in(workspace, monkeypatch, args):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    monkeypatch.setenv("AGNES_SERVER", "http://server.test:8000")
    monkeypatch.setenv("AGNES_TOKEN", "tok")
    with patch("cli.commands.pull.run_pull", return_value=_FailingPullResult()):
        return runner.invoke(pull_app, args)


def test_pull_exits_nonzero_on_table_failure_normal_path(tmp_path, monkeypatch):
    """#596: a forced per-table failure must exit 1 (was 0) on the normal
    human-readable path, with the warning still rendered to stderr."""
    from cli.lib.hooks import install_claude_hooks

    install_claude_hooks(tmp_path)  # modern workspace, no legacy nudge
    result = _run_failing_pull_in(tmp_path, monkeypatch, [])
    assert result.exit_code == 1, "partial pull must exit non-zero"
    assert "hash mismatch" in _clean(result.stderr or "")


def test_pull_exits_nonzero_on_table_failure_quiet_path(tmp_path, monkeypatch):
    """#596: the silent SessionStart-hook path (`--quiet`) must also exit 1 on
    a table failure — the canonical hook's `|| true` is what swallows it, not
    a hidden exit 0."""
    result = _run_failing_pull_in(tmp_path, monkeypatch, ["--quiet"])
    assert result.exit_code == 1
    assert "warn" in _clean(result.stderr or "")


def test_pull_exits_nonzero_on_table_failure_json_path(tmp_path, monkeypatch):
    """#596: the `--json` path must emit the summary dict (so consumers can
    read `errors`) AND exit 1."""
    result = _run_failing_pull_in(tmp_path, monkeypatch, ["--json"])
    assert result.exit_code == 1, "json path must exit non-zero on errors"
    # The JSON object is still emitted before the non-zero exit.
    payload = json.loads(_clean(result.stdout).strip())
    assert payload["errors"], "json output must carry the error list"
    assert payload["errors"][0]["table"] == "kbc_project"


def test_pull_exits_zero_when_no_errors(tmp_path, monkeypatch):
    """Counterpart: a clean pull (no errors) still exits 0 on every path —
    the non-zero exit must be gated strictly on `result.errors`."""
    from cli.lib.hooks import install_claude_hooks

    install_claude_hooks(tmp_path)
    assert _run_pull_in(tmp_path, monkeypatch, []).exit_code == 0
    assert _run_pull_in(tmp_path, monkeypatch, ["--quiet"]).exit_code == 0
    assert _run_pull_in(tmp_path, monkeypatch, ["--json"]).exit_code == 0


def test_pull_empty_manifest_explains_zero_tables(tmp_path, monkeypatch):
    """#754: an empty manifest with no transport/server errors (real
    `run_pull` reaches this branch only when the manifest fetch itself
    succeeded — see `cli/lib/pull.py:run_pull` step 1) must print an
    explanatory line distinguishing "nothing granted/registered" from a
    transport failure, instead of the bare pre-fix
    "Updated 0 tables (0 total)." with zero context."""
    from cli.lib.hooks import install_claude_hooks

    install_claude_hooks(tmp_path)
    result = _run_pull_in(tmp_path, monkeypatch, [])
    assert result.exit_code == 0
    out = _clean(result.stdout)
    assert "no tables" in out.lower() or "nothing" in out.lower()
    # Post-#356 the actionable hint is the data-package/stack flow, not
    # per-table grants — the message must point at `agnes stack browse`.
    assert "stack browse" in out.lower()
    assert "data package" in out.lower() or "data_package" in out.lower()


def test_pull_empty_manifest_silent_under_quiet(tmp_path, monkeypatch):
    """The explanatory line is a non-error UX nicety — must NOT print under
    `--quiet` (the SessionStart hook path stays silent on success)."""
    result = _run_pull_in(tmp_path, monkeypatch, ["--quiet"])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_pull_empty_manifest_silent_under_json(tmp_path, monkeypatch):
    """`--json` output stays machine-readable — no extra prose line mixed
    into the JSON payload."""
    result = _run_pull_in(tmp_path, monkeypatch, ["--json"])
    assert result.exit_code == 0
    json.loads(_clean(result.stdout).strip())  # must still parse cleanly


def test_pull_no_server_friendly_exit(tmp_path, monkeypatch):
    """No configured server -> exit 1 with friendly hint (no traceback)."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.delenv("AGNES_SERVER", raising=False)
    monkeypatch.delenv("AGNES_TOKEN", raising=False)
    result = runner.invoke(pull_app, [])
    # Either exit 1 with hint, or exit 0 if a default server URL applies.
    # Either way, there must be no Python traceback in stderr/stdout.
    assert "Traceback" not in (_clean(result.output) + _clean(result.stderr or ""))


class TestPullErrorRendering:
    """`PullResult.errors` entries are dicts built at the failure site; the
    CLI used to print them with an f-string, so an analyst whose download
    403'd got a Python repr:

        warn: {'table': 'orders', 'error': "Client error '403 Forbidden' ..."}
    """

    def test_table_error_renders_as_prose(self):
        from cli.commands.pull import format_pull_error

        line = format_pull_error({"table": "orders", "error": "Client error '403 Forbidden' for url '...'"})
        assert not line.startswith("{")
        assert "'table'" not in line
        assert line == "orders: Client error '403 Forbidden' for url '...'"

    def test_stage_and_package_shapes_render(self):
        from cli.commands.pull import format_pull_error

        assert format_pull_error({"stage": "manifest", "error": "boom"}) == "manifest: boom"
        assert format_pull_error({"package": "sales", "name": "orders", "error": "nope"}) == "orders sales: nope"

    def test_unknown_shape_falls_back_to_str(self):
        from cli.commands.pull import format_pull_error

        assert format_pull_error("plain string") == "plain string"
        assert format_pull_error({"weird": 1}) == str({"weird": 1})

    def test_both_warn_sites_use_the_formatter(self):
        """Static guard: neither emission path may go back to the raw f-string.

        `format_pull_error` only helps if the call sites use it; the quiet
        (SessionStart-hook) path and the interactive path each print errors
        and it was the interactive one an analyst hit.
        """
        from pathlib import Path

        src = Path("cli/commands/pull.py").read_text(encoding="utf-8")
        assert 'f"warn: {e}"' not in src, "raw repr of the error dict is back — route it through format_pull_error"
        assert src.count("format_pull_error(e)") == 2, "both the --quiet and the interactive error paths must format"


class TestPullRevealsWorkspace:
    """Issue #1312 (cheap proposal 1): `agnes pull` never echoed which
    workspace it targeted — neither on the human path nor in `--json`."""

    def test_json_payload_carries_workspace(self, tmp_path, monkeypatch):
        from pathlib import Path as _P

        result = _run_pull_in(tmp_path, monkeypatch, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(_clean(result.stdout).strip())
        assert payload["workspace"] == str(_P(tmp_path).resolve())

    def test_human_output_shows_resolved_workspace(self, tmp_path, monkeypatch):
        from cli.lib.hooks import install_claude_hooks

        install_claude_hooks(tmp_path)  # modern workspace, no legacy nudge noise
        result = _run_pull_in(tmp_path, monkeypatch, [])
        assert result.exit_code == 0, result.output
        out = _clean(result.stdout)
        assert f"Workspace: {tmp_path.resolve()}" in out

    def test_workspace_line_suppressed_under_quiet(self, tmp_path, monkeypatch):
        result = _run_pull_in(tmp_path, monkeypatch, ["--quiet"])
        assert result.exit_code == 0
        assert "Workspace:" not in _clean(result.stdout)


def _shape(p):
    (p / ".claude").mkdir(parents=True, exist_ok=True)
    (p / ".claude" / "init-complete").write_text("x", encoding="utf-8")
    return p


def _run_pull_from_cwd(cwd, monkeypatch, args, config_dir=None):
    """Like `_run_pull_in` but resolves the workspace the way an analyst
    typing `agnes pull` from an arbitrary shell actually does — via
    AGNES_CONFIG_DIR + workspace_root + cwd — instead of forcing
    AGNES_LOCAL_DIR (which always wins and would mask anchor divergence)."""
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setenv("AGNES_SERVER", "http://server.test:8000")
    monkeypatch.setenv("AGNES_TOKEN", "tok")
    if config_dir is not None:
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(config_dir))
    monkeypatch.chdir(cwd)
    with patch("cli.commands.pull.run_pull", return_value=_FakePullResult()):
        return runner.invoke(pull_app, args)


class TestPullWarnsOnAnchorDivergence:
    """Issue #1312 (remaining scope, item 1): pull resolved a workspace and
    said so, but never flagged when that workspace differs from the
    anchored `workspace_root` — the one `agnes update` / SessionStart hooks
    target. A pull that silently lands in the "wrong" (cwd) workspace looked
    identical to a correct one."""

    def test_note_when_cwd_differs_from_configured_anchor(self, tmp_path, monkeypatch):
        from cli.config import set_workspace_root

        config_dir = tmp_path / "config"
        anchor = _shape(tmp_path / "anchor")
        cwd = _shape(tmp_path / "cwd-ws")
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(config_dir))
        set_workspace_root(str(anchor))

        result = _run_pull_from_cwd(cwd, monkeypatch, [])
        assert result.exit_code == 0, result.output
        err = _clean(result.stderr or "")
        assert str(anchor.resolve()) in err
        assert "differs" in err.lower() or "anchor" in err.lower()

    def test_no_note_when_cwd_matches_anchor(self, tmp_path, monkeypatch):
        from cli.config import set_workspace_root

        config_dir = tmp_path / "config"
        anchor = _shape(tmp_path / "anchor")
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(config_dir))
        set_workspace_root(str(anchor))

        result = _run_pull_from_cwd(anchor, monkeypatch, [])
        assert result.exit_code == 0, result.output
        assert "anchor" not in _clean(result.stderr or "").lower()

    def test_no_note_when_no_anchor_configured(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "config"
        cwd = _shape(tmp_path / "cwd-ws")
        result = _run_pull_from_cwd(cwd, monkeypatch, [], config_dir=config_dir)
        assert result.exit_code == 0, result.output
        assert "anchor" not in _clean(result.stderr or "").lower()

    def test_no_note_with_explicit_workspace_flag(self, tmp_path, monkeypatch):
        """An explicit `--workspace` is an intentional override — not the
        silent divergence #1312 is about."""
        from cli.config import set_workspace_root

        config_dir = tmp_path / "config"
        anchor = _shape(tmp_path / "anchor")
        cwd = _shape(tmp_path / "cwd-ws")
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(config_dir))
        set_workspace_root(str(anchor))

        result = _run_pull_from_cwd(cwd, monkeypatch, ["--workspace", str(cwd)])
        assert result.exit_code == 0, result.output
        assert "anchor" not in _clean(result.stderr or "").lower()

    def test_note_suppressed_under_quiet_and_json(self, tmp_path, monkeypatch):
        from cli.config import set_workspace_root

        config_dir = tmp_path / "config"
        anchor = _shape(tmp_path / "anchor")
        cwd = _shape(tmp_path / "cwd-ws")
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(config_dir))
        set_workspace_root(str(anchor))

        quiet_result = _run_pull_from_cwd(cwd, monkeypatch, ["--quiet"])
        assert "anchor" not in _clean(quiet_result.stderr or "").lower()

        json_result = _run_pull_from_cwd(cwd, monkeypatch, ["--json"])
        assert json_result.exit_code == 0, json_result.output

    def test_json_payload_carries_workspace_root(self, tmp_path, monkeypatch):
        from cli.config import set_workspace_root

        config_dir = tmp_path / "config"
        anchor = _shape(tmp_path / "anchor")
        cwd = _shape(tmp_path / "cwd-ws")
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(config_dir))
        set_workspace_root(str(anchor))

        result = _run_pull_from_cwd(cwd, monkeypatch, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(_clean(result.stdout).strip())
        assert payload["workspace"] == str(cwd.resolve())
        assert payload["workspace_root"] == str(anchor.resolve())

    def test_json_payload_workspace_root_null_when_unconfigured(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "config"
        cwd = _shape(tmp_path / "cwd-ws")
        result = _run_pull_from_cwd(cwd, monkeypatch, ["--json"], config_dir=config_dir)
        assert result.exit_code == 0, result.output
        payload = json.loads(_clean(result.stdout).strip())
        assert payload["workspace_root"] is None
