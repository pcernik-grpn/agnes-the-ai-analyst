"""FastAPI route serving each internal-mode data app's persistent bare git
repo over git smart-HTTP, with push support.

Registered at `/data-apps.git/{slug}/{path:path}` (GET + POST). Mirrors
`app/marketplace_server/git_router.py`'s CGI-subprocess mechanism (see that
module's docstring for why `git http-backend` is shelled out to as a real
OS subprocess rather than served by a pure-Python git implementation) — the
CGI plumbing (`token_from_basic_auth`, `_build_cgi_env`,
`_run_git_http_backend`) is imported and reused verbatim; only the
authentication/authorization wiring differs:

  - The marketplace endpoint serves one RBAC-filtered repo built fresh per
    caller (no target resource beyond "the marketplace").
  - This endpoint serves a specific app's repo, named in the path
    (`{slug}`), that must already exist on disk (created by the app's
    create-endpoint via `src.data_apps.git_repos.init_app_repo` — this
    router never creates one).

Authorization:
  - **read** (git-upload-pack / clone / fetch): the caller must pass the
    same RBAC gate `require_resource_access(ResourceType.DATA_APP, "{slug}")`
    would apply — owner of the app, Admin, or a group grant on
    `(data_app, <slug>)`.
  - **push** (git-receive-pack): owner or Admin only. A resource grant is
    read access, not write access — granted analysts can pull the app's
    source but not push to it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.auth.access import can_access, is_user_admin
from app.auth.jwt import verify_token
from app.auth.pat_resolver import DATA_APP_GIT_SCOPE_PREFIX, resolve_token_to_user
from app.instance_config import feature_enabled
from app.marketplace_server.git_router import (
    _build_cgi_env,
    _run_git_http_backend,
    token_from_basic_auth,
)
from app.resource_types import ResourceType
from src.data_apps.git_repos import repo_path
from src.repositories import data_apps_repo

logger = logging.getLogger(__name__)

router = APIRouter()


def _disabled() -> Response:
    return Response(
        content=b'{"detail":"data_apps_disabled"}',
        status_code=404,
        media_type="application/json",
    )


def _unauthorized() -> Response:
    return Response(
        content=b"authentication required\n",
        status_code=401,
        media_type="text/plain; charset=utf-8",
        headers={"WWW-Authenticate": 'Basic realm="agnes-data-apps"'},
    )


def _forbidden() -> Response:
    return Response(
        content=b"forbidden\n",
        status_code=403,
        media_type="text/plain; charset=utf-8",
    )


def _not_found() -> Response:
    return Response(
        content=b"not found\n",
        status_code=404,
        media_type="text/plain; charset=utf-8",
    )


def _server_error() -> Response:
    return Response(
        content=b"internal server error\n",
        status_code=500,
        media_type="text/plain; charset=utf-8",
    )


def _is_push_request(request: Request, path: str) -> bool:
    """True for a `git-receive-pack` request (push), by either the smart-HTTP
    `?service=` query param (info/refs negotiation) or the RPC path itself
    (`.../git-receive-pack`)."""
    service = request.query_params.get("service") or ""
    return "git-receive-pack" in service or path.endswith("git-receive-pack")


async def _data_apps_git(slug: str, path: str, request: Request):
    if not feature_enabled("data_apps", "enabled", env_var="AGNES_DATA_APPS_ENABLED", default=False):
        return _disabled()

    token = token_from_basic_auth(request.headers.get("authorization"))
    if not token:
        return _unauthorized()

    def _resolve_auth_and_app():
        """Sync — DB reads (token + app row + RBAC), all routed through the
        `src.repositories` factory (no raw ``conn`` — `resolve_token_to_user`,
        `data_apps_repo()`, `is_user_admin`, and `can_access` each resolve
        the active backend themselves). Run via `run_in_threadpool` so this
        never lands on the shared event loop, same rationale as
        `_marketplace_git._resolve_and_build_repo`.

        `allow_data_app_git_scope=True` is what makes this the one surface a
        `data-app-git:<slug>` credential (minted by
        `app.api.data_apps._mint_git_credential`) can authenticate — every
        other caller of `resolve_token_to_user` defaults to rejecting it.
        Pinned further below: the scope's `<slug>` must match the repo being
        requested, so a credential minted for one app can't be replayed
        against another app's repo."""
        user, _reason = resolve_token_to_user(None, token, allow_data_app_git_scope=True)
        if not user:
            return None, None, None

        from app.auth.session_principal import PRINCIPAL_TYPES

        if isinstance(user, PRINCIPAL_TYPES):
            # A restricted principal (agent session / co-session) is a frozen
            # dataclass with no single caller identity — the owner/admin
            # checks below have nothing sound to run against, and used to
            # raise on `user["id"]` and surface as a raw 500 (#1656). The git
            # surface is owner-authority; fail closed with a clean 403.
            return user, data_apps_repo().get_by_slug(slug), False

        app_row = data_apps_repo().get_by_slug(slug)
        if not app_row:
            return user, None, None

        payload = verify_token(token) or {}
        scope = payload.get("scope") or ""
        if scope.startswith(DATA_APP_GIT_SCOPE_PREFIX) and scope[len(DATA_APP_GIT_SCOPE_PREFIX) :] != slug:
            # Pin the credential to the app it was minted for — a
            # `data-app-git:other-app` token must not authenticate against
            # this app's repo even though it otherwise resolved to a valid
            # user.
            return user, app_row, False

        is_owner = user.get("id") == app_row.get("owner_user_id")
        admin = is_user_admin(user["id"])
        is_push = _is_push_request(request, path)
        if is_push:
            # `git_write: false` denies the push outright, ownership or not.
            # The container's credential is minted FOR the app's owner, so
            # `is_owner` was true for it and "the clone token" was in fact a
            # push credential — non-expiring, and baked into every hosted
            # container's `config.json`, where any code running in the app
            # (including an external repo, or a compromised dependency) can
            # read it. Nothing at this layer could tell it apart from the
            # analyst's own 24-hour authoring PAT: both carry
            # `data-app-git:<slug>`, and the scope encodes WHICH repo, never
            # what may be done to it. Absent claim = writable, so the
            # analyst's credential and every token minted before this release
            # are unaffected. (agnes-reviewer-rbac on this PR.)
            if payload.get("git_write") is False:
                return user, app_row, False
            allowed = is_owner or admin
        else:
            allowed = is_owner or admin or can_access(user["id"], ResourceType.DATA_APP.value, slug)
        return user, app_row, allowed

    user, app_row, allowed = await run_in_threadpool(_resolve_auth_and_app)

    if user is None:
        return _unauthorized()
    if app_row is None:
        return _not_found()
    if not allowed:
        return _forbidden()

    try:
        repo_dir = repo_path(slug)
    except ValueError:
        # Defense-in-depth: get_by_slug already 404'd above for any slug
        # that isn't a real registry row (the practical path for a
        # syntactically-invalid slug like URL-encoded path traversal), but
        # `repo_path`'s own SLUG_RE validation must never surface as a raw
        # 500 if some other caller ever reaches here with a malformed slug.
        return _not_found()
    if not (repo_dir / "HEAD").exists():
        # Registered app row but no repo materialized yet (create-endpoint
        # is Task 7's job — this router never calls init_app_repo itself).
        return _not_found()

    body = await request.body()
    remote_user = user.get("email") or user.get("id")
    env = _build_cgi_env(request, path, repo_dir, remote_user, len(body))

    try:
        status_code, headers, stream = await _run_git_http_backend(env, body)
    except FileNotFoundError:
        logger.exception("git http-backend binary not found")
        return _server_error()
    except Exception:
        logger.exception("git http-backend failed for data app %r", slug)
        return _server_error()

    return StreamingResponse(stream, status_code=status_code, headers=dict(headers))


# Registered as two distinct routes (not one `methods=["GET", "POST"]` route)
# so each method gets its own `operation_id` — see `marketplace_git_get`/
# `marketplace_git_post` for the same rationale (duplicate-operation-id
# warning otherwise).
router.add_api_route(
    "/data-apps.git/{slug}/{path:path}",
    _data_apps_git,
    methods=["GET"],
    operation_id="data_apps_git_get",
)
router.add_api_route(
    "/data-apps.git/{slug}/{path:path}",
    _data_apps_git,
    methods=["POST"],
    operation_id="data_apps_git_post",
)
