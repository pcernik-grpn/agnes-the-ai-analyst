"""`agnes admin sharepoint` — admin/ops triggers for SharePoint connector
maintenance jobs, plus the split-a-large-site management pair below.

The standalone fact-graph trigger, and the crawl / ACL-sync / subtree-sweep
triggers that stay admin-web-UI-only (see CONTRIBUTING.md's "admin/
scheduler maintenance op" exemption class,
`tests/test_documentation_api_triple_surface.py`), earned a CLI counterpart
because an operator asking "how do we get the fact graph populated with
what we already have?" needs an answer that does not require opening a
browser (a support runbook, a script run against a remote instance).

``scope bulk-add`` / ``connection clone`` are the CLI counterparts to
``POST /api/admin/sharepoint/connections/{connection_id}/scopes/bulk`` and
``POST /api/admin/sharepoint/connections/{connection_id}/clone`` — the fast
path for splitting one large SharePoint site across several connections,
each with its own crawl and facts jobs, so they run in parallel: clone the
source connection (same credential material, zero scopes), then bulk-add
the split's folder paths onto each clone.

CLI counterpart to
``POST /api/admin/sharepoint/connections/{connection_id}/facts-extract``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer

from cli.client import api_post

admin_sharepoint_app = typer.Typer(help="Admin: SharePoint connector maintenance triggers")
scope_app = typer.Typer(help="SharePoint connect wizard scope management")
connection_app = typer.Typer(help="SharePoint connection management")
admin_sharepoint_app.add_typer(scope_app, name="scope")
admin_sharepoint_app.add_typer(connection_app, name="connection")


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


@scope_app.command("bulk-add")
def scope_bulk_add(
    connection_id: str = typer.Argument(..., help="SharePoint source_connections id"),
    paths_file: Optional[Path] = typer.Option(
        None,
        "--paths-file",
        help='JSON file: a list of folder paths, or {"paths": [...]}. Combined with any --path.',
    ),
    path: List[str] = typer.Option(
        [], "--path", help="One folder path to add, admin-typed (e.g. 'Folder A/Sub') — repeatable"
    ),
    drive_id: Optional[str] = typer.Option(
        None,
        "--drive-id",
        help="Graph drive id — required unless this connection already has a scope with one set",
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Confirm many folder paths as scopes in one call — the fast path for
    splitting one large SharePoint site across several connections, each
    with its own crawl and facts jobs, so they run in parallel (pair with
    ``agnes admin sharepoint connection clone`` above).

    Never all-or-nothing: every path is resolved and reported independently
    — created, skipped (a path already present on this connection), or
    failed (Graph could not resolve it: not found / forbidden). Exits 0 as
    long as the call itself succeeded, even when some paths failed —
    inspect the per-path breakdown (``--json`` for the full detail) rather
    than the exit code.
    """
    paths: List[str] = list(path)
    if paths_file is not None:
        try:
            raw = json.loads(paths_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            typer.echo(f"Error: could not read {paths_file}: {exc}", err=True)
            raise typer.Exit(1) from exc
        if isinstance(raw, dict):
            raw = raw.get("paths")
        if not isinstance(raw, list):
            typer.echo(f'Error: {paths_file} must be a JSON list of paths, or {{"paths": [...]}}', err=True)
            raise typer.Exit(1)
        paths.extend(str(p) for p in raw)

    if not paths:
        typer.echo("Error: no paths given — pass --path (repeatable) and/or --paths-file", err=True)
        raise typer.Exit(1)

    body: dict = {"paths": paths}
    if drive_id:
        body["drive_id"] = drive_id

    resp = api_post(f"/api/admin/sharepoint/connections/{connection_id}/scopes/bulk", json=body)
    if resp.status_code != 200:
        _fail(resp)
    result = resp.json()
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(f"Created {len(result['created'])}, skipped {len(result['skipped'])}, failed {len(result['failed'])}")
    for entry in result["failed"]:
        typer.echo(f"  failed: {entry['path']} ({entry['reason']})")
    for entry in result["skipped"]:
        typer.echo(f"  skipped: {entry['path']} (already present as {entry['source_scope_id']})")


@connection_app.command("clone")
def connection_clone(
    connection_id: str = typer.Argument(..., help="SharePoint source_connections id to clone"),
    name: str = typer.Option(..., "--name", help="Name for the new sibling connection"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Create a sibling SharePoint connection wired to the SAME credential
    material as ``connection_id`` (tenant/client identity, certificate/
    client-secret env-var reference — never a copied secret value), with
    zero scopes. Pair with ``agnes admin sharepoint scope bulk-add`` to
    populate the clone with its own slice of the split.

    ``409 connection_name_exists`` if ``--name`` is already taken; ``404``
    for an unknown or non-SharePoint connection id.
    """
    resp = api_post(f"/api/admin/sharepoint/connections/{connection_id}/clone", json={"name": name})
    if resp.status_code != 201:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Cloned {connection_id} -> {body.get('id')} ({body.get('name')})")
