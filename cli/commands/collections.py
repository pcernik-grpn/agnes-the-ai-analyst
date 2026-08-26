"""`agnes collections` — manage file corpora (Collections Slice 2).

Commands:
  create  --name ... [--description ...] [--json]
  list    [--json]
  show    <id>       [--json]
  upload    <id> <path...>   (multipart POST per file)
  reingest  <id> <file_id>   (re-run ingestion for one file)
  rm        <id>       [--yes]
  rm-file   <id> <file_id>   [--yes]
"""

from __future__ import annotations

import json as json_lib
from pathlib import Path
from typing import Optional

import typer

from cli.v2_client import (
    V2ClientError,
    api_delete,
    api_get_json,
    api_post_json,
    api_post_multipart,
)

collections_app = typer.Typer(help="Manage file collections (upload corpora)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_collection(col: dict) -> str:
    desc = col.get("description") or ""
    return f"{col['id']:20s}  {col.get('slug', ''):20s}  {col['name']}  {desc}"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@collections_app.command("create")
def create_collection(
    name: str = typer.Option(..., "--name", help="Collection name"),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Description"),
    slug: Optional[str] = typer.Option(None, "--slug", help="URL-safe slug (auto-generated if omitted)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Create a new file collection (admin only)."""
    payload: dict = {"name": name}
    if description is not None:
        payload["description"] = description
    if slug is not None:
        payload["slug"] = slug
    try:
        body = api_post_json("/api/collections", payload)
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return
    line = f"Created: id={body['id']}  slug={body.get('slug', '')}  name={body['name']}"
    # Older servers don't return `visibility`; say where the upload landed
    # only when the server said so (it may be "workspace" under
    # library.auto_share_admin_uploads).
    if body.get("visibility"):
        line += f"  visibility={body['visibility']}"
    typer.echo(line)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@collections_app.command("list")
def list_collections(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """List collections accessible to you (RBAC-filtered)."""
    try:
        body = api_get_json("/api/collections")
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    items = body.get("items", [])
    if not items:
        typer.echo("No collections found.")
        return
    typer.echo(f"{'ID':20s}  {'SLUG':20s}  NAME")
    for col in items:
        typer.echo(_fmt_collection(col))


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@collections_app.command("search")
def search_collections(
    query: str = typer.Argument(..., help="Search query"),
    k: int = typer.Option(10, "--k", "--limit", help="Max results"),
    collection_id: Optional[str] = typer.Option(None, "--collection", "-c", help="Restrict to one collection id"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Hybrid search across your accessible collections (RBAC-filtered)."""
    params: dict = {"q": query, "k": k}
    if collection_id:
        params["corpus_id"] = collection_id
    try:
        body = api_get_json("/api/collections/search", **params)
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return
    results = body.get("results", [])
    if not results:
        typer.echo("No matches.")
        return
    for r in results:
        loc = r.get("filename") or r.get("file_id")
        typer.echo(f"[{r.get('score')}] {loc} #{r.get('ordinal')}: {(r.get('text') or '')[:120]}")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@collections_app.command("show")
def show_collection(
    collection_id: str = typer.Argument(..., help="Collection ID (e.g. col_abc123)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Show detail + file list for a collection."""
    try:
        body = api_get_json(f"/api/collections/{collection_id}")
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(f"ID:          {body['id']}")
    typer.echo(f"Slug:        {body.get('slug', '')}")
    typer.echo(f"Name:        {body['name']}")
    if body.get("description"):
        typer.echo(f"Description: {body['description']}")
    typer.echo(f"Created by:  {body.get('created_by', '')}")
    files = body.get("files", [])
    typer.echo(f"\nFiles ({len(files)}):")
    if not files:
        typer.echo("  (none)")
        return
    typer.echo(f"  {'FILE_ID':20s}  {'STATUS':10s}  {'SIZE':8s}  FILENAME")
    for f in files:
        size = f.get("size_bytes") or 0
        typer.echo(f"  {f['file_id']:20s}  {f['processing_status']:10s}  {size:8d}  {f['filename']}")


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


@collections_app.command("upload")
def upload_files(
    collection_id: str = typer.Argument(..., help="Collection ID"),
    paths: list[Path] = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="One or more local file paths to upload",
    ),
    logical_path: Optional[str] = typer.Option(
        None,
        "--path",
        help="Logical id for upsert (single file only). Re-uploading the same "
        "--path REPLACES the existing file instead of adding a duplicate.",
    ),
):
    """Upload one or more files into a collection (multipart POST per file).

    Each file is sent as a separate request.  The server's extension
    allowlist determines whether a file lands as ``pending`` (tier1/tier2)
    or ``rejected`` (unsupported type).

    Pass ``--path`` to give a single file a stable logical identity so
    re-uploading it upserts (replaces) rather than duplicates.
    """
    if logical_path is not None and len(paths) != 1:
        typer.echo("--path can only be used when uploading a single file", err=True)
        raise typer.Exit(2)
    any_error = False
    for path in paths:
        fname = path.name
        file_bytes = path.read_bytes()
        files = {
            "files": (fname, file_bytes, "application/octet-stream"),
        }
        data = {"paths": logical_path} if logical_path is not None else None
        try:
            results = api_post_multipart(f"/api/collections/{collection_id}/files", files=files, data=data)
        except V2ClientError as exc:
            # api_post_multipart raises on ALL 4xx, INCLUDING 422. The upload
            # endpoint returns 422 with the full per-file result list when some
            # files are rejected — recover it from the error body so the user
            # still sees which files succeeded vs were rejected.
            if exc.status_code == 422 and isinstance(exc.body, list):
                results = exc.body
                any_error = True
            else:
                typer.echo(f"  {fname}: ERROR — {exc}", err=True)
                any_error = True
                continue

        # results is the per-file list (200, or 422 recovered above).
        if isinstance(results, list):
            for row in results:
                status = row.get("processing_status", "?")
                fid = row.get("file_id", "?")
                typer.echo(f"  {row.get('filename', fname)}: {status} (file_id={fid})")
                if status == "rejected":
                    any_error = True
        else:
            typer.echo(str(results))

    if any_error:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# reingest
# ---------------------------------------------------------------------------


@collections_app.command("cat")
def cat_file(
    collection_id: str = typer.Argument(..., help="Collection id (col_...)"),
    file_id: str = typer.Argument(..., help="File id (cf_...) from `collections show`"),
    as_json: bool = typer.Option(False, "--json", help="Emit the raw preview payload"),
):
    """Print one file's extracted text (requires access to the collection).

    The companion to `collections search`: search finds a file when you know
    a word inside it, this reads one you can already name. The server caps
    the text at ~20k characters — a truncated read says so on stderr rather
    than handing back a silent prefix.
    """
    try:
        out = api_get_json(f"/api/collections/{collection_id}/files/{file_id}/preview")
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(out, indent=2, default=str))
        return

    # Key on the TEXT, not on `kind`. `kind` is the browser modal's switch
    # (draw an image, embed a PDF, print text) — a PDF comes back
    # `kind="pdf"` and still carries the ingested text a reader here wants.
    # Gating on `kind == "text"` refused exactly the format this command
    # exists for.
    text = out.get("text")
    if not text:
        # `reason` is the server's own sentence for "indexed yet?", "rejected",
        # "no extractable text" — relaying it beats inventing a summary.
        typer.echo(out.get("reason") or "No text preview is available for this file.", err=True)
        raise typer.Exit(1)

    typer.echo(text)
    if out.get("truncated"):
        typer.echo(
            f"\n[truncated] Showing the first {len(text)} characters. "
            f"Use `agnes collections search <term> --collection {collection_id}` to reach the rest.",
            err=True,
        )


@collections_app.command("reingest")
def reingest_file(
    collection_id: str = typer.Argument(..., help="Collection id (col_...)"),
    file_id: str = typer.Argument(..., help="File id (cf_...) from `collections show`"),
):
    """Re-run ingestion for one file (requires access to the collection; after fixing the file or config)."""
    try:
        out = api_post_json(f"/api/collections/{collection_id}/files/{file_id}/reingest", {})
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"reingest queued: {out.get('file_id', file_id)} status={out.get('processing_status', '?')}")


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


@collections_app.command("rm")
def remove_collection(
    collection_id: str = typer.Argument(..., help="Collection ID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Soft-delete a collection (admin only)."""
    if not yes:
        confirmed = typer.confirm(f"Delete collection {collection_id}?")
        if not confirmed:
            raise typer.Abort()
    try:
        api_delete(f"/api/collections/{collection_id}")
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"Deleted: {collection_id}")


# ---------------------------------------------------------------------------
# rm-file
# ---------------------------------------------------------------------------


@collections_app.command("rm-file")
def remove_file(
    collection_id: str = typer.Argument(..., help="Collection id (col_...)"),
    file_id: str = typer.Argument(..., help="File id (cf_...) from `collections show`"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete one file from a collection (requires access to the collection)."""
    if not yes:
        confirmed = typer.confirm(f"Delete file {file_id} from {collection_id}?")
        if not confirmed:
            raise typer.Abort()
    try:
        api_delete(f"/api/collections/{collection_id}/files/{file_id}")
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"Deleted file: {file_id}")
