"""Keboola OAuth login provider.

Same shape as the Google provider: authlib redirect flow with session-backed
``state``, then a session cookie. Identity comes from verifying the OAuth
access token against the stack's /tokens/verify (see keboola_verify — the
master-token/project/role gates live there). First login auto-provisions via
the shared helper; membership in the configured project is the trust
boundary, so the allowed_domain filter is deliberately NOT applied here.
"""

import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.auth import keboola_provisioning as kprov
from app.auth._common import safe_next_path
from app.auth.jwt import SESSION_COOKIE_MAX_AGE_SECONDS, create_access_token
from app.auth.provider_registry import require_provider
from app.auth.providers import keboola_projects as kp
from app.auth.providers import keboola_verify as kv
from app.auth.provisioning import UserDeactivatedError, ensure_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth/keboola",
    tags=["auth"],
    dependencies=[Depends(require_provider("keboola"))],
)

oauth = OAuth()

# Config the current "keboola" registration was built from —
# (client_id, client_secret, oauth_host). See _oauth_client.
_client_fingerprint: tuple[str, str, str | None] | None = None

# KeboolaVerifyError.reason → /login?error=<code>. Every code has copy in
# login.html; anything unmapped falls back to the generic failure.
_ERROR_CODE_BY_REASON = {
    "project_mismatch": "keboola_project_mismatch",
    "not_master_token": "keboola_not_permitted",
    # The login path's own master-token failure: the assumption that an
    # interactive OAuth token always verifies as a master token is
    # platform-unverified (see keboola_verify's module docstring), so if it
    # ever fails it gets a self-describing code instead of the generic
    # not-permitted banner.
    "oauth_not_master_token": "keboola_oauth_not_master",
    "role_forbidden": "keboola_not_permitted",
    "no_admin_identity": "keboola_not_permitted",
    "invalid_token": "keboola_oauth_failed",
    "verify_failed": "keboola_oauth_failed",
    "not_configured": "keboola_not_configured",
}


def is_available() -> bool:
    """Config-completeness only — the allowlist is a separate layer (spec).

    A discovery mode (``auth.keboola.multi_project_mode`` = auto/select)
    stands in for the project binding: the wildcard instance is complete
    without a concrete ``project_id``.
    """
    if not (kv.client_id() and kv.client_secret() and kv.stack_url()):
        return False
    return bool(kv.configured_project_id() or kv.multi_project_active())


def _oauth_client():
    """Register (or re-register) the authlib client for the CURRENT config.

    Config is instance.yaml, read at first use — unlike Google's import-time
    env vars it can change at runtime (server-config overlay save: secret
    rotation, oauth_host/stack_url move). authlib's registry caches the
    client object per name forever, so a register-once client would keep
    signing with the stale credentials until restart while is_available()
    reads live (Devin Review on PR #1288). A fingerprint of the effective
    config is compared on every call; on change the cached client is dropped
    and re-registered. Safe to call repeatedly."""
    global _client_fingerprint
    fingerprint = (kv.client_id(), kv.client_secret(), kv.oauth_host())
    client = oauth.create_client("keboola")
    if client is not None and fingerprint == _client_fingerprint:
        return client
    # First use, or the config changed since registration: rebuild. authlib
    # keeps instantiated clients in `oauth._clients` (create_client returns
    # the cached instance before ever consulting the registry), so the stale
    # instance must be evicted for a fresh `register` to take effect.
    oauth._clients.pop("keboola", None)
    host = fingerprint[2]
    oauth.register(
        name="keboola",
        client_id=fingerprint[0],
        client_secret=fingerprint[1],
        authorize_url=f"{host}/oauth/authorize",
        access_token_url=f"{host}/oauth/token",
        client_kwargs={"scope": "email"},
    )
    _client_fingerprint = fingerprint
    return oauth.create_client("keboola")


@router.get("/login")
async def keboola_login(request: Request):
    """Redirect to the Keboola OAuth authorize endpoint (state in session)."""
    if not is_available():
        return RedirectResponse(url="/login?error=keboola_not_configured", status_code=302)
    next_path = safe_next_path(request.query_params.get("next"), default="")
    if next_path:
        request.session["login_next"] = next_path
    else:
        request.session.pop("login_next", None)
    redirect_uri = str(request.url_for("keboola_callback"))
    return await _oauth_client().authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def keboola_callback(request: Request):
    """Exchange the code, verify the access token at the stack, sign in."""
    if not is_available():
        return RedirectResponse(url="/login?error=keboola_not_configured", status_code=302)
    try:
        # SSRF: oauth_host is re-validated at use time (not just when
        # stored), same posture as stack_url in
        # keboola_verify._fetch_verify — a DNS-rebind or config edit
        # between store and use must not point the token exchange at a
        # private/internal address.
        from app.api.admin import _validate_url_not_private

        # Offload — _validate_url_not_private resolves the host (blocking DNS),
        # which must not run on the single event loop (Tier-1 convention).
        await run_in_threadpool(_validate_url_not_private, kv.oauth_host(), "auth.keboola.oauth_host")
        token = await _oauth_client().authorize_access_token(request)
    except Exception:
        logger.exception("Keboola OAuth token exchange failed")
        return RedirectResponse(url="/login?error=keboola_oauth_failed", status_code=302)
    access_token = str(token.get("access_token") or "")
    # Backstop: any unexpected failure in the post-exchange flow (verify →
    # provisioning → JWT → cookie) must land on the friendly login banner,
    # never a raw 500. The specific except branches below return their own
    # redirects from inside the try, so the backstop never shadows them.
    try:
        try:
            # Sync HTTP verify off the event loop (same Tier-1 posture as auth).
            identity = await run_in_threadpool(kv.verify_oauth_access_token, access_token)
        except kv.KeboolaVerifyError as exc:
            logger.info("Keboola login rejected: %s", exc.reason)
            code = _ERROR_CODE_BY_REASON.get(exc.reason, "keboola_oauth_failed")
            return RedirectResponse(url=f"/login?error={code}", status_code=302)
        # Multi-project discovery (auto/select modes). Under the wildcard
        # gate this IS the trust boundary — "member of at least one
        # allowed-role project" — so a discovery failure or an empty result
        # fails the login closed. With a concrete project_id the
        # single-project verify above already enforced the boundary, and
        # discovery only feeds provisioning: there it fails soft.
        discovery: list[kp.DiscoveredProject] | None = None
        if kv.multi_project_active():
            try:
                discovery = await run_in_threadpool(kp.discover_allowed_projects, access_token)
            except kp.KeboolaProjectApiError as exc:
                logger.warning("Keboola login project discovery failed: %s", exc.reason)
                if kv.is_wildcard_project():
                    return RedirectResponse(url="/login?error=keboola_oauth_failed", status_code=302)
                discovery = None
            except Exception:
                # The fail-soft promise above must hold for ANY surprise, not
                # just the client's typed errors — a pinned-project login has
                # already passed its trust boundary (the single-project
                # verify), and a provisioning-only helper crashing on an
                # unexpected introspect shape must not break it. Under the
                # wildcard discovery IS the boundary, so the same surprise
                # stays fail-closed via the outer backstop.
                if kv.is_wildcard_project():
                    raise
                logger.warning("Keboola login project discovery failed unexpectedly", exc_info=True)
                discovery = None
            if discovery is not None and not discovery and kv.is_wildcard_project():
                logger.info("Keboola login rejected: no project with an allowed role")
                return RedirectResponse(url="/login?error=keboola_not_permitted", status_code=302)

        try:
            user = await run_in_threadpool(
                ensure_user, identity.email, identity.name, source="auth.keboola:first-signin"
            )
        except UserDeactivatedError:
            return RedirectResponse(url="/login?error=deactivated", status_code=302)

        # Provisioning never blocks a login that already passed its gates.
        # auto: the WHOLE per-project pass (PAT mint/verify round-trips, up
        # to ~10 s each) plus the slow tail (chat-tools MCP introspection,
        # semantic-layer refresh) rides a post-response background task —
        # a user who reaches many projects must not wait through, or be
        # proxy-timed-out of, their own sign-in (Devin Review on this PR).
        # Memberships therefore land moments after the redirect, not before
        # it. select: the stash + membership sync for already-connected
        # projects are cheap (no upstream HTTP) and MUST be readable the
        # instant the user lands on the projects page, so they stay inline.
        provisioning_tail: BackgroundTask | None = None
        if discovery:
            mode = kv.multi_project_mode()
            try:
                if mode == "auto":
                    provisioning_tail = BackgroundTask(kprov.run_login_provisioning, user, discovery, access_token)
                elif mode == "select":
                    # Membership stays synced for already-imported projects;
                    # the rest wait for the user's import decision against
                    # the stashed discovery (/api/auth/keboola/projects).
                    await run_in_threadpool(kprov.provision_projects, user, [], discovery, access_token)
                    await run_in_threadpool(kprov.store_pending_discovery, user, discovery, access_token)
            except Exception:  # noqa: BLE001 — deliberately soft: the login itself is good
                logger.warning("Keboola login provisioning failed; login proceeds", exc_info=True)

        jwt_token = create_access_token(user["id"], user["email"])
        from app.auth.login_audit import audit_login_success

        audit_login_success(user["id"], provider="keboola", request=request)
        target = safe_next_path(request.session.pop("login_next", None))

        from app.auth.public_url import cookie_secure
        from app.instance_config import session_cookie_domain

        response = RedirectResponse(url=target, status_code=302, background=provisioning_tail)
        response.set_cookie(
            key="access_token",
            value=jwt_token,
            httponly=True,
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            samesite="lax",
            secure=cookie_secure(request),
            domain=session_cookie_domain(),
        )
        return response
    except Exception:
        # Deliberately no token/identity details in the log record.
        logger.exception("Keboola OAuth callback failed after token exchange")
        return RedirectResponse(url="/login?error=keboola_oauth_failed", status_code=302)
