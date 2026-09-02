"""`agnes admin sharepoint` — admin/ops triggers and status for SharePoint
connector maintenance.

Two surfaces:

  - ``facts-extract`` — the standalone fact-graph trigger.
  - ``runs`` — the extraction fleet dashboard (2026-09-02), from the
    terminal: is it on pace, is anything stuck, what is it costing, across
    every SharePoint connection at once. CLI counterpart to
    ``GET /api/admin/sharepoint/extraction/runs`` — the same endpoint
    ``/admin/extraction`` polls.

The crawl / ACL-sync / subtree-sweep TRIGGERS stay admin-web-UI-only, an
established precedent (see CONTRIBUTING.md's "admin/scheduler maintenance
op" exemption class, `tests/test_documentation_api_triple_surface.py`) —
``facts-extract`` earns a CLI counterpart because an operator asking "how do
we get the fact graph populated with what we already have?" needs an answer
that does not require opening a browser (a support runbook, a script run
against a remote instance). ``runs`` earns one for the same reason a monitor
does: an operator watching an 8-connection, ~20-hour extraction over SSH has
no browser open at all.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import typer
from rich.console import Console
from rich.table import Table

from cli.client import api_get, api_post

admin_sharepoint_app = typer.Typer(help="Admin: SharePoint connector maintenance triggers")
# `runs` renders a nine-column table (connection through error). A default,
# terminal-detected width truncates every cell to a few characters when
# stdout isn't a real tty (piped output, the test runner) — a fixed, wide
# console keeps the fleet table readable regardless of where it's printed.
_console = Console(width=200)


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        msg = detail
    elif isinstance(detail, dict):
        # A structured detail carries the human line in `message` — dumping
        # the whole object would bury it in escaped JSON, the one thing
        # this branch exists to avoid. Same reader as `admin_digest.py`'s
        # own `_fail`.
        msg = detail.get("message") or detail.get("error") or json.dumps(detail)
    elif detail is not None:
        msg = json.dumps(detail)
    else:
        msg = resp.text or f"HTTP {resp.status_code}"
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


@admin_sharepoint_app.command("facts-extract")
def facts_extract(
    connection_id: str = typer.Argument(..., help="SharePoint source_connections id"),
    doc_id: List[str] = typer.Option(
        [], "--doc-id", help="Narrow the pass to this document's source_doc_id (repeatable)"
    ),
    timeout_s: Optional[int] = typer.Option(
        None,
        "--timeout-s",
        help="Hard ceiling for this one run, seconds (0 = unbounded). Default: the configured "
        "extraction.facts.run_timeout_s.",
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Build the fact graph over this connection's ALREADY-INDEXED corpus,
    without running a crawl first.

    Enqueues the ``sharepoint-facts-extraction`` job — the same job a "run
    facts extraction now" click on the admin source card triggers, and the
    ONLY way to (re)build the graph over documents already sitting in a
    collection: previously the sole trigger was chained onto a crawl's own
    tail, so populating the graph over an existing corpus meant re-running
    an entire crawl just to reach it.

    Refuses with a clear reason rather than a bare HTTP error: ``409
    facts_extraction_disabled`` (``extraction.facts.enabled`` or
    ``facts.enabled`` is off), ``409 facts_extraction_already_running`` (one
    is already queued/running for this connection), ``404`` (unknown or
    non-SharePoint connection id).
    """
    payload: dict = {}
    if doc_id:
        payload["doc_ids"] = list(doc_id)
    if timeout_s is not None:
        payload["timeout_s"] = timeout_s

    resp = api_post(
        f"/api/admin/sharepoint/connections/{connection_id}/facts-extract",
        json=payload or None,
    )
    if resp.status_code != 202:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Enqueued sharepoint-facts-extraction job {body.get('job_id')} (status: {body.get('status')})")


# ---------------------------------------------------------------------------
# `runs` — the extraction fleet dashboard, from the terminal.
# ---------------------------------------------------------------------------


def _fmt_ago(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.0f}h ago"


def _fmt_rate(rate: Optional[float]) -> str:
    return "—" if rate is None else f"{rate:.1f}"


def _fmt_cost(usd: Optional[float]) -> str:
    return "—" if not usd else f"${usd:.4f}"


def _fmt_tokens(usage: Dict[str, Any]) -> str:
    """Sums input/output tokens across every priced stage (`ner`/`ocr`/
    `facts`) — see the API's `_run_total_cost_usd` for why `usage` is keyed
    by stage and only populated once a run has finished."""
    in_tok = out_tok = 0
    seen = False
    for stage in (usage or {}).values():
        if not isinstance(stage, dict):
            continue
        if isinstance(stage.get("input_tokens"), (int, float)):
            in_tok += stage["input_tokens"]
            seen = True
        if isinstance(stage.get("output_tokens"), (int, float)):
            out_tok += stage["output_tokens"]
            seen = True
    return f"{in_tok:,} / {out_tok:,}" if seen else "—"


def _fmt_facts(facts: Optional[Dict[str, Any]]) -> str:
    if not facts:
        return "—"
    done = facts.get("docs_done")
    if facts.get("phase_active"):
        total = facts.get("docs_total")
        pending = max((total or 0) - (done or 0), 0) if total is not None and done is not None else "?"
        return f"{done or 0} / {total if total is not None else '?'} ({pending} pending)"
    if done is not None:
        return f"{done} (0 pending)"
    return "—"


def _fmt_phase(run: Optional[Dict[str, Any]]) -> str:
    if not run:
        return "idle"
    outcome = run.get("outcome") or "?"
    phase = run.get("phase") or "crawl"
    return f"{outcome}/{phase}"


def _print_fleet_table(body: Dict[str, Any], *, show_all: bool) -> None:
    rows = body.get("connections") or []
    totals = body.get("totals") or {}
    scope = "all connections" if show_all else "active"
    table = Table(title=f"SharePoint extraction fleet ({len(rows)} {scope}, as of {body.get('as_of', '')})")
    table.add_column("CONNECTION", style="bold")
    table.add_column("PHASE")
    table.add_column("FILES DONE/SEEN", justify="right")
    table.add_column("FILES/MIN", justify="right")
    table.add_column("FACTS DONE/PENDING")
    table.add_column("TOKENS IN/OUT", justify="right")
    table.add_column("EST. COST", justify="right")
    table.add_column("LAST CHECKPOINT")
    table.add_column("ERROR")

    for row in rows:
        run = row.get("run")
        files_done = run.get("files_done") if run else None
        files_seen = run.get("files_seen") if run else None
        files_cell = "—" if files_done is None else f"{files_done:,} / {files_seen:,}"
        phase = _fmt_phase(run)
        if row.get("stuck"):
            phase = f"[bold red]{phase} STUCK?[/bold red]"
        error = (run or {}).get("error") or ""
        table.add_row(
            str(row.get("connection_name") or row.get("connection_id")),
            phase,
            files_cell,
            _fmt_rate(row.get("files_per_min")),
            _fmt_facts(row.get("facts")),
            _fmt_tokens((run or {}).get("usage") or {}),
            _fmt_cost(row.get("estimated_cost_usd")),
            _fmt_ago(row.get("checkpoint_age_s")),
            error[:60],
        )
    _console.print(table)
    _console.print(
        f"Totals — connections: {totals.get('connections', 0)}, active: {totals.get('active', 0)}, "
        f"stuck: {totals.get('stuck', 0)}, files/min: {_fmt_rate(totals.get('files_per_min'))}, "
        f"facts done: {totals.get('facts_docs_done', 0):,}, est. cost: {_fmt_cost(totals.get('estimated_cost_usd'))}"
    )


@admin_sharepoint_app.command("runs")
def runs(
    show_all: bool = typer.Option(
        False,
        "--all",
        help="Every SharePoint connection, running or not (default: only connections with a run active right now)",
    ),
    as_json: bool = typer.Option(False, "--json"),
    watch: bool = typer.Option(False, "--watch", help="Refresh every 10s until interrupted (Ctrl-C)"),
):
    """The extraction fleet dashboard, from the terminal: is it on pace, is
    anything stuck, what is it costing — one row per SharePoint connection.

    CLI counterpart to ``GET /api/admin/sharepoint/extraction/runs``, the
    same endpoint the ``/admin/extraction`` web dashboard polls. Default
    scope is connections with a run CURRENTLY active; ``--all`` broadens to
    every SharePoint connection, idle ones included. PG-only: on a
    DuckDB-backed instance this refuses with the typed
    ``501 requires_postgres_backend`` the API itself returns.
    """

    def _fetch() -> Dict[str, Any]:
        qs = "?all=1" if show_all else "?active=1"
        resp = api_get(f"/api/admin/sharepoint/extraction/runs{qs}")
        if resp.status_code != 200:
            _fail(resp)
        return resp.json()

    if not watch:
        body = _fetch()
        if as_json:
            typer.echo(json.dumps(body, indent=2))
        else:
            _print_fleet_table(body, show_all=show_all)
        return

    try:
        while True:
            body = _fetch()
            if as_json:
                typer.echo(json.dumps(body, indent=2))
            else:
                _console.clear()
                _print_fleet_table(body, show_all=show_all)
            time.sleep(10)
    except KeyboardInterrupt:
        raise typer.Exit(0) from None
