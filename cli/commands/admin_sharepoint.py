"""`agnes admin sharepoint` — admin/ops triggers for SharePoint connector
maintenance jobs.

The crawl / ACL-sync / subtree-sweep triggers stay admin-web-UI-only, an
established precedent (see CONTRIBUTING.md's "admin/scheduler maintenance
op" exemption class, `tests/test_documentation_api_triple_surface.py`).
Two surfaces earn a CLI counterpart on top of that default:

* `facts-extract` — an operator asking "how do we get the fact graph
  populated with what we already have?" needs an answer that does not
  require opening a browser (a support runbook, a script run against a
  remote instance). CLI counterpart to
  ``POST /api/admin/sharepoint/connections/{connection_id}/facts-extract``.
* `facts-config` — a per-connection retry-policy override (cost-levers
  task, lever A): one high-value connection keeps the corrective retry ON
  while a long-tail connection runs with it OFF, set without an
  instance.yaml edit. CLI counterpart to
  ``PATCH /api/admin/sharepoint/connections/{connection_id}/extraction/
  facts-config``.
"""

from __future__ import annotations

import json
from typing import List, Optional

import typer

from cli.client import api_patch, api_post

admin_sharepoint_app = typer.Typer(help="Admin: SharePoint connector maintenance triggers")

_RETRY_MODES = ("off", "on_gate_fail", "always")


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


@admin_sharepoint_app.command("facts-config")
def facts_config(
    connection_id: str = typer.Argument(..., help="SharePoint source_connections id"),
    retry_mode: Optional[str] = typer.Option(
        None,
        "--retry-mode",
        help=f"Per-connection override for the corrective-retry policy: one of {', '.join(_RETRY_MODES)}. "
        "off = never retry a failing quote (cheapest, lowest recall); on_gate_fail = retry only when the "
        "verbatim gate still rejects part of the output (the instance default); always = retry even a "
        "document the deterministic repair already fixed, for maximum recall at maximum cost.",
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Remove the override — this connection falls back to the instance-level default."
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Set (or clear) this connection's own ``extraction.facts.retry_mode``,
    overriding the instance-level default (cost-levers task, lever A) — a
    curated, high-stakes connection can keep the corrective retry ON (a
    dropped quote there is a lost citation) while a long-tail connection
    runs with it OFF, without an instance.yaml edit that would flip every
    connection at once.

    Exactly one of ``--retry-mode`` / ``--clear`` is required. Prints the
    RESOLVED value and where it came from (``connection`` or ``instance``)
    — the same shape the admin config drawer would show.
    """
    if clear and retry_mode is not None:
        typer.echo("Error: pass either --retry-mode or --clear, not both", err=True)
        raise typer.Exit(1)
    if not clear and retry_mode is None:
        typer.echo("Error: one of --retry-mode or --clear is required", err=True)
        raise typer.Exit(1)
    if retry_mode is not None and retry_mode not in _RETRY_MODES:
        typer.echo(f"Error: --retry-mode must be one of {', '.join(_RETRY_MODES)}", err=True)
        raise typer.Exit(1)

    resp = api_patch(
        f"/api/admin/sharepoint/connections/{connection_id}/extraction/facts-config",
        json={"retry_mode": retry_mode},
    )
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    resolved = body.get("retry_mode") or {}
    typer.echo(f"retry_mode: {resolved.get('value')} (source: {resolved.get('source')})")
