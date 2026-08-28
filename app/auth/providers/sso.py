"""External identity login (``sso`` provider slot) — runtime-configured
Entra ID OIDC (design 2026-08-28).

One additional, customer-owned external identity login per instance,
configured by an admin at runtime (``/api/admin/sso/*`` — tenant ID, client
ID, Fernet-encrypted client secret, mandatory email-domain allowlist, button
label) and stored in the PG-only ``sso_config`` singleton. Unlike the env-var
``microsoft`` provider, everything here is read LIVE from the database, so
config changes take effect on the next request with no restart.

The slot name is deliberately generic (``sso``, not ``entra``): the protocol
lives in the config row's ``provider_type`` discriminator (``'entra_oidc'``
only today; SAML may widen it later without a redesign). Exactly one external
IdP per instance — the config table is a singleton by construction.

**Trust model** (the operator doc ``docs/auth-sso-entra.md`` repeats this):
the external tenant's admin can assert any email for the *permitted domains*
— on first-ever SSO login Agnes attaches by email, so the mandatory
``allowed_email_domains`` list is the boundary BEFORE an account links, and
the ``(tid, oid)`` subject binding protects it AFTER. The allowlist is
deliberately NOT inherited from instance ``auth.allowed_domain``.

Gating is deliberately INLINE, not the router-level ``require_provider``
dependency the other five providers use: **normal mode** enforces
``provider_allowed("sso") and is_available()`` and answers the same 404 a
disallowed provider answers today, while **test mode** (``?mode=test`` and
the callback leg carrying the session marker) requires an *admin session*
plus ``is_configured()`` and deliberately ignores the allowlist and the
``enabled`` flag — a router-level dependency would make the pre-enable admin
test unreachable exactly when it is needed.

This module deliberately does NOT duplicate the hardened pure helpers of the
``microsoft`` provider — ``tenant_id_error``, ``resolve_identity``,
``_is_directory_guid`` are imported from ``app.auth.providers.microsoft``.

Availability contract: :func:`is_available` (and :func:`is_configured`) must
NEVER let ``RequiresPostgresBackend`` escape — the provider-registry probes
treat a raising probe as "could not tell", which would suppress the registry
lockout rescue and spam warnings on every login render of a DuckDB-backed
instance. On DuckDB the answer is simply ``False``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth._common import safe_next_path
from app.auth.dependencies import get_optional_user
from app.auth.jwt import SESSION_COOKIE_MAX_AGE_SECONDS, create_access_token
from app.auth.providers.microsoft import _is_directory_guid, resolve_identity
from src.repositories import (
    RequiresPostgresBackend,
    audit_repo,
    sso_config_repo,
    user_external_identities_repo,
)

logger = logging.getLogger(__name__)

PROVIDER_TYPE = "entra_oidc"

# Deliberately no router-level require_provider("sso") dependency — see the
# module docstring (test mode must stay reachable pre-enable and outside an
# explicit allowlist).
router = APIRouter(prefix="/auth/sso", tags=["auth"])

oauth = OAuth()

_TEST_MARKER_SESSION_KEY = "sso_test"
_client_fingerprint: Optional[tuple] = None


# ---------------------------------------------------------------------------
# config layer
# ---------------------------------------------------------------------------


def _config_state() -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """``(config, decrypted_client_secret)`` — ``(None, None)`` when the
    app-state backend is DuckDB (PG-only repo) or nothing is configured."""
    try:
        repo = sso_config_repo()
        cfg = repo.get_config()
        if cfg is None:
            return None, None
        return cfg, repo.get_client_secret()
    except RequiresPostgresBackend:
        return None, None


def is_configured() -> bool:
    """Config-completeness: row present, non-empty domain allowlist, secret
    stored AND decryptable. Independent of the ``enabled`` flag — the admin
    test sign-in works pre-enable on exactly this predicate."""
    cfg, secret = _config_state()
    return bool(cfg and cfg["allowed_email_domains"] and secret)


def is_available() -> bool:
    """Postgres backend + configured + enabled — the login-page/registry probe."""
    cfg, secret = _config_state()
    return bool(cfg and cfg["enabled"] and cfg["allowed_email_domains"] and secret)


def login_offering() -> Optional[str]:
    """The login button label when the provider is available, else ``None``.

    One config read for the login page's block — availability and the
    ``display_name`` come from the same row.
    """
    cfg, secret = _config_state()
    if cfg and cfg["enabled"] and cfg["allowed_email_domains"] and secret:
        return str(cfg["display_name"])
    return None


def startup_warnings() -> list[str]:
    """Operator-facing boot messages, emitted from ``app.main``'s lifespan.

    An enabled external login is always announced — the instance trusts a
    third party's tenant to assert identities for the permitted domains, and
    the operator should see that in the boot log. An enabled row whose secret
    no longer decrypts (rotated/malformed vault key) is a loud error: the
    button silently disappeared from the login page.
    """
    try:
        repo = sso_config_repo()
        cfg = repo.get_config()
        if cfg is None or not cfg["enabled"]:
            return []
        if repo.get_client_secret() is None:
            return [
                (
                    "External SSO sign-in is enabled but its client secret is missing or no longer "
                    "decrypts (vault key rotated?). The login button is HIDDEN until the secret is "
                    "re-set on the SSO admin panel."
                )
            ]
        return [
            (
                f"External SSO sign-in (Entra ID) is ENABLED: tenant {cfg['tenant_id']!r} may assert "
                f"identities for: {', '.join(cfg['allowed_email_domains'])}. Keep that allowlist "
                "scoped to domains the external tenant is entitled to assert."
            )
        ]
    except RequiresPostgresBackend:
        return []


def _oauth_client():
    """Register (or re-register) the authlib client for the CURRENT config.

    Follows ``keboola.py::_oauth_client``: the config is a live DB row
    (secret rotation, tenant re-point via the admin panel), while authlib's
    registry caches the client object per name forever — so a fingerprint of
    the effective config is compared on every call and the cached client is
    evicted + re-registered on change. Safe to call repeatedly.
    """
    global _client_fingerprint
    cfg, secret = _config_state()
    if cfg is None or secret is None:
        return None
    fingerprint = (cfg["tenant_id"], cfg["client_id"], secret)
    client = oauth.create_client("sso")
    if client is not None and fingerprint == _client_fingerprint:
        return client
    oauth._clients.pop("sso", None)
    oauth.register(
        name="sso",
        client_id=fingerprint[1],
        client_secret=fingerprint[2],
        # quote() is belt-and-braces — the admin API validates the tenant via
        # tenant_id_error() before it is ever stored — so the tenant can
        # never escape its segment of the discovery URL.
        server_metadata_url=(
            f"https://login.microsoftonline.com/{quote(str(fingerprint[0]).strip(), safe='')}"
            "/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )
    _client_fingerprint = fingerprint
    return oauth.create_client("sso")


# ---------------------------------------------------------------------------
# claim evaluation + binding (the callback's testable pieces)
# ---------------------------------------------------------------------------


def evaluate_claims(user_info: dict, cfg: dict) -> tuple[Optional[str], str, str, str]:
    """``(error_code, email, oid, tid)`` for one validated token's claims.

    Pure function (no DB): identity resolution (imported ``microsoft`` rules
    — ``email`` claim first, non-``#EXT#`` UPN fallback), the ``oid``/``tid``
    extraction (never ``sub``), the defensive tenant pin (belt-and-braces —
    when the CONFIGURED tenant is a GUID; for a verified-domain config the
    tenant-pinned issuer validation is the authority), and the fail-closed
    domain allowlist. GUIDs are lowercased so binding keys are stable.
    """
    email = resolve_identity(user_info)
    if not email or "@" not in email:
        return "sso_no_email", "", "", ""
    oid = str(user_info.get("oid") or "").strip().lower()
    if not oid:
        return "sso_no_subject", email, "", ""
    tid = str(user_info.get("tid") or "").strip().lower()
    if not tid:
        return "sso_wrong_tenant", email, oid, ""
    configured = str(cfg.get("tenant_id") or "").strip()
    if _is_directory_guid(configured) and tid != configured.lower():
        return "sso_wrong_tenant", email, oid, tid
    domain = email.split("@")[-1]
    if domain not in (cfg.get("allowed_email_domains") or []):
        return "domain_not_allowed", email, oid, tid
    return None, email, oid, tid


def _audit_linked(user_id: str, subject: str, replaced_subject: Optional[str] = None) -> None:
    """Best-effort ``sso.identity.linked`` audit row. Subjects are directory
    object IDs (not secrets — the admin identities API lists them); no email
    addresses, no token material."""
    params: dict[str, Any] = {"subject": subject}
    if replaced_subject:
        params["replaced_subject"] = replaced_subject
    try:
        audit_repo().log(user_id=user_id, action="sso.identity.linked", resource=f"user:{user_id}", params=params)
    except Exception:
        logger.warning("audit log failed for sso.identity.linked")


def bind_external_identity(
    *, subject: str, tenant_id: str, email: str, name: str
) -> tuple[Optional[dict], Optional[str]]:
    """The binding algorithm (design, Scope piece 3): ``(user, error_code)``.

    "This login's identity" is ``(entra_oidc, <token tid>, <token oid>)`` —
    the VALIDATED token's claims, never the configured tenant string.

    1. Subject hit -> that user, after an explicit ``active`` check (the
       subject-hit path bypasses ``ensure_user``, so without the check here a
       deactivated user would get a fresh cookie and then 401 on every
       request). Subject binding beats email; email drift is logged at INFO,
       never rewritten on either side.
    2. Miss -> email attach via the shared ``ensure_user`` provisioning path
       (JIT create, Everyone membership once, deactivated rejection), then
       INSERT — replacing a stale row from a no-longer-configured tenant or
       protocol, refusing a same-tenant conflicting subject (email recycling
       at the customer: the successor must not inherit the predecessor's
       account), and resolving a concurrent-first-login unique violation by
       re-reading the winner.
    """
    from src.repositories import users_repo
    from src.repositories.user_external_identities_pg import IdentityLinkConflictError

    ids_repo = user_external_identities_repo()

    hit = ids_repo.get_by_subject(PROVIDER_TYPE, tenant_id, subject)
    if hit:
        user = users_repo().get_by_id(hit["user_id"])
        if user is None:
            # FK ON DELETE CASCADE makes this unreachable; refuse rather than
            # silently fall through to the email-attach path.
            return None, "sso_identity_conflict"
        if not bool(user.get("active", True)):
            return None, "deactivated"
        if str(user.get("email") or "").lower() != email:
            logger.info(
                "sso login email drift for user %s: the token asserts a different email than "
                "the account holds (subject binding wins; nothing is rewritten)",
                user["id"],
            )
        ids_repo.touch_last_login(user["id"])
        return user, None

    from app.auth.provisioning import UserDeactivatedError, ensure_user

    try:
        user = ensure_user(email, name, source="auth.sso:first-signin")
    except UserDeactivatedError:
        return None, "deactivated"

    existing = ids_repo.get_by_user_id(user["id"])
    if existing:
        if (existing["provider_type"], existing["tenant_id"]) != (PROVIDER_TYPE, tenant_id):
            # Stale binding from a config that no longer exists (tenant
            # re-point, future protocol cutover): the active config's
            # assertions are the current trust anchor — replace, loudly.
            logger.warning(
                "sso identity for user %s replaced: stale binding (%s, tenant %s, subject %s) "
                "superseded by the active config's login (tenant %s, subject %s)",
                user["id"],
                existing["provider_type"],
                existing["tenant_id"],
                existing["subject"],
                tenant_id,
                subject,
            )
            try:
                ids_repo.link(
                    user_id=user["id"],
                    provider_type=PROVIDER_TYPE,
                    tenant_id=tenant_id,
                    subject=subject,
                    email_at_link=email,
                    replace_existing=True,
                )
            except IdentityLinkConflictError:
                return _resolve_link_race(ids_repo, user, tenant_id, subject)
            _audit_linked(user["id"], subject, replaced_subject=existing["subject"])
            ids_repo.touch_last_login(user["id"])
            return user, None
        if existing["subject"] != subject:
            # Same tenant, different principal resolving to the same mailbox
            # — canonically email recycling at the customer. Refuse; recovery
            # is an explicit admin decision (unlink or deactivate). WARNING
            # carries the user id and both subject GUIDs, no raw emails.
            logger.warning(
                "sso identity conflict for user %s: existing subject %s, asserted subject %s — "
                "refusing sign-in; an admin must unlink the stale identity or deactivate the account",
                user["id"],
                existing["subject"],
                subject,
            )
            return None, "sso_identity_conflict"
        # Same identity already linked (concurrent login of the same person).
        ids_repo.touch_last_login(user["id"])
        return user, None

    try:
        ids_repo.link(
            user_id=user["id"],
            provider_type=PROVIDER_TYPE,
            tenant_id=tenant_id,
            subject=subject,
            email_at_link=email,
        )
    except IdentityLinkConflictError:
        return _resolve_link_race(ids_repo, user, tenant_id, subject)
    _audit_linked(user["id"], subject)
    ids_repo.touch_last_login(user["id"])
    return user, None


def _resolve_link_race(ids_repo, user: dict, tenant_id: str, subject: str) -> tuple[Optional[dict], Optional[str]]:
    """Race rule: on a unique violation from a concurrent first login,
    re-read by this login's identity and proceed when the winner matches;
    otherwise refuse with the same conflict error."""
    winner = ids_repo.get_by_subject(PROVIDER_TYPE, tenant_id, subject)
    if winner and winner["user_id"] == user["id"]:
        ids_repo.touch_last_login(user["id"])
        return user, None
    logger.warning(
        "sso identity link race for user %s lost to a conflicting binding (subject %s) — refusing sign-in",
        user["id"],
        subject,
    )
    return None, "sso_identity_conflict"


# ---------------------------------------------------------------------------
# routes (inline gating — see module docstring)
# ---------------------------------------------------------------------------


def _is_admin_session(user: Optional[Any]) -> bool:
    """Whether this request carries a real admin's interactive session.

    Delegates to the canonical :func:`app.auth.access.is_admin_session` —
    the boolean form of ``require_admin``'s checks — so this route (which
    resolves the user optionally, serving anonymous normal-mode traffic on
    the same path) can never drift from the admin gate.
    """
    from app.auth.access import is_admin_session

    return is_admin_session(user)


def _normal_mode_gate() -> None:
    """The inline equivalent of ``require_provider("sso")`` + availability:
    excluded or unavailable answers 404, never advertising the route."""
    from app.auth.provider_registry import provider_allowed

    if not (provider_allowed("sso") and is_available()):
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("/login")
async def sso_login(request: Request, user: Optional[dict] = Depends(get_optional_user)):
    """Redirect to the configured tenant's Entra authorize endpoint.

    ``?mode=test`` (admin-only, works pre-enable and outside the allowlist)
    stashes a session marker so the callback renders the side-effect-free
    result page instead of signing anyone in.
    """
    if request.query_params.get("mode") == "test":
        if not _is_admin_session(user):
            raise HTTPException(status_code=403, detail="Admin access required")
        if not is_configured():
            return RedirectResponse(url="/login?error=sso_not_configured")
        request.session[_TEST_MARKER_SESSION_KEY] = True
        request.session.pop("login_next", None)
    else:
        _normal_mode_gate()
        # A stale test marker from an abandoned admin test must never leak
        # into a normal sign-in.
        request.session.pop(_TEST_MARKER_SESSION_KEY, None)
        next_path = safe_next_path(request.query_params.get("next"), default="")
        if next_path:
            request.session["login_next"] = next_path
        else:
            request.session.pop("login_next", None)

    client = _oauth_client()
    if client is None:
        return RedirectResponse(url="/login?error=sso_not_configured")
    redirect_uri = str(request.url_for("sso_callback"))
    # prompt=select_account (deliberate difference from the `microsoft`
    # provider): customer users commonly hold several active Entra sessions,
    # and without the picker Entra may silently SSO an already-signed-in
    # identity — e.g. a B2B guest session — which then dies on the domain
    # allowlist with no chance to pick the right account.
    return await client.authorize_redirect(request, redirect_uri, prompt="select_account")


@router.get("/callback")
async def sso_callback(request: Request, user: Optional[dict] = Depends(get_optional_user)):
    """Code exchange -> claims -> identity -> bind -> JWT cookie.

    The test-mode leg (session marker) re-verifies the CURRENT session user
    is an admin, completes the exchange, and renders the result page —
    without ``ensure_user``, without an identity row, without a cookie.
    """
    # Pop unconditionally: the marker is single-use whatever happens next.
    test_mode = bool(request.session.pop(_TEST_MARKER_SESSION_KEY, None))
    if test_mode:
        if not _is_admin_session(user):
            raise HTTPException(status_code=403, detail="Admin access required")
        if not is_configured():
            return RedirectResponse(url="/login?error=sso_not_configured")
    else:
        _normal_mode_gate()

    cfg, _secret = _config_state()
    if cfg is None:
        return RedirectResponse(url="/login?error=sso_not_configured")

    try:
        client = _oauth_client()
        if client is None:
            return RedirectResponse(url="/login?error=sso_not_configured")
        token = await client.authorize_access_token(request)
        user_info = token.get("userinfo", {}) or {}

        error, email, oid, tid = evaluate_claims(user_info, cfg)

        if test_mode:
            return _render_test_result(request, cfg, user_info, error, email, oid, tid)

        if error:
            return RedirectResponse(url=f"/login?error={error}")

        bound_user, bind_error = bind_external_identity(
            subject=oid,
            tenant_id=tid,
            email=email,
            name=str(user_info.get("name") or ""),
        )
        if bind_error:
            return RedirectResponse(url=f"/login?error={bind_error}")

        jwt_token = create_access_token(bound_user["id"], bound_user["email"])
        target = safe_next_path(request.session.pop("login_next", None))

        from app.auth.public_url import cookie_secure
        from app.instance_config import session_cookie_domain

        response = RedirectResponse(url=target, status_code=302)
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

    except HTTPException:
        raise
    except RequiresPostgresBackend:
        # Unreachable behind the gates above (both swallow it to a 404 /
        # redirect first), but if it ever fires it is a backend-routing
        # problem, not an OAuth failure — let the app-wide typed-501 handler
        # answer instead of mislabeling it below.
        raise
    except Exception as e:
        # %r, not f-string: authlib raises OAuthError built verbatim from the
        # `error`/`error_description` QUERY PARAMS, and it does so BEFORE
        # state validation — so any unauthenticated caller can put arbitrary
        # text (CRLF included) on this line. repr() escapes the newlines; the
        # slice caps the flood.
        logger.error("SSO OAuth error: %r", str(e)[:500])
        return RedirectResponse(url="/login?error=sso_oauth_failed")


def _render_test_result(
    request: Request, cfg: dict, user_info: dict, error: Optional[str], email: str, oid: str, tid: str
):
    """The admin test sign-in's result page: resolved claims, the
    domain-allowlist verdict, and which account the login WOULD attach to —
    computed read-only (no ``ensure_user``, no identity write, no cookie)."""
    from app.web.router import _build_context, templates
    from src.repositories import users_repo

    would_attach = None
    identity_bound_to = None
    if email:
        try:
            existing = users_repo().get_by_email_ci(email)
            would_attach = existing["email"] if existing else None
        except Exception:
            would_attach = None
    if oid and tid:
        try:
            row = user_external_identities_repo().get_by_subject(PROVIDER_TYPE, tid, oid)
            if row:
                bound = users_repo().get_by_id(row["user_id"])
                identity_bound_to = bound["email"] if bound else None
        except Exception:
            identity_bound_to = None

    domain = email.split("@")[-1] if email and "@" in email else ""
    ctx = _build_context(
        request,
        result_error=error,
        resolved_email=email,
        resolved_oid=oid,
        resolved_tid=tid,
        domain=domain,
        domain_allowed=bool(domain and domain in cfg.get("allowed_email_domains", [])),
        allowed_domains=cfg.get("allowed_email_domains", []),
        display_name=cfg.get("display_name", ""),
        would_attach=would_attach,
        identity_bound_to=identity_bound_to,
    )
    return templates.TemplateResponse(request, "sso_test_result.html", ctx)
