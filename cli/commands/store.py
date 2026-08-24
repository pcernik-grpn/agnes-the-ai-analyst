"""`agnes store {upload,update,delete,mine}` — Flea Market creator-side ops.

For browsing and installing marketplace items use ``agnes marketplace``.
These commands cover the creator workflow: publish, update, remove, and
download your own entries. All commands authenticate via the configured PAT
(see ``cli auth``); endpoints are gated by ``get_current_user`` server-side.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from src.store_categories import STORE_CATEGORIES

from cli.v2_client import (
    V2ClientError,
    api_delete,
    api_get_json,
    api_get_stream,
    api_post_json,
    api_post_multipart,
    api_put_multipart,
)

store_app = typer.Typer(help="Flea Market — upload and manage your own skills/agents/plugins")


@store_app.command("upload")
def upload_entity(
    type: str = typer.Argument(..., help="skill | agent | plugin"),
    zip_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    name: Optional[str] = typer.Option(None, "--name"),
    description: Optional[str] = typer.Option(None, "--description"),
    category: Optional[str] = typer.Option(
        None,
        "--category",
        help="Category (case-insensitive). One of: " + ", ".join(STORE_CATEGORIES),
    ),
    video_url: Optional[str] = typer.Option(None, "--video-url"),
):
    """Upload a Flea Market entity from a local ZIP file."""
    files = {
        "file": (zip_path.name, zip_path.read_bytes(), "application/zip"),
    }
    data: dict = {"type": type}
    if name:
        data["name"] = name
    if description:
        data["description"] = description
    if category:
        data["category"] = category
    if video_url:
        data["video_url"] = video_url
    try:
        body = api_post_multipart("/api/store/entities", files=files, data=data)
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(
        f"Uploaded: id={body['id']} name={body['name']} invocation={body['invocation_name']} version={body['version']}"
    )
    if body.get("visibility_status") == "pending":
        typer.echo(f"Held for automated review — check progress with: agnes store status {body['id']} --wait")


@store_app.command("publish-md")
def publish_markdown(
    name: str = typer.Argument(..., help="Skill/agent name (lowercase, digits, dashes)"),
    skill_md: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="Path to the Markdown file"),
    type: str = typer.Option("skill", "--type", help="skill | agent"),
    description: Optional[str] = typer.Option(None, "--description"),
    category: Optional[str] = typer.Option(
        None,
        "--category",
        help="Category (case-insensitive). One of: " + ", ".join(STORE_CATEGORIES),
    ),
):
    """Publish a skill or agent from a single Markdown file — no ZIP needed.

    The server wraps the file into the same guardrail + review pipeline as
    ``agnes store upload``.
    """
    payload: dict = {
        "type": type,
        "name": name,
        "skill_md": skill_md.read_text(encoding="utf-8"),
    }
    if description:
        payload["description"] = description
    if category:
        payload["category"] = category
    try:
        body = api_post_json("/api/store/entities/from-markdown", payload)
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"Published: id={body['id']} name={body['name']} version={body['version']}")
    if body.get("visibility_status") == "pending":
        typer.echo(f"Held for automated review — check progress with: agnes store status {body['id']} --wait")


@store_app.command("delete")
def delete_entity(
    entity_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete a Flea Market entity (owner or admin only)."""
    if not yes:
        # A piped answer (`echo y | agnes store delete <id>`) must still work —
        # `typer.confirm` reads it like a terminal would. Only genuine EOF (no
        # input at all) is the shape a chat sandbox, a CI step, or any other
        # non-interactive agent context hits; `typer.confirm` surfaces that as
        # `typer.Abort`, with a bare "Aborted." that names no remedy. Catch
        # only that case and say what to do instead.
        try:
            confirm = typer.confirm(f"Delete entity {entity_id}?")
        except typer.Abort:
            # `click.confirm` raises `Abort` for BOTH EOFError and
            # KeyboardInterrupt, so the exception alone cannot tell "nothing
            # was ever going to answer" from "a person pressed Ctrl-C". On a
            # tty it is the latter: re-raise, so the user gets the plain
            # "Aborted." they asked for rather than being told their terminal
            # does not exist and nudged toward --yes — which would skip the
            # very confirmation they just declined.
            if sys.stdin.isatty():
                raise
            typer.echo(
                f"Refusing to delete {entity_id} without confirmation: no interactive terminal. Re-run with --yes.",
                err=True,
            )
            raise typer.Exit(1)
        if not confirm:
            raise typer.Abort()
    try:
        api_delete(f"/api/store/entities/{entity_id}")
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"Deleted: {entity_id}")


@store_app.command("update")
def update_entity(
    entity_id: str = typer.Argument(...),
    description: Optional[str] = typer.Option(None, "--description"),
    category: Optional[str] = typer.Option(
        None,
        "--category",
        help="Category (case-insensitive). One of: " + ", ".join(STORE_CATEGORIES),
    ),
    video_url: Optional[str] = typer.Option(None, "--video-url"),
    photo: Optional[Path] = typer.Option(
        None,
        "--photo",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Replace the entity's photo with this image file",
    ),
    zip_path: Optional[Path] = typer.Option(
        None,
        "--zip",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Replace the plugin tree with this new ZIP",
    ),
):
    """In-place edit a Flea Market entity. Owner or admin only.

    Server-side authorization (PUT /api/store/entities/{id}) admits the
    owner OR any member of the Admin group; CLI doesn't enforce, the
    server does. Pass any combination of --description / --category /
    --video-url / --photo / --zip; omitted fields are left untouched
    (note: an empty string clears nothing — there's no API affordance to
    clear a field back to NULL via PUT today).
    """
    files: dict = {}
    data: dict = {}
    if zip_path:
        files["file"] = (zip_path.name, zip_path.read_bytes(), "application/zip")
    if photo:
        files["photo"] = (photo.name, photo.read_bytes(), f"image/{photo.suffix.lstrip('.')}")
    if description is not None:
        data["description"] = description
    if category is not None:
        data["category"] = category
    if video_url is not None:
        data["video_url"] = video_url
    if not files and not data:
        typer.echo(
            "Nothing to update — pass at least one of --description / --category / --video-url / --photo / --zip.",
            err=True,
        )
        raise typer.Exit(2)
    try:
        body = api_put_multipart(
            f"/api/store/entities/{entity_id}",
            files=files or None,
            data=data,
        )
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"Updated: id={body['id']} version={body['version']}")


_TERMINAL_SUBMISSION_STATUSES = {
    "approved",
    "blocked_inline",
    "blocked_llm",
    "review_error",
    "overridden",
}


def _print_status(body: dict) -> str:
    sub = body.get("submission") or {}
    status = sub.get("status") or body.get("visibility_status") or "unknown"
    typer.echo(f"{body.get('name')} ({body.get('type')}, id={body.get('entity_id')})")
    typer.echo(f"  visibility: {body.get('visibility_status')}  version_no: {body.get('version_no')}")
    typer.echo(f"  submission: {status}" + (f"  error: {sub['error']}" if sub.get("error") else ""))
    if sub.get("summary"):
        typer.echo(f"  summary: {sub['summary']}")
    if body.get("hint"):
        typer.echo(f"  hint: {body['hint']}")
    return status


@store_app.command("status")
def entity_status(
    entity_id: str = typer.Argument(..., help="Store entity id (from `agnes store upload` output)"),
    wait: bool = typer.Option(False, "--wait", help="Poll until the review reaches a terminal state"),
    timeout: int = typer.Option(600, "--timeout", help="Max seconds to wait with --wait"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show the review-pipeline status of your uploaded entity.

    After `agnes store upload` the guardrail review runs asynchronously
    server-side; the entity stays hidden (`visibility: pending`) until it
    passes. Use `--wait` to block until the verdict lands. Exit codes:
    0 = live (approved/overridden), 1 = blocked or review error,
    2 = still pending (timeout with --wait, or non-terminal without).
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            body = api_get_json(f"/api/store/entities/{entity_id}/status")
        except V2ClientError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1)
        if as_json:
            typer.echo(json.dumps(body, indent=2))
            sub = body.get("submission") or {}
            status = sub.get("status") or body.get("visibility_status") or "unknown"
        else:
            status = _print_status(body)
        if status in ("approved", "overridden"):
            raise typer.Exit(0)
        if status in _TERMINAL_SUBMISSION_STATUSES:
            raise typer.Exit(1)
        if not wait:
            raise typer.Exit(2)
        if time.monotonic() >= deadline:
            typer.echo(f"Review still {status} after {timeout}s — giving up.", err=True)
            raise typer.Exit(2)
        time.sleep(5)


# ---------------------------------------------------------------------------
# `agnes store mine` — bundle of the caller's OWN entities (creator scope).
#
# Whole-Store bulk reads (`pull` / `info`) live under `agnes admin store`
# because operationally they're backup tooling for operators. This stays
# in user namespace because every authenticated user is allowed to grab
# a backup of their own creations (offline archive, leaving the org,
# moving to another instance).
# ---------------------------------------------------------------------------


@store_app.command("mine")
def pull_my_entities(
    out: Path = typer.Option(
        Path("my-store-entities.zip"),
        "-o",
        "--out",
        help="Where to save the ZIP (default: ./my-store-entities.zip)",
    ),
    unpack: Optional[Path] = typer.Option(
        None,
        "--unpack",
        help="Instead of saving the ZIP, unpack it into this directory.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Download a bundle of every Flea Market entity you own (created).

    Server-side this is the same ``GET /api/store/bundle.zip`` endpoint
    that `agnes admin store pull` uses, scoped to the caller via
    ``?owner=me`` (the server resolves the magic value to your user_id).
    """
    import shutil as _shutil
    import tempfile as _tempfile
    import zipfile as _zipfile

    if unpack:
        scratch = Path(_tempfile.mkdtemp(prefix="agnes_store_mine_"))
        zip_path = scratch / "bundle.zip"
        try:
            try:
                api_get_stream("/api/store/bundle.zip", str(zip_path), owner="me")
            except V2ClientError as e:
                typer.echo(str(e), err=True)
                raise typer.Exit(1)
            if unpack.exists():
                _shutil.rmtree(unpack)
            unpack.mkdir(parents=True, exist_ok=True)
            with _zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(unpack)
        finally:
            _shutil.rmtree(scratch, ignore_errors=True)
        if as_json:
            typer.echo(json.dumps({"unpacked_to": str(unpack)}))
        else:
            typer.echo(f"Unpacked your Store entities → {unpack}")
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = api_get_stream("/api/store/bundle.zip", str(out), owner="me")
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps({"bytes": size, "path": str(out)}))
    else:
        typer.echo(f"Wrote {size:,} bytes → {out}")


@store_app.command("rate")
def rate_entity(
    entity_id: str = typer.Argument(...),
    vote: int = typer.Option(
        ...,
        "--vote",
        "-v",
        help="1 = thumbs up, -1 = thumbs down, 0 = clear your vote",
    ),
):
    """Rate a Flea Market entity thumbs up/down (#398).

    Casts, changes, or clears your single vote on an entity. Prints the
    updated ``{up, down, my_vote}`` tally. Server-side gated by
    ``get_current_user``.

    Example: ``agnes store rate <entity_id> --vote -1`` (thumbs down).
    """
    if vote not in (1, -1, 0):
        typer.echo("vote must be 1, -1, or 0", err=True)
        raise typer.Exit(1)
    try:
        body = api_post_json(f"/api/store/entities/{entity_id}/rate", {"vote": vote})
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"up={body['up']} down={body['down']} my_vote={body['my_vote']}")
