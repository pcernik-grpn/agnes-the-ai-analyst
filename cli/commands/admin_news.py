"""`agnes admin news {show,draft,edit,publish,unpublish,versions,export}`.

Talks to the live server through the `/api/admin/news/*` endpoints
(PAT-authed). Direct-DB access is the wrong contract here because the
running server holds the DuckDB write lock — the CLI must coexist with
it. Same pattern as `agnes admin add-user`, `agnes admin set-password`,
etc.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from cli.client import api_get, api_post, api_put

admin_news_app = typer.Typer(help="Admin: edit the /home + /news content")


def _exit_on_error(resp, expected_status: tuple[int, ...] = (200,)) -> dict:
    """Print server-side error detail and exit if `resp.status_code` is
    outside `expected_status`. Returns the parsed JSON body otherwise."""
    if resp.status_code in expected_status:
        try:
            return resp.json()
        except Exception:
            return {}
    detail = ""
    try:
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else body
    except Exception:
        detail = resp.text
    typer.echo(f"server returned {resp.status_code}: {detail}", err=True)
    raise typer.Exit(2)


def _print_row(row: dict | None, label: str = "news") -> None:
    if not row or row.get("published") is False and not row.get("intro") and not row.get("content"):
        # `current` returns {published: False} envelope when nothing's published.
        if row is None or (isinstance(row, dict) and row.get("published") is False and "version" not in row):
            typer.echo(f"({label}: none)")
            return
    typer.echo(f"version    : {row.get('version')}")
    typer.echo(f"status     : {'published' if row.get('published') else 'draft'}")
    typer.echo(f"created_at : {row.get('created_at') or ''}")
    typer.echo(f"updated_at : {row.get('updated_at') or ''}")
    typer.echo(f"created_by : {row.get('created_by') or ''}")
    if row.get("published"):
        typer.echo(f"pub_at     : {row.get('published_at') or ''}")
        typer.echo(f"pub_by     : {row.get('published_by') or ''}")
    typer.echo("-- intro --")
    typer.echo(row.get("intro") or "")
    typer.echo("-- content --")
    typer.echo(row.get("content") or "")


def _load_from_file(path: str) -> tuple[str, str]:
    """Parse `--from FILE` (or `-` for stdin). YAML or JSON object with
    `intro` + `content` keys. Returns (intro, content)."""
    if path == "-":
        text = sys.stdin.read()
    else:
        text = Path(path).read_text(encoding="utf-8")
    # Try YAML first (it is a superset of JSON). Fall back to JSON only
    # for YAMLError to avoid swallowing unrelated exceptions like
    # ImportError or KeyboardInterrupt.
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise typer.BadParameter("file must contain a mapping with `intro` and `content` keys")
    return str(data.get("intro") or ""), str(data.get("content") or "")


@admin_news_app.command("show")
def show(
    version: int = typer.Option(None, "--version", "-v", help="Show a specific version (default: current published)"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Print the current published version, or a specific version."""
    if version is not None:
        resp = api_get(f"/api/admin/news/versions/{version}")
    else:
        resp = api_get("/api/admin/news/current")
    body = _exit_on_error(resp)
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    if isinstance(body, dict) and body.get("published") is False and "version" not in body:
        typer.echo("(published: none)")
        return
    _print_row(body, label="published")


@admin_news_app.command("draft")
def draft():
    """Print the active draft (or 'no draft' if none)."""
    resp = api_get("/api/admin/news/draft")
    if resp.status_code == 404:
        typer.echo("(draft: none)")
        return
    body = _exit_on_error(resp)
    _print_row(body, label="draft")


@admin_news_app.command("edit")
def edit(
    intro: str = typer.Option(None, "--intro", help="Intro HTML (used on /home)"),
    content: str = typer.Option(None, "--content", help="Full content HTML (used on /news)"),
    from_file: str = typer.Option(None, "--from", help="Read {intro, content} from YAML/JSON file (`-` for stdin)"),
    expect_version: int = typer.Option(
        None,
        "--expect-version",
        help="Refuse the edit unless the active draft is at this version "
        "(0 = expect no draft). Guards against overwriting a draft a "
        "concurrent admin started or edited after your last fetch.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip the local collision check that warns when a draft was "
        "created by another author. --expect-version still applies.",
    ),
):
    """Upsert the active draft. One of --from / (--intro and --content) is required.

    Without --expect-version or --force the CLI checks the active draft
    first and refuses to overwrite a draft authored by someone else,
    printing what's there. Pass --expect-version N (matching the version
    you reviewed) to confirm the overwrite, or --force to commit anyway.
    """
    if from_file is not None:
        intro_v, content_v = _load_from_file(from_file)
    elif intro is None and content is None:
        typer.echo("error: provide --from FILE or --intro / --content", err=True)
        raise typer.Exit(2)
    else:
        intro_v = intro or ""
        content_v = content or ""

    # Local collision check before sending the write. The server-side
    # expected_version guard is the authoritative race-free check; this
    # local pass exists so the CLI can show a friendlier "draft was
    # started by X" hint when the operator forgot --force.
    if not force and expect_version is None:
        resp = api_get("/api/admin/news/draft")
        if resp.status_code == 200:
            existing = resp.json()
            # Caller's identity comes from the PAT they're authenticated
            # with — we don't have that locally, so just show the draft
            # author and let the operator decide.
            typer.echo(
                f"warning: active draft v{existing['version']} was started "
                f"by {existing.get('created_by') or '(unknown)'} "
                f"(updated {existing.get('updated_at') or ''}). "
                f"Saving will overwrite their changes. Re-run with "
                f"`--expect-version {existing['version']}` to confirm or "
                f"`--force` to suppress this check.",
                err=True,
            )
            raise typer.Exit(2)

    qs = ""
    if expect_version is not None:
        qs = f"?expected_version={expect_version}"
    resp = api_put(
        f"/api/admin/news/draft{qs}",
        json={"intro": intro_v, "content": content_v},
    )
    if resp.status_code == 409:
        body = resp.json().get("detail") or {}
        typer.echo(
            f"error: version conflict: expected v{body.get('expected')}, "
            f"active draft is v{body.get('actual')} "
            f"(by {body.get('actual_by') or '?'}). "
            f"Run `agnes admin news draft` to inspect.",
            err=True,
        )
        raise typer.Exit(2)
    body = _exit_on_error(resp)
    typer.echo(f"saved draft v{body['version']}")


@admin_news_app.command("publish")
def publish(
    version: int = typer.Option(
        None,
        "--version",
        help="Refuse to publish unless the active draft is at this version. "
        "Guards against publishing a draft a concurrent admin replaced "
        "after you reviewed it. Without --version, publishes whatever "
        "the active draft is.",
    ),
):
    """Publish the active draft.

    With `--version N`, the publish only proceeds when the active draft
    is exactly v{N}. If another admin replaced the draft since your
    last review, the call refuses and tells you what's now active.
    """
    qs = "" if version is None else f"?expected_version={version}"
    resp = api_post(f"/api/admin/news/publish{qs}")
    if resp.status_code == 409:
        body = resp.json().get("detail")
        if body == "no_draft":
            typer.echo("no active draft to publish", err=True)
            raise typer.Exit(1)
        if isinstance(body, dict) and body.get("error") == "version_conflict":
            typer.echo(
                f"error: version conflict: expected draft v{body.get('expected')}, "
                f"active draft is v{body.get('actual')} "
                f"(by {body.get('actual_by') or '?'}). "
                f"Inspect with `agnes admin news draft`, then re-run with the "
                f"matching `--version`.",
                err=True,
            )
            raise typer.Exit(2)
    body = _exit_on_error(resp)
    typer.echo(f"published v{body['version']}")


@admin_news_app.command("unpublish")
def unpublish(
    version: int = typer.Argument(..., help="Version number to unpublish (becomes a draft)"),
):
    """Flip a published version back to draft state."""
    resp = api_post(f"/api/admin/news/unpublish/{version}")
    if resp.status_code == 404:
        typer.echo(f"version {version} not found", err=True)
        raise typer.Exit(1)
    if resp.status_code == 409:
        typer.echo(resp.json().get("detail") or "conflict", err=True)
        raise typer.Exit(2)
    body = _exit_on_error(resp)
    typer.echo(f"unpublished v{body['version']}")


@admin_news_app.command("versions")
def versions(
    limit: int = typer.Option(20, "--limit", help="Max rows to print"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Table of versions: number, status, created_at, by, published_at."""
    resp = api_get(f"/api/admin/news/versions?limit={limit}")
    body = _exit_on_error(resp)
    rows = body.get("versions", [])
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("(no versions)")
        return
    typer.echo(f"{'v':>4}  {'status':<10}  {'created':<19}  {'by':<28}  {'published':<19}  intro")
    typer.echo("-" * 110)
    for r in rows:
        ca = (r.get("created_at") or "")[:19]
        pa = (r.get("published_at") or "")[:19]
        typer.echo(
            f"{r['version']:>4}  {r['status']:<10}  {ca:<19}  {(r.get('created_by') or ''):<28}  "
            f"{pa:<19}  {r.get('intro_preview') or ''}"
        )


@admin_news_app.command("export")
def export(path: str = typer.Argument(..., help="YAML file to write {intro, content} into")):
    """Dump the currently-published version to a YAML file."""
    import yaml

    resp = api_get("/api/admin/news/current")
    body = _exit_on_error(resp)
    if isinstance(body, dict) and body.get("published") is False and "version" not in body:
        typer.echo("no published version to export", err=True)
        raise typer.Exit(1)
    Path(path).write_text(
        yaml.safe_dump(
            {"intro": body.get("intro") or "", "content": body.get("content") or ""},
            allow_unicode=True,
            sort_keys=False,
            width=200,
        ),
        encoding="utf-8",
    )
    typer.echo(f"exported v{body.get('version')} to {path}")
