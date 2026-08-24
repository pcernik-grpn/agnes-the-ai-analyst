"""Explore commands — agnes explore {table}."""

import json
from pathlib import Path

import typer

from src.sql_ident import quote_ident

explore_app = typer.Typer(help="Explore data tables")

_VALID_SCOPES = ("auto", "local", "server")


class _LocalDbMissing(Exception):
    """Raised by `_run_explore_local` when there's no local DuckDB file yet."""


class _LocalTableMiss(Exception):
    """Raised by `_run_explore_local` when `table` isn't in the local
    DuckDB — possibly a `query_mode='remote'` or `server_only` table, which
    by design has no local view (#607)."""

    def __init__(self, table: str, available: list[str]):
        super().__init__(f"Table '{table}' not found")
        self.table = table
        self.available = available


@explore_app.callback(invoke_without_command=True)
def explore(
    table: str = typer.Argument(..., help="Table name to explore"),
    remote: bool = typer.Option(False, "--remote", help="Fetch from server"),
    scope: str = typer.Option(
        None,
        "--scope",
        help="Where to look: auto (local first, fall back to server), local, server [default: auto]",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show profile and sample data for a table."""
    if scope is not None and scope not in _VALID_SCOPES:
        typer.echo(
            f"Error: --scope must be one of {', '.join(_VALID_SCOPES)} (got {scope!r}).",
            err=True,
        )
        raise typer.Exit(1)

    # `None` means --scope was not given (defaults to auto) — same sentinel
    # convention as `agnes query` so an explicit `--scope local` isn't
    # rejected as conflicting with the (harmless) default.
    scope_explicit = scope is not None
    scope = scope or "auto"

    if remote and scope_explicit and scope == "local":
        typer.echo("Error: --remote and --scope local are mutually exclusive.", err=True)
        raise typer.Exit(1)

    effective_scope = "server" if remote else scope

    if effective_scope == "server":
        _explore_remote(table, as_json)
    elif effective_scope == "local":
        _explore_local(table, as_json)
    else:
        _explore_auto(table, as_json)


def _run_explore_local(table: str, as_json: bool):
    """Execute the local-DuckDB profile lookup for `table`.

    Raises `_LocalDbMissing` if there's no local DB yet, `_LocalTableMiss`
    if `table` doesn't resolve to a table or view. Callers decide how to
    present each case (scope=local prints today's guidance and exits;
    scope=auto falls back to the server).
    """
    from src.duckdb_conn import _open_duckdb

    from cli.lib.workspace_resolve import resolve_data_workspace

    local_dir = resolve_data_workspace() or Path.cwd().resolve()
    db_path = local_dir / "user" / "duckdb" / "analytics.duckdb"
    if not db_path.exists():
        raise _LocalDbMissing()

    conn = _open_duckdb(str(db_path), read_only=True)
    try:
        # Check table exists
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name = ?", [table]
            ).fetchall()
        ]
        if not tables:
            # Also check views
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_name = ? AND table_type='VIEW'",
                    [table],
                ).fetchall()
            ]
        if not tables:
            available = [
                r[0]
                for r in conn.execute("SELECT table_name FROM information_schema.tables ORDER BY table_name").fetchall()
            ]
            raise _LocalTableMiss(table, available)

        # Row count
        count = conn.execute(f"SELECT count(*) FROM {quote_ident(table)}").fetchone()[0]

        # Column info
        columns = conn.execute(f"DESCRIBE {quote_ident(table)}").fetchall()
        col_info = [{"name": c[0], "type": c[1], "nullable": c[2]} for c in columns]

        # Sample rows
        sample = conn.execute(f"SELECT * FROM {quote_ident(table)} LIMIT 5").fetchall()
        sample_cols = [desc[0] for desc in conn.description]

        info = {
            "table": table,
            "row_count": count,
            "columns": col_info,
            "sample_rows": [dict(zip(sample_cols, row)) for row in sample],
        }

        if as_json:
            typer.echo(json.dumps(info, indent=2, default=str))
        else:
            typer.echo(f"Table: {table}")
            typer.echo(f"Rows: {count:,}")
            typer.echo(f"Columns ({len(col_info)}):")
            for c in col_info:
                typer.echo(f"  {c['name']:30s} {c['type']}")
            typer.echo(f"\nSample ({min(5, count)} rows):")
            from rich.console import Console
            from rich.table import Table

            console = Console()
            t = Table()
            for c in sample_cols:
                t.add_column(c)
            for row in sample:
                t.add_row(*(str(v) if v is not None else "" for v in row))
            console.print(t)
    finally:
        conn.close()


def _explore_local(table: str, as_json: bool):
    """`--scope local` behavior: today's guidance messages on failure, no
    server-side fallback."""
    try:
        _run_explore_local(table, as_json)
    except _LocalDbMissing:
        typer.echo("Local DuckDB not found. Run: agnes pull", err=True)
        raise typer.Exit(1)
    except _LocalTableMiss as miss:
        typer.echo(f"Table '{miss.table}' not found. Available:", err=True)
        for name in miss.available:
            typer.echo(f"  {name}")
        raise typer.Exit(1)


def _explore_auto(table: str, as_json: bool):
    """`--scope auto` (default): look locally first, falling back to
    server-side execution when there's no local data yet or `table` isn't
    resolvable locally (possibly `remote`/`server_only`)."""
    try:
        _run_explore_local(table, as_json)
    except _LocalDbMissing:
        typer.echo("[scope] no local data yet — running server-side", err=True)
        _explore_remote(table, as_json)
    except _LocalTableMiss as miss:
        typer.echo(f"[scope] '{miss.table}' not found locally — running server-side", err=True)
        _explore_remote(table, as_json)


def _explore_remote(table: str, as_json: bool):
    from cli.client import api_get

    resp = api_get(f"/api/catalog/profile/{table}")
    if resp.status_code != 200:
        typer.echo(f"Profile not found: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json.dumps(resp.json(), indent=2))
    else:
        profile = resp.json()
        typer.echo(f"Table: {table}")
        typer.echo(json.dumps(profile, indent=2, default=str))
