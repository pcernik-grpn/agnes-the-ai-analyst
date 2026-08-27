"""`agnes app ...` — manage hosted data apps.

Consumes the control-plane REST surface documented in
``app/api/data_apps.py`` (Task 7 of the data-apps platform plan):

  - ``list``                          GET    /api/data-apps
  - ``show <slug>``                   GET    /api/data-apps/{slug}
  - ``create``                        POST   /api/data-apps
  - ``deploy <slug> [--mode dev]``    POST   /api/data-apps/{slug}/deploy
  - ``git-credential <slug>``         POST   /api/data-apps/{slug}/git-credential
  - ``draft create <slug>``           POST   /api/data-apps/{slug}/drafts
  - ``draft delete <slug> <draft>``   DELETE /api/data-apps/{slug}/drafts/{draft_slug}
  - ``logs <slug>``                   GET    /api/data-apps/{slug}/logs
  - ``open <slug>``                   GET    /api/data-apps/{slug}          (prints url only)
  - ``stop <slug>``                   POST   /api/data-apps/{slug}/stop
  - ``delete <slug>``                 DELETE /api/data-apps/{slug}

``open`` is deliberately print-only — no browser launch — so headless
environments (CI, remote shells) behave identically to a desktop one.

``draft create``/``draft delete``/``git-credential`` are wave 3B's
draft-iteration surface (Task 8) — a draft shares its prod parent's git
repo (a registry sibling row pinned to an iteration branch, not a copy);
deploy it with ``agnes app deploy <draft_slug> --mode dev``.

Secrets management (``PUT /api/data-apps/{slug}/secrets``) and the
admin/scheduler-only ``POST /api/data-apps/reap-idle`` have no CLI command;
see the exemption reasons in
``tests/test_documentation_api_triple_surface.py``.
"""

from __future__ import annotations

import json as json_lib
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_patch, api_post

data_apps_app = typer.Typer(help="Manage hosted data apps")

# Maps the REST `detail` error codes (see app/api/data_apps.py's HTTPException
# call sites) to a human-actionable message. Unknown/unmapped details fall
# back to the raw string so a new server-side error code is never swallowed.
_ERROR_MESSAGES = {
    "app_quota_exceeded": "You've hit your data-app quota for this account. Stop or delete one before creating another.",
    "slug_exists": "That slug is already taken. Pick a different one.",
    "invalid_slug": "Invalid slug — use lowercase letters, numbers, and hyphens only.",
    "reserved_slug": "That slug is reserved for a web-UI route and can't be used for a data app. Pick a different one.",
    "invalid_repo_mode": "Invalid --repo-url/--repo-branch combination.",
    "create_in_progress": "Another create request for your account is already in flight. Try again in a moment.",
    "deploy_empty_repo": "This app's repo has no commits yet — push something before deploying.",
    "external_repo_sha_unsupported": (
        "External-repo apps always deploy HEAD of their configured branch — pinning a specific"
        " sha isn't supported yet. Retry without --sha."
    ),
    "runner_unavailable": "The data-app runner is unavailable right now. Try again shortly, or check `agnes status`.",
    "data_apps_disabled": "Data apps are not enabled on this server. Ask an admin to enable them in instance.yaml.",
    "forbidden": "You don't have access to this data app.",
    "data_app_not_found": "Data app not found.",
    "owner_not_found": "The app's owner account no longer exists on the server.",
    # Wave 3B draft-iteration model (Task 8).
    "parent_is_draft": "This app is itself a draft — drafts can't have their own drafts. Create the draft from the prod app instead.",
    "invalid_branch": "Invalid branch name — use lowercase letters, numbers, dots, underscores, and hyphens only.",
    "dev_requires_draft": "--mode dev only deploys draft apps. Deploy the prod app without --mode, or target the draft's own slug.",
    "prod_on_draft": "This app is a draft — deploy it with --mode dev (drafts have no prod ref to fast-forward).",
    "not_a_draft": "That slug isn't a draft of this app.",
    "parent_has_no_main": "This app's repo has no `main` branch yet — push something before creating a draft.",
    "parent_not_found": "This draft's parent app no longer exists on the server.",
    "path_not_allowed": "That path isn't reachable from here.",
}


# A 502 has two very different halves. `runner_unavailable` (above) means the
# sidecar never answered. `runner_error: <code>` means it did, and named the
# problem — so the code is the lead and must always survive into the output;
# these only append the next step. Unmapped codes still print bare rather than
# being swallowed.
_RUNNER_ERROR_PREFIX = "runner_error: "
_RUNNER_HINTS = {
    "image_not_found": (
        "the runner could not get the runtime image. On a host that has never run a data app"
        " this is usually a cold ~1.3 GB pull that outran the timeout — retry (the partial pull"
        " resumes), or pre-pull the image on the host."
    ),
    "image_not_allowed": (
        "the runtime image isn't on the runner's allowlist — check data_apps.runtime_image"
        " against APPS_RUNNER_IMAGE_PREFIX on the server."
    ),
    "bad_runner_token": (
        "the server and the runner disagree about their shared token — check APPS_RUNNER_TOKEN"
        " matches for both, and that the runner was recreated (not just restarted) after it"
        " last changed."
    ),
    "not_found": "the runner has no container for this app — deploy it first.",
}


def _detail(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return resp.text
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    if isinstance(detail, str) and detail.startswith(_RUNNER_ERROR_PREFIX):
        code = detail[len(_RUNNER_ERROR_PREFIX) :]
        hint = _RUNNER_HINTS.get(code)
        return f"{code} — {hint}" if hint else detail
    return _ERROR_MESSAGES.get(detail, detail or resp.text)


def _fail(resp) -> None:
    typer.echo(f"Failed: {_detail(resp)}", err=True)
    raise typer.Exit(1)


def _not_found(slug: str) -> None:
    typer.echo(f"Data app not found: {slug}", err=True)
    typer.echo("Try: agnes app list  — to see the apps you can access.", err=True)
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@data_apps_app.command("list")
def list_apps(
    limit: int = typer.Option(20, "--limit", help="Max results"),
    linked: bool = typer.Option(False, "--linked", help="Show only linked (externally-hosted) apps"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """List data apps you can see (owner, Admin, or a granted group)."""
    params = {"kind": "linked"} if linked else None
    resp = api_get("/api/data-apps", params=params)
    if resp.status_code != 200:
        _fail(resp)

    apps = resp.json()[:limit]

    if json:
        typer.echo(json_lib.dumps(apps, indent=2, default=str))
        return

    if not apps:
        typer.echo("No data apps found.")
        typer.echo("Try: agnes app create <slug> <name>  — to create one.")
        return

    typer.echo(f"{'SLUG':20s} {'NAME':20s} {'KIND':8s} {'STATE':10s} URL")
    for a in apps:
        typer.echo(
            f"{a.get('slug', ''):20s} {a.get('name', ''):20s} "
            f"{a.get('kind', ''):8s} {a.get('state', ''):10s} {a.get('url', '')}"
        )


@data_apps_app.command("set-description")
def set_description(
    slug: str = typer.Argument(..., help="App slug"),
    description: str = typer.Argument(..., help="New description"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Set an app's description — hosted or linked.

    For a linked app the ingest sync refreshes its synced description and this
    pins a human-authored one the sync won't clobber. For a hosted app there is
    no sync, so this is simply how you change the description after
    ``agnes app create`` seeded it. Owner/Admin only.
    """
    resp = api_patch(f"/api/data-apps/{slug}", json={"description": description})
    if resp.status_code != 200:
        _fail(resp)
    app = resp.json()
    if json:
        typer.echo(json_lib.dumps(app, indent=2, default=str))
        return
    typer.echo(f"Updated {slug}: {app.get('effective_description', '')}")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@data_apps_app.command("show")
def show_app(
    slug: str = typer.Argument(..., help="App slug"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Show detail for one data app."""
    resp = api_get(f"/api/data-apps/{slug}")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    a = resp.json()
    if json:
        typer.echo(json_lib.dumps(a, indent=2, default=str))
        return

    typer.echo(f"Slug:        {a.get('slug', slug)}")
    typer.echo(f"Name:        {a.get('name', '')}")
    if a.get("kind"):
        typer.echo(f"Kind:        {a['kind']}")
    typer.echo(f"State:       {a.get('state', '')}")
    # A failed deploy/stop records WHY in `state_detail` (the runner's own
    # words, via `_handle_runner_failure`), and the REST detail response has
    # carried it all along — but no surface printed it, so the one recorded
    # explanation was invisible everywhere and operators were left guessing
    # from a bare `error`.
    if a.get("state_detail"):
        typer.echo(f"Detail:      {a['state_detail']}")
    typer.echo(f"URL:         {a.get('url', '')}")
    # effective_description = admin-pinned override where present, synced text
    # otherwise — same value every other surface (list, web, MCP) shows.
    if a.get("effective_description") or a.get("description"):
        typer.echo(f"Description: {a.get('effective_description') or a['description']}")
    if a.get("deployed_sha"):
        typer.echo(f"Deployed:    {a['deployed_sha']}")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@data_apps_app.command("create")
def create_app(
    slug: str = typer.Argument(..., help="URL-safe slug"),
    name: str = typer.Argument(..., help="Display name"),
    description: str = typer.Option("", "--description", help="Description"),
    repo_url: Optional[str] = typer.Option(None, "--repo-url", help="External git repo URL — sets repo_mode=external"),
    repo_branch: str = typer.Option("main", "--repo-branch", help="Branch to track (external repo mode only)"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Create a new data app.

    Defaults to an internal, server-hosted git repo (``repo_mode=internal``,
    the server default). Pass ``--repo-url`` to track an external repo
    instead (``repo_mode=external``); ``--repo-branch`` selects which branch
    of that repo is tracked (default ``main``).
    """
    payload: dict = {"slug": slug, "name": name, "description": description}
    if repo_url:
        payload["repo_mode"] = "external"
        payload["repo_url"] = repo_url
        payload["repo_branch"] = repo_branch

    resp = api_post("/api/data-apps", json=payload)
    if resp.status_code != 201:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(f"Created: slug={body.get('slug', slug)}")
    typer.echo(f"Git URL: {body.get('git_url', '')}")


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


@data_apps_app.command("deploy")
def deploy_app(
    slug: str = typer.Argument(..., help="App slug"),
    sha: Optional[str] = typer.Option(
        None, "--sha", help="Deploy this commit sha (default: fast-forward to the tracked branch's latest)"
    ),
    mode: Optional[str] = typer.Option(
        None, "--mode", help="'dev' deploys a draft's pinned branch (default: deploy prod)"
    ),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Deploy (or redeploy) an app — fast-forwards ``agnes-live`` and hands off to the runner.

    ``--mode dev`` deploys a draft app on its pinned iteration branch instead
    (no ``agnes-live`` ref to fast-forward, so ``--sha`` is ignored for a
    draft's own slug).
    """
    payload: dict = {}
    if sha:
        payload["sha"] = sha
    if mode:
        payload["mode"] = mode

    resp = api_post(f"/api/data-apps/{slug}/deploy", json=payload)
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(f"State: {body.get('state', '')}  deployed_sha={body.get('deployed_sha', '')}")


# ---------------------------------------------------------------------------
# git-credential
# ---------------------------------------------------------------------------


@data_apps_app.command("git-credential")
def git_credential(
    slug: str = typer.Argument(..., help="App slug"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Mint a fresh git push credential (clone URL) for an app (owner/Admin)."""
    resp = api_post(f"/api/data-apps/{slug}/git-credential")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(body.get("git_clone_url", ""))


# ---------------------------------------------------------------------------
# draft (sub-group)
# ---------------------------------------------------------------------------

draft_app = typer.Typer(help="Manage data-app drafts")
data_apps_app.add_typer(draft_app, name="draft")


@draft_app.command("create")
def draft_create(
    slug: str = typer.Argument(..., help="PROD app slug"),
    branch: str = typer.Option("init", "--branch", help="Iteration branch name"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Create a draft of a prod app on an iteration branch (owner/Admin).

    The draft shares the prod app's git repo — no second repo, no copy.
    Deploy it with ``agnes app deploy <draft_slug> --mode dev``.
    """
    resp = api_post(f"/api/data-apps/{slug}/drafts", json={"branch": branch})
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 201:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(f"Created draft: slug={body.get('slug', '')}  branch={body.get('branch', branch)}")
    typer.echo(f"Git URL: {body.get('git_clone_url', '')}")


@draft_app.command("delete")
def draft_delete(
    slug: str = typer.Argument(..., help="PROD app slug (the draft's parent)"),
    draft_slug: str = typer.Argument(..., help="Draft's own slug"),
):
    """Tear down a draft of a prod app (owner/Admin)."""
    resp = api_delete(f"/api/data-apps/{slug}/drafts/{draft_slug}")
    if resp.status_code == 404:
        _not_found(draft_slug)
    if resp.status_code != 204:
        _fail(resp)

    typer.echo(f"Deleted draft {draft_slug}")


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


@data_apps_app.command("logs")
def logs_app(
    slug: str = typer.Argument(..., help="App slug"),
    tail: int = typer.Option(200, "--tail", help="Number of trailing log lines"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Show the last N lines of runner logs for an app (owner/Admin only)."""
    resp = api_get(f"/api/data-apps/{slug}/logs", params={"tail": tail})
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(body.get("logs", ""))


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


@data_apps_app.command("open")
def open_app(slug: str = typer.Argument(..., help="App slug")):
    """Print the app's URL. Does NOT launch a browser — headless parity."""
    resp = api_get(f"/api/data-apps/{slug}")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    typer.echo(resp.json().get("url", ""))


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@data_apps_app.command("stop")
def stop_app(
    slug: str = typer.Argument(..., help="App slug"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Stop a running app."""
    resp = api_post(f"/api/data-apps/{slug}/stop")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(f"State: {body.get('state', '')}")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@data_apps_app.command("delete")
def delete_app(
    slug: str = typer.Argument(..., help="App slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete a data app (runner stop + service-token revoke + registry row delete)."""
    if not yes:
        confirmed = typer.confirm(f"Delete data app {slug}?")
        if not confirmed:
            raise typer.Abort()

    resp = api_delete(f"/api/data-apps/{slug}")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 204:
        _fail(resp)

    typer.echo(f"Deleted: {slug}")
