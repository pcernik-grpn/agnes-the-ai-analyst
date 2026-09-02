"""`agnes collections` — manage file corpora (Collections Slice 2).

Commands:
  create  --name ... [--description ...] [--json]
  edit    <id> [--name ...] [--description ...] [--slug ...] [--json]
  list    [--json]
  show    <id>       [--limit N] [--offset N] [--q TERM] [--json]
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
    api_patch_json,
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
    """Create a new file collection (any signed-in user; you own what you create)."""
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
# edit
# ---------------------------------------------------------------------------


@collections_app.command("edit")
def edit_collection(
    collection_id: str = typer.Argument(..., help="Collection id (col_...) from `collections list`"),
    name: Optional[str] = typer.Option(None, "--name", help="New display name"),
    description: Optional[str] = typer.Option(
        None,
        "--description",
        "-d",
        help='New description; pass an empty string ("") to clear it',
    ),
    slug: Optional[str] = typer.Option(
        None, "--slug", help="New URL slug (normalised to [a-z0-9-]); changes the /library/<slug> URL"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Edit a collection's name, description or slug (owner or admin).

    Only the options you pass are changed — anything omitted is left alone.
    Renaming does NOT move the slug, so links already handed out keep working;
    pass --slug as well when you want the URL to follow the name.
    """
    payload: dict = {}
    # Presence, not truthiness: `--description ""` is a deliberate clear, and
    # dropping it here would make that the one edit the CLI cannot express.
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if slug is not None:
        payload["slug"] = slug
    if not payload:
        typer.echo(
            "Nothing to change. Pass at least one of --name, --description, --slug "
            f"— e.g. agnes collections edit {collection_id} --name 'Q3 contracts'",
            err=True,
        )
        raise typer.Exit(1)

    try:
        body = api_patch_json(f"/api/collections/{collection_id}", payload)
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return
    typer.echo(
        f"Updated: id={body['id']}  slug={body.get('slug', '')}  name={body['name']}"
        f"  changed={','.join(sorted(payload))}"
    )


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
    limit: int = typer.Option(25, "--limit", help="Max files to show in this page (server clamps to 1-200)"),
    offset: int = typer.Option(0, "--offset", help="Skip this many files (for pagination)"),
    q: Optional[str] = typer.Option(None, "--q", help="Filter files by a filename/path substring (case-insensitive)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Show detail + a page of files for a collection.

    The file list is paginated (default 25) — a large collection can hold
    far more files than fit on one page. When the footer says the list was
    cut short, pass the suggested `--offset` for the next page. `--q`
    filters by a filename/path substring; it does not search file
    CONTENTS — use `agnes collections search` for that.
    """
    try:
        detail = api_get_json(f"/api/collections/{collection_id}")
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    # A blank `--q` means "no filter", never "match nothing" — omit it
    # entirely rather than sending `q=""` to the server.
    q_clean = (q or "").strip()
    params: dict = {"limit": limit, "offset": offset}
    if q_clean:
        params["q"] = q_clean
    try:
        files_page = api_get_json(f"/api/collections/{collection_id}/files", **params)
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    files = files_page.get("files", [])
    total = files_page.get("total", len(files))
    page_limit = files_page.get("limit", limit)
    page_offset = files_page.get("offset", offset)

    if as_json:
        out = dict(detail)
        out.pop("files_total", None)
        out.pop("files_truncated", None)
        out["files"] = files
        out["total"] = total
        out["limit"] = page_limit
        out["offset"] = page_offset
        typer.echo(json_lib.dumps(out, indent=2, default=str))
        return

    typer.echo(f"ID:          {detail['id']}")
    typer.echo(f"Slug:        {detail.get('slug', '')}")
    typer.echo(f"Name:        {detail['name']}")
    if detail.get("description"):
        typer.echo(f"Description: {detail['description']}")
    typer.echo(f"Created by:  {detail.get('created_by', '')}")

    typer.echo(f"\nFiles ({total}):")
    if not files:
        if q_clean:
            typer.echo(f"  (none matched --q {q_clean!r} — try a different term, or drop --q to see all files)")
        else:
            typer.echo("  (none)")
        return
    typer.echo(f"  {'FILE_ID':20s}  {'STATUS':10s}  {'SIZE':8s}  FILENAME")
    for f in files:
        size = f.get("size_bytes") or 0
        typer.echo(f"  {f['file_id']:20s}  {f['processing_status']:10s}  {size:8d}  {f['filename']}")

    shown_through = page_offset + len(files)
    if shown_through < total:
        typer.echo(f"\nShowing {len(files)} of {total} files. Use --offset {shown_through} for the next page.")


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
    """Soft-delete a collection (owner or admin)."""
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
