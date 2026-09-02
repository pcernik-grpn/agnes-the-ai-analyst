"""`agnes admin sharepoint` — admin/ops triggers for SharePoint connector
maintenance jobs.

Two surfaces: the manual crawl trigger with its per-run options
(``extract``) and the standalone fact-graph trigger (``facts-extract``).
The ACL-sync / subtree-sweep triggers stay admin-web-UI-only, an
established precedent (see CONTRIBUTING.md's "admin/scheduler maintenance
op" exemption class, `tests/test_documentation_api_triple_surface.py`) —
these two earn a CLI counterpart because an operator asking "how do we
re-read everything in this scope?" or "how do we get the fact graph
populated with what we already have?" needs an answer that does not require
opening a browser (a support runbook, a script run against a remote
instance). Both are deliberately NOT MCP-exposed — an agent-invokable tool
that can kick off a full re-crawl or an LLM pass over an entire corpus is
a cost surface no analyst query needs.

CLI counterparts to
``POST /api/admin/sharepoint/connections/{connection_id}/extract`` and
``POST /api/admin/sharepoint/connections/{connection_id}/facts-extract``.
"""

from __future__ import annotations

import json
from typing import List, Optional

import typer

from cli.client import api_post

admin_sharepoint_app = typer.Typer(help="Admin: SharePoint connector maintenance triggers")


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


@admin_sharepoint_app.command("extract")
def extract(
    connection_id: str = typer.Argument(..., help="SharePoint source_connections id"),
    concurrency: Optional[int] = typer.Option(
        None,
        "--concurrency",
        min=1,
        max=16,
        help="Files of one delta page pipelined at once for this run (1 = sequential). "
        "Default: the configured extraction.crawler.concurrency.",
    ),
    timeout_s: Optional[int] = typer.Option(
        None,
        "--timeout-s",
        min=0,
        max=86400,
        help="Hard ceiling for this one run, seconds (0 = unbounded). Default: the configured extraction.timeout_s.",
    ),
    resync: bool = typer.Option(
        False,
        "--resync",
        help="Drop the persisted deltaLinks and item-failure queue first, so every drive "
        "re-enumerates from scratch. Already-ingested files are NOT re-downloaded (cTags "
        "are kept) — the recovery path for a connection whose change cursor ran past "
        "documents it never ingested.",
    ),
    force_reprocess: bool = typer.Option(
        False,
        "--force-reprocess",
        help="Re-read every file in scope, ignoring the change cursor AND the per-file cTags "
        "for this run only: every document is re-downloaded, re-converted and re-ingested "
        "(and re-extracted, when fact extraction is on). Costs a full crawl. Nothing is "
        "written to the crawl state up front, so an interrupted run resumes as before.",
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Run the built-in crawl for this connection now.

    Enqueues the ``corpus-extraction`` job — the same job the source card's
    "Run extraction now" button triggers — with the SAME per-run options
    that card offers. Every option is for this run only; nothing here
    changes a configured value.

    Refuses with a clear reason rather than a bare HTTP error: ``409
    extraction_disabled`` (``sharepoint.enabled`` is off), ``409
    extraction_dependencies_missing`` (the ``extraction`` extra is not
    installed), ``409 extraction_already_running`` (a run is already
    queued/running for this connection), ``404`` (unknown or non-SharePoint
    connection id).
    """
    payload: dict = {}
    if concurrency is not None:
        payload["concurrency"] = concurrency
    if timeout_s is not None:
        payload["timeout_s"] = timeout_s
    if resync:
        payload["resync"] = True
    if force_reprocess:
        payload["force_reprocess"] = True

    resp = api_post(
        f"/api/admin/sharepoint/connections/{connection_id}/extract",
        json=payload or None,
    )
    if resp.status_code != 202:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Enqueued corpus-extraction job {body.get('job_id')} (status: {body.get('status')})")


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
