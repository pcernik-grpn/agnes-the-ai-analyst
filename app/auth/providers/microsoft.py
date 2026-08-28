"""Microsoft Entra ID (Azure AD) OAuth provider for FastAPI.

Single-tenant sign-in only, and that is enforced rather than promised:
``MICROSOFT_TENANT_ID`` is validated (:func:`tenant_id_error`) to be a
directory GUID or a verified domain before it is ever interpolated into the
discovery URL, and the three reserved multi-tenant endpoints
(``common`` / ``organizations`` / ``consumers``) are refused by name. A tenant
that fails validation leaves the provider UNAVAILABLE — the login button is
hidden, ``/auth/microsoft/*`` answers ``microsoft_not_configured`` — with a
loud boot error, so an instance can never silently come up multi-tenant.

**Trust model.** One tenant is the authentication boundary, not by itself the
identity boundary: B2B guest accounts invited into the tenant sign in too, and
their ``email`` claim carries their EXTERNAL address. Since ``ensure_user``
matches accounts by the email string alone (no provider column, no IdP-subject
binding), a guest can land on an account another provider created. Pin
``auth.allowed_domain`` — :func:`startup_warnings` says so at boot when it is
unset. See ``docs/auth-microsoft-oauth.md``.

On success the user is created (or matched) via the shared ``ensure_user``
provisioning path and granted Everyone membership. Group sync mirrors
``app.auth.providers.google``'s ``apply_user_groups`` — Entra ID group
memberships (Graph ``GET /me/memberOf``) are written into
``user_group_members`` (``source='microsoft_sync'``) via
``app.auth.microsoft_group_sync.apply_user_groups`` — but is config-gated
and OFF by default (``auth.microsoft.group_sync_enabled`` /
``AGNES_MICROSOFT_GROUP_SYNC_ENABLED``, see ``app.switches``); see
``docs/auth-microsoft-oauth.md`` for the Entra app permission it needs and
the admin-consent step.
"""

import os
import logging
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.auth.jwt import create_access_token, SESSION_COOKIE_MAX_AGE_SECONDS
from app.auth._common import safe_next_path
from app.auth.provider_registry import require_provider
from app.instance_config import get_allowed_domains


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth/microsoft",
    tags=["auth"],
    dependencies=[Depends(require_provider("microsoft"))],
)

oauth = OAuth()

MICROSOFT_CLIENT_ID = os.environ.get("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_TENANT_ID = os.environ.get("MICROSOFT_TENANT_ID", "")


# The three Microsoft-reserved endpoint names. Each is a legal path segment in
# the discovery URL and each turns this provider into exactly the multi-tenant
# configuration it says it is not: `common` = any work/school OR personal
# account, `organizations` = any work/school account in ANY tenant,
# `consumers` = any personal Microsoft account. With `auth.allowed_domain`
# unset that means any Microsoft account on earth signs in and self-provisions.
#
# The two GUIDs are the same thing spelled differently: Microsoft publishes
# well-known directory IDs for the consumer tenants, and a discovery URL built
# from one is functionally `consumers`. They pass the GUID shape check, so
# refusing only the names would leave the door open to whoever pasted a GUID.
_RESERVED_TENANT_IDS = frozenset(
    {
        "common",
        "organizations",
        "consumers",
        "9188040d-6c67-4c5b-b112-36a304b66dad",
        "f8cdef31-a31e-4b4a-93e4-5f571e91255a",
    }
)


def _is_directory_guid(value: str) -> bool:
    """``8-4-4-4-12`` hex — the Entra directory (tenant) ID form."""
    parts = value.split("-")
    if len(parts) != 5:
        return False
    return all(
        len(part) == width and all(c in "0123456789abcdefABCDEF" for c in part)
        for part, width in zip(parts, (8, 4, 4, 4, 12))
    )


def _is_verified_domain(value: str) -> bool:
    """A tenant's verified domain, e.g. ``example.onmicrosoft.com``.

    Hand-rolled label walk rather than a regex: the input is operator-supplied
    but the check must stay obviously linear-time (security playbook §5) and a
    nested-quantifier domain regex is the classic way to lose that.
    """
    if not 1 <= len(value) <= 253 or "." not in value:
        return False
    labels = value.split(".")
    for label in labels:
        if not 1 <= len(label) <= 63 or label[0] == "-" or label[-1] == "-":
            return False
        if not all((c.isascii() and c.isalnum()) or c == "-" for c in label):
            return False
    tld = labels[-1]
    return tld.isascii() and tld.isalpha()


def tenant_id_error(tenant_id: str) -> str | None:
    """Why ``tenant_id`` is not a single-tenant identifier, or ``None`` if it is.

    Returning the reason (not just a bool) is the point: the refusal has to
    reach the operator's boot log with the *why*, otherwise "Microsoft login
    stopped working" is indistinguishable from an unset env var.
    """
    value = (tenant_id or "").strip()
    if not value:
        return "MICROSOFT_TENANT_ID is not set."
    if value.lower() in _RESERVED_TENANT_IDS:
        return (
            f"MICROSOFT_TENANT_ID={value!r} names a Microsoft multi-tenant endpoint, not a tenant. "
            "This provider is single-tenant by design — the tenant is the boundary that decides who "
            f"may sign in and self-provision an account, and {value.lower()!r} removes it (any "
            "Microsoft account outside your organization could sign in, and with auth.allowed_domain "
            "unset would be provisioned). Set the tenant's directory ID (GUID) or one of its verified "
            "domains, e.g. example.onmicrosoft.com."
        )
    if _is_directory_guid(value) or _is_verified_domain(value):
        return None
    return (
        f"MICROSOFT_TENANT_ID={value!r} is neither a directory ID (GUID) nor a verified domain "
        "(e.g. example.onmicrosoft.com). Copy the Directory (tenant) ID from the Entra app "
        "registration's Overview page."
    )


def is_available() -> bool:
    return bool(
        MICROSOFT_CLIENT_ID
        and MICROSOFT_CLIENT_SECRET
        and MICROSOFT_TENANT_ID
        and tenant_id_error(MICROSOFT_TENANT_ID) is None
    )


def startup_warnings() -> list[str]:
    """Operator-facing boot messages, emitted from ``app.main``'s lifespan.

    Silence here means "configured and pinned"; an unconfigured instance says
    nothing at all (Microsoft login is opt-in).
    """
    if not (MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET and MICROSOFT_TENANT_ID):
        return []
    problem = tenant_id_error(MICROSOFT_TENANT_ID)
    if problem:
        return [f"Microsoft sign-in is DISABLED: {problem}"]
    if not get_allowed_domains():
        return [
            "Microsoft sign-in is enabled but auth.allowed_domain is unset. A single Entra tenant is "
            "NOT by itself an identity boundary: B2B guest accounts invited into the tenant sign in "
            "too, and their `email` claim carries their EXTERNAL address. Agnes matches (or creates) "
            "an account by that address alone — possibly one that already exists from Google or "
            "password sign-in. Pin auth.allowed_domain to the domains you own."
        ]
    return []


def _upn_is_usable_identity(upn: str) -> bool:
    """Whether a ``preferred_username`` may stand in for a missing email claim.

    Entra B2B guest UPNs look like ``user_othercorp.com#EXT#@tenant.onmicrosoft.com``
    — not a mailbox, not an address (``#`` is invalid in a local part), and
    provisioning an Agnes account keyed on it is meaningless. Everything else
    email-shaped is accepted: most work/school tenants use a mail-shaped UPN
    and many omit the ``email`` claim entirely, so dropping the fallback
    outright would lock those tenants out.

    NOT a guard against guests, despite what the shape of the string suggests:
    :func:`resolve_identity` only consults the UPN when the ``email`` claim is
    absent, and Entra emits ``email`` for guest accounts by default — so a B2B
    guest is resolved from that claim and never reaches this function.
    ``auth.allowed_domain`` is the only control on guests.
    """
    return bool(upn) and "@" in upn and "#" not in upn


def resolve_identity(user_info: dict) -> str:
    """The address this sign-in becomes, normalized, or ``""`` when there is none.

    ``email`` first (a real mailbox when present), ``preferred_username`` only
    as the narrowed fallback above. Lower-cased and stripped — normalization
    also happens in ``ensure_user`` so every provider agrees, but doing it here
    keeps the allowed_domain check below case-insensitive.
    """
    claim_email = str(user_info.get("email") or "").strip()
    if claim_email:
        return claim_email.lower()
    upn = str(user_info.get("preferred_username") or "").strip()
    return upn.lower() if _upn_is_usable_identity(upn) else ""


def _oauth_scope() -> str:
    """The scope requested at ``/auth/microsoft/login``.

    ``openid email profile`` unless Entra group sync is turned on
    (``auth.microsoft.group_sync_enabled`` / ``AGNES_MICROSOFT_GROUP_SYNC_ENABLED``),
    in which case the delegated Graph permission ``GroupMember.Read.All`` is
    added so the resulting access token can call ``GET /me/memberOf`` — see
    ``app.auth.microsoft_group_sync`` and ``docs/auth-microsoft-oauth.md``.

    Read once, at import time (this function runs inside ``_setup_oauth()``
    below, called once at module load) — same "restart to apply" contract as
    every other boot-time-consumed switch (``app.switches.Switch.effect``).
    An operator who flips the switch live gets the *sync* gate immediately
    (``microsoft_group_sync.apply_user_groups`` reads it live, per sign-in),
    but the wider consent scope only takes effect after a restart. Fails
    closed to the narrower scope on any config-read error — better a
    sign-in with no group sync than a login button that cannot render
    because ``instance.yaml`` failed to load this early in boot.
    """
    scope = "openid email profile"
    try:
        from app.auth.microsoft_group_sync import group_sync_enabled

        if group_sync_enabled():
            scope += " GroupMember.Read.All"
    except Exception:
        logger.warning("Microsoft OAuth scope: could not resolve group_sync_enabled at setup", exc_info=True)
    return scope


def _setup_oauth():
    if not is_available():
        problem = tenant_id_error(MICROSOFT_TENANT_ID) if MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET else None
        if problem and MICROSOFT_TENANT_ID:
            # Loud: an operator who set all three vars expects a login button.
            logger.error("Microsoft sign-in is DISABLED: %s", problem)
        return
    oauth.register(
        name="microsoft",
        client_id=MICROSOFT_CLIENT_ID,
        client_secret=MICROSOFT_CLIENT_SECRET,
        # quote() is belt-and-braces — tenant_id_error() already rejects
        # anything with a path separator — so the tenant can never escape its
        # segment of the discovery URL.
        server_metadata_url=(
            f"https://login.microsoftonline.com/{quote(MICROSOFT_TENANT_ID.strip(), safe='')}"
            "/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": _oauth_scope()},
    )


_setup_oauth()


@router.get("/login")
async def microsoft_login(request: Request):
    """Redirect to Microsoft Entra ID OAuth.

    Honors `?next=<path>` by stashing the sanitized value in the session so the
    callback can redirect there instead of the default /dashboard. The session
    is the right stash — OAuth flow is stateful and the `state` param is
    managed by Authlib.
    """
    if not is_available():
        return RedirectResponse(url="/login?error=microsoft_not_configured")
    next_path = safe_next_path(request.query_params.get("next"), default="")
    if next_path:
        request.session["login_next"] = next_path
    else:
        # Clear any stale value from an earlier aborted attempt.
        request.session.pop("login_next", None)
    redirect_uri = str(request.url_for("microsoft_callback"))
    return await oauth.microsoft.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def microsoft_callback(request: Request):
    """Handle Microsoft Entra ID OAuth callback."""
    if not is_available():
        return RedirectResponse(url="/login?error=microsoft_not_configured")

    try:
        token = await oauth.microsoft.authorize_access_token(request)
        user_info = token.get("userinfo", {})
        # `email` first; `preferred_username` (the UPN) only as a narrowed
        # fallback — Entra work/school accounts often omit `email`, but a B2B
        # guest UPN is not an identity. See resolve_identity().
        email = resolve_identity(user_info)
        name = user_info.get("name", "")

        if not email or "@" not in email:
            return RedirectResponse(url="/login?error=microsoft_no_email")

        # Domain check. This is the only thing standing between a tenant's B2B
        # guests (whose `email` claim is their EXTERNAL address) and an Agnes
        # account matched by that address — see the module docstring.
        allowed = get_allowed_domains()
        if allowed:
            domain = email.split("@")[-1].lower()
            if domain not in allowed:
                return RedirectResponse(url="/login?error=domain_not_allowed")

        # Find or create user. ``ensure_user`` grants Everyone membership at
        # creation time only (once) — we deliberately do NOT re-assert it on
        # every login, so an admin who later removes the membership stays
        # removed. This matches google.py / keboola.py, which rely solely on
        # that one-time grant.
        from app.auth.provisioning import UserDeactivatedError, ensure_user

        try:
            user = ensure_user(email, name, source="auth.microsoft:first-signin")
        except UserDeactivatedError:
            return RedirectResponse(url="/login?error=deactivated")

        # Sync Entra ID groups → user_group_members (source='microsoft_sync').
        # Config-gated, off by default — see app.auth.microsoft_group_sync
        # and docs/auth-microsoft-oauth.md. Fail-soft: apply_user_groups
        # never raises; on any failure (disabled, no Graph permission,
        # network outage) it returns soft_failed=True and login proceeds
        # with the previous membership snapshot untouched, exactly like the
        # Google callback above. Unlike google.py's callback, there is no
        # DuckDB-system-db-handle/conn dance here: apply_user_groups routes
        # every read/write through the src.repositories factory and never
        # touches ``conn`` (see its docstring) — opening a DuckDB handle just
        # to pass it in and immediately discard it would be pointless work on
        # every sign-in AND a new backend-split-guard violation
        # (tests/test_backend_split_guard.py) on a Postgres instance.
        from app.auth.microsoft_group_sync import apply_user_groups

        access_token = str(token.get("access_token") or "")
        sync_result = apply_user_groups(user["id"], email, access_token, None)

        # Login gate: mirrors the Google callback's not_in_allowed_group
        # gate — denied=True means the prefix filter is configured and
        # Graph returned a non-empty memberOf that contained zero groups
        # matching it. soft_failed (sync disabled, empty fetch, Graph
        # error) does NOT trigger this, so a transient Graph outage or
        # the feature being off can never lock a user out.
        if sync_result.denied:
            return RedirectResponse(url="/login?error=microsoft_not_in_allowed_group")

        # Issue JWT — identity-only, authorization derives from
        # user_group_members at request time (see app.auth.access).
        jwt_token = create_access_token(user["id"], user["email"])
        from app.auth.login_audit import audit_login_success

        audit_login_success(user["id"], provider="microsoft", request=request)

        # Redirect to the post-login target. Prefer the value stashed by
        # microsoft_login() — re-sanitize defensively in case of session
        # tampering. default=None → safe_next_path resolves to the
        # operator-configured home route (AGNES_HOME_ROUTE /
        # instance.home_route / /dashboard).
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
        # %r, not f-string: authlib raises OAuthError built verbatim from the
        # `error`/`error_description` QUERY PARAMS, and it does so BEFORE state
        # validation — so any unauthenticated caller can put arbitrary text
        # (CRLF included) on this line. repr() escapes the newlines; the slice
        # caps the flood.
        logger.error("Microsoft OAuth error: %r", str(e)[:500])
        return RedirectResponse(url="/login?error=microsoft_oauth_failed")
