"""`agnes pull` — refresh registered data into the workspace.

Thin Typer wrapper around `cli/lib/pull.py:run_pull`. Used by:
- Manual invocation: analyst types `agnes pull` to force a refresh.
- SessionStart hook: `agnes pull --quiet 2>/dev/null || true` runs at the start
  of every Claude Code session in this workspace.

Errors render via `cli/error_render.py:render_error()` for typed-error
shape consistency with other CLI commands. The wrapper intentionally does
no I/O of its own — config lookup, manifest fetch, parquet download, view
rebuild, and rules-bundle write all live in `run_pull`. This keeps the
command code trivially testable and the data-refresh primitive reusable
from other entrypoints (init, analyst setup, future MCP tools).

Task 18 will register `pull_app` on the root Typer app and delete the
legacy `agnes sync` command. Until then this module is callable only via
direct import (which is exactly what the test does).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from cli.config import get_server_url, get_token
from cli.error_render import render_error
from cli.lib.pull import PullResult, run_pull


pull_app = typer.Typer(help="Refresh registered data from the server")


@pull_app.callback(invoke_without_command=True)
def pull(
    quiet: bool = typer.Option(False, "--quiet", help="Suppress success stdout (errors still surface on stderr)"),
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON object summarizing the pull"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute the delta without writing anything to disk"),
    skip_materialize: bool = typer.Option(
        False,
        "--skip-materialize",
        help=(
            "Skip materialized-mode tables (server-side scheduled BQ "
            "scan results, often multi-GB). Their data is still discoverable "
            "via `agnes catalog` and remote-mode tables still pull. Useful "
            "for a fast first init when an analyst only needs --remote access."
        ),
    ),
    workspace_str: Optional[str] = typer.Option(
        None,
        "--workspace",
        help=(
            "Target workspace dir (default: AGNES_LOCAL_DIR, else the current "
            "dir if it is a workspace, else the anchored workspace_root)."
        ),
    ),
):
    """Refresh data from the server into the workspace's server/parquet +
    user/duckdb (resolved via AGNES_LOCAL_DIR → shaped cwd → anchored
    workspace_root; override with --workspace)."""
    server_url = get_server_url()
    if not server_url:
        # `get_server_url()` falls back to a localhost default today, so this
        # branch is mostly a defensive guard — if a future config change ever
        # returns an empty string we still want a friendly hint, not a crash
        # halfway through the manifest fetch.
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "server_unreachable",
                        "hint": "No server configured. Run: agnes init --server-url <URL> --token <PAT>",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    token = get_token()
    if not token:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "auth_failed",
                        "hint": "No token. Run: agnes auth import-token --token <PAT>",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    explicit_workspace = bool(workspace_str)
    if workspace_str:
        workspace = Path(workspace_str).resolve()
    else:
        from cli.lib.workspace_resolve import resolve_data_workspace

        resolved = resolve_data_workspace()
        if resolved is None:
            typer.echo(
                render_error(
                    0,
                    {
                        "detail": {
                            "kind": "partial_state",
                            "hint": (
                                "No workspace found — run `agnes init` first, or pass "
                                "--workspace <dir> / set AGNES_LOCAL_DIR. Refusing to "
                                "download data into an arbitrary directory."
                            ),
                        }
                    },
                ),
                err=True,
            )
            raise typer.Exit(1)
        workspace = resolved

    # #1312 (remaining scope, item 1): `agnes pull` names the workspace it
    # resolved (below), but until now never said whether that differs from
    # the anchored `workspace_root` — the directory `agnes update` and the
    # SessionStart/SessionEnd hooks converge. A pull that silently lands in
    # the "wrong" (cwd) workspace looked identical to a correct one. An
    # explicit `--workspace` is a deliberate override, not the silent
    # divergence this is about, so it never gets the note.
    from cli.lib.workspace_resolve import workspace_anchor

    anchor = workspace_anchor()
    if anchor is not None and not explicit_workspace and anchor != workspace and not (quiet or as_json):
        typer.echo(
            f"note: this differs from your configured workspace anchor ({anchor}) "
            "— `agnes update` and the Claude Code hooks target that directory instead. "
            "Run from there, or pass --workspace to make this pull's target explicit.",
            err=True,
        )

    # Legacy-hook nudge (#478): workspaces bootstrapped by the OLD server
    # flow (a `collect_session` / `server/scripts/` SessionEnd hook, no
    # `agnes init` hooks) never invoke `agnes self-upgrade`, so their CLI
    # drifts stale forever. Emit ONE stderr line pointing the analyst at
    # `agnes init`. We do NOT auto-migrate — the analyst owns when their
    # hook layout changes. Suppressed under --quiet (the SessionStart hook
    # path, kept silent) and --json (machine-readable output). Best-effort:
    # a malformed settings.json must never abort the pull.
    if not (quiet or as_json):
        try:
            from cli.lib.hooks import workspace_has_legacy_hooks

            if workspace_has_legacy_hooks(workspace):
                typer.echo(
                    "This workspace uses an outdated hook layout — run `agnes init` to enable auto-update.",
                    err=True,
                )
        except Exception:
            pass

    # Lazy TTL sweep (#407): drop any `--ttl` snapshots whose expiry has
    # elapsed before refreshing. Best-effort and fully wrapped — a sweep
    # failure (locking quirk, permissions) must NEVER block a pull, which is
    # the load-bearing SessionStart hook. Skip under --dry-run (no disk
    # writes anywhere) and --json (machine-readable output stays clean).
    if not dry_run:
        try:
            from cli.snapshot_meta import sweep_expired_snapshots

            swept = sweep_expired_snapshots(workspace / "user" / "snapshots")
            if swept and not (quiet or as_json):
                for name in swept:
                    typer.echo(f"swept expired snapshot: {name}", err=True)
        except Exception:
            # Intentionally swallowed — see the comment above.
            pass

    # Show progress unless quiet (SessionStart hooks) or json (machine-
    # readable output where Rich's terminal-control sequences would be
    # garbage in the consumer's parser).
    show_progress = not (quiet or as_json)
    try:
        result: PullResult = run_pull(
            server_url,
            token,
            workspace,
            dry_run=dry_run,
            skip_materialize=skip_materialize,
            show_progress=show_progress,
        )
    except Exception as exc:
        # `run_pull` is documented to record per-table / per-stage failures
        # under `result.errors` rather than raising, so reaching this branch
        # means something genuinely unexpected blew up (e.g. a programming
        # error in a helper). Render it through the same typed-error pipe so
        # the operator gets a consistent shape, then exit non-zero.
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "manifest_unauthorized",
                        "hint": f"Pull failed: {exc}",
                        "message": str(exc),
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    # Fold the stack-sync per-item failures into `result.errors` before ANY
    # branch reads it. Printing them as a warn line on the human path was not
    # enough: `result.errors` is what decides the exit code (#596), what the
    # `--json` payload carries, and what the `--quiet` SessionStart path
    # reports — so a data package, skill or memory domain that never landed
    # left a scripted or hook-driven pull exiting 0 with an empty `errors`
    # array. Same reasoning #596 already applied to a table that failed to
    # land. (Devin Review on this PR.)
    _fold_stack_sync_errors(result)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    # #1312 — which workspace this pull actually targeted
                    # (resolved above from --workspace / AGNES_LOCAL_DIR /
                    # cwd / the anchored workspace_root), so a scripted
                    # caller doesn't have to re-derive it.
                    "workspace": str(workspace),
                    # #1312 — the configured `workspace_root` anchor (or
                    # null when unset), so a scripted caller can detect the
                    # same divergence the human-readable note above warns
                    # about without re-reading config.yaml itself.
                    "workspace_root": str(anchor) if anchor is not None else None,
                    "tables_updated": result.tables_updated,
                    "tables_removed": result.tables_removed,
                    "parquets_total": result.parquets_total,
                    "rules_count": result.rules_count,
                    "snapshot_views_blocked": list(getattr(result, "snapshot_views_blocked", []) or []),
                    # WF-4 (wave 2H) — provenance counters. `getattr` with a
                    # 0 default keeps this endpoint tolerant of duck-typed
                    # `PullResult` stand-ins in tests that predate these
                    # fields (`_FakePullResult` et al. in test_cli_pull.py).
                    "tables_via_signed_url": getattr(result, "tables_via_signed_url", 0),
                    "tables_via_app": getattr(result, "tables_via_app", 0),
                    "duration_s": round(result.duration_s, 3),
                    "errors": result.errors,
                }
            )
        )
        # #596 — a per-table / per-stage failure must surface as a non-zero
        # exit even on the machine-readable path. Emit the JSON dict first
        # (the consumer parses `errors`), THEN exit 1 so a wrapping script's
        # `set -e` / `||` reacts to the failure.
        if result.errors:
            raise typer.Exit(1)
        return

    # Printed before the early returns so `--quiet` callers get it too. NOTE
    # this is not the automatic path: the canonical SessionStart hook runs
    # `agnes update --quiet` detached with stdout AND stderr to /dev/null, so
    # nothing printed here reaches anyone on that path. `agnes update` carries
    # the same names in its run report instead, which persists to
    # `.claude/agnes/update.log` (#1129 review corrected an earlier comment
    # here that claimed the hook forwards stderr — it does not).
    withheld = list(getattr(result, "snapshot_views_blocked", []) or [])
    if withheld:
        shown = ", ".join(sorted(withheld)[:5])
        more = f" (+{len(withheld) - 5} more)" if len(withheld) > 5 else ""
        typer.echo(
            f"Withheld {len(withheld)} snapshot name(s) that now belong to a table you can no\n"
            f"longer read locally: {shown}{more}.\n"
            "Re-create them under a different name (`agnes snapshot create <table> --as <name>`).",
            err=True,
        )

    if quiet:
        # Quiet mode is for the SessionStart hook — silent on success so
        # Claude Code's stdout stays clean. Errors still flow to stderr so
        # the user sees them in their terminal even when the hook redirects
        # `2>/dev/null` (the hook explicitly forwards stderr too in the
        # canonical `agnes init` template).
        if result.errors:
            for e in result.errors:
                typer.echo(f"warn: {format_pull_error(e)}", err=True)
            # #596 — even in the silent SessionStart-hook path, a table that
            # failed to land must exit non-zero so the canonical hook's
            # trailing `|| true` is what swallows it (a deliberate operator
            # choice), not a hidden exit 0 that hides data loss.
            raise typer.Exit(1)
        return

    # #1312 — name which workspace this pull actually refreshed. Resolution
    # falls through several layers (--workspace / AGNES_LOCAL_DIR / cwd / the
    # anchored workspace_root — see `cli/lib/workspace_resolve.py`), so a
    # silent success gives no confirmation the analyst is looking at, e.g.,
    # a different repo's workspace. Gated on `quiet` like the rest of this
    # block (the --json payload above carries the same value instead).
    typer.echo(f"Workspace: {workspace}")

    # Surface tables_removed alongside tables_updated so an operator who
    # dropped a data package from their stack sees the prune count in the
    # primary summary line — not just buried in the per-type status block
    # below. Pruning is a security-relevant op (revokes local query access);
    # silent removals were the Devin Review finding on #594.
    if result.tables_removed:
        typer.echo(
            f"Updated {result.tables_updated} tables, removed {result.tables_removed} ({result.parquets_total} total)."
        )
    else:
        typer.echo(f"Updated {result.tables_updated} tables ({result.parquets_total} total).")
    typer.echo(f"Rules: {result.rules_count}.")

    # WF-4 (wave 2H) — provenance summary. Only printed once a
    # `signed_url` has actually been used (an instance with no object
    # store configured, or `distribution.signed_urls: off`, never sees
    # this line — no noise for the common case). `getattr` keeps this
    # tolerant of duck-typed `PullResult` stand-ins in tests.
    via_signed_url = getattr(result, "tables_via_signed_url", 0)
    if via_signed_url:
        via_app = getattr(result, "tables_via_app", 0)
        typer.echo(f"  {via_signed_url} via signed URL, {via_app} via app path.")

    # #754 — an empty manifest with zero errors is ambiguous: it means
    # either "nothing is registered on the server yet" or "your stack has
    # no data packages" — NOT a transport/server failure (those already
    # land in `result.errors` via the `except` in `run_pull`'s
    # manifest-fetch step and are reported below). Since #356 only data
    # packages in the user's stack surface tables (per-table
    # resource_grants no longer manifest), so the actionable next step is
    # browsing/subscribing packages, not asking for a table grant.
    if result.parquets_total == 0 and not result.errors:
        typer.echo(
            "No tables available to pull — either nothing is registered on "
            "the server yet, or your stack has no data packages. Browse "
            "the packages available to you with `agnes stack browse` and "
            "subscribe with `agnes stack add data_package <id>`; if none "
            "are listed, ask your admin to publish a data package and "
            "grant your group access to it."
        )

    # v49 (Task 8.12): per-type status block surfaced from `SyncReport`.
    # The new per-type sync loop in ``cli/lib/pull_sync.py`` reports
    # added/updated/removed counts for direct_tables, data_packages, and
    # memory_domains; rendering them here lets the operator see at a
    # glance what changed without trawling debug logs. Skipped when the
    # manifest predates v49 (no `stack_sync` on PullResult) so older
    # servers still produce the legacy two-line output.
    stack = getattr(result, "stack_sync", None)
    if stack is not None:
        _emit_stack_sync_block(stack)

    if result.errors:
        for e in result.errors:
            typer.echo(f"warn: {format_pull_error(e)}", err=True)
        # #596 — a partial pull (any table failed to land) must exit non-zero
        # so manual invocation and CI both see the failure instead of a
        # success-looking exit 0 that silently hides missing tables.
        raise typer.Exit(1)


_ERROR_SUBJECT_KEYS = ("stack", "table", "name", "package", "slug", "digest", "corpus_id", "stage")


def format_pull_error(entry) -> str:
    """One readable line for an entry of ``PullResult.errors``.

    The entries are dicts assembled at the failure site
    (``{"table": ..., "error": ...}`` and friends). They used to be printed
    with an f-string, so an analyst whose download 403'd got the repr:

        warn: {'table': 'orders', 'error': "Client error '403 Forbidden' ..."}

    Anything unrecognized falls back to ``str`` so this never renders less
    than before.
    """
    if not isinstance(entry, dict):
        return str(entry)
    subject = " ".join(str(entry[k]) for k in _ERROR_SUBJECT_KEYS if entry.get(k) not in (None, ""))
    message = str(entry.get("error") or "").strip()
    if subject and message:
        return f"{subject}: {message}"
    return message or subject or str(entry)


def _fold_stack_sync_errors(result) -> None:
    """Move each ``TypeReport.errors`` entry onto ``result.errors``.

    Tagged with the type it came from so the shared formatter names it. The
    entries live on the per-type reports (``cli/lib/pull_sync.py``), a
    different list from the one every exit path reads.
    """
    stack = getattr(result, "stack_sync", None)
    if stack is None or not hasattr(result, "errors"):
        return
    for label in ("direct_tables", "data_packages", "memory_domains"):
        rep = getattr(stack, label, None)
        for entry in getattr(rep, "errors", []) or []:
            if isinstance(entry, dict):
                result.errors.append({"stack": label, **entry})
            else:
                result.errors.append({"stack": label, "error": str(entry)})


def _emit_stack_sync_block(stack) -> None:
    """Print the v49 per-type ``SyncReport`` summary.

    Format mirrors the rest of `agnes pull`'s output: plain text, one
    line per type. Lines are emitted only when something changed for
    that type — a clean idempotent pull stays as quiet as before
    (just the legacy "Updated 0 tables …" header).

    Layout::

        Stack sync:
          marketplace_plugins: ✓ 0 changes
          data_packages:       2 added, 1 updated, 0 removed
          memory_domains:      ✓ 0 changes
          direct_tables:       ✓ 0 changes

    Invariant violations (if any) surface as a trailing warning so a
    drifted disk state isn't silently swept under the rug.
    """

    # Tolerate either dataclass shape (real ``SyncReport``) or test
    # doubles supplying a duck-typed object with .direct_tables etc.
    def _line(label: str, rep) -> str:
        added = getattr(rep, "added", 0)
        updated = getattr(rep, "updated", 0)
        removed = getattr(rep, "removed", 0)
        if not (added or updated or removed):
            return f"  {label:<22} ✓ 0 changes"
        parts = []
        if added:
            parts.append(f"{added} added")
        if updated:
            parts.append(f"{updated} updated")
        if removed:
            parts.append(f"{removed} removed")
        return f"  {label:<22} {', '.join(parts)}"

    direct = getattr(stack, "direct_tables", None)
    pkgs = getattr(stack, "data_packages", None)
    mem = getattr(stack, "memory_domains", None)
    if direct is None and pkgs is None and mem is None:
        return

    typer.echo("Stack sync:")
    if direct is not None:
        typer.echo(_line("direct_tables:", direct))
    if pkgs is not None:
        typer.echo(_line("data_packages:", pkgs))
    if mem is not None:
        typer.echo(_line("memory_domains:", mem))

    # Per-item failures are NOT printed here: `_fold_stack_sync_errors` moves
    # them onto `result.errors` before any exit path reads it, so they print
    # through the same formatter as every other pull error AND count towards
    # the exit code and the `--json` payload. Printing them here as well would
    # simply double them. (Devin Review on this PR — first as "these reach no
    # surface at all", then as "a warn line is not an exit code".)

    violations = getattr(stack, "invariant_violations", []) or []
    if violations:
        typer.echo(
            f"warn: {len(violations)} stack invariant violation{'s' if len(violations) != 1 else ''} — see logs.",
            err=True,
        )
