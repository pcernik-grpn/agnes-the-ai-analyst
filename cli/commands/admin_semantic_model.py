"""`agnes admin semantic-model` — admin CRUD over canonical Ossie semantic
models (open semantic-layer contract, Task 11).

CLI counterpart to the ``/api/admin/semantic-models`` + public
``/api/semantic-models/*`` surface:

  - ``list``           → ``GET /api/admin/semantic-models``
  - ``show``           → ``GET /api/admin/semantic-models/{id}``
  - ``import``         → ``POST /api/admin/semantic-models`` (reads a local file)
  - ``export``         → ``GET /api/semantic-models/{slug}.yaml``
  - ``detach``         → ``POST /api/admin/semantic-models/{id}/detach`` (PG-only, F3)
  - ``reattach``       → ``POST /api/admin/semantic-models/{id}/reattach`` (PG-only, F3)
  - ``link-package``   → ``POST /api/admin/semantic-models/{slug}/packages``
  - ``unlink-package`` → ``DELETE /api/admin/semantic-models/{slug}/packages/{package_id}``
  - ``validate`` → local only, no server call, no token: schema-checks a
    file against the vendored Ossie spec (``src.semantic.document_validation``)
    so an author can fix a document before ever reaching an admin session.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_post

admin_semantic_model_app = typer.Typer(help="Admin: semantic-model CRUD (Ossie documents)")


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


def _not_found(ref: str) -> None:
    typer.echo(f"Semantic model not found: {ref}", err=True)
    typer.echo("Try: agnes admin semantic-model list", err=True)
    raise typer.Exit(1)


@admin_semantic_model_app.command("list")
def list_models(
    term: Optional[str] = typer.Argument(None, help="Filter by substring in slug/name/description"),
    limit: int = typer.Option(50, "--limit", help="Max rows to show"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """List every stored semantic model (any status)."""
    resp = api_get("/api/admin/semantic-models")
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
    rows = rows[:limit]

    if as_json:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return

    typer.echo(f"Semantic models: {len(rows)}")
    for r in rows:
        typer.echo(f"{r.get('id', ''):<28}  {r.get('slug', ''):<20}  {r.get('source', ''):<10}  {r.get('status', '')}")


@admin_semantic_model_app.command("show")
def show_model(
    model_ref: str = typer.Argument(..., help="Model id or slug"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Show one semantic model's metadata (not the document body — use `export` for that)."""
    resp = api_get(f"/api/admin/semantic-models/{model_ref}")
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


@admin_semantic_model_app.command("import")
def import_model(
    path: str = typer.Argument(..., help="Path to a local Ossie YAML document"),
    description: Optional[str] = typer.Option(None, "--description", help="Optional description to store"),
):
    """Upload a local document, creating or replacing the hand-authored
    (``source='manual'``) model it declares.

    A document imported from a registered `semantic-source` (git/upload/
    connection) instead — see `agnes admin semantic-source` — is owned by
    that source and cannot be edited here; only a manually-imported
    document stays editable through this API.
    """
    p = Path(path)
    if not p.exists():
        typer.echo(f"Path not found: {path}", err=True)
        raise typer.Exit(1)
    text = p.read_text()
    payload = {"document": text}
    if description is not None:
        payload["description"] = description
    resp = api_post("/api/admin/semantic-models", json=payload)
    if resp.status_code == 422:
        body = resp.json().get("detail", {})
        errors = body.get("errors") if isinstance(body, dict) else None
        typer.echo("Document failed schema validation:", err=True)
        for e in errors or [body]:
            typer.echo(f"  {e}", err=True)
        raise typer.Exit(1)
    if resp.status_code not in (200, 201):
        _fail(resp)
    body = resp.json()
    typer.echo(f"Imported semantic model id={body.get('id')} slug={body.get('slug')}")


@admin_semantic_model_app.command("export")
def export_model(
    slug: str = typer.Argument(..., help="Model slug"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write to this file instead of stdout"),
):
    """Export the stored document byte-for-byte (comments and key order survive)."""
    resp = api_get(f"/api/semantic-models/{slug}.yaml")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code == 403:
        typer.echo(f"Access denied: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        _fail(resp)
    text = resp.text
    if output:
        Path(output).write_text(text)
        typer.echo(f"Wrote {output}")
        return
    typer.echo(text, nl=not text.endswith("\n"))


@admin_semantic_model_app.command("detach")
def detach_model(
    model_ref: str = typer.Argument(..., help="Model id or slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """F3: stop sync from overwriting a source-owned model, so it can be
    edited (``import``/``agnes admin semantic-model import``, or via the
    web UI). PG-only (A3 ratchet) — 501s naming the feature on a
    DuckDB-backed instance."""
    if not yes:
        if not typer.confirm(f"Detach {model_ref}? Sync will stop overwriting it until you re-attach."):
            raise typer.Abort()
    resp = api_post(f"/api/admin/semantic-models/{model_ref}/detach", json={"confirm_detach": True})
    if resp.status_code == 404:
        _not_found(model_ref)
    if resp.status_code != 200:
        _fail(resp)
    typer.echo(f"Detached {model_ref}")


@admin_semantic_model_app.command("reattach")
def reattach_model(
    model_ref: str = typer.Argument(..., help="Model id or slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """F3: return a detached model to the sync path — the next sync run
    rewrites its document from the source, discarding any local edit."""
    if not yes:
        resp = api_get(f"/api/admin/semantic-models/{model_ref}")
        row = resp.json() if resp.status_code == 200 else {}
        source_content_hash = row.get("source_content_hash")
        # NULL means no sync has run since detach yet — "unknown", not
        # "changed" (a bare `!=` would misreport that).
        changed = source_content_hash is not None and source_content_hash != row.get("detach_base_hash")
        warning = " The source has changed since you detached it." if changed else ""
        if not typer.confirm(f"Re-attach {model_ref}? Your local edits will be replaced at the next sync.{warning}"):
            raise typer.Abort()
    resp = api_post(f"/api/admin/semantic-models/{model_ref}/reattach", json={"confirm_reattach": True})
    if resp.status_code == 404:
        _not_found(model_ref)
    if resp.status_code != 200:
        _fail(resp)
    typer.echo(f"Re-attached {model_ref}")


@admin_semantic_model_app.command("link-package")
def link_package(
    slug: str = typer.Argument(..., help="Model slug"),
    package_id: str = typer.Argument(..., help="Data Package id"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Link a semantic model to a Data Package, granting it that package's
    visibility for non-admin readers (export/search). Idempotent: linking
    an already-linked pair is a no-op."""
    resp = api_post(f"/api/admin/semantic-models/{slug}/packages", json={"package_id": package_id})
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


@admin_semantic_model_app.command("unlink-package")
def unlink_package(
    slug: str = typer.Argument(..., help="Model slug"),
    package_id: str = typer.Argument(..., help="Data Package id"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Unlink a semantic model from a Data Package. Idempotent: unlinking a
    pair that was never linked is a no-op."""
    resp = api_delete(f"/api/admin/semantic-models/{slug}/packages/{package_id}")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Unlinked '{slug}' from package '{package_id}'. Packages: {', '.join(body.get('package_ids', [])) or '(none)'}")


@admin_semantic_model_app.command("validate")
def validate_model(
    path: str = typer.Argument(..., help="Path to a local Ossie YAML document"),
):
    """Schema-check a local file against the vendored Ossie spec.

    Runs entirely offline: no server, no token. An author fixing a
    document should not need a reachable instance to iterate.
    """
    p = Path(path)
    if not p.exists():
        typer.echo(f"Path not found: {path}", err=True)
        raise typer.Exit(1)

    from src.semantic.document_validation import validate_document

    result = validate_document(p.read_text())
    if result.ok:
        typer.echo(f"OK — spec version {result.spec_version}")
        return
    typer.echo("Invalid document:", err=True)
    for e in result.errors:
        typer.echo(f"  {e}", err=True)
    raise typer.Exit(1)
