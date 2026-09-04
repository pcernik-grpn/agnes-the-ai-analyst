"""JWT token creation and verification for API auth."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt


def _get_secret_key() -> str:
    """Resolve the JWT signing key. Fail-closed in production."""
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return os.environ.get("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")

    from app.auth.dependencies import is_local_dev_mode

    env_key = os.environ.get("JWT_SECRET_KEY")
    if not is_local_dev_mode():
        # Production: an explicit, strong key is mandatory. Never auto-generate
        # (a per-cold-start or shared signing key lets anyone forge admin tokens).
        if not env_key:
            raise RuntimeError(
                "JWT_SECRET_KEY is required in production — refusing to run with an "
                "auto-generated or shared signing key."
            )
        if len(env_key) < 32:
            raise RuntimeError(f"JWT_SECRET_KEY too short ({len(env_key)} chars); minimum 32 required.")
        return env_key

    # Local dev keeps the auto-generate-and-persist convenience.
    from app.secrets import get_jwt_secret

    key = get_jwt_secret()
    if len(key) < 32:
        import warnings as _warnings

        _warnings.warn(
            f"JWT_SECRET_KEY is {len(key)} chars — minimum 32 recommended",
            UserWarning,
            stacklevel=2,
        )
    return key


def validate_jwt_secret_or_raise() -> None:
    """Boot-time guard: force the fail-closed check before serving traffic."""
    _get_secret_key()


_SECRET_KEY_CACHE: Optional[str] = None

ALGORITHM = "HS256"

# Interactive web-login session lifetime. Single source of truth for both the
# JWT `exp` (default in create_access_token) and the `access_token` cookie
# max_age set by every login provider (google / email / password).
#
# A session JWT (typ="session") is trusted off signature + exp for most of its
# life, but not unconditionally: app.auth.pat_resolver.resolve_token_to_user
# also compares its `iat` against users.session_revoked_before (issue #1676),
# a per-user timestamp floor bumped by users_repo().revoke_sessions(...). That
# floor is bumped by three doors today — POST /auth/logout, a self-serve
# password change (app/auth/providers/password.py::password_change, which
# mints the caller a fresh cookie in the same response so the browser that
# just changed its own password is not the collateral damage), and a
# password reset (reset_confirm) — plus the admin-only POST /api/admin/
# users/{user_id}/revoke-sessions (app/api/admin_user_sessions.py), which
# ends a user's sessions without deactivating the account. So a 30-day
# session's *default* lifetime is still 30 days, but deactivating the
# account is no longer the only server-side kill switch — the column is
# PG-only (A3 ratchet): on a DuckDB-backed instance revoke_sessions() is a
# documented no-op and an old token keeps resolving for the rest of its exp.
SESSION_TOKEN_TTL_DAYS = 30
ACCESS_TOKEN_EXPIRE_HOURS = SESSION_TOKEN_TTL_DAYS * 24  # 720 h = 30 days (default JWT exp)
SESSION_COOKIE_MAX_AGE_SECONDS = SESSION_TOKEN_TTL_DAYS * 24 * 3600  # 2_592_000 s


def _get_cached_secret_key() -> str:
    """Return the JWT secret, caching after first call.

    The cache is reset when TESTING env var is set so that each test
    module picks up the correct JWT_SECRET_KEY from monkeypatch/env.
    """
    global _SECRET_KEY_CACHE
    # In test mode, always re-read from env to respect monkeypatch
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return os.environ.get("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    if _SECRET_KEY_CACHE is None:
        _SECRET_KEY_CACHE = _get_secret_key()
    return _SECRET_KEY_CACHE


def get_signing_secret() -> str:
    """Public accessor for the server's signing secret.

    Same key that signs session JWTs, exposed for OTHER short-lived signed
    payloads that aren't themselves JWTs (e.g. the outbound MCP OAuth
    connect flow's ``state`` param — ``app.auth.oauth_connect_state``) so
    every server-side signature shares one fail-closed key-resolution path
    instead of each caller re-implementing ``_get_secret_key``'s
    prod-vs-dev/env-var rules.
    """
    return _get_cached_secret_key()


def _refuse_if_service_account_session(user_id: str, typ: str) -> None:
    """Guard 1 (issue #1534): the SINGLE choke point every login provider
    shares to mint the credential a completed login hands back.

    Every provider (Google, Microsoft, password, email magic-link, Keboola,
    SSO, the legacy ``/auth/token`` endpoint, MCP-OAuth's authorization-code
    exchange and refresh) calls this function with ``typ="session"`` — none
    of them share any OTHER common helper (``_set_login_cookie`` lives only
    in ``app/auth/providers/password.py`` and covers just that provider's own
    four routes) — so refusing here once covers all of them with no
    provider-by-provider patch. A PAT minted *for* the service account
    (``typ="pat"``) always passes that ``typ`` explicitly and sails through
    unaffected.

    Fails OPEN on any lookup error (missing ``users`` table, no DB wired up
    at all) rather than raising — a large fraction of the test suite mints
    tokens for fabricated ids with no backing row and often no DB set up at
    all, and the actual defense against a service account signing in is that
    no login provider can ever resolve a real identity to its synthetic
    address in the first place. This check is defense-in-depth on top of
    that, and must never turn an infra hiccup into a broken login for
    everyone else.
    """
    if typ != "session":
        return
    try:
        from src.repositories import users_repo
        from src.service_accounts import ServiceAccountInteractiveLoginError, is_service_account

        user = users_repo().get_by_id(user_id)
    except Exception:
        return
    if is_service_account(user):
        raise ServiceAccountInteractiveLoginError(user_id)


def create_access_token(
    user_id: str,
    email: str,
    expires_delta: Optional[timedelta] = None,
    token_id: Optional[str] = None,
    typ: str = "session",
    omit_exp: bool = False,
    extra_claims: Optional[dict] = None,
) -> str:
    """Create a JWT. `typ` is "session" (interactive login) or "pat" (long-lived).

    If `omit_exp=True`, no `exp` claim is embedded. This is used by PATs with
    "no expiry" — the authoritative expiry check is the DB row in
    `personal_access_tokens.expires_at`, and a claim-less JWT avoids the
    misleading ~100y horizon that previously pretended to be "never".

    `extra_claims` merges arbitrary key/value pairs into the JWT payload
    after the reserved identity/metadata claims. Reserved keys (sub, email,
    typ, iat, jti, exp) are protected — they cannot be overridden by the
    caller.

    No ``role`` claim — authorization is derived from
    ``user_group_members`` at request time via ``app.auth.access.is_user_admin``.
    The JWT carries only identity (``sub``, ``email``) and token metadata.

    Raises ``src.service_accounts.ServiceAccountInteractiveLoginError`` when
    ``typ="session"`` (the default) and ``user_id`` names a ``kind='service'``
    row (issue #1534) — see :func:`_refuse_if_service_account_session`.
    """
    _refuse_if_service_account_session(user_id, typ)
    payload = {
        "sub": user_id,
        "email": email,
        "typ": typ,
        "iat": datetime.now(timezone.utc),
        "jti": token_id or uuid.uuid4().hex,
    }
    if not omit_exp:
        expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS))
        payload["exp"] = expire
    if extra_claims:
        _reserved = {"sub", "email", "typ", "iat", "jti", "exp"}
        for k, v in extra_claims.items():
            if k in _reserved:
                continue
            payload[k] = v
    return jwt.encode(payload, _get_cached_secret_key(), algorithm=ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """Verify and decode a JWT token. Returns payload dict or None."""
    try:
        payload = jwt.decode(token, _get_cached_secret_key(), algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
