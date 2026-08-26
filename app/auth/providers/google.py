"""Google OAuth provider for FastAPI.

Group memberships are sourced via Application Default Credentials in
``app.auth.group_sync.fetch_user_groups`` (no per-user OAuth scope needed for
that path), so the OAuth flow only handles authentication and returns a
session JWT. Membership writes go through ``app.auth.group_sync.apply_user_groups``,
shared with ``POST /auth/refresh-groups`` so the OAuth-only refresh limitation
is no longer a thing — CLI / PAT-driven users can re-sync without a browser
sign-in.
"""

import os
import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.auth.jwt import create_access_token, SESSION_COOKIE_MAX_AGE_SECONDS
from app.auth._common import safe_next_path
from app.auth.provider_registry import require_provider
from app.instance_config import get_allowed_domains


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth/google",
    tags=["auth"],
    dependencies=[Depends(require_provider("google"))],
)

oauth = OAuth()

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")


def is_available() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def startup_warnings() -> list[str]:
    """Operator-facing boot messages, emitted from ``app.main``'s lifespan.

    Silence here means "configured and pinned"; an unconfigured instance says
    nothing at all (Google login is opt-in) — mirrors
    ``app.auth.providers.microsoft.startup_warnings``. Unlike Microsoft,
    Google has no tenant to serve as even a partial identity boundary: with
    ``auth.allowed_domain`` unset, ANY Google account can sign in and
    self-provision an Agnes account. That gap had no boot-time signal until
    now (RBAC review on PR #1569).
    """
    if not is_available():
        return []
    if not get_allowed_domains():
        return [
            "Google sign-in is enabled but auth.allowed_domain is unset. There is no "
            "tenant or other boundary behind Google OAuth — any Google account can sign "
            "in and self-provision an account. Pin auth.allowed_domain to the domains "
            "you own."
        ]
    return []


def _setup_oauth():
    if not is_available():
        return
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


_setup_oauth()


@router.get("/login")
async def google_login(request: Request):
    """Redirect to Google OAuth.

    Honors `?next=<path>` by stashing the sanitized value in the session so the
    callback can redirect there instead of the default /dashboard. The session
    is the right stash — OAuth flow is stateful and the `state` param is
    managed by Authlib.
    """
    if not is_available():
        return RedirectResponse(url="/login?error=google_not_configured")
    next_path = safe_next_path(request.query_params.get("next"), default="")
    if next_path:
        request.session["login_next"] = next_path
    else:
        # Clear any stale value from an earlier aborted attempt.
        request.session.pop("login_next", None)
    redirect_uri = str(request.url_for("google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def google_callback(request: Request):
    """Handle Google OAuth callback."""
    if not is_available():
        return RedirectResponse(url="/login?error=google_not_configured")

    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get("userinfo", {})
        email = user_info.get("email", "")
        name = user_info.get("name", "")

        if not email:
            return RedirectResponse(url="/login?error=no_email")

        # Domain check
        allowed = get_allowed_domains()
        if allowed:
            # Both sides folded: the allowlist is lower-cased by
            # get_allowed_domains(), and the claim is whatever the IdP sent.
            domain = email.split("@")[-1].lower()
            if domain not in allowed:
                return RedirectResponse(url="/login?error=domain_not_allowed")

        # Find or create user, sync Workspace group memberships into
        # user_group_members.
        from src.db import get_system_db
        from src.repositories import use_pg
        from app.auth.group_sync import apply_user_groups

        # On Postgres the system state lives in PG; opening the system DuckDB
        # here would create a stale ``state/system.duckdb`` (and is a hard error
        # once the invariant is enforced). ``apply_user_groups`` and the repo
        # calls below all route through the factory under ``use_pg()`` and
        # ignore ``conn``, so ``None`` is safe on PG.
        conn = None if use_pg() else get_system_db()
        try:
            from app.auth.provisioning import UserDeactivatedError, ensure_user

            try:
                user = ensure_user(email, name, source="auth.google:first-signin")
            except UserDeactivatedError:
                return RedirectResponse(url="/login?error=deactivated")

            # Sync Workspace groups → user_group_members (source='google_sync').
            # Shared write path with /auth/refresh-groups so post-OAuth-callback
            # refreshes use the same logic. Fail-soft: ``apply_user_groups``
            # never raises; on transient API failure it returns
            # ``soft_failed=True`` and preserves the previous snapshot.
            sync_result = apply_user_groups(user["id"], email, conn)

            # Login gate: ``denied=True`` means the prefix filter is configured
            # and the Admin SDK returned a non-empty fetch that contained zero
            # groups matching the prefix — i.e. the user is signed into Google
            # but is not a member of any group permitted to use this Agnes
            # instance. ``soft_failed`` (empty fetch / API error) does NOT
            # trigger the gate, so transient outages can't lock users out.
            if sync_result.denied:
                return RedirectResponse(url="/login?error=not_in_allowed_group")
        finally:
            if conn is not None:
                conn.close()

        # Issue JWT — identity-only, authorization derives from
        # user_group_members at request time (see app.auth.access).
        jwt_token = create_access_token(user["id"], user["email"])

        # Redirect to the post-login target. Prefer the value stashed by
        # google_login() — re-sanitize defensively in case of session tampering.
        # default=None → safe_next_path resolves to the operator-configured
        # home route (AGNES_HOME_ROUTE / instance.home_route / /dashboard).
        target = safe_next_path(request.session.pop("login_next", None))

        # Redirect to target with token in cookie. Secure whenever served over
        # HTTPS (proxy-aware via request scheme + resolved public origin), not
        # only when DOMAIN is set — see app.auth.public_url.cookie_secure.
        from app.auth.public_url import cookie_secure

        use_secure = cookie_secure(request)
        response = RedirectResponse(url=target, status_code=302)
        from app.instance_config import session_cookie_domain

        response.set_cookie(
            key="access_token",
            value=jwt_token,
            httponly=True,
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            samesite="lax",
            secure=use_secure,
            domain=session_cookie_domain(),
        )
        return response

    except Exception as e:
        logger.error(f"Google OAuth error: {e}")
        return RedirectResponse(url="/login?error=oauth_failed")
