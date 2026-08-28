"""Auth endpoints — login, token generation, bootstrap."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import duckdb
from argon2 import PasswordHasher

from app.auth.jwt import create_access_token
from app.auth.access import is_user_admin
from app.auth.dependencies import _get_db, get_current_user
from app.auth.provider_registry import require_provider
from app.auth.rate_limit import limiter as _rate_limiter
from src.db import SYSTEM_ADMIN_GROUP
from src.user_identity import normalize_email

from src.repositories import (
    audit_repo,
    user_curated_subscriptions_repo,
    user_group_members_repo,
    user_groups_repo,
    users_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenRequest(BaseModel):
    email: str
    password: str = ""


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    role: str


class BootstrapRequest(BaseModel):
    email: str
    name: str = ""
    password: str = ""


def _audit(user_id: str, action: str, result: str | None = None) -> None:
    """Fire-and-forget audit log entry. Swallows all errors.

    ``audit_repo()`` is factory-routed (DuckDB or Postgres per ``use_pg()``)
    and opens its own backend connection, so no system-DuckDB handle is needed
    here — opening one would create a stale ``state/system.duckdb`` on a
    Postgres instance (and is a hard error once the invariant is enforced).
    """
    try:
        audit_repo().log(
            user_id=user_id,
            action=action,
            resource="auth",
            result=result,
        )
    except Exception:
        pass  # Audit failure must not block auth


@router.post(
    "/token",
    response_model=TokenResponse,
    dependencies=[Depends(require_provider("password"))],
)
@_rate_limiter.limit("10/minute")
async def create_token(
    request: Request,
    body: TokenRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Issue a JWT token. Requires password authentication.

    Gated by the ``password`` provider allowlist entry (route-level
    ``Depends`` rather than an inline check) — the body is a JSON Pydantic
    model, and FastAPI resolves body validation together with dependencies
    in ``solve_dependencies()``; an inline guard as the first line of the
    function never runs when the body fails to parse (e.g. a form-encoded
    POST), so an excluded provider would 422 instead of 404. The
    dependency raises before body validation gets a chance to.
    """
    repo = users_repo()
    # Strip only — case is folded by the lookup (SQL). A pasted address
    # carries whitespace and must not be a hard auth failure.
    email = (body.email or "").strip()
    from app.auth.providers.sso import sso_forced_for_email

    if sso_forced_for_email(email):
        # Forced to the sso door — the copy this endpoint already answers
        # for accounts with no password hash.
        raise HTTPException(
            status_code=401,
            detail="This account uses external authentication. Please log in via your configured provider.",
        )
    canonical = repo.get_by_email_ci(email)
    if not canonical:
        raise HTTPException(status_code=401, detail="User not found")

    # `canonical` answers "which account is this address" (oldest wins), which
    # is the wrong resolution for a credential: where two case variants of one
    # address coexist, the password hash may sit on the newer row. So the
    # password selects the row — see `_row_verifying_password` in the password
    # provider, whose contract this mirrors — and only then is `active` judged,
    # on the row that actually proved the credential.
    from app.auth.providers.password import _row_verifying_password

    if not body.password:
        # Distinguishing "no password set" from "password required" needs no
        # per-row resolution: an address whose every row lacks a hash is an
        # external-auth account either way.
        if not any(u.get("password_hash") for u in repo.list_by_email_ci(email)):
            raise HTTPException(
                status_code=401,
                detail="This account uses external authentication. Please log in via your configured provider.",
            )
        raise HTTPException(status_code=401, detail="Password required")

    try:
        user, any_hash = _row_verifying_password(repo, email, body.password)
    except Exception:
        logger.exception("Unexpected error during password verification")
        raise HTTPException(status_code=500, detail="Internal server error")
    if user is None:
        if not any_hash:
            raise HTTPException(
                status_code=401,
                detail="This account uses external authentication. Please log in via your configured provider.",
            )
        _audit(canonical["id"], "login_failed", result="invalid_password")
        raise HTTPException(status_code=401, detail="Invalid password")
    if not bool(user.get("active", True)):
        _audit(user["id"], "login_failed", result="deactivated")
        raise HTTPException(status_code=401, detail="Account deactivated")

    role_label = "admin" if is_user_admin(user["id"], conn) else "user"
    token = create_access_token(
        user_id=user["id"],
        email=user["email"],
    )
    _audit(user["id"], "token_created")
    return TokenResponse(
        access_token=token,
        user_id=user["id"],
        email=user["email"],
        role=role_label,
    )


@router.post("/bootstrap", response_model=TokenResponse)
@_rate_limiter.limit("3/minute")
async def bootstrap(
    request: Request,
    body: BootstrapRequest,
):
    """Bootstrap the first admin account.

    Allowed when no user has a password_hash yet. This covers:
    (a) No users exist at all.
    (b) Only seed users (created by SEED_ADMIN_EMAIL at startup) exist, which
        have no password and cannot log in — bootstrap lets the operator
        activate them with a password.

    If a user with the given email already exists (e.g. as a seed), this
    endpoint sets its password_hash (or clears it, if no password was supplied —
    useful for OAuth-only flows) and promotes it to admin.

    Locked once the instance has an admin (or any password-holding user), unless
    the caller presents a valid ``X-Bootstrap-Token`` header matching the
    operator-set ``AGNES_BOOTSTRAP_TOKEN`` — the escape hatch for
    destroy-recreate runbooks.
    """
    repo = users_repo()
    existing = repo.list_all()

    # Bootstrap is locked once the instance already has an admin — with the
    # older "any user has a password_hash" rule kept as a belt-and-suspenders
    # fallback.
    #
    # SECURITY (pre-launch hardening): this endpoint is UNAUTHENTICATED, so its
    # only safe window is genuine first-install, before any admin exists. Keying
    # the lock solely on password_hash left bootstrap PERMANENTLY OPEN on OAuth /
    # magic-link-only deployments (where no user ever gets a password) and open
    # across the pre-bootstrap window on every fresh boot — letting an
    # unauthenticated caller mint themselves, or overwrite an existing account,
    # into the Admin god-mode group. A seed admin (SEED_ADMIN_EMAIL) is added to
    # the Admin group at startup, so seed-based deployments lock from first boot;
    # provision the first admin that way (set SEED_ADMIN_PASSWORD for password
    # login) rather than via this endpoint once users exist. The Admin-member
    # check (not "any user exists") intentionally still permits first-install on
    # a no-seed instance where only the synthetic scheduler user is present.
    #
    # Escape hatch: an operator who sets AGNES_BOOTSTRAP_TOKEN can re-bootstrap
    # even after an admin exists (destroy-recreate runbook) by presenting it in
    # the X-Bootstrap-Token header — knowledge of the server-side secret stands
    # in for "no admin yet". Timing-safe compare; without the token an existing
    # admin locks the endpoint.
    import hmac
    import os

    # "Admin exists" must mean a HUMAN admin. The synthetic scheduler service
    # user (scheduler@system.local) is auto-added to the Admin group at startup
    # whenever SCHEDULER_API_TOKEN is set (see ensure_scheduler_user), so a raw
    # member count would wrongly lock first-install bootstrap on a fresh
    # scheduler-token instance that has no real admin yet. Exclude it by email.
    from app.auth.scheduler_token import SCHEDULER_USER_EMAIL

    admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
    admin_members = user_group_members_repo().list_members_for_group(admin_group["id"]) if admin_group else []
    admin_exists = any(m.get("email") != SCHEDULER_USER_EMAIL for m in admin_members)
    users_with_password = [u for u in existing if u.get("password_hash")]

    bootstrap_token = os.environ.get("AGNES_BOOTSTRAP_TOKEN") or ""
    presented_token = request.headers.get("x-bootstrap-token", "")
    token_ok = bool(bootstrap_token) and hmac.compare_digest(presented_token, bootstrap_token)

    if (admin_exists or users_with_password) and not token_ok:
        raise HTTPException(
            status_code=403,
            detail=(
                "Bootstrap disabled — this instance is already initialised (an "
                "admin or a password-holding user exists). Provision the first "
                "admin via SEED_ADMIN_EMAIL / sign in and manage admins under "
                "/admin/access, or present a valid X-Bootstrap-Token header when "
                "AGNES_BOOTSTRAP_TOKEN is configured."
            ),
        )

    password_hash = PasswordHasher().hash(body.password) if body.password else None

    # If a matching user already exists (e.g. seed), update it; else create fresh.
    # Resolved through the SHARED resolver, not by scanning `existing` — that
    # list is ordered by email, so picking the first case-insensitive match
    # tie-breaks alphabetically while every sign-in door tie-breaks on oldest.
    # With two case variants present, bootstrap would then set the password on
    # a row no sign-in path ever resolves to. (`existing` is still the read
    # behind the lockout check above.)
    normalized_email = normalize_email(body.email)
    # Shape check after normalization — the same one `POST /api/users` gained,
    # applied here because this is the MORE privileged write: whitespace-only or
    # missing input collapses to "", and the create branch below would then mint
    # an ADMIN account whose identity no auth provider can ever resolve (and a
    # second such call would collide on UNIQUE(email)). Deliberately minimal:
    # an `@` with something either side, not RFC 5322, so an internal or dev
    # address still works.
    _local, _, _domain = normalized_email.partition("@")
    if not _local or not _domain:
        raise HTTPException(status_code=422, detail="A valid email address is required")
    existing_user = repo.get_by_email_ci(normalized_email)
    if existing_user:
        user_id = existing_user["id"]
        repo.update(id=user_id, password_hash=password_hash)
        _audit(user_id, "bootstrap_activated_seed")
    else:
        user_id = str(uuid.uuid4())
        repo.create(
            id=user_id,
            email=normalized_email,
            name=body.name or normalized_email.split("@")[0],
            password_hash=password_hash,
        )
        # v39: bootstrap user is the very first user; on first install
        # there are no system plugins yet so the fanout is a noop. Wire
        # it anyway so the later bootstrap-of-rebuilt-instance path (rare
        # but supported) inherits the existing mandatory tier.
        try:
            user_curated_subscriptions_repo().fanout_system_for_user(user_id)
        except Exception:
            logger.exception(
                "system-plugin fanout failed for bootstrap user %s",
                body.email,
            )
        _audit(user_id, "bootstrap_completed")

    # Promote the bootstrap user to the Admin system group — replaces the v9
    # ``user_role_grants`` write that the old bootstrap path relied on. Look the
    # group up through the factory so we get the ACTIVE backend's id: a raw
    # _get_db (always-DuckDB) read returned the DuckDB Admin-group id, and the
    # membership written to Postgres then referenced an id absent from PG, so
    # the bootstrapped first admin had no admin access on a Postgres instance.
    admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
    if admin_group:
        user_group_members_repo().add_member(
            user_id=user_id,
            group_id=admin_group["id"],
            source="system_seed",
            added_by="auth.bootstrap",
        )

    # Issue #748: also grant Everyone (unless AGNES_GROUP_EVERYONE_EMAIL maps
    # it to a Workspace group). Bootstrap is a first-install flow that runs
    # for both the create and activate-existing-seed branches above, so this
    # sits at the same shared point as the Admin grant rather than inside
    # either branch — opt-out is not meaningful here (there's no "later" to
    # opt out from before the very first admin exists). Fail-soft like the
    # other creation paths: the grant is not critical to issuing the token.
    try:
        from app.auth.group_sync import ensure_everyone_membership

        ensure_everyone_membership(user_id, added_by="auth.bootstrap")
    except Exception:
        logger.exception(
            "ensure_everyone_membership failed for bootstrap user %s",
            body.email,
        )

    # The account's own address, not the spelling that was typed.
    account_email = (existing_user or {}).get("email") or normalized_email
    token = create_access_token(user_id=user_id, email=account_email)
    return TokenResponse(
        access_token=token,
        user_id=user_id,
        email=account_email,
        role="admin",
    )


class RefreshGroupsResponse(BaseModel):
    """Response shape for ``POST /auth/refresh-groups``.

    ``applied``: True iff the synced membership set was rewritten. False when
    ``soft_failed`` (transient Admin SDK failure / empty fetch — previous
    snapshot preserved) or ``denied`` (prefix filter excluded every fetched
    group — caller has no eligible group on this instance).

    ``added``/``removed``: diff of synced (``source='google_sync'``) rows
    versus the snapshot before the call. Admin- and seed-sourced rows are
    untouched and excluded from the diff. Reported as group display names
    (Workspace email for synced rows, ``Admin``/``Everyone`` for mapped
    system rows).

    ``current``: every group name the caller is in **after** the refresh
    across all sources (admin, seed, google_sync). Useful for the CLI to
    show the user where their access stands.
    """

    applied: bool
    denied: bool = False
    soft_failed: bool = False
    fetched: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    current: list[str] = []


@router.post("/refresh-groups", response_model=RefreshGroupsResponse)
@_rate_limiter.limit("5/minute")
async def refresh_groups(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-sync the caller's Workspace group memberships against the Admin SDK.

    Hot path the OSS callback covers via the browser OAuth round-trip; this
    endpoint is the CLI / PAT counterpart so a user who's been added to a
    new Workspace group between their last browser sign-ins can refresh
    without re-logging in. Reuses the same write path as the OAuth
    callback (``app.auth.group_sync.apply_user_groups``), so policy
    (prefix filter, admin/everyone mapping, fail-soft on empty fetch)
    stays consistent.

    Returns the diff of synced rows + the post-refresh group set so the
    caller can see exactly what changed. Rate-limited at 5/min/IP — the
    slowapi default key is the request's remote IP, not the authenticated
    user, so a shared-NAT / VPN scenario divides the budget across users
    on the same egress. Matches the pattern of the other rate-limited
    endpoints in this router (``/token``, ``/bootstrap``). Refreshing is
    cheap on our side but each call costs a Workspace Admin SDK quota
    unit, so the limit guards the upstream quota.
    """
    from app.auth.group_sync import apply_user_groups

    # Read the membership graph through the repo factory so the diff
    # computation runs against the active backend — `user_group_members_repo()`
    # routes to Postgres when `use_pg()` is True, matching where
    # `apply_user_groups` writes (it uses the same factory internally).
    # The `conn` dependency is a DuckDB cursor for legacy callers, but it's
    # not what we want for the read-back here; using it would produce a
    # `before == after` (both empty/stale) and a lying response on PG.
    # See PR #520 Devin review for the original drift report.
    members_repo = user_group_members_repo()

    def _synced_names() -> set[str]:
        return {
            row["name"]
            for row in members_repo.list_groups_with_meta_for_user(user["id"])
            if row["source"] == "google_sync"
        }

    def _all_names() -> list[str]:
        return sorted(row["name"] for row in members_repo.list_groups_with_meta_for_user(user["id"]))

    before = _synced_names()
    result = apply_user_groups(user["id"], user["email"], conn)
    after = _synced_names() if result.applied else before

    added = sorted(after - before)
    removed = sorted(before - after)
    current = _all_names()

    _audit(
        user["id"],
        "auth.refresh_groups",
        result=("applied" if result.applied else "denied" if result.denied else "soft_failed"),
    )

    return RefreshGroupsResponse(
        applied=result.applied,
        denied=result.denied,
        soft_failed=result.soft_failed,
        fetched=result.fetched,
        added=added,
        removed=removed,
        current=current,
    )
