"""Query commands — agnes query."""

import hashlib
import json
import re
import sys
import time
from typing import Optional

import typer

from cli.query_hints import missing_table, remote_table_hint

# Default TTL for auto-snapshots created by the --auto-snapshot fallback
# (#616). Reused if still fresh, rebuilt once elapsed.
_AUTO_SNAPSHOT_TTL = "24h"

_VALID_SCOPES = ("auto", "local", "server")


class _LocalDbMissing(Exception):
    """Raised by `_run_local` when there's no local DuckDB file yet."""


class _LocalTableMiss(Exception):
    """Raised by `_run_local` when the query failed on an unresolvable
    table name that might be a `query_mode='remote'` or `server_only`
    table (i.e. DuckDB's "Table with name X does not exist")."""

    def __init__(self, table: str, original: Exception):
        super().__init__(str(original))
        self.table = table
        self.original = original


def query_command(
    sql: Optional[str] = typer.Argument(None, help="SQL query to execute (positional)"),
    sql_opt: Optional[str] = typer.Option(None, "--sql", help="SQL query to execute (named option)"),
    remote: bool = typer.Option(False, "--remote", help="Shorthand for --scope server"),
    local_flag: bool = typer.Option(False, "--local", help="Shorthand for --scope local"),
    scope: Optional[str] = typer.Option(
        None,
        "--scope",
        help="Where to run: auto (local first, fall back to server), local, server [default: auto]",
    ),
    fmt: str = typer.Option("table", "--format", "-f", help="Output format: table, json, csv"),
    json_flag: bool = typer.Option(False, "--json", help="Shortcut for --format json"),
    limit: int = typer.Option(1000, "--limit", help="Max rows to return"),
    stdin: bool = typer.Option(False, "--stdin", help='Read SQL from stdin as JSON {"sql": "..."}'),
    auto_snapshot: bool = typer.Option(
        False,
        "--auto-snapshot",
        help=(
            "On a remote VIEW query that trips the BigQuery scan cap, "
            "auto-materialize the view as a local snapshot and re-run the "
            "query against it (reuses a fresh snapshot within 24h). "
            "Requires --remote/--scope server; no-op for physical-table queries."
        ),
    ),
):
    """Execute SQL query against DuckDB."""
    # `--json` is an alias for `--format json` (issue #345 D). Paste-prompts
    # and LLM-assisted analysis routinely reach for `--json`; the typer
    # "Did you mean --stdin?" suggestion that the absence of this flag
    # produced was actively misleading.
    if json_flag:
        if fmt != "table" and fmt != "json":
            typer.echo(
                f"Error: --json and --format={fmt} are mutually exclusive.",
                err=True,
            )
            raise typer.Exit(1)
        fmt = "json"

    if scope is not None and scope not in _VALID_SCOPES:
        typer.echo(
            f"Error: --scope must be one of {', '.join(_VALID_SCOPES)} (got {scope!r}).",
            err=True,
        )
        raise typer.Exit(1)

    # `None` means --scope was not given (defaults to auto). The sentinel —
    # rather than click's get_parameter_source, whose behavior shifted across
    # click releases — distinguishes an explicit `--scope local`/`--scope
    # server` from the default so real conflicts with --remote/--local are
    # rejected without rejecting the (harmless) default.
    scope_explicit = scope is not None
    scope = scope or "auto"

    if remote and local_flag:
        typer.echo("Error: --remote and --local are mutually exclusive.", err=True)
        raise typer.Exit(1)
    if remote and scope_explicit and scope == "local":
        typer.echo("Error: --remote and --scope local are mutually exclusive.", err=True)
        raise typer.Exit(1)
    if local_flag and scope_explicit and scope == "server":
        typer.echo("Error: --local and --scope server are mutually exclusive.", err=True)
        raise typer.Exit(1)

    if remote:
        effective_scope = "server"
    elif local_flag:
        effective_scope = "local"
    else:
        effective_scope = scope

    # Resolve SQL from exactly one of: positional, --sql, or --stdin
    sources_provided = sum(
        [
            sql is not None,
            sql_opt is not None,
            stdin,
        ]
    )
    if sources_provided == 0:
        typer.echo("Error: provide SQL as a positional argument, --sql option, or --stdin flag.", err=True)
        raise typer.Exit(1)
    if sources_provided > 1:
        typer.echo("Error: only one of positional SQL, --sql, or --stdin may be used at a time.", err=True)
        raise typer.Exit(1)

    if stdin:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
            resolved_sql = payload["sql"]
        except (json.JSONDecodeError, KeyError) as exc:
            typer.echo(f"Error: failed to parse stdin JSON: {exc}", err=True)
            raise typer.Exit(1)
    elif sql_opt is not None:
        resolved_sql = sql_opt
    else:
        resolved_sql = sql

    if effective_scope == "server":
        _query_remote(resolved_sql, fmt, limit, auto_snapshot=auto_snapshot)
    elif effective_scope == "local":
        _query_local(resolved_sql, fmt, limit)
    else:
        _query_auto(resolved_sql, fmt, limit, auto_snapshot=auto_snapshot)


_TABLE_REF_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.\"]*)", re.IGNORECASE)


def _tables_in_sql(sql: str) -> list[str]:
    """Best-effort list of table/view identifiers referenced by `sql`, for
    audit metadata only (`query.local_offline` params) — NEVER the SQL text
    itself. Regex-based, not a full parser: approximate names beat none for
    an offline audit trail."""
    seen: list[str] = []
    for m in _TABLE_REF_RE.finditer(sql):
        name = m.group(1).strip('"').split(".")[-1]
        if name and name not in seen:
            seen.append(name)
    return seen


def _run_local(sql: str, fmt: str, limit: int):
    """Execute `sql` against the local DuckDB.

    Raises `_LocalDbMissing` if there's no local DB yet, `_LocalTableMiss`
    if the failure looks like an unresolvable table name (possibly a
    `query_mode='remote'` or `server_only` table), or re-raises any other
    exception unchanged. Callers decide how to present each case.

    Every attempt that actually reaches DuckDB (success or failure) records
    one `query.local_offline` client-reported audit event to the local spool
    (`cli.lib.audit_spool`) — this command never talks to the server, so
    without this the whole offline-query surface would be invisible to
    `audit_log` (F3 — audit-full-coverage plan, Task 9). Never the SQL text,
    only its hash.
    """
    from src.duckdb_conn import _open_duckdb

    from cli.lib.audit_spool import record_local_event
    from cli.lib.workspace_resolve import resolve_data_workspace
    from src.audit_helpers import hash_args

    local_dir = resolve_data_workspace()
    if local_dir is None:
        raise _LocalDbMissing()
    db_path = local_dir / "user" / "duckdb" / "analytics.duckdb"
    if not db_path.exists():
        raise _LocalDbMissing()

    conn = _open_duckdb(str(db_path), read_only=True)
    t0 = time.perf_counter()
    try:
        result = conn.execute(sql).fetchmany(limit)
        columns = [desc[0] for desc in conn.description] if conn.description else []
        record_local_event(
            "query.local_offline",
            {
                "tables": _tables_in_sql(sql),
                "sql_hash": hash_args(sql),
                "rows": len(result),
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            },
        )
        _output(columns, result, fmt)
    except Exception as e:
        record_local_event(
            "query.local_offline",
            {
                "tables": _tables_in_sql(sql),
                "sql_hash": hash_args(sql),
                "rows": 0,
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            },
        )
        # DuckDB's "Did you mean <similar materialized view>" suggestion is
        # misleading when the unresolvable identifier is actually a
        # `query_mode='remote'` table OR a `server_only` table — neither has
        # a local view by design (#607: server_only rows are kept server-side
        # and never distributed to the laptop by `agnes pull`). We don't
        # verify against the remote registry here (this command is
        # offline-friendly), so callers treat the match as conditional
        # ("might be") — safe even when the name was just a typo.
        table = missing_table(str(e))
        if table:
            raise _LocalTableMiss(table, e) from e
        raise
    finally:
        conn.close()


def _query_local(sql: str, fmt: str, limit: int):
    """Run query against local DuckDB (`--scope local` behavior): prints
    today's guidance messages on failure, with no server-side fallback."""
    try:
        _run_local(sql, fmt, limit)
    except _LocalDbMissing:
        # No local data yet. Lead with `--remote` (runs server-side, no
        # download) — the right path in a constrained sandbox where a full
        # `agnes pull` would drag down every granted table. `agnes pull`
        # stays the offline-friendly option for laptop analysts.
        typer.echo(
            "No local DuckDB yet (nothing pulled). Two ways to run this query:\n"
            '  - Server-side, no download (recommended):  agnes query --remote "<SQL>"\n'
            "  - Or sync data locally first:               agnes pull   "
            "(downloads every table you can access — may be large)",
            err=True,
        )
        raise typer.Exit(1)
    except _LocalTableMiss as miss:
        typer.echo(f"Query error: {miss.original}", err=True)
        typer.echo("", err=True)
        typer.echo(remote_table_hint(miss.table, surface="cli"), err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Query error: {e}", err=True)
        raise typer.Exit(1)


def _query_auto(sql: str, fmt: str, limit: int, *, auto_snapshot: bool = False):
    """`--scope auto` (default): run locally first, falling back to
    server-side execution when there's no local data yet or the query hits
    an unresolvable table (possibly `remote`/`server_only`). Any other
    local failure fails exactly like `--scope local` — no fallback."""
    try:
        _run_local(sql, fmt, limit)
    except _LocalDbMissing:
        typer.echo("[scope] no local data yet — running server-side", err=True)
        _query_remote(sql, fmt, limit, auto_snapshot=auto_snapshot)
    except _LocalTableMiss as miss:
        typer.echo(
            f"[scope] '{miss.table}' not found locally — running server-side",
            err=True,
        )
        _query_remote(sql, fmt, limit, auto_snapshot=auto_snapshot)
    except Exception as e:
        typer.echo(f"Query error: {e}", err=True)
        raise typer.Exit(1)


def _normalize_sql(sql: str) -> str:
    """Normalize SQL for deterministic hashing: collapse all whitespace to
    single spaces, strip, lowercase. Two queries that differ only in
    whitespace/case map to the same auto-snapshot id (#616)."""
    return re.sub(r"\s+", " ", (sql or "").strip()).lower()


def _auto_snapshot_id(view_target: str) -> str:
    """Deterministic `auto_<sha8>` snapshot id for a VIEW target (#616).

    Keyed on the view name (not the full SQL) so a JOIN across N views
    gets N distinct snapshots, and so the same view shared by multiple
    queries hits one cached snapshot. The hash anchors the id against
    accidental view-id collisions (e.g. a 32-char identifier vs. its
    truncation) and gives us an opaque prefix that's safe as a DuckDB
    table name. Devin Review ANALYSIS_0001 on #619 (multi-view)."""
    digest = hashlib.sha256(_normalize_sql(view_target).encode("utf-8")).hexdigest()[:8]
    return f"auto_{digest}"


def _snapshot_is_fresh(snapshot_id: str) -> bool:
    """True if a snapshot named ``snapshot_id`` exists with an unexpired TTL.

    A snapshot with no `expires_at` (manually created without --ttl) is NOT
    treated as fresh for auto-reuse — auto-snapshots always carry a TTL, so
    a TTL-less one of the same name is some other artifact we shouldn't lean
    on. Returns False on any read error (rebuild is the safe default)."""
    from datetime import datetime, timezone

    from cli.commands.snapshot import _snap_dir
    from cli.snapshot_meta import read_meta

    try:
        meta = read_meta(_snap_dir(), snapshot_id)
    except Exception:
        return False
    if meta is None or not meta.expires_at:
        return False
    try:
        exp = datetime.fromisoformat(meta.expires_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


def _create_auto_snapshot(*, view_target, snapshot_id, ttl):
    """Materialize the over-cap VIEW's raw data as a local snapshot via
    the `snapshot create --from-query` path (#616).

    Materializes ``SELECT * FROM <view>`` — the **raw view content** — NOT
    the user's full query. The substitution pass below then rewrites the
    original SQL to read from the snapshot, so all transformations (WHERE,
    GROUP BY, DISTINCT, LIMIT, ORDER BY, window functions, …) apply once
    locally. Materializing the full SQL would double-apply every
    transformation when the rewritten query runs (e.g. a COUNT becomes
    COUNT(COUNT(*))). Devin Review BUG_0001 on #619.

    Delegates to the snapshot command's create so the materialize, view
    registration, meta write, and TTL stamping all stay in one place.
    Raises typer.Exit on failure (propagated to the caller)."""
    from cli.commands.snapshot import _create_snapshot

    _create_snapshot(
        table_id=snapshot_id,
        # `SELECT * FROM <view>` is intentionally bare-identifier (no
        # quoting): server-side `from_query` execution runs against the
        # same catalog/connection that resolved the view in the original
        # query, so canonical-case identifiers Just Work. Quoting the
        # bare identifier here would break case-sensitive lookups for
        # users whose registry IDs are not already lowercase.
        from_query=f"SELECT * FROM {view_target}",
        as_name=snapshot_id,
        ttl=ttl,
        force=True,
        quiet=True,
    )


def _substitute_view(sql: str, view: str, replacement: str) -> str:
    """Replace word-boundary occurrences of the bare view identifier ``view``
    with ``replacement`` in ``sql`` (#616). Leaves substrings inside larger
    identifiers untouched (`\\b` anchors). Case-insensitive because
    ``view_targets`` carries the registry's canonical-case identifier
    while the user's SQL may use any case (DuckDB identifiers are
    case-insensitive). A case-sensitive match would silently leave the
    original view name in the rewritten SQL and `_query_local` would die
    with a cryptic "table not found" instead of returning the local
    result. Devin Review BUG_0002 on #619."""
    return re.sub(rf"\b{re.escape(view)}\b", replacement, sql, flags=re.IGNORECASE)


def _try_auto_snapshot_fallback(sql: str, fmt: str, limit: int, detail: dict) -> bool:
    """Handle a structured `remote_scan_too_large` 400 for VIEW targets by
    materializing the view(s) as local snapshot(s) and re-running the query
    locally (#616). Returns True if the fallback ran (success path printed
    output), False if it doesn't apply (caller falls back to re-raise)."""
    if detail.get("reason") != "remote_scan_too_large":
        return False
    view_targets = detail.get("view_targets") or []
    if not view_targets:
        return False

    from cli.commands.snapshot import _format_size

    scan = _format_size(int(detail.get("scan_bytes") or 0))
    cap = _format_size(int(detail.get("limit_bytes") or 0))

    # Multi-view JOINs are now supported (Devin Review ANALYSIS_0001 on
    # #619): each view gets its OWN snapshot keyed on the view name, its
    # own raw-content materialize (`SELECT * FROM <view>`), and its own
    # substitution pass. The previous behaviour deterministic-hashed the
    # full SQL into one snapshot ID and reused it for every view — which
    # would have silently self-joined the first materialized view with
    # itself once BUG_0001 was fixed. Hashing per view also means the
    # same view shared across two over-cap queries hits one cached
    # snapshot instead of two.
    rewritten = sql
    for view in view_targets:
        snapshot_id = _auto_snapshot_id(view)
        if not _snapshot_is_fresh(snapshot_id):
            _create_auto_snapshot(
                view_target=view,
                snapshot_id=snapshot_id,
                ttl=_AUTO_SNAPSHOT_TTL,
            )
        typer.echo(
            f"[auto-snapshot] {view} -> {snapshot_id} ({scan} > {cap} cap)",
            err=True,
        )
        rewritten = _substitute_view(rewritten, view, snapshot_id)

    # Re-run the rewritten SQL against the LOCAL snapshots (normal local path).
    _query_local(rewritten, fmt, limit)
    return True


def _query_remote(sql: str, fmt: str, limit: int, *, auto_snapshot: bool = False):
    """Run query against server DuckDB via API."""
    from cli.client import QUERY_TIMEOUT_S, api_post
    from cli.error_render import render_error

    resp = api_post(
        "/api/query",
        json={"sql": sql, "limit": limit},
        timeout=QUERY_TIMEOUT_S,
    )
    if resp.status_code != 200:
        # Parse JSON body if possible, fall back to text. The shared
        # renderer pretty-prints typed BQ errors (cross_project_forbidden,
        # remote_scan_too_large, bq_path_not_registered) instead of
        # flattening the structured detail to a single truncated line.
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        # #616: opt-in auto-recovery from the BigQuery scan cap on VIEW
        # targets. Only fires for the structured remote_scan_too_large 400
        # with non-empty view_targets; everything else re-raises unchanged.
        if (
            auto_snapshot
            and resp.status_code == 400
            and isinstance(body, dict)
            and isinstance(body.get("detail"), dict)
        ):
            if _try_auto_snapshot_fallback(sql, fmt, limit, body["detail"]):
                return
        typer.echo(render_error(resp.status_code, body), err=True)
        raise typer.Exit(1)

    data = resp.json()
    _output(data["columns"], data["rows"], fmt)
    # Table access policies (§10): `row_scope` is present when a table this
    # query touched has an access policy applied — this result is the
    # caller's own scoped slice, not the whole table. "Silent partial scope
    # is forbidden" (command-ux.md) applies to row filtering as much as to
    # source scope, so this is unconditional, not opt-in. Written to STDERR
    # so json/csv stdout stays pure, same convention as the notices below.
    if data.get("row_scope"):
        typer.echo(f"[scope] {data['row_scope']['note']}", err=True)
    if data.get("truncated"):
        typer.echo(f"(truncated at {limit} rows)", err=True)
    # BigQuery dry-run scan estimate — present only for query_mode='remote'
    # rows; local queries return None and emit no line. Written to STDERR so
    # json/csv stdout stays pure (#393).
    if data.get("bytes_scanned") is not None:
        from cli.commands.snapshot import _format_size

        typer.echo(
            f"BigQuery scanned ~{_format_size(data['bytes_scanned'])} (dry-run estimate)",
            err=True,
        )


def _output(columns: list, rows: list, fmt: str):
    if fmt == "json":
        output = [dict(zip(columns, row)) for row in rows]
        typer.echo(json.dumps(output, indent=2, default=str))
    elif fmt == "csv":
        typer.echo(",".join(columns))
        for row in rows:
            typer.echo(",".join(str(v) if v is not None else "" for v in row))
    else:
        # Table format using rich, with a vertical-record fallback when the
        # column count would collapse every cell to zero width.
        #
        # Issue #255: `SELECT * FROM order_economics LIMIT 3` against a
        # 53-column table on an 80-col TTY produced an empty grid with
        # only header pipes visible — rich shrinks each column to fit and
        # gives up at 53 × 1-char minimum. Fallback to a psql-`\x`-style
        # record view ("─── row 1 ───\n  col: val\n…") when the column
        # count exceeds what the terminal can sensibly render.
        import shutil
        from rich.console import Console
        from rich.table import Table

        term_cols = shutil.get_terminal_size((120, 24)).columns
        # Conservative threshold: rich's column overhead (separators +
        # padding) is ~3 chars; below ~6 chars per column the result is
        # unreadable. Switch to vertical when columns × 6 > terminal.
        too_wide = len(columns) * 6 > term_cols
        console = Console()
        if too_wide:
            for i, row in enumerate(rows, 1):
                console.print(f"─── row {i} ───", style="dim")
                pad = max(len(c) for c in columns)
                for col, val in zip(columns, row):
                    rendered = "" if val is None else str(val)
                    console.print(f"  {col:<{pad}} : {rendered}")
            return
        table = Table()
        for col in columns:
            table.add_column(col)
        for row in rows:
            table.add_row(*(str(v) if v is not None else "" for v in row))
        console.print(table)
