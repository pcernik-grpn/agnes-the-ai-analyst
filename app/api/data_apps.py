"""Control-plane REST API for hosted data apps (v96 registry).

Composes everything the earlier data-apps tasks built: the ``data_apps``
registry (``src/repositories/data_apps.py``), the container-spec / runtime
config.json builders (``src/data_apps/spec.py``), the git-repo lifecycle
helpers (``src/data_apps/git_repos.py``), the sidecar HTTP client
(``src/data_apps/runner_client.py``), and the secret vault
(``app/secrets_vault.py``).

Endpoints (see ``docs/superpowers/plans/2026-07-21-data-apps-platform.md``
Task 7 for the full design rationale):

  - ``GET    /api/data-apps``                — list apps the caller can see
    (drafts excluded — see the ``{slug}`` detail's inlined ``drafts`` field)
  - ``POST   /api/data-apps``                — create (quota + slug checks)
  - ``GET    /api/data-apps/{slug}``          — detail (RBAC-gated); prod
    apps carry an inlined ``drafts: [...]`` list
  - ``POST   /api/data-apps/{slug}/deploy``   — fast-forward + mint service
    token + build spec + hand to the runner sidecar
  - ``POST   /api/data-apps/{slug}/stop``     — runner stop, state -> stopped
  - ``DELETE /api/data-apps/{slug}``          — runner stop + token revoke +
    row delete (repo directory intentionally left on disk); cascades to any
    live drafts
  - ``POST   /api/data-apps/{slug}/drafts``   — create a draft copy on a
    branch (owner/Admin of the parent)
  - ``DELETE /api/data-apps/{slug}/drafts/{draft_slug}`` — tear down one
    draft (owner/Admin of the parent)
  - ``PUT    /api/data-apps/{slug}/secrets``  — encrypt + store secrets
  - ``GET    /api/data-apps/{slug}/logs``     — runner logs (owner/Admin)
  - ``GET    /api/data-apps/{slug}/readiness``— any RBAC-passing caller
  - ``POST   /api/data-apps/{slug}/preview-grant`` — mint a short-TTL
    ``data-app-preview:<slug>`` cookie for the in-chat preview iframe (wave
    3C, spec §7); any RBAC-passing caller (view access is enough)
  - ``POST   /api/data-apps/reap-idle``       — admin-only idle sweep

RBAC: owner of the app, Admin (god-mode), or a group holding a
``resource_grants`` row on ``(data_app, <slug>)`` may *view*; only owner or
Admin may mutate (deploy/stop/delete/secrets/logs).

``_runner()`` is a module-level indirection (not a constructed singleton) so
tests can monkeypatch ``app.api.data_apps._runner`` with a stub — the single
seam the whole feature's tests rely on.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re as _re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import exc as sa_exc

from app.auth.access import can_access, is_user_admin, require_admin
from app.auth.dependencies import (
    _get_db,
    get_current_user,
    reject_keboola_header_credential,
    revocable_credential_id,
)
from app.auth.jwt import create_access_token
from app.auth.pat_resolver import DATA_APP_PREVIEW_SCOPE_PREFIX, PARENT_TOKEN_ID_CLAIM
from app.instance_config import feature_enabled, get_data_apps_config, get_public_url
from app.resource_types import ResourceType
from app.secrets_vault import VaultKeyNotConfiguredError, decrypt_secret, encrypt_secret
from src.data_apps.git_repos import fast_forward_live, init_app_repo
from src.data_apps.runner_client import RunnerClient, RunnerError, RunnerUnavailable, up_timeout
from src.data_apps.spec import AGNES_INTERNAL_URL, RESERVED_SLUGS, SLUG_RE, build_config_json, build_container_spec
from src.repositories import access_token_repo, audit_repo, data_apps_repo, users_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data-apps", tags=["data-apps"])

# Branch names accepted by `POST /{slug}/drafts` — a conservative git
# ref-component charset (lowercase alnum + `.` `_` `/` `-`), not the full
# git-check-ref-format grammar; good enough to reject shell/path-hostile
# input before it reaches `ensure_branch`'s `update-ref` call. This charset
# check alone still admits several forms `git-check-ref-format` refuses
# (`a..b`, `a//b`, a trailing `/` or `.`, an `x.lock` suffix) — those are
# rejected by `_is_git_valid_branch` below, checked alongside this regex.
_BRANCH_RE = _re.compile(r"^[a-z0-9][a-z0-9._/-]{0,60}$")


def _is_git_valid_branch(branch: str) -> bool:
    """Reject branch names `_BRANCH_RE`'s charset check lets through but
    `git update-ref` itself refuses (``man git-check-ref-format``, abridged
    to the rules a lowercase-alnum-`.`-`_`-`/`-`-` charset can still hit):
    a ``..`` component separator, a ``//`` empty path component, a
    trailing ``/`` or ``.``, and an ``x.lock`` suffix (reserved for git's
    own lockfiles). Checked *before* `ensure_branch` runs so a git-invalid
    name never reaches `subprocess.run(..., check=True)` and surfaces as an
    unhandled `CalledProcessError` (belt-and-braces: `create_draft` also
    catches that exception, in case some other git-invalid form slips past
    this check)."""
    return not (
        ".." in branch or "//" in branch or branch.endswith("/") or branch.endswith(".") or branch.endswith(".lock")
    )


# idle_timeout_s clamp — 5 minutes .. 24 hours. Prevents an accidental 0/huge
# value from either reaping an app instantly or never reaping it at all.
_IDLE_TIMEOUT_MIN = 300
_IDLE_TIMEOUT_MAX = 86400

# A row stuck in `deploying` longer than this (updated_at) is recovered by
# reap-idle rather than left wedged forever — see reap_idle_data_apps. Covers
# both wake paths: the ingress proxy's fire-and-forget recreate-mode wake
# (`app/api/data_apps_proxy.py::_spawn_wake`) whose background task died
# without a caller left to notice, and an operator-triggered `POST .../deploy`
# whose request process crashed mid-flight. 10 minutes is generous relative to
# a normal container pull/start, short enough that an operator doesn't wait a
# full idle_timeout_s cycle to find out a wake silently failed.
_DEPLOY_STALE_TIMEOUT_S = 600

# Marker the reconcile scan leaves in `state_detail` on the FIRST sweep that
# finds a `running` row's container dead, so `error` is only written once a
# second sweep agrees. Written with state unchanged (`running`), which bumps
# `updated_at` and therefore defers the confirming look by a whole start grace —
# the spacing that keeps Docker's restart backoff from reading as a dead app.
_RECONCILE_PENDING = "reconcile-pending"

# Fallback values for keys instance.yaml's `data_apps:` block may omit —
# mirrors the documented defaults in config/instance.yaml.example so an
# operator who only sets `enabled: true` still gets a working feature.
_CONFIG_DEFAULTS = {
    "runtime_image": "keboolapublic.azurecr.io/data-app-python-js:1.6.2_python-3.13_node-24",
    "subdomain_base": "",
    # Serve hosted apps on the MAIN origin (same origin as the Agnes `/api`)?
    # OFF by default, deliberately: a hosted app runs user-authored JS, and on
    # the main origin that JS shares the viewer's browsing context — it can
    # `fetch('/api/...', {credentials:'include'})` with the viewer's own Agnes
    # session cookie and READ the response (mint a PAT, read admin config),
    # because same-origin reads need no CORS and the `CsrfOriginMiddleware`
    # only refuses CROSS-origin state changes. No response header closes this
    # (a same-origin `window.open`/`<iframe>` to `/api` is DOM-readable and
    # ungoverned by CSP), so the only real fix is origin isolation. Configure
    # `subdomain_base` to serve apps from their own origin (where the existing
    # CORS + CsrfOrigin defenses contain the attack); requests that arrive on a
    # data-app subdomain are always served. The in-chat preview does NOT need
    # this flag — it is served same-origin only for a caller holding a per-app
    # `data-app-preview:<slug>` token (see `data_apps_proxy._same_origin_serving_
    # refused`). This flag is the explicit escape hatch for serving ALL apps
    # same-origin to everyone (trusted authors only) — see
    # `app/api/data_apps_proxy.py` and docs/architecture.md#hosted-data-apps.
    "allow_same_origin": False,
    "default_idle_timeout_s": 1800,
    "default_sleep_mode": "recreate",
    "default_mem_limit": "1g",
    "default_cpus": 1.0,
    "max_apps_per_user": 3,
    # Container-hardening posture — instance-wide, never per-app-overridable
    # (an app author choosing their own sandbox escape hatch would defeat the
    # point). `cap_drop: ALL` / `no-new-privileges` are unconditional and have
    # no knob; only these two are configurable. `container_read_only` is off
    # by default because the read-only rootfs needs a tmpfs list verified
    # against the shipped nginx+supervisord runtime image — see
    # `src/data_apps/spec.py::build_container_spec`.
    "container_read_only": False,
    "container_pids_limit": 512,
}

# `POST /api/data-apps` quota-check-then-create serialization. Short TTL —
# the lease is only held for the duration of one create request, never
# renewed; ttl_s is a crash-safety backstop, not the expected hold time.
_CREATE_LEASE_TTL_S = 10
_CREATE_LEASE_RETRIES = 3
_CREATE_LEASE_RETRY_DELAY_S = 0.1


def _runner() -> RunnerClient:
    return RunnerClient()


def _acquire_create_lease(user_id: str) -> tuple[bool, str, str]:
    """Serialize concurrent `POST /api/data-apps` calls for the same user so
    the count-then-create quota check can't race (two concurrent requests
    both observe `count < max_apps_per_user` and both proceed, landing the
    user over quota).

    Returns `(held, lease_name, holder)`. `held=False` with no exception
    means either the coordination backend is unavailable (single-process
    dev fallback: proceed unserialized rather than hard-fail create
    entirely) — logged, not raised. Once retries are exhausted against a
    lease actually held by a concurrent request, raises 409
    `create_in_progress` instead of returning.
    """
    from app.coordination.base import CoordinationUnavailable
    from app.coordination.factory import coordination
    from app.coordination.leases import default_holder_id

    lease_name = f"dataapp:create:{user_id}"
    holder = default_holder_id()
    try:
        for attempt in range(_CREATE_LEASE_RETRIES):
            if coordination().lease_acquire(lease_name, holder, ttl_s=_CREATE_LEASE_TTL_S):
                return True, lease_name, holder
            if attempt < _CREATE_LEASE_RETRIES - 1:
                time.sleep(_CREATE_LEASE_RETRY_DELAY_S)
    except CoordinationUnavailable:
        logger.warning("create-lease: coordination backend unavailable; proceeding unserialized")
        return False, lease_name, holder
    raise HTTPException(status_code=409, detail="create_in_progress")


def _release_create_lease(lease_name: str, holder: str) -> None:
    from app.coordination.base import CoordinationUnavailable
    from app.coordination.factory import coordination

    try:
        coordination().lease_release(lease_name, holder)
    except CoordinationUnavailable:
        pass


# `POST /{slug}/deploy`, `POST /{slug}/stop`, `DELETE /{slug}`, the
# scheduler's idle-reap sweep, and the ingress proxy's wake-on-request path
# (`_trigger_wake` in `app/api/data_apps_proxy.py`) all end up calling the
# runner sidecar's `up()`/`stop()` for the same slug.
# `services/apps_runner/api.py::up()` does an unlocked check-then-act (get
# old container -> remove -> run new) — two of these calls racing for the
# same slug can both observe the same "old" container and both call
# `containers.run(...)`, landing two containers fighting over the same
# name/network. `dataapp:op:{slug}` is the single lease shared by all these
# call sites so at most one runner-mutating operation is ever in flight per
# app. The idle-reap sweep uses the non-blocking `try_acquire_op_lease`
# directly (skip-and-retry-next-tick) rather than `require_op_lease`
# (retry-then-409) since it has no HTTP caller to return an error to.
#
# It intentionally lives here rather than inside `redeploy_current`:
# `_trigger_wake` and `deploy_data_app` both call `redeploy_current`, and
# each already holds this lease itself before doing so — acquiring it
# again inside `redeploy_current` would be a self-deadlock (`lease_acquire`
# is not reentrant for the same holder, see `CoordinationBackend`'s
# docstring).
#
# The TTL is DERIVED from the runner client's `up` budget rather than being a
# flat number of its own: the lease has to outlive the longest operation it
# serializes, or it stops serializing anything. A cold host pulls the ~1.3 GB
# runtime image inside `runner.up()` — budgeted at `up_timeout()` (600 s by
# default, `APPS_RUNNER_UP_TIMEOUT` for slower links) — so a flat 120 s lease
# would lapse mid-deploy and let a concurrent deploy/stop/delete/wake in on
# exactly the app whose container is still being created: the unlocked
# check-then-act race in `services/apps_runner/api.py::up()` that this lease
# exists to prevent. The wake path (`_trigger_wake`) never releases the lease
# explicitly at all — it relies on the TTL alone — so an under-sized TTL is
# not merely a narrow window there but the whole guarantee.
#
# The margin covers everything `deploy_data_app` does around the runner call
# (token mint, spec build, state writes) inside the same lease.
_OP_LEASE_MARGIN_S = 60.0
_OP_LEASE_RETRIES = 3
_OP_LEASE_RETRY_DELAY_S = 0.1


def op_lease_ttl_s() -> float:
    """TTL for `dataapp:op:{slug}`, always above the runner's `up` budget.

    Read at acquire time (not import time) so an operator's
    `APPS_RUNNER_UP_TIMEOUT` is honored without a code change.
    """
    return up_timeout() + _OP_LEASE_MARGIN_S


def _op_lease_name(slug: str) -> str:
    return f"dataapp:op:{slug}"


def try_acquire_op_lease(slug: str) -> tuple[bool, str]:
    """One non-blocking attempt to acquire the per-slug op lease.

    Used by `_trigger_wake`, which must never block the ingress request
    on another in-flight operation — losing the race just means
    returning immediately (the caller renders the holding page either
    way), same as the wake-specific lease this replaces. Synchronous
    endpoints that need retry-then-409 semantics instead call
    `require_op_lease`.

    Returns `(acquired, holder)`. On `CoordinationUnavailable` (no
    cross-process backend configured), treats the lease as acquired —
    single-process dev fallback: proceed unserialized rather than
    refusing the operation just because coordination happens to be down.
    """
    from app.coordination.base import CoordinationUnavailable
    from app.coordination.factory import coordination
    from app.coordination.leases import default_holder_id

    holder = default_holder_id()
    try:
        acquired = coordination().lease_acquire(_op_lease_name(slug), holder, ttl_s=op_lease_ttl_s())
    except CoordinationUnavailable:
        return True, holder
    return acquired, holder


def release_op_lease(slug: str, holder: str) -> None:
    from app.coordination.base import CoordinationUnavailable
    from app.coordination.factory import coordination

    try:
        coordination().lease_release(_op_lease_name(slug), holder)
    except CoordinationUnavailable:
        pass


def require_op_lease(slug: str) -> str:
    """Synchronous-endpoint policy for `deploy_data_app`/`stop_data_app`: a
    few quick retries against `try_acquire_op_lease`, then 409
    `operation_in_progress` if the lease is still held by someone else
    (a concurrent deploy/stop request, or an in-flight wake). The retries
    only smooth over near-simultaneous requests about to release on their
    own — a genuinely in-flight operation (e.g. a wake's backgrounded
    redeploy, held for up to `op_lease_ttl_s()`) is expected to make the
    caller retry later, not block the request for the full TTL.

    Returns the holder id to pass to `release_op_lease` in a `finally`.
    """
    holder = ""
    for attempt in range(_OP_LEASE_RETRIES):
        acquired, holder = try_acquire_op_lease(slug)
        if acquired:
            return holder
        if attempt < _OP_LEASE_RETRIES - 1:
            time.sleep(_OP_LEASE_RETRY_DELAY_S)
    raise HTTPException(status_code=409, detail="operation_in_progress")


def _effective_config() -> dict:
    return {**_CONFIG_DEFAULTS, **get_data_apps_config()}


def same_origin_serving_allowed() -> bool:
    """Whether hosted apps may be served on the MAIN origin (same origin as
    the Agnes `/api`), where a hosted app's JS shares the viewer's session.

    Off by default (`data_apps.allow_same_origin`, or
    ``AGNES_DATA_APPS_ALLOW_SAME_ORIGIN``); see `_CONFIG_DEFAULTS` for why
    same-origin serving is unsafe. The proxy calls this to decide whether a
    request that did NOT arrive on a data-app subdomain may be served
    (`app/api/data_apps_proxy.py`). A request that arrived on a subdomain is
    already on an isolated origin and is served regardless of this flag.

    Resolved through the switch registry (`app/switches.py::SWITCHES`,
    entry ``data_apps_allow_same_origin``) rather than a hand-rolled
    env/config pair — the CONTRIBUTING.md sync-map's "new user-visible
    switch" rule, and what puts the flag in the `/admin/server-config`
    inventory with its lock reason.
    """
    from app.switches import switch_value

    return bool(switch_value("data_apps_allow_same_origin"))


def same_origin_serving_warning() -> Optional[str]:
    """A startup warning when hosted apps are enabled but will be refused at
    serve time for lack of an isolated origin, else ``None``.

    Fires when ``data_apps.enabled`` is on, ``allow_same_origin`` is off, and
    no ``subdomain_base`` is configured — the deployment has no way to serve a
    hosted app (every request lands on the main origin and is refused by
    ``data_apps_proxy._same_origin_serving_refused``). Kept pure so it is
    unit-testable; the app lifespan logs it (`app/main.py`).
    """
    if not feature_enabled("data_apps", "enabled", env_var="AGNES_DATA_APPS_ENABLED", default=False):
        return None
    if same_origin_serving_allowed():
        return None
    if (_effective_config().get("subdomain_base") or "").strip():
        return None
    return (
        "data_apps.enabled is on but hosted apps will NOT be served: same-origin "
        "serving is disabled and no data_apps.subdomain_base is configured. Serve apps "
        "from an isolated origin (set data_apps.subdomain_base), or accept same-origin "
        "serving with data_apps.allow_same_origin=true / "
        "AGNES_DATA_APPS_ALLOW_SAME_ORIGIN=1 (a hosted app's JS then runs with the "
        "viewer's session — trusted authors only)."
    )


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[dict] = None,
) -> None:
    try:
        audit_repo().log(user_id=actor_id, action=action, resource=resource, params=params)
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _feature_gate() -> None:
    if not feature_enabled("data_apps", "enabled", env_var="AGNES_DATA_APPS_ENABLED", default=False):
        raise HTTPException(status_code=404, detail="data_apps_disabled")


def _can_view(user: dict, row: dict) -> bool:
    if user["id"] == row["owner_user_id"]:
        return True
    if is_user_admin(user["id"]):
        return True
    return can_access(user["id"], ResourceType.DATA_APP.value, row["slug"])


def _require_owner_or_admin(user: dict, row: dict) -> None:
    if user["id"] == row["owner_user_id"] or is_user_admin(user["id"]):
        return
    raise HTTPException(status_code=403, detail="forbidden")


def _get_row_or_404(slug: str, *, allow_hidden: bool = False) -> dict:
    row = data_apps_repo().get_by_slug(slug)
    if not row:
        raise HTTPException(status_code=404, detail="data_app_not_found")
    # A soft-deleted linked app (its upstream config disappeared) is hidden from
    # the list; keep it hidden from every by-slug surface too (detail, PATCH,
    # and the hosted-only op endpoints, which linked rows never reach anyway),
    # so a stale grant can't still read/operate on a gone app. Its row + grants
    # persist for a lossless re-link when the upstream app reappears.
    #
    # `allow_hidden` is for DELETE alone: purging a retired row (and its
    # lingering grants) is exactly what an admin needs to do to a hidden app,
    # so the read-side 404 must not make it permanently undeletable (Devin
    # Review on #1116).
    if row.get("state") == "linked_hidden" and not allow_hidden:
        raise HTTPException(status_code=404, detail="data_app_not_found")
    return row


def _reject_linked(row: dict) -> None:
    """400 for hosted-only lifecycle actions on an externally-hosted row.

    `state` doubles as the reconciler's scope filter (`state='linked'`), so a
    stop/deploy that rewrote it would knock the row out of the sync's control
    permanently — the app could never be retired (`linked_hidden`) again after
    disappearing upstream, leaving users a dead Open link (Devin Review on
    #1116). Agnes hosts nothing for a linked app anyway; the runner has no
    container to act on.
    """
    if row.get("repo_mode") == "linked":
        raise HTTPException(
            status_code=400,
            detail="linked_app_not_hosted: this app runs outside Agnes — deploy/stop/logs do not apply",
        )


def _app_url(slug: str, cfg: dict) -> str:
    base = (cfg.get("subdomain_base") or "").strip()
    if base:
        return f"https://{slug}.{base}/"
    return f"/apps/{slug}/"


def _serialize(row: dict, cfg: Optional[dict] = None) -> dict:
    cfg = cfg if cfg is not None else _effective_config()
    out = {k: v for k, v in row.items() if k not in ("secrets_enc", "service_token_id")}
    kind = "linked" if row.get("repo_mode") == "linked" else "hosted"
    out["kind"] = kind
    # A linked app opens at its external URL (Agnes doesn't proxy it); a hosted
    # app opens at the reverse-proxy path. `effective_description` lets the admin
    # override the synced description without the next sync clobbering it.
    out["url"] = (row.get("external_url") or "") if kind == "linked" else _app_url(row["slug"], cfg)
    out["effective_description"] = row.get("description_override") or row.get("description") or ""
    return out


def _clamp_idle_timeout(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if value < _IDLE_TIMEOUT_MIN or value > _IDLE_TIMEOUT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"idle_timeout_s must be between {_IDLE_TIMEOUT_MIN} and {_IDLE_TIMEOUT_MAX}",
        )
    return value


def _decrypt_secrets(row: dict) -> dict:
    enc = row.get("secrets_enc")
    if not enc:
        return {}
    try:
        return json.loads(decrypt_secret(enc.encode("ascii")))
    except Exception:
        logger.warning("failed to decrypt secrets for data app %s; deploying with none", row["slug"])
        return {}


def _revoke_service_token(row: dict) -> None:
    """Revoke everything this app's container authenticates with.

    Both credentials, because both callers (an explicit stop and a delete)
    mean "this app is not coming back on its own". The container git token
    used to be left behind here, and being expiry-less it then outlived the
    app itself — a deleted app's repository stayed reachable with a live
    credential. (Devin Review on this PR.)
    """
    _revoke_container_git_tokens_for_row(row)
    token_id = row.get("service_token_id")
    if not token_id:
        return
    try:
        access_token_repo().revoke(token_id)
    except Exception:
        logger.warning("failed to revoke previous service token %s for data app %s", token_id, row["slug"])


def _revoke_container_git_tokens_for_row(row: dict) -> None:
    """``_revoke_container_git_tokens`` with the slugs resolved off an app row.

    A draft clones its PARENT's repo, so the scope half of the name is the
    parent's slug — the same resolution `_deploy` does before minting.
    """
    repo_slug = row.get("slug") or ""
    if row.get("is_draft") and row.get("parent_app_id"):
        parent = data_apps_repo().get(row["parent_app_id"])
        if parent:
            repo_slug = parent["slug"]
    if repo_slug and row.get("owner_user_id"):
        _revoke_container_git_tokens(row["owner_user_id"], repo_slug, row.get("slug") or "")


def _rmtree_config_dir(slug: str) -> None:
    """Best-effort removal of the RUNTIME config dir (``${DATA_DIR}/apps/<slug>``,
    holding the ``config.json`` apps-runner wrote — see ``_resolve_host_path``
    in ``services/apps_runner/api.py``). It carries the now-revoked service
    JWT in plaintext, so it's removed as hygiene on both a full app delete
    (``delete_data_app``) and a single draft delete/cascade
    (``_teardown_draft``). Shared so the two call sites can't drift."""
    apps_dir = os.path.join(os.environ.get("DATA_DIR", "/data"), "apps")
    config_dir = os.path.join(apps_dir, slug)
    # `${DATA_DIR}/apps/git` is the shared bare-repo store, not any one app's
    # config directory (`src.data_apps.git_repos.repo_path`). "git" is refused at
    # create time (`RESERVED_SLUGS`), but a row written before that guard existed
    # would still reach this line, and deleting one app must never be able to
    # destroy every app's git history.
    if os.path.realpath(config_dir) == os.path.realpath(os.path.join(apps_dir, "git")):
        logger.error(
            "refusing to remove the shared bare-repo store for slug %r; "
            "remove the app's row by hand if it really is stale",
            slug,
        )
        return
    try:
        shutil.rmtree(config_dir, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("failed to remove config dir %s (continuing)", config_dir)


def _mint_service_token(slug: str, owner: dict) -> tuple[str, str]:
    """Mint a PAT for this app's owner, store it via `access_token_repo().create`,
    and return the new token id.

    The `scope: "data-app:<slug>"` claim is ENFORCED: `app/auth/pat_resolver.py`
    admits it only on the data surface an app actually uses (see
    `_DATA_APP_ALLOWED_EXACT` / `_DATA_APP_ALLOWED_SUBTREES` there) and
    fail-closed refuses it everywhere else. Before that gate existed, code
    running in the container — including an externally-cloned, less-trusted
    repo — reached `/api/admin/*` whenever the owner was an Admin, and could
    call `POST /cli/auth/rescope-surface` to trade this token (minted WITHOUT
    expiry, see `omit_exp` below) for a fresh 90-day full-surface PAT.

    What the gate does NOT change: within that surface the app still reads
    with the OWNER's grants, evaluated live per request. That is the
    documented trade-off in docs/DEPLOYMENT.md ("granting access to view/open
    an app is an act of publication"), not something a scope can narrow.

    Mirrors `app/api/tokens.py::create_token`'s minting lines exactly (JWT +
    sha256 hash + prefix) — the raw JWT is only handed to `build_config_json`
    (as `#password`/`AGNES_TOKEN`), never returned to the caller.
    """
    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id=owner["id"],
        email=owner["email"],
        token_id=token_id,
        typ="pat",
        # No `exp` claim, matching the `expires_at=None` on the row below. The
        # two disagreed: the record said "never", the JWT carried the default
        # 30-day expiry, so a hosted app that slept and woke more than a month
        # after its last deploy started failing every Agnes call with nothing
        # anywhere saying why. (Devin Review on this PR, on the sibling git
        # token; the same mismatch was here.)
        omit_exp=True,
        extra_claims={"scope": f"data-app:{slug}"},
    )
    prefix = token_id.replace("-", "")[:8]
    token_hash = hashlib.sha256(jwt_token.encode()).hexdigest()
    access_token_repo().create(
        id=token_id,
        user_id=owner["id"],
        name=f"data-app:{slug}",
        token_hash=token_hash,
        prefix=prefix,
        expires_at=None,
    )
    return token_id, jwt_token


def _mint_container_git_token(repo_slug: str, app_slug: str, owner: dict) -> tuple[str, str]:
    """Mint the credential the CONTAINER clones its repo with.

    This has to exist separately from `_mint_service_token`, and the deploy
    path used to reuse that one — which could never work. The git surface
    (`app/api/data_apps_git.py`) is the only caller of `resolve_token_to_user`
    that passes `allow_data_app_git_scope=True`, and it admits exactly the
    `data-app-git:<slug>` scope; a `data-app:<slug>` service token is rejected
    there like anywhere else. So every hosted app's entrypoint got
    ``remote: authentication required`` on its first clone and crash-looped
    forever. Watched end to end on a live instance before this fix: the
    container restarting every few seconds, `git clone` failing, the app row
    stuck in `error`, and nothing in the Agnes log to say why — the rejection
    happens inside the git surface, which does not log a denial.

    Two properties matter and they pull in different directions from
    `_mint_git_credential`, which is the *analyst's* 24-hour authoring
    credential:

    - **Scope follows the REPO, not the app.** A draft shares its parent's
      repository, and the git surface pins `scope`'s slug to the repo being
      requested — a token minted for the draft's own slug is refused against
      the parent's repo. Callers pass `repo_slug`, already resolved.
    - **No expiry.** The container re-clones whenever it is recreated, which
      includes waking from `sleep_mode: recreate` long after the deploy. A
      24-hour token would leave an app that deployed fine on Monday unable to
      wake on Wednesday — a failure that looks like a hosting bug and is not
      one. This matches `_mint_service_token`, which is unbounded for the same
      reason, and carries the same documented trade-off.
    """
    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id=owner["id"],
        email=owner["email"],
        token_id=token_id,
        typ="pat",
        # No `exp` claim — see `_mint_service_token`. The container re-clones
        # on every recreate, including waking from `sleep_mode: recreate` long
        # after the deploy, which is the whole reason this credential is
        # unbounded; a 30-day JWT expiry made that wake fail to fetch its own
        # code and crash-loop with no explanation. (Devin Review on this PR.)
        omit_exp=True,
        # Clone-only. The scope encodes WHICH repo, never what may be done to
        # it, and this credential is minted for the app's OWNER — so without
        # this claim the git surface saw an owner and allowed pushes, making
        # "the clone token" a non-expiring read/write credential sitting in
        # every hosted container's `config.json`. Enforced in
        # `app/api/data_apps_git.py`. (agnes-reviewer-rbac on this PR.)
        extra_claims={"scope": f"data-app-git:{repo_slug}", "git_write": False},
    )
    access_token_repo().create(
        id=token_id,
        user_id=owner["id"],
        name=_container_git_token_name(repo_slug, app_slug),
        token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
        expires_at=None,
    )
    return token_id, jwt_token


def _container_git_token_name(repo_slug: str, app_slug: str) -> str:
    """The name every container git token is created under.

    One literal, because it is also the *key* these tokens are found by when
    they have to be revoked: unlike the service token, whose id is kept on the
    app row (`service_token_id`), this credential had nowhere to be recorded,
    so nothing could revoke it. Keying the sweep on the name avoids a column,
    a migration on both ladders and a parity sibling for a value only this
    module reads.

    It carries BOTH slugs on purpose. The *scope* follows the repo — a draft
    clones its parent's repository, so its token must be minted against the
    parent's slug — but the *ownership* follows the app: a parent and each of
    its drafts hold distinct, simultaneously-live tokens against that one
    repo. Keying only on `repo_slug` would make a draft's deploy revoke the
    parent's live container credential, breaking the parent the next time it
    woke and re-cloned.
    """
    return f"data-app-git:{repo_slug} (container {app_slug})"


def _revoke_container_git_tokens(owner_id: str, repo_slug: str, app_slug: str, *, keep: str | None = None) -> None:
    """Revoke this app's container git tokens, optionally sparing the newest.

    These are minted with **no expiry** on purpose — a container re-clones
    whenever it is recreated, including waking from `sleep_mode: recreate`
    long after the deploy — so nothing ages them out. Every deploy minted
    another and none was recorded anywhere, so they accumulated without bound
    and stayed valid forever, including for apps that had since been deleted.
    Each one grants read/write on the app's repository.

    Best-effort per token: on the deploy path this runs after a deploy the
    caller already considers successful, and a bookkeeping failure must not
    turn it into an error. (Devin Review on this PR.)
    """
    name = _container_git_token_name(repo_slug, app_slug)
    try:
        tokens = access_token_repo().list_for_user(owner_id, include_revoked=False)
    except Exception:  # noqa: BLE001
        logger.warning("could not list tokens to revoke container git credentials for %s", app_slug, exc_info=True)
        return
    for token in tokens:
        if token.get("name") == name and token.get("id") != keep:
            _revoke_quietly(token["id"])


_GIT_CREDENTIAL_TTL = timedelta(hours=24)


def mint_git_token(row: dict, *, parent_token_id: str | None = None) -> tuple[str, str]:
    """Mint the raw `data-app-git:<slug>` PAT that `_mint_git_credential`
    embeds, and return ``(token_id, jwt)``.

    Split out because the broker's git leg needs the token WITHOUT a URL: it
    proxies an in-sandbox `git` request to the git surface and attaches the
    credential itself, so the sandbox never holds one (see
    `app/api/broker.py::data_apps_git_broker`). The token id comes back so
    that caller can revoke it the moment the request is done — a per-request
    credential that outlived the request would pile up rows for nothing.

    ``parent_token_id`` binds the minted credential's life to the credential
    that asked for it (`pat_resolver.PARENT_TOKEN_ID_CLAIM`): revoking that
    one revokes this one, checked in `resolve_token_to_user`. Pass it whenever
    the mint is driven by a caller holding a revocable credential — i.e. a
    PAT, via `app.auth.dependencies.revocable_credential_id`. Without it a
    leaked PAT's 24-hour successors survived the revocation of the PAT itself,
    on every data app its owner owned, silently.

    Defaults to ``None`` for the two mints that have no such parent: the
    broker's per-request token (already revoked in its own ``finally``) and
    any mint driven by an interactive session JWT, which has no
    ``personal_access_tokens`` row to bind to.
    """
    owner = users_repo().get_by_id(row["owner_user_id"])
    if not owner:
        raise OwnerNotFoundError(row["owner_user_id"])
    slug = row["slug"]
    token_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + _GIT_CREDENTIAL_TTL
    jwt_token = create_access_token(
        user_id=owner["id"],
        email=owner["email"],
        token_id=token_id,
        typ="pat",
        expires_delta=_GIT_CREDENTIAL_TTL,
        extra_claims=(
            {"scope": f"data-app-git:{slug}", PARENT_TOKEN_ID_CLAIM: parent_token_id}
            if parent_token_id
            else {"scope": f"data-app-git:{slug}"}
        ),
    )
    access_token_repo().create(
        id=token_id,
        user_id=owner["id"],
        name=f"data-app-git:{slug}",
        token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
        expires_at=expires_at,
    )
    return token_id, jwt_token


def _mint_git_credential(row: dict, *, parent_token_id: str | None = None) -> str:
    """Mint a PAT scoped `data-app-git:<slug>` for this app's owner and
    return a clone URL with it embedded as `agnes:<jwt>@` basic-auth.

    This is the *agent's* push credential for working on the app's repo —
    independent of (and never stored as) the container's own
    `service_token_id` runtime credential minted by `_mint_service_token`.
    Unlike that one (deliberately unbounded, mirrors upstream's "unlimited
    per-app credentials"), this credential is scoped to a single authoring
    session — it expires after `_GIT_CREDENTIAL_TTL` (24h), stored on both
    the JWT's `exp` claim (via `expires_delta`) and the DB row's
    `expires_at`, kept in sync so neither outlives the other.

    The base URL is `get_public_url()` (same helper `create_data_app` uses
    for its own `git_url`) so the clone URL works from an analyst laptop,
    the MCP tool, or a remote sandbox — none of which can resolve
    `AGNES_INTERNAL_URL` (the in-cluster hostname). Unlike `create_data_app`,
    which falls back to a root-relative path when no public URL is
    configured, this credential must embed `agnes:<jwt>@` into an absolute
    URL with a scheme+host to put the basic-auth in, so an unconfigured
    public URL falls back to `AGNES_INTERNAL_URL` instead (the previous,
    always-internal behavior) rather than a schemeless path. This is
    unrelated to `redeploy_current`'s own `clone_url`, which stays on
    `AGNES_INTERNAL_URL` unconditionally — that one is used *inside* the
    container's config.json, which only ever runs in-cluster.
    """
    _token_id, jwt_token = mint_git_token(row, parent_token_id=parent_token_id)
    slug = row["slug"]
    base = get_public_url() or (os.environ.get("SERVER_URL") or "").strip().rstrip("/") or AGNES_INTERNAL_URL
    return _clone_url_with_credential(base, jwt_token, slug)


def _clone_url_with_credential(base: str, jwt_token: str, slug: str) -> str:
    """`<scheme>://agnes:<jwt>@<host>/data-apps.git/<slug>`.

    Its own function so the assembly can be tested without minting a token
    against a real owner. The scheme guard is the reason it needs testing: a
    schemeless base (`host:port`, which compose files do carry in
    ``SERVER_URL``) has nothing for the credential to be injected after, so
    the `replace("://", …)` was a silent no-op and the clone URL went out
    authenticating as nobody — the failure the ``SERVER_URL`` fallback exists
    to prevent, one step further along. Defaulted to https rather than
    rejected: this is a fallback for a value the operator set for something
    else, and refusing it would take away the only working address on that
    box. (Devin Review on this PR.)
    """
    if "://" not in base:
        base = f"https://{base}"
    return f"{base.replace('://', f'://agnes:{jwt_token}@')}/data-apps.git/{slug}"


# Cookie carrying a `data-app-preview:<slug>` token — see `_mint_preview_token`.
# Name PREFIX only: the real name is per-app (`preview_cookie_name`), which is
# what keeps two apps' credentials from colliding in the browser jar now that
# the cookie is `Path=/`. Kept as a module constant because the proxy also
# accepts the bare legacy name during a rollout.
_PREVIEW_COOKIE_NAME = "adp_preview"


def preview_cookie_name(slug: str) -> str:
    """Browser cookie name carrying ``slug``'s preview credential.

    Per-app, and that is load-bearing: a browser keys a cookie on
    ``(name, domain, path)``, and the cookie must be ``Path=/`` to reach the
    readiness poll (see `_mint_preview_token`). One shared name at that path
    is therefore ONE jar slot for the whole instance — minting a preview for a
    second app evicts the first app's credential, and because the slug pin
    lives in the token scope the survivor resolves to nothing for the other
    app: its poll 401s forever while the app is healthy. Two chat tabs
    previewing two apps is the ordinary case, not a corner (Devin Review on
    this PR).

    Read-side helper too (`_resolve_proxy_caller`), so it is deliberately
    total — an unknown/malformed slug just yields a name no cookie carries.
    Header-safety is enforced where the header is actually built, at mint.
    """
    return f"{_PREVIEW_COOKIE_NAME}_{slug}"


# What a slug may contain before it is interpolated into a `Set-Cookie` NAME.
# Deliberately a character class and not `SLUG_RE`: the only question at that
# point is header safety (no CR/LF, `;`, `=`, space, quote), and `SLUG_RE` also
# imposes a length/shape that legitimately-created rows need not satisfy —
# `keboola_adapter._sanitize` can yield a single character, and refusing to
# serve those apps' previews would be a bug of this fix's own making.
_COOKIE_NAME_SAFE_RE = _re.compile(r"^[A-Za-z0-9_-]+$")


class SlugNotCookieSafeError(ValueError):
    """The mint's deliberate header-safety refusal — and ONLY that.

    A dedicated type so the preview-grant handler can map exactly this
    refusal to 400 ``slug_not_cookie_safe``; a blanket ``except ValueError``
    also swallowed unrelated ``ValueError``s from ``create_access_token`` or
    the repo insert, presenting a 5xx-class backend fault as a bad app name
    (Devin Review on #1321)."""


# Q4 (spec §7/§11): 30-minute default TTL, renewed on every
# `agnes_data_app_preview` call, hard-capped by SessionEnd's best-effort
# revoke (`revoke_preview_tokens_for_user`, called from `app/chat/manager.py`).
_PREVIEW_TOKEN_TTL_S = 1800


def _mint_preview_token(row: dict, requester: dict, *, ttl_s: int = _PREVIEW_TOKEN_TTL_S) -> tuple[str, str]:
    """Mint a short-TTL `data-app-preview:<slug>` scoped token for the
    ``requester`` (the RBAC-approved caller who asked for the grant — owner,
    Admin, or a granted viewer, NOT necessarily the app owner) and return
    `(jwt, set_cookie_header)`. Minting under the caller — not the app owner —
    is what lets the SessionEnd revoke (keyed on the chat session's user) find
    and tear down the grant, and keeps proxy-side attribution honest.

    Migration-free by design (Q4): reuses the existing `access_tokens` table
    (`scopes`/`expires_at` already exist) rather than adding TTL state to the
    durable `resource_grants` model. Mirrors `_mint_git_credential` almost
    exactly — same mint-then-persist shape, same JWT `exp` / DB `expires_at`
    pairing so neither outlives the other — except the credential here is a
    view-only capability accepted ONLY by the ingress proxy's serving path
    (`app/api/data_apps_proxy.py`), never the JSON control-plane API (see
    `DATA_APP_PREVIEW_SCOPE_PREFIX`'s fail-closed default in
    `app.auth.pat_resolver.resolve_token_to_user`).

    The cookie is delivered to the browser, never a URL query parameter (an
    iframe `src`'s history/referrer would leak it) — a per-app NAME
    (`preview_cookie_name`) scopes it to exactly the app it authorizes;
    `HttpOnly` keeps it out of the hosted app's own (less-trusted) JS;
    `SameSite=Lax` is enough since the iframe navigation that sends it is a
    plain top-level-adjacent GET, not a cross-site POST.
    """
    slug = row["slug"]
    # BEFORE any side effect. The slug is interpolated into the `Set-Cookie`
    # NAME below, so it is re-checked at mint rather than trusted: every row
    # reaches this through a validated create, and a header built from an
    # unvalidated name is exactly the shape a CRLF injection needs. The check
    # sat after the mint at first, which made the refusal side-effectful — a
    # rejected slug still left a live, unrevoked 30-minute credential row in
    # `access_tokens` that nothing would ever hand out or clean up (Devin
    # Review on this PR). Validate-first makes the refusal free of both the
    # JWT and the row.
    if not _COOKIE_NAME_SAFE_RE.match(slug or ""):
        raise SlugNotCookieSafeError(f"data app slug is not safe in a cookie name: {slug!r}")
    token_id = str(uuid.uuid4())
    ttl = timedelta(seconds=ttl_s)
    expires_at = datetime.now(timezone.utc) + ttl
    jwt_token = create_access_token(
        user_id=requester["id"],
        email=requester["email"],
        token_id=token_id,
        typ="pat",
        expires_delta=ttl,
        extra_claims={"scope": f"{DATA_APP_PREVIEW_SCOPE_PREFIX}{slug}"},
    )
    access_token_repo().create(
        id=token_id,
        user_id=requester["id"],
        name=f"{DATA_APP_PREVIEW_SCOPE_PREFIX}{slug}",
        token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
        expires_at=expires_at,
    )
    # `Path=/` rather than `/apps/<slug>/`, and the reason is load-bearing: the
    # holding page polls `/api/data-apps/<slug>/readiness`, which is NOT under
    # `/apps/<slug>/`. A browser only attaches a cookie whose `Path` is a prefix
    # of the request path, so the narrower scoping meant the poll went out
    # unauthenticated, 401'd, was swallowed by the template's `catch`, and the
    # preview spun forever while the app was up — the very failure this loop
    # exists to fix (Devin Review on this PR).
    #
    # Widening the path does NOT widen authority: the slug pin lives in the
    # token's own scope (`data-app-preview:<slug>`, checked by
    # `_resolve_proxy_caller`), never in the cookie path, so a cookie sent on
    # more routes still authorizes exactly one app's preview. It stays HttpOnly
    # + SameSite=Lax.
    #
    # What the path WAS silently doing is keeping two apps' cookies apart, so
    # the per-app scoping moves to the cookie NAME instead — see
    # `preview_cookie_name`. The name embeds the slug, which is why the
    # header-safety check at the top of this function exists.
    #
    # Still path-prefix ingress only (the verified default; `subdomain_base`
    # unset). In subdomain mode the app is served on a different origin whose
    # paths start at `/`, so subdomain-mode preview remains a follow-up — it
    # needs a cross-origin cookie the same-origin fetch cannot set.
    cookie = f"{preview_cookie_name(slug)}={jwt_token}; Max-Age={max(ttl_s, 0)}; Path=/; SameSite=Lax; HttpOnly"
    return jwt_token, cookie


def revoke_preview_tokens_for_user(user_id: str) -> None:
    """Best-effort revoke of every live `data-app-preview:*` token belonging
    to ``user_id`` — the SessionEnd hard cap alongside
    ``ticket_repo().revoke_session`` (Q4). The 30-minute ``expires_at`` is
    the real backstop (this only tightens the window a chat session's
    preview grants stay usable after the session ends); a failure here must
    never block the caller's own teardown, so every exception is swallowed.

    Reuses ``access_token_repo().list_for_user`` + ``.revoke`` — both
    already dual-backend (DuckDB/Postgres) via the repo factory — so no new
    repository method is needed for this task.
    """
    try:
        repo = access_token_repo()
        for tok in repo.list_for_user(user_id, include_revoked=False):
            if (tok.get("name") or "").startswith(DATA_APP_PREVIEW_SCOPE_PREFIX):
                with contextlib.suppress(Exception):
                    repo.revoke(tok["id"])
    except Exception:
        logger.warning("revoke_preview_tokens_for_user: best-effort revoke failed for user %s", user_id)


def _handle_runner_failure(repo, app_id: str, exc: Exception) -> None:
    detail = getattr(exc, "detail", None) or str(exc)
    repo.set_state(app_id, "error", str(detail))


def _runner_http_error(exc: Exception) -> HTTPException:
    """Map a runner-call failure to the 502 the caller sees, WITHOUT
    flattening the two very different causes into one word.

    ``RunnerUnavailable`` means the transport failed — the sidecar is down,
    unreachable, or slower than the client timeout. ``runner_unavailable``
    is then literally true and the operator should go look at the process.

    ``RunnerError`` means the sidecar answered, with its own diagnosis
    (``image_not_found``, ``image_not_allowed``, ``bad_runner_token``,
    ``docker_error: ...``). Reporting *that* as "unavailable" is a lie that
    has now cost two investigations: both times a healthy, responding
    sidecar was blamed while its actual answer — which named the problem
    outright — was discarded here. Pass its words through instead.
    """
    if isinstance(exc, RunnerError):
        detail = getattr(exc, "detail", None) or str(exc)
        return HTTPException(status_code=502, detail=f"runner_error: {detail}")
    return HTTPException(status_code=502, detail="runner_unavailable")


class OwnerNotFoundError(Exception):
    """Raised by :func:`redeploy_current` when ``app_row['owner_user_id']``
    no longer resolves to a live user row. Distinct from ``ValueError``
    (spec-build failures) so callers can tell "deploy target row is
    internally inconsistent" (500) apart from "spec inputs are invalid"
    (400) without string-matching the exception message."""


class DraftParentMissingError(Exception):
    """Raised by :func:`redeploy_current` when a draft row's
    ``parent_app_id`` no longer resolves to a live parent row (the parent
    app was deleted out from under a still-live draft). Without this
    check, ``redeploy_current`` would silently fall back to cloning the
    draft's own (nonexistent) repo slug and the deploy would appear to
    succeed. ``deploy_data_app`` maps this to HTTP 409 ``parent_not_found``."""


def _revoke_quietly(token_id: str) -> None:
    """Best-effort revoke of a token no container ever received.

    Never raises: this runs on the failure path of a deploy that is already
    reporting an error, and a bookkeeping problem must not replace the real
    one in the caller's traceback.
    """
    try:
        access_token_repo().revoke(token_id)
    except Exception:  # noqa: BLE001
        logger.warning("could not revoke unused container git token %s", token_id, exc_info=True)


def _rollback_new_service_token(repo, app_id: str, new_token_id: str, previous_token_id: str) -> None:
    """Undo a tentatively-minted+stored service token after a deploy step
    following the mint fails (spec build or runner `up`).

    Revokes the just-minted token (it was never handed to a running
    container — no container ever saw it in its config.json — so it's
    pure dead weight if left live) and restores the row's
    `service_token_id` to whatever it was before this deploy attempt
    (`""` if the app had never deployed before). The previously-working
    token itself is never touched here — a failed redeploy must leave a
    still-sleeping-but-deployed app able to wake with its last-known-good
    credential.
    """
    try:
        access_token_repo().revoke(new_token_id)
    except Exception:
        logger.warning("failed to revoke rolled-back service token %s for data app %s", new_token_id, app_id)
    repo.update(app_id, service_token_id=previous_token_id)


def redeploy_current(row: dict) -> None:
    """Mint a fresh service token, build the runtime spec/config off
    ``row`` as it stands (i.e. whatever ``agnes-live`` currently points at
    — this function never touches the git ref itself), and hand it to the
    runner sidecar's ``up``.

    This is the shared mint -> config -> ``runner.up`` pipeline extracted
    from ``deploy_data_app``'s body (Task 7) so both the operator-triggered
    ``POST /{slug}/deploy`` (which fast-forwards ``agnes-live`` to a new sha
    *before* calling this) and the wake-on-request path (``_trigger_wake``
    in ``app/api/data_apps_proxy.py``, redeploying a sleeping
    ``sleep_mode="recreate"`` app at its last-deployed sha) go through
    exactly one implementation — no drift between the two call sites'
    mint/rollback semantics.

    Token mint/rollback semantics are preserved byte-for-byte from the
    original inline body: the new token is stored on the row TENTATIVELY,
    before it's known the runner call will succeed. On any failure below
    (`ValueError` from spec building, or `RunnerUnavailable`/`RunnerError`
    from the runner call) the tentative token is revoked and the row's
    `service_token_id` is restored to whatever it was before this call —
    never leaving a still-sleeping-but-deployed app without a working
    credential. The previous token is only revoked (this function's own
    side effect on success) once the runner has actually accepted the
    deploy.

    Raises `OwnerNotFoundError`, `DraftParentMissingError`, `ValueError`,
    `RunnerUnavailable`, or `RunnerError` on failure; each already left the
    row in "error" state (via `_handle_runner_failure`) for the runner-call
    case, or with an untouched state for the owner/parent/spec-build cases —
    callers decide how to surface that (HTTP response for `deploy_data_app`,
    `set_state("error", ...)` for `_trigger_wake`) without this function
    taking an opinion on HTTP status codes or wake-vs-deploy framing.
    """
    slug = row["slug"]
    repo = data_apps_repo()

    # A draft whose parent has since been deleted must fail loudly rather
    # than silently falling back (below) to cloning the draft's own
    # (nonexistent) repo slug — checked before any side effect (token mint)
    # so there's nothing to roll back on this path.
    if row.get("is_draft") and row.get("parent_app_id") and not repo.get(row["parent_app_id"]):
        raise DraftParentMissingError(row["parent_app_id"])

    owner = users_repo().get_by_id(row["owner_user_id"])
    if not owner:
        raise OwnerNotFoundError(row["owner_user_id"])

    previous_token_id = row.get("service_token_id") or ""
    new_token_id, jwt_token = _mint_service_token(slug, owner)
    repo.update(row["id"], service_token_id=new_token_id)
    row = repo.get(row["id"])  # refresh — carries the new (tentative) service_token_id

    secrets = _decrypt_secrets(row)
    # Drafts share the parent app's git repo (they never get one of their
    # own — see `create_draft`'s docstring), so the clone URL must point at
    # the PARENT's slug, not the draft's own. `build_config_json` already
    # selects the draft's pinned `draft_branch` off `row` via `is_draft`;
    # this only fixes which repo that branch is cloned from.
    repo_slug = slug
    if row.get("is_draft") and row.get("parent_app_id"):
        parent = repo.get(row["parent_app_id"])
        if parent:
            repo_slug = parent["slug"]
    clone_url = f"{AGNES_INTERNAL_URL}/data-apps.git/{repo_slug}"

    # NOT `jwt_token` (the service token). The git surface admits only the
    # `data-app-git:<slug>` scope, so handing the container its `data-app:`
    # service token made every first clone fail with `remote: authentication
    # required` and the container crash-loop forever. Minted against
    # `repo_slug` — a draft clones its PARENT's repo, and the scope's slug is
    # pinned to the repo being requested. See `_mint_container_git_token`.
    git_token_id, git_token = _mint_container_git_token(repo_slug, slug, owner)

    try:
        config_json = build_config_json(
            row,
            secrets=secrets,
            clone_url=clone_url,
            clone_token=git_token,
            # The RUNTIME credential, not the clone one: `AGNES_TOKEN` is what
            # the app calls the Agnes API with, and a git-scoped token is
            # refused by every data endpoint. (Devin Review on this PR.)
            service_token=jwt_token,
        )
        spec = build_container_spec(row, defaults=_effective_config(), data_dir=os.environ.get("DATA_DIR", "/data"))
    except ValueError:
        _rollback_new_service_token(repo, row["id"], new_token_id, previous_token_id)
        # Same reasoning as the service token above: no container ever saw
        # this credential, so leaving it live is pure dead weight.
        _revoke_quietly(git_token_id)
        raise

    try:
        _runner().up(slug, spec, config_json)
    except (RunnerUnavailable, RunnerError) as exc:
        _rollback_new_service_token(repo, row["id"], new_token_id, previous_token_id)
        _revoke_quietly(git_token_id)
        _handle_runner_failure(repo, row["id"], exc)
        raise

    # The runner accepted the deploy — only now is it safe to revoke the
    # previous token. Had we revoked it eagerly (before the runner call),
    # any failure above would have left the app with NO working credential
    # at all, even though the previously-deployed container is still
    # running/sleeping and may need to wake using it.
    if previous_token_id:
        try:
            access_token_repo().revoke(previous_token_id)
        except Exception:
            logger.warning("failed to revoke previous service token %s for data app %s", previous_token_id, slug)
    # The container git token gets the same treatment, and for the same
    # reason it has to happen *here* rather than before the runner call: the
    # previously-deployed container may still be running or asleep and will
    # re-clone with the old credential if it wakes. Sparing the one we just
    # handed to the runner, everything older for this repo goes.
    _revoke_container_git_tokens(owner["id"], repo_slug, slug, keep=git_token_id)


class CreateDataAppRequest(BaseModel):
    slug: str
    name: str
    description: str = ""
    repo_mode: str = "internal"
    repo_url: str = ""
    repo_branch: str = "main"
    idle_timeout_s: Optional[int] = None
    sleep_mode: Optional[str] = None


class DeployRequest(BaseModel):
    sha: Optional[str] = None
    mode: Optional[str] = None


class SecretsRequest(BaseModel):
    secrets: dict[str, str] = {}


class CreateDraftRequest(BaseModel):
    branch: str = "init"


class SetDescriptionRequest(BaseModel):
    description: str = ""


def _draft_slug(parent_slug: str, branch: str) -> str:
    """Derive the draft's own slug from its parent + branch:
    ``<parent>--<branch>``, lowercased, non-``SLUG_RE`` characters folded to
    ``-``, truncated to 40 chars. Raises 400 ``invalid_slug`` if the result
    still fails ``SLUG_RE`` (e.g. an all-symbol branch name collapsing to
    nothing usable, or a leading/trailing ``-`` after truncation) — or if it
    collapses to the parent's own slug, which the 40-char truncation can do
    for near-max-length parent slugs (a 38-40 char parent leaves no room for
    ``--<branch>`` before truncation cuts it back down to just the parent).
    That case must not fall through to ``create_draft``'s UNIQUE constraint:
    it would surface as a misleading 409 ``slug_exists`` that has nothing to
    do with an actual name collision and is the same for every branch."""
    raw = f"{parent_slug}--{branch}".lower()
    cleaned = _re.sub(r"[^a-z0-9-]", "-", raw)[:40].strip("-")
    if not SLUG_RE.match(cleaned) or cleaned == parent_slug:
        raise HTTPException(status_code=400, detail="invalid_slug")
    return cleaned


@router.get("")
async def list_data_apps(
    user: dict = Depends(get_current_user),
    kind: Optional[str] = None,
    source: Optional[str] = None,
):
    _feature_gate()
    if kind is not None and kind not in ("hosted", "linked"):
        raise HTTPException(status_code=400, detail="invalid_kind")
    cfg = _effective_config()
    # Drafts are working copies layered on a parent app, not independent
    # apps a caller should stumble onto in the human-facing/CLI list —
    # they're reached via `GET /{slug}`'s inlined `drafts` field instead.
    # Hidden linked rows (app removed upstream) are excluded from `list()` by
    # the `state='linked_hidden'` filter below only when filtering to linked;
    # the default list already omits them since callers key off visible state.
    repo = data_apps_repo()
    if kind == "linked":
        # Filter in SQL, not post-fetch: linked rows are created per upstream
        # app by the ingest reconciler, so their count is data-driven and can
        # exceed list()'s page cap — post-filtering a capped page could return
        # an incomplete (or empty) pool to the wizard/CLI/MCP even though
        # matching rows exist (Devin Review on #1116). list_linked() already
        # excludes linked_hidden and scopes by connection prefix; source_ref
        # is "<connection_id>:<external_app_id>".
        rows = repo.list_linked(source_ref_prefix=f"{source}:" if source is not None else None)
    else:
        rows = repo.list(include_drafts=False)
    out = []
    for r in rows:
        if r.get("state") == "linked_hidden":
            continue  # soft-deleted linked app — not shown
        if not _can_view(user, r):
            continue
        serialized = _serialize(r, cfg)
        if kind is not None and serialized["kind"] != kind:
            continue
        # Filter to one ingest connection (see above; kept for the non-linked
        # branch and as a belt-and-braces check on the SQL-filtered rows).
        if source is not None and not str(r.get("source_ref") or "").startswith(f"{source}:"):
            continue
        out.append(serialized)
    return out


@router.patch("/{slug}")
async def set_data_app_description(
    slug: str,
    payload: SetDescriptionRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Set an app's description — hosted or linked.

    For a **linked** app the stored ``description`` is refreshed by the ingest
    sync, and this writes an override the sync won't clobber. For a **hosted**
    app there is no sync, so the same column simply holds its current
    description: ``POST /api/data-apps`` seeds one at create time and this is
    the only way to change it afterwards. (Hosted rows were refused here with
    ``409 not_managed`` until this release, which made the description
    write-once — a typo could only be fixed by recreating the app.)

    One column serves both because every data-app reader already resolves
    through ``effective_description`` (``description_override or description``)
    — ``_serialize`` here, and the library/RBAC projection in
    ``app/resource_types.py``. The column keeps its ``_override`` name, which
    reads oddly for a hosted app with nothing to override; renaming it is a
    migration for no behavioural gain, and the API surface exposes
    ``effective_description`` rather than either raw column.

    Owner or Admin only, both cases.
    """
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)
    data_apps_repo().set_description_override(slug, payload.description)
    _audit(conn, user["id"], "data_app.set_description", f"data_app:{slug}", {})
    return _serialize(_get_row_or_404(slug))


@router.post("", status_code=201, dependencies=[Depends(reject_keboola_header_credential)])
async def create_data_app(
    payload: CreateDataAppRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _feature_gate()
    if not SLUG_RE.match(payload.slug):
        raise HTTPException(status_code=400, detail="invalid_slug")
    if payload.slug in RESERVED_SLUGS:
        raise HTTPException(status_code=400, detail="reserved_slug")
    if payload.repo_mode not in ("internal", "external"):
        raise HTTPException(status_code=400, detail="invalid_repo_mode")
    idle_timeout_s = _clamp_idle_timeout(payload.idle_timeout_s)

    cfg = _effective_config()
    is_admin = is_user_admin(user["id"])
    # Quota is admin-exempt, so the race this lease guards against (two
    # concurrent requests both observing count < max_apps_per_user) only
    # exists for non-admin callers — skip the lease entirely for Admin.
    lease_held = False
    lease_name = holder = ""
    if not is_admin:
        lease_held, lease_name, holder = _acquire_create_lease(user["id"])

    try:
        if not is_admin:
            max_apps = cfg["max_apps_per_user"]
            existing = data_apps_repo().list(owner_user_id=user["id"], include_drafts=False)
            if len(existing) >= max_apps:
                raise HTTPException(status_code=403, detail="app_quota_exceeded")

        repo = data_apps_repo()
        kwargs: dict[str, Any] = dict(
            slug=payload.slug,
            name=payload.name,
            owner_user_id=user["id"],
            description=payload.description,
            repo_mode=payload.repo_mode,
            repo_url=payload.repo_url,
            repo_branch=payload.repo_branch,
        )
        kwargs["idle_timeout_s"] = idle_timeout_s if idle_timeout_s is not None else cfg["default_idle_timeout_s"]
        kwargs["sleep_mode"] = payload.sleep_mode if payload.sleep_mode is not None else cfg["default_sleep_mode"]

        try:
            app_id = repo.create(**kwargs)
        except (duckdb.ConstraintException, sa_exc.IntegrityError):
            raise HTTPException(status_code=409, detail="slug_exists")

        if payload.repo_mode == "internal":
            init_app_repo(payload.slug)

        _audit(conn, user["id"], "data_app.create", f"data_app:{payload.slug}", {"name": payload.name})

        public = get_public_url()
        git_url = f"{public}/data-apps.git/{payload.slug}" if public else f"/data-apps.git/{payload.slug}"
        return {"id": app_id, "slug": payload.slug, "git_url": git_url}
    finally:
        if lease_held:
            _release_create_lease(lease_name, holder)


@router.get("/{slug}")
async def get_data_app(slug: str, user: dict = Depends(get_current_user)):
    _feature_gate()
    row = _get_row_or_404(slug)
    if not _can_view(user, row):
        raise HTTPException(status_code=403, detail="forbidden")
    out = _serialize(row)
    # Drafts are hidden from the list endpoint (`include_drafts=False`);
    # this is where they surface instead — inlined on their PROD parent's
    # detail response. Empty for a draft's own detail (drafts don't have
    # drafts — `create_draft` rejects `parent_is_draft`). Gated the same as
    # the draft-mutating endpoints (owner/Admin only, not any grantee that
    # merely passed `_can_view`) — a read grant on the parent app is not
    # meant to expose in-progress draft branch/state/URL metadata.
    if not row.get("is_draft") and (user["id"] == row["owner_user_id"] or is_user_admin(user["id"])):
        out["drafts"] = [
            {
                "id": d["id"],
                "slug": d["slug"],
                "branch": d["draft_branch"],
                "state": d["state"],
                "url": _app_url(d["slug"], _effective_config()),
            }
            for d in data_apps_repo().list_drafts(row["id"])
        ]
    return out


@router.post("/{slug}/deploy", dependencies=[Depends(reject_keboola_header_credential)])
async def deploy_data_app(
    slug: str,
    payload: DeployRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)
    # AFTER authz — the distinct 400 must not leak an app's kind/existence
    # to callers who would otherwise get a plain 403 (Devin Review on #1116).
    _reject_linked(row)

    holder = require_op_lease(slug)
    try:
        repo = data_apps_repo()
        if payload.mode == "dev":
            # Dev-mode deploys serve the draft's pinned `draft_branch` straight
            # from the parent's repo — `build_config_json` already selects it
            # (`is_draft`) and `redeploy_current` already clones from the
            # parent's slug for draft rows, so there is no ref to fast-forward
            # here at all (drafts never advance `agnes-live`).
            if not row.get("is_draft"):
                raise HTTPException(status_code=400, detail="dev_requires_draft")
            sha = ""
        elif row.get("is_draft"):
            # A draft has no `agnes-live` ref of its own to fast-forward to —
            # only `mode="dev"` deploys are valid for a draft row.
            raise HTTPException(status_code=400, detail="prod_on_draft")
        elif row["repo_mode"] == "external":
            # External repos have no internal bare repo (`init_app_repo` is
            # internal-only at create) — nothing for `fast_forward_live` to
            # fast-forward. The runtime clones HEAD of `repo_branch` at boot
            # (spec §2: external repos are HEAD-at-wake; sha pinning is future
            # work), so an explicit sha in the request can't be honored.
            if payload.sha:
                raise HTTPException(status_code=400, detail="external_repo_sha_unsupported")
            sha = ""
        else:
            try:
                sha = fast_forward_live(slug, payload.sha)
            except ValueError as exc:
                if "no commits to deploy" in str(exc):
                    raise HTTPException(status_code=409, detail="deploy_empty_repo")
                raise HTTPException(status_code=400, detail=str(exc))

        try:
            # OFF the event loop. `redeploy_current` ends in a synchronous
            # httpx call to the runner sidecar budgeted at `up_timeout()`
            # (600 s by default, for a cold ~1.3 GB image pull). Called inline
            # from this `async def`, one first-deploy-on-a-cold-host would
            # freeze the single uvicorn event loop for that whole window —
            # no /api/health, no sign-in, every other request queued behind
            # an image download. `_run_wake_fn` in `data_apps_proxy.py`
            # already offloads the very same callable for the wake path.
            await run_in_threadpool(redeploy_current, row)
        except OwnerNotFoundError:
            raise HTTPException(status_code=500, detail="owner_not_found")
        except DraftParentMissingError:
            raise HTTPException(status_code=409, detail="parent_not_found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except (RunnerUnavailable, RunnerError) as exc:
            # `redeploy_current` already recorded state=error + state_detail
            # via `_handle_runner_failure`; this only decides what the caller
            # of THIS request is told.
            raise _runner_http_error(exc)

        repo.record_deploy(row["id"], sha)
        repo.set_state(row["id"], "running")
        _audit(conn, user["id"], "data_app.deploy", f"data_app:{slug}", {"sha": sha})

        return {"state": "running", "deployed_sha": sha}
    finally:
        release_op_lease(slug, holder)


@router.post("/{slug}/git-credential", dependencies=[Depends(reject_keboola_header_credential)])
async def mint_git_credential(
    slug: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Mint the app owner's 24-hour `data-app-git:<slug>` push credential.

    Deliberately gated by `get_current_user`, NOT `require_session_token`:
    `agnes app git-credential` authenticates with the PAT `agnes login` wrote
    (`cli/config.py::get_token`), and the same is true of this route's MCP
    tool over the PAT-authenticated SSE transport. Refusing a PAT here would
    break a shipped command while changing nothing an attacker can do — the
    git surface admits a plain PAT for a push directly
    (`app/api/data_apps_git.py`: `allowed = is_owner or admin`, pinned by
    `tests/test_data_apps_git.py::test_push_allowed_for_owner`), so a PAT
    holder reaches the same repo one `git push` away with no mint call at all.
    The mint is a convenience wrapper over authority the caller already holds,
    not the gate that grants it.

    What the mint DID add was persistence: the credential is its own
    `personal_access_tokens` row, so revoking the PAT that produced it left it
    pushing for the rest of its 24 hours. `revocable_credential_id` closes
    that — when the caller holds a PAT, the successor is stamped with its id
    and dies with it.
    """
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)
    # AFTER authz — the distinct 400 must not leak an app's kind/existence
    # to callers who would otherwise get a plain 403 (Devin Review on #1116).
    _reject_linked(row)
    try:
        url = _mint_git_credential(row, parent_token_id=revocable_credential_id(request))
    except OwnerNotFoundError:
        raise HTTPException(status_code=500, detail="owner_not_found")
    _audit(conn, user["id"], "data_app.git_credential", f"data_app:{slug}", {})
    return {"git_clone_url": url}


@router.post("/{slug}/preview-grant", dependencies=[Depends(reject_keboola_header_credential)])
async def create_preview_grant(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Mint a short-TTL `data-app-preview:<slug>` cookie for the in-chat
    preview iframe (wave 3C, spec §7 / Q4).

    Chat-surface-internal: this is what `agnes_data_app_preview`'s live-URL
    call hits server-side to hand the iframe a same-origin cookie so it can
    load `/apps/<slug>/` without a cross-origin login. Any caller who can
    already *view* the app (owner, Admin, or a group grant — same predicate
    as `GET /{slug}`) may request one; unlike `git-credential`, this is not
    owner/Admin-only, since a granted viewer previewing a draft is a normal
    part of the review loop. No REST/CLI analogue is expected — see
    `_EXEMPT` in `tests/test_documentation_api_triple_surface.py`.
    """
    _feature_gate()
    row = _get_row_or_404(slug)
    if not _can_view(user, row):
        raise HTTPException(status_code=403, detail="forbidden")
    _reject_linked(row)
    try:
        _token, cookie = _mint_preview_token(row, user)
    except SlugNotCookieSafeError:
        # The mint's own header-safety refusal (slug unsafe in a cookie name)
        # is deliberate and side-effect-free — surface it as a clean 400, not
        # an unhandled 500. Caught by its dedicated type only: a blanket
        # ValueError catch also relabelled unrelated backend faults
        # (create_access_token claims, repo validation) as a bad app name;
        # those must keep surfacing as 500s (Devin Review on this PR).
        raise HTTPException(status_code=400, detail="slug_not_cookie_safe")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_PREVIEW_TOKEN_TTL_S)
    _audit(conn, user["id"], "data_app.preview_grant", f"data_app:{slug}", {})
    # Install the cookie via a real Set-Cookie response header, not just the JSON
    # body: the cookie is `HttpOnly`, and a browser silently discards an HttpOnly
    # cookie set through `document.cookie` (RFC 6265bis). The chat frontend makes
    # a same-origin `fetch()` to this endpoint (the browser is already the chat
    # user), so this header lands the cookie on the app origin for the iframe to
    # send — HttpOnly intact. `preview_cookie` stays in the body for callers/tests.
    resp = JSONResponse({"preview_cookie": cookie, "expires_at": expires_at.isoformat()})
    resp.headers.append("set-cookie", cookie)
    return resp


@router.post("/{slug}/drafts", status_code=201, dependencies=[Depends(reject_keboola_header_credential)])
async def create_draft(
    slug: str,
    payload: CreateDraftRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Create a draft copy of the prod app ``slug`` on ``payload.branch``.

    The draft row (Task 1's ``create_draft``) is a full ``data_apps`` row
    with ``is_draft=True`` and ``parent_app_id`` pointing at the prod app —
    it is deployed/stopped/deleted like any other app, just excluded from
    the default list (Task 6). ``ensure_branch`` (Task 2) creates the
    branch on the *prod* app's git repo (drafts don't get their own repo —
    they're a ref + a registry row layered on top of the parent's); the
    git credential handed back is likewise minted against the parent
    (``_mint_git_credential(parent)``), since that's the repo the branch —
    and thus any push — actually lives in.

    Ordering is deliberate: the registry row is inserted BEFORE the git
    branch is created. Doing it the other way round would leave an orphaned
    branch on the parent's repo whenever ``create_draft`` hits a slug
    collision (409 ``slug_exists``) — a git side effect with no
    corresponding row, invisible to any registry-driven cleanup. With the
    row created first, a slug collision has no git side effect at all; if
    ``ensure_branch`` then fails (409 ``parent_has_no_main``), the
    just-inserted row is deleted so a failed create-draft call never leaves
    a dangling draft behind either.
    """
    _feature_gate()
    parent = _get_row_or_404(slug)
    _require_owner_or_admin(user, parent)
    _reject_linked(parent)
    if parent.get("is_draft"):
        raise HTTPException(status_code=400, detail="parent_is_draft")
    if not _BRANCH_RE.match(payload.branch) or not _is_git_valid_branch(payload.branch):
        raise HTTPException(status_code=400, detail="invalid_branch")
    draft_slug = _draft_slug(slug, payload.branch)

    repo = data_apps_repo()
    try:
        draft_id = repo.create_draft(
            parent_app_id=parent["id"],
            slug=draft_slug,
            branch=payload.branch,
            owner_user_id=parent["owner_user_id"],
        )
    except (duckdb.ConstraintException, sa_exc.IntegrityError):
        raise HTTPException(status_code=409, detail="slug_exists")

    from src.data_apps.git_repos import delete_branch, ensure_branch

    try:
        ensure_branch(slug, payload.branch, base="main")
    except ValueError:
        repo.delete(draft_id)
        raise HTTPException(status_code=409, detail="parent_has_no_main")
    except subprocess.CalledProcessError:
        # Belt-and-braces: `_is_git_valid_branch` should already reject
        # everything `git update-ref` refuses, but if some other
        # git-invalid form ever slips past it, fail the same way (400
        # `invalid_branch`, draft row rolled back) rather than surfacing an
        # unhandled 500 and leaving an orphaned draft row that would turn a
        # retry into a misleading 409 `slug_exists`.
        repo.delete(draft_id)
        raise HTTPException(status_code=400, detail="invalid_branch")

    try:
        # Push credential is against the PROD repo — and bound to the caller's
        # own credential, exactly as `mint_git_credential` does it: this route
        # hands back the same 24-hour token and must not be the one surface
        # that forgets the binding.
        git_url = _mint_git_credential(parent, parent_token_id=revocable_credential_id(request))
    except OwnerNotFoundError:
        # Same rollback contract as the two failure paths above — a failed
        # create-draft call must never leave the row or the branch behind,
        # or a retry gets a misleading 409 `slug_exists`.
        repo.delete(draft_id)
        with contextlib.suppress(ValueError):
            delete_branch(slug, payload.branch)
        raise HTTPException(status_code=500, detail="owner_not_found")

    _audit(
        conn,
        user["id"],
        "data_app.draft_create",
        f"data_app:{draft_slug}",
        {"parent": slug, "branch": payload.branch},
    )
    return {"id": draft_id, "slug": draft_slug, "branch": payload.branch, "git_clone_url": git_url}


async def _teardown_draft(repo, parent_slug: str, draft: dict, conn: duckdb.DuckDBPyConnection, actor_id: str) -> None:
    """Shared draft-teardown body: best-effort container stop, service-token
    revoke, draft-branch delete on the PARENT repo, registry row delete,
    and config-dir cleanup. Used by both ``delete_draft`` (single draft) and
    ``delete_data_app``'s cascade (all drafts of a deleted parent) so the two
    call sites can't drift on what "delete a draft" actually tears down.

    A deployed draft has its own slug and container reachable via
    ``/apps/<draft_slug>/``, so the container-stop step takes the SAME
    ``dataapp:op:{draft_slug}`` lease every other runner-mutating operation
    serializes on (``deploy_data_app``/``stop_data_app``/``delete_data_app``/
    the ingress proxy's wake path) — without it, a concurrent wake-on-request
    for the draft could race ``runner.up()`` against this teardown's
    ``runner.stop()``, the exact unlocked check-then-act corruption the
    lease exists to prevent (see CHANGELOG 0.76.23).

    ``async`` only so the runner call can be offloaded — both call sites are
    already `async def` handlers, and a blocking sidecar call left inline
    there runs on the single event loop.
    """
    draft_slug = draft["slug"]
    holder = require_op_lease(draft_slug)
    try:
        await run_in_threadpool(_runner().stop, draft_slug, "recreate")
    except (RunnerUnavailable, RunnerError):
        logger.warning("_teardown_draft: runner stop failed for %s (continuing)", draft_slug)
    finally:
        release_op_lease(draft_slug, holder)

    _revoke_service_token(draft)

    from src.data_apps.git_repos import delete_branch

    try:
        delete_branch(parent_slug, draft["draft_branch"])
    except ValueError:
        pass

    repo.delete(draft["id"])
    _rmtree_config_dir(draft_slug)

    _audit(
        conn,
        actor_id,
        "data_app.draft_delete",
        f"data_app:{draft_slug}",
        {"parent": parent_slug},
    )


@router.delete("/{slug}/drafts/{draft_slug}", status_code=204)
async def delete_draft(
    slug: str,
    draft_slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Tear down a single draft: owner/Admin of the PARENT app; 400
    ``not_a_draft`` if ``draft_slug`` isn't a draft of ``slug`` (including
    ``draft_slug == slug`` itself); 404 if ``draft_slug`` doesn't exist at
    all. Mirrors ``delete_data_app``'s teardown via the shared
    ``_teardown_draft`` helper — container stop, token revoke, branch
    delete, row delete, config-dir cleanup.
    """
    _feature_gate()
    parent = _get_row_or_404(slug)
    _require_owner_or_admin(user, parent)

    repo = data_apps_repo()
    draft = repo.get_by_slug(draft_slug)
    if draft is None:
        raise HTTPException(status_code=404, detail="data_app_not_found")
    if not draft.get("is_draft") or draft.get("parent_app_id") != parent["id"]:
        raise HTTPException(status_code=400, detail="not_a_draft")

    await _teardown_draft(repo, slug, draft, conn, user["id"])


@router.post("/{slug}/stop")
async def stop_data_app(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)
    # AFTER authz — the distinct 400 must not leak an app's kind/existence
    # to callers who would otherwise get a plain 403 (Devin Review on #1116).
    _reject_linked(row)

    holder = require_op_lease(slug)
    try:
        repo = data_apps_repo()
        try:
            await run_in_threadpool(_runner().stop, slug, "recreate")
        except (RunnerUnavailable, RunnerError) as exc:
            _handle_runner_failure(repo, row["id"], exc)
            raise _runner_http_error(exc)

        repo.set_state(row["id"], "stopped")
        # Spec §8/§10: an explicit stop revokes the service token — unlike
        # reap-idle's sleep transition (see reap_idle_data_apps), which leaves
        # it live so the app can wake later. A stop is an operator decision that
        # the app isn't coming back on its own; the credential goes with it.
        _revoke_service_token(row)
        repo.update(row["id"], service_token_id="")
        _audit(conn, user["id"], "data_app.stop", f"data_app:{slug}")
        return {"state": "stopped"}
    finally:
        release_op_lease(slug, holder)


@router.delete("/{slug}", status_code=204)
async def delete_data_app(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Runner stop + service-token revoke + registry row delete.

    204 No Content, no body (project convention — see
    ``tests/test_api_design_rules.py::test_delete_returns_204``). The git
    repo directory under ``${DATA_DIR}/apps/git/<slug>.git`` is
    intentionally left on disk — deletion is a registry-only operation;
    that fact is recorded in the audit log params, not the response body.
    The RUNTIME config dir (``${DATA_DIR}/apps/<slug>``, holding the
    ``config.json`` apps-runner wrote — see ``_resolve_host_path`` in
    ``services/apps_runner/api.py``) is different: it carries the
    now-revoked service JWT in plaintext, so it's removed best-effort as
    hygiene rather than kept like the git repo.

    Cascades to any live drafts: a prod app's drafts share its git repo and
    reference its id as ``parent_app_id``, so deleting the parent without
    tearing them down first would leave orphaned draft rows/branches/
    containers behind. Each draft is torn down via the same
    ``_teardown_draft`` helper ``delete_draft`` uses, BEFORE this app's own
    teardown. (Drafts can't themselves have drafts — ``create_draft``
    rejects ``parent_is_draft`` — so this loop is a no-op when ``row`` is
    itself a draft — which can't happen anyway, see below.)

    A draft ``slug`` is rejected outright (400 ``use_draft_delete_route``)
    rather than falling through to this function's own teardown below,
    which — unlike ``_teardown_draft`` — never deletes the draft's branch
    on the PARENT's repo; going through here would silently orphan it.
    ``DELETE /{slug}/drafts/{draft_slug}`` is the only path that tears a
    draft down correctly.
    """
    _feature_gate()
    # allow_hidden: a retired linked row (and its lingering grants) is exactly
    # what an admin needs to purge — see _get_row_or_404.
    row = _get_row_or_404(slug, allow_hidden=True)
    _require_owner_or_admin(user, row)
    if row.get("is_draft"):
        raise HTTPException(status_code=400, detail="use_draft_delete_route")

    repo = data_apps_repo()
    # Linked rows: registry-only removal — Agnes hosts nothing for them, so
    # there is no runner container, service token, git repo, or config dir to
    # tear down, and running the hosted teardown would 500 on the synthetic
    # 'system' owner. This is the admin's ONLY retire path for a row orphaned
    # by unregistering its MCP source/lister (the reconciler can no longer
    # hide it); on a still-syncing source the next lister run recreates the
    # row under the same deterministic slug, grants intact — a harmless no-op
    # loop rather than data loss (Devin Review on #1116).
    if row.get("repo_mode") == "linked":
        repo.delete(row["id"])
        _audit(
            conn,
            user["id"],
            "data_app.delete",
            f"data_app:{slug}",
            {"linked": True, "source_ref": row.get("source_ref")},
        )
        return
    # Drafts live on the parent's repo and have their own containers/branches;
    # tear them down first so deleting a prod app can't strand them.
    for draft in repo.list_drafts(row["id"]):
        await _teardown_draft(repo, slug, draft, conn, user["id"])

    holder = require_op_lease(slug)
    try:
        # Best-effort: a dead runner must not block deleting the registry row
        # (there'd otherwise be no way to remove an app whose container host is
        # gone).
        try:
            await run_in_threadpool(_runner().stop, slug, "recreate")
        except (RunnerUnavailable, RunnerError):
            logger.warning("delete_data_app: runner stop failed for %s (continuing)", slug)

        _revoke_service_token(row)
        repo.delete(row["id"])

        _rmtree_config_dir(slug)

        _audit(
            conn,
            user["id"],
            "data_app.delete",
            f"data_app:{slug}",
            {"repo_dir_left_on_disk": True},
        )
    finally:
        release_op_lease(slug, holder)


@router.put("/{slug}/secrets")
async def set_data_app_secrets(
    slug: str,
    payload: SecretsRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)
    # AFTER authz — the distinct 400 must not leak an app's kind/existence
    # to callers who would otherwise get a plain 403 (Devin Review on #1116).
    _reject_linked(row)

    try:
        enc = encrypt_secret(json.dumps(payload.secrets))
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc

    data_apps_repo().update(row["id"], secrets_enc=enc.decode("ascii"))
    _audit(conn, user["id"], "data_app.secrets_update", f"data_app:{slug}", {"keys": sorted(payload.secrets)})
    return {"updated": True}


@router.get("/{slug}/logs")
async def get_data_app_logs(slug: str, tail: int = 200, user: dict = Depends(get_current_user)):
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)
    # AFTER authz — the distinct 400 must not leak an app's kind/existence
    # to callers who would otherwise get a plain 403 (Devin Review on #1116).
    _reject_linked(row)

    try:
        logs = await run_in_threadpool(_runner().logs, slug, tail)
    except (RunnerUnavailable, RunnerError) as exc:
        raise _runner_http_error(exc)
    return {"logs": logs}


def _readiness_cors_headers(request: Request, slug: str) -> dict[str, str]:
    """CORS response headers for ``slug``'s OWN subdomain origin, or ``{}``.

    On a subdomain-served instance the holding page is rendered on
    ``https://<slug>.<subdomain_base>`` and its readiness poll goes to the
    main host (`_readiness_poll_url`) with ``credentials: "include"`` — a
    cross-origin GET whose *response* the page's JS must be allowed to read.

    That allowance is deliberately NOT the app-wide ``CORSMiddleware``'s job.
    A first cut added an ``allow_origin_regex`` over every data-app subdomain
    there, paired with ``allow_credentials=True`` — but those subdomains serve
    USER-AUTHORED app code, and the session cookie already rides to the main
    host from them (``Domain=.<parent>``, and subdomains are same-site so
    ``SameSite`` does not block the send). An app-wide credentialed allowance
    therefore let any hosted app's JS read every authenticated Agnes endpoint
    as whoever is viewing it; CORS was the one barrier left, and the regex
    removed it (Devin Review on this PR). So the grant is scoped twice
    instead: to THIS route only (headers attached by the handler, no
    middleware policy), and to THIS app's own origin only — ``a.<base>``
    cannot read ``b``'s readiness, let alone anything else.

    The poll is a "simple" CORS request (GET, no custom headers), so no
    preflight ever fires and response headers are the entire surface needed.
    Only the success path attaches them: an unreadable cross-origin error
    response behaves exactly like a readable non-ready one — the page's
    ``catch`` swallows it and the poll continues — so errors stay uniform
    with the path-prefix form. Port is deliberately ignored in the match
    (origins carry one on non-default-port deployments, as the old regex's
    ``(:\\d+)?`` acknowledged); scheme is pinned to http(s).

    Known dependency: ``CORS_ORIGINS='*'`` defeats this grant. With a
    wildcard, the app-wide middleware runs with ``allow_all_origins`` and
    stamps ``Access-Control-Allow-Origin: *`` (credential-less — main.py
    drops credentials for wildcards) OVER these headers, so the credentialed
    poll response becomes unreadable and the holding page spins.
    ``app/main.py`` logs a loud error for exactly that combination
    (wildcard + ``subdomain_base``); the fix is an explicit origin
    allowlist, never re-adding a subdomain grant to the middleware
    (Devin Review on #1321).
    """
    # `Vary: Origin` on EVERY path, refusals included: the response's headers
    # differ by Origin, and a cache keyed only on the URL could store the
    # header-less refusal variant and replay it to the app's own origin —
    # the browser then refuses the read, the holding page's `catch` swallows
    # it, and the poll spins forever with the app up (Devin Review on this
    # PR). Starlette's own CORSMiddleware emits it unconditionally for
    # explicit-origin policies for the same reason.
    vary_only = {"Vary": "Origin"}
    origin = request.headers.get("origin") or ""
    if not origin:
        return vary_only
    base = (get_data_apps_config().get("subdomain_base") or "").strip().strip(".")
    if not base:
        return vary_only
    # With CORS_ORIGINS='*' the app-wide middleware stamps a credential-less
    # `Access-Control-Allow-Origin: *` OVER whatever this returns, while a
    # handler-set `Allow-Credentials: true` would survive — shipping the
    # browser-invalid `*`+credentials pair. The poll is broken under the
    # wildcard either way (main.py logs a dedicated error for wildcard +
    # subdomain_base); don't emit half a grant into that response.
    # The wildcard verdict is captured on app.state at build time, from the
    # SAME read the middleware was configured with — a request-time env
    # re-read could diverge on overlay-configured instances, where
    # create_app loads overlay env after the middleware is registered
    # (Devin on #1321). Env is only the fallback for app objects built
    # outside create_app (unit-test shims).
    wildcard = getattr(request.app.state, "cors_has_wildcard", None)
    if wildcard is None:
        wildcard = "*" in (o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(","))
    if wildcard:
        return vary_only
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(origin)
    except ValueError:
        return vary_only
    if parts.scheme not in ("http", "https"):
        return vary_only
    if (parts.hostname or "") != f"{slug}.{base}".lower():
        return vary_only
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }


@router.get("/{slug}/readiness")
async def get_data_app_readiness(slug: str, request: Request, response: Response):
    """Runner-backed readiness probe. Doubles as the wake-completion flip:
    when a `deploying` app's runner reports `ready`, this call itself
    transitions the row to `running` — the ingress proxy's holding page
    (`app/api/data_apps_proxy.py``, ``data_app_waking.html``'s poll loop)
    is the only caller that hits this endpoint on a cadence, so the flip
    happening here (rather than a dedicated poller) is what actually
    surfaces "the app is up" back to the browser tab that triggered the
    wake. See that module's docstring for the other half of this contract.

    The caller is resolved through the PROXY's chain, not a plain
    ``Depends(get_current_user)``. The holding page this serves is rendered
    by the proxy, which accepts a ``data-app-preview:<slug>`` scoped token —
    the credential the in-chat preview iframe carries — while
    ``get_current_user`` rejects it outright. So a preview iframe used to get
    a holding page whose poll 401'd forever and never noticed the app come
    up (Devin Review on this PR; pre-existing for the `sleeping`/`deploying`
    branches, but the starting-app branch added here reaches it far more
    often). Serving the page and refusing its only poll is half a fix.

    Resolved BEFORE the registry read: with auth moved out of the ``Depends``
    chain into the handler body, a lookup-first ordering answered anonymous
    probes 404 for a made-up slug and 401 for a real one — letting a caller
    with no credentials at all enumerate which hosted apps exist (Devin
    Review on this PR). Anonymous now gets a uniform 401 either way, the
    same shape the old ``Depends(get_current_user)`` signature enforced.

    Imported inside the function: `data_apps_proxy` imports this module, so a
    module-level import would be circular.
    """
    from app.api.data_apps_proxy import _resolve_proxy_caller

    _feature_gate()
    user, via_preview = await run_in_threadpool(_resolve_proxy_caller, request, slug, None)
    if user is None:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    row = _get_row_or_404(slug)
    # `via_preview` is already pinned to THIS slug by the resolver's scope
    # check, which is what makes skipping the grant check safe here.
    if not via_preview and not _can_view(user, row):
        raise HTTPException(status_code=403, detail="forbidden")
    _reject_linked(row)

    # Subdomain-mode poll: let THIS app's own origin read the answer.
    response.headers.update(_readiness_cors_headers(request, slug))

    state = row["state"]
    ready = False
    if state in ("running", "deploying"):
        try:
            # Offloaded like every other sidecar call: this endpoint is the
            # one the holding page polls on a cadence, so a slow-to-answer
            # runner would otherwise stall the loop once per poll, per waking
            # app.
            status = await run_in_threadpool(_runner().status, slug)
            ready = bool(status.get("ready"))
        except (RunnerUnavailable, RunnerError):
            ready = False

    if state == "deploying" and ready:
        data_apps_repo().set_state(row["id"], "running")
        state = "running"

    return {"state": state, "ready": ready}


@router.post("/reap-idle")
async def reap_idle_data_apps(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin-only idle sweep (called by the scheduler, whose synthetic user
    is Admin). `idle_timeout_s` is per-app, so each `running` row is
    compared, in Python, against its *own* configured threshold rather than
    one shared value — a single `repo.list(state="running", ...)` scan
    (`list_idle` stays on the repo for callers that want SQL-side filtering
    against one shared threshold — e.g. any future admin/ops tooling — and
    remains contract-tested, but reap-idle itself no longer calls it per row).

    A runner failure on one app is recorded (state -> "error",
    state_detail carries the runner's message) and does not abort the rest
    of the sweep — one dead container must not wedge every other reap.

    Also checks `deploying` rows stuck longer than `_DEPLOY_STALE_TIMEOUT_S`
    (a wake or operator-deploy that never finished — e.g. the ingress
    proxy's backgrounded `_spawn_wake` task died without anything left to
    observe it, or a `POST .../deploy` request process crashed mid-flight)
    against the runner before declaring the app dead: if the runner reports
    the container is actually up and ready, the row is recovered to
    `running` (reported as `recovered`) rather than errored out from under
    a deploy that in fact succeeded; only when the runner says otherwise
    (or can't be reached) is the row flipped to `error` (reported as
    `timed_out`).
    """
    _feature_gate()
    repo = data_apps_repo()
    reaped: list[str] = []
    now = datetime.now(timezone.utc)
    for row in repo.list(state="running", limit=100000):
        last_request_at = row.get("last_request_at")
        if last_request_at is None:
            continue
        if last_request_at.tzinfo is None:
            last_request_at = last_request_at.replace(tzinfo=timezone.utc)
        if (now - last_request_at).total_seconds() <= row["idle_timeout_s"]:
            continue
        # Non-blocking: a row with a deploy/stop/wake already in flight is
        # left running and picked up on the next scheduler tick rather than
        # having this sweep block or race the in-flight operation's own
        # runner.stop()/up() call (see the op-lease invariant above).
        acquired, holder = try_acquire_op_lease(row["slug"])
        if not acquired:
            logger.info("reap-idle: skipping %s — another operation is in flight", row["slug"])
            continue
        stopped = False
        try:
            await run_in_threadpool(_runner().stop, row["slug"], row.get("sleep_mode") or "recreate")
            stopped = True
        except (RunnerUnavailable, RunnerError) as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            repo.set_state(row["id"], "error", f"reap-idle stop failed: {detail}")
            logger.warning("reap-idle: runner stop failed for %s: %s", row["slug"], detail)
        finally:
            # The state write must happen while still holding the lease —
            # releasing first (as a bare `finally: release_op_lease(...)`
            # would) opens a window where a concurrent deploy/wake grabs the
            # freed lease, starts a container, and then this sweep's
            # "sleeping" write lands after it and clobbers that state.
            #
            # Guarded on `stopped`, because `finally` also runs on the way out
            # of the `except` arm: writing "sleeping" unconditionally there
            # overwrote the `error` just recorded for a stop that FAILED, and
            # reported the slug as reaped while its container was still live —
            # the opposite of what this endpoint's docstring promises.
            if stopped:
                repo.set_state(row["id"], "sleeping")
                reaped.append(row["slug"])
            release_op_lease(row["slug"], holder)

    recovered: list[str] = []
    timed_out: list[str] = []
    for row in repo.list(state="deploying"):
        updated_at = row.get("updated_at")
        if updated_at is None:
            continue
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if (now - updated_at).total_seconds() <= _DEPLOY_STALE_TIMEOUT_S:
            continue
        ready = False
        try:
            status = await run_in_threadpool(_runner().status, row["slug"])
            ready = bool(status.get("ready"))
        except (RunnerUnavailable, RunnerError) as exc:
            logger.warning("reap-idle: status check failed for %s: %s", row["slug"], exc)
        if ready:
            repo.set_state(row["id"], "running")
            logger.info("reap-idle: %s was actually ready; recovered to running", row["slug"])
            recovered.append(row["slug"])
            continue
        repo.set_state(row["id"], "error", "wake/deploy timed out")
        logger.warning("reap-idle: %s stuck in deploying past %ds; marked error", row["slug"], _DEPLOY_STALE_TIMEOUT_S)
        timed_out.append(row["slug"])

    # Reconcile `running` rows whose container is actually dead. A first-deploy
    # crash loop is written to `running` (not `deploying`, see deploy_data_app),
    # so the stale-deploying scan above never catches it: readiness reports
    # ready:false forever and nothing flips the row. Bounded by the same start
    # grace as the deploying scan (`updated_at` older than _DEPLOY_STALE_TIMEOUT_S)
    # so a container that only just started is never second-guessed — a healthy
    # running container still reports "running" here regardless of age, so only a
    # genuinely dead ("stopped"/"absent") container is flipped to error.
    #
    # This works under the runtime's bounded `on-failure` policy
    # (`MaximumRetryCount: 3`, see `services/apps_runner/api.py::up`), which is
    # what ships today: while the daemon is still spending that retry budget a
    # crash-looping container alternates between Docker `running` and
    # `restarting`, and the runner folds `restarting` into "stopped", so the
    # loop is caught mid-flight; once the budget is exhausted the doomed
    # container settles as `exited` and reads "stopped" for good. A single
    # transient restart also reads dead either way, which is why a dead reading
    # still has to be confirmed by a second sweep before `error` is written
    # (below). The bounded policy also means Docker does not bring a container
    # back after a daemon or host restart, so a previously-live app settles as
    # `exited` after a reboot, is reconciled to `error` here, and needs an
    # explicit redeploy — the ingress proxy wakes only `sleeping` rows.
    reconciled: list[str] = []
    for row in repo.list(state="running", limit=100000):
        updated_at = row.get("updated_at")
        if updated_at is None:
            continue
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if (now - updated_at).total_seconds() <= _DEPLOY_STALE_TIMEOUT_S:
            continue
        # Probe WITHOUT the op lease. `updated_at` is only bumped by
        # `set_state`/`record_deploy` — never by `touch_last_request` — so
        # "stale `updated_at` while running" describes essentially every
        # healthy long-lived app, not a suspicious one. Taking the lease for
        # each of them would contend with a concurrent `POST /{slug}/deploy`
        # or `/stop` on a perfectly healthy app, and `require_op_lease` gives
        # up after a few 100 ms retries with 409 `operation_in_progress`. A
        # live container answers "running" here and costs no lease at all.
        try:
            status = await run_in_threadpool(_runner().status, row["slug"])
        except (RunnerUnavailable, RunnerError) as exc:
            logger.warning("reap-idle: reconcile status check failed for %s: %s", row["slug"], exc)
            continue
        if status.get("container") not in ("stopped", "absent"):
            # Alive. Drop a pending note from an earlier sweep so a single
            # transient restart cannot accumulate towards an `error` later.
            #
            # Guarded exactly like the dead path below, and for the same reason:
            # `row` came from a `repo.list(state="running")` snapshot taken
            # before this loop began, so a stop or a second sweep may have moved
            # the row to `sleeping`/`stopped` since — and the container is not
            # removed until that stop completes, so the probe above can still
            # report "running". An unguarded write would put `running` back over
            # it, leaving the registry claiming a container that is gone and the
            # proxy latching `error` on the next request. The lease is only taken
            # when there is actually a note to clear, so a healthy app still
            # costs no lease on the common path.
            if _RECONCILE_PENDING in (row.get("state_detail") or ""):
                acquired, holder = try_acquire_op_lease(row["slug"])
                if not acquired:
                    logger.info(
                        "reap-idle: leaving %s's pending note for the next sweep — an operation is in flight",
                        row["slug"],
                    )
                    continue
                try:
                    fresh = repo.get(row["id"])
                    if (
                        fresh is not None
                        and fresh["state"] == "running"
                        and _RECONCILE_PENDING in (fresh.get("state_detail") or "")
                    ):
                        repo.set_state(row["id"], "running", "")
                        logger.info(
                            "reap-idle: %s recovered before confirmation; cleared the pending note", row["slug"]
                        )
                finally:
                    release_op_lease(row["slug"], holder)
            continue

        # It looks dead — now the lease is required before judging it, and
        # only now. `deploy_data_app` holds the lease for the whole deploy
        # while leaving the row in `running` with an arbitrarily old
        # `updated_at` (it writes "running" again only once `redeploy_current`
        # returns, up to `up_timeout()` = 600 s on a cold image pull), and the
        # runner's `up()` removes the old container BEFORE creating the new
        # one. So a mid-deploy app reports `absent` entirely legitimately, and
        # flipping it to `error` would stick: the ingress proxy latches on
        # `error` and never re-checks, leaving only a manual redeploy.
        acquired, holder = try_acquire_op_lease(row["slug"])
        if not acquired:
            logger.info("reap-idle: skipping reconcile of %s — another operation is in flight", row["slug"])
            continue
        try:
            try:
                # Re-probe under the lease. A deploy or stop that finished
                # between the probe above and this acquisition released the
                # lease, so the container can be up again even though the row
                # never left `running` — one more round-trip, paid only for an
                # app that already looked dead.
                status = await run_in_threadpool(_runner().status, row["slug"])
            except (RunnerUnavailable, RunnerError) as exc:
                logger.warning("reap-idle: reconcile status check failed for %s: %s", row["slug"], exc)
                continue
            container = status.get("container")
            if container not in ("stopped", "absent"):
                continue
            # And re-read the row: `absent` is the correct container state for
            # the `sleeping` row a concurrent stop just produced, not an error
            # to record over it.
            fresh = repo.get(row["id"])
            if fresh is None or fresh["state"] != "running":
                continue
            # One dead reading is not proof. The runner folds Docker's
            # `restarting` into "stopped", so a healthy app that exited once and
            # is inside Docker's restart backoff (delay doubles up to ~1 min)
            # reads dead — and both probes above are milliseconds apart, so the
            # second one does not decorrelate that. Writing `error` there would
            # latch a container that was about to come back on its own, and the
            # ingress proxy never re-checks `error`.
            #
            # So the first sighting only records a note, leaving the row
            # `running`. That write bumps `updated_at`, which puts the row back
            # outside this scan's start grace — so the confirming look happens a
            # grace period later (10 min, an order of magnitude past Docker's
            # backoff cap) without needing any state of its own. A container that
            # recovered reports "running" then and the note is cleared; one still
            # dead is dead.
            if _RECONCILE_PENDING not in (fresh.get("state_detail") or ""):
                repo.set_state(row["id"], "running", f"{_RECONCILE_PENDING}: container {container}")
                logger.info(
                    "reap-idle: %s state=running but container %s; confirming on the next sweep",
                    row["slug"],
                    container,
                )
                continue
            repo.set_state(row["id"], "error", f"container {container} while state=running")
            logger.warning("reap-idle: %s state=running but container %s; marked error", row["slug"], container)
            reconciled.append(row["slug"])
        finally:
            release_op_lease(row["slug"], holder)

    _audit(
        conn,
        user["id"],
        "data_app.reap_idle",
        "data_app:*",
        {"reaped": reaped, "timed_out": timed_out, "recovered": recovered, "reconciled": reconciled},
    )
    return {"reaped": reaped, "timed_out": timed_out, "recovered": recovered, "reconciled": reconciled}
