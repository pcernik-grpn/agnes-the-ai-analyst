"""`agnes status` — workspace status: initialized? data fresh? hooks active?

Server-health checks live under `agnes diagnose system` (see the
`agnes diagnose` group).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer

# Mirrors the dual-marker convention documented in cli/commands/init.py:
# `.claude/init-complete` is the authoritative sentinel written by every
# successful init (default OR Initial-Workspace-override mode); the legacy
# CLAUDE.md substring is kept as a fallback for pre-#259 workspaces. The
# sentinel-first ordering matters for override workspaces AND all
# post-rebrand default workspaces: neither contains the literal "AI Data
# Analyst" substring (the marker is hardcoded against the pre-rebrand
# default template's `# {{ instance.name }} — AI Data Analyst` heading),
# and the legacy grep alone would then falsely report "Initialized: no"
# even when init wrote the sentinel and the workspace is functional.
_INIT_SENTINEL = Path(".claude") / "init-complete"
_INIT_MARKER = "AI Data Analyst"


status_app = typer.Typer(help="Show workspace status (initialized? data fresh? hooks active?)")


@status_app.callback(invoke_without_command=True)
def status(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    from cli.lib.workspace_resolve import resolve_data_workspace

    workspace = resolve_data_workspace() or Path.cwd().resolve()

    initialized = (workspace / _INIT_SENTINEL).exists()
    if not initialized:
        claude_md = workspace / "CLAUDE.md"
        if claude_md.exists():
            try:
                initialized = _INIT_MARKER in claude_md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                initialized = False

    from cli.lib.local_tables import count_local_tables

    table_count, unregistered_count = count_local_tables(workspace)

    db_path = workspace / "user" / "duckdb" / "analytics.duckdb"
    last_synced = None
    if db_path.exists():
        last_synced = datetime.fromtimestamp(db_path.stat().st_mtime, tz=UTC).isoformat()

    # Sessions live in <projects_root>/<encoded-workspace_root>/ where Claude
    # Code writes them. Count what `agnes push` would scan — anchored on the
    # `workspace_root` config key (the same anchor push uses), so a status run
    # from any cwd reports the real workspace. 0 when unset.
    from cli.config import get_workspace_root
    from cli.lib.session_paths import list_session_files
    from cli.lib.workspace_resolve import workspace_anchor

    ws_root = get_workspace_root()
    session_count = len(list_session_files(Path(ws_root))) if ws_root else 0

    # #1312 (remaining scope, item 2): every OTHER field above is anchored on
    # `workspace` (`resolve_data_workspace()`, cwd-first), but the upload
    # count is anchored on `workspace_root` (push's anchor) — deliberately
    # different resolvers (see `cli/lib/workspace_resolve.py`). Mixing them
    # with no label meant an analyst standing in a foreign-but-shaped
    # directory saw an upload count for a workspace never named on screen.
    # Label it whenever the two genuinely differ; say nothing extra when
    # they agree (the common case) so the line stays as terse as before.
    anchor = workspace_anchor()
    anchor_differs = anchor is not None and anchor != workspace

    info = {
        "workspace": str(workspace),
        "initialized": initialized,
        "parquet_tables": table_count,
        "tables_downloaded_no_local_view": unregistered_count,
        "duckdb_exists": db_path.exists(),
        "last_synced": last_synced,
        "sessions_pending_upload": session_count,
        "session_anchor": str(anchor) if anchor is not None else None,
        "session_anchor_differs_from_workspace": anchor_differs,
    }

    if as_json:
        typer.echo(json.dumps(info, indent=2))
        return

    typer.echo(f"Workspace : {workspace}")
    typer.echo(f"Initialized: {'yes' if initialized else 'no'}")
    if unregistered_count:
        # Two numbers, deliberately not summed: `queryable` is what
        # `agnes query --local` can resolve, while the second counts
        # parquets the stack sync (step 8 of `agnes pull`) put into
        # `.claude/data/_shared/` that no DuckDB view covers. Summing them
        # would promise local data that is not reachable; omitting the
        # second would hide real bytes on disk.
        typer.echo(f"Tables    : {table_count} queryable, {unregistered_count} downloaded (no local view)")
    else:
        typer.echo(f"Tables    : {table_count}")
    typer.echo(f"DuckDB    : {'yes' if info['duckdb_exists'] else 'no'}")
    typer.echo(f"Last sync : {last_synced or 'never'}")
    if anchor_differs:
        typer.echo(f"Pending uploads: {session_count} sessions (anchor: {anchor} — differs from Workspace above)")
    else:
        typer.echo(f"Pending uploads: {session_count} sessions")

    if not initialized:
        typer.echo("")
        # Gate on EITHER count. Data delivered by the stack sync lands only in
        # `unregistered_count`, so gating on `table_count` alone told a
        # workspace whose data all sits in that store to "bootstrap" one line
        # after reporting dozens of downloaded tables — reintroducing the exact
        # contradiction this change exists to remove. Reachable in practice:
        # `_rebuild_duckdb_views` creates `analytics.duckdb` on every pull, so a
        # directory pulled into is workspace-shaped and gets resolved here even
        # though `agnes init` never ran in it.
        if table_count or unregistered_count:
            # A workspace can hold data while carrying no init sentinel, and
            # reporting a bare "no" next to a populated `Tables` line reads as
            # a contradiction. The two ask different questions: `agnes pull`
            # only needs the directory to be workspace-*shaped*
            # (`is_workspace_shaped` in cli/lib/workspace_resolve.py accepts a
            # bare `server/parquet/`), whereas "initialized" means `agnes init`
            # ran HERE and installed the hooks + template. Name the half
            # that is actually missing instead of implying the data is not there.
            # The combined figure, not `table_count`: the point of the line is
            # "data is present", and a workspace whose tables are all in the
            # stack-sync store would otherwise announce "holds data (0 tables)".
            typer.echo(
                f"This workspace holds data ({table_count + unregistered_count} tables) but "
                "`agnes init` never ran here — no Claude Code hooks, no workspace template."
            )
            typer.echo("Run `agnes init --server-url <URL> --token <PAT>` to finish setting it up.")
        else:
            typer.echo("Run `agnes init --server-url <URL> --token <PAT>` to bootstrap.")
