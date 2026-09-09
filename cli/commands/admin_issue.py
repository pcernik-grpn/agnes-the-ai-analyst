"""`agnes admin issue …` — the issue-report queue: list, show, reply, resolve.

Same split as `semantic-model feedback` (any signed-in user files a report)
vs `admin semantic feedback` (the admin works the queue): `agnes issue …`
(`cli/commands/issue.py`) is a caller's own reports, this group is every
report, across every reporter.

    admin issue list             GET  /api/admin/issues
    admin issue show ID          GET  /api/issues/{id}
    admin issue reply ID TEXT    POST /api/issues/{id}/comments
    admin issue resolve ID       POST /api/admin/issues/{id}/resolve

ID is `42`, `#42` or `iss_…` — every subcommand accepts any of the three.
"""

from __future__ import annotations

import json

import typer

from cli.client import api_get, api_post
from cli.error_render import render_error
from cli.query_hints import issue_not_found_hint

admin_issue_app = typer.Typer(help="Issue reports from users: the queue, replies, resolution", no_args_is_help=True)

_QUEUE_PATH = "/api/admin/issues"
_ISSUES_PATH = "/api/issues"
_CLIENT_HEADER = {"X-Agnes-Client": "cli"}


def _fail(resp) -> None:
    """Render an HTTP error response and exit non-zero — same shape as
    `cli.commands.issue._fail` (duplicated rather than imported: the two
    groups are deliberately independent modules, same as `semantic_model.py`
    / `admin_semantic.py`'s own pair of `_fail_needs_postgres` helpers)."""
    if resp.status_code == 501:
        typer.echo(
            "This command needs the Postgres app-state backend — this instance still runs the frozen "
            "DuckDB backend. Migrate it (see docs/migrations.md) to use the issue queue.",
            err=True,
        )
        raise typer.Exit(1)
    if resp.status_code == 404:
        typer.echo(issue_not_found_hint(), err=True)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — error rendering must not itself crash
        body = resp.text
    typer.echo(render_error(resp.status_code, body), err=True)
    raise typer.Exit(1)


def _ref(issue_id: str) -> str:
    return issue_id.lstrip("#")


def _display_ref(ref: str) -> str:
    return f"#{ref}" if ref.isdigit() else ref


@admin_issue_app.command("list")
def list_queue(
    status: str = typer.Option("open", "--status", help="open | resolved | all"),
    limit: int = typer.Option(100, "--limit", help="Max rows to show"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Every issue report, across every reporter (admin only)."""
    params: dict = {"limit": limit}
    if status != "all":
        params["status"] = status
    resp = api_get(_QUEUE_PATH, params=params)
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    rows = body.get("data", [])
    if not rows:
        typer.echo("No issues in the queue.")
        return

    typer.echo(f"{'#':<6}{'KIND':<14}{'STATUS':<10}{'REPORTER':<28}TITLE")
    for r in rows:
        reporter = r.get("created_by_email") or r.get("created_by") or ""
        typer.echo(
            f"{'#' + str(r.get('number')):<6}{r.get('kind', ''):<14}{r.get('status', ''):<10}"
            f"{reporter:<28}{r.get('title', '')}"
        )
    truncated = body.get("truncated")
    if truncated:
        typer.echo(f"… showing {truncated['limit']} of {truncated['total']} — raise --limit")


@admin_issue_app.command("show")
def show_issue(
    issue_id: str = typer.Argument(..., help="Issue id (iss_…), number (42), or #number"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """One issue report and its comments (any reporter — admin sees every report)."""
    resp = api_get(f"{_ISSUES_PATH}/{_ref(issue_id)}")
    if resp.status_code != 200:
        _fail(resp)
    row = resp.json()
    if as_json:
        typer.echo(json.dumps(row, indent=2, default=str))
        return

    typer.echo(
        f"#{row.get('number')} · {row.get('kind')} · {row.get('status')} · filed {row.get('created_at')} "
        f"by {row.get('created_by_email') or row.get('created_by')}"
    )
    if row.get("body"):
        typer.echo("")
        typer.echo(row["body"])
    comments = row.get("comments") or []
    typer.echo("")
    typer.echo(f"--- {len(comments)} comment{'' if len(comments) == 1 else 's'}")
    for c in comments:
        typer.echo(f"[{c.get('author_kind')}] {c.get('author_email') or c.get('author_id')} {c.get('created_at')}")
        typer.echo(f"  {c.get('body')}")


@admin_issue_app.command("reply")
def reply(
    issue_id: str = typer.Argument(..., help="Issue id (iss_…), number (42), or #number"),
    text: str = typer.Argument(..., metavar="TEXT", help="Reply text"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Reply to a user's issue report — the same comment thread `agnes issue show` reads."""
    ref = _ref(issue_id)
    resp = api_post(f"{_ISSUES_PATH}/{ref}/comments", json={"body": text}, headers=_CLIENT_HEADER)
    if resp.status_code != 201:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return
    typer.echo(f"Reply added to {_display_ref(ref)}")


@admin_issue_app.command("resolve")
def resolve(
    issue_id: str = typer.Argument(..., help="Issue id (iss_…), number (42), or #number"),
    note: str | None = typer.Option(None, "--note", help="What was done about it — recorded on the report"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Mark an issue report resolved, with an optional note."""
    ref = _ref(issue_id)
    resp = api_post(f"{_QUEUE_PATH}/{ref}/resolve", json={"resolution_note": note})
    if resp.status_code == 404:
        typer.echo(f"No issue {issue_id!r}.", err=True)
        typer.echo("  Find it: agnes admin issue list", err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        _fail(resp)
    row = resp.json()
    if as_json:
        typer.echo(json.dumps(row, indent=2, default=str))
        return
    typer.echo(f"Resolved: #{row.get('number')} by {row.get('resolved_by')}")
