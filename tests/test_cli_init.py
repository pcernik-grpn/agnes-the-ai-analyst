"""Tests for `agnes init` orchestrator command."""

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


from cli.commands.init import init_app  # noqa: E402

import pytest  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _home_in_tmp(tmp_path_factory, monkeypatch):
    """Redirect HOME into tmp for every test in this file.

    `agnes init` ends by installing a launcher shortcut into the shell rc
    file under $HOME (cli/lib/shortcut.py). Without this, any test that
    reaches that step appends a per-test marker block to the developer's
    real ~/.zshrc. Tests that assert on shortcut behavior override HOME
    again with their own fake home — this default just guarantees no test
    can forget. The guard in tests/conftest.py fails leaking tests loudly.
    """
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


def _make_api_get():
    """Build a stub api_get fn that returns canned responses for every endpoint
    `agnes init` and the inner `run_pull` touch.

    Returned closure is suitable for monkeypatching both
    `cli.commands.init.api_get` and `cli.lib.pull.api_get` so the verify-PAT
    call from init AND the manifest+memory-bundle calls from pull all
    succeed in tests.
    """
    from unittest.mock import MagicMock

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/catalog/tables":
            resp.json.return_value = []
        elif path == "/api/welcome":
            resp.json.return_value = {
                "content": "# Test CLAUDE.md\n\nUse `agnes pull`.\n",
            }
        elif path == "/api/sync/manifest":
            resp.json.return_value = {"tables": {}}
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        else:
            resp.json.return_value = {}
        # raise_for_status is a no-op MagicMock by default — fine for 200s.
        return resp

    return _api_get


def test_init_help():
    result = runner.invoke(init_app, ["--help"])
    assert result.exit_code == 0
    assert "--server-url" in _clean(result.output)
    assert "--token" in _clean(result.output)
    assert "--force" in _clean(result.output)
    assert "--workspace" in _clean(result.output)


def test_init_writes_expected_files(tmp_path, monkeypatch):
    """Mocked end-to-end: init writes CLAUDE.md, settings.json, AGNES_WORKSPACE.md."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://test.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "CLAUDE.md").exists()
    assert "agnes pull" in (tmp_path / "CLAUDE.md").read_text()
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".claude" / "CLAUDE.local.md").exists()
    assert (tmp_path / "AGNES_WORKSPACE.md").exists()
    # run_pull always creates the analytics.duckdb file (load-bearing).
    assert (tmp_path / "user" / "duckdb" / "analytics.duckdb").exists()
    # init anchors the workspace root in config so `agnes push` (and its
    # SessionEnd hook) can find the Claude Code session folder.
    from cli.config import get_workspace_root

    assert get_workspace_root() == str(tmp_path.resolve())


def test_init_no_dead_dirs_zero_grants(tmp_path, monkeypatch):
    """Zero grants -> no .claude/rules, no server/parquet, no user/sessions."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
        ],
    )
    for forbidden in [
        "data/parquet",
        "data/duckdb",
        "data/metadata",
        "user/artifacts",
        "user/sessions",
        "server/parquet",
        ".claude/rules",
    ]:
        assert not (tmp_path / forbidden).exists(), f"forbidden created: {forbidden}"


def test_init_force_preserves_local_md(tmp_path, monkeypatch):
    """--force regenerates CLAUDE.md but never touches CLAUDE.local.md."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    # First init seeds the workspace + writes the default CLAUDE.local.md stub.
    r1 = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert r1.exit_code == 0, r1.output
    (tmp_path / ".claude" / "CLAUDE.local.md").write_text("# my notes")

    # Second init with --force must overwrite CLAUDE.md but leave the
    # operator-written CLAUDE.local.md alone.
    r2 = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
            "--force",
        ],
    )
    assert r2.exit_code == 0, r2.output
    assert "my notes" in (tmp_path / ".claude" / "CLAUDE.local.md").read_text()


def test_init_force_backs_up_existing_claude_md(tmp_path, monkeypatch):
    """Issue #164: --force overwrites CLAUDE.md, but the prior content
    must be preserved as `CLAUDE.md.bak.<timestamp>` so an operator who
    edited it can recover their notes. The backup carries an ISO
    timestamp so re-running --force in the same workspace doesn't
    clobber a prior backup.
    """
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    # Seed an existing CLAUDE.md the operator has edited.
    (tmp_path / "CLAUDE.md").write_text("# AI Data Analyst\n\nMy custom edits — must survive reinit.\n")

    r = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
            "--force",
        ],
    )
    assert r.exit_code == 0, r.output

    # Backup file: glob since the timestamp is dynamic.
    backups = list(tmp_path.glob("CLAUDE.md.bak.*"))
    assert len(backups) == 1, [p.name for p in backups]
    assert "must survive reinit" in backups[0].read_text()
    # The summary line names the backup so the operator can find it.
    assert "Backed up" in r.output, r.output


def test_init_force_prunes_old_claude_md_backups(tmp_path, monkeypatch):
    """#1476: repeated `--force` runs must not accumulate `.bak.*` files
    forever — retention caps the count at the module's `_MAX_BACKUPS_PER_FILE`."""
    from src.initial_workspace import _MAX_BACKUPS_PER_FILE

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    (tmp_path / "CLAUDE.md").write_text("# AI Data Analyst\n\ncurrent\n")
    for stamp in ["20260810T000000Z", "20260811T000000Z", "20260812T000000Z", "20260813T000000Z"]:
        (tmp_path / f"CLAUDE.md.bak.{stamp}").write_text(stamp)

    r = runner.invoke(
        init_app,
        ["--server-url", "http://x", "--token", "t", "--workspace", str(tmp_path), "--force"],
    )
    assert r.exit_code == 0, r.output

    backups = list(tmp_path.glob("CLAUDE.md.bak.*"))
    assert len(backups) == _MAX_BACKUPS_PER_FILE, [p.name for p in backups]


def test_init_deletes_bootstrap_token_file(tmp_path, monkeypatch):
    """#580 Finding 1: `agnes init` clears the transient `~/.agnes/token`
    once it has consumed it — the raw PAT must not linger in a plaintext
    file at the default umask after init. The authoritative copy lives in
    ~/.config/agnes/token.json (0o600).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    # Simulate the setup-prompt bootstrap: PAT written to ~/.agnes/token.
    bootstrap_dir = home / ".agnes"
    bootstrap_dir.mkdir()
    token_file = bootstrap_dir / "token"
    token_file.write_text("eyJ-bootstrap-pat\n", encoding="utf-8")

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token-file",
            str(token_file),
            "--workspace",
            str(tmp_path / "ws"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert not token_file.exists(), "~/.agnes/token should be deleted after init"


def test_init_token_file_unreadable_but_present_hard_fails(tmp_path, monkeypatch):
    """An ABSENT --token-file falls back to the saved credential (the file
    was consumed by an earlier init — benign). A file that EXISTS but cannot
    be read is a real error: silently falling back would authenticate with a
    possibly-expired saved credential and blame the server. A directory in
    the token-file's place triggers exactly that read failure portably.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    bad = home / ".agnes" / "token"
    bad.mkdir(parents=True)  # a DIRECTORY named token — read_text raises, path exists

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token-file",
            str(bad),
            "--workspace",
            str(tmp_path / "ws"),
        ],
    )
    assert result.exit_code == 1
    # The error renderer wraps long hints at terminal width, which can split
    # the phrase across lines — normalize whitespace before matching.
    assert "could not be read" in " ".join(result.output.split())


def test_init_token_file_strips_windows_powershell_bom(tmp_path, monkeypatch):
    """/home's Windows Step 4 command writes ~/.agnes/token via PowerShell;
    Windows PowerShell 5 emits UTF-8 *with BOM* for `-Encoding utf8` (older
    copies of the guide, or a user retyping the command with `utf8`). A plain
    utf-8 read keeps U+FEFF glued to the token — str.strip() does not remove
    it — and the bearer header is silently corrupted. The reader must strip
    the BOM so the stored credential is the clean token.
    """
    import json

    home = tmp_path / "home"
    home.mkdir()
    cfg_dir = tmp_path / "_cfg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    bootstrap_dir = home / ".agnes"
    bootstrap_dir.mkdir()
    token_file = bootstrap_dir / "token"
    token_file.write_bytes(b"\xef\xbb\xbf" + b"eyJ-bootstrap-pat\n")

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token-file",
            str(token_file),
            "--workspace",
            str(tmp_path / "ws"),
        ],
    )
    assert result.exit_code == 0, result.output
    saved = json.loads((cfg_dir / "token.json").read_text(encoding="utf-8"))
    stored = saved.get("token") or next(iter(saved.values()))
    assert stored == "eyJ-bootstrap-pat", f"BOM survived into the credential: {stored!r}"


def test_init_succeeds_when_bootstrap_token_absent(tmp_path, monkeypatch):
    """The bootstrap-token cleanup is best-effort: init must still succeed
    when ~/.agnes/token was never created (e.g. --token / AGNES_TOKEN path)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "inline-pat",
            "--workspace",
            str(tmp_path / "ws"),
        ],
    )
    assert result.exit_code == 0, result.output


def test_init_does_not_mark_bootstrap_session_private(tmp_path, monkeypatch):
    """The bootstrap session is deliberately NOT auto-marked private, even
    when init runs inside a Claude Code session (CLAUDE_CODE_SESSION_ID set).
    Marking a session private is exclusively the analyst's own deliberate
    action (`/agnes-private`); the PAT pasted by the setup-prompt heredoc is
    protected by push-time JWT redaction instead, so the setup transcript
    uploads like any other session. (Reverts the auto-mark half of #771;
    the redaction half stays.)"""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "bootstrap-sid-123")
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    workspace = tmp_path / "ws"
    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://test.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(workspace),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "marked private" not in result.output

    from cli.lib.private_list import is_private

    assert not is_private(workspace, "bootstrap-sid-123")
    assert not (workspace / ".claude" / "agnes-sessions-private.txt").exists()


def test_init_does_not_mark_private_when_no_session_id(tmp_path, monkeypatch):
    """Outside a Claude Code session (no CLAUDE_CODE_SESSION_ID), there is no
    session to mark — init must not create a private-list entry or fail."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    workspace = tmp_path / "ws"
    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://test.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(workspace),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "marked private" not in result.output
    assert not (workspace / ".claude" / "agnes-sessions-private.txt").exists()


def test_init_partial_state_friendly_exit(tmp_path, monkeypatch):
    """CLAUDE.md exists with marker but no settings.json -> friendly hint, exit 1."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    workspace = tmp_path
    (workspace / "CLAUDE.md").write_text("# AI Data Analyst\n")
    # Without --force, init should refuse and print a hint
    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(workspace),
        ],
    )
    assert result.exit_code == 1
    assert "Traceback" not in (_clean(result.output) + _clean(result.stderr or ""))


def test_init_auth_failed_on_401(tmp_path, monkeypatch):
    """PAT verify endpoint returns 401 -> auth_failed typed error, exit 1."""
    from unittest.mock import MagicMock

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 401
        resp.json.return_value = {"detail": "Invalid token"}
        return resp

    monkeypatch.setattr("cli.commands.init.api_get", _api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "bad-pat",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    output = result.output + (result.stderr or "")
    assert "Traceback" not in output
    # Typed-error envelope should mention the kind or the actionable hint.
    assert ("auth_failed" in output) or ("Token expired" in output) or ("Token format invalid" in output)


def test_init_server_unreachable_on_connect_error(tmp_path, monkeypatch):
    """Network failure during verify -> server_unreachable typed error, exit 1."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))

    def _api_get(path, *args, **kwargs):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr("cli.commands.init.api_get", _api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://unreachable.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    output = result.output + (result.stderr or "")
    assert "Traceback" not in output
    assert ("server_unreachable" in output) or ("Cannot reach" in output)


def test_init_manifest_unauthorized_when_pull_records_manifest_error(tmp_path, monkeypatch):
    """Manifest stage fails -> manifest_unauthorized typed error, exit 1.

    Reproduces the I1 review finding: `run_pull` records per-stage failures
    into `result.errors` rather than raising. Without the post-pull error
    inspection, init would silently exit 0 with a partially-set-up workspace.
    """
    from unittest.mock import MagicMock
    from cli.lib.pull import PullResult

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))

    # init's verify-PAT call succeeds; welcome-fetch succeeds.
    def _init_api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/welcome":
            resp.json.return_value = {"content": "# Test\n"}
        else:
            resp.json.return_value = []
        return resp

    monkeypatch.setattr("cli.commands.init.api_get", _init_api_get, raising=False)

    # run_pull returns a PullResult carrying a manifest-stage error.
    def _fake_run_pull(server_url, token, workspace, *, dry_run=False):
        result = PullResult()
        result.errors.append({"stage": "manifest", "error": "401 Unauthorized"})
        return result

    monkeypatch.setattr("cli.commands.init.run_pull", _fake_run_pull, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    output = result.output + (result.stderr or "")
    assert "Traceback" not in output
    assert ("manifest_unauthorized" in output) or ("Manifest fetch failed" in output)


def test_init_default_skips_materialized_and_reports_note(tmp_path, monkeypatch):
    """Onboarding-speed default flip: plain `agnes init` (no flag) skips
    materialized-mode tables on the first pull and tells the user how to
    get them, so a single multi-GB scheduled-query parquet can't stall an
    otherwise-instant first-time install."""
    from cli.lib.pull import PullResult

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)

    captured = {}

    def _fake_run_pull(server_url, token, workspace, *, dry_run=False, skip_materialize=False, show_progress=False):
        captured["skip_materialize"] = skip_materialize
        result = PullResult()
        # Modelled on what the real `run_pull` produces: the skip branch in
        # `cli/lib/pull.py` `continue`s BEFORE the `non_remote_total` counter,
        # so an instance whose only distributable rows are materialized yields
        # parquets_total == 0 and materialized_skipped == 3. The earlier fake
        # set parquets_total=3 with tables_updated=0, a combination
        # skip_materialize=True can never produce — which is why the summary
        # bug it was meant to guard went unnoticed.
        result.parquets_total = 0
        result.tables_updated = 0
        result.materialized_skipped = 3
        return result

    monkeypatch.setattr("cli.commands.init.run_pull", _fake_run_pull, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["skip_materialize"] is True
    assert "materialized row(s) skipped by default" in result.output
    assert "3 materialized row(s) skipped" in result.output
    assert "agnes pull" in result.output
    assert "agnes init --materialize" in result.output


def test_init_does_not_claim_skipped_rows_when_there_are_none(tmp_path, monkeypatch):
    """The note counts rows actually left alone, not rows fetched.

    `parquets_total` is the number of non-remote tables the run CONSIDERED, so
    reading it as "skipped" told an analyst with three ordinary tables and no
    materialized ones that three rows had been skipped — while the same three
    were sitting in their workspace."""
    from cli.lib.pull import PullResult

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.setattr("cli.commands.init.api_get", _make_api_get(), raising=False)

    def _fake_run_pull(server_url, token, workspace, *, dry_run=False, skip_materialize=False, show_progress=False):
        result = PullResult()
        result.parquets_total = 3
        result.tables_updated = 3
        result.materialized_skipped = 0
        return result

    monkeypatch.setattr("cli.commands.init.run_pull", _fake_run_pull, raising=False)

    result = runner.invoke(
        init_app,
        ["--server-url", "http://x", "--token", "t", "--workspace", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "skipped" not in result.output
    assert "3/3 local table(s) fetched" in result.output


def test_init_materialize_flag_forces_full_pull(tmp_path, monkeypatch):
    """`--materialize` overrides the skip-by-default and passes
    skip_materialize=False through to run_pull, restoring the pre-flip
    full first pull."""
    from cli.lib.pull import PullResult

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)

    captured = {}

    def _fake_run_pull(server_url, token, workspace, *, dry_run=False, skip_materialize=False, show_progress=False):
        captured["skip_materialize"] = skip_materialize
        result = PullResult()
        result.parquets_total = 3
        result.tables_updated = 3
        return result

    monkeypatch.setattr("cli.commands.init.run_pull", _fake_run_pull, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
            "--materialize",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["skip_materialize"] is False
    # Reworded with the counter fix: `parquets_total` counts non-remote
    # tables considered, not materialized rows, and the old phrasing was the
    # same conflation that produced the wrong "skipped" count.
    assert "3/3 local table(s) fetched" in result.output


def test_init_explicit_skip_materialize_flag_still_works(tmp_path, monkeypatch):
    """`--skip-materialize` (the pre-flip flag spelling) is preserved for
    backward compatibility with existing scripts/docs — it's a no-op today
    since it matches the new default, but must not error."""
    result = runner.invoke(init_app, ["--help"])
    assert result.exit_code == 0
    assert "--skip-materialize" in _clean(result.output)
    assert "--materialize" in _clean(result.output)


def test_init_uses_explicit_token_arg_not_stale_disk_token(tmp_path, monkeypatch):
    """Regression for Devin Review finding on init.py:99.

    Repro: a prior `agnes init` left a stale token in
    `~/.config/agnes/token.json`. The new run passes a fresh token via
    `--token`. Pre-fix, step 2's PAT-verify call read the on-disk token
    first and only fell back to the env var — so the explicit `--token`
    arg was silently ignored, the verify ran with the stale token, and
    init failed 401 with a confusing 'token expired' error even though
    the supplied token was valid.

    Fix: a ContextVar-based override (set by `_override_server_env`)
    short-circuits `get_token()` BEFORE the on-disk read.
    """
    import json
    from unittest.mock import MagicMock

    cfg_dir = tmp_path / "_cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))

    # Seed a stale token on disk — this is what the bug exposed: the verify
    # call would prefer this over the --token arg.
    token_file = cfg_dir / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "STALE-DO-NOT-USE",
                "email": "old@example.com",
            }
        ),
        encoding="utf-8",
    )

    captured = {"verify_token": None}

    def _api_get(path, *args, **kwargs):
        # Verify endpoint: snapshot whatever token cli.config.get_token()
        # returns at the moment of the call. If the override is wired
        # correctly, this will be the --token arg, not the stale disk
        # value.
        if path == "/api/catalog/tables":
            from cli.config import get_token

            captured["verify_token"] = get_token()
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/welcome":
            resp.json.return_value = {"content": "# Test\n"}
        elif path == "/api/sync/manifest":
            resp.json.return_value = {"tables": {}}
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        else:
            resp.json.return_value = []
        return resp

    monkeypatch.setattr("cli.commands.init.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "FRESH-PAT-FROM-USER",
            "--workspace",
            str(tmp_path / "ws"),
            "--force",
        ],
    )

    assert captured["verify_token"] == "FRESH-PAT-FROM-USER", (
        "Step 2 verify call must use the explicit --token arg, "
        f"not the stale on-disk token. Got: {captured['verify_token']!r}"
    )
    output = result.output + (result.stderr or "")
    assert "Traceback" not in output


def test_token_override_contextvar_does_not_leak_outside_block():
    """The override must be scoped to the `with` block — leaking it would
    poison subsequent `get_token()` calls (e.g. a long-running daemon
    that runs `agnes init` once and then `agnes pull` later in the same
    process)."""
    from cli.config import _with_token_override, get_token
    import os

    # Sandbox AGNES_CONFIG_DIR so the test's own config dir doesn't muddy
    # the assertion (get_token would fall through to AGNES_TOKEN env or
    # to None depending on host state).
    prior_env = os.environ.pop("AGNES_TOKEN", None)
    try:
        with _with_token_override("INSIDE"):
            assert get_token() == "INSIDE"
        # Outside the block: override cleared, falls through to file/env.
        # Without a config file or AGNES_TOKEN set, returns None.
        assert get_token() != "INSIDE"
    finally:
        if prior_env is not None:
            os.environ["AGNES_TOKEN"] = prior_env


import os  # noqa: E402


def _make_shortcut_env(tmp_path, monkeypatch, platform="linux"):
    """Set up a fake HOME with workspace, monkeypatch sys.platform.

    Also neutralizes ``shutil.which`` inside the shortcut module so the
    PATH-collision guard never sees the developer machine's real PATH;
    tests that exercise that guard install their own fake.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr("cli.lib.shortcut.shutil.which", lambda cmd: None)
    workspace = tmp_path / "MyWorkspace"
    workspace.mkdir()
    return home, workspace


def _bin(home):
    return home / ".local" / "bin"


def test_shortcut_installs_executable_script_into_local_bin(tmp_path, monkeypatch):
    """POSIX: the launcher is a #!/bin/sh script in ~/.local/bin, 0o755,
    carrying our ownership marker, cd-ing into the workspace and exec-ing
    claude with --permission-mode auto. No rc file is touched."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    script = _bin(home) / "myworkspace"
    assert script.exists()
    assert os.access(script, os.X_OK)
    content = script.read_text(encoding="utf-8")
    assert content.startswith("#!/bin/sh\n")
    assert "\n# >>> agnes launcher: myworkspace <<<\n" in content
    assert "# # >>>" not in content  # marker embedded verbatim, no double comment
    assert f'cd "{workspace}" && exec claude --permission-mode auto "$@"' in content
    assert not (home / ".zshrc").exists()
    assert not (home / ".bashrc").exists()


def test_shortcut_routes_via_bin_launcher_when_present(tmp_path, monkeypatch):
    """When bin/<word> exists and is executable, the script execs it (not bare
    `claude`) so the operator's welcome skill fires — IWT contract."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    bin_dir = workspace / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "myworkspace"
    launcher.write_text('#!/bin/bash\ncd workspace && claude --agent myworkspace "$@"\n')
    launcher.chmod(0o755)

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    content = (_bin(home) / "myworkspace").read_text(encoding="utf-8")
    assert f'exec "{launcher}" --permission-mode auto "$@"' in content


def test_shortcut_falls_back_to_claude_when_no_bin_launcher(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    content = (_bin(home) / "myworkspace").read_text(encoding="utf-8")
    assert "exec claude --permission-mode auto" in content
    assert "bin/myworkspace" not in content


def test_shortcut_idempotent_reinstall_overwrites_own_script(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)
    first = (_bin(home) / "myworkspace").read_text(encoding="utf-8")
    install_launcher_shortcut(workspace)
    assert (_bin(home) / "myworkspace").read_text(encoding="utf-8") == first
    assert list(_bin(home).iterdir()) == [_bin(home) / "myworkspace"]


def test_shortcut_never_overwrites_foreign_file_suffixes_ai(tmp_path, monkeypatch):
    """A user-owned ~/.local/bin/myworkspace must survive; we fall back to
    myworkspaceai."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    _bin(home).mkdir(parents=True)
    foreign = _bin(home) / "myworkspace"
    foreign.write_text("#!/bin/sh\necho user script\n")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert foreign.read_text() == "#!/bin/sh\necho user script\n"
    assert (_bin(home) / "myworkspaceai").exists()


def test_shortcut_skips_when_both_names_foreign(tmp_path, monkeypatch, capsys):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    _bin(home).mkdir(parents=True)
    (_bin(home) / "myworkspace").write_text("user\n")
    (_bin(home) / "myworkspaceai").write_text("user\n")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert (_bin(home) / "myworkspace").read_text() == "user\n"
    assert (_bin(home) / "myworkspaceai").read_text() == "user\n"
    assert "skip" in capsys.readouterr().err.lower()


def test_shortcut_avoids_shadowing_binary_on_path(tmp_path, monkeypatch):
    """Workspace named like a real command (e.g. `Node`) must not shadow it —
    the PATH-level guard kicks in and the script gets the ai suffix."""
    home, _ = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    workspace = tmp_path / "Node"
    workspace.mkdir()
    import cli.lib.shortcut as shortcut

    def fake_which(cmd):
        return "/usr/bin/node" if cmd == "node" else None

    monkeypatch.setattr(shortcut.shutil, "which", fake_which)
    shortcut.install_launcher_shortcut(workspace)

    assert not (_bin(home) / "node").exists()
    assert (_bin(home) / "nodeai").exists()


def test_shortcut_no_shortcut_flag_writes_nothing(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace, no_shortcut=True)

    assert not (_bin(home)).exists()
    assert not (home / ".bashrc").exists()


def test_shortcut_write_failure_does_not_raise(tmp_path, monkeypatch, capsys):
    """A write failure must not propagate out of install_launcher_shortcut —
    best-effort contract with `agnes init`."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    # Make ~/.local a file so mkdir of the bin dir fails.
    (home / ".local").write_text("not a dir")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)  # must not raise
    assert "could not install" in capsys.readouterr().err.lower()


def test_shortcut_sanitizes_workspace_name_with_special_chars(tmp_path, monkeypatch):
    """A workspace folder with spaces/dots yields a clean alphanumeric script
    name — special chars never leak into the filename or the script body."""
    home, _ = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    workspace = tmp_path / "My Team A.I."
    workspace.mkdir()

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    script = _bin(home) / "myteamai"
    assert script.exists()
    assert "# >>> agnes launcher: myteamai <<<" in script.read_text(encoding="utf-8")


def test_shortcut_skips_when_name_has_no_alphanumerics(tmp_path, monkeypatch, capsys):
    home, _ = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    workspace = tmp_path / "___"
    workspace.mkdir()

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert not (_bin(home)).exists()
    assert "skipping shortcut" in capsys.readouterr().err


def test_shortcut_second_workspace_gets_own_script(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    other = tmp_path / "OtherTeam"
    other.mkdir()

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)
    install_launcher_shortcut(other)

    assert (_bin(home) / "myworkspace").exists()
    assert (_bin(home) / "otherteam").exists()


def test_launcher_word_avoids_shell_builtin_collision():
    """A workspace named 'Test' would produce 'test' which shadows the POSIX
    built-in; the guard appends 'ai' so the script name is 'testai'."""
    from cli.lib import shortcut as sc

    assert sc._launcher_word("Test") == "testai"
    assert sc._launcher_word("CD") == "cdai"
    # Non-built-in names are unaffected.
    assert sc._launcher_word("MyWorkspace") == "myworkspace"


def test_launcher_word_avoids_toolchain_command_collision():
    """A workspace named 'Agnes' would produce 'agnes' — a launcher of that
    name shadows the agnes CLI binary itself, breaking every subsequent CLI
    command (#783). Same for 'claude'. The guard appends 'ai' exactly as it
    does for shell built-ins."""
    from cli.lib import shortcut as sc

    assert sc._launcher_word("Agnes") == "agnesai"
    assert sc._launcher_word("agnes") == "agnesai"
    assert sc._launcher_word("Claude") == "claudeai"
    # Sanitization happens before the guard: dots strip down to 'agnes'.
    assert sc._launcher_word("A.g.n.e.s") == "agnesai"


def test_shortcut_never_shadows_agnes_cli(tmp_path, monkeypatch):
    """Installing into a workspace named 'Agnes' (the default brand) must not
    create a script named `agnes` — that would hijack every `agnes` CLI call
    into a Claude chat session (#783)."""
    home, _ = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    workspace = tmp_path / "Agnes"
    workspace.mkdir()

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert not (_bin(home) / "agnes").exists()
    assert (_bin(home) / "agnesai").exists()


def test_install_removes_legacy_rc_blocks(tmp_path, monkeypatch):
    """Old marked function blocks are stripped from ~/.zshrc and ~/.bashrc on
    install; user lines survive. The .zshrc variant has the blank line the
    legacy writer inserted; the .bashrc variant sits flush under a user line
    — stripping must not glue user lines together in either layout."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    block = (
        "# >>> agnes launcher: myworkspace <<<\n"
        'function myworkspace {\n  cd "/old/path" && claude --permission-mode auto "$@"\n}\n'
        "# <<< agnes launcher: myworkspace >>>\n"
    )
    (home / ".zshrc").write_text(f"# user line above\n\n{block}# user line below\n", encoding="utf-8")
    (home / ".bashrc").write_text(f"# user line above\n{block}# user line below\n", encoding="utf-8")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert (home / ".zshrc").read_text(encoding="utf-8") == "# user line above\n# user line below\n"
    assert (home / ".bashrc").read_text(encoding="utf-8") == "# user line above\n# user line below\n"
    assert (_bin(home) / "myworkspace").exists()


def test_install_removes_pre783_raw_word_block(tmp_path, monkeypatch):
    """A pre-collision-guard install wrote the block under the raw word
    (`function agnes` for a workspace named Agnes) — it must be cleaned even
    though the chosen script name is the suffixed one."""
    home, _ = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    workspace = tmp_path / "Agnes"
    workspace.mkdir()
    (home / ".zshrc").write_text(
        "# >>> agnes launcher: agnes <<<\n"
        'function agnes {\n  cd x && claude --permission-mode auto "$@"\n}\n'
        "# <<< agnes launcher: agnes >>>\n",
        encoding="utf-8",
    )

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert "agnes launcher" not in (home / ".zshrc").read_text(encoding="utf-8")
    assert (_bin(home) / "agnesai").exists()


def test_install_warns_on_foreign_shadowing_function(tmp_path, monkeypatch, capsys):
    """An unmarked `function myworkspace` in an rc file would shadow the new
    PATH script in interactive shells — warn, never edit."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    (home / ".zshrc").write_text("function myworkspace {\n  echo mine\n}\n", encoding="utf-8")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert "function myworkspace" in (home / ".zshrc").read_text(encoding="utf-8")
    assert "shadow" in capsys.readouterr().err.lower()


def test_shortcut_collision_skip_preserves_legacy_block(tmp_path, monkeypatch):
    """When both candidate script names are foreign, the install must skip
    WITHOUT stripping a still-working legacy rc-function launcher — removing
    it would leave the user with no launcher at all."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    _bin(home).mkdir(parents=True)
    (_bin(home) / "myworkspace").write_text("user\n")
    (_bin(home) / "myworkspaceai").write_text("user\n")
    legacy = (
        "# >>> agnes launcher: myworkspace <<<\n"
        "function myworkspace {\n  cd x\n}\n"
        "# <<< agnes launcher: myworkspace >>>\n"
    )
    (home / ".zshrc").write_text(legacy, encoding="utf-8")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert (home / ".zshrc").read_text(encoding="utf-8") == legacy
    assert (_bin(home) / "myworkspace").read_text() == "user\n"


def test_migrate_blocked_when_collision_prevents_install(tmp_path, monkeypatch):
    """Legacy block + both names foreign → migrate reports 'blocked' and the
    legacy launcher keeps working."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    _bin(home).mkdir(parents=True)
    (_bin(home) / "myworkspace").write_text("user\n")
    (_bin(home) / "myworkspaceai").write_text("user\n")
    legacy = (
        "# >>> agnes launcher: myworkspace <<<\n"
        "function myworkspace {\n  cd x\n}\n"
        "# <<< agnes launcher: myworkspace >>>\n"
    )
    (home / ".zshrc").write_text(legacy, encoding="utf-8")

    from cli.lib.shortcut import migrate_launcher_shortcut

    assert migrate_launcher_shortcut(workspace) == "blocked"
    assert (home / ".zshrc").read_text(encoding="utf-8") == legacy


def test_migrate_absent_is_noop(tmp_path, monkeypatch):
    """No legacy block and no script → migrate must not install anything —
    preserves the `agnes init --no-shortcut` opt-out."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")

    from cli.lib.shortcut import migrate_launcher_shortcut

    assert migrate_launcher_shortcut(workspace) == "absent"
    assert not (_bin(home)).exists()


def test_migrate_from_legacy_rc_block(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    (home / ".zshrc").write_text(
        "# >>> agnes launcher: myworkspace <<<\n"
        "function myworkspace {\n  cd x\n}\n"
        "# <<< agnes launcher: myworkspace >>>\n",
        encoding="utf-8",
    )

    from cli.lib.shortcut import migrate_launcher_shortcut

    assert migrate_launcher_shortcut(workspace) == "migrated"
    assert "agnes launcher" not in (home / ".zshrc").read_text(encoding="utf-8")
    assert (_bin(home) / "myworkspace").exists()


def test_migrate_converges_existing_script(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")

    from cli.lib.shortcut import install_launcher_shortcut, migrate_launcher_shortcut

    install_launcher_shortcut(workspace)
    assert migrate_launcher_shortcut(workspace) == "converged"
    assert (_bin(home) / "myworkspace").exists()


def test_shortcut_windows_writes_cmd_shim(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "win32")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    shim = _bin(home) / "myworkspace.cmd"
    assert shim.exists()
    # Reading raw bytes preserves the shim's exact line endings (\r\n): the
    # shim must carry \r\n, not \r\r\n from universal-newline translation.
    # Path.read_text(newline=...) isn't available until Python 3.13, so we
    # decode the bytes ourselves instead.
    content = shim.read_bytes().decode("utf-8")
    assert "\r\r" not in content
    assert "\r\n" in content
    content = content.replace("\r\n", "\n")
    assert content.startswith("@echo off")
    # `::` label-comment, NOT `rem`: cmd.exe applies redirection parsing to
    # rem lines, so `rem >>> ... <<<` would error on every launch.
    assert ":: >>> agnes launcher: myworkspace <<<" in content
    assert "rem >>>" not in content
    assert f'cd /d "{workspace}"' in content
    assert "claude --permission-mode auto %*" in content


def test_shortcut_windows_cleans_both_powershell_profiles(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "win32")
    legacy = (
        "# >>> agnes launcher: myworkspace <<<\n"
        "function myworkspace {\n  Set-Location x\n}\n"
        "# <<< agnes launcher: myworkspace >>>\n"
    )
    from cli.lib.shortcut import _ps_profile_paths

    for profile in _ps_profile_paths(home):
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(legacy, encoding="utf-8")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    for profile in _ps_profile_paths(home):
        assert "agnes launcher" not in profile.read_text(encoding="utf-8")
    assert (_bin(home) / "myworkspace.cmd").exists()


def test_shortcut_windows_routes_via_bin_cmd_launcher(tmp_path, monkeypatch):
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "win32")
    bin_dir = workspace / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "myworkspace.cmd"
    launcher.write_text("@echo off\r\nclaude --agent myworkspace %*\r\n")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    content = (_bin(home) / "myworkspace.cmd").read_text(encoding="utf-8")
    assert f'call "{launcher}" --permission-mode auto %*' in content


def test_init_no_shortcut_flag_accepted(tmp_path, monkeypatch):
    """agnes init --no-shortcut is accepted and exits 0 without installing a
    launcher script or touching rc files."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://test.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(tmp_path / "ws"),
            "--no-shortcut",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not (home / ".local" / "bin").exists()
    for rc in (home / ".zshrc", home / ".bashrc"):
        if rc.exists():
            assert "# >>> agnes launcher" not in rc.read_text(), f"{rc} contains launcher marker despite --no-shortcut"


def test_init_writes_shortcut_and_reports_it(tmp_path, monkeypatch):
    """Agnes init (without --no-shortcut) installs the script and reports it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    # Use a distinctive workspace name so we can assert on the word
    ws = tmp_path / "TestBrand"
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://test.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(ws),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (home / ".local" / "bin" / "testbrand").exists()
    assert "Shortcut" in result.output or "testbrand" in result.output


def test_init_absent_token_file_note_points_at_the_likely_cause(tmp_path, monkeypatch):
    """The sibling of the hard-fail case above. An absent --token-file stays a
    soft fallback on purpose — templates pass the flag unconditionally. But in
    expired-credential recovery, absent means the fresh token was never
    written, and the fallback then retries with the expired one, so the failure
    surfaces as a server-side auth error that reads like the server's fault.
    The note has to name that cause while we still know it
    (Devin Review on #1139).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.setenv("AGNES_TOKEN", "eyJ-from-env")
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    missing = home / ".agnes" / "token"  # never created

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token-file",
            str(missing),
            "--workspace",
            str(tmp_path / "ws"),
        ],
    )

    # Soft fallback preserved — it still ran with AGNES_TOKEN.
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())  # the renderer wraps at terminal width
    assert "does not exist" in out
    assert "expired" in out, "the note does not mention the expired-credential case"
    assert "/home" in out, "the note does not point back at the token step"


# ---------------------------------------------------------------------------
# Unsafe-workspace guard — `agnes init` refuses $HOME, /, /tmp and other
# system directories so the install prompt no longer needs a prose decision
# tree; the refusal (and its hint) live in the CLI itself.
# ---------------------------------------------------------------------------


def test_init_refuses_home_directory(tmp_path, monkeypatch):
    """--workspace pointing at $HOME is refused before any network or
    filesystem side effect (no token consumed, nothing written)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))

    result = runner.invoke(
        init_app,
        ["--server-url", "http://x", "--token", "t", "--workspace", str(home)],
    )
    assert result.exit_code != 0
    out = " ".join(_clean(result.output).split())
    assert "unsafe_workspace" in out
    assert "workspace folder" in out, "hint must tell the user the fix"
    assert not (home / "CLAUDE.md").exists()
    assert not (home / ".claude").exists()


def test_init_refuses_cwd_home_directory(tmp_path, monkeypatch):
    """Same refusal when $HOME is the implicit cwd (no --workspace)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.chdir(home)

    result = runner.invoke(init_app, ["--server-url", "http://x", "--token", "t"])
    assert result.exit_code != 0
    assert "unsafe_workspace" in _clean(result.output)


def test_init_refuses_system_directories(tmp_path, monkeypatch):
    """The POSIX system list is refused — including macOS' /tmp →
    /private/tmp symlink resolution."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))

    for target in ("/", "/tmp", "/etc", "/usr", "/var", "/opt"):
        result = runner.invoke(
            init_app,
            ["--server-url", "http://x", "--token", "t", "--workspace", target],
        )
        assert result.exit_code != 0, f"{target} was not refused"
        assert "unsafe_workspace" in _clean(result.output), target


def test_init_allows_subdirectory_of_home(tmp_path, monkeypatch):
    """Only exact matches are unsafe — a workspace folder under $HOME (the
    documented default ~/Desktop/<brand>) initializes normally."""
    home = tmp_path / "home"
    ws = home / "Desktop" / "Agnes"
    ws.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    result = runner.invoke(
        init_app,
        ["--server-url", "http://x", "--token", "t", "--workspace", str(ws)],
    )
    assert result.exit_code == 0, result.output
