"""`agnes init` — bootstrap an analyst workspace.

Single-paste flow: web user clicks "Generate prompt" on /setup?role=analyst,
pastes into Claude Code in an empty folder; Claude runs `agnes init` (among
other steps). Non-interactive: --token + --server-url required.

Steps in order:
1. Detect existing workspace (`CLAUDE.md` containing the init marker) — exit 1
   unless --force, with a typed `partial_state` error.
2. Verify the PAT via `GET /api/catalog/tables` — typed `auth_failed` on 401,
   `server_unreachable` on network error.
3. Persist server URL + PAT to `~/.config/agnes/` so subsequent `agnes pull` /
   `agnes push` invocations (including the SessionStart/End hooks installed
   below) inherit the credentials without env vars.
4. Fetch the rendered CLAUDE.md from `GET /api/welcome` (server-rendered,
   RBAC-filtered, role-aware).
5. Seed `.claude/settings.json` with default model + permissions, then call
   `cli.lib.hooks.install_claude_hooks` to merge in the SessionStart/End hook
   commands. Then call `cli.lib.commands.install_claude_commands` to drop
   the Agnes-managed slash commands (today: `/update-agnes-plugins`) into
   `<workspace>/.claude/commands/`. Idempotent on re-run.
6. Write the `.claude/CLAUDE.local.md` stub only when absent — `--force`
   regenerates CLAUDE.md but **never** clobbers the operator-edited
   CLAUDE.local.md.
7. Run the first `cli.lib.pull.run_pull` so the workspace ships with current
   parquets, DuckDB views, and the corporate-memory bundle.
8. Render `AGNES_WORKSPACE.md` from `config/agnes_workspace_template.txt` —
   client-side template, three placeholders.

Errors render via `cli/error_render.py:render_error` with typed `kind` values
(`auth_failed`, `server_unreachable`, `partial_state`, `manifest_unauthorized`)
matching the rest of the CLI surface.

Task 18 will register `init_app` on the root Typer app.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get
from cli.config import _config_dir, save_config, save_token
from cli.error_render import render_error
from cli.server_moved import classify_redirect, is_redirect
from cli.lib.automode import (
    TrustResult,
    ensure_marketplace_trusted,
    marketplace_trust_entries,
    marketplace_trust_state,
    prune_stale_loopback_declarations,
)
from cli.lib.commands import install_claude_commands
from cli.lib.hooks import install_claude_hooks
from cli.lib.initial_workspace import apply_override, probe_status
from cli.lib.marketplace import configured_marketplace_host
from cli.lib.pull import PullResult, _override_server_env, run_pull
from cli.lib.session_paths import user_settings_path
from cli.lib.shortcut import install_launcher_shortcut


# Legacy substring that flags an already-bootstrapped workspace. Pre-rebrand
# default CLAUDE.md templates rendered `# {{ instance.name }} — AI Data
# Analyst`, so the string appears in every server-rendered CLAUDE.md from
# those CLI versions. Current inits are detected via the `.claude/init-complete`
# sentinel instead (the default template no longer contains this string);
# the substring check is kept only for pre-#259 workspaces.
_INIT_MARKER = "AI Data Analyst"

# Sentinel written at the very END of a successful `agnes init`. Existence
# of CLAUDE.md alone is NOT a "workspace is initialized" signal because
# CLAUDE.md is written early in the flow — long before the parquet pull,
# the AGNES_WORKSPACE.md render, and the final summary. Killed runs
# (SIGKILL from the harness, network drop mid-pull, operator Ctrl-C)
# leave CLAUDE.md on disk but not this sentinel. The next `agnes init`
# can then resume without requiring `--force`, which would otherwise
# force a full re-download of any large materialized parquet that was
# 80 % complete. Issue #259.
_INIT_COMPLETE_FILE = ".claude/init-complete"


# Env vars that, when set to a non-existent path, cause every TLS handshake
# on the host to fail before Agnes itself runs. Past versions of the Agnes
# setup script's TLS trust block (and older bootstrap helpers) wrote
# pointers to ``~/.agnes/ca-bundle.pem`` into the user's persistent env
# (Windows User scope; shell rc files on POSIX). When the file goes away
# (re-init on a new VM, manual cleanup, machine swap) the pointers go
# stale — gws auth login, claude plugin marketplace add, even pip/uv,
# all fail with UnknownIssuer / FileNotFoundError. Reported by the
# Windows test user 2026-05-11. SSL_CERT_FILE in particular REPLACES
# (not appends to) the trust store, so a stale pointer is silently
# catastrophic.
_CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO")


# Directories `agnes init` refuses to use as a workspace, exact match after
# path resolution. Initializing into any of these scatters `.claude/`,
# `.agnes/`, `AGNES_WORKSPACE.md` and marketplace clones across a directory
# that already has unrelated meaning ($HOME, filesystem roots, system
# paths). The refusal lives here — in code, with an actionable hint — so
# the install prompt only needs one line about it instead of a prose
# decision tree the setup agent had to interpret.
_UNSAFE_WORKSPACE_PATHS = (
    "/",
    "/tmp",
    "/etc",
    "/usr",
    "/var",
    "/opt",
    "/root",
    "/bin",
    "/sbin",
    "/boot",
    "/sys",
    "/proc",
)


def _unsafe_workspace_reason(workspace: Path) -> Optional[str]:
    """Return a short reason when ``workspace`` is an unsafe init target,
    ``None`` when it is fine.

    Exact matches only — a subdirectory of $HOME (the documented default
    ``~/Desktop/<brand>``) is a normal workspace. Comparison happens on
    resolved paths so macOS' ``/tmp`` → ``/private/tmp`` symlink (and any
    similar alias) cannot dodge the list.
    """
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):  # no resolvable home — skip that check
        home = None
    if home is not None and workspace == home:
        return "your home directory"
    # Filesystem root, covering Windows drive roots (C:\) as well.
    if workspace == Path(workspace.anchor):
        return "a filesystem root"
    unsafe_resolved = set()
    for p in _UNSAFE_WORKSPACE_PATHS:
        try:
            unsafe_resolved.add(Path(p).resolve())
        except OSError:
            continue
    if workspace in unsafe_resolved:
        return "a system directory"
    return None


def _chmod_workspace_hooks(workspace: Path) -> None:
    """Set execute bit on every `.sh` under `<workspace>/.claude/hooks/`.

    Claude Code's plugin install path doesn't always preserve the execute
    bit on shell hook files — depending on the archive format the plugin
    ships in (zip, no-bit-preserving git checkout config, etc.), hooks
    can land on disk as `rw-r--r--` and every fire returns Permission
    denied. The user-visible symptom is a silent SessionStart / PreToolUse
    failure that looks like the hooks just aren't installed.

    Best-effort. No-op on Windows NTFS via Git Bash (chmod is meaningless
    on NTFS without ACLs). Failures are swallowed — a hook the user can
    still read is no worse than the pre-fix baseline.
    """
    hooks_dir = workspace / ".claude" / "hooks"
    if not hooks_dir.is_dir():
        return
    for path in hooks_dir.rglob("*.sh"):
        try:
            current = path.stat().st_mode
            # Add user/group/other execute. Same effect as `chmod +x`.
            path.chmod(current | 0o111)
        except OSError:
            pass


def _is_windows_host() -> bool:
    """True when the Python interpreter sees Windows underneath.

    Covers native Python on Windows (``sys.platform == 'win32'``) and
    Git Bash / MSYS launchers (interpreter still reports win32; the
    bash shell wrapper is irrelevant for User-scope env-var management).
    POSIX-only edge cases (WSL with `windows` in /proc/version) stay on
    the POSIX path — User-scope env vars don't exist there in the
    Windows-registry sense, so the cleanup is a no-op.
    """
    return sys.platform == "win32"


def _cleanup_stale_ca_env_vars() -> None:
    """Clear stale SSL_CERT_FILE / REQUESTS_CA_BUNDLE / GIT_SSL_CAINFO
    pointers from the current process AND (on Windows) from User scope.

    Two layers because the failure mode hits both:
    1. Current-process env — what the upcoming `api_get` call to
       /api/catalog/tables actually reads. Without clearing it here, the
       httpx call falls over with a FileNotFoundError before init can
       finish step 2.
    2. Windows User-scope env — what every future shell + every native
       Windows tool (gws, claude.exe, pip, uv) inherits. Without
       clearing it there, the user re-hits the same wall the next time
       they open PowerShell — exactly what the 2026-05-11 Windows test
       user reported ("the init was supposed to clear these but they
       persisted; fixed by removing both vars from User scope").

    Best-effort. We only delete a var when it points at a path that does
    NOT exist on disk — intentional operator config (e.g. SSL_CERT_FILE
    pointing at a corporate certifi bundle) is preserved. PowerShell
    invocation failures are swallowed silently because the init shouldn't
    abort on a defensive cleanup helper.
    """
    cleared_process: list[tuple[str, str]] = []
    for var in _CA_ENV_VARS:
        cur = os.environ.get(var)
        if cur and not Path(cur).exists():
            del os.environ[var]
            cleared_process.append((var, cur))
    for name, path in cleared_process:
        typer.echo(f"agnes init: cleared stale process env {name}={path} (file does not exist)")

    if not _is_windows_host():
        return

    # Build a single PowerShell invocation that checks + clears all three
    # User-scope vars in one shot. Quoting strategy: pass the script via
    # -Command with single-quoted strings inside so Python's f-string
    # composition stays simple. We use [Environment]::SetEnvironmentVariable
    # with $null (the documented way to delete a User-scope env var on
    # Windows; setx has no delete verb).
    statements = []
    for var in _CA_ENV_VARS:
        statements.append(
            "$cur = [Environment]::GetEnvironmentVariable('" + var + "', 'User'); "
            "if ($cur -and -not (Test-Path -LiteralPath $cur)) { "
            "[Environment]::SetEnvironmentVariable('" + var + "', $null, 'User'); "
            "Write-Host ('agnes init: cleared stale User-scope " + var + "=' + $cur + ' (file does not exist)') "
            "}"
        )
    ps_script = "; ".join(statements)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # PowerShell missing (cygwin-only environments), or hung. Skip —
        # the current-process cleanup above already covers the immediate
        # `api_get` failure; persistent state cleanup is best-effort.
        return
    if result.stdout:
        # Forward PowerShell's confirmation lines to the user so the
        # cleanup is auditable. stderr from PowerShell (rare here) is
        # swallowed — the worst it'd add is "execution policy" noise on
        # restricted hosts, which isn't actionable.
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                typer.echo(line)


def _stdin_is_interactive() -> bool:
    """Is there a human on the other end to answer a prompt?

    A named seam rather than an inline ``sys.stdin.isatty()`` so both branches
    are reachable from tests: Click's ``CliRunner`` swaps ``sys.stdin`` during
    ``invoke``, so patching the stream a test can see does not affect the
    stream the command reads. A closed or detached stdin raises rather than
    answering, and that is a "no human" too.
    """
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _trust_optin_command(server_url: str = "") -> str:
    """The re-run command, complete enough to paste.

    `agnes init` requires `--server-url` (or a bundle), so printing the flags
    alone handed the reader a command that fails the moment they run it.
    (Devin Review on #1262.)
    """
    base = "agnes init --force --trust-marketplace-host"
    return f"{base} --server-url {server_url}" if server_url else base


def _maybe_declare_marketplace_trust(host: str, decision: Optional[bool], server_url: str = "") -> None:
    """Declare *host* in the user-scope ``autoMode.environment`` — if asked to.

    This is the one thing ``agnes init`` does outside the workspace it was
    pointed at: ``~/.claude/settings.json`` applies to every project on the
    machine. It used to happen silently, announced only by a line printed
    afterwards, which is how an agent came to report that a tool had written
    trust claims about itself into its configuration without anyone
    authorizing it. The claim was true and the mechanism is the sanctioned
    one; being unasked is what made it wrong.

    So the write is opt-in, and the three paths differ on purpose:

    - ``--trust-marketplace-host`` / ``--no-trust-marketplace-host``: the
      operator already decided; do that.
    - Interactive: show the file and the exact lines, then ask. Default No —
      declining costs one approval prompt later, agreeing changes machine-wide
      settings, and the cheaper mistake should be the default.
    - Non-interactive (the pasted-install path, where the "operator" is an
      agent relaying a script): skip, and say how to opt in. Claude Code's
      auto mode may then ask before ``agnes refresh-marketplace --bootstrap``
      installs plugins — which is the human deciding in the moment, the thing
      the declaration was pre-empting.
    """
    settings_path = user_settings_path()

    # Dead ephemeral ports go first, before any consent gate: every local
    # dev-server restart mints a new 127.0.0.1:<port> "host", so the pairs
    # written for previous ports piled up forever — each one blessing an
    # address the OS will hand to whatever local process asks next. Removing
    # this tool's own stale grants NARROWS trust, the opposite of the write
    # the consent prompt exists for, so no path skips it — not even
    # --no-trust-marketplace-host, which is a request for less trust.
    pruned = prune_stale_loopback_declarations(settings_path, host)
    if pruned:
        typer.echo(
            f"Removed stale auto-mode declarations for previous local ports from {settings_path}: " + ", ".join(pruned)
        )

    if decision is False:
        typer.echo(f"Skipping the auto-mode trust declaration for {host} (--no-trust-marketplace-host).")
        return

    # Nothing to decide when the declaration is already in place and current:
    # asking again on every re-run nags about a settled question, and the
    # unattended branch announced it was "not declaring" something that had
    # been declared long ago. (Devin Review on #1262.)
    if marketplace_trust_state(settings_path, host) is TrustResult.ALREADY_PRESENT:
        typer.echo(f"{host} was already declared in {settings_path} (autoMode.environment).")
        return

    # A machine carrying OUR retired wording is refreshed without asking, in
    # every path including the unattended one. That is not a new declaration:
    # the host is already declared, the trust already granted; only the words
    # this tool wrote about itself change, and leaving an agent-facing claim
    # in place that an agent flagged is the worse of the two. Asking for
    # consent to declare something already declared is also the nag the check
    # above removes. (Devin Review on #1262.)
    if marketplace_trust_state(settings_path, host) is TrustResult.REWRITTEN:
        result = ensure_marketplace_trusted(settings_path, host)
        if result is TrustResult.REWRITTEN:
            typer.echo(
                f"Replaced the older declaration of {host} in {settings_path} (autoMode.environment) — "
                "the previous wording argued for a conclusion instead of describing the host."
            )
            return

    if decision is None:
        if not _stdin_is_interactive():
            typer.echo(
                f"Not declaring {host} as internal infrastructure: that writes to {settings_path}, "
                "which applies to every project on this machine, so it is not done unattended. "
                "Claude Code's auto mode may ask before `agnes refresh-marketplace --bootstrap` "
                "installs plugins — approve it there, or re-run with "
                f"`{_trust_optin_command(server_url)}` to declare it once."
            )
            return
        typer.echo("")
        typer.echo(f"Optional: declare {host} as internal infrastructure for Claude Code's auto mode.")
        typer.echo(f"This edits {settings_path} — your user-scope settings, which apply to")
        typer.echo("every project on this machine, not just this workspace. It would add:")
        for entry in marketplace_trust_entries(host):
            typer.echo(f"  - {entry}")
        typer.echo("Declining costs one approval prompt when the marketplace is first cloned.")
        if not typer.confirm("Add them?", default=False):
            typer.echo("Skipped. Approve the marketplace bootstrap when auto mode asks.")
            return

    result = ensure_marketplace_trusted(settings_path, host)
    if result is TrustResult.WRITTEN:
        typer.echo(f"Declared {host} in {settings_path} (autoMode.environment). Delete those two entries to undo.")
    elif result is TrustResult.REWRITTEN:
        typer.echo(
            f"Replaced the older declaration of {host} in {settings_path} (autoMode.environment) — "
            "the previous wording argued for a conclusion instead of describing the host."
        )
    elif result is TrustResult.ALREADY_PRESENT:
        typer.echo(f"{host} was already declared in {settings_path} (autoMode.environment).")
    else:
        # Someone who just said yes must not be told the change is in place: a
        # settings file that could not be read is the case where they later
        # wonder why auto mode keeps asking. The reason is on stderr above.
        typer.echo(
            f"Could not declare {host} in {settings_path} — nothing was saved (see the warning above). "
            "Approve the marketplace bootstrap when auto mode asks, or fix that file and re-run "
            f"`{_trust_optin_command(server_url)}`."
        )


init_app = typer.Typer(help="Bootstrap an analyst workspace in this directory")


@init_app.callback(invoke_without_command=True)
def init(
    server_url: Optional[str] = typer.Option(
        None,
        "--server-url",
        help="Agnes server URL. Required unless --bundle is provided.",
    ),
    token: Optional[str] = typer.Option(
        None,
        "--token",
        help=(
            "Personal access token. Prefer --token-file or the AGNES_TOKEN "
            "env var: an inline --token puts the secret into the command "
            "line, where it is visible in shell history and in process "
            "listings to every other user on the machine."
        ),
    ),
    token_file: Optional[str] = typer.Option(
        None,
        "--token-file",
        help=(
            "Path to a file whose first non-blank line is the PAT. Wins "
            "over AGNES_TOKEN env when both are set; loses to an explicit "
            "--token flag. The recommended way to pass the token: it stays "
            "out of argv, so it reaches neither shell history nor the "
            "process table."
        ),
    ),
    bundle: Optional[str] = typer.Option(
        None,
        "--bundle",
        help=(
            "Path to an Agnes Cowork Setup Bundle (a directory containing "
            ".agnes-bundle.json, or the .zip file itself). When provided, "
            "exchanges the embedded setup token for a PAT automatically — "
            "no --server-url or --token required. The bundle is one-use "
            "and its .agnes-bundle.json is deleted from disk after exchange."
        ),
    ),
    force: bool = typer.Option(False, "--force", help="Re-initialize an existing workspace"),
    as_admin: bool = typer.Option(
        False,
        "--as-admin",
        help=(
            "Admins only: give this workspace the FULL data-read surface "
            "(catalog + server-side query see every registered table) "
            "instead of the default stack-scoped one. Exchanges the saved "
            "PAT for a surface=all token via /cli/auth/rescope-surface "
            "(server re-checks admin membership). Parquet distribution "
            "stays stack-scoped either way — `agnes pull` downloads only "
            "your stack."
        ),
    ),
    trust_marketplace_host: Optional[bool] = typer.Option(
        None,
        "--trust-marketplace-host/--no-trust-marketplace-host",
        help=(
            "Declare this instance's marketplace host in ~/.claude/settings.json "
            "(autoMode.environment), so Claude Code's auto mode treats plugin installs "
            "from it as internal rather than as untrusted external code. That file "
            "applies to every project on this machine, not just this workspace, so it "
            "is asked for rather than assumed: without this flag an interactive run "
            "prompts and an unattended run skips. Skipping costs one approval prompt "
            "when the marketplace is first cloned."
        ),
    ),
    workspace_str: Optional[str] = typer.Option(None, "--workspace", help="Target dir (default: cwd)"),
    skip_materialize: bool = typer.Option(
        True,
        "--skip-materialize/--materialize",
        help=(
            "Skip materialized-mode tables on the first pull (default: on). "
            "The first init can otherwise spend tens of minutes silently "
            "downloading a single multi-GB scheduled-query parquet while "
            "every lighter table waits behind it. Materialized rows are "
            "still discoverable via `agnes catalog`; fetch them on demand "
            "with a later `agnes pull` (no flag needed there), or force "
            "them into this first pull with --materialize."
        ),
    ),
    no_shortcut: bool = typer.Option(
        False,
        "--no-shortcut",
        help=(
            "Skip installing the one-word launcher script into ~/.local/bin "
            "(POSIX) or ~/.local/bin/<word>.cmd (Windows). By default "
            "`agnes init` installs an executable script named after the "
            "workspace folder so you can launch from any terminal."
        ),
    ),
):
    """Bootstrap workspace: auth, CLAUDE.md, hooks, first pull, AGNES_WORKSPACE.md."""
    workspace = Path(workspace_str).resolve() if workspace_str else Path.cwd()

    # ------------------------------------------------------------------
    # Unsafe-workspace guard — FIRST, before the bundle exchange (which
    # consumes a one-use setup token) and before any network call or
    # filesystem write, so a refusal has zero side effects.
    # ------------------------------------------------------------------
    unsafe_reason = _unsafe_workspace_reason(workspace)
    if unsafe_reason is not None:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "unsafe_workspace",
                        "hint": (
                            f"{workspace} is {unsafe_reason} — initializing here "
                            "would scatter .claude/, .agnes/ and workspace files "
                            "across it. Create a dedicated workspace folder "
                            "(e.g. ~/Desktop/Agnes), cd into it, and re-run "
                            "`agnes init` from there."
                        ),
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(code=1)

    # ------------------------------------------------------------------
    # Bundle flow (M4): when --bundle is provided, exchange the embedded
    # setup token for a PAT before the normal token-resolution path.
    #
    # Precedence after bundle handling: --server-url overrides the bundle's
    # server_url if both are given (unusual but safe).
    # ------------------------------------------------------------------
    bundle_json_path: Optional[Path] = None
    if bundle:
        bundle_path = Path(bundle).expanduser().resolve()
        try:
            if bundle_path.is_dir():
                # Directory: prefer `agnes-bundle.json` (no dot — current format,
                # visible to Claude tools); fall back to `.agnes-bundle.json`
                # (legacy bundles downloaded before the rename).
                _candidate = bundle_path / "agnes-bundle.json"
                if not _candidate.exists():
                    _candidate = bundle_path / ".agnes-bundle.json"
                bundle_json_path = _candidate
                bundle_data = json.loads(bundle_json_path.read_text(encoding="utf-8"))
            elif bundle_path.suffix == ".zip":
                # ZIP file: extract bundle JSON in memory.
                # Supports both flat ZIPs (legacy) and folder-prefixed ZIPs
                # (current format where unzipping creates a workspace folder).
                # Also supports the old `.agnes-bundle.json` name (dot-prefixed)
                # for bundles generated before the visibility rename.
                with zipfile.ZipFile(bundle_path) as zf:
                    names = zf.namelist()
                    bundle_json_name = None
                    # Current format: `agnes-bundle.json` (no dot)
                    for candidate_name in ("agnes-bundle.json", ".agnes-bundle.json"):
                        if candidate_name in names:
                            bundle_json_name = candidate_name
                            break
                    if bundle_json_name is None:
                        # Folder-prefixed: look for <folder>/agnes-bundle.json or .agnes-bundle.json
                        for suffix in ("/agnes-bundle.json", "/.agnes-bundle.json"):
                            candidates = sorted(n for n in names if n.endswith(suffix))
                            if candidates:
                                bundle_json_name = candidates[0]
                                break
                    if bundle_json_name is None:
                        raise ValueError("agnes-bundle.json not found inside the ZIP")
                    bundle_data = json.loads(zf.read(bundle_json_name).decode("utf-8"))
                bundle_json_path = None  # nothing to delete from disk
            else:
                raise ValueError(f"--bundle must be a directory or a .zip file, got: {bundle_path}")
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            typer.echo(
                render_error(
                    0,
                    {
                        "detail": {
                            "kind": "partial_state",
                            "hint": f"Could not read bundle from {bundle!r}: {exc}",
                        }
                    },
                ),
                err=True,
            )
            raise typer.Exit(1)

        bundle_server_url = bundle_data.get("server_url", "").rstrip("/")
        setup_token_raw = bundle_data.get("setup_token", "")

        if not bundle_server_url or not setup_token_raw:
            typer.echo(
                render_error(
                    0,
                    {
                        "detail": {
                            "kind": "partial_state",
                            "hint": "Bundle is missing server_url or setup_token.",
                        }
                    },
                ),
                err=True,
            )
            raise typer.Exit(1)

        if not server_url:
            server_url = bundle_server_url

        # Exchange setup token → PAT (unauthenticated call; setup_token IS auth)
        typer.echo(f"Connecting to {server_url} …")
        try:
            import httpx as _httpx

            exchange_resp = _httpx.post(
                f"{server_url}/api/auth/exchange-setup-token",
                json={"setup_token": setup_token_raw},
                timeout=30,
            )
            # A moved server answers 3xx here too, and `raise_for_status()`
            # does not treat that as an error — the flow fell through to
            # `.json()` on an empty body and reported a JSON decode failure
            # for a server that had simply changed address. Same diagnosis as
            # both HTTP clients use. (Devin Review on #1266.)
            if is_redirect(exchange_resp.status_code):
                # One classifier for every caller (`cli/server_moved.py`) —
                # re-deriving it here is how the two ended up disagreeing on
                # the code name. Only the REMEDY is local: `agnes init` reads
                # neither `AGNES_SERVER` nor `config.yaml`, it takes the
                # address as an argument. (Devin Review on #1266, twice.)
                code, target = classify_redirect(exchange_resp.headers.get("Location", "") or "", server_url)
                detail: dict = {"code": code}
                if code == "server_moved":
                    detail["moved_to"] = target
                    detail["fix"] = f"agnes init --server-url {target} …"
                    detail["hint"] = (
                        f"{server_url} answered HTTP {exchange_resp.status_code} and that address has "
                        "moved. Redirects are not followed automatically — credentials are stripped on a "
                        "cross-origin hop. Re-run setup against the new address with the command above; "
                        "the setup token is unchanged."
                    )
                elif code == "insecure_redirect":
                    detail["blocked_target"] = target
                    detail["hint"] = (
                        f"{server_url} answered HTTP {exchange_resp.status_code} pointing at the "
                        "unencrypted address above. Setup will not send a token there. If the server "
                        "really moved, re-run with an https address; if it did not, a proxy in front of "
                        "it is rewriting the scheme."
                    )
                else:
                    detail["hint"] = (
                        f"{server_url} answered HTTP {exchange_resp.status_code} instead of exchanging "
                        "the setup token, and the redirect stays on the same address. Check whether a "
                        "proxy sits in front of the server."
                    )
                typer.echo(render_error(exchange_resp.status_code, {"detail": detail}), err=True)
                raise typer.Exit(1)
            if exchange_resp.status_code == 401:
                typer.echo(
                    render_error(
                        401,
                        {
                            "detail": {
                                "kind": "auth_failed",
                                "hint": (
                                    "Setup token is invalid, expired, or already used. "
                                    "Download a new bundle from Agnes and try again."
                                ),
                            }
                        },
                    ),
                    err=True,
                )
                raise typer.Exit(1)
            exchange_resp.raise_for_status()
            exchange_data = exchange_resp.json()
        except typer.Exit:
            raise
        except Exception as exc:
            typer.echo(
                render_error(
                    0,
                    {
                        "detail": {
                            "kind": "server_unreachable",
                            "hint": f"Token exchange failed: {exc}",
                        }
                    },
                ),
                err=True,
            )
            raise typer.Exit(1)

        if not token:
            token = exchange_data.get("access_token")
        user_email = exchange_data.get("user_email", "")
        typer.echo(f"Authenticated as {user_email}")

        # Remove bundle file from disk (setup token must not linger)
        if bundle_json_path and bundle_json_path.exists():
            try:
                bundle_json_path.unlink()
            except OSError:
                pass  # best-effort; file will expire server-side anyway

    # ------------------------------------------------------------------
    # Validate that we now have a server URL (required unless --bundle
    # provided it above).
    # ------------------------------------------------------------------
    if not server_url:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "partial_state",
                        "hint": "Supply --server-url or use --bundle to provide it automatically.",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    server_url = server_url.rstrip("/")

    # ------------------------------------------------------------------
    # Resolve the token. Precedence (highest to lowest):
    #   1. explicit --token flag
    #   2. --token-file flag
    #   3. AGNES_TOKEN env var
    #   4. ~/.config/agnes/token.json (saved by `agnes auth login` /
    #      `agnes auth import-token`) — M1 bug fix: was missing before
    #   5. --bundle exchange result (already set above as `token`)
    #   6. → error
    #
    # --token-file and AGNES_TOKEN exist so the PAT never has to travel in
    # argv, where shell history and the process table both expose it. This
    # is the repo-wide "no secrets on the command line" rule, not a special
    # case — see .claude/skills/agnes-conventions/references/security.md.
    # ------------------------------------------------------------------
    if token is None and token_file:
        try:
            # utf-8-sig: Windows PowerShell 5 writes UTF-8 *with BOM* for
            # `-Encoding utf8`; a plain utf-8 read keeps U+FEFF glued to the
            # token (str.strip() does not remove it) and the bearer auth
            # fails. utf-8-sig reads BOM-less files identically.
            for line in Path(token_file).expanduser().read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line:
                    token = line
                    break
        except OSError as exc:
            # ABSENT file is benign: the install prompt always passes
            # --token-file, but on a machine that already signed in the file
            # was consumed by an earlier `agnes init` — the saved credential
            # in ~/.config/agnes/token.json (fallback below) is the normal
            # source then. A file that EXISTS but cannot be read is a real
            # error (permissions, a directory in its place): silently falling
            # back would authenticate with a possibly-expired saved credential
            # and misattribute the failure to the server — exactly the
            # expired-token recovery scenario, where the fresh token IS the
            # file. Hard-fail that case.
            if Path(token_file).expanduser().exists():
                typer.echo(
                    render_error(
                        0,
                        {
                            "detail": {
                                "kind": "partial_state",
                                "hint": f"--token-file {token_file!r} exists but could not be read: {exc}",
                            }
                        },
                    ),
                    err=True,
                )
                raise typer.Exit(1)
            # Deliberately a note, not a hard fail: templates pass
            # --token-file unconditionally and the file legitimately may not
            # exist when AGNES_TOKEN is set. But in the documented
            # expired-credential recovery the ABSENT file means the fresh token
            # was never written — and the fallback then retries with the
            # expired one, so the failure surfaces as a server-side auth error
            # that looks like the server's fault. Name the likely cause here,
            # where we still know it (Devin Review on #1139).
            typer.echo(
                f"note: --token-file {token_file!r} does not exist; "
                "falling back to AGNES_TOKEN / the saved credential. "
                "If you are recovering an expired credential, that saved one is "
                "the expired credential — re-run the 'Get your token' step on "
                "/home so the file is written, then run this again.",
                err=True,
            )
    if token is None:
        token = os.environ.get("AGNES_TOKEN", "").strip() or None
    if token is None:
        # Fallback: PAT saved on a previous `agnes auth login` /
        # `agnes auth import-token` run. Lets `agnes init --server-url X`
        # work without re-supplying the token every time.
        _tok_path = _config_dir() / "token.json"
        if _tok_path.exists():
            try:
                _tok_data = json.loads(_tok_path.read_text(encoding="utf-8"))
                token = _tok_data.get("access_token") or None
            except (OSError, ValueError):
                pass
    if not token:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "partial_state",
                        "hint": (
                            "Supply a token via --token, --token-file, AGNES_TOKEN env var, "
                            "or run `agnes auth login` first."
                        ),
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    # Best-effort cleanup before ANY TLS handshake fires below — stale
    # SSL_CERT_FILE / REQUESTS_CA_BUNDLE / GIT_SSL_CAINFO pointers from a
    # previous Agnes install on this host (or its Windows User-scope
    # registry entries) would otherwise blow up step 2's `api_get` with
    # an opaque "UnknownIssuer" / "FileNotFoundError" before the user
    # has any way to see what's wrong. Reported by the 2026-05-11
    # Windows test pass.
    _cleanup_stale_ca_env_vars()

    # ------------------------------------------------------------------
    # Step 1: detect an existing workspace.
    #
    # An init is considered to have happened when EITHER:
    #   - the completion sentinel `.claude/init-complete` exists
    #     (authoritative, written at the end of every successful init —
    #     default OR override mode), OR
    #   - the legacy "AI Data Analyst" string is in CLAUDE.md (pre-#259
    #     default-mode workspaces that succeeded under an older CLI
    #     version that didn't write a sentinel).
    #
    # The CLAUDE.md substring check is intentionally kept for legacy
    # workspaces but does NOT trigger for Initial-Workspace-override
    # workspaces (admin's repo CLAUDE.md doesn't contain the string).
    # In override mode the sentinel IS the authoritative signal — this
    # is why the override `agnes init` flow writes the sentinel as its
    # very last step, same as the default flow.
    # ------------------------------------------------------------------
    claude_md = workspace / "CLAUDE.md"
    init_complete = workspace / _INIT_COMPLETE_FILE
    sentinel_says_inited = init_complete.exists()
    claude_md_says_inited = False
    if claude_md.exists():
        try:
            existing = claude_md.read_text(encoding="utf-8")
            claude_md_says_inited = _INIT_MARKER in existing
        except (OSError, UnicodeDecodeError):
            # A CLAUDE.md with non-UTF-8 bytes (operator edited with a
            # legacy encoding) shouldn't crash the gate evaluation — fall
            # back to "marker not found" so the sentinel-existence branch
            # below carries the decision instead.
            existing = ""
    if (sentinel_says_inited or claude_md_says_inited) and not force:
        if sentinel_says_inited:
            typer.echo(
                render_error(
                    0,
                    {
                        "detail": {
                            "kind": "partial_state",
                            "hint": (
                                "Workspace already initialized. Run `agnes update` "
                                "to converge the CLI, workspace, plugins and data "
                                "off your saved credential, or re-run with --force "
                                "to redo from scratch."
                            ),
                        }
                    },
                ),
                err=True,
            )
            raise typer.Exit(1)
        # CLAUDE.md substring matches but no sentinel — previous default-
        # mode init was killed mid-flight (issue #259). Resume rather
        # than refuse so a large materialized parquet stays partially
        # cached and we don't re-download from zero.
        typer.echo(
            "Previous init was interrupted (no completion sentinel "
            "found). Resuming — partial downloads will continue where "
            "they stopped.",
            err=True,
        )

    # ------------------------------------------------------------------
    # Step 2: verify the PAT via /api/catalog/tables.
    #
    # `api_get` reads server URL + token from env vars (`AGNES_SERVER`,
    # `AGNES_TOKEN`) via `cli.config`. Wrap the call in
    # `_override_server_env` so the explicit args take effect without
    # mutating the caller's environment permanently. Same mechanism as
    # `cli.lib.pull.run_pull`.
    # ------------------------------------------------------------------
    try:
        with _override_server_env(server_url, token):
            resp = api_get("/api/catalog/tables")
        if resp.status_code == 401:
            typer.echo(
                render_error(
                    401,
                    {
                        "detail": {
                            "kind": "auth_failed",
                            "hint": f"Token expired or invalid — get a fresh one at {server_url}/setup",
                        }
                    },
                ),
                err=True,
            )
            raise typer.Exit(1)
        resp.raise_for_status()
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "server_unreachable",
                        "hint": f"Cannot reach {server_url} — check network or server status",
                        "message": str(exc),
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    # ------------------------------------------------------------------
    # Step 3: save server URL + token to ~/.config/agnes/ so subsequent
    # invocations (including the SessionStart hook) read them by default.
    # `email=""` because the JWT carries it server-side; we don't decode
    # the token on the client.
    # ------------------------------------------------------------------
    save_config({"server": server_url})
    save_token(token, email="")

    # ------------------------------------------------------------------
    # Step 3.1 (v106, --as-admin): swap the stack-surface PAT for a
    # full-surface one. Server-side is the authority: the endpoint 403s
    # unless the caller's PAT owner is an Admin-group member right now.
    # On success the FULL-surface token replaces the saved one, so every
    # subsequent step (pull, hooks) and the workspace itself ride it.
    # ------------------------------------------------------------------
    if as_admin:
        try:
            from cli.client import api_post

            # Same `_override_server_env` wrapping as the Step-2 verify:
            # without it `api_post` would prefer AGNES_SERVER/AGNES_TOKEN
            # env vars over the just-saved config, so in an env-credential
            # context (e.g. a sandbox) the rescope could hit a different
            # server/credential than the exchange above (Devin Review on
            # #1090).
            with _override_server_env(server_url, token):
                resp = api_post("/cli/auth/rescope-surface", json={})
            if resp.status_code == 200:
                new_token = resp.json().get("token") or ""
                if new_token:
                    token = new_token
                    save_token(token, email="")
                    typer.echo("Workspace surface: FULL (--as-admin) — catalog/query see every registered table.")
            elif resp.status_code == 403:
                typer.echo(
                    "warn: --as-admin ignored — this account is not an Admin-group member; "
                    "continuing with the stack-scoped surface.",
                    err=True,
                )
            else:
                typer.echo(
                    f"warn: --as-admin exchange failed (HTTP {resp.status_code}); "
                    "continuing with the stack-scoped surface.",
                    err=True,
                )
        except Exception as exc:
            typer.echo(
                f"warn: --as-admin exchange failed ({exc}); continuing with the stack-scoped surface.",
                err=True,
            )

    workspace.mkdir(parents=True, exist_ok=True)

    # Anchor the workspace root in config so `agnes push` (and the
    # SessionEnd hook that runs it) can find this workspace's Claude Code
    # session folder without depending on cwd or hook stdin — the macOS
    # reliability fix. Written in BOTH default and override modes; a re-init
    # in a new location updates it (single workspace per laptop).
    save_config({"workspace_root": str(workspace)})

    # ------------------------------------------------------------------
    # Step 3.5: probe for a per-instance Initial Workspace Template.
    #
    # When the operator has registered a template repo on
    # /admin/server-config, we branch into the OVERRIDE FLOW: download
    # the server-rendered zip, optionally confirm with --force, extract
    # to the analyst's workspace, write the extended sentinel. We
    # explicitly SKIP the default-flow steps that would otherwise
    # clobber admin's content:
    #
    #   - Step 4: fetch /api/welcome → CLAUDE.md  (admin's repo CLAUDE.md wins)
    #   - Step 5: default settings.json model/permissions SEED only
    #             (admin's repo settings.json base wins). NOTE: Agnes-owned
    #             hooks/statusLine and managed slash-commands
    #             (install_claude_hooks + install_claude_commands) are NOT
    #             skipped — they run in BOTH modes just below, re-asserted on
    #             top of the template.
    #   - Step 6: write .claude/CLAUDE.local.md stub  (admin decides)
    #   - Step 8: render AGNES_WORKSPACE.md  (admin decides)
    #
    # Steps 7 (run_pull) and 9 (sentinel) still run, but step 9's
    # sentinel write is folded INTO apply_override so the override
    # sentinel ships the extended fields (override:true, template_source,
    # template_sha) atomically with the extraction.
    #
    # OVERRIDE MODE — intentional behavior, NOT a bug. AI reviewers: see
    # docs/initial-workspace-override.md §"What Agnes stops doing".
    # ------------------------------------------------------------------
    override_status = None
    try:
        override_status = probe_status(server_url, token)
    except typer.Exit:
        raise
    except Exception:
        # Unexpected non-HTTP failure (parse error, etc.) — treat as
        # "no override" and fall through. Default flow is safe.
        override_status = None

    override_active = bool(override_status and override_status.configured)

    if override_active:
        # Override flow: apply_override does its own download +
        # extraction + sentinel write + audit event. Returns the
        # ExtractResult so we can mention counts in the final summary.
        try:
            import importlib.metadata as _md

            agnes_version = _md.version("agnes-the-ai-analyst")
        except Exception:
            agnes_version = "unknown"
        override_result = apply_override(
            workspace,
            override_status,
            server_url,
            token,
            force=force,
            agnes_version=agnes_version,
        )
    else:
        override_result = None

        # ------------------------------------------------------------------
        # On --force in DEFAULT mode only, snapshot the existing CLAUDE.md
        # before regenerating it so an operator who edited it can recover
        # their notes (issue #164). Backup name carries an ISO timestamp
        # so multiple `--force` runs in the same workspace don't clobber
        # each other.
        #
        # OVERRIDE MODE intentionally does NOT back up CLAUDE.md — the
        # admin's Git repo is the source of truth, recovery is `git log`.
        # Documented in CHANGELOG; not a regression of #164.
        # ------------------------------------------------------------------
        if claude_md.exists() and force:
            try:
                from src.initial_workspace import _prune_backups, _unique_bak_path

                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_path = _unique_bak_path(workspace / f"CLAUDE.md.bak.{ts}")
                backup_path.write_bytes(claude_md.read_bytes())
                # #1476: bound backup growth — keep only the most recent few.
                _prune_backups(claude_md)
                typer.echo(f"Backed up existing CLAUDE.md → {backup_path.name}")
            except OSError as exc:
                typer.echo(
                    f"Warning: could not write CLAUDE.md backup ({exc}); continuing with --force overwrite",
                    err=True,
                )

        # ------------------------------------------------------------------
        # Step 4: fetch the rendered CLAUDE.md from /api/welcome.
        # ------------------------------------------------------------------
        try:
            with _override_server_env(server_url, token):
                welcome_resp = api_get("/api/welcome", params={"server_url": server_url})
            welcome_resp.raise_for_status()
        except Exception as exc:
            typer.echo(
                render_error(
                    0,
                    {
                        "detail": {
                            "kind": "server_unreachable",
                            "hint": "Failed to fetch CLAUDE.md from /api/welcome",
                            "message": str(exc),
                        }
                    },
                ),
                err=True,
            )
            raise typer.Exit(1)
        welcome_content = welcome_resp.json().get("content", "")
        claude_md.write_text(welcome_content, encoding="utf-8")

    if not override_active:
        # ------------------------------------------------------------------
        # Step 5 (DEFAULT mode only): seed first-run model + permissions when
        # settings.json is absent. The Agnes-owned hook/statusLine/command
        # layer is installed for BOTH modes just below — Agnes owns those, not
        # the admin template. OVERRIDE mode skips ONLY this default
        # model/permissions seed: the admin template ships its own
        # settings.json base, onto which Agnes re-asserts its hook entries.
        # ------------------------------------------------------------------
        settings_path = workspace / ".claude" / "settings.json"
        if not settings_path.exists():
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {"model": "sonnet", "permissions": {"allow": ["Read", "Bash", "Bash(agnes *)", "Grep", "Glob"]}},
                    indent=2,
                ),
                encoding="utf-8",
            )

        # ------------------------------------------------------------------
        # Step 6: CLAUDE.local.md stub — only when absent. `--force` does NOT
        # overwrite; the operator's notes survive a re-init.
        #
        # OVERRIDE MODE: NOT created by Agnes. If the admin's template repo
        # ships a CLAUDE.local.md, that one wins; otherwise the file simply
        # doesn't exist. Documented contract: full override = full control.
        # ------------------------------------------------------------------
        local_md = workspace / ".claude" / "CLAUDE.local.md"
        if not local_md.exists():
            local_md.parent.mkdir(parents=True, exist_ok=True)
            local_md.write_text(
                "# My Notes\n\nPersonal notes for this workspace. Uploaded on `agnes push`.\n",
                encoding="utf-8",
            )

    # ------------------------------------------------------------------
    # Agnes-owned settings — BOTH modes: hooks, statusLine, managed slash
    # commands. Agnes owns these regardless of mode. In OVERRIDE mode this
    # runs AFTER apply_override, so Agnes's hook entries sit on TOP of the
    # admin template's settings.json. A template that ships its own hook /
    # statusLine entries is a maintainer mistake — `_replace_or_add` strips
    # the Agnes-marked entries and re-adds the canonical ones; third-party
    # entries and a user statusLine are preserved. `install_claude_commands`
    # only overwrites Agnes-managed command files, never user commands.
    # ------------------------------------------------------------------
    install_claude_hooks(workspace)
    install_claude_commands(workspace)

    # ------------------------------------------------------------------
    # Offer to declare the marketplace host as internal infrastructure for
    # Claude Code's auto-mode classifier, in the USER-scope ~/.claude/
    # settings.json (NOT the workspace settings above — the classifier reads
    # `autoMode` only from user/managed settings). Without the declaration the
    # classifier soft-denies `agnes refresh-marketplace --bootstrap` as
    # "Untrusted Code Integration", which costs one approval prompt rather
    # than breaking anything. The host is derived from the configured server
    # (saved via save_config above), never hardcoded.
    #
    # Opt-in, because this is the only write that leaves the workspace — see
    # `_maybe_declare_marketplace_trust`. Best-effort either way: a failure
    # here must never break init.
    # ------------------------------------------------------------------
    try:
        marketplace_host = configured_marketplace_host()
        if marketplace_host:
            _maybe_declare_marketplace_trust(marketplace_host, trust_marketplace_host, server_url or "")
    except (KeyboardInterrupt, typer.Abort):
        # Ctrl-C at the consent prompt means "stop", not "carry on without
        # declaring". Swallowing it here printed a warning and walked straight
        # into the first sync, which is the long part someone hitting Ctrl-C
        # is usually trying to avoid. (Devin Review on #1262.)
        typer.echo("\nSetup cancelled.")
        raise typer.Exit(code=130)
    except Exception as exc:  # noqa: BLE001 — best-effort, never break init
        typer.echo(f"warn: could not declare auto-mode trust: {exc}", err=True)

    # ------------------------------------------------------------------
    # Always chmod +x hook scripts that landed on disk, regardless of
    # which path seeded the workspace. In DEFAULT mode the hooks come
    # from `install_claude_hooks` above; in OVERRIDE mode they come
    # from the admin's initial-workspace-template clone — and `git
    # checkout` of that template doesn't reliably preserve the +x bit
    # (filemode=false repos, archive extractions, FUSE/NFS mounts),
    # so hooks like `.claude/hooks/skill-nudge/nudge.sh` or
    # `.claude/hooks/prompt-history/log-prompt.sh` could land non-
    # executable and fire `Permission denied` on the very next
    # SessionStart. `_chmod_workspace_hooks` recurses (`rglob`) so
    # subdir-scoped hook layouts are covered. Best-effort, no-op on
    # Windows NTFS.
    # ------------------------------------------------------------------
    _chmod_workspace_hooks(workspace)

    # ------------------------------------------------------------------
    # Step 7: first pull. `run_pull` records per-stage failures inside
    # `result.errors` rather than raising for transient issues, so any
    # exception escaping here is a programming error worth surfacing.
    # ------------------------------------------------------------------
    try:
        # `agnes init` always runs interactively (analyst typing the
        # command), so progress is on by default — Pavel's #185 Phase 1
        # was a 44-minute silent download on the very first install.
        # Pass it through to run_pull.
        result: PullResult = run_pull(
            server_url,
            token,
            workspace,
            skip_materialize=skip_materialize,
            show_progress=True,
        )
    except Exception as exc:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "manifest_unauthorized",
                        "hint": "Initial pull failed — workspace partially set up",
                        "message": str(exc),
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    # `run_pull` records per-stage failures into `result.errors` and only
    # raises for programming errors. A manifest-stage failure here means
    # the analyst has a saved token + saved server URL but no parquets,
    # no DuckDB views — surface a typed error so the operator knows the
    # workspace is not actually queryable. Common cause: PAT validates
    # against /api/catalog/tables but lacks resource_grants for any tables.
    manifest_err = next((e for e in result.errors if e.get("stage") == "manifest"), None)
    if manifest_err:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "manifest_unauthorized",
                        "hint": "Manifest fetch failed — workspace partially set up. "
                        "Check that the PAT has resource_grants for at least one table.",
                        "message": manifest_err.get("error", ""),
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    if not override_active:
        # ------------------------------------------------------------------
        # Step 8: render AGNES_WORKSPACE.md from the static client-side
        # template. Three placeholders: created_at, server_url, workspace_path.
        #
        # OVERRIDE MODE skips — admin's template owns workspace docs (often
        # there's nothing here at all, or the admin ships their own
        # AGNES_WORKSPACE.md content).
        # ------------------------------------------------------------------
        here = Path(__file__).parent
        template_path = here.parent.parent / "config" / "agnes_workspace_template.txt"
        if template_path.exists():
            template = template_path.read_text(encoding="utf-8")
        else:
            # Defensive fallback — the template ships with the repo so this
            # branch only fires on a broken install. Better than crashing.
            template = "# Agnes workspace\n\nCreated: {created_at}\nServer: {server_url}\n"
        workspace_md = (
            template.replace("{created_at}", datetime.now(timezone.utc).isoformat())
            .replace("{server_url}", server_url)
            .replace("{workspace_path}", str(workspace))
        )
        (workspace / "AGNES_WORKSPACE.md").write_text(workspace_md, encoding="utf-8")

    # ------------------------------------------------------------------
    # Step 9: write the completion sentinel. The next `agnes init` (no
    # flags) checks this; absence means a previous attempt was killed
    # mid-flight and we should resume rather than refuse. Issue #259.
    #
    # OVERRIDE MODE already wrote the extended sentinel (with
    # override: true + template_source + template_sha) from inside
    # apply_override(), so skip — don't clobber its extra fields with
    # the basic default-mode shape.
    # ------------------------------------------------------------------
    if override_active:
        pass  # apply_override already wrote the extended sentinel
    else:
        # Default mode: fetch operator-provisioned per-tenant params and
        # write <workspace>/.claude/agnes/.env so seed-resident connector
        # skills can read them at install time. Best-effort; empty overlay
        # or older server (no /api/connectors/params endpoint) silently
        # skips the file.
        try:
            from cli.lib.initial_workspace import write_agnes_env

            write_agnes_env(workspace, server_url, token)
        except Exception as e:
            # Best-effort — failure here doesn't block init. Seed skills
            # will fall back to interactive prompts.
            typer.echo(
                f"  Warning: .env.agnes write skipped ({e})",
                err=True,
            )

        sentinel = workspace / _INIT_COMPLETE_FILE
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        try:
            import importlib.metadata as _md

            agnes_version = _md.version("agnes-the-ai-analyst")
        except Exception:
            agnes_version = "unknown"
        sentinel.write_text(
            f"completed_at: {datetime.now(timezone.utc).isoformat()}\n"
            f"agnes_version: {agnes_version}\n"
            f"server_url: {server_url}\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Step final-1: clear the transient bootstrap token file.
    #
    # The setup prompt writes the raw PAT to `~/.agnes/token` and feeds it
    # to `agnes init --token-file ~/.agnes/token`. That file is a transient
    # *input*, not a credential store — the authoritative copy now lives in
    # `~/.config/agnes/token.json` (written 0o600 by `save_token` above).
    # Leaving the plaintext PAT behind in `~/.agnes/token` is an avoidable
    # exposure (it sits at the default umask and lingers indefinitely), so
    # delete it once init has consumed it. Best-effort: a removal failure
    # must never fail an otherwise-successful init. (#580, Finding 1.)
    # ------------------------------------------------------------------
    _bootstrap_token = Path(os.path.expanduser("~/.agnes/token"))
    try:
        _bootstrap_token.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass

    # NOTE: the bootstrap session is deliberately NOT auto-marked private.
    # An earlier revision did that (#753/#771) to keep the PAT pasted by the
    # setup-prompt heredoc out of the server's transcript store, but the
    # push-time JWT redaction (`cli/lib/transcript_redact.py`, the other half
    # of #771) already guarantees the raw token never leaves the client.
    # Uploading the setup transcript is the product's designed behavior, and
    # marking a session private is exclusively the analyst's own deliberate
    # action (`/agnes-private`) — never something the tooling does for them.

    # ------------------------------------------------------------------
    # Install the one-word launcher script into ~/.local/bin (also cleans
    # up legacy rc-function blocks). Runs in BOTH default and override
    # modes — the workspace launcher (bin/<word>) is seeded by the IWT in
    # override mode; default mode falls back to
    # `claude --permission-mode auto`. Best-effort; never aborts init.
    # ------------------------------------------------------------------
    install_launcher_shortcut(workspace, no_shortcut=no_shortcut)

    # ------------------------------------------------------------------
    # Final: human-readable summary.
    # ------------------------------------------------------------------
    typer.echo("Workspace ready.")
    typer.echo(f"  Server   : {server_url}")
    if override_active and override_result is not None:
        typer.echo(
            f"  Template : {override_status.template_source} "
            f"@ {override_status.template_sha[:10] if override_status.template_sha else '—'}"
        )
        typer.echo(
            f"  Files    : {len(override_result.created)} created, "
            f"{len(override_result.overwritten)} overwritten from template"
        )
    # Two different numbers, and reading one as the other is how this line
    # went wrong before: `parquets_total` counts the non-remote tables this run
    # CONSIDERED (the skip branch in `cli/lib/pull.py` `continue`s before that
    # counter), while `materialized_skipped` counts the rows left alone.
    # Reporting `parquets_total` as "skipped" told an analyst with three
    # ordinary tables and no materialized ones that three rows were skipped,
    # and gating the note on it hid the note entirely on the instance the note
    # exists for — everything materialized, so the count is zero and the fall-
    # through printed the bare "0/0" that issue #257 set out to prevent.
    #
    # The fetched count is stated honestly rather than hardcoded to zero: with
    # `--skip-materialize` an instance can still have ordinary tables to pull.
    fetched = f"{result.tables_updated}/{result.parquets_total} local table(s) fetched"
    if skip_materialize and result.materialized_skipped > 0:
        typer.echo(
            f"  Tables   : {fetched} — {result.materialized_skipped} "
            f"materialized row(s) skipped by default. Fetch them on demand "
            f"with `agnes pull`, or re-run `agnes init --materialize` for "
            f"the full first pull. Catalog still serves all registered tables."
        )
    else:
        typer.echo(f"  Tables   : {fetched}")
    typer.echo(f"  Rules    : {result.rules_count}")
    typer.echo(f"  Workspace: {workspace}")
    typer.echo("")
    typer.echo("Try: agnes catalog")
