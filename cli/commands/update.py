"""`agnes update` — one idempotent, best-effort convergence of the workspace + CLI.

A single command that brings a workspace (and the CLI itself) to the instance's
correct, current, healthy state — and the recommended way to repair a broken
install or pick up a new release. Runs the same whether triggered automatically
(SessionStart hook, detached) or manually (`agnes update` typed in a terminal).

Steps, in order, each wrapped so one failure never aborts the rest:

  1. CLI binary self-upgrade — the ONLY step with a rollback: a direct uv/pip
     reinstall guarded by a smoke test, with best-effort rollback to the prior
     wheel where one is available (in `cli/commands/self_upgrade.py`; a fully
     staged swap is a separate, not-yet-implemented hardening). A
     freshly-installed binary becomes active on the NEXT `agnes` invocation:
     the running interpreter can't replace itself, and `os.execv` is unreliable
     on Windows, so there is deliberately NO re-exec. Steps 2-6 run on the
     current binary.
  2. Workspace template — OVERRIDE: safe 3-way merge (backs up analyst edits to
     `.bak`, retaining only the most recent few) only when the server template
     SHA moved; DEFAULT: refresh the server-rendered CLAUDE.md, backing it up
     (with the same retention) before overwrite — unless the only difference
     from the on-disk copy is a date-rollover stamp (#1476).
  3. Agnes-owned settings — hooks / statusLine / managed slash-commands. Agnes
     owns these in BOTH modes and (re)asserts them authoritatively; foreign
     hook entries and a user statusLine are preserved.
  3b. Launcher shortcut — migrate a legacy rc-function launcher to the
     ~/.local/bin script; skipped when no prior install evidence exists (so
     the `agnes init --no-shortcut` opt-out survives updates).
  4. Marketplace plugins — bootstrap when the clone is missing, else cheap
     `--check` and a full reconcile only on drift.
  4b. Sessions — `agnes push` catch-up for transcripts (+ CLAUDE.local.md) the
     SessionEnd hook missed. SessionEnd is not a reliable trigger (a closed
     terminal window can take Claude Code down before it runs the hook);
     SessionStart cannot be missed. Runs BEFORE the pull so a large parquet
     download never delays the upload.
  5. Data — `agnes pull` (MD5-skip, atomic sidecar swap; already idempotent).
  6. Report — append a JSON line recording the run outcome to the (rotated)
     `<workspace>/.claude/agnes/update.log`. Template/init sentinels are NOT
     touched here; they are managed by the workspace/init helpers (Step 2).

Only ONE update runs at a time: the command holds
`~/.config/agnes/update.lock` (cross-platform `filelock`); a second invocation
from any source exits 0 immediately. The OS releases the lock on process exit
(including crash), so the next run always proceeds. The command sets
`AGNES_NO_UPDATE_CHECK=1` for itself and its children so its own internal
`agnes` sub-invocations don't re-trigger the background auto-update.

Steps other than the CLI swap have no rollback and need none — they are
idempotent / safe to re-run, so a partial or failed run converges on the next
run.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import typer

from cli.config import _config_dir, get_server_url, get_token, get_workspace_root, load_config

update_app = typer.Typer(
    name="update",
    help="Converge this workspace + the agnes CLI to the instance's current healthy state.",
    invoke_without_command=True,
)

# Keep the report from growing unbounded across hundreds of SessionStart runs.
_REPORT_MAX_BYTES = 256 * 1024


def _agnes_version() -> str:
    try:
        import importlib.metadata as _md

        return _md.version("agnes-the-ai-analyst")
    except Exception:
        return "unknown"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _mask_volatile_dates(text: str) -> str:
    """Replace ISO ``YYYY-MM-DD`` date substrings with a fixed placeholder.

    ``GET /api/welcome`` re-renders the workspace-prompt template on every
    call. The shipped default template (and any admin-authored override) can
    end with a "generated {{ today }}" stamp, so an otherwise-unchanged
    instance still renders different bytes at every UTC date rollover
    (#1476). Masking date-shaped substrings before the content-equality
    check in :func:`_refresh_default_claude_md` keeps a pure date rollover
    from counting as a real change; a genuine content change still differs
    after masking and still triggers the normal backup-then-overwrite.
    """
    return _ISO_DATE_RE.sub("<date>", text)


def _resolve_workspace() -> Optional[Path]:
    """Locate the analyst workspace, so `agnes update` works from any cwd.

    Order: ``AGNES_LOCAL_DIR`` (an explicit operator/CI override — nothing in
    this tree sets it, see the audit in ``cli/lib/workspace_resolve.py``; an
    earlier version of this docstring claimed Claude Code's hook did, which was
    never true) → ``get_workspace_root()`` (the config anchor written by
    ``agnes init``) → the current dir IF it looks initialised. Returns ``None``
    when no initialised workspace can be found — the CLI step still runs (it is
    workspace-independent); the workspace steps are skipped with a note.

    Consequence worth stating, because the user-level SessionStart hook makes
    it reachable from anywhere: with the global layer enabled, opening an
    unrelated repository resolves the ANCHOR and runs the workspace chain
    against it, ``_step_pull`` included. That is the documented "keeps data
    fresh from any repo" behaviour rather than an accident, and its cost is
    bounded — ``agnes pull`` downloads only parquets whose MD5 changed, and
    the single-instance lock means a burst of session starts does not multiply
    it — but the FIRST run after enabling can be a large download triggered
    from a repository that has nothing to do with the data.
    """
    env_dir = os.environ.get("AGNES_LOCAL_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    root = get_workspace_root()
    if root:
        return Path(root).resolve()
    cwd = Path.cwd()
    if (cwd / ".claude" / "init-complete").exists():
        return cwd
    return None


def _step_bootstrap_token_cleanup(report: list[dict]) -> None:
    """Remove a leftover ``~/.agnes/token`` once the saved credential is proven.

    Step 4 of the web install guide writes the bootstrap token file; only
    ``agnes init`` consumes and deletes it. On the reconcile path (this
    command) the file used to survive indefinitely — a plaintext 90-day
    credential on disk guarded only by umask/NTFS ACLs. Once this run has
    completed an authenticated server round-trip (a workspace/push/pull step
    that neither errored nor was skipped), the saved credential in
    ``~/.config/agnes/token.json`` is proven to work and the bootstrap file
    is redundant — remove it. When NO step proved the credential (auth
    failure, offline run), the file is kept on purpose: it is exactly the
    input the expired-credential recovery (``agnes init --force
    --token-file``) needs. (``_step_push`` reports a zero-work run — which
    makes no HTTP request at all — as ``skipped`` for exactly this reason:
    finishing without work proves nothing about the credential.)
    """
    bootstrap = Path.home() / ".agnes" / "token"
    try:
        exists = bootstrap.is_file()
    except OSError:
        exists = False
    if not exists:
        return  # nothing to clean; no report noise
    # This gate is load-bearing beyond "did anything work". On /home step 4 the
    # bootstrap token is written and `claude` launched on the same line, so the
    # SessionStart hook's detached `agnes update` can reach this cleanup seconds
    # later — before the user has pasted the step-5 install script. The flow
    # still converges only because of three things: on a fresh machine
    # `_resolve_workspace()` returns None so no authenticated step runs and this
    # code is never reached; `agnes init --token-file` falls back to the saved
    # credential when the file is gone; and a zero-work push classifies as
    # `skipped`, not success, so expired-credential recovery does not trip the
    # gate either. Relaxing any of those turns this into deleting a token the
    # user is about to need (Devin Review on #1139).
    proven = any(
        step.get("stage") in ("workspace", "push", "pull") and step.get("status") not in ("error", "skipped")
        for step in report
    )
    if not proven:
        report.append(
            {
                "stage": "bootstrap-token",
                "status": "skipped",
                "detail": "leftover ~/.agnes/token kept — no authenticated step succeeded this run",
            }
        )
        return
    try:
        bootstrap.unlink()
        report.append(
            {
                "stage": "bootstrap-token",
                "status": "ok",
                "detail": "removed leftover ~/.agnes/token (saved credential verified this run)",
            }
        )
    except OSError as exc:
        report.append(
            {
                "stage": "bootstrap-token",
                "status": "error",
                "detail": f"could not remove leftover ~/.agnes/token: {exc}",
            }
        )


def _run_step(name: str, fn: Callable[[], None], report: list[dict]) -> None:
    """Run one convergence step, swallowing any failure into the report.

    Best-effort contract: a single broken step (corrupt file, network blip,
    programming error) must never abort the remaining steps or flip the exit
    code. ``typer.Exit`` raised by reused command internals is treated as a
    recorded outcome, not a fatal error.
    """
    from cli.client import RedirectHardStop

    try:
        fn()
    except typer.Exit as exc:  # reused internals signal via exit codes
        code = getattr(exc, "exit_code", 0)
        if code not in (0, None):
            report.append({"stage": name, "status": "error", "detail": f"exit_code={code}"})
    except RedirectHardStop as exc:
        # Opting in by name: this does not derive from `Exception`, so the
        # clause below never sees it. Before, it was a `sys.exit(2)` that
        # walked through every handler here and ended the run mid-way —
        # taking the report with it, since that is written after the last
        # step. The step isolation this function exists for now covers it.
        #
        # The version floor next door is deliberately NOT opted into: a
        # server that refuses this CLI version must stop the run, not become
        # one row while the remaining steps keep talking to it.
        report.append({"stage": name, "status": "error", "detail": exc.user_message})
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        report.append({"stage": name, "status": "error", "detail": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------- #
# Step 1 — CLI binary (only step with a rollback)
# --------------------------------------------------------------------------- #
def _step_cli(*, quiet: bool, report: list[dict]) -> None:
    from cli.commands import self_upgrade as su
    from cli.update_check import UpdateInfo
    from cli.upgrade_status import record_outcome

    info = su._resolve_info(force=False)
    if isinstance(info, su._Redirected):
        # THIS is the unattended path: `agnes init` installs one detached
        # `agnes update --quiet` as the SessionStart hook, not
        # `agnes self-upgrade`. Folding a redirect into "already current /
        # offline" below would keep the falsely reassuring no-op alive on the
        # only path that runs by itself — and skip `record_outcome`, so the
        # #478 counter (the sole channel a silent path has) would never move.
        # (Devin Review on #1275.)
        record_outcome(success=False, reason=info.reason)
        report.append(
            {
                "stage": "cli",
                "status": "error",
                "detail": f"{info.reason}; run `agnes self-upgrade` for the remedy",
            }
        )
        return
    if info is None:
        # Genuinely current — a probe that completed and concluded "nothing to
        # do" is a healthy pipeline, so reset the #478 counter the same way
        # the interactive command does. Without this, a redirect counted here
        # while the CLI happened to be current kept warning "server moved"
        # after the server was fixed. (Devin Review on #1275.)
        record_outcome(success=True)
        report.append({"stage": "cli", "status": "ok", "detail": "already current"})
        return
    if not isinstance(info, UpdateInfo):
        # Offline / unreachable — a transient blip must neither count as a
        # failure nor clear an accumulated one; the counter stays untouched.
        report.append({"stage": "cli", "status": "ok", "detail": "offline"})
        return
    # `_do_install_with_smoke_and_rollback` records the upgrade outcome itself
    # (with a reason) — we only translate the return code into a report line.
    rc = su._do_install_with_smoke_and_rollback(info, quiet=quiet)
    if rc == su._INSTALL_DEFERRED:
        # Unattended run with no safe rollback artifact — intentionally NOT
        # attempted. Not a failure: the counter is untouched.
        report.append(
            {
                "stage": "cli",
                "status": "deferred",
                "detail": f"{info.installed} -> {info.latest} "
                "(deferred: no safe rollback artifact; will retry next session)",
            }
        )
        return
    if rc == su._INSTALL_STAGED:
        # Windows: the swap was handed to a detached helper that completes after
        # this process exits. Not a failure; the helper records the real outcome.
        # Name the target version so the log says WHAT is being installed.
        report.append(
            {
                "stage": "cli",
                "status": "staged",
                "detail": f"{info.installed} -> {info.latest} "
                "(windows deferred install; completes after this process exits)",
            }
        )
        return
    if rc == su._INSTALL_OK:
        report.append(
            {
                "stage": "cli",
                "status": "updated",
                "detail": f"{info.installed} -> {info.latest} (active next run)",
            }
        )
    else:
        report.append({"stage": "cli", "status": "error", "detail": "install failed; rolled back to current"})


# --------------------------------------------------------------------------- #
# Step 1b — token expiry (#477; workspace-independent, report-only — never
# interactive, mirrors the CLI step's placement).
# --------------------------------------------------------------------------- #
def _step_token(token: Optional[str], report: list[dict]) -> None:
    """Surface the stored PAT's expiry as a convergence report line.

    Proactive re-mint (issue #477, Option 3): no refresh-token grant, no PAT
    TTL change. `agnes update` runs unattended via the SessionStart hook, so
    this step is report-only — never a prompt, never a browser launch. The
    interactive stderr nudge (same underlying data) lives in
    `cli/token_status.py::maybe_print_nudge`, wired into the root callback,
    and is separately skipped under `--quiet` so it doesn't fire twice for
    the same hook invocation.
    """
    if not token:
        report.append({"stage": "token", "status": "skipped", "detail": "no token configured"})
        return
    from cli.token_status import days_remaining, format_status_line, get_renew_days

    line = format_status_line(token)
    left = days_remaining(token)
    renew_days = get_renew_days()
    if left is not None and renew_days > 0 and left <= renew_days:
        report.append({"stage": "token", "status": "renew-soon", "detail": line})
    else:
        report.append({"stage": "token", "status": "ok", "detail": line})


# --------------------------------------------------------------------------- #
# Step 2 — workspace template (OVERRIDE merge / DEFAULT CLAUDE.md)
# --------------------------------------------------------------------------- #
def _step_workspace(workspace: Path, *, server_url: str, token: str, report: list[dict]) -> None:
    from cli.lib.initial_workspace import (
        apply_update,
        download_zip,
        probe_status,
        write_agnes_env,
    )
    from cli.lib.override import read_override_metadata
    from src.initial_workspace import is_override_workspace

    status = probe_status(server_url, token)
    if status is not None and status.configured:
        # OVERRIDE mode (Initial Workspace Template configured).
        if not status.synced:
            report.append(
                {
                    "stage": "workspace",
                    "status": "skipped",
                    "detail": "template configured but not synced (ask admin to Sync now)",
                }
            )
            return
        sentinel = read_override_metadata(workspace) or {}
        if not is_override_workspace(workspace):
            report.append(
                {
                    "stage": "workspace",
                    "status": "skipped",
                    "detail": "no override sentinel; run `agnes update-workspace` once interactively",
                }
            )
        elif sentinel.get("template_sha") == status.template_sha:
            report.append({"stage": "workspace", "status": "ok", "detail": "template already current"})
        else:
            # A missing stored baseline (pre-baseline install, or a workspace
            # that was moved — the baseline is keyed by absolute path) is safe:
            # the 3-way engine backs up every file that differs from the new
            # template before overwriting, and apply_update() then establishes
            # the baseline so the next run is a precise merge.
            new_zip = download_zip(server_url, token)
            result = apply_update(workspace, new_zip, status, server_url, token, agnes_version=_agnes_version())
            report.append(
                {
                    "stage": "workspace",
                    "status": "merged",
                    "detail": {
                        "created": len(result.created),
                        "updated": len(result.updated),
                        "backed_up": [b for _, b in result.backed_up],
                        "template_sha": (status.template_sha or "")[:10],
                    },
                }
            )
        # Refresh per-tenant operator params regardless of the merge decision.
        # A raised exception is a real failure (surface it in the report); a
        # None return is a soft signal (older server / empty overlay), not an
        # error — reported as skipped so a genuine write failure stands out.
        try:
            env_path = write_agnes_env(workspace, server_url, token)
        except Exception as exc:  # noqa: BLE001 — best-effort env refresh
            report.append({"stage": "env", "status": "error", "detail": f"{type(exc).__name__}: {exc}"})
        else:
            if env_path is not None:
                report.append({"stage": "env", "status": "ok", "detail": f"wrote {env_path.name}"})
            else:
                report.append(
                    {
                        "stage": "env",
                        "status": "skipped",
                        "detail": "no connector params (older server / empty overlay)",
                    }
                )
    else:
        # DEFAULT mode — no template; CLAUDE.md is server-rendered.
        _refresh_default_claude_md(workspace, server_url=server_url, token=token, report=report)


def _refresh_default_claude_md(workspace: Path, *, server_url: str, token: str, report: list[dict]) -> None:
    from cli.client import api_get
    from cli.lib.pull import _override_server_env
    from src.initial_workspace import _prune_backups, _unique_bak_path

    with _override_server_env(server_url, token):
        resp = api_get("/api/welcome", params={"server_url": server_url})
    resp.raise_for_status()
    content = resp.json().get("content", "")
    if not content:
        report.append({"stage": "workspace", "status": "skipped", "detail": "empty /api/welcome content"})
        return
    claude_md = workspace / "CLAUDE.md"
    if claude_md.exists():
        on_disk = claude_md.read_text(encoding="utf-8")
        # #1476: a pure date rollover (the shipped template's "generated
        # {{ today }}" stamp, or a date in an admin override) must not by
        # itself count as a change — mask date-shaped substrings before
        # comparing. A genuine content change still differs after masking.
        if on_disk == content or _mask_volatile_dates(on_disk) == _mask_volatile_dates(content):
            report.append({"stage": "workspace", "status": "ok", "detail": "CLAUDE.md already current"})
            return
    backup_name = ""
    if claude_md.exists():
        bak = _unique_bak_path(claude_md.with_name(f"CLAUDE.md.bak.{_utc_stamp()}"))
        bak.write_bytes(claude_md.read_bytes())
        backup_name = bak.name
        _prune_backups(claude_md)
    claude_md.write_text(content, encoding="utf-8")
    report.append(
        {
            "stage": "workspace",
            "status": "refreshed",
            "detail": f"CLAUDE.md updated{f' (backup {backup_name})' if backup_name else ''}",
        }
    )


# --------------------------------------------------------------------------- #
# Step 3 — Agnes-owned settings (hooks / statusline / commands), both modes
# --------------------------------------------------------------------------- #
def _step_agnes_owned(workspace: Path, *, report: list[dict]) -> None:
    from cli.lib.commands import install_claude_commands
    from cli.lib.hooks import install_claude_hooks

    install_claude_hooks(workspace)
    install_claude_commands(workspace)
    report.append({"stage": "agnes-owned", "status": "ok", "detail": "hooks / statusline / commands reasserted"})


# --------------------------------------------------------------------------- #
# Step 4 — marketplace plugins (bootstrap if missing; full reconcile on drift)
# --------------------------------------------------------------------------- #
def _reassert_enabled_plugins() -> dict[str, list[str]]:
    """Ensure the workspace `settings.json` enables every plugin in the LOCAL
    marketplace manifest; return `{"enabled": [...], "settings_pruned": [...]}`
    — the names newly flipped on and the stale entries dropped.

    Runs on the no-drift path: the marketplace content is current, but step 2's
    template merge (or a manual edit) may have reset `settings.json` and dropped
    the stack's `enabledPlugins` — leaving plugins installed but DISABLED in the
    workspace. Reasserting from the on-disk manifest is cheap (no fetch) and
    idempotent (`_enable_plugins_in_workspace_settings` writes only on change),
    and mirrors how step 3 reasserts hooks/statusline unconditionally. cwd is the
    workspace here (the update callback chdir'd into it), which is where
    `_enable_plugins_in_workspace_settings` writes."""
    from cli.commands.refresh_marketplace import (
        _enable_plugins_in_workspace_settings,
        _read_marketplace_plugin_versions,
    )

    manifest = _read_marketplace_plugin_versions()
    if not manifest:
        return {"enabled": [], "settings_pruned": []}
    ev: dict[str, list[str]] = {"installed": [], "updated": [], "enabled": []}
    _enable_plugins_in_workspace_settings(manifest, events=ev)
    return {"enabled": ev["enabled"], "settings_pruned": ev.get("settings_pruned", [])}


# --------------------------------------------------------------------------- #
# Step 3b — launcher shortcut (migrates a legacy rc-function launcher to the
# ~/.local/bin script; no-op when the user never had a shortcut, preserving
# the `agnes init --no-shortcut` opt-out).
# --------------------------------------------------------------------------- #
def _step_launcher(workspace: Path, *, report: list[dict]) -> None:
    from cli.lib.shortcut import migrate_launcher_shortcut

    status = migrate_launcher_shortcut(workspace, quiet=True)
    report.append({"stage": "launcher", "status": "ok", "detail": status})


def _step_marketplace(*, report: list[dict], quiet: bool = False) -> None:
    import contextlib
    import io

    from cli.commands.refresh_marketplace import _EXIT_MARKETPLACE_DRIFT, refresh_marketplace
    from cli.lib.marketplace import CLONE_DIR

    def _invoke(*, check: bool, bootstrap: bool) -> int:
        # refresh_marketplace echoes progress to stdout; under --json/--quiet
        # that corrupts the single-JSON contract (and the quiet-hook promise),
        # so capture and discard its stdout. Its git subprocesses use
        # capture_output=True, so nothing leaks past this at the fd level.
        sink = contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext()
        try:
            with sink:
                refresh_marketplace(check=check, bootstrap=bootstrap)
            return 0
        except typer.Exit as exc:
            return int(getattr(exc, "exit_code", 0) or 0)

    if not (CLONE_DIR / ".git").is_dir():
        rc = _invoke(check=False, bootstrap=True)
        report.append(
            {
                "stage": "marketplace",
                "status": "bootstrapped" if rc == 0 else "error",
                "detail": f"clone missing; bootstrap exit={rc}",
            }
        )
        return

    rc = _invoke(check=True, bootstrap=False)
    if rc == _EXIT_MARKETPLACE_DRIFT:
        full = _invoke(check=False, bootstrap=False)
        report.append(
            {
                "stage": "marketplace",
                "status": "reconciled" if full == 0 else "error",
                "detail": f"drift detected; reconcile exit={full}",
            }
        )
    elif rc == 0:
        # No marketplace drift — but the workspace settings.json may have been
        # reset since the last reconcile (step 2's template merge drops the
        # stack's enabledPlugins). Reassert them from the local manifest so
        # installed stack plugins stay ENABLED; cheap (no fetch) and idempotent.
        # Same sink as _invoke: _enable_plugins_in_workspace_settings echoes
        # its enable/drop notices, and this call sits OUTSIDE the wrapped
        # refresh_marketplace invocation, so under --json/--quiet the lines
        # would leak to raw stdout and break the single-JSON / silent-hook
        # contract (Devin Review on #1105). The outcome reaches the caller
        # via the run report below instead.
        sink = contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext()
        with sink:
            reassert = _reassert_enabled_plugins()
        enabled = reassert["enabled"]
        pruned = reassert["settings_pruned"]
        if enabled or pruned:
            parts = []
            if enabled:
                parts.append(f"re-enabled {len(enabled)} stack plugin(s) in settings.json: " + ", ".join(enabled))
            if pruned:
                parts.append(
                    f"dropped {len(pruned)} stale settings entr{'y' if len(pruned) == 1 else 'ies'}: "
                    + ", ".join(pruned)
                )
            report.append(
                {
                    "stage": "marketplace",
                    "status": "enabled" if enabled else "pruned",
                    "detail": "; ".join(parts),
                }
            )
        else:
            report.append({"stage": "marketplace", "status": "ok", "detail": "plugins already current"})
    else:
        report.append({"stage": "marketplace", "status": "error", "detail": f"check exit={rc}"})


# --------------------------------------------------------------------------- #
# Step 4a½ — user-scope (all-repositories) layer convergence
# --------------------------------------------------------------------------- #
def _step_global(*, report: list[dict], quiet: bool = False) -> None:
    """Converge the user-scope layer (spec §7.2). Workspace-independent —
    runs OUTSIDE the workspace chdir block and also when no workspace
    exists. Gated on the `global_scope` config flag; `global_hook: false`
    (set by `agnes global enable --no-hook`) keeps the hook un-asserted."""
    import contextlib
    import io

    cfg = load_config()
    if not cfg.get("global_scope"):
        report.append({"stage": "global", "status": "skipped", "detail": "global_scope not enabled"})
        return
    from cli.commands.global_scope import run_convergence

    sub_report: list[dict] = []
    sink = contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext()
    with sink:
        run_convergence(want_hook=bool(cfg.get("global_hook", False)), force=False, report=sub_report)
    bad = [r for r in sub_report if r.get("status") == "error"]
    report.append(
        {
            "stage": "global",
            "status": "error" if bad else "ok",
            "detail": "; ".join(f"{r['stage']}={r['status']}" for r in sub_report),
        }
    )


# --------------------------------------------------------------------------- #
# Step 4b — session catch-up push
# --------------------------------------------------------------------------- #
def _step_push(*, report: list[dict]) -> None:
    """Upload session transcripts (+ CLAUDE.local.md) the SessionEnd hook missed.

    SessionEnd is not a dependable trigger: closing the terminal window (or a
    crash / kill) can take Claude Code down before it gets to run the hook, so
    `agnes push` never fires for that session. The transcript then sits on disk
    and stays invisible in the admin session views, which read the summary
    table the server-side pipeline fills and have no filesystem fallback.
    SessionStart, by contrast, cannot be missed — the session is being created.

    `agnes push` is already a full folder scan with ledger dedup by
    (session_id, byte size), so ONE call here uploads everything earlier runs
    missed — including a session still open in another window whose transcript
    has grown since its last upload. Re-uploading is safe end to end: the
    server overwrites by filename, and the pipeline purges a session's rows
    before reprocessing a changed transcript.

    Note the anchor asymmetry, deliberately left as-is: push locates the
    session folder from the `workspace_root` config key ONLY, while
    `agnes update` resolves its workspace from `AGNES_LOCAL_DIR` first (see
    `_resolve_workspace`). Running `agnes update` against a workspace other
    than the configured anchor therefore pushes the anchor's sessions. That is
    push's existing contract — the SessionEnd hook behaves identically — and
    the two agree in practice because `update` backfills the anchor before the
    steps run. Changing it would change the SessionEnd path too.

    Reuses the `push` command callback rather than its internals so the two
    triggers can never drift in dedup, redaction, or locking behavior. `--json`
    yields a machine-readable summary for the report line, and its stdout is
    captured unconditionally so `agnes update`'s own `--json` (exactly one
    object) and `--quiet` (silence) stdout contracts hold. push's own lock is a
    different file from update.lock, so there is no deadlock.

    The just-started session's own transcript is uploaded too, as a nearly
    empty stub that the SessionEnd push replaces once it has grown — one extra
    upload plus one extra pipeline reprocess per session. That is deliberate:
    filtering it out would mean either forking push's scan or reading the
    session id from hook stdin, which is precisely the unreliable input (empty
    on macOS) that push was rewritten to stop depending on.
    """
    import contextlib
    import io

    from cli.commands.push import push

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        push(quiet=False, as_json=True, dry_run=False)
    raw = buf.getvalue().strip()

    if not raw:
        # push returns before emitting its JSON when another push holds the
        # per-workspace lock — typically the previous session's still-running
        # detached SessionEnd run, which is doing this very same folder scan.
        # That is a success, not an error.
        report.append({"stage": "push", "status": "skipped", "detail": "another push already running"})
        return

    try:
        summary = json.loads(raw.splitlines()[-1])
    except ValueError:
        report.append({"stage": "push", "status": "error", "detail": f"unparseable push output: {raw[:200]}"})
        return

    detail = f"{summary.get('sessions', 0)} session(s)"
    if summary.get("local_md"):
        detail += " + CLAUDE.local.md"
    if summary.get("private_skipped"):
        detail += f", {summary['private_skipped']} private skipped"
    errors = summary.get("errors") or []
    if errors:
        report.append({"stage": "push", "status": "error", "detail": f"{detail}; errors={errors}"})
    elif not summary.get("sessions") and not summary.get("local_md"):
        # Zero uploads and no CLAUDE.local.md means push deliberately made
        # no HTTP request at all (the capability probe is gated on having
        # work), so this run proved nothing about the saved credential.
        # Report skipped, not ok — _step_bootstrap_token_cleanup counts a
        # non-error/non-skipped push as an authenticated round-trip, and a
        # no-op must never delete the ~/.agnes/token recovery input on a
        # run where workspace/pull failed auth.
        report.append({"stage": "push", "status": "skipped", "detail": "nothing to upload; no server request made"})
    else:
        report.append({"stage": "push", "status": "ok", "detail": detail})


# --------------------------------------------------------------------------- #
# Step 5 — data pull
# --------------------------------------------------------------------------- #
def _step_pull(workspace: Path, *, server_url: str, token: str, quiet: bool, report: list[dict]) -> None:
    from cli.lib.pull import run_pull

    result = run_pull(server_url, token, workspace, dry_run=False, skip_materialize=False, show_progress=not quiet)
    if getattr(result, "errors", None):
        report.append({"stage": "pull", "status": "error", "detail": list(result.errors)})
    else:
        detail = f"{result.tables_updated} tables, {result.parquets_total} parquets"
        # The SessionStart hook runs THIS command, detached, with stdout and
        # stderr both sent to /dev/null — so a name withheld from a snapshot is
        # taken on a path where nothing the pull prints can ever be seen. The
        # run report is the only durable channel (it lands in
        # `.claude/agnes/update.log`), so it has to carry the withheld names or
        # the analyst meets "Table with name <x> does not exist" with no
        # explanation anywhere (#1129 review).
        withheld = list(getattr(result, "snapshot_views_blocked", []) or [])
        if withheld:
            shown = ", ".join(sorted(withheld)[:5])
            more = f" (+{len(withheld) - 5} more)" if len(withheld) > 5 else ""
            detail += (
                f"; withheld {len(withheld)} snapshot name(s) now owned by a table you can no longer "
                f"read locally: {shown}{more} — re-create with `agnes snapshot create <table> --as <name>`"
            )
        report.append({"stage": "pull", "status": "ok", "detail": detail})


# --------------------------------------------------------------------------- #
# Step 6 — report
# --------------------------------------------------------------------------- #
def _write_report(workspace: Path, entry: dict) -> Optional[Path]:
    log = workspace / ".claude" / "agnes" / "update.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        # Rotate: keep the tail when the file grows past the cap so the log is
        # bounded but still carries recent history.
        if log.exists() and log.stat().st_size > _REPORT_MAX_BYTES:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return log
    except OSError:
        return None


@update_app.callback(invoke_without_command=True)
def update(
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress progress output (SessionStart hook path). Errors still go to the report."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the run report as a single JSON object on stdout."),
) -> None:
    """Converge the workspace + CLI; safe to run repeatedly and from any cwd."""
    from cli.lib.push_lock import acquire_path_or_skip

    # Disable nested auto-update detection in this process AND its children
    # (run_pull / refresh-marketplace / the smoke-test `agnes --version`).
    os.environ["AGNES_NO_UPDATE_CHECK"] = "1"

    report: list[dict] = []

    # The lock lives under the config dir; creating/accessing it can fail
    # (read-only FS, permission denied) BEFORE the guarded block. Degrade into
    # a config-error report + clean exit rather than a raw traceback out of the
    # command whose whole job is to repair a broken install.
    try:
        lock_file = _config_dir() / "update.lock"
    except OSError as exc:
        report.append(
            {"stage": "config", "status": "error", "detail": f"cannot access config dir: {type(exc).__name__}: {exc}"}
        )
        entry = {"ts": _utc_stamp(), "agnes_version": _agnes_version(), "workspace": None, "steps": report}
        if as_json:
            typer.echo(json.dumps(entry))
        elif not quiet:
            typer.echo("agnes update — convergence report:")
            for step in report:
                typer.echo(f"  [{step['status']}] {step['stage']}: {step['detail']}")
        raise typer.Exit(0)

    with acquire_path_or_skip(lock_file) as lock:
        if lock is None:
            if not quiet and not as_json:
                typer.echo("Another `agnes update` is already running — exiting.")
            raise typer.Exit(0)

        # `agnes update` is the repair path, so read ALL persisted-config
        # values under the best-effort boundary: get_server_url/get_token AND
        # _resolve_workspace (which re-reads config.yaml via get_workspace_root)
        # must degrade into skipped workspace steps + a report line, not a raw
        # traceback out of the command meant to fix a broken install. A None
        # token routes to the "no token configured" skip below; a None
        # workspace routes to the "no initialised workspace" skip.
        server_url = ""
        token: Optional[str] = None
        workspace: Optional[Path] = None
        try:
            server_url = get_server_url()
            token = get_token()
            workspace = _resolve_workspace()
        except Exception as exc:  # noqa: BLE001 — best-effort, mirror _run_step
            report.append({"stage": "config", "status": "error", "detail": f"{type(exc).__name__}: {exc}"})

        # Legacy-fleet backfill of the `workspace_root` config anchor.
        # Workspaces initialized before that key existed relied on the old
        # `agnes self-upgrade` SessionStart hook, whose Typer callback called
        # `_maybe_backfill_workspace_root()`. `agnes update` is now the SOLE
        # SessionStart entry and reaches `_do_install...` directly (via
        # `_step_cli`), bypassing that backfill — so without this the anchor
        # never lands and the SessionEnd `agnes push --quiet` silently uploads
        # nothing on those clients (push reads `workspace_root` from config
        # only; it does NOT fall back to AGNES_LOCAL_DIR). Best-effort: the
        # helper is guarded by the `.claude/init-complete` sentinel, writes
        # ONLY when unset, and swallows its own errors — no network, no lock.
        try:
            from cli.commands.self_upgrade import _maybe_backfill_workspace_root

            _maybe_backfill_workspace_root()
        except Exception:  # noqa: BLE001 — best-effort, mirror _run_step
            pass

        # --json / --quiet must keep stdout clean for their contracts: --json
        # emits exactly ONE JSON object, the SessionStart --quiet hook emits
        # nothing. Child steps otherwise print progress, so route them through
        # a combined quiet flag.
        step_quiet = quiet or as_json

        # Step 1 — CLI binary (workspace-independent; always runs).
        _run_step("cli", lambda: _step_cli(quiet=step_quiet, report=report), report)

        # Step 1b — token expiry (#477; workspace-independent, report-only).
        _run_step("token", lambda: _step_token(token, report), report)

        if workspace is None:
            report.append(
                {
                    "stage": "workspace",
                    "status": "skipped",
                    "detail": "no initialised workspace found (run from the workspace or `agnes init`)",
                }
            )
        elif not token:
            report.append({"stage": "workspace", "status": "skipped", "detail": "no token configured"})
        else:
            # Workspace-relative steps need cwd == workspace (marketplace installs
            # plugins with --scope project into cwd, and the report log lands
            # under the workspace). If we can't enter the workspace (deleted,
            # not a dir, unreadable), DO NOT run those steps from the launching
            # cwd — that would scatter plugin/settings writes into an unrelated
            # directory. Record the failure and skip; the workspace-independent
            # CLI step above already ran.
            prev_cwd = Path.cwd()
            try:
                os.chdir(workspace)
            except OSError as exc:
                report.append(
                    {
                        "stage": "workspace",
                        "status": "error",
                        "detail": f"cannot enter workspace {workspace}: {exc}; skipped workspace steps",
                    }
                )
            else:
                try:
                    _run_step(
                        "workspace",
                        lambda: _step_workspace(workspace, server_url=server_url, token=token, report=report),
                        report,
                    )
                    _run_step("agnes-owned", lambda: _step_agnes_owned(workspace, report=report), report)
                    _run_step("launcher", lambda: _step_launcher(workspace, report=report), report)
                    _run_step("marketplace", lambda: _step_marketplace(report=report, quiet=step_quiet), report)
                    # Before the pull: the upload is the latency-sensitive half
                    # (a missed session stays invisible server-side until it
                    # lands), while a large parquet download can take minutes.
                    _run_step("push", lambda: _step_push(report=report), report)
                    _run_step(
                        "pull",
                        lambda: _step_pull(
                            workspace, server_url=server_url, token=token, quiet=step_quiet, report=report
                        ),
                        report,
                    )
                    _run_step(
                        "bootstrap-token",
                        lambda: _step_bootstrap_token_cleanup(report),
                        report,
                    )
                finally:
                    try:
                        os.chdir(prev_cwd)
                    except OSError:
                        pass

        # User-scope layer (spec §7.2) — workspace-independent by design:
        # runs from the LAUNCHING cwd (safe: the user target performs no
        # cwd writes) and also when workspace is None.
        _run_step("global", lambda: _step_global(report=report, quiet=step_quiet), report)

        entry = {
            "ts": _utc_stamp(),
            "agnes_version": _agnes_version(),
            "workspace": str(workspace) if workspace else None,
            "steps": report,
        }
        log_path = _write_report(workspace, entry) if workspace else None

    if as_json:
        typer.echo(json.dumps(entry))
        return
    if quiet:
        return

    typer.echo("agnes update — convergence report:")
    for step in report:
        typer.echo(f"  [{step['status']}] {step['stage']}: {step['detail']}")
    if log_path:
        typer.echo(f"Report: {log_path}")
    if any(s["stage"] == "cli" and s["status"] == "updated" for s in report):
        typer.echo("CLI was updated — run `agnes update` once more to finish converging on the new version.")
