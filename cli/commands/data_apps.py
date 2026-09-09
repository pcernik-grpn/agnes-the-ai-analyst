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
  - ``share <slug>``                  GET    /api/sharing/data_app/{slug}
  - ``share <slug> --group/--everyone/--private``
                                       PUT    /api/sharing/data_app/{slug}
  - ``set-identity <slug> owner|viewer``
                                       PATCH  /api/data-apps/{slug}

``open`` is deliberately print-only — no browser launch — so headless
environments (CI, remote shells) behave identically to a desktop one.

``share`` mirrors the owner-scoped Library sharing API
(``app/api/sharing.py``) for the ``data_app`` resource type: with no mutation
flags it shows the current sharing state (visibility, group names); with
``--group``/``--everyone``/``--private`` it sets the desired END STATE — the
call replaces the whole audience list, it never adds to it.

``set-identity`` sets which identity the app's own server-side data calls run
as (``owner`` — the creator, today's default — or ``viewer`` — the caller
currently loading the app), and redeploys the app when it's already running.
Postgres-backed instances only (A3 PG-first ratchet) — a DuckDB-backed
instance answers a typed 501, surfaced as a friendly message.

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
from pathlib import Path
from typing import Optional

import httpx
import typer

from cli.client import api_delete, api_get, api_patch, api_post, api_put
from cli.config import get_token

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
    # Deploy-time exposure scan (#1946). "deploy_check_failed" itself is
    # handled specially in `deploy_app` (it prints the findings, not this
    # generic message) — this entry only covers it reaching `_fail` some
    # other way (e.g. `--json`-less scripting against a raw response).
    "deploy_check_failed": "The deploy check found issues that block deployment in 'block' mode.",
    "deploy_check_unavailable_external_repo": (
        "This app's source lives outside Agnes, so it can't be scanned — and this server's"
        " data_apps.deploy_checks is set to 'block', which refuses a deploy it can't scan."
        " Ask an admin to switch it to 'warn' (or 'off') for external-repo apps."
    ),
    # Sharing (`app share`) — mirrors app/api/sharing.py's set_shares ValueError.
    "group_not_shareable": "You can only share with a group you belong to, plus Everyone.",
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
    # A3 PG-first ratchet: a PG-only feature (e.g. `set-identity`) on a
    # DuckDB-backed instance — the typed 501 body's `detail` is already a full
    # sentence (see `RequiresPostgresBackend`), but this is a friendlier,
    # consistent one-liner every PG-only CLI command uses.
    if isinstance(body, dict) and body.get("error") == "requires_postgres_backend":
        return (
            "Requires the Postgres app-state backend — this instance still runs the frozen"
            " DuckDB backend. Migrate it (see docs/migrations.md) to use this command."
        )
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    if isinstance(detail, dict):
        # Deploy-time exposure scan (#1946): a structured `{"error": ...}`
        # detail rather than a bare string — look up the code, findings (if
        # any) are printed separately by the caller.
        detail = detail.get("error", "")
    if isinstance(detail, str) and detail.startswith(_RUNNER_ERROR_PREFIX):
        code = detail[len(_RUNNER_ERROR_PREFIX) :]
        hint = _RUNNER_HINTS.get(code)
        return f"{code} — {hint}" if hint else detail
    return _ERROR_MESSAGES.get(detail, detail or resp.text)


def _fail(resp) -> None:
    typer.echo(f"Failed: {_detail(resp)}", err=True)
    raise typer.Exit(1)


def _print_deploy_check(report: Optional[dict]) -> None:
    """Human-readable deploy-check findings — rule id, `file:line`, message.
    Silent when there's nothing to say: no report, a `pass`, or `skipped`."""
    if not report or not report.get("findings"):
        return
    for finding in report["findings"]:
        loc = finding.get("file", "")
        if finding.get("line"):
            loc = f"{loc}:{finding['line']}"
        typer.echo(f"  [{finding.get('rule_id', '?')}] {loc}: {finding.get('message', '')}")


def _not_found(slug: str) -> None:
    typer.echo(f"Data app not found: {slug}", err=True)
    typer.echo("Try: agnes app list  — to see the apps you can access.", err=True)
    raise typer.Exit(1)


def _sharing_not_found(slug: str) -> None:
    """The sharing endpoints 404 (never 403) a caller who doesn't own the app —
    see ``app/api/sharing.py::_require_owned`` — so this reads exactly like a
    missing app, not a permission wall."""
    typer.echo(
        f"Data app not found or not yours: {slug} — only the owner or an Admin can manage sharing.",
        err=True,
    )
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
    if resp.status_code == 422:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        # Deploy-time exposure scan (#1946) in `block` mode: print the
        # findings that caused the refusal, not just the generic message.
        if isinstance(detail, dict) and detail.get("error") == "deploy_check_failed":
            typer.echo(f"Failed: {_ERROR_MESSAGES['deploy_check_failed']}", err=True)
            _print_deploy_check(detail.get("deploy_check"))
            raise typer.Exit(1)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(f"State: {body.get('state', '')}  deployed_sha={body.get('deployed_sha', '')}")
    _print_deploy_check(body.get("deploy_check"))


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


@data_apps_app.command("fetch")
def fetch_app(
    slug: str = typer.Argument(..., help="App slug"),
    path: str = typer.Argument("/", help="Path on the app, e.g. /provenance.json"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write the body to a file instead of stdout"),
    timeout: float = typer.Option(30.0, "--timeout", help="Seconds to wait for the app"),
):
    """GET a path from a hosted data app, authenticated as you.

    Reaches exactly what a browser reaches: the same ingress proxy, the same
    authentication, the same RBAC. It opens no door a grant-holder did not
    already have — the one thing it changes is who holds the credential. The
    CLI resolves your token itself, so it never appears in a command, in shell
    history, or in an agent's transcript. That is the entire point, which is
    why the target is built from the URL the API returns and never from
    anything you type: a `path` that could name a host would turn this into a
    way to post an Agnes token wherever someone asked.

    Reaching the app also requires that the deployment serves apps on their own
    origin (`data_apps.subdomain_base`). Without it the API hands back the
    `/apps/<slug>/` form, which the ingress refuses outright — this says so
    rather than letting a bare 403 surface.
    """
    if "://" in path or path.startswith("//"):
        typer.echo("The path argument is a path on the app, not a URL.", err=True)
        typer.echo("Your Agnes token is only ever sent to the app's own origin.", err=True)
        raise typer.Exit(1)

    resp = api_get(f"/api/data-apps/{slug}")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    base = (resp.json().get("url") or "").strip()
    if not base.startswith(("http://", "https://")):
        typer.echo(f"This deployment does not serve data apps on their own origin (got {base!r}).", err=True)
        typer.echo("Apps are refused on the main origin, so there is nothing to fetch.", err=True)
        typer.echo("An admin sets `data_apps.subdomain_base` to enable it.", err=True)
        raise typer.Exit(1)

    # Resolve against the app root and refuse anything that escapes it. `..`
    # cannot reach the app's filesystem — this is HTTP, not a file read — but a
    # traversal that walks off the origin would still aim the token elsewhere.
    target = httpx.URL(base).join(path)
    if not str(target).startswith(base.rstrip("/") + "/") and str(target).rstrip("/") != base.rstrip("/"):
        typer.echo(f"That path resolves outside the app ({target}).", err=True)
        raise typer.Exit(1)

    try:
        r = httpx.get(
            str(target),
            headers={"Authorization": f"Bearer {get_token() or ''}"},
            timeout=timeout,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"Could not reach the app: {exc}", err=True)
        raise typer.Exit(1)

    if r.status_code != 200:
        typer.echo(f"Failed: HTTP {r.status_code} from {target}", err=True)
        if r.status_code in (502, 503):
            typer.echo("The app may still be waking — try again in a moment.", err=True)
        raise typer.Exit(1)

    if output:
        Path(output).write_bytes(r.content)
        typer.echo(f"Wrote {len(r.content)} bytes to {output}", err=True)
        return
    typer.echo(r.text)


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


# ---------------------------------------------------------------------------
# share
# ---------------------------------------------------------------------------

# The "share with the whole workspace" sentinel — a literal, not a real
# group's uuid — see src.grant_scopes.EVERYONE_TARGET_ID and app/api/sharing.py.
_EVERYONE_TARGET_ID = "everyone"


def _fetch_share_groups() -> list[dict]:
    """Audiences the caller may share into, per ``GET /api/sharing/groups`` —
    ``[{id, name, is_everyone}]``, the "everyone" sentinel included."""
    resp = api_get("/api/sharing/groups")
    if resp.status_code != 200:
        _fail(resp)
    return resp.json()


def _resolve_group_ids(names: list[str], groups: list[dict]) -> list[str]:
    """Resolve each ``--group`` value to a group id — a raw id as-is, a name
    case-insensitively. Unknown values fail loudly, listing what IS available,
    rather than silently dropping an audience the caller asked for."""
    by_id = {g["id"] for g in groups}
    by_name = {g["name"].lower(): g["id"] for g in groups}
    resolved: list[str] = []
    unknown: list[str] = []
    for raw in names:
        if raw in by_id:
            resolved.append(raw)
        elif raw.lower() in by_name:
            resolved.append(by_name[raw.lower()])
        else:
            unknown.append(raw)
    if unknown:
        available = ", ".join(sorted(g["name"] for g in groups))
        typer.echo(f"Unknown group(s): {', '.join(unknown)}. Available: {available}", err=True)
        raise typer.Exit(1)
    return resolved


def _print_share_state(state: dict, groups: list[dict]) -> None:
    names_by_id = {g["id"]: g["name"] for g in groups}
    typer.echo(f"Visibility: {state.get('visibility', '')}")
    group_ids = state.get("group_ids") or []
    if group_ids:
        # A group id with no matching name (not in `/api/sharing/groups`,
        # e.g. one the caller can no longer share into) is printed raw rather
        # than dropped — the audience is still real even if unnamed here.
        typer.echo(f"Shared with: {', '.join(names_by_id.get(gid, gid) for gid in group_ids)}")
    else:
        typer.echo("Shared with: (private)")
    pending = state.get("pending_group_ids") or []
    if pending:
        typer.echo(f"Pending approval: {', '.join(names_by_id.get(gid, gid) for gid in pending)}")


@data_apps_app.command("share")
def share_app(
    slug: str = typer.Argument(..., help="App slug"),
    group: list[str] = typer.Option(
        [], "--group", help="Group name (or id) to share with — repeatable; case-insensitive"
    ),
    everyone: bool = typer.Option(False, "--everyone", help="Share with the whole workspace"),
    private: bool = typer.Option(False, "--private", help="Make the app private again (clears all sharing)"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Show or set who a data app is shared with (owner/Admin only).

    With no flags, prints the current sharing state — visibility plus the
    group names it's shared with. With ``--group``/``--everyone``/
    ``--private``, sets the DESIRED END STATE: the call replaces the whole
    audience list, it does not add to it, so a repeat with a narrower
    ``--group`` set drops the groups left out. ``--private`` clears sharing
    entirely and cannot be combined with ``--group``/``--everyone``.
    """
    if private and (group or everyone):
        typer.echo("--private cannot be combined with --group/--everyone.", err=True)
        raise typer.Exit(1)

    if not group and not everyone and not private:
        resp = api_get(f"/api/sharing/data_app/{slug}")
        if resp.status_code == 404:
            _sharing_not_found(slug)
        if resp.status_code != 200:
            _fail(resp)
        state = resp.json()
        if json:
            typer.echo(json_lib.dumps(state, indent=2, default=str))
            return
        _print_share_state(state, _fetch_share_groups())
        return

    groups = _fetch_share_groups()
    group_ids: list[str] = [] if private else _resolve_group_ids(group, groups)
    if everyone and _EVERYONE_TARGET_ID not in group_ids:
        group_ids.append(_EVERYONE_TARGET_ID)

    resp = api_put(f"/api/sharing/data_app/{slug}", json={"group_ids": group_ids})
    if resp.status_code == 404:
        _sharing_not_found(slug)
    # 403 (`group_not_shareable`) falls through to `_fail`, same as any other
    # unmapped status — `_ERROR_MESSAGES` gives it a friendly line.
    if resp.status_code not in (200, 202):
        _fail(resp)

    state = resp.json()
    if json:
        typer.echo(json_lib.dumps(state, indent=2, default=str))
        return
    _print_share_state(state, groups)
    if resp.status_code == 202:
        typer.echo("(some groups are queued for admin approval — see 'Pending approval' above)")


# ---------------------------------------------------------------------------
# set-identity
# ---------------------------------------------------------------------------


def _print_redeploy(redeploy: dict | None) -> None:
    if not redeploy:
        return
    if not redeploy.get("triggered"):
        typer.echo("Redeploy: not triggered (app has no active deployment).")
        return
    ok = redeploy.get("ok")
    if ok is False:
        detail = redeploy.get("detail", "")
        typer.echo(f"Redeploy: triggered, failed — {detail}" if detail else "Redeploy: triggered, failed")
    else:
        typer.echo("Redeploy: triggered")


@data_apps_app.command("set-identity")
def set_identity(
    slug: str = typer.Argument(..., help="App slug"),
    data_identity: str = typer.Argument(..., help="'owner' or 'viewer'"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Set which identity a data app's own server-side data calls run as (owner/Admin only).

    ``owner`` (today's default) runs the app's data calls as the app's
    creator — the app sees everything its owner can. ``viewer`` narrows that
    to the caller currently loading the app, so a shared app's data access
    follows whoever is looking at it rather than always its creator.
    Redeploys the app when it's already running (see the ``Redeploy:`` line).

    Postgres-backed instances only (A3 PG-first ratchet) — a DuckDB-backed
    instance answers a friendly "requires the Postgres app-state backend"
    error instead.
    """
    if data_identity not in ("owner", "viewer"):
        typer.echo("data_identity must be 'owner' or 'viewer'.", err=True)
        raise typer.Exit(1)

    resp = api_patch(f"/api/data-apps/{slug}", json={"data_identity": data_identity})
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return
    typer.echo(f"Data identity: {body.get('data_identity', data_identity)}")
    _print_redeploy(body.get("redeploy"))
