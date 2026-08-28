"""`agnes admin semantic` — everything about the semantic layer that needs an
admin, in one group.

Block 6 of #1707. Three groups used to divide this surface by which endpoint
family they happened to call, not by anything a reader could predict:

  * ``agnes admin semantic-model``  — document CRUD
  * ``agnes admin semantic-source`` — where documents are synced from
  * ``agnes admin semantic-layer``  — the Keboola import-health report

All three read as "the semantic layer", so choosing between them was a memory
test. They are now one group, split by NOUN:

  * ``list`` / ``show`` / ``import`` / ``delete`` / ``detach`` / ``reattach`` /
    ``link-package`` / ``unlink-package``  — the documents
  * ``source add|list|sync|rm``             — where documents come from
  * ``coverage`` / ``coverage tables`` / ``keboola-import`` / ``health`` /
    ``mute`` / ``mutes`` / ``unmute``       — is the layer any good

``keboola-import`` is the old ``admin semantic-layer coverage`` under an
honest name: it predicts what a *connected Keboola project's* semantic layer
would import, which is a different question from either coverage report here.

The non-admin half lives in :mod:`cli.commands.semantic_model` (``agnes
semantic-model``): search, show, export, validate, apply, context, schema,
validate-query, feedback. ``export`` and ``validate`` moved there on purpose —
reading a document you already have access to, and schema-checking a local
file with no server at all, are not admin operations.

The old spellings survive one release as hidden aliases; see
:mod:`cli.commands.admin_semantic_model`, :mod:`cli.commands.
admin_semantic_source` and :mod:`cli.commands.admin_semantic_layer`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_post

admin_semantic_app = typer.Typer(help="Admin: the semantic layer — documents, their sources, and its health")

# ---------------------------------------------------------------------------
# Endpoints, named once
# ---------------------------------------------------------------------------

_MODELS_PATH = "/api/admin/semantic-models"
_SOURCES_PATH = "/api/admin/semantic-sources"
_COVERAGE_PATH = "/api/admin/semantic-model/coverage"
_COVERAGE_TAGS_PATH = "/api/admin/semantic-model/coverage/tags"
_TABLE_COVERAGE_PATH = "/api/admin/semantic-coverage"
_KEBOOLA_IMPORT_PATH = "/api/admin/semantic-layer/coverage"
_HEALTH_PATH = "/api/admin/semantic-layer/health"
_MUTES_PATH = "/api/admin/semantic-layer/mutes"

_COVERAGE_DOMAINS = ("semantic", "metrics", "glossary", "skill", "agent", "knowledge_base")
_COVERAGE_TAGGABLE = ("marketplace_plugin", "agent", "memory_domain")
_STATUS_GLYPHS = {"ok": "ok", "partial": "partial", "missing": "MISSING", "not_applicable": "n/a"}
_MUTE_SCOPE_FORMS = ("source:<source-id>", "domain:<domain>", "source:<source-id>:domain:<domain>")

# Keboola import skip reasons, in the wording an operator can act on.
_REASON_LABELS = {
    "missing_name": "no name upstream",
    "embedded_sql_comment": "SQL comment in the expression swallows the FROM clause",
    "foreign_alias_reference": "references another dataset via an alias (needs a relationship)",
    "ambiguous_relationship": "more than one relationship touches this dataset",
    "unsupported_relationship_type": "relationship type is not supported",
    "unverified_relationship_direction": "dataset sits on the unverified side of the relationship",
}


# ---------------------------------------------------------------------------
# Error rendering
# ---------------------------------------------------------------------------


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    msg = (
        detail
        if isinstance(detail, str)
        else (json.dumps(detail) if detail is not None else (resp.text or f"HTTP {resp.status_code}"))
    )
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


def _detail(resp) -> dict:
    """The typed ``detail`` object, or ``{}`` — never an exception.

    A non-JSON error body (a proxy's HTML 502, say) must not turn a refusal
    into a traceback: the caller still needs to print SOMETHING actionable.
    """
    try:
        detail = resp.json().get("detail")
    except Exception:
        return {}
    return detail if isinstance(detail, dict) else {}


def _not_found(ref: str) -> None:
    typer.echo(f"Semantic model not found: {ref}", err=True)
    typer.echo("Try: agnes admin semantic list", err=True)
    raise typer.Exit(1)


def _source_not_found(source_id: str) -> None:
    typer.echo(f"Semantic source not found: {source_id}", err=True)
    typer.echo("Try: agnes admin semantic source list", err=True)
    raise typer.Exit(1)


def _fail_needs_postgres(resp, what: str) -> None:
    """Turn the PG-only 501 into the one sentence that names the fix.

    Coverage, health, mutes and the feedback queue live in Postgres-only
    tables (A3 PG-first ratchet). Printing the raw 501 body would leave the
    reader guessing that their request was malformed.
    """
    if resp.status_code != 501:
        return
    typer.echo(
        f"{what} needs the Postgres app-state backend — this instance still runs the frozen "
        "DuckDB backend. Migrate it (see docs/migrations.md) to use this command.",
        err=True,
    )
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# The documents — `agnes admin semantic list|show|import|delete|…`
# ---------------------------------------------------------------------------


@admin_semantic_app.command("list")
def list_models(
    term: Optional[str] = typer.Argument(None, help="Filter by substring in slug/name/description"),
    limit: int = typer.Option(50, "--limit", help="Max rows to show"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Every stored semantic model, at any status (admin only).

    Reads `GET /api/admin/semantic-models`, which is why it sees drafts and
    invalid documents. The non-admin listing is `agnes semantic-model search
    <term>`, which reads the public, resource-gated search endpoint and shows
    only what the caller may actually read.
    """
    resp = api_get(_MODELS_PATH)
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if term:
        needle = term.lower()
        rows = [
            r
            for r in rows
            if needle in " ".join(filter(None, [r.get("slug"), r.get("name"), r.get("description")])).lower()
        ]
    total = len(rows)
    rows = rows[:limit]

    if as_json:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return

    typer.echo(f"Semantic models: {len(rows)}")
    for r in rows:
        typer.echo(f"{r.get('id', ''):<28}  {r.get('slug', ''):<20}  {r.get('source', ''):<10}  {r.get('status', '')}")
    if total > len(rows):
        typer.echo(f"… {total - len(rows)} more — raise --limit or narrow with a search term")


@admin_semantic_app.command("show")
def show_model(
    model_ref: str = typer.Argument(..., help="Model id or slug"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """One model's stored metadata, at any status (admin only).

    Not the document body — `agnes semantic-model export <slug>` is that.
    The non-admin counterpart is `agnes semantic-model show <slug>`, which
    resolves through the public search endpoint and therefore only sees
    models the caller may read.
    """
    resp = api_get(f"{_MODELS_PATH}/{model_ref}")
    if resp.status_code == 404:
        _not_found(model_ref)
    if resp.status_code != 200:
        _fail(resp)
    row = resp.json()
    if as_json:
        typer.echo(json.dumps(row, indent=2, default=str))
        return
    typer.echo(f"ID:          {row.get('id')}")
    typer.echo(f"Slug:        {row.get('slug')}")
    typer.echo(f"Name:        {row.get('name')}")
    typer.echo(f"Source:      {row.get('source')} (source_ref={row.get('source_ref')})")
    typer.echo(f"Status:      {row.get('status')}")
    typer.echo(f"Spec version: {row.get('spec_version')}")
    if row.get("content_hash"):
        typer.echo(f"Content hash: {row.get('content_hash')}")


@admin_semantic_app.command("import")
def import_model(
    path: str = typer.Argument(..., help="Path to a local Ossie YAML document"),
    description: Optional[str] = typer.Option(None, "--description", help="Optional description to store"),
):
    """Upload a local document, bypassing the moderation queue (admin only).

    Creates or replaces the hand-authored (`source='manual'`) model the
    document declares. The any-user write path is `agnes semantic-model
    apply`, which queues a non-admin's document for review instead.

    A document imported from a registered source (git/upload/connection) —
    see `agnes admin semantic source` — is owned by that source and cannot be
    edited here; only a manually-imported document stays editable through
    this API. Pre-check the file offline first: `agnes semantic-model
    validate <file>`.
    """
    p = Path(path)
    if not p.exists():
        typer.echo(f"Path not found: {path}", err=True)
        raise typer.Exit(1)
    text = p.read_text()
    payload = {"document": text}
    if description is not None:
        payload["description"] = description
    resp = api_post(_MODELS_PATH, json=payload)
    if resp.status_code == 422:
        body = resp.json().get("detail", {})
        errors = body.get("errors") if isinstance(body, dict) else None
        typer.echo("Document failed schema validation:", err=True)
        for e in errors or [body]:
            typer.echo(f"  {e}", err=True)
        typer.echo("  Check it offline first: agnes semantic-model validate <file>", err=True)
        raise typer.Exit(1)
    if resp.status_code not in (200, 201):
        _fail(resp)
    body = resp.json()
    typer.echo(f"Imported semantic model id={body.get('id')} slug={body.get('slug')}")


@admin_semantic_app.command("delete")
def delete_model(
    model_ref: str = typer.Argument(..., help="Model id or slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Remove one stored model and its flat projection (admin only).

    Deletes the document row AND prunes what it projected into
    `metric_definitions` / `glossary_terms` / `column_metadata`, so nothing is
    left pointing at a model that no longer exists. A source-owned model is
    refused (`409`) — a scheduled sync would simply recreate it; `detach` it
    first if the document is wrong at the source and you intend to own it
    here.
    """
    if not yes:
        if not typer.confirm(f"Delete {model_ref}? Its metrics/glossary/column projections go with it."):
            raise typer.Abort()
    resp = api_delete(f"{_MODELS_PATH}/{model_ref}")
    if resp.status_code == 404:
        _not_found(model_ref)
    if resp.status_code == 409:
        detail = _detail(resp)
        typer.echo(detail.get("message") or f"{model_ref} is owned by a semantic source.", err=True)
        typer.echo(
            "  A sync would recreate it. Either delete it at the source, or "
            f"detach it first: agnes admin semantic detach {model_ref}",
            err=True,
        )
        raise typer.Exit(1)
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Deleted {model_ref}")


@admin_semantic_app.command("detach")
def detach_model(
    model_ref: str = typer.Argument(..., help="Model id or slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """F3: stop sync from overwriting a source-owned model, so it can be
    edited (`agnes admin semantic import`, `agnes semantic-model apply`, or
    the web UI). PG-only (A3 ratchet) — 501s naming the feature on a
    DuckDB-backed instance."""
    if not yes:
        if not typer.confirm(f"Detach {model_ref}? Sync will stop overwriting it until you re-attach."):
            raise typer.Abort()
    resp = api_post(f"{_MODELS_PATH}/{model_ref}/detach", json={"confirm_detach": True})
    if resp.status_code == 404:
        _not_found(model_ref)
    if resp.status_code != 200:
        _fail(resp)
    typer.echo(f"Detached {model_ref}")


@admin_semantic_app.command("reattach")
def reattach_model(
    model_ref: str = typer.Argument(..., help="Model id or slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """F3: return a detached model to the sync path — the next sync run
    rewrites its document from the source, discarding any local edit."""
    if not yes:
        resp = api_get(f"{_MODELS_PATH}/{model_ref}")
        row = resp.json() if resp.status_code == 200 else {}
        source_content_hash = row.get("source_content_hash")
        # NULL means no sync has run since detach yet — "unknown", not
        # "changed" (a bare `!=` would misreport that).
        changed = source_content_hash is not None and source_content_hash != row.get("detach_base_hash")
        warning = " The source has changed since you detached it." if changed else ""
        if not typer.confirm(f"Re-attach {model_ref}? Your local edits will be replaced at the next sync.{warning}"):
            raise typer.Abort()
    resp = api_post(f"{_MODELS_PATH}/{model_ref}/reattach", json={"confirm_reattach": True})
    if resp.status_code == 404:
        _not_found(model_ref)
    if resp.status_code != 200:
        _fail(resp)
    typer.echo(f"Re-attached {model_ref}")


@admin_semantic_app.command("link-package")
def link_package(
    slug: str = typer.Argument(..., help="Model slug"),
    package_id: str = typer.Argument(..., help="Data Package id"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Link a semantic model to a Data Package, granting it that package's
    visibility for non-admin readers (search/export). Idempotent: linking an
    already-linked pair is a no-op."""
    resp = api_post(f"{_MODELS_PATH}/{slug}/packages", json={"package_id": package_id})
    if resp.status_code == 404:
        detail = (resp.json() or {}).get("detail")
        if detail == "data_package_not_found":
            typer.echo(f"Data package not found: {package_id}", err=True)
            raise typer.Exit(1)
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Linked '{slug}' to package '{package_id}'. Packages: {', '.join(body.get('package_ids', [])) or '(none)'}")


@admin_semantic_app.command("unlink-package")
def unlink_package(
    slug: str = typer.Argument(..., help="Model slug"),
    package_id: str = typer.Argument(..., help="Data Package id"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Unlink a semantic model from a Data Package. Idempotent: unlinking a
    pair that was never linked is a no-op."""
    resp = api_delete(f"{_MODELS_PATH}/{slug}/packages/{package_id}")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Unlinked '{slug}' from package '{package_id}'. Packages: {', '.join(body.get('package_ids', [])) or '(none)'}")


# ---------------------------------------------------------------------------
# Where documents come from — `agnes admin semantic source …`
# ---------------------------------------------------------------------------

source_app = typer.Typer(help="Where semantic documents are synced from (git repo / upload / existing connection)")
admin_semantic_app.add_typer(source_app, name="source")


@source_app.command("add")
def add_source(
    kind: str = typer.Option(..., "--kind", help="git | upload | connection"),
    name: str = typer.Option(..., "--name", help="Display name"),
    adapter: str = typer.Option(
        "native",
        "--adapter",
        help="Adapter: native | keboola_metastore | snowflake_semantic | databricks_semantic (default: native)",
    ),
    repo_url: Optional[str] = typer.Option(None, "--repo-url", help="git: repository URL"),
    ref: Optional[str] = typer.Option(None, "--ref", help="git: branch/tag (default: repo default)"),
    glob: Optional[str] = typer.Option(None, "--glob", help="git: glob for document files (default: **/*.yaml)"),
    token_env: Optional[str] = typer.Option(None, "--token-env", help="git: env var holding the clone token"),
    file: Optional[str] = typer.Option(None, "--file", help="upload: local document to read into config.documents"),
    disabled: bool = typer.Option(False, "--disabled", help="Create disabled (excluded from sync)"),
):
    """Register a source to sync semantic models from."""
    config: dict = {}
    if kind == "git":
        if not repo_url:
            typer.echo("--repo-url is required for --kind git", err=True)
            raise typer.Exit(2)
        config = {"repo_url": repo_url}
        if ref:
            config["ref"] = ref
        if glob:
            config["glob"] = glob
        if token_env:
            config["token_env"] = token_env
    elif kind == "upload":
        if not file:
            typer.echo("--file is required for --kind upload", err=True)
            raise typer.Exit(2)
        p = Path(file)
        if not p.exists():
            typer.echo(f"Path not found: {file}", err=True)
            raise typer.Exit(1)
        config = {"documents": [p.read_text()]}
    elif kind == "connection":
        config = {}
    else:
        typer.echo(f"Unknown --kind {kind!r} (expected git, upload or connection)", err=True)
        raise typer.Exit(2)

    resp = api_post(
        _SOURCES_PATH,
        json={"kind": kind, "name": name, "adapter": adapter, "config": config, "enabled": not disabled},
    )
    if resp.status_code != 201:
        _fail(resp)
    body = resp.json()
    typer.echo(f"Created semantic source id={body.get('id')} kind={kind}")


@source_app.command("list")
def list_sources(
    enabled_only: bool = typer.Option(False, "--enabled-only", help="Only show enabled sources"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """List registered semantic sources."""
    params = {"enabled_only": enabled_only} if enabled_only else None
    resp = api_get(_SOURCES_PATH, params=params)
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return
    typer.echo(f"Semantic sources: {len(rows)}")
    for r in rows:
        state = "enabled" if r.get("enabled") else "disabled"
        last = r.get("last_sync_status") or "never synced"
        typer.echo(f"{r.get('id', ''):<16}  {r.get('kind', ''):<10}  {r.get('name', ''):<24}  {state:<9}  {last}")


@source_app.command("sync")
def sync_source(
    source_id: str = typer.Argument(..., help="Semantic source id"),
):
    """Fetch and import one source now. A failed fetch imports nothing and
    is recorded on the source, never mistaken for "upstream went empty"."""
    resp = api_post(f"{_SOURCES_PATH}/{source_id}/sync")
    if resp.status_code == 404:
        _source_not_found(source_id)
    if resp.status_code != 200:
        _fail(resp)
    report = resp.json()
    typer.echo(
        f"written {report.get('models_written', 0)}  "
        f"unchanged {report.get('models_unchanged', 0)}  "
        f"pruned {len(report.get('models_pruned') or [])}  "
        f"invalid {len(report.get('invalid') or [])}"
    )
    for bad in report.get("invalid") or []:
        errors = bad.get("errors") or []
        typer.echo(f"  skipped a document: {'; '.join(errors)}", err=True)


@source_app.command("rm")
def remove_source(
    source_id: str = typer.Argument(..., help="Semantic source id from `agnes admin semantic source list`"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Unregister one semantic source (admin only).

    Removes the sync configuration, not the models it already imported: those
    rows stay, and `agnes admin semantic health` reports them as models whose
    source is gone. Delete them explicitly with `agnes admin semantic delete`
    if that is what you meant.
    """
    if not yes:
        if not typer.confirm(f"Remove semantic source {source_id}? Models it already imported are kept."):
            raise typer.Abort()
    resp = api_delete(f"{_SOURCES_PATH}/{source_id}")
    if resp.status_code == 404:
        _source_not_found(source_id)
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Removed semantic source {source_id}")
    typer.echo("  Models it imported are still stored — see: agnes admin semantic health")


# ---------------------------------------------------------------------------
# Cross-domain coverage (F4.1) — `agnes admin semantic coverage …`
# ---------------------------------------------------------------------------

# No `help=` on purpose: Typer prefers it over the callback docstring, and the
# whole point of the two long docstrings below is that a reader can tell the
# three near-identical "coverage" reports apart from `--help` alone.
coverage_app = typer.Typer(invoke_without_command=True)
admin_semantic_app.add_typer(coverage_app, name="coverage")


def _print_coverage(*, source: Optional[str], as_json: bool) -> None:
    """Render `GET /api/admin/semantic-model/coverage` as a source × domain grid.

    Server-side computation, so there is no local/server scope to choose:
    the report is a fact about the SERVER's registry and connections, and a
    laptop has neither. (Command-UX standard: no new boolean scope flag —
    there is no second scope to name here.)
    """
    params = {"source": source} if source else None
    resp = api_get(_COVERAGE_PATH, params=params)
    _fail_needs_postgres(resp, "Coverage")
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    sources = body.get("sources") or []
    if not sources:
        hint = f" for --source {source}" if source else ""
        typer.echo(f"No data sources to report on{hint}.")
        typer.echo("  Connect one at /admin/data-sources, or `agnes admin connection add`.")
        return

    widths = {d: max(len(d), 12) for d in _COVERAGE_DOMAINS}
    name_width = max(max(len(s.get("name") or s["source_id"]) for s in sources), 6)
    header = "SOURCE".ljust(name_width) + "  " + "  ".join(d.ljust(widths[d]) for d in _COVERAGE_DOMAINS)
    typer.echo(header)
    for entry in sources:
        domains = entry.get("domains") or {}
        cells = []
        for domain in _COVERAGE_DOMAINS:
            status = (domains.get(domain) or {}).get("status") or "?"
            cells.append(_STATUS_GLYPHS.get(status, status).ljust(widths[domain]))
        typer.echo((entry.get("name") or entry["source_id"]).ljust(name_width) + "  " + "  ".join(cells))

    # The grid says WHICH cell is short; the lines below say what to do about
    # it. A grid on its own is a scoreboard, and the report is not one.
    for entry in sources:
        gaps = [
            (domain, cell)
            for domain, cell in (entry.get("domains") or {}).items()
            if (cell or {}).get("status") in ("missing", "partial")
        ]
        if not gaps:
            continue
        typer.echo("")
        typer.echo(f"{entry.get('name') or entry['source_id']} ({entry.get('source_type') or 'unknown'}):")
        for domain, cell in gaps:
            action = cell.get("action") or {}
            suffix = f" → {action['label']}: {action['href']}" if action.get("href") else ""
            typer.echo(f"  {domain}: {cell.get('detail') or cell.get('status')}{suffix}")


@coverage_app.callback(invoke_without_command=True)
def coverage(
    ctx: typer.Context,
    source: Optional[str] = typer.Option(
        None, "--source", help="Only this source connection id (`__local__` for tables with no connection)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """PER SOURCE: what each connected data source still lacks.

    One row per source, one column per domain — semantic model, metrics,
    glossary, skill, agent, knowledge base — as `ok` / `partial` / `MISSING` /
    `n/a`. `n/a` is not a gap: it means the domain cannot be filled for that
    source type in this build (e.g. no semantic-layer adapter exists for it),
    so it is deliberately not reported as work to do.

    Reads `GET /api/admin/semantic-model/coverage` (MCP:
    `semantic_model_coverage`). Two neighbours answer DIFFERENT questions:
    `coverage tables` is a per-TABLE list, and `keboola-import` predicts a
    live Keboola project's import rather than reading anything stored.
    `coverage show` is the explicit form of this bare invocation.
    """
    if ctx.invoked_subcommand is not None:
        return
    _print_coverage(source=source, as_json=as_json)


@coverage_app.command("show")
def coverage_show(
    source: Optional[str] = typer.Option(
        None, "--source", help="Only this source connection id (`__local__` for tables with no connection)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Explicit form of the bare `agnes admin semantic coverage` (same output)."""
    _print_coverage(source=source, as_json=as_json)


@coverage_app.command("tables")
def coverage_tables(
    limit: int = typer.Option(0, "--limit", min=0, help="Cap the number of tables listed (0 = no cap)."),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """PER TABLE: which registered tables no valid semantic model describes.

    A flat list of `table_registry` rows, source-agnostic — a table bound
    through ANY semantic source (Keboola, git, manual, upload, connection)
    counts as covered. Reads `GET /api/admin/semantic-coverage` (MCP:
    `admin_semantic_coverage`).

    Different question from its two neighbours: the bare `agnes admin
    semantic coverage` is a per-SOURCE grid across six domains (this one
    only looks at the semantic domain, and only at tables), and
    `keboola-import` predicts what a live Keboola project WOULD import
    rather than reading what is stored.
    """
    resp = api_get(_TABLE_COVERAGE_PATH)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    tables = body.get("tables") or []
    if as_json:
        if limit and limit > 0 and len(tables) > limit:
            body["tables"] = tables[:limit]
            body["truncated"] = {"limit": limit, "total": len(tables)}
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    if not tables:
        typer.echo("Every registered table has semantic-layer coverage.")
        return

    shown = tables[:limit] if limit and limit > 0 else tables
    typer.echo(f"{len(tables)} table(s) with no semantic-layer coverage:")
    for row in shown:
        typer.echo(f"  {row.get('id')} ({row.get('name')})")
    if limit and limit > 0 and len(tables) > limit:
        typer.echo(f"  … and {len(tables) - limit} more — raise --limit to see them")


@coverage_app.command("tag")
def coverage_tag(
    resource_type: str = typer.Argument(..., help=f"One of: {', '.join(_COVERAGE_TAGGABLE)}"),
    resource_id: str = typer.Argument(..., help="Same id format the RBAC grant for that type uses"),
    source_id: str = typer.Argument(..., help="source_connections.id this resource is ABOUT"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Record that a skill / agent / knowledge domain is about a data source.

    This is the only input the coverage report cannot derive: those three
    live in their own tables with no notion of a source. Mirrors
    `POST /api/admin/semantic-model/coverage/tags` and the MCP
    `semantic_model_coverage_tag` tool.
    """
    if resource_type not in _COVERAGE_TAGGABLE:
        typer.echo(
            f"Unknown resource type {resource_type!r} — expected one of {', '.join(_COVERAGE_TAGGABLE)}", err=True
        )
        raise typer.Exit(1)

    resp = api_post(
        _COVERAGE_TAGS_PATH,
        json={"resource_type": resource_type, "resource_id": resource_id, "source_id": source_id},
    )
    if resp.status_code == 409:
        typer.echo(f"Already tagged: {resource_type} {resource_id!r} → {source_id}", err=True)
        typer.echo("  See the current tags: agnes admin semantic coverage --json", err=True)
        raise typer.Exit(1)
    if resp.status_code == 404:
        typer.echo(f"No source connection {source_id!r}.", err=True)
        typer.echo("  List them: agnes admin connection list", err=True)
        raise typer.Exit(1)
    if resp.status_code not in (200, 201):
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return
    typer.echo(f"Tagged: {resource_type} {resource_id!r} → {source_id} (tag {body.get('id')})")


@coverage_app.command("untag")
def coverage_untag(
    tag_id: str = typer.Argument(..., help="Tag id from `agnes admin semantic coverage --json`"),
):
    """Remove one source tag.

    Mirrors `DELETE /api/admin/semantic-model/coverage/tags/{tag_id}` and the
    MCP `semantic_model_coverage_untag` tool.
    """
    resp = api_delete(f"{_COVERAGE_TAGS_PATH}/{tag_id}")
    if resp.status_code == 404:
        typer.echo(f"No coverage tag {tag_id!r}.", err=True)
        typer.echo("  Find the id: agnes admin semantic coverage --json", err=True)
        raise typer.Exit(1)
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Untagged: {tag_id}")


# ---------------------------------------------------------------------------
# Keboola import health — `agnes admin semantic keboola-import`
# ---------------------------------------------------------------------------


@admin_semantic_app.command("keboola-import")
def keboola_import(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
    limit: int = typer.Option(10, "--limit", help="Max unregistered tables / blocked metrics listed per project"),
):
    """PER KEBOOLA PROJECT: how much of its published semantic layer can reach Agnes.

    Computed LIVE against each connected project's Metastore plus
    `table_registry` — it reads no stored counters, so it is accurate
    immediately after a restart and answers a prediction ("what would import")
    rather than a fact about what is stored. That is what separates it from
    the two `coverage` reports; it was called `agnes admin semantic-layer
    coverage`, which made three different questions look like one.

    Reads `GET /api/admin/semantic-layer/coverage` (MCP:
    `admin_semantic_layer_coverage`). Metrics for tables this instance does
    not register are normal and reported as a plain count — a semantic layer
    usually describes far more of a project than any one instance registers.
    """
    resp = api_get(_KEBOOLA_IMPORT_PATH)
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return

    sources = data.get("sources") or []
    if not sources:
        typer.echo("No Keboola project has a master (owner) token configured.")
        typer.echo("Add one with: agnes admin connection secret <connection-id> --kind master")
        return

    for source in sources:
        project = source.get("project") or {}
        label = f"{source.get('name', '')}"
        if project.get("id") is not None:
            label += f"  (project {project['id']} {project.get('name', '')!r})"
        typer.echo(f"\n{label}")

        if source.get("error"):
            typer.echo(f"  error: {source['error']}")
            continue

        metrics = source.get("metrics") or {}
        upstream, importable = metrics.get("upstream", 0), metrics.get("importable", 0)
        typer.echo(f"  metrics:  {importable} / {upstream} upstream")
        typer.echo(f"  glossary: {(source.get('glossary') or {}).get('upstream', 0)} upstream")

        for warning in source.get("warnings") or []:
            typer.echo(f"  ! {warning.get('message', '')}")

        blocked = source.get("blocked") or []
        if blocked:
            typer.echo(f"  blocked by their own definition ({len(blocked)}):")
            for row in blocked[:limit]:
                reason = _REASON_LABELS.get(row.get("reason", ""), row.get("reason", ""))
                typer.echo(f"     {row.get('metric', '')} — {reason}")
            if len(blocked) > limit:
                typer.echo(f"     … and {len(blocked) - limit} more")

        unregistered = source.get("unregistered_tables") or []
        if unregistered:
            shown = ", ".join(unregistered[:limit])
            more = f" … and {len(unregistered) - limit} more" if len(unregistered) > limit else ""
            typer.echo(f"  no table registered here for {len(unregistered)} dataset(s): {shown}{more}")
            if importable == 0 and upstream:
                typer.echo("     register one with: agnes admin register-table --help")


# ---------------------------------------------------------------------------
# Health roll-up (F4.2) — `agnes admin semantic health`
# ---------------------------------------------------------------------------


@admin_semantic_app.command("health")
def health(as_json: bool = typer.Option(False, "--json", help="Emit raw JSON")):
    """Is the semantic layer trustworthy right now (admin only)?

    Sync failures, models whose source was deleted or renamed away from under
    them, documents that failed validation, three static quality checks (a
    metric with no description, one name defined twice with a different
    formula, a cross-dataset metric with no declared relationship), a roll-up
    of `coverage`'s missing/partial counts, and every active mute.

    Mirrors `GET /api/admin/semantic-layer/health` and the MCP
    `semantic_layer_health` tool.
    """
    resp = api_get(_HEALTH_PATH)
    _fail_needs_postgres(resp, "The health report")
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    sources = body.get("sources") or []
    failed = [s for s in sources if s.get("last_sync_status") == "error"]
    if failed:
        typer.echo(f"Sync failures ({len(failed)}):")
        for s in failed:
            typer.echo(f"  {s.get('name') or s['source_id']}: {s.get('last_sync_error') or 'unknown error'}")
        typer.echo("")

    orphaned = body.get("orphaned_models") or []
    if orphaned:
        typer.echo(f"Models whose source is gone ({len(orphaned)}):")
        for m in orphaned:
            typer.echo(f"  {m.get('slug') or m['model_id']} (source_ref={m.get('source_ref')})")
        typer.echo("")

    # Block 5 of #1707 (PR #1717), ported here with the command: a table
    # delete (or a rename that landed under a new id/name) left a metric
    # binding or a profiled column pointing at nothing. `binding` names which
    # half of `src/semantic/orphans.py` produced the finding.
    #
    # This renderer hardcodes one section per health-check key, so a key with
    # no section is swallowed in silence — which is why this block must
    # survive the merge of #1717 and this PR rather than being resolved away
    # as a duplicate.
    table_bindings = body.get("orphaned_table_bindings") or []
    if table_bindings:
        typer.echo(f"Semantic objects bound to a deleted/renamed table ({len(table_bindings)}):")
        for f in table_bindings:
            if f.get("binding") == "metric":
                missing = ", ".join(f.get("missing_tables") or [])
                typer.echo(f"  metric {f.get('name') or f['metric_id']} -> missing table(s): {missing}")
            else:
                typer.echo(f"  table {f.get('table_id')}: {f.get('column_count')} profiled column(s) orphaned")
        typer.echo("")

    invalid = body.get("invalid_models") or []
    if invalid:
        typer.echo(f"Invalid documents ({len(invalid)}):")
        for m in invalid:
            typer.echo(f"  {m.get('slug') or m['model_id']}: {m.get('validation_errors')}")
        typer.echo("")

    missing_desc = body.get("metrics_missing_description") or []
    if missing_desc:
        typer.echo(f"Metrics with no description ({len(missing_desc)}):")
        for m in missing_desc:
            typer.echo(f"  {m.get('name')}")
        typer.echo("")

    dupes = body.get("duplicate_metric_names") or []
    if dupes:
        typer.echo(f"Metric names defined more than once, differently ({len(dupes)}):")
        for d in dupes:
            typer.echo(f"  {d.get('name')} — {len(d.get('expressions') or [])} different formula(s)")
        typer.echo("")

    missing_rel = body.get("metrics_missing_relationships") or []
    if missing_rel:
        typer.echo(f"Cross-dataset metrics with no declared relationship ({len(missing_rel)}):")
        for m in missing_rel:
            typer.echo(f"  {m.get('metric_name')} spans {', '.join(m.get('datasets') or [])}")
        typer.echo("")

    summary = body.get("coverage_summary") or {}
    typer.echo(f"Coverage: {summary.get('missing_count', 0)} missing, {summary.get('partial_count', 0)} partial")

    mutes_list = body.get("mutes") or []
    if mutes_list:
        typer.echo(f"Muted ({len(mutes_list)} of the above are silenced — see `agnes admin semantic mutes`)")

    if not (failed or orphaned or table_bindings or invalid or missing_desc or dupes or missing_rel):
        typer.echo("No sync failures, disconnected models, or invalid documents.")


# ---------------------------------------------------------------------------
# Mutes (F4.3) — `agnes admin semantic mute|unmute|mutes`
#
# Top-level verbs, not a `mute` sub-group: the three commands are one action
# each, and a group whose every member is a bare verb reads as `mute mute`.
# ---------------------------------------------------------------------------


@admin_semantic_app.command("mute")
def mute(
    scope: str = typer.Argument(..., help=f"What to silence — one of: {', '.join(_MUTE_SCOPE_FORMS)}"),
    reason: Optional[str] = typer.Option(
        None, "--reason", help="Why it is expected — stored on the mute and shown wherever it appears"
    ),
    expires: Optional[str] = typer.Option(
        None,
        "--expires",
        help="ISO-8601 instant to un-silence itself at (e.g. 2026-10-01T00:00:00Z); omit = until unmuted",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Silence one semantic-layer check you already know about (admin only).

    Muting is legitimate — a gap you have read, judged expected and scheduled
    should not shout on every page load. Muting ANONYMOUSLY is not: your
    identity, the time and (if you give one) your reason are stored with it and
    shown wherever the mute appears, so nobody later has to guess whether the
    check was fixed or hidden. Pass `--reason` unless there is genuinely
    nothing to say.

    Scopes: `domain:<domain>` silences one domain across every source,
    `source:<id>` silences one source entirely, and
    `source:<id>:domain:<domain>` silences the single cell. The coverage
    domains are semantic, metrics, glossary, skill, agent, knowledge_base
    (`agnes admin semantic coverage` prints them as columns); `__local__` is
    the source id of the bucket for tables with no connection.

    Mirrors `POST /api/admin/semantic-layer/mutes` and the MCP
    `mute_semantic_check` tool.
    """
    payload = {"scope": scope, "reason": reason, "expires_at": expires}
    resp = api_post(_MUTES_PATH, json={k: v for k, v in payload.items() if v is not None})
    _fail_needs_postgres(resp, "Muting a check")

    if resp.status_code == 400:
        detail = _detail(resp)
        typer.echo(detail.get("message") or f"Could not mute {scope!r}.", err=True)
        if detail.get("error") == "invalid_scope":
            typer.echo(f"  Expected one of: {', '.join(_MUTE_SCOPE_FORMS)}", err=True)
        raise typer.Exit(1)
    if resp.status_code == 404:
        typer.echo(f"No such data source in scope {scope!r}.", err=True)
        typer.echo("  List them: agnes admin connection list", err=True)
        raise typer.Exit(1)
    if resp.status_code == 409:
        typer.echo(f"Already muted: {_detail(resp).get('message') or scope}", err=True)
        typer.echo("  See it: agnes admin semantic mutes", err=True)
        raise typer.Exit(1)
    if resp.status_code == 422:
        # The only field the server parses for us is `--expires`; a 422 here is
        # almost always an unparseable instant, so name the format rather than
        # dumping pydantic's field-path list at the reader.
        typer.echo(f"Could not read --expires {expires!r} — expected ISO-8601, e.g. 2026-10-01T00:00:00Z.", err=True)
        raise typer.Exit(1)
    if resp.status_code not in (200, 201):
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return
    until = f" until {body.get('expires_at')}" if body.get("expires_at") else ""
    typer.echo(f"Muted: {body.get('scope')}{until} (mute {body.get('id')})")
    if not body.get("reason"):
        # Not an error — the API allows it — but the whole point of the record
        # is the next reader, and "muted by you, no reason given" is a thin
        # thing to inherit.
        typer.echo("  No reason recorded. To leave one, unmute it and mute it again with --reason:")
        typer.echo(f"    agnes admin semantic unmute {body.get('id')}")


@admin_semantic_app.command("unmute")
def unmute(
    mute_id: str = typer.Argument(..., help="Mute id from `agnes admin semantic mutes`"),
):
    """Let one check report again (admin only).

    Mirrors `DELETE /api/admin/semantic-layer/mutes/{mute_id}` and the MCP
    `unmute_semantic_check` tool.
    """
    resp = api_delete(f"{_MUTES_PATH}/{mute_id}")
    _fail_needs_postgres(resp, "Unmuting a check")
    if resp.status_code == 404:
        typer.echo(f"No semantic-layer mute {mute_id!r}.", err=True)
        typer.echo("  Find the id: agnes admin semantic mutes", err=True)
        raise typer.Exit(1)
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Unmuted: {mute_id}")


@admin_semantic_app.command("mutes")
def mutes(
    include_expired: bool = typer.Option(
        False, "--include-expired", help="Also show mutes that have lapsed (the record outlives the silence)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """What is currently silenced, and who silenced it (admin only).

    Server-side state, so there is no local/server scope to choose — mutes are
    a fact about the instance's own health report.

    Mirrors `GET /api/admin/semantic-layer/mutes` and the MCP
    `semantic_mutes_list` tool.
    """
    resp = api_get(_MUTES_PATH, params={"include_expired": "true"} if include_expired else None)
    _fail_needs_postgres(resp, "The muted-check list")
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    items = body.get("items") or []
    if not items:
        typer.echo("No muted checks." if include_expired else "No muted checks (nothing is being silenced).")
        typer.echo('  Silence one you already know about: agnes admin semantic mute domain:<domain> --reason "…"')
        return

    for item in items:
        expires = item.get("expires_at")
        window = f"until {expires}" if expires else "no expiry"
        typer.echo(f"{item.get('id')}  {(item.get('scope') or '?'):<44} {window}")
        # Who and when on their own line, always printed: they are the record,
        # not a detail the reader has to ask for with a flag.
        typer.echo(f"    muted by {item.get('muted_by') or 'unknown'} at {item.get('muted_at')}")
        typer.echo(f"    reason: {item.get('reason') or '(none given)'}")


# ---------------------------------------------------------------------------
# The report queue — `agnes admin semantic feedback list|resolve`
#
# `feedback submit` is NOT here: filing "that answer looked wrong" is open to
# anyone signed in and stays in `agnes semantic-model feedback submit`, beside
# the analysis that produced the bad number. Working the queue calls
# `require_admin` endpoints, so it lives on this side of the same split every
# other command in this block follows: placement follows authority.
# ---------------------------------------------------------------------------

feedback_app = typer.Typer(help="Admin: the semantic-feedback queue — read it, close it")
admin_semantic_app.add_typer(feedback_app, name="feedback")

_FEEDBACK_ADMIN_PATH = "/api/admin/semantic-feedback"
_FEEDBACK_STATUSES = ("open", "acknowledged", "resolved")


@feedback_app.command("list")
def feedback_list(
    status: Optional[str] = typer.Option(
        None, "--status", help=f"Only this status ({', '.join(_FEEDBACK_STATUSES)}); omit for all"
    ),
    limit: int = typer.Option(0, "--limit", min=0, help="Cap rows shown (0 = no cap)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """The report queue, newest first (admin only).

    Reads what anyone signed in filed with `agnes semantic-model feedback
    submit` (or the chat agent's `flag_semantic_issue`). Mirrors
    `GET /api/admin/semantic-feedback` and the MCP `semantic_feedback_list`
    tool.
    """
    if status is not None and status not in _FEEDBACK_STATUSES:
        typer.echo(f"Unknown status {status!r} — expected one of {', '.join(_FEEDBACK_STATUSES)}", err=True)
        raise typer.Exit(1)

    resp = api_get(_FEEDBACK_ADMIN_PATH, params={"status": status} if status else None)
    _fail_needs_postgres(resp, "The feedback queue")
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    items = body.get("items") or []
    total = len(items)
    truncated = limit and limit > 0 and total > limit
    if truncated:
        items = items[:limit]

    if as_json:
        # The JSON surface discloses the cap too — a machine caller must not
        # read a sliced list as "these are all of them" (command-UX standard).
        out: dict = {"items": items, "count": len(items)}
        if truncated:
            out["truncated"] = {"limit": limit, "total": total}
        typer.echo(json.dumps(out, indent=2, default=str))
        return

    if not items:
        scope = f" with status {status}" if status else ""
        typer.echo(f"No feedback reports{scope}.")
        typer.echo('  File one: agnes semantic-model feedback submit "<question>"')
        return

    for item in items:
        typer.echo(
            f"{item.get('id')}  {(item.get('status') or '?'):<12} "
            f"{(item.get('created_by') or 'unknown'):<28} {item.get('question')}"
        )
        if item.get("metric_id"):
            typer.echo(f"    metric: {item['metric_id']}")
        if item.get("comment"):
            typer.echo(f"    comment: {item['comment']}")
        if item.get("sql"):
            typer.echo(f"    sql: {item['sql']}")
        if item.get("resolution_note"):
            typer.echo(f"    resolved by {item.get('resolved_by')}: {item['resolution_note']}")
    if truncated:
        typer.echo(f"… {total - len(items)} more — raise --limit or narrow with --status")


@feedback_app.command("resolve")
def feedback_resolve(
    feedback_id: str = typer.Argument(..., help="Report id from `agnes admin semantic feedback list`"),
    note: Optional[str] = typer.Option(None, "--note", help="What was done about it — recorded on the report"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Close one report, on the record (admin only).

    Mirrors `POST /api/admin/semantic-feedback/{id}/resolve` and the MCP
    `semantic_feedback_resolve` tool.
    """
    resp = api_post(f"{_FEEDBACK_ADMIN_PATH}/{feedback_id}/resolve", json={"resolution_note": note})
    _fail_needs_postgres(resp, "Resolving feedback")
    if resp.status_code == 404:
        typer.echo(f"No semantic feedback {feedback_id!r}.", err=True)
        typer.echo("  Find the id: agnes admin semantic feedback list", err=True)
        raise typer.Exit(1)
    if resp.status_code == 409:
        typer.echo(f"Already resolved: {feedback_id}", err=True)
        typer.echo("  See who closed it: agnes admin semantic feedback list --status resolved", err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return
    typer.echo(f"Resolved: {feedback_id} by {body.get('resolved_by')}")
