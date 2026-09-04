"""`agnes admin sessions` — terminal access to the global Sessions browser.

Three subcommands mirroring the new /api/admin/sessions/* endpoints:

    agnes admin sessions list                       # all sessions, table view
    agnes admin sessions list --errors --since 7d   # only sessions with tool errors
    agnes admin sessions show <user> <file>         # transcript dump (chronological)
    agnes admin sessions download <user> <file>     # save the raw .jsonl

`<user>` is the e-mail the listing prints in its User column (the server
resolves it to the on-disk session directory) or that directory name itself.

All require admin auth.
"""

from __future__ import annotations

import json as json_lib
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import typer

from cli.client import api_get
from cli.commands.admin_activity import _parse_since, _handle_error

sessions_app = typer.Typer(help="Browse Claude Code sessions across all users")


#: `{username}` / `{session_file}` are URL path SEGMENTS — percent-encode
#: them so an e-mail (`@`, `+`) or any other unexpected character reaches the
#: server as one segment instead of silently re-shaping the request path.
def _seg(value: str) -> str:
    return quote(value, safe="")


def _fmt_duration(s: Optional[int]) -> str:
    if s is None:
        return "—"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s / 3600:.1f}h"


def _fmt_ts(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    # Compact for table display: YYYY-MM-DD HH:MM
    return iso.replace("T", " ")[:16]


@sessions_app.command("list")
def list_sessions(
    since: str = typer.Option(
        "7d",
        "--since",
        help="How far back to look (e.g. 1h, 24h, 7d, 30d, or raw minutes)",
    ),
    anchor: str = typer.Option(
        "uploaded",
        "--anchor",
        help="Window anchor: 'uploaded' (arrival — default) or 'started'",
    ),
    user: Optional[str] = typer.Option(
        None,
        "--user",
        "-u",
        help="Filter to a single user — the e-mail shown in the User column (exact match)",
    ),
    errors_only: bool = typer.Option(
        False,
        "--errors",
        help="Show only sessions where at least one tool call failed",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Filter by primary model name (e.g. 'claude-sonnet-4-6')",
    ),
    q: Optional[str] = typer.Option(
        None,
        "--q",
        "--query",
        "-q",
        help="Search session_id or filename",
    ),
    limit: int = typer.Option(50, "--limit", help="Rows per page (1–200)"),
    offset: int = typer.Option(0, "--offset"),
    sort: str = typer.Option(
        "tool_errors:desc",
        "--sort",
        help="Sort spec: `<col>:<asc|desc>`. Cols: started_at, tool_calls, tool_errors, active_seconds, username, primary_model",
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """List sessions across all users.

    Default sort surfaces error-heavy sessions first so operators can drill
    into failures quickly. Pass `--sort started_at:desc` for chronological.
    """
    params = {
        "since_minutes": _parse_since(since),
        "anchor": anchor,
        "limit": limit,
        "offset": offset,
        "sort": sort,
    }
    if user:
        params["username"] = user
    if model:
        params["model"] = model
    if errors_only:
        params["only_errors"] = "true"
    if q:
        params["q"] = q

    resp = api_get("/api/admin/sessions/list", params=params)
    _handle_error(resp, "sessions list")
    data = resp.json()

    if as_json:
        typer.echo(json_lib.dumps(data, indent=2, default=str))
        return

    rows = data.get("rows", [])
    if not rows:
        typer.echo("No sessions match these filters.")
        return

    # Compact table layout — fits a 120-char terminal.
    header = f"{'When':<17} {'User':<20} {'Active':>7} {'Tools':>6} {'Errs':>5} {'Model':<28} {'File':<24}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in rows:
        when = _fmt_ts(r.get("started_at"))
        uname = (r.get("username") or "—")[:20]
        active = _fmt_duration(r.get("active_seconds"))
        # ALL call kinds (native + MCP + subagent), same total the web list shows.
        tools = str((r.get("tool_calls") or 0) + (r.get("mcp_calls") or 0) + (r.get("subagent_dispatches") or 0))
        errs = str(r.get("tool_errors") or 0)
        model_s = (r.get("primary_model") or "—")[:28]
        fname = (r.get("session_file") or "").split("/")[-1][:24]
        # Highlight error rows with a leading `!`
        prefix = "! " if (r.get("tool_errors") or 0) > 0 else "  "
        typer.echo(f"{prefix}{when:<15} {uname:<20} {active:>7} {tools:>6} {errs:>5} {model_s:<28} {fname:<24}")

    typer.echo("")
    total = data.get("total", 0)
    shown_lo = offset + 1 if rows else 0
    shown_hi = offset + len(rows)
    typer.echo(
        f"Showing {shown_lo}–{shown_hi} of {total}."
        + (f"  Next page: --offset {data['next_offset']}" if data.get("next_offset") is not None else "")
    )


@sessions_app.command("show")
def show_transcript(
    username: str = typer.Argument(
        ...,
        help="User e-mail as printed by `agnes admin sessions list`, or the on-disk session directory",
    ),
    session_file: str = typer.Argument(..., help="Session filename, e.g. abc-123-def.jsonl"),
    errors_only: bool = typer.Option(False, "--errors", help="Print only tool-result events flagged as errors"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON instead of pretty transcript"),
):
    """Print a session's transcript chronologically.

    Tool calls are shown with their JSON input; tool results show the
    flattened text output. Errored tool results are marked with [ERROR].
    Use `--errors` to grep to just the failures.
    """
    resp = api_get(f"/api/admin/sessions/{_seg(username)}/{_seg(session_file)}/transcript")
    _handle_error(resp, "sessions show")
    data = resp.json()

    if as_json:
        typer.echo(json_lib.dumps(data, indent=2, default=str))
        return

    summary = data.get("summary") or {}
    if summary:
        typer.echo(f"# Session: {username}/{session_file}")
        typer.echo(f"# Started:  {summary.get('started_at') or '—'}")
        typer.echo(f"# Active:   {_fmt_duration(summary.get('active_seconds'))}")
        native = summary.get("tool_calls") or 0
        mcp = summary.get("mcp_calls") or 0
        subagents = summary.get("subagent_dispatches") or 0
        total_calls = native + mcp + subagents
        typer.echo(
            f"# Tools:    {total_calls} calls ({native} native · {mcp} MCP · {subagents} subagent), "
            f"{summary.get('tool_errors') or 0} errors"
        )
        typer.echo(f"# Model:    {summary.get('primary_model') or '—'}")
        typer.echo("")
    else:
        typer.echo(f"# Session: {username}/{session_file}  (no summary row — UsageProcessor hasn't run)")
        typer.echo("")

    events = data.get("events", [])
    if errors_only:
        events = [e for e in events if e.get("kind") == "tool_result" and e.get("is_error")]
        if not events:
            typer.echo("No tool errors in this session.")
            return

    for ev in events:
        kind = ev.get("kind")
        ts = (ev.get("ts") or "")[11:19]
        if kind == "text":
            role = ev.get("role", "user")
            typer.echo(f"--- [{ts}] {role.upper()} ---")
            typer.echo(ev.get("text") or "")
            typer.echo("")
        elif kind == "tool_use":
            tool = ev.get("tool_name") or "?"
            typer.echo(f"--- [{ts}] TOOL_USE: {tool} ---")
            inp = ev.get("input")
            typer.echo(json_lib.dumps(inp, indent=2) if inp is not None else "—")
            typer.echo("")
        elif kind == "tool_result":
            marker = "TOOL_RESULT [ERROR]" if ev.get("is_error") else "TOOL_RESULT"
            tu_id = ev.get("tool_use_id") or ""
            typer.echo(f"--- [{ts}] {marker} (for {tu_id[:8]}…) ---")
            typer.echo(ev.get("text") or "—")
            typer.echo("")


@sessions_app.command("download")
def download(
    username: str = typer.Argument(
        ...,
        help="User e-mail as printed by `agnes admin sessions list`, or the on-disk session directory",
    ),
    session_file: str = typer.Argument(..., help="Session filename, e.g. abc-123-def.jsonl"),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Where to write the file. Defaults to ./<session_file> in the current dir.",
    ),
):
    """Download the raw JSONL of a session.

    Useful when you want to feed it to another tool, grep with jq, or
    archive the conversation. Same audit-log entry as the web download.
    """
    # No stream=True: httpx.Client.get has no such kwarg (passing it raised
    # TypeError before the request ever left), and api_get buffers the
    # response body regardless. Session JSONLs are small; buffering is fine.
    resp = api_get(f"/api/admin/sessions/{_seg(username)}/{_seg(session_file)}/download")
    _handle_error(resp, "sessions download")

    target = output or Path(session_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with target.open("wb") as fh:
        for chunk in resp.iter_bytes():
            fh.write(chunk)
            n += len(chunk)
    typer.echo(f"Wrote {n} bytes to {target}")


@sessions_app.command("kpis")
def kpis(
    since: str = typer.Option("7d", "--since"),
    anchor: str = typer.Option("uploaded", "--anchor", help="Window anchor: uploaded|started"),
    user: Optional[str] = typer.Option(None, "--user", "-u"),
    errors_only: bool = typer.Option(False, "--errors"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Show top-level numbers (sessions, distinct users, error rate)."""
    params = {"since_minutes": _parse_since(since), "anchor": anchor}
    if user:
        params["username"] = user
    if errors_only:
        params["only_errors"] = "true"
    resp = api_get("/api/admin/sessions/kpis", params=params)
    _handle_error(resp, "sessions kpis")
    data = resp.json()
    if as_json:
        typer.echo(json_lib.dumps(data, indent=2))
        return
    typer.echo(f"Sessions:           {data.get('sessions_total', 0):,}")
    typer.echo(f"Distinct users:     {data.get('distinct_users', 0):,}")
    typer.echo(f"Sessions w/ errors: {data.get('error_sessions', 0):,}")
    typer.echo(f"Tool calls total:   {data.get('tool_calls_total', 0):,}")
    typer.echo(f"Tool errors total:  {data.get('tool_errors_total', 0):,}")
    typer.echo(f"Tool error rate:    {data.get('tool_error_rate', 0) * 100:.2f}%")
