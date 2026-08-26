"""Tests for `agnes onboard` — the deterministic install orchestrator.

The thin install prompt hands orchestration to the CLI: the agent pastes
~20 lines that install the CLI and run `agnes onboard`, and everything the
old English "program" did (dir check, init, catalog smoke, preflight,
marketplace, diagnose, summary) happens here, deterministically.

These tests pin the two halves that must never drift:

* the workspace-dir gate matrix (unsafe / prepared / unrelated), and
* the step orchestration contract — ordering, fatal-vs-continue semantics,
  the ``--json`` report shape, and the promise that no token ever reaches
  stdout.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cli.commands.onboard as onb
from cli.commands.onboard import onboard_app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


runner = CliRunner()

# Captured before any fixture stubs it out (see `test_server_url_missing_fails_fast`).
_REAL_RESOLVE_SERVER_URL = onb._resolve_server_url


# --------------------------------------------------------------------------- #
# Step 0 — workspace-dir classification matrix
# --------------------------------------------------------------------------- #


def test_classify_empty_dir_is_prepared(tmp_path):
    verdict, detail = onb.classify_workspace_dir(tmp_path)
    assert verdict == onb.DIR_PREPARED
    assert detail == ""


def test_classify_allowlisted_artefacts_only_is_prepared(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".agnes").mkdir()
    (tmp_path / "AGNES_WORKSPACE.md").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "bash.exe.stackdump").write_text("x", encoding="utf-8")
    verdict, _ = onb.classify_workspace_dir(tmp_path)
    assert verdict == onb.DIR_PREPARED


def test_classify_unrelated_content(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / "taxes-2025.xlsx").write_text("x", encoding="utf-8")
    (tmp_path / "photos").mkdir()
    verdict, detail = onb.classify_workspace_dir(tmp_path)
    assert verdict == onb.DIR_UNRELATED
    # The summary names what is actually in the way (allowlisted entries
    # are not "in the way" and must not be listed).
    assert "taxes-2025.xlsx" in detail
    assert "photos" in detail
    assert ".claude" not in detail


def test_classify_home_is_unsafe(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    verdict, detail = onb.classify_workspace_dir(tmp_path)
    assert verdict == onb.DIR_UNSAFE
    assert "home" in detail


def test_classify_filesystem_root_is_unsafe():
    verdict, detail = onb.classify_workspace_dir(Path(Path.cwd().anchor))
    assert verdict == onb.DIR_UNSAFE
    assert detail


@pytest.mark.parametrize("system_dir", ["/etc", "/usr", "/var", "/opt", "/bin"])
def test_classify_system_dirs_are_unsafe(system_dir):
    p = Path(system_dir)
    if not p.exists():  # non-POSIX runner
        pytest.skip(f"{system_dir} not present on this platform")
    verdict, _ = onb.classify_workspace_dir(p)
    assert verdict == onb.DIR_UNSAFE


# --------------------------------------------------------------------------- #
# Fixtures: stub every step so the orchestration contract is what's under test
# --------------------------------------------------------------------------- #


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    """Replace all steps with recorders; return the mutable call log."""
    calls: list[str] = []

    def _rec(name: str, status: str = "ok", detail: str = "stub"):
        def _fn(*_a, **_kw):
            calls.append(name)
            return {"step": name, "status": status, "detail": detail}

        return _fn

    def _init(*_a, **_kw):
        calls.append("init")
        return [{"step": "init", "status": "ok", "detail": "stub"}]

    monkeypatch.setattr(onb, "_step_init", _init)
    monkeypatch.setattr(onb, "_step_catalog", _rec("catalog"))
    monkeypatch.setattr(onb, "_step_preflight", _rec("preflight"))
    monkeypatch.setattr(onb, "_step_marketplace", _rec("marketplace"))
    monkeypatch.setattr(onb, "_step_diagnose", _rec("diagnose"))
    monkeypatch.setattr(onb, "_fetch_connectors", list)
    monkeypatch.setattr(onb, "_resolve_server_url", lambda explicit: explicit or "https://agnes.example.com")
    return calls


# --------------------------------------------------------------------------- #
# Step 0 gate — CLI behaviour
# --------------------------------------------------------------------------- #


def test_unsafe_dir_refuses_with_distinct_exit_code(tmp_path, monkeypatch, stubbed):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path)])
    assert result.exit_code == onb.EXIT_UNSAFE_DIR
    out = _clean(result.output)
    assert "AGNES_WORKSPACE.md" in out  # explains what would be scattered
    assert "cd" in out  # tells the caller what to do instead
    assert stubbed == []  # nothing ran


def test_unsafe_dir_never_creates_anything(tmp_path, monkeypatch, stubbed):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    runner.invoke(onboard_app, ["--workspace", str(tmp_path)])
    assert list(tmp_path.iterdir()) == []


def test_unrelated_dir_asks_for_accept_dir(tmp_path, stubbed):
    (tmp_path / "taxes.xlsx").write_text("x", encoding="utf-8")
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path)])
    assert result.exit_code == onb.EXIT_UNRELATED_DIR
    assert result.exit_code != onb.EXIT_UNSAFE_DIR
    out = _clean(result.output)
    assert "--accept-dir" in out
    assert "taxes.xlsx" in out
    assert stubbed == []


def test_accept_dir_overrides_the_unrelated_gate(tmp_path, stubbed):
    (tmp_path / "taxes.xlsx").write_text("x", encoding="utf-8")
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path), "--accept-dir"])
    assert result.exit_code == 0, result.output
    assert stubbed == ["init", "catalog", "preflight", "marketplace", "diagnose"]


def test_prepared_dir_is_announced(tmp_path, stubbed):
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert str(tmp_path) in _clean(result.output)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def test_steps_run_in_the_designed_order(tmp_path, stubbed):
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert stubbed == ["init", "catalog", "preflight", "marketplace", "diagnose"]


def test_init_failure_is_fatal(tmp_path, monkeypatch, stubbed):
    def _boom(*_a, **_kw):
        stubbed.append("init")
        raise RuntimeError("server unreachable")

    monkeypatch.setattr(onb, "_step_init", _boom)
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path)])
    assert result.exit_code == onb.EXIT_INIT_FAILED
    assert stubbed == ["init"]  # nothing after init ran
    assert "server unreachable" in _clean(result.output)


def test_non_init_failure_does_not_abort_the_rest(tmp_path, monkeypatch, stubbed):
    def _boom(*_a, **_kw):
        stubbed.append("marketplace")
        raise RuntimeError("git clone failed")

    monkeypatch.setattr(onb, "_step_marketplace", _boom)
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path)])
    # Reported, not fatal: diagnose still ran and the NEXT block still prints.
    assert stubbed == ["init", "catalog", "preflight", "marketplace", "diagnose"]
    assert result.exit_code == 0, result.output
    out = _clean(result.output)
    assert "git clone failed" in out
    assert "NEXT:" in out


def test_preflight_failure_is_reported_but_not_fatal(tmp_path, monkeypatch, stubbed):
    monkeypatch.setattr(
        onb,
        "_step_preflight",
        lambda *_a, **_kw: (stubbed.append("preflight"), {"step": "preflight", "status": "failed", "detail": "no git"})[
            1
        ],
    )
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert stubbed[-1] == "diagnose"
    assert "no git" in _clean(result.output)


def test_summary_ends_with_a_next_block(tmp_path, stubbed):
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path)])
    out = _clean(result.output)
    assert "NEXT:" in out
    assert "Claude Code" in out
    assert "restart" in out.lower()


def test_connectors_section_lists_available_connectors(tmp_path, monkeypatch, stubbed):
    monkeypatch.setattr(
        onb,
        "_fetch_connectors",
        lambda: [
            {"slug": "connector-asana", "display_name": "Asana", "short_summary": "Tasks and projects."},
        ],
    )
    out = _clean(runner.invoke(onboard_app, ["--workspace", str(tmp_path)]).output)
    assert "Asana" in out
    assert "Tasks and projects." in out
    # The follow-up hint points at surfaces that exist on every install.
    assert "agnes connectors show" in out


def test_connectors_section_skipped_when_the_api_fails(tmp_path, monkeypatch, stubbed):
    monkeypatch.setattr(onb, "_fetch_connectors", lambda: None)
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    # No empty "this instance offers nothing" section (the tmp_path name can
    # itself contain the word, so match the section header, not the word).
    assert "Available connectors" not in _clean(result.output)


# --------------------------------------------------------------------------- #
# --json report
# --------------------------------------------------------------------------- #


def test_json_report_shape(tmp_path, monkeypatch, stubbed):
    monkeypatch.setattr(
        onb,
        "_fetch_connectors",
        lambda: [{"slug": "connector-asana", "display_name": "Asana", "short_summary": "Tasks."}],
    )
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == onb.SCHEMA_VERSION
    assert payload["workspace"] == str(tmp_path.resolve())
    assert payload["dir_status"] == onb.DIR_PREPARED
    assert payload["overall"] == "ok"
    assert [s["step"] for s in payload["steps"]] == [
        "init",
        "catalog",
        "preflight",
        "marketplace",
        "diagnose",
    ]
    assert payload["connectors"][0]["slug"] == "connector-asana"
    assert "next" in payload


def test_json_report_marks_degraded_on_a_failed_step(tmp_path, monkeypatch, stubbed):
    monkeypatch.setattr(
        onb,
        "_step_marketplace",
        lambda *_a, **_kw: (
            stubbed.append("marketplace"),
            {"step": "marketplace", "status": "failed", "detail": "boom"},
        )[1],
    )
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path), "--json"])
    payload = json.loads(result.stdout)
    assert payload["overall"] == "degraded"
    assert result.exit_code == 0  # degraded is reported, not fatal


def test_json_gate_refusal_is_still_json(tmp_path, stubbed):
    (tmp_path / "taxes.xlsx").write_text("x", encoding="utf-8")
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path), "--json"])
    assert result.exit_code == onb.EXIT_UNRELATED_DIR
    payload = json.loads(result.stdout)
    assert payload["dir_status"] == onb.DIR_UNRELATED
    assert payload["steps"] == []
    # Both surfaces carry the same instruction.
    assert payload["next"].startswith("NEXT:")


def test_json_init_failure_report(tmp_path, monkeypatch, stubbed):
    monkeypatch.setattr(onb, "_step_init", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("401")))
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path), "--json"])
    assert result.exit_code == onb.EXIT_INIT_FAILED
    payload = json.loads(result.stdout)
    assert payload["overall"] == "failed"
    assert payload["steps"][0]["step"] == "init"
    assert payload["steps"][0]["status"] == "failed"


# --------------------------------------------------------------------------- #
# Idempotency: an initialized workspace converges via `agnes update`
# --------------------------------------------------------------------------- #


def test_initialized_workspace_converges_instead_of_reinitializing(tmp_path, monkeypatch):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text("override: false\n", encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(onb, "_run_init", lambda **kw: seen.append("init"))
    monkeypatch.setattr(onb, "_run_update", lambda **kw: (seen.append("update"), {"steps": []})[1])

    rows = onb._step_init(tmp_path, server_url="https://agnes.example.com", quiet=True)
    assert seen == ["update"]
    assert rows[0]["step"] == "init"
    assert rows[0]["status"] == "already-configured"


def test_fresh_workspace_runs_init(tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(onb, "_run_init", lambda **kw: seen.append("init"))
    monkeypatch.setattr(onb, "_run_update", lambda **kw: seen.append("update"))
    monkeypatch.setattr(onb, "_bootstrap_token_path", lambda: tmp_path / "missing-token")

    rows = onb._step_init(tmp_path, server_url="https://agnes.example.com", quiet=True)
    assert seen == ["init"]
    assert rows[0]["status"] == "ok"


def test_leftover_bootstrap_token_is_flagged_loudly(tmp_path, monkeypatch):
    leftover = tmp_path / "token"
    leftover.write_text("pat-secret-value\n", encoding="utf-8")
    monkeypatch.setattr(onb, "_run_init", lambda **kw: None)
    monkeypatch.setattr(onb, "_bootstrap_token_path", lambda: leftover)

    rows = onb._step_init(tmp_path, server_url="https://agnes.example.com", quiet=True)
    warn = [r for r in rows if r["step"] == "bootstrap-token"]
    assert warn and warn[0]["status"] == "warning"
    # The warning names the FILE, never its contents.
    assert "pat-secret-value" not in json.dumps(rows)


def test_consumed_bootstrap_token_produces_no_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(onb, "_run_init", lambda **kw: None)
    monkeypatch.setattr(onb, "_bootstrap_token_path", lambda: tmp_path / "gone")
    rows = onb._step_init(tmp_path, server_url="https://agnes.example.com", quiet=True)
    assert [r["step"] for r in rows] == ["init"]


def test_run_init_passes_every_init_parameter(monkeypatch):
    """`_run_init` calls the `agnes init` callback directly, so a new option
    on that callback would otherwise arrive as a raw ``OptionInfo`` default.
    Pin the full keyword set against the real signature."""
    from cli.commands.init import init as init_cmd

    captured: dict = {}
    monkeypatch.setattr("cli.commands.init.init", lambda **kw: captured.update(kw))
    onb._run_init(workspace=Path("/tmp/ws"), server_url="https://agnes.example.com")
    expected = set(inspect.signature(init_cmd).parameters)
    assert set(captured) == expected


# --------------------------------------------------------------------------- #
# Server URL resolution — no silent default
# --------------------------------------------------------------------------- #


def test_server_url_prefers_the_explicit_flag(monkeypatch):
    monkeypatch.setattr("cli.config.load_config", lambda: {"server": "https://saved.example.com"})
    monkeypatch.delenv("AGNES_SERVER", raising=False)
    assert onb._resolve_server_url("https://flag.example.com/") == "https://flag.example.com"


def test_server_url_falls_back_to_saved_config(monkeypatch):
    monkeypatch.delenv("AGNES_SERVER", raising=False)
    monkeypatch.setattr("cli.config.load_config", lambda: {"server": "https://saved.example.com"})
    assert onb._resolve_server_url(None) == "https://saved.example.com"


def test_server_url_missing_fails_fast(tmp_path, monkeypatch, stubbed):
    monkeypatch.delenv("AGNES_SERVER", raising=False)
    monkeypatch.setattr("cli.config.load_config", dict)
    monkeypatch.setattr(onb, "_resolve_server_url", _REAL_RESOLVE_SERVER_URL)
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path)])
    assert result.exit_code == onb.EXIT_CONFIG
    out = _clean(result.output)
    assert "--server-url" in out
    assert stubbed == []


# --------------------------------------------------------------------------- #
# Individual steps
# --------------------------------------------------------------------------- #


def test_catalog_empty_list_is_not_an_error(monkeypatch):
    monkeypatch.setattr(onb, "api_get_json", lambda path, **kw: {"tables": []})
    row = onb._step_catalog(quiet=True)
    assert row["status"] == "ok"
    assert "granted" in row["detail"]  # the "ask your admin" hint


def test_catalog_counts_visible_tables(monkeypatch):
    monkeypatch.setattr(onb, "api_get_json", lambda path, **kw: {"tables": [{"id": "a"}, {"id": "b"}]})
    row = onb._step_catalog(quiet=True)
    assert row["status"] == "ok"
    assert "2" in row["detail"]


def test_preflight_reports_missing_binaries_with_install_hints(monkeypatch):
    monkeypatch.setattr(onb.shutil, "which", lambda name: None)
    row = onb._step_preflight()
    assert row["status"] == "failed"
    detail = row["detail"]
    assert "git" in detail and "claude" in detail
    assert "https://docs.claude.com/claude-code" in detail
    # Per-OS hints for the platform we're on.
    assert any(tok in detail for tok in ("brew", "winget", "apt-get", "dnf", "npm"))


def test_preflight_ok_when_both_present(monkeypatch):
    monkeypatch.setattr(onb.shutil, "which", lambda name: f"/usr/bin/{name}")
    row = onb._step_preflight()
    assert row["status"] == "ok"


def test_marketplace_passes_every_refresh_parameter(monkeypatch, tmp_path):
    """Same sentinel hazard as `_run_init`: a Typer callback called as a plain
    function needs its FULL keyword set."""
    from cli.commands.refresh_marketplace import refresh_marketplace

    captured: dict = {}
    monkeypatch.setattr(
        "cli.commands.refresh_marketplace.refresh_marketplace",
        lambda **kw: captured.update(kw),
    )
    row = onb._step_marketplace(workspace=tmp_path, quiet=True)
    assert row["status"] == "ok"
    assert set(captured) == set(inspect.signature(refresh_marketplace).parameters)


def test_marketplace_failure_is_reported_with_a_next_step(monkeypatch, tmp_path):
    import typer as _typer

    monkeypatch.setattr(
        "cli.commands.refresh_marketplace.refresh_marketplace",
        lambda **kw: (_ for _ in ()).throw(_typer.Exit(3)),
    )
    row = onb._step_marketplace(workspace=tmp_path, quiet=True)
    assert row["status"] == "failed"
    assert "agnes refresh-marketplace" in row["detail"]


def test_diagnose_maps_overall_status(monkeypatch, tmp_path):
    monkeypatch.setattr(onb, "_run_cli", lambda args, **kw: (0, json.dumps({"overall": "healthy"})))
    assert onb._step_diagnose(workspace=tmp_path, quiet=True)["status"] == "ok"
    monkeypatch.setattr(onb, "_run_cli", lambda args, **kw: (0, json.dumps({"overall": "degraded"})))
    assert onb._step_diagnose(workspace=tmp_path, quiet=True)["status"] == "warning"
    monkeypatch.setattr(onb, "_run_cli", lambda args, **kw: (1, "not json"))
    assert onb._step_diagnose(workspace=tmp_path, quiet=True)["status"] == "failed"


def _registered_group_names() -> set[str]:
    from cli.main import app

    names = {
        getattr(g, "name", None) or (g.typer_instance.info.name if hasattr(g, "typer_instance") else None)
        for g in app.registered_groups
    }
    return {n for n in names if n}


def test_registered_in_main_app():
    assert "onboard" in _registered_group_names()


def test_onboard_is_a_maintenance_command():
    """It runs `agnes update` internally and takes the same lock — the root
    callback must not spawn a competing background updater."""
    from cli.main import _MAINTENANCE_COMMANDS

    assert "onboard" in _MAINTENANCE_COMMANDS


def test_singular_connector_alias_is_registered():
    """The thin-install-prompt design names `agnes connector list`; it aliases
    the same Typer as `agnes connectors`."""
    assert {"connector", "connectors"} <= _registered_group_names()


def test_no_secret_ever_reaches_the_output(tmp_path, monkeypatch, stubbed):
    """The bootstrap token is referenced by path only — never read into
    argv, stdout, or the JSON report."""
    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path), "--json"])
    payload = json.dumps(json.loads(result.stdout))
    assert "--token " not in payload
    assert "Bearer" not in payload


def test_workspace_flag_reaches_the_marketplace_and_diagnose_steps():
    """`--workspace X` must move every step into X, not only the gated ones.

    `refresh_marketplace` has no workspace parameter and its `target="project"`
    means "the current directory"; `_step_diagnose` shells out to
    `agnes diagnose`, which reads workspace-relative state. Both used to run in
    whatever directory the operator invoked `agnes onboard --workspace X` from,
    so the marketplace was bootstrapped into the wrong tree and the health
    report described the wrong workspace — both reporting success, which is what
    made it invisible. Asserted on the signatures and the call sites rather than
    by running a real bootstrap, which needs a server.
    """
    import inspect

    from cli.commands import onboard

    for fn in (onboard._step_marketplace, onboard._step_diagnose):
        assert "workspace" in inspect.signature(fn).parameters, (
            f"{fn.__name__} must take the workspace, or --workspace cannot reach it"
        )

    # `_run_cli` must be able to place the child, and diagnose must use it.
    assert "cwd" in inspect.signature(onboard._run_cli).parameters
    src = inspect.getsource(onboard._step_diagnose)
    assert "cwd=workspace" in src, "diagnose still runs in the caller's cwd"

    # And the marketplace step must actually enter the workspace.
    src = inspect.getsource(onboard._step_marketplace)
    assert "chdir(workspace)" in src, "marketplace still bootstraps into the caller's cwd"


# --------------------------------------------------------------------------- #
# Re-run repair path: an existing workspace is ours, not "unrelated content"
# --------------------------------------------------------------------------- #

_REAL_STEP_INIT2 = onb._step_init


def _complete_workspace(tmp_path: Path) -> Path:
    """Simulate a workspace a finished `agnes onboard` run leaves behind."""
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "init-complete").write_text("override: false\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# workspace\n", encoding="utf-8")
    (tmp_path / "AGNES_WORKSPACE.md").write_text("docs\n", encoding="utf-8")
    (tmp_path / "server").mkdir(exist_ok=True)
    (tmp_path / "user").mkdir(exist_ok=True)
    (tmp_path / "template-artifact.md").write_text("admin-authored\n", encoding="utf-8")
    return tmp_path


def test_completed_workspace_is_prepared_regardless_of_its_content(tmp_path):
    """The repair path must survive its own first run: a finished run leaves
    files the allowlist cannot enumerate (the workspace template is
    admin-authored), so the gate keys off the init sentinel."""
    verdict, detail = onb.classify_workspace_dir(_complete_workspace(tmp_path))
    assert verdict == onb.DIR_PREPARED
    assert detail == ""


def test_interrupted_init_artefacts_are_allowlisted(tmp_path):
    """A run killed before the sentinel still leaves Agnes-written files; those
    are ours too and must not read as unrelated content."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        '{"hooks": {"SessionStart": "( nohup agnes update --quiet & )"}}', encoding="utf-8"
    )
    (tmp_path / "CLAUDE.md").write_text("# workspace\n", encoding="utf-8")
    (tmp_path / "server").mkdir()
    (tmp_path / "user").mkdir()
    (tmp_path / ".gitignore").write_text("x", encoding="utf-8")
    verdict, _ = onb.classify_workspace_dir(tmp_path)
    assert verdict == onb.DIR_PREPARED


def test_completed_workspace_reruns_without_accept_dir(tmp_path, stubbed):
    result = runner.invoke(onboard_app, ["--workspace", str(_complete_workspace(tmp_path))])
    assert result.exit_code == 0, result.output
    assert stubbed == ["init", "catalog", "preflight", "marketplace", "diagnose"]


def test_initialized_home_dir_is_still_unsafe(tmp_path, monkeypatch):
    """A stray sentinel cannot buy an install into $HOME."""
    _complete_workspace(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    verdict, _ = onb.classify_workspace_dir(tmp_path)
    assert verdict == onb.DIR_UNSAFE


# --------------------------------------------------------------------------- #
# Missing workspace dir: refused, never created
# --------------------------------------------------------------------------- #


def test_classify_missing_dir(tmp_path):
    verdict, detail = onb.classify_workspace_dir(tmp_path / "ghost")
    assert verdict == onb.DIR_MISSING
    assert "does not exist" in detail


def test_missing_dir_is_refused_with_exit_23(tmp_path, monkeypatch, stubbed):
    """A --workspace target that does not exist is refused outright — the
    command never creates directories, and deferring to `agnes init` (which
    WOULD create it) leaves later steps resolving paths this gate never
    classified."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "never-created"
    result = runner.invoke(onboard_app, ["--workspace", str(target)])
    assert result.exit_code == onb.EXIT_MISSING_DIR
    assert not target.exists()
    assert "does not exist" in _clean(result.output)
    assert "mkdir -p" in _clean(result.output)


# --------------------------------------------------------------------------- #
# `agnes update` convergence: benign early-outs and failed stages
# --------------------------------------------------------------------------- #


def test_update_lock_skip_is_not_a_failed_init(tmp_path, monkeypatch):
    """`agnes update` raises `typer.Exit(0)` on benign early-outs (the
    single-instance lock is held by the background SessionStart refresh).
    `typer.Exit` is an Exception, so letting it escape would turn "nothing
    to do" into a fatal init failure."""
    import typer as _typer

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text("override: false\n", encoding="utf-8")
    monkeypatch.setattr(onb, "_run_init", lambda **kw: pytest.fail("must not re-init"))
    monkeypatch.setattr(
        "cli.commands.update.update",
        lambda **kw: (_ for _ in ()).throw(_typer.Exit(0)),
    )

    rows = onb._step_init(tmp_path, server_url="https://agnes.example.com", quiet=True)
    assert rows[0]["status"] == "already-configured"
    assert "already running" in rows[0]["detail"]


def test_update_nonzero_exit_is_a_real_failure(tmp_path, monkeypatch, stubbed):
    import typer as _typer

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text("override: false\n", encoding="utf-8")
    monkeypatch.setattr(onb, "_step_init", _REAL_STEP_INIT2)
    monkeypatch.setattr(onb, "_run_init", lambda **kw: pytest.fail("must not re-init"))
    monkeypatch.setattr(
        "cli.commands.update.update",
        lambda **kw: (_ for _ in ()).throw(_typer.Exit(3)),
    )

    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path), "--json"])
    assert result.exit_code == onb.EXIT_INIT_FAILED
    payload = json.loads(result.stdout)
    assert payload["overall"] == "failed"
    assert payload["steps"][0]["status"] == "failed"
    assert "3" in payload["steps"][0]["detail"]
    assert "agnes update" in payload["steps"][0]["detail"]


def test_convergence_errors_mark_the_init_row_as_a_warning(tmp_path, monkeypatch):
    """A convergence that reported failed stages is not "already configured"."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text("override: false\n", encoding="utf-8")
    monkeypatch.setattr(onb, "_run_init", lambda **kw: pytest.fail("must not re-init"))
    monkeypatch.setattr(
        onb,
        "_run_update",
        lambda **kw: {"steps": [{"stage": "plugins", "status": "error"}, {"stage": "pull", "status": "ok"}]},
    )

    rows = onb._step_init(tmp_path, server_url="https://agnes.example.com", quiet=True)
    assert rows[0]["step"] == "init"
    assert rows[0]["status"] == "warning"
    assert "plugins" in rows[0]["detail"]


def test_convergence_errors_degrade_the_run(tmp_path, monkeypatch, stubbed):
    """…and that warning must reach `overall`, the only machine-readable
    channel for "something is off" (the exit code stays 0 by design)."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text("override: false\n", encoding="utf-8")
    monkeypatch.setattr(onb, "_step_init", _REAL_STEP_INIT2)
    monkeypatch.setattr(onb, "_run_init", lambda **kw: pytest.fail("must not re-init"))
    monkeypatch.setattr(onb, "_run_update", lambda **kw: {"steps": [{"stage": "plugins", "status": "error"}]})

    result = runner.invoke(onboard_app, ["--workspace", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["steps"][0]["status"] == "warning"
    assert payload["overall"] != "ok"


def test_generic_project_without_agnes_marker_is_unrelated(tmp_path):
    """`CLAUDE.md`, `server/`, `user/`, `.gitignore` are generic names a random
    checkout may hold; without an Agnes marker (`.claude`/`.agnes`) they must
    still read as unrelated content, not buy a silent install."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# some other project\n", encoding="utf-8")
    (tmp_path / "server").mkdir()
    (tmp_path / "user").mkdir()
    # A bare Claude Code dir is NOT an Agnes marker — it exists in every
    # repo the user has opened in Claude Code.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")
    verdict, detail = onb.classify_workspace_dir(tmp_path)
    assert verdict == onb.DIR_UNRELATED
    assert "CLAUDE.md" in detail


def test_non_utf8_settings_json_does_not_crash_the_gate(tmp_path):
    """A Claude Code settings.json with invalid UTF-8 must classify the
    folder, not blow up the gate with UnicodeDecodeError."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_bytes(b"\xff\xfe{broken}")
    (tmp_path / "CLAUDE.md").write_text("# something\n", encoding="utf-8")
    verdict, _ = onb.classify_workspace_dir(tmp_path)
    assert verdict == onb.DIR_UNRELATED
