"""`agnes admin semantic-source` — admin CRUD over semantic-layer sync
sources (git repo / uploaded documents / existing connection), open
semantic-layer contract, Task 11.

CLI counterpart to the ``/api/admin/semantic-sources`` surface:

  - ``add``  → ``POST /api/admin/semantic-sources``
  - ``list`` → ``GET /api/admin/semantic-sources``
  - ``sync`` → ``POST /api/admin/semantic-sources/{id}/sync``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get, api_post

admin_semantic_source_app = typer.Typer(help="Admin: semantic-source sync configuration")


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


@admin_semantic_source_app.command("add")
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
        "/api/admin/semantic-sources",
        json={"kind": kind, "name": name, "adapter": adapter, "config": config, "enabled": not disabled},
    )
    if resp.status_code != 201:
        _fail(resp)
    body = resp.json()
    typer.echo(f"Created semantic source id={body.get('id')} kind={kind}")


@admin_semantic_source_app.command("list")
def list_sources(
    enabled_only: bool = typer.Option(False, "--enabled-only", help="Only show enabled sources"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """List registered semantic sources."""
    params = {"enabled_only": enabled_only} if enabled_only else None
    resp = api_get("/api/admin/semantic-sources", params=params)
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


@admin_semantic_source_app.command("sync")
def sync_source(
    source_id: str = typer.Argument(..., help="Semantic source id"),
):
    """Fetch and import one source now. A failed fetch imports nothing and
    is recorded on the source, never mistaken for "upstream went empty"."""
    resp = api_post(f"/api/admin/semantic-sources/{source_id}/sync")
    if resp.status_code == 404:
        typer.echo(f"Semantic source not found: {source_id}", err=True)
        typer.echo("Try: agnes admin semantic-source list", err=True)
        raise typer.Exit(1)
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
