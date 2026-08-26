"""`agnes onboard` — one deterministic command that installs an analyst workspace.

The install prompt used to be a 400–550 line English "program" executed by a
non-deterministic interpreter (the setup agent): it skipped steps, mis-triaged
errors, and every fix meant editing prose. This command is that program, moved
into code. The prompt shrinks to "install the CLI, run `agnes onboard`, follow
its output".

Steps, in order:

  0. dir check    — refuse home/system dirs, proceed silently in a prepared
                    folder, ask for `--accept-dir` when the folder holds
                    unrelated content. Never `mkdir`/`cd` on the user's behalf.
  1. init         — `agnes init --token-file ~/.agnes/token` on a fresh
                    workspace; the `agnes update` convergence on an already
                    initialized one. THE ONLY FATAL STEP — nothing after it
                    can work without a credential and a workspace.
  2. catalog      — smoke-test the data surface. An empty list is a healthy
                    outcome (no grants yet), not an error.
  3. preflight    — `git` + `claude` on PATH, with per-OS install hints.
  4. marketplace  — `agnes refresh-marketplace --bootstrap`.
  5. diagnose     — overall health.
  6. summary      — a recap of every step, the connectors this instance
                    offers, and a `NEXT:` block.

Contracts worth stating because they are load-bearing:

* **Idempotent.** Safe to re-run at any point; "already configured" is a
  success, not a no-op to apologize for.
* **No secrets anywhere.** The bootstrap token is referenced by PATH only
  (`agnes init --token-file`), never read into argv, stdout, or the report —
  argv is world-readable in the process table and shell history.
* **One failure never eats the rest.** Every step but `init` is wrapped; a
  failed step is reported and the run continues to the summary, which is the
  actual product. A degraded run still exits 0 — the `NEXT:` instruction
  (restart Claude Code) stays valid, and the machine-readable channel for
  "something is off" is the report's ``overall`` field, not the exit code.
  The exit code is reserved for the two decisions a caller must act on: a
  refused directory and a failed init.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import typer

from cli.config import resolve_server_url
from cli.v2_client import V2ClientError, api_get_json

onboard_app = typer.Typer(
    name="onboard",
    help="Install and verify this analyst workspace end to end (idempotent).",
    invoke_without_command=True,
)

SCHEMA_VERSION = 1

# Exit codes. 0/1 keep their usual meaning; the directory verdicts get their
# own codes so a caller (a human, or a Claude Code agent relaying the output)
# can branch without parsing prose. Chosen above Click's reserved 2 and next
# to `refresh-marketplace`'s 20 so the CLI's special codes stay in one band.
EXIT_INIT_FAILED = 1
EXIT_CONFIG = 1
EXIT_UNSAFE_DIR = 21
EXIT_UNRELATED_DIR = 22
EXIT_MISSING_DIR = 23

# Directory verdicts.
DIR_UNSAFE = "unsafe"
DIR_PREPARED = "prepared"
DIR_UNRELATED = "unrelated"
DIR_MISSING = "missing"

# Artefacts a *prepared* workspace folder may already hold. Anything else is
# unrelated content and needs an explicit `--accept-dir`. `bash.exe.stackdump`
# is Git-Bash-on-Windows litter that lands in a freshly created folder through
# no fault of the user; `.gitignore` alone only ever discriminates an empty
# scaffold repo (any real checkout trips the gate on other entries), so
# refusing over it is pure false-positive surface.
#
# A *completed* workspace is covered by the sentinel short-circuit in
# `classify_workspace_dir` — the admin-authored workspace template can ship
# anything, so no allowlist could enumerate it. The *interrupted* case
# (killed before the sentinel, `agnes init` step 9) is covered by
# `_AGNES_INTERRUPTED_ARTEFACTS` below, gated on a real Agnes marker.
PREPARED_ALLOWLIST = frozenset(
    {
        ".git",
        ".gitignore",
        ".claude",
        ".agnes",
        "AGNES_WORKSPACE.md",
        "README.md",
        "bash.exe.stackdump",
    }
)

# Additionally allowlisted ONLY when the directory carries a real Agnes
# marker (`_has_agnes_marker`). An interrupted init leaves `CLAUDE.md`
# (step 4), `server/` + `user/` (the first pull) behind — but the names are
# generic enough that a random project checkout may hold all of them, so
# without the marker they must still read as unrelated content, or the gate
# silently adopts a stranger's repo.
_AGNES_INTERRUPTED_ARTEFACTS = frozenset(
    {
        "CLAUDE.md",
        "server",
        "user",
    }
)


def _has_agnes_marker(workspace: Path) -> bool:
    """True when the directory shows evidence Agnes itself set it up.

    A bare `.claude/` directory is NOT a marker — that is Claude Code's own
    per-project dir and exists in essentially every repo the user has opened
    in Claude Code, which is exactly where the install prompt gets pasted.
    What is distinctive: a workspace-local `.agnes/`, or a
    `.claude/settings.json` carrying the agnes hooks `agnes init` installs
    (step 5 of the init flow — before the first pull writes `server/` and
    `user/`, so an interrupted run that left those behind left this too).
    """
    try:
        if (workspace / ".agnes").exists():
            return True
        settings = workspace / ".claude" / "settings.json"
        # Bytes, not text: a settings file with invalid UTF-8 must classify
        # the folder, not crash the gate (UnicodeDecodeError is a ValueError,
        # which the OSError guard would miss).
        return settings.is_file() and b"agnes" in settings.read_bytes()
    except OSError:
        return False

# Written by `agnes init` as its very last step; its presence means this
# directory is an Agnes workspace we created ourselves.
INIT_SENTINEL = Path(".claude") / "init-complete"


def is_initialized_workspace(workspace: Path) -> bool:
    """True when ``workspace`` already holds a completed `agnes init`."""
    try:
        return (workspace / INIT_SENTINEL).exists()
    except OSError:
        return False

# How many unrelated entries to name before summarizing the rest.
_MAX_LISTED_ENTRIES = 8


# --------------------------------------------------------------------------- #
# Step 0 — workspace directory classification
# --------------------------------------------------------------------------- #
def classify_workspace_dir(workspace: Path) -> tuple[str, str]:
    """Classify ``workspace`` as unsafe / prepared / unrelated.

    Returns ``(verdict, detail)`` where ``detail`` is a short reason for
    ``unsafe`` and a comma-separated sample of the offending entries for
    ``unrelated`` (empty for ``prepared``).

    The unsafe list is shared with `agnes init` rather than re-declared here —
    one refusal policy, one place to change it.
    """
    from cli.commands.init import _unsafe_workspace_reason

    resolved = workspace.resolve()
    # Checked first on purpose: a stray sentinel must never buy an install
    # into $HOME or a system directory.
    unsafe = _unsafe_workspace_reason(resolved)
    if unsafe is not None:
        return DIR_UNSAFE, unsafe

    # Already an Agnes workspace → prepared, whatever else it holds. `agnes
    # onboard` advertises itself as safe to re-run, and a finished run leaves
    # files no allowlist can enumerate (CLAUDE.md, server/, user/, plus the
    # admin-authored workspace template's own content). Without this the
    # repair path would refuse every real workspace with `--accept-dir`.
    if is_initialized_workspace(resolved):
        return DIR_PREPARED, ""

    if not resolved.is_dir():
        # The command never creates a directory — a target that does not
        # exist must be refused here, NOT deferred: `agnes init` would create
        # it and succeed while later steps resolved their own directories
        # against a cwd this gate never classified.
        return DIR_MISSING, "does not exist"

    try:
        entries = sorted(p.name for p in resolved.iterdir())
    except OSError:
        # Exists but unreadable: not our call to make here. Treat as prepared
        # and let `agnes init` produce the real filesystem error.
        return DIR_PREPARED, ""

    allowlist = PREPARED_ALLOWLIST
    if _has_agnes_marker(resolved):
        allowlist = allowlist | _AGNES_INTERRUPTED_ARTEFACTS
    unrelated = [name for name in entries if name not in allowlist]
    if not unrelated:
        return DIR_PREPARED, ""

    shown = ", ".join(unrelated[:_MAX_LISTED_ENTRIES])
    if len(unrelated) > _MAX_LISTED_ENTRIES:
        shown += f", … (+{len(unrelated) - _MAX_LISTED_ENTRIES} more)"
    return DIR_UNRELATED, shown


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _bootstrap_token_path() -> Path:
    """Where the web install guide writes the login token."""
    return Path.home() / ".agnes" / "token"


def _resolve_server_url(explicit: Optional[str]) -> Optional[str]:
    """Explicit flag → ``AGNES_SERVER`` → saved config. ``None`` when unknown.

    Deliberately does NOT reuse `cli.config.get_server_url`, which falls back
    to ``http://localhost:8000``: onboarding against an invented default would
    fail deep inside `agnes init` with a connection error instead of here with
    "pass --server-url". `agnes auth login` needs the same answer, so the logic
    now lives in `cli.config.resolve_server_url` and this stays as the local
    name onboarding's callers already use.
    """
    return resolve_server_url(explicit)


def _quiet_stdout(quiet: bool):
    """Swallow a reused command's chatter when the caller wants one JSON object."""
    return contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext()


def _run_cli(args: list[str], *, timeout: int = 300, cwd: Path | None = None) -> tuple[int, str]:
    """Invoke this same CLI as a subprocess; return ``(exit_code, stdout)``.

    Used where a command is only cleanly reachable as a command (its callback
    takes a `typer.Context` it inspects). Mirrors the invocation form in
    `cli/main.py::_spawn_background_update`, including the recursion guard so
    the child never re-triggers the background auto-update.
    """
    env = {**os.environ, "AGNES_NO_UPDATE_CHECK": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "cli.main", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        # The child must run IN the workspace, not in whatever directory the
        # operator happened to invoke `agnes onboard --workspace X` from: every
        # step this helper reaches (`diagnose`) reads workspace-relative state.
        cwd=str(cwd) if cwd is not None else None,
    )
    return proc.returncode, proc.stdout


def _row(step: str, status: str, detail: str) -> dict:
    return {"step": step, "status": status, "detail": detail}


def _guarded(step: str, fn: Callable[[], dict]) -> dict:
    """Run one non-fatal step; turn any failure into a reported row."""
    try:
        return fn()
    except typer.Exit as exc:
        code = int(getattr(exc, "exit_code", 0) or 0)
        if code == 0:
            return _row(step, "ok", "completed")
        return _row(step, "failed", f"exit_code={code}")
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        return _row(step, "failed", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Step 1 — init / converge
# --------------------------------------------------------------------------- #
def _run_init(*, workspace: Path, server_url: str) -> None:
    """Call the `agnes init` callback directly with its FULL keyword set.

    Typer callbacks carry `OptionInfo` sentinels as defaults, so an omitted
    keyword would arrive as that sentinel rather than a value. Every parameter
    is therefore passed explicitly, and
    `tests/test_cli_onboard.py::test_run_init_passes_every_init_parameter`
    pins the set against the real signature so a new option can't drift in
    silently.
    """
    from cli.commands.init import init as init_cmd

    init_cmd(
        server_url=server_url,
        token=None,
        # Path only — the token never reaches argv or this process's memory.
        token_file=str(_bootstrap_token_path()),
        bundle=None,
        force=False,
        as_admin=False,
        trust_marketplace_host=None,
        workspace_str=str(workspace),
        skip_materialize=True,
        no_shortcut=False,
    )


def _run_update() -> dict:
    """Run the `agnes update` convergence and return its parsed run report.

    `agnes update` ends benign early-outs with ``typer.Exit(0)`` — the
    single-instance lock is held by the SessionStart background refresh, or
    the config dir is unreadable. ``typer.Exit`` subclasses ``Exception``,
    so letting it escape would reach our caller's catch-all and turn "nothing
    to do" into a fatal init failure. Those runs are reported as
    ``{"early_exit": True}``; a NON-zero exit is a real failure and is
    re-raised with a legible message (bare ``Exit: 3`` tells nobody anything).
    """
    from cli.commands.update import update as update_cmd

    buf = io.StringIO()
    early_exit = False
    try:
        with contextlib.redirect_stdout(buf):
            update_cmd(quiet=False, as_json=True)
    # Catch typer.Exit itself, NOT click.exceptions.Exit: newer typer vendors
    # click as `typer._click`, so the standalone click's Exit is a different
    # class there and the except arm would silently stop matching.
    except typer.Exit as exc:
        code = int(getattr(exc, "exit_code", 0) or 0)
        if code != 0:
            raise RuntimeError(
                f"`agnes update` exited {code} — run `agnes update` on its own to see the failing stage"
            ) from exc
        early_exit = True

    raw = buf.getvalue().strip()
    report: dict = {"steps": []}
    if raw:
        try:
            parsed = json.loads(raw.splitlines()[-1])
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            report = parsed
    # An early exit that still printed a report (the config-dir path) carries
    # its own error stages — those speak for themselves.
    if early_exit and not (report.get("steps") or []):
        report["early_exit"] = True
    return report


def _step_init(workspace: Path, *, server_url: str, quiet: bool) -> list[dict]:
    """Bootstrap or converge the workspace. Returns one or two report rows.

    Raises on failure — this is the one step whose failure aborts the run.
    """
    if is_initialized_workspace(workspace):
        # Run the convergence IN the workspace — `agnes update` falls back to
        # the cwd when its config anchor is unset, same per-step rule as the
        # marketplace and diagnose steps.
        with _quiet_stdout(quiet), contextlib.chdir(workspace):
            report = _run_update()
        if report.get("early_exit"):
            return [
                _row(
                    "init",
                    "already-configured",
                    "workspace already initialized; another `agnes update` is already running "
                    "(the background SessionStart refresh) and converges it",
                )
            ]
        steps = report.get("steps") or []
        bad = [s for s in steps if s.get("status") == "error"]
        detail = "workspace already initialized; converged via `agnes update`"
        if bad:
            detail += f" ({len(bad)} convergence issue(s): " + ", ".join(str(s.get("stage")) for s in bad) + ")"
            # A convergence that failed stages is not "already configured".
            # Reporting it as such would let the run's only substantive step
            # fail while `overall` still says "ok" — and `overall` is the
            # machine-readable channel for "something is off" (see module
            # docstring), since the exit code stays 0 by design.
            return [_row("init", "warning", detail)]
        return [_row("init", "already-configured", detail)]

    with _quiet_stdout(quiet):
        _run_init(workspace=workspace, server_url=server_url)

    rows = [_row("init", "ok", f"workspace initialized at {workspace}")]

    # The bootstrap token is a plaintext, long-lived credential on disk;
    # `agnes init` deletes it once the credential is saved. If it survived,
    # say so loudly — by PATH, never by content.
    leftover = _bootstrap_token_path()
    try:
        still_there = leftover.is_file()
    except OSError:
        still_there = False
    if still_there:
        rows.append(
            _row(
                "bootstrap-token",
                "warning",
                f"{leftover} still exists after init — the saved credential should have replaced it. "
                "Delete that file once `agnes catalog` works; it is a plaintext token on disk.",
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Step 2 — catalog smoke
# --------------------------------------------------------------------------- #
def _step_catalog(*, quiet: bool) -> dict:
    try:
        with _quiet_stdout(quiet):
            data = api_get_json("/api/v2/catalog")
    except V2ClientError as exc:
        return _row("catalog", "failed", f"catalog fetch failed: {exc}")
    tables = data.get("tables") or []
    if not tables:
        return _row(
            "catalog",
            "ok",
            "0 tables visible — your admin hasn't granted you access yet. "
            "Ask them for a data-package grant, then re-run `agnes pull`.",
        )
    return _row("catalog", "ok", f"{len(tables)} table(s) visible — try `agnes catalog`")


# --------------------------------------------------------------------------- #
# Step 3 — preflight (git + claude on PATH)
# --------------------------------------------------------------------------- #
def _install_hint(binary: str) -> str:
    system = platform.system()
    if binary == "git":
        if system == "Darwin":
            return "git: brew install git"
        if system == "Windows":
            return "git: winget install --id Git.Git -e --source winget --silent"
        return "git: sudo apt-get install git   OR   sudo dnf install git"
    if system == "Windows":
        return "claude: winget install --id Anthropic.ClaudeCode -e   (see https://docs.claude.com/claude-code)"
    if system == "Darwin":
        return "claude: see https://docs.claude.com/claude-code (native installer)"
    return "claude: npm i -g @anthropic-ai/claude-code   (see https://docs.claude.com/claude-code)"


def _step_preflight() -> dict:
    missing = [name for name in ("git", "claude") if shutil.which(name) is None]
    if not missing:
        return _row("preflight", "ok", "git and claude are on PATH")
    hints = "; ".join(_install_hint(name) for name in missing)
    return _row(
        "preflight",
        "failed",
        f"missing on PATH: {', '.join(missing)} — install then re-run `agnes onboard`. {hints}",
    )


# --------------------------------------------------------------------------- #
# Step 4 — marketplace bootstrap
# --------------------------------------------------------------------------- #
def _step_marketplace(*, workspace: Path, quiet: bool) -> dict:
    from cli.commands.refresh_marketplace import refresh_marketplace

    try:
        with _quiet_stdout(quiet):
            # Every parameter passed explicitly: a Typer callback invoked as a
            # plain function receives `OptionInfo` sentinels for anything
            # omitted (`target` normalizes one defensively — don't rely on it).
            # `refresh_marketplace` has no workspace parameter and
            # `target="project"` means "the current directory", so without this
            # chdir `--workspace X` bootstrapped the marketplace into the
            # caller's cwd instead — silently, since the command reports success
            # either way.
            with contextlib.chdir(workspace):
                refresh_marketplace(check=False, bootstrap=True, target="project")
        return _row("marketplace", "ok", "plugins cloned / up to date")
    except typer.Exit as exc:
        code = int(getattr(exc, "exit_code", 0) or 0)
        if code == 0:
            return _row("marketplace", "ok", "plugins cloned / up to date")
        return _row(
            "marketplace",
            "failed",
            f"`agnes refresh-marketplace --bootstrap` exited {code} — re-run it on its own to see the git output.",
        )


# --------------------------------------------------------------------------- #
# Step 5 — diagnose
# --------------------------------------------------------------------------- #
def _step_diagnose(*, workspace: Path, quiet: bool) -> dict:  # noqa: ARG001 — subprocess is silent already
    rc, out = _run_cli(["diagnose", "--json"], cwd=workspace)
    try:
        overall = json.loads(out).get("overall")
    except ValueError:
        return _row("diagnose", "failed", f"`agnes diagnose` exited {rc} with unparseable output")
    if overall == "healthy":
        return _row("diagnose", "ok", "Overall: healthy")
    if overall == "degraded":
        return _row("diagnose", "warning", "Overall: degraded — run `agnes diagnose` for the failing checks")
    return _row("diagnose", "failed", f"Overall: {overall} — run `agnes diagnose` for the failing checks")


# --------------------------------------------------------------------------- #
# Step 6 — connectors (informational; never fatal)
# --------------------------------------------------------------------------- #
def _fetch_connectors() -> Optional[list[dict]]:
    """This instance's connector manifest, or ``None`` when it can't be read.

    ``None`` (not ``[]``) means "don't render the section at all" — an older
    server or a transient failure should not print an empty list that reads
    like "this instance offers nothing".
    """
    try:
        body = api_get_json("/api/connectors/manifest")
    except Exception:  # noqa: BLE001 — informational section, never fatal
        return None
    connectors = body.get("connectors")
    return connectors if isinstance(connectors, list) else None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
_NEXT_BLOCK = (
    "NEXT: restart Claude Code from this directory so every plugin, MCP server "
    "and SessionStart hook installed above actually loads — type `/exit` (or "
    "close the session), then run `claude` again from here."
)


def _emit_refusal(*, as_json: bool, workspace: Path, verdict: str, detail: str, lines: list[str], code: int) -> None:
    """Print a directory refusal (prose on stderr, or one JSON object) and exit.

    ``lines`` is the human form; its ``NEXT:`` line doubles as the JSON
    report's ``next`` field so both surfaces carry the same instruction.
    """
    next_hint = next((line for line in lines if line.startswith("NEXT:")), lines[-1])
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "workspace": str(workspace),
                    "dir_status": verdict,
                    "dir_detail": detail,
                    "overall": "refused",
                    "steps": [],
                    "connectors": [],
                    "next": next_hint,
                },
                indent=2,
            )
        )
    else:
        for line in lines:
            typer.echo(line, err=True)
    raise typer.Exit(code)


def _render_summary(
    *,
    workspace: Path,
    verdict: str,
    rows: list[dict],
    connectors: Optional[list[dict]],
) -> None:
    typer.echo("")
    typer.echo(f"agnes onboard — summary ({workspace}, {verdict})")
    for row in rows:
        typer.echo(f"  [{row['status']:18s}] {row['step']:14s} {row['detail']}")

    if connectors:
        typer.echo("")
        typer.echo("Available connectors on this instance:")
        for c in connectors:
            summary = c.get("short_summary", "")
            typer.echo(f"  {c.get('slug', ''):24s} {c.get('display_name', ''):24s} {summary}")
        # Deliberately NOT advertising a slash command here: none of the
        # Agnes-managed commands (`cli/lib/commands.py`) is a connector
        # launcher, and a seed template may or may not ship one. Both hints
        # below exist on every install.
        typer.echo('  Set one up: ask in the next Claude Code session (e.g. "set up Jira"),')
        typer.echo("  or read the steps yourself with: agnes connectors show <slug>")

    typer.echo("")
    typer.echo(_NEXT_BLOCK)


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #
@onboard_app.callback(invoke_without_command=True)
def onboard(
    workspace_str: Optional[str] = typer.Option(
        None,
        "--workspace",
        help="Target workspace directory (default: the current directory).",
    ),
    server_url: Optional[str] = typer.Option(
        None,
        "--server-url",
        help="Agnes server URL. Falls back to AGNES_SERVER, then the saved config.",
    ),
    accept_dir: bool = typer.Option(
        False,
        "--accept-dir",
        help=(
            "Install into the target directory even though it holds unrelated "
            "content. Never overrides the home/system-directory refusal."
        ),
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the whole run report as a single JSON object on stdout.",
    ),
) -> None:
    """Install and verify this analyst workspace end to end. Safe to re-run."""
    # This command runs `agnes init` / `agnes update` internally, and the
    # latter holds the single-instance update lock. Without the guard the root
    # callback could spawn a background `agnes update` that takes the lock
    # first and makes our own convergence step a silent no-op.
    os.environ["AGNES_NO_UPDATE_CHECK"] = "1"

    workspace = Path(workspace_str).resolve() if workspace_str else Path.cwd().resolve()

    # --- Step 0: directory gate (no filesystem writes, ever) ---------------
    verdict, dir_detail = classify_workspace_dir(workspace)
    if verdict == DIR_UNSAFE:
        _emit_refusal(
            as_json=as_json,
            workspace=workspace,
            verdict=verdict,
            detail=dir_detail,
            code=EXIT_UNSAFE_DIR,
            lines=[
                f"Refusing to install into {workspace} — it is {dir_detail}.",
                "Installing here would scatter .claude/, .agnes/, AGNES_WORKSPACE.md and "
                "marketplace clones into a home or system directory.",
                "NEXT: create a dedicated workspace folder, cd into it, and re-run "
                "`agnes onboard` from there — for example:",
                "  mkdir -p ~/Desktop/agnes-workspace && cd ~/Desktop/agnes-workspace",
            ],
        )
    if verdict == DIR_MISSING:
        _emit_refusal(
            as_json=as_json,
            workspace=workspace,
            verdict=verdict,
            detail=dir_detail,
            code=EXIT_MISSING_DIR,
            lines=[
                f"{workspace} does not exist.",
                "`agnes onboard` never creates a directory on your behalf.",
                "NEXT: create the folder yourself, then re-run — for example:",
                f"  mkdir -p {workspace} && cd {workspace} && agnes onboard",
                "Nothing was created or changed.",
            ],
        )
    if verdict == DIR_UNRELATED and not accept_dir:
        _emit_refusal(
            as_json=as_json,
            workspace=workspace,
            verdict=verdict,
            detail=dir_detail,
            code=EXIT_UNRELATED_DIR,
            lines=[
                f"{workspace} already holds unrelated content: {dir_detail}",
                "An analyst workspace writes .claude/, .agnes/, CLAUDE.md and AGNES_WORKSPACE.md here.",
                "NEXT: pick one —",
                f"  1) install here anyway:  agnes onboard --accept-dir --workspace {workspace}",
                "  2) use another folder:   cd <empty-or-prepared-folder> && agnes onboard",
                "Nothing was created or changed.",
            ],
        )

    # --- Config: fail fast rather than invent a server --------------------
    resolved_server = _resolve_server_url(server_url)
    if not resolved_server:
        message = (
            "No Agnes server URL configured. Pass --server-url https://<your-host>, "
            "set AGNES_SERVER, or run this from a workspace that has already been initialized."
        )
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "workspace": str(workspace),
                        "dir_status": verdict,
                        "overall": "refused",
                        "steps": [],
                        "connectors": [],
                        "next": message,
                    },
                    indent=2,
                )
            )
        else:
            typer.echo(message, err=True)
        raise typer.Exit(EXIT_CONFIG)

    quiet = as_json
    if not quiet:
        typer.echo(f"agnes onboard — using {workspace} ({verdict})")

    rows: list[dict] = []

    # --- Step 1: init (the only fatal step) --------------------------------
    try:
        rows.extend(_step_init(workspace, server_url=resolved_server, quiet=quiet))
    except Exception as exc:  # noqa: BLE001 — reported, then fatal
        detail = f"{type(exc).__name__}: {exc}"
        rows.append(_row("init", "failed", detail))
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "workspace": str(workspace),
                        "dir_status": verdict,
                        "overall": "failed",
                        "steps": rows,
                        "connectors": [],
                        "next": "Fix the error above and re-run `agnes onboard`.",
                    },
                    indent=2,
                )
            )
        else:
            typer.echo(f"  [failed] init: {detail}", err=True)
            typer.echo(
                "NEXT: nothing after init can work without a workspace and a credential. "
                "Fix the error above and re-run `agnes onboard`.",
                err=True,
            )
        raise typer.Exit(EXIT_INIT_FAILED)

    # --- Steps 2-5: reported, never fatal ---------------------------------
    rows.append(_guarded("catalog", lambda: _step_catalog(quiet=quiet)))
    rows.append(_guarded("preflight", _step_preflight))
    rows.append(_guarded("marketplace", lambda: _step_marketplace(workspace=workspace, quiet=quiet)))
    rows.append(_guarded("diagnose", lambda: _step_diagnose(workspace=workspace, quiet=quiet)))

    connectors = _fetch_connectors()
    overall = "degraded" if any(r["status"] in ("failed", "warning") for r in rows) else "ok"

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "workspace": str(workspace),
                    "dir_status": verdict,
                    "overall": overall,
                    "steps": rows,
                    "connectors": connectors or [],
                    "next": _NEXT_BLOCK,
                },
                indent=2,
            )
        )
        return

    _render_summary(workspace=workspace, verdict=verdict, rows=rows, connectors=connectors)
