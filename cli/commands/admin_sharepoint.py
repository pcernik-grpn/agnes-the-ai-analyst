"""`agnes admin sharepoint` — admin/ops triggers and status for SharePoint
connector maintenance, plus the split-a-large-site management pair below.

Seven surfaces:

  - ``extract`` — the manual crawl trigger with its per-run options
    (``--concurrency``, ``--timeout-s``, ``--resync``, ``--force-reprocess``,
    ``--retry-failed``); CLI counterpart to ``POST /api/admin/sharepoint/
    connections/{connection_id}/extract`` — the same job the source card's
    "Run extraction now" button enqueues.
  - ``facts-extract`` — the standalone fact-graph trigger.
  - ``scope bulk-add`` / ``connection clone`` — the CLI counterparts to
    ``POST /api/admin/sharepoint/connections/{connection_id}/scopes/bulk``
    and ``POST /api/admin/sharepoint/connections/{connection_id}/clone`` —
    the fast path for splitting one large SharePoint site across several
    connections, each with its own crawl and facts jobs, so they run in
    parallel: clone the source connection (same credential material, zero
    scopes), then bulk-add the split's folder paths onto each clone.
    ``scope bulk-add`` also takes ``--collection-id``/``--collection-name``
    to route every path it confirms to ONE shared collection instead of
    minting one per path.
  - ``collections consolidate`` — the after-the-fact fix when a site
    ALREADY ended up split across many per-scope collections: fold them
    into one target (dry-run preview by default, ``--execute`` for the
    real merge). CLI counterpart to ``POST /api/admin/sharepoint/
    connections/{connection_id}/collections/consolidate``.
  - ``runs`` — the extraction fleet dashboard (2026-09-02), from the
    terminal: is it on pace, is anything stuck, what is it costing, across
    every SharePoint connection at once. CLI counterpart to
    ``GET /api/admin/sharepoint/extraction/runs`` — the same endpoint
    ``/admin/extraction`` polls.
  - ``facts-config`` — a per-connection retry-policy/transport/provider
    override (cost-levers task, lever A): one high-value connection keeps
    the corrective retry ON while a long-tail connection runs with it OFF,
    or a connection is pinned to a specific LLM provider (e.g. off a
    workspace that hit its usage cap), set without an instance.yaml edit.
    CLI counterpart to
    ``PATCH /api/admin/sharepoint/connections/{connection_id}/extraction/
    facts-config``.
  - ``crawl-config`` — a per-connection age filter: a backfill run can crawl
    only what changed on/after a cutoff date instead of re-walking a whole
    multi-year corpus. CLI counterpart to
    ``PATCH /api/admin/sharepoint/connections/{connection_id}/extraction/
    crawl-config``.

The ACL-sync / subtree-sweep TRIGGERS stay admin-web-UI-only, an
established precedent (see CONTRIBUTING.md's "admin/scheduler maintenance
op" exemption class, `tests/test_documentation_api_triple_surface.py`) —
``extract`` and ``facts-extract`` earn a CLI counterpart because an operator
asking "how do we re-read everything in this scope?" or "how do we get the
fact graph populated with what we already have?" needs an answer that does
not require opening a browser (a support runbook, a script run against a
remote instance); both are deliberately NOT MCP-exposed — an agent-invokable
tool that can kick off a full re-crawl or an LLM pass over an entire corpus
is a cost surface no analyst query needs. ``runs`` earns one for the same
reason a monitor does: an operator watching a ~20-hour extraction over SSH
has no browser open at all.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from rich.console import Console
from rich.table import Table

from cli.client import api_get, api_patch, api_post

admin_sharepoint_app = typer.Typer(help="Admin: SharePoint connector maintenance triggers")
scope_app = typer.Typer(help="SharePoint connect wizard scope management")
connection_app = typer.Typer(help="SharePoint connection management")
collections_app = typer.Typer(help="SharePoint per-scope collection management")
admin_sharepoint_app.add_typer(scope_app, name="scope")
admin_sharepoint_app.add_typer(connection_app, name="connection")
admin_sharepoint_app.add_typer(collections_app, name="collections")

# `runs` renders a nine-column table (connection through error). A default,
# terminal-detected width truncates every cell to a few characters when
# stdout isn't a real tty (piped output, the test runner) — a fixed, wide
# console keeps the fleet table readable regardless of where it's printed.
_console = Console(width=200)

_RETRY_MODES = ("off", "on_gate_fail", "always")
_PROVIDERS = ("inherit", "anthropic", "vertex")


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
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="Give every item this connection's own failure queue already knows about one "
        "more chance — including ones already given up on after repeated failures — "
        "without a full --resync. The targeted recovery for a handful of permanently-stuck "
        "documents (a conversion crash, a transient download error). This run's ordinary "
        "incremental delta walk still runs afterward, unaffected.",
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
    if retry_failed:
        payload["retry_failed"] = True

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
    collection_id: Optional[str] = typer.Option(
        None,
        "--collection-id",
        help=(
            "Route every scope THIS call creates to an EXISTING, live collection instead of "
            "minting one per path — mutually exclusive with --collection-name."
        ),
    ),
    collection_name: Optional[str] = typer.Option(
        None,
        "--collection-name",
        help=(
            "Mint ONE new collection with this name and route every scope THIS call creates to "
            "it — mutually exclusive with --collection-id."
        ),
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

    A split site otherwise forks across as many collections as there are
    confirmed scopes — one bulk-add call per connection, one collection per
    path. ``--collection-id``/``--collection-name`` route every scope THIS
    call creates to ONE shared collection instead; the SAME
    ``--collection-id`` across several bulk-add calls (one per connection
    in the split) grows one collection for the whole site.
    """
    if collection_id and collection_name:
        typer.echo("Error: --collection-id and --collection-name are mutually exclusive", err=True)
        raise typer.Exit(1)

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
    if collection_id:
        body["collection_id"] = collection_id
    if collection_name:
        body["collection"] = {"name": collection_name}

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
    material as ``connection_id`` (tenant/client identity, and the
    certificate/client-secret — a vault-stored one is copied verbatim,
    never decrypted; an env-var-sourced one resolves on its own, nothing to
    copy), with zero scopes. Pair with ``agnes admin sharepoint scope
    bulk-add`` to populate the clone with its own slice of the split.

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
    if body.get("secret_copied"):
        typer.echo("Vault secret copied — the clone is ready to crawl.")
    else:
        typer.echo("No vault secret to copy (source resolves its certificate from an env var).")


@collections_app.command("consolidate")
def collections_consolidate(
    connection_id: str = typer.Argument(..., help="SharePoint source_connections id"),
    target_collection_id: Optional[str] = typer.Option(
        None,
        "--target-collection-id",
        help="Fold into this EXISTING, live collection — mutually exclusive with --target-name",
    ),
    target_name: Optional[str] = typer.Option(
        None,
        "--target-name",
        help="Mint ONE new collection with this name as the fold target — mutually exclusive "
        "with --target-collection-id",
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually perform the merge. Without this flag the call is a dry-run preview only.",
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Fold this connection's own per-scope collections into ONE target —
    the after-the-fact fix for a large site split across many bulk-added
    scopes that ended up one collection per scope (CLI counterpart to
    ``POST /api/admin/sharepoint/connections/{connection_id}/collections/
    consolidate``).

    Defaults to a DRY RUN: lists the collections that would be folded and
    their file counts, without touching anything. Pass ``--execute`` to
    perform the real merge (files/chunks/claims re-pointed, resource grants
    unioned, source collections soft-deleted).

    ``409`` if a source collection is still routed to by a DIFFERENT
    connection's own scope, or if the merge would collide on a duplicate
    file path / source-document id across the collections being folded.
    """
    if bool(target_collection_id) == bool(target_name):
        typer.echo("Error: pass exactly one of --target-collection-id or --target-name", err=True)
        raise typer.Exit(1)

    body: dict = {"dry_run": not execute}
    if target_collection_id:
        body["target_collection_id"] = target_collection_id
    else:
        body["target"] = {"name": target_name}

    resp = api_post(
        f"/api/admin/sharepoint/connections/{connection_id}/collections/consolidate",
        json=body,
    )
    if resp.status_code != 200:
        _fail(resp)
    result = resp.json()
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return

    target = result["target"]
    sources = result["sources"]
    if result["dry_run"]:
        typer.echo(f"[dry run] would fold {len(sources)} collection(s) into '{target['name']}' ({target['id']}):")
        for s in sources:
            typer.echo(f"  {s['name']} ({s['id']}) — {s['file_count']} file(s)")
        for b in result.get("blocking") or []:
            typer.echo(
                f"  BLOCKED: {b['collection_id']} is still referenced by connection {b['connection_id']} "
                "— consolidating would refuse with 409",
                err=True,
            )
    else:
        typer.echo(f"Folded {len(sources)} collection(s) into '{target['name']}' ({target['id']}):")
        typer.echo(
            f"  files={result['files_moved']} chunks={result['chunks_moved']} "
            f"sources={result['sources_moved']} events={result['events_moved']} "
            f"claims={result['claims_moved']} grants={result['grants_merged']} "
            f"scopes_repointed={result['scopes_repointed']}"
        )


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
    transport: Optional[str] = typer.Option(
        None,
        "--transport",
        help="Per-connection override for which API carries the extraction calls: sync (immediate, bound by the "
        "account's per-minute token limit) or batch (Anthropic Batches API: no per-minute ceiling, half the price, "
        "hours of latency). Left untouched when not given.",
    ),
    clear_transport: bool = typer.Option(
        False, "--clear-transport", help="Remove the transport override — falls back to extraction.facts.transport."
    ),
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        help=f"Per-connection override for which LLM provider carries this stage's calls: one of "
        f"{', '.join(_PROVIDERS)}. inherit (the instance default) follows this instance's ai.provider; "
        "anthropic/vertex pin this stage regardless of it. The Anthropic Batches API has no Vertex "
        "equivalent — a connection resolved to vertex always runs the sync transport. Left untouched "
        "when not given.",
    ),
    clear_provider: bool = typer.Option(
        False, "--clear-provider", help="Remove the provider override — falls back to extraction.facts.provider."
    ),
    vertex_region: Optional[str] = typer.Option(
        None,
        "--vertex-region",
        help="Per-connection override for WHICH Vertex AI region a provider=vertex pass's client talks to, on "
        "top of this instance's own ai.vertex.region. Google enforces Claude-on-Vertex quotas per region, so "
        "pinning different connections to different regions raises the account's effective throughput. "
        "Lowercase letters, digits and dash ('global' allowed). Only meaningful when the resolved provider "
        "is vertex. Left untouched when not given.",
    ),
    clear_vertex_region: bool = typer.Option(
        False,
        "--clear-vertex-region",
        help="Remove the vertex_region override — falls back to extraction.facts.vertex_region, then this "
        "instance's ai.vertex.region.",
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Set (or clear) this connection's own ``extraction.facts.retry_mode``,
    ``extraction.facts.transport``, ``extraction.facts.provider`` and/or
    ``extraction.facts.vertex_region``, overriding the instance-level
    defaults (cost-levers task, lever A) — a curated, high-stakes connection
    can keep the corrective retry ON and run sync (a dropped quote there is
    a lost citation, and its facts should land in minutes) while a
    long-tail connection runs with retries OFF on the Batches API, without
    an instance.yaml edit that would flip every connection at once.
    ``--provider`` is the same lever for the incident this knob exists to
    fix: a connection whose Anthropic key has hit its workspace usage cap
    can be pinned to ``vertex`` without waiting for the instance-wide
    ``ai.provider`` to change. ``--vertex-region`` is a further lever on
    top of that: Vertex enforces its Claude quotas PER REGION, so spreading
    several connections' passes across regions multiplies the account's
    effective throughput at the same per-call price.

    ``--retry-mode`` / ``--clear`` are mutually exclusive and one is
    required unless ``--transport`` / ``--clear-transport`` / ``--provider``
    / ``--clear-provider`` / ``--vertex-region`` / ``--clear-vertex-region``
    is given. Prints the RESOLVED values and where they came from
    (``connection`` or ``instance``) — the same shape the admin config
    drawer would show.
    """
    if clear and retry_mode is not None:
        typer.echo("Error: pass either --retry-mode or --clear, not both", err=True)
        raise typer.Exit(1)
    if clear_transport and transport is not None:
        typer.echo("Error: pass either --transport or --clear-transport, not both", err=True)
        raise typer.Exit(1)
    if clear_provider and provider is not None:
        typer.echo("Error: pass either --provider or --clear-provider, not both", err=True)
        raise typer.Exit(1)
    if clear_vertex_region and vertex_region is not None:
        typer.echo("Error: pass either --vertex-region or --clear-vertex-region, not both", err=True)
        raise typer.Exit(1)
    touching_transport = clear_transport or transport is not None
    touching_provider = clear_provider or provider is not None
    touching_vertex_region = clear_vertex_region or vertex_region is not None
    if (
        not clear
        and retry_mode is None
        and not touching_transport
        and not touching_provider
        and not touching_vertex_region
    ):
        typer.echo(
            "Error: one of --retry-mode or --clear is required "
            "(or --transport / --clear-transport / --provider / --clear-provider / "
            "--vertex-region / --clear-vertex-region)",
            err=True,
        )
        raise typer.Exit(1)
    if retry_mode is not None and retry_mode not in _RETRY_MODES:
        typer.echo(f"Error: --retry-mode must be one of {', '.join(_RETRY_MODES)}", err=True)
        raise typer.Exit(1)
    if transport is not None and transport not in ("sync", "batch"):
        typer.echo("Error: --transport must be sync or batch", err=True)
        raise typer.Exit(1)
    if provider is not None and provider not in _PROVIDERS:
        typer.echo(f"Error: --provider must be one of {', '.join(_PROVIDERS)}", err=True)
        raise typer.Exit(1)
    if vertex_region is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", vertex_region.strip().lower()):
        typer.echo("Error: --vertex-region must be lowercase letters, digits and dash ('global' allowed)", err=True)
        raise typer.Exit(1)

    # The retry policy keeps its original contract (omitted == cleared), so a
    # transport/provider/vertex-region-only call re-sends the connection's
    # current retry override rather than wiping it.
    payload: dict = {"retry_mode": retry_mode}
    if not clear and retry_mode is None and (touching_transport or touching_provider or touching_vertex_region):
        current = api_get(f"/api/admin/sharepoint/connections/{connection_id}")
        if current.status_code == 200:
            facts_now = (((current.json().get("config") or {}).get("extraction") or {}).get("facts")) or {}
            payload["retry_mode"] = facts_now.get("retry_mode")
    if touching_transport:
        payload["transport"] = None if clear_transport else transport
    if touching_provider:
        payload["provider"] = None if clear_provider else provider
    if touching_vertex_region:
        payload["vertex_region"] = None if clear_vertex_region else vertex_region

    resp = api_patch(
        f"/api/admin/sharepoint/connections/{connection_id}/extraction/facts-config",
        json=payload,
    )
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    resolved = body.get("retry_mode") or {}
    typer.echo(f"retry_mode: {resolved.get('value')} (source: {resolved.get('source')})")
    resolved_t = body.get("transport") or {}
    if resolved_t:
        typer.echo(f"transport: {resolved_t.get('value')} (source: {resolved_t.get('source')})")
    resolved_p = body.get("provider") or {}
    if resolved_p:
        typer.echo(
            f"provider: {resolved_p.get('value')} (source: {resolved_p.get('source')}) "
            f"— effective: {resolved_p.get('effective')}"
        )
    resolved_vr = body.get("vertex_region") or {}
    if resolved_vr:
        typer.echo(f"vertex_region: {resolved_vr.get('value')} (source: {resolved_vr.get('source')})")


@admin_sharepoint_app.command("crawl-config")
def crawl_config(
    connection_id: str = typer.Argument(..., help="SharePoint source_connections id"),
    min_modified: Optional[str] = typer.Option(
        None,
        "--min-modified",
        help="Crawl only files modified on/after this UTC date (YYYY-MM-DD) — e.g. a backfill that only "
        "needs everything changed since a given cutoff. Items modified before it are skipped and counted; "
        "an item with no modified timestamp is always kept.",
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove the override — the connection crawls unfiltered."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Set (or clear) this connection's own ``extraction.crawl.min_modified``
    age filter — a 190k-document connection can crawl only what changed
    since a cutoff date instead of re-walking the whole corpus.

    Exactly one of ``--min-modified`` / ``--clear`` is required. Prints the
    RESOLVED value and where it came from (``connection`` or ``none``) — the
    same shape the admin config drawer would show.
    """
    if clear and min_modified is not None:
        typer.echo("Error: pass either --min-modified or --clear, not both", err=True)
        raise typer.Exit(1)
    if not clear and min_modified is None:
        typer.echo("Error: one of --min-modified or --clear is required", err=True)
        raise typer.Exit(1)
    if min_modified is not None:
        try:
            date.fromisoformat(min_modified)
        except ValueError:
            typer.echo(f"Error: --min-modified must be an ISO YYYY-MM-DD date, got {min_modified!r}", err=True)
            raise typer.Exit(1) from None

    resp = api_patch(
        f"/api/admin/sharepoint/connections/{connection_id}/extraction/crawl-config",
        json={"min_modified": min_modified},
    )
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    resolved = body.get("min_modified") or {}
    typer.echo(f"min_modified: {resolved.get('value')} (source: {resolved.get('source')})")
