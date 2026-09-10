"""`agnes issue …` — report a problem and follow your own reports.

Subcommand → route (all accept the default `agnes login` credential):

    issue report TITLE      POST /api/issues (+ PUT /api/issues/{id}/screenshot)
    issue list               GET  /api/issues/mine
    issue show ID            GET  /api/issues/{id}
    issue comment ID TEXT    POST /api/issues/{id}/comments

ID is `42`, `#42` or `iss_…` — every subcommand accepts any of the three.
The admin queue is `agnes admin issue …` (placement follows authority — same
split as `semantic-model feedback` vs `admin semantic feedback`: this group
is any signed-in user's own reports, the admin group is every report).
"""

from __future__ import annotations

import json
import platform
from datetime import UTC, datetime
from pathlib import Path

import typer

from cli.client import api_get, api_post, api_put
from cli.error_render import render_error
from cli.query_hints import issue_not_found_hint

issue_app = typer.Typer(help="Report a problem and follow your own reports", no_args_is_help=True)

KINDS = ("bug", "wrong_answer", "request", "question", "other")
_CLIENT_HEADER = {"X-Agnes-Client": "cli"}
_ISSUES_PATH = "/api/issues"
_MINE_PATH = "/api/issues/mine"
#: Mirrors `_MAX_SCREENSHOT_BYTES` in app/api/issues.py — checked client-side
#: so an oversized file is refused before it is read, not after.
_MAX_SCREENSHOT_BYTES = 3 * 1024 * 1024


def _fail(resp) -> None:
    """Render an HTTP error response and exit non-zero.

    Two special cases before the generic ``render_error`` fallback: a `501`
    is the A3 PG-first-ratchet refusal (this table is Postgres-only), which
    reads better as one plain sentence than the raw typed body, and a `404`
    gets the shared "list your reports" hint prepended — issue ids must not
    be probeable, so the CLI never distinguishes "wrong id" from "someone
    else's report" (see `app/api/issues.py`'s `_owned_or_admin`).
    """
    if resp.status_code == 501:
        typer.echo(
            "This command needs the Postgres app-state backend — this instance still runs the frozen "
            "DuckDB backend. Migrate it (see docs/migrations.md) to use issue reporting.",
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
    """Normalize `#42` / `42` / `iss_…` to whatever the API path segment
    accepts — the server resolves all three forms itself, so this only
    strips the leading `#` a caller may have typed."""
    return issue_id.lstrip("#")


def _display_ref(ref: str) -> str:
    return f"#{ref}" if ref.isdigit() else ref


def _doctor_client_section() -> dict:
    """The client half of `agnes doctor` (never the admin-only server half —
    that one calls out to the server and can carry another user's data)."""
    from cli.commands.doctor import _build_bundle

    return _build_bundle()["client"]


def _age(iso_ts: str | None) -> str:
    """Compact age like `3d` / `4h` / `12m`, or `-` when unparseable."""
    if not iso_ts:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso_ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    except ValueError:
        return "-"
    delta = datetime.now(UTC) - dt
    seconds = max(delta.total_seconds(), 0)
    if seconds >= 86400:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 60)}m"


def _replies_label(count: int) -> str:
    if not count:
        return "-"
    return f"{count} reply" if count == 1 else f"{count} replies"


@issue_app.command("report")
def report(
    title: str = typer.Argument(..., help="One line: what is wrong"),
    body: str | None = typer.Option(None, "-m", "--body", help="What happened"),
    kind: str = typer.Option("bug", "--kind", help="bug | wrong_answer | request | question | other"),
    url: str | None = typer.Option(None, "--url", help="Page or object the problem is about"),
    screenshot: Path | None = typer.Option(None, "--screenshot", exists=True, dir_okay=False, help="PNG to attach"),
    attach_doctor: bool = typer.Option(
        False, "--attach-doctor", help="Embed the client section of `agnes doctor` (auth/workspace/local-delivery)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Report a problem — a bug, a wrong answer, something missing, or something unclear.

    Any signed-in caller. Stored in this instance; a text summary is
    mirrored to the operator's channel when one is configured (see
    `docs/issue-reporting.md`). The client version, platform, and a
    timestamp are attached automatically — `--attach-doctor` additionally
    embeds the client half of a support bundle.
    """
    if kind not in KINDS:
        typer.echo(f"--kind must be one of: {', '.join(KINDS)}", err=True)
        raise typer.Exit(2)

    # Validate the screenshot BEFORE the report is created. Checking it at the
    # upload site meant an oversized file still filed the report, told the
    # server to expect a screenshot — so the operator mirror waited 20 s for an
    # upload that never came — and then exited without printing the number, so
    # the reporter believed nothing had been filed and reported it again
    # (#2402). Refusing up front costs nothing and leaves no orphan.
    if screenshot is not None:
        size = screenshot.stat().st_size
        if size > _MAX_SCREENSHOT_BYTES:
            typer.echo(
                f"{screenshot.name} is {size / 1024 / 1024:.1f} MB — the limit is 3 MB. "
                "Attach a smaller image, or report it without one. Nothing was filed.",
                err=True,
            )
            raise typer.Exit(2)

    from cli.main import _cli_version

    context: dict = {
        "cli_version": _cli_version(),
        "platform": platform.platform(),
        "captured_at": datetime.now(UTC).isoformat(),
    }
    if attach_doctor:
        context["doctor"] = _doctor_client_section()

    resp = api_post(
        _ISSUES_PATH,
        json={
            "title": title,
            "body": body,
            "kind": kind,
            "page_url": url,
            "context": context,
            # The upload is a separate PUT below, so the operator mirror has
            # to be told to wait for it — otherwise its message names no
            # screenshot even when `--screenshot` attached one.
            "expect_screenshot": screenshot is not None,
        },
        headers=_CLIENT_HEADER,
    )
    if resp.status_code != 201:
        _fail(resp)
    row = resp.json()

    if screenshot is not None:
        data = screenshot.read_bytes()
        put = api_put(
            f"{_ISSUES_PATH}/{row['id']}/screenshot",
            content=data,
            headers={"Content-Type": "image/png", **_CLIENT_HEADER},
        )
        if put.status_code != 204:
            typer.echo(f"Filed #{row['number']} but the screenshot was refused: {put.text}", err=True)

    if as_json:
        typer.echo(json.dumps(row, indent=2, default=str))
        return
    typer.echo(f"Filed #{row['number']} ({row['id']})")
    # Deliberately says nothing about webhook delivery. The creation response
    # ALWAYS predates it — the operator mirror is a background task that has
    # not run yet, and with a screenshot it waits on purpose — so reading
    # `webhook_delivered_at` here reported "no operator channel confirmed
    # delivery" even when the message landed a second later (review on
    # #2402). What is true at this point is that the report is stored; whether
    # the chat copy went out is `agnes issue show` a moment later.
    typer.echo(f"Follow it: agnes issue show {row['number']}")


@issue_app.command("list")
def list_issues(
    status: str = typer.Option("open", "--status", help="open | resolved | all"),
    limit: int = typer.Option(50, "--limit", help="Max rows to show"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Your own issue reports, most recently active first."""
    params: dict = {"limit": limit}
    if status != "all":
        params["status"] = status
    resp = api_get(_MINE_PATH, params=params)
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    rows = body.get("data", [])
    if not rows:
        typer.echo('No issues yet. Report one with: agnes issue report "<what is wrong>"')
        return

    typer.echo(f"{'#':<6}{'KIND':<14}{'STATUS':<10}{'REPLIES':<10}{'AGE':<6}TITLE")
    for r in rows:
        typer.echo(
            f"{'#' + str(r.get('number')):<6}{r.get('kind', ''):<14}{r.get('status', ''):<10}"
            f"{_replies_label(r.get('comment_count', 0)):<10}{_age(r.get('created_at')):<6}{r.get('title', '')}"
        )
    truncated = body.get("truncated")
    if truncated:
        typer.echo(f"… showing {truncated['limit']} of {truncated['total']} — raise --limit")


@issue_app.command("show")
def show_issue(
    issue_id: str = typer.Argument(..., help="Issue id (iss_…), number (42), or #number"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """One issue report — header, body, then comments."""
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


@issue_app.command("comment")
def add_comment(
    issue_id: str = typer.Argument(..., help="Issue id (iss_…), number (42), or #number"),
    text: str = typer.Argument(..., metavar="TEXT", help="Comment text"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Add a comment to one of your own issue reports."""
    ref = _ref(issue_id)
    resp = api_post(f"{_ISSUES_PATH}/{ref}/comments", json={"body": text}, headers=_CLIENT_HEADER)
    if resp.status_code != 201:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return
    typer.echo(f"Comment added to {_display_ref(ref)}")
