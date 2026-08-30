"""`agnes admin activity` — terminal access to Activity Center.

Three subcommands that mirror the three /api/admin/activity/* JSON endpoints:

    agnes admin activity              # audit_log timeline (last 24h, table output)
    agnes admin activity health       # health pulse (scheduler / sync / users / memory)
    agnes admin activity sync         # cross-table sync history feed

All require admin auth (the server returns 403 otherwise).
"""

from __future__ import annotations

import json as json_lib
from typing import Optional

import typer

from cli.client import api_get

activity_app = typer.Typer(help="Activity Center — audit_log timeline, health pulse, sync history")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_since(value: str) -> int:
    """Parse a human shorthand into minutes.

    Accepts:
        30m   → 30
        1h    → 60
        24h   → 1440
        7d    → 10080
        raw integer string → pass-through

    Raises typer.BadParameter on unrecognised format.
    """
    s = value.strip().lower()
    try:
        return int(s)  # bare integer → minutes
    except ValueError:
        pass
    if s.endswith("m"):
        try:
            return int(s[:-1])
        except ValueError:
            pass
    if s.endswith("h"):
        try:
            return int(s[:-1]) * 60
        except ValueError:
            pass
    if s.endswith("d"):
        try:
            return int(s[:-1]) * 60 * 24
        except ValueError:
            pass
    raise typer.BadParameter(
        f"Cannot parse duration {value!r}. Use formats like 30m, 1h, 24h, 7d, or a bare integer (minutes)."
    )


def _handle_error(resp, context: str) -> None:
    """Print a clean error and exit non-zero for non-2xx responses."""
    if resp.status_code in (401, 403):
        typer.echo(
            "[err] authentication required — run `agnes auth login` or import a PAT",
            err=True,
        )
        raise typer.Exit(1)
    if resp.status_code >= 500:
        try:
            body = resp.json().get("detail", resp.text)
        except Exception:
            body = resp.text
        typer.echo(f"[err] server error on {context}: {body}", err=True)
        raise typer.Exit(1)
    if resp.status_code >= 400:
        try:
            body = resp.json().get("detail", resp.text)
        except Exception:
            body = resp.text
        typer.echo(f"[err] {context}: {body}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# timeline (default callback — invoked as `agnes admin activity`)
# ---------------------------------------------------------------------------


@activity_app.callback(invoke_without_command=True)
def timeline(
    ctx: typer.Context,
    since: str = typer.Option("24h", "--since", help="How far back to look (e.g. 1h, 7d, 30m, or raw minutes)"),
    limit: int = typer.Option(50, "--limit", help="Maximum rows to return (1–200)"),
    action: Optional[str] = typer.Option(None, "--action", help="Filter by action prefix (e.g. sync.)"),
    user: Optional[str] = typer.Option(None, "--user", help="Filter by user_id (full or prefix)"),
    resource: Optional[str] = typer.Option(None, "--resource", help="Filter by resource (e.g. table:orders)"),
    result: Optional[str] = typer.Option(None, "--result", help="Filter by result prefix (e.g. error)"),
    result_class: Optional[str] = typer.Option(
        None, "--result-class", help="Filter by result class (success|error|denied|none|other)"
    ),
    source: Optional[str] = typer.Option(
        None, "--source", help="Filter by source (web|cli|scheduler|system|agent|other)"
    ),
    trail: Optional[str] = typer.Option(
        None,
        "--trail",
        help="Narrow to one physical trail (audit|sync|llm|agent_scope). "
        "Unset returns the unified timeline across all four.",
    ),
    include_self_reads: bool = typer.Option(
        False,
        "--include-self-reads",
        help="Include the Activity Center's own activity.read audit rows (hidden by default)",
    ),
    search: Optional[str] = typer.Option(None, "--search", help="Full-text search on params JSON"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON to stdout"),
):
    """Tail the unified activity timeline (default last 24h, up to 50 rows).

    Folds in audit_log, sync_history, llm_usage and agent_scope_snapshots
    (never chat_messages — privacy decision). Equivalent to GET
    /api/admin/activity with filters.
    """
    if ctx.invoked_subcommand is not None:
        return

    try:
        since_minutes = _parse_since(since)
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)

    params: dict = {"since_minutes": since_minutes, "limit": limit}
    if action:
        params["action_prefix"] = action
    if user:
        params["user_id"] = user
    if resource:
        params["resource"] = resource
    if result:
        params["result_pattern"] = result
    if result_class:
        params["result_class"] = result_class
    if trail:
        params["trail"] = trail
    if source:
        params["source"] = source
    if include_self_reads:
        params["include_self_reads"] = "true"
    if search:
        params["q"] = search

    try:
        resp = api_get("/api/admin/activity", params=params)
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)

    _handle_error(resp, "activity timeline")

    data = resp.json()
    if as_json:
        typer.echo(json_lib.dumps(data, indent=2, default=str))
        return

    rows = data.get("rows", [])
    if not rows:
        typer.echo("No activity found for the given filters.")
        return

    # Table output
    col_time = 20
    col_action = 28
    col_user = 22
    col_result = 10
    header = (
        f"  {'TIME':<{col_time}}  {'ACTION':<{col_action}}  {'USER':<{col_user}}  {'RESULT':<{col_result}}  RESOURCE"
    )
    typer.echo(header)
    typer.echo("  " + "-" * (len(header) - 2))
    for row in rows:
        ts = str(row.get("timestamp") or row.get("ts") or "")[:col_time]
        act = str(row.get("action") or "")[:col_action]
        uid = str(row.get("user_id") or row.get("user_email") or "")[:col_user]
        res = str(row.get("result") or "")[:col_result]
        resource_val = str(row.get("resource") or "")
        typer.echo(f"  {ts:<{col_time}}  {act:<{col_action}}  {uid:<{col_user}}  {res:<{col_result}}  {resource_val}")

    next_cur = data.get("next_cursor")
    if next_cur:
        typer.echo("\n  (more rows available — pass --limit higher or use --json to page with cursor)")


# ---------------------------------------------------------------------------
# health subcommand
# ---------------------------------------------------------------------------


@activity_app.command("health")
def health(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON to stdout"),
):
    """Show the Activity Center health pulse (5 metrics + status sentence).

    Equivalent to GET /api/admin/activity/health.
    """
    try:
        resp = api_get("/api/admin/activity/health")
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)

    _handle_error(resp, "activity health")

    data = resp.json()
    if as_json:
        typer.echo(json_lib.dumps(data, indent=2, default=str))
        return

    status = data.get("status", "unknown")
    sentence = data.get("sentence", "")
    fields = data.get("fields", [])

    # Status line with colour hint
    status_marker = {"green": "[ok]", "yellow": "[!] ", "red": "[!!]"}.get(status, "[?] ")
    typer.echo(f"\n  STATUS  {status_marker} {status.upper()}")
    typer.echo(f"  {sentence}\n")

    col_key = 22
    col_val = 20
    typer.echo(f"  {'METRIC':<{col_key}}  {'VALUE':<{col_val}}  STATUS")
    typer.echo("  " + "-" * (col_key + col_val + 14))
    for f in fields:
        key = str(f.get("key") or "")[:col_key]
        val = str(f.get("value") or "")[:col_val]
        color = f.get("color", "")
        marker = {"green": "ok", "yellow": "warn", "red": "ALERT"}.get(color, color)
        typer.echo(f"  {key:<{col_key}}  {val:<{col_val}}  {marker}")


# ---------------------------------------------------------------------------
# sync subcommand
# ---------------------------------------------------------------------------


@activity_app.command("sync")
def sync(
    since: str = typer.Option("24h", "--since", help="How far back to look (e.g. 1h, 7d, 30m, or raw minutes)"),
    limit: int = typer.Option(100, "--limit", help="Maximum rows to return (1–500)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON to stdout"),
):
    """Show cross-table sync history feed.

    Equivalent to GET /api/admin/activity/sync.
    """
    try:
        since_minutes = _parse_since(since)
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)

    params: dict = {"since_minutes": since_minutes, "limit": limit}

    try:
        resp = api_get("/api/admin/activity/sync", params=params)
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)

    _handle_error(resp, "activity sync")

    data = resp.json()
    if as_json:
        typer.echo(json_lib.dumps(data, indent=2, default=str))
        return

    rows = data.get("rows", [])
    if not rows:
        typer.echo("No sync history found for the given window.")
        return

    col_table = 28
    col_time = 20
    col_rows = 10
    col_dur = 10
    col_status = 8
    header = (
        f"  {'TABLE':<{col_table}}  {'SYNCED AT':<{col_time}}  {'ROWS':>{col_rows}}  {'DURATION':>{col_dur}}  STATUS"
    )
    typer.echo(header)
    typer.echo("  " + "-" * (len(header) - 2))
    for row in rows:
        table_id = str(row.get("table_id") or row.get("name") or "")[:col_table]
        raw_ts = str(row.get("synced_at") or row.get("last_synced_at") or "")
        # Render ISO 8601 → `YYYY-MM-DD HH:MM:SSZ` (19 chars). Naive
        # `[:col_time]` on an ISO timestamp truncated at 20 chars after
        # the microseconds delimiter, producing the trailing-dot eyesore
        # `2026-05-26T12:46:54.` — meaningless to readers and breaks the
        # column alignment in scripts that grep on whitespace.
        synced_at = (raw_ts.replace("T", " ")[:19] + "Z") if raw_ts else ""
        row_count = str(row.get("rows") or row.get("row_count") or "")
        dur_ms = row.get("duration_ms")
        dur_str = f"{dur_ms}ms" if dur_ms is not None else ""
        status_val = str(row.get("status") or "")
        typer.echo(
            f"  {table_id:<{col_table}}  {synced_at:<{col_time}}  {row_count:>{col_rows}}  {dur_str:>{col_dur}}  {status_val}"
        )
