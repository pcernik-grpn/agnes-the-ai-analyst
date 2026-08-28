"""agnes — CLI tool for the Agnes AI harness.

Primary interface for AI agents. Install: uv tool install agnes-the-ai-analyst
"""

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import typer

# Force UTF-8 on Windows stdout/stderr at import time. The default Windows
# console codepage (cp1250 on cs-CZ, cp1252 on en-US, …) cannot encode the
# Braille spinner glyphs Rich uses for `agnes pull` progress, nor the
# em-dash / accented chars that show up in skill markdown via
# `agnes skills list`. Both crash with UnicodeEncodeError /
# UnicodeDecodeError before any command-level code runs. `reconfigure` is
# a no-op on non-TextIOWrapper streams (pytest capture, pipes wrapped by
# other tooling) — swallow the AttributeError there.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from cli.commands.attachment import attachment_app
from cli.commands.auth import auth_app
from cli.commands.chat import chat_app
from cli.commands.init import init_app
from cli.commands.mark_private import mark_private_app
from cli.commands.onboard import onboard_app
from cli.commands.onboarded import onboarded_app
from cli.commands.pull import pull_app
from cli.commands.push import push_app
from cli.commands.refresh_marketplace import refresh_marketplace_app
from cli.commands.statusline import statusline_app
from cli.commands.update_workspace import update_workspace_app
from cli.commands.update import update_app
from cli.commands.query import query_command
from cli.commands.status import status_app
from cli.commands.admin import admin_app
from cli.commands.diagnose import diagnose_app
from cli.commands.doctor import doctor_app
from cli.commands.skills import skills_app
from cli.commands.self_upgrade import self_upgrade_app
from cli.commands.setup import setup_app
from cli.commands.server import server_app
from cli.commands.explore import explore_app
from cli.commands.catalog import catalog_app
from cli.commands.global_scope import global_app
from cli.commands.glossary import glossary_app
from cli.commands.semantic_model import semantic_model_app
from cli.commands.schema import schema_app
from cli.commands.describe import describe
from cli.commands.sample import sample
from cli.commands.snapshot import snapshot_app
from cli.commands.disk_info import disk_info_app
from cli.commands.store import store_app
from cli.commands.my_stack import my_stack_app
from cli.commands.marketplace import marketplace_app
from cli.commands.stack import stack_app
from cli.commands.mcp import mcp_app
from cli.commands.docs import docs_app
from cli.commands.collections import collections_app
from cli.commands.facts import facts_app
from cli.commands.config import config_app
from cli.commands.connectors import connectors_app
from cli.commands.data_apps import data_apps_app
from cli.commands.search import search_app
from cli.commands.agent import agent_app


def _cli_version() -> str:
    """Return the installed CLI version from package metadata.

    Falls back to `"unknown"` when the package is not installed (e.g. running
    from a source checkout without `uv pip install -e .`). Deliberately does
    not read pyproject.toml at runtime — that file is not shipped with the
    wheel and the metadata lookup is the canonical source.
    """
    try:
        return _pkg_version("agnes-the-ai-analyst")
    except PackageNotFoundError:
        return "unknown"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"agnes {_cli_version()}")
        raise typer.Exit()


app = typer.Typer(
    name="agnes",
    help="Agnes — AI Harness CLI",
    no_args_is_help=True,
)


@app.callback()
def _root(
    version: bool = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the CLI version and exit.",
    ),
) -> None:
    """Root callback — carries the --version option and fires the auto-update check.

    Update check runs before subcommand dispatch but after the --version flag
    (which exits early). It's best-effort: any failure is swallowed so a bad
    network never blocks a working `agnes` command. Disable with
    `AGNES_NO_UPDATE_CHECK=1`.
    """
    _maybe_warn_outdated()
    _maybe_warn_token_expiry()


_MAINTENANCE_COMMANDS = frozenset(
    {
        "update",
        "self-upgrade",
        "self-update",
        "pull",
        "push",
        "refresh-marketplace",
        "init",
        # `agnes onboard` composes init / update / refresh-marketplace and
        # takes the same `update.lock`. Left off this list, an out-of-date CLI
        # would spawn the detached updater from `_root` first, that child would
        # take the lock, and onboard's own convergence step would silently
        # no-op on a held lock.
        "onboard",
        # `agnes global` converges the same artifacts `agnes update` does and
        # now holds the same `update.lock`. Left off this list, an out-of-date
        # CLI would spawn the detached updater from `_root` first, that child
        # would take the lock, and the command the user actually typed would
        # abort with "retry in a moment" — on the first run after a release,
        # every time (Devin on #1184).
        "global",
    }
)


def _is_maintenance_command() -> bool:
    """True if the current invocation IS an update-family command.

    The root callback fires BEFORE subcommand dispatch, so without this guard
    running `agnes update` (or the SessionStart hook that invokes it) would
    itself spawn ANOTHER `agnes update`. Inspect argv directly — parsing
    hasn't happened yet; the first non-flag token is the subcommand name."""
    for arg in sys.argv[1:]:
        if arg.startswith("-"):
            continue
        return arg in _MAINTENANCE_COMMANDS
    return False


def _spawn_background_update(latest: str) -> None:
    """Kick off a detached, non-interactive `agnes update --quiet` (no prompt).

    Fire-and-forget: the child holds `~/.config/agnes/update.lock` so
    concurrent spawns exit 0, and sets `AGNES_NO_UPDATE_CHECK=1` for itself
    and its children so nothing re-triggers detection. A per-version marker
    means we kick at most one update per distinct server version — the binary
    only becomes current next session, so `is_outdated()` stays true all
    session and, without this guard, every command would spawn. Cross-platform
    detach (POSIX `start_new_session`, Windows `DETACHED_PROCESS`). Best-effort;
    never raises."""
    import os
    import subprocess

    try:
        from cli.config import _config_dir

        marker = _config_dir() / "auto-update-attempt"
        try:
            if marker.exists() and marker.read_text(encoding="utf-8").strip() == latest:
                return  # already kicked an update for this version this cycle
        except OSError:
            pass

        env = {**os.environ, "AGNES_NO_UPDATE_CHECK": "1"}
        argv = [sys.executable, "-m", "cli.main", "update", "--quiet"]
        popen_kwargs: dict = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            close_fds=True,
        )
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — outlive the parent.
            popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(argv, **popen_kwargs)

        try:
            marker.write_text(latest, encoding="utf-8")
        except OSError:
            pass
    except Exception:
        pass  # best-effort: a spawn failure must never break the command


def _maybe_warn_outdated() -> None:
    """On CLI version drift, kick a detached background `agnes update` — no
    prompt, no blocking. Detection is best-effort and cached (24h) by
    `update_check`. Suppressed when `AGNES_NO_UPDATE_CHECK` is set (we're
    already inside an update tree) or when the current command is itself an
    update-family command (the root callback runs before dispatch). A single
    `update.lock` guarantees only one update actually runs. Never raises."""
    try:
        import os

        if not os.environ.get("AGNES_NO_UPDATE_CHECK") and not _is_maintenance_command():
            from cli.config import get_server_url
            from cli.update_check import check

            info = check(get_server_url())
            if info and info.is_outdated() and info.latest:
                _spawn_background_update(info.latest)
    except Exception:
        pass  # best-effort: never fail a command on the probe
    _maybe_warn_upgrade_failures()


def _command_is_quiet() -> bool:
    """True if the current invocation passed --quiet (the SessionStart hook
    path). The root callback runs for every command, but the silent
    self-upgrade-failure warning must only surface on NON-quiet commands —
    so quiet hooks stay quiet. We inspect argv rather than the parsed
    options because the root callback fires before per-command parsing."""
    return "--quiet" in sys.argv[1:]


def _maybe_warn_upgrade_failures() -> None:
    """Surface repeated SILENT self-upgrade failures (#478).

    The SessionStart hook runs a detached `agnes update --quiet`, whose CLI
    step invokes the self-upgrade installer; its failures are invisible
    (fully detached, output suppressed). The installer records each outcome
    in `$AGNES_CONFIG_DIR/upgrade_status.json`; once N attempts in a row have
    failed, the NEXT non-quiet `agnes` command warns once.

    Skipped entirely:
    - under `--quiet` (keeps the SessionStart hook silent), and
    - while a self-upgrade subprocess is running (the recursion sentinel),
      so the smoke-test `agnes --version` never emits the warning.

    Best-effort: never raises."""
    try:
        import os

        if os.environ.get("AGNES_SELF_UPGRADE_IN_PROGRESS") == "1":
            return
        if _command_is_quiet():
            return
        from cli.upgrade_status import (
            format_failure_notice,
            mark_warned,
            should_warn,
        )

        if should_warn():
            typer.echo(format_failure_notice(), err=True)
            mark_warned()  # warn once per failure level — don't spam
    except Exception:
        pass  # best-effort: never fail a command on the status probe


def _maybe_warn_token_expiry() -> None:
    """Surface a proactive PAT-renewal nudge (#477).

    Reads the stored token's `exp` claim locally — no network call, no
    server round-trip — and prints a one-line stderr warning when it's
    inside the renewal window (`AGNES_TOKEN_RENEW_DAYS`, default 7 days;
    `0` disables). At most once per UTC calendar day via a marker file
    (see `cli/token_status.py`).

    Skipped under `--quiet` (the SessionStart `agnes update --quiet` hook
    path) — the same info is instead carried as a report line by
    `agnes update` itself (`cli/commands/update.py`'s "token" stage), so
    nothing is lost; it just never becomes an interactive stderr print
    inside a detached, output-suppressed hook.

    Best-effort: never raises."""
    try:
        if _command_is_quiet():
            return
        from cli.token_status import maybe_print_nudge

        maybe_print_nudge()
    except Exception:
        pass  # best-effort: never fail a command on the expiry probe


# Register subcommands
app.add_typer(attachment_app, name="attachment")
app.add_typer(auth_app, name="auth")
app.add_typer(chat_app, name="chat")
app.add_typer(init_app, name="init")
app.add_typer(onboard_app, name="onboard")
app.add_typer(onboarded_app, name="onboarded")
app.add_typer(pull_app, name="pull")
app.add_typer(push_app, name="push")
app.add_typer(mark_private_app, name="mark-private")
app.add_typer(statusline_app, name="statusline")
app.add_typer(update_workspace_app, name="update-workspace")
app.add_typer(update_app, name="update")
app.add_typer(refresh_marketplace_app, name="refresh-marketplace")
app.command("query")(query_command)
app.add_typer(status_app, name="status")
app.add_typer(admin_app, name="admin")
app.add_typer(diagnose_app, name="diagnose")
app.add_typer(doctor_app, name="doctor")
app.add_typer(skills_app, name="skills")
app.add_typer(self_upgrade_app, name="self-upgrade")
# Hidden verb alias: `agnes self-update` resolves to the SAME callback as
# `agnes self-upgrade` (issue #617 asked for `self-update`; `self-upgrade`
# stays canonical and is what the out-of-date banner recommends). Both
# point at the one `self_upgrade_app` Typer, so they are byte-for-byte the
# same implementation — idempotent, no divergence.
app.add_typer(self_upgrade_app, name="self-update", hidden=True)
app.add_typer(setup_app, name="setup")
app.add_typer(server_app, name="server")
app.add_typer(explore_app, name="explore")
app.add_typer(catalog_app, name="catalog")
app.add_typer(glossary_app, name="glossary")
app.add_typer(semantic_model_app, name="semantic-model")
app.add_typer(schema_app, name="schema")
app.command("describe")(describe)
# `agnes sample <table>` — shorthand for `agnes describe <table> -n 5`.
# CLAUDE.md and the agent-rails protocol have referenced `sample` for a
# while; AI analysts following docs literally now Just Work. Issue #254.
app.command("sample")(sample)
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(disk_info_app, name="disk-info")
app.add_typer(store_app, name="store")
app.add_typer(my_stack_app, name="my-stack")
app.add_typer(marketplace_app, name="marketplace")
app.add_typer(stack_app, name="stack")
# Visible: `connect` / `disconnect` / `my-secret` are supported user commands
# (docs/api-reference.md), and the server itself prints `agnes mcp my-secret
# set <source-id>` as the remedy for a missing credential — hiding the group
# would hide that remedy. What #1707 Block 6 retired is only the SERVER
# invocation: bare `agnes mcp` (and the hidden `agnes mcp serve`) starts the
# stdio MCP server, which is now INTERNAL — the hosted chat sandbox spawns it
# per session (app/chat/runner.py) and `agnes global enable` wires it into
# Claude Code's user scope. Both keep working; only the advertising stops.
app.add_typer(mcp_app, name="mcp")
app.add_typer(docs_app, name="docs")
app.add_typer(collections_app, name="collections")
app.add_typer(facts_app, name="facts")
app.add_typer(connectors_app, name="connectors")
# Hidden verb alias: `agnes connector` resolves to the SAME Typer as
# `agnes connectors` (the thin-install-prompt design names the singular).
# One implementation, two spellings — no divergence possible.
app.add_typer(connectors_app, name="connector", hidden=True)
app.add_typer(data_apps_app, name="app")
app.add_typer(search_app, name="search")
app.add_typer(config_app, name="config")
app.add_typer(agent_app, name="agent")
app.add_typer(global_app, name="global")


def _capture_cli_exception(exc: BaseException, kind: str) -> None:
    """Best-effort PostHog forward for CLI-level errors. No-op when off."""
    try:
        from src.observability import get_posthog

        argv = sys.argv[1:]
        command = argv[0] if argv else "<no-command>"
        get_posthog().capture_exception(
            exc,
            distinct_id="cli",
            properties={
                "component": "cli",
                "command": command,
                "argv": " ".join(argv)[:512],
                "error_kind": kind,
            },
        )
        get_posthog().shutdown()
    except Exception:
        pass  # never replace the user-visible error with a tracing failure


def main() -> None:
    """Wrap ``app()`` so AgnesTransportError (and other typed CLI errors)
    surface as a one-line message + exit, never as a Python traceback. The
    full traceback is already logged to ``~/.config/agnes/last-error.log``
    by the api_* helpers — operators read it from there for support
    forwarding. Anything that escapes this wrapper IS a CLI bug worth
    fixing — log + print "internal error" so the analyst doesn't see a
    Pythonist's traceback either.

    Also forwards captured exceptions to PostHog (no-op when disabled) so
    operators can see CLI-level failures alongside server-side ones.
    Normal control-flow exits (typer.Exit / SystemExit / KeyboardInterrupt)
    are never reported.

    Pavel's #185 Phase 3B: previously a `httpx.ReadTimeout` from an
    `agnes query --remote` against a slow BQ view dumped a 30-frame
    traceback to the analyst's terminal. Now: one clean line + a hint,
    return code 1.
    """
    from cli.client import AgnesTransportError, RedirectHardStop, _log_traceback

    try:
        app()
    except RedirectHardStop as exc:
        # A redirect the CLI will not follow. It used to print here-and-now
        # from inside the response hook and call `sys.exit(2)`; rendering it
        # at the top level instead is what lets a command opt into handling
        # it (`agnes diagnose`, `agnes update`'s `_run_step`). Both halves of
        # the old output are reproduced exactly — the lowercase `error: `
        # prefix and the exit code — so a command that has NOT opted in
        # behaves identically to before, which is the claim this rests on.
        #
        # Deliberately NOT forwarded to telemetry. The old form raised
        # `SystemExit`, which this wrapper explicitly never reports, so
        # capturing here would quietly open a new telemetry stream as a
        # side effect of a structural fix. That is a separate decision.
        typer.echo(f"error: {exc.user_message}", err=True)
        if exc.hint:
            typer.echo(exc.hint, err=True)
        sys.exit(exc.exit_code)
    except AgnesTransportError as exc:
        _capture_cli_exception(exc, kind="transport")
        typer.echo(f"Error: {exc.user_message}", err=True)
        if exc.hint:
            typer.echo(exc.hint, err=True)
        sys.exit(exc.exit_code)
    except typer.Exit:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # last-resort net — escaped exceptions are bugs
        _capture_cli_exception(exc, kind="unhandled")
        log = _log_traceback(exc, context="unhandled at CLI top-level")
        typer.echo(
            f"Error: internal CLI error ({type(exc).__name__}). Full traceback logged to {log}.",
            err=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
