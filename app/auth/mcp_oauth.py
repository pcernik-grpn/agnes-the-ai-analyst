"""OAuth 2.1 Authorization Server provider for the Agnes MCP connector.

Implements ``OAuthAuthorizationServerProvider`` from the MCP SDK so any
MCP-compatible AI agent (Claude Desktop, Claude.ai, Cursor, Cline, …) can
connect to Agnes using the standard browser-based OAuth 2.1 + PKCE flow
without needing a manually-issued PAT.

The SDK serves the standard OAuth endpoints (``/register``, ``/authorize``,
``/token``) relative to the streamable sub-app mounted at ``/api/mcp/http``
(see ``app/main.py``); only the login/consent bridge below lives under
``/api/mcp/oauth/*``.

Flow summary
------------
1. MCP client POSTs to the SDK's ``register`` endpoint (under
   ``/api/mcp/http``, RFC 7591 dynamic client registration) — client
   metadata is persisted in ``oauth_clients`` via the factory repo.
2. MCP client redirects user browser to the SDK's ``authorize`` endpoint.
   The SDK's ``AuthorizationHandler`` calls ``provider.authorize()``,
   which:
   a. Checks for an active INTERACTIVE Agnes session (``Authorization``
      header or the ``access_token`` cookie set by the browser login
      flow). Non-interactive credentials — a PAT, an agent PAT, the
      scheduler secret, an ``X-StorageApi-Token``, or any agent-surface
      session JWT — are refused: the consent POST mints a 30-day refresh
      token, so accepting one would launder it into a durable successor
      credential that survives revoking it (see ``_get_session_user``).
   b. If no session: redirects to ``/auth/google/login?next=…`` (or
      the email-magic-link login page) so the user authenticates with
      the identity provider they already use for Agnes.
   c. After login the user is sent back to ``/api/mcp/oauth/consent``
      with the pending authorization parameters stashed in the session.
   d. On the consent page the user clicks "Allow" which POSTs back and
      triggers ``_complete_authorize``:  a short-lived authorization
      code is minted and stored, then the browser is redirected to the
      MCP client's ``redirect_uri?code=…&state=…``.
3. MCP client POSTs to the SDK's ``token`` endpoint with the authorization
   code + PKCE verifier.  ``exchange_authorization_code`` validates,
   deletes the code, mints a JWT token via ``create_access_token``, and
   persists it in ``oauth_access_tokens``.  The JWT is a standard Agnes
   session JWT, so ``resolve_token_to_user`` accepts it unchanged and
   ALL existing RBAC applies automatically via the self-call pattern in
   ``mcp_http.py``.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from urllib.parse import urlparse

from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


# ---------------------------------------------------------------------------
# RFC 8252 §7.3 — loopback redirect URI helpers
# ---------------------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    """Return True for the three canonical loopback hostnames."""
    return host in ("127.0.0.1", "::1", "localhost")


def _loopback_uri_match(requested: str, registered: str) -> bool:
    """RFC 8252 §7.3: for loopback URIs the port MUST be ignored.

    Two loopback URIs match when scheme, host, and path are equal,
    regardless of the port number.  This is the correct behaviour for
    native-app OAuth clients (VS Code, etc.) that bind a random ephemeral
    port for the redirect listener.
    """
    r = urlparse(str(requested))
    reg = urlparse(str(registered))
    return (
        r.scheme == reg.scheme
        and _is_loopback_host(r.hostname or "")
        and _is_loopback_host(reg.hostname or "")
        and r.path == reg.path
    )


class _LoopbackAwareClient(OAuthClientInformationFull):
    """Thin wrapper that applies RFC 8252 §7.3 loopback redirect URI matching.

    The MCP SDK's OAuthClientInformationFull.validate_redirect_uri() does an
    exact membership check (redirect_uri not in self.redirect_uris).  VS Code
    native MCP binds a random ephemeral port for its loopback redirect listener
    — the registered URI port and the runtime URI port therefore differ.  Per
    RFC 8252 §7.3 the port MUST be ignored for loopback URIs; this wrapper
    implements that rule by catching the SDK's InvalidRedirectUriError and
    re-checking with port-agnostic comparison.
    """

    def validate_redirect_uri(self, redirect_uri):  # type: ignore[override]
        from mcp.shared.auth import InvalidRedirectUriError

        try:
            return super().validate_redirect_uri(redirect_uri)
        except InvalidRedirectUriError:
            if redirect_uri and _is_loopback_host(urlparse(str(redirect_uri)).hostname or ""):
                for registered in self.redirect_uris or []:
                    if _loopback_uri_match(str(redirect_uri), str(registered)):
                        return redirect_uri
            raise


logger = logging.getLogger(__name__)

# How long (seconds) an authorization code is valid before it expires.
_AUTH_CODE_TTL = 300  # 5 minutes
# Access-token lifetime in seconds (matches Agnes PAT "no expiry" pattern;
# keep short so revocation takes effect quickly).
_ACCESS_TOKEN_TTL = 3600 * 8  # 8 hours
# Refresh-token lifetime (optional rotation).
_REFRESH_TOKEN_TTL = 3600 * 24 * 30  # 30 days

# Session key used to stash pending authorization state during the
# login-redirect round-trip.
_SESSION_PENDING_AUTH_KEY = "mcp_oauth_pending"

# A finished consent leaves a short-lived outcome marker behind under this
# prefix so a REPLAY of the consent page — a double-clicked Allow, or a reload
# of the tab the client leaves behind after taking the redirect — can say what
# actually happened, instead of reading as "Authorization request expired" on a
# connection that in fact succeeded. The marker carries no subject, so it can
# never be exchanged for a token.
_CONSENT_OUTCOME_PREFIX = "consent_outcome_"
_CONSENT_OUTCOME_TTL = 600  # 10 minutes

# What the connection can actually do, in the user's language. The only OAuth
# scope Agnes issues is the coarse "read" (see
# _oauth_client_registration_options in app/api/mcp_streamable.py), but the MCP
# surface behind it is the caller's whole authority — write and delete tools
# included — narrowed only by their own RBAC. Listing the raw scope token was
# therefore misleading: the consent screen said "read" and the client then
# offered dozens of write/delete tools.
_SCOPE_DESCRIPTIONS = {
    "read": "Read what you can already see in Agnes — catalog, tables, query results, documents, memory",
}
_WRITE_CAPABILITY = (
    "Act on your behalf through Agnes tools that create, update and delete — this connection is not read-only"
)
_RBAC_CAPABILITY = "Never do more than your own Agnes permissions allow"


class AgnesMCPOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """Agnes implementation of the MCP SDK OAuth provider protocol.

    All token persistence goes through the ``oauth_clients_repo()``
    factory so the correct backend (DuckDB or Postgres) is selected at
    runtime — never instantiated directly here.
    """

    # ------------------------------------------------------------------
    # RFC 7591 dynamic client registration
    # ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        from src.repositories import oauth_clients_repo

        row = oauth_clients_repo().get_client(client_id)
        if row is None:
            return None
        base = _row_to_client_info(row)
        # Wrap in _LoopbackAwareClient so RFC 8252 §7.3 port-ignoring applies
        # for VS Code native MCP (random ephemeral loopback port).
        return _LoopbackAwareClient(**base.model_dump())

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        from src.repositories import oauth_clients_repo
        from app.secrets import encrypt_client_secret

        meta = client_info.model_dump(exclude={"client_id", "client_secret"})
        # redirect_uris is stored separately for fast lookup; remove from meta
        meta.pop("redirect_uris", None)
        meta.pop("client_id_issued_at", None)
        meta.pop("client_secret_expires_at", None)

        oauth_clients_repo().upsert_client(
            client_id=client_info.client_id,
            # #869: encrypt the client_secret at rest so a DB/backup leak can't
            # read usable secrets; get_client decrypts it back for SDK auth.
            client_secret=encrypt_client_secret(client_info.client_secret),
            redirect_uris=[str(u) for u in (client_info.redirect_uris or [])],
            client_name=getattr(client_info, "client_name", None),
            client_metadata=meta,
        )

    # ------------------------------------------------------------------
    # Authorization (step 2 — redirect to login/consent)
    # ------------------------------------------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Return the URL the SDK should redirect the browser to.

        This is called by the SDK's AuthorizationHandler *before* we have
        access to the live Starlette ``Request`` object (the handler only
        gives us the parsed params).  We therefore encode the pending
        authorization state into a short-lived token stored in the
        ``oauth_auth_codes`` table under a ``_pending_`` prefix, and
        send the browser to our consent bridge at
        ``/api/mcp/oauth/consent?pending=<token>``.

        The consent bridge reads the pending state, checks / establishes
        the Agnes session, shows the consent page, and on confirmation
        calls ``_complete_authorize`` to write the real authorization code.
        """
        # Stash pending auth state as a temp record so we can retrieve it
        # after the login round-trip without relying on a cookie (some
        # clients open the authorize URL in a system browser with no shared
        # cookie jar).
        pending_token = "pending_" + secrets.token_urlsafe(32)
        from src.repositories import oauth_clients_repo

        oauth_clients_repo().save_auth_code(
            code=pending_token,
            client_id=client.client_id,
            scopes=list(params.scopes or []),
            code_challenge=params.code_challenge,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            expires_at=time.time() + _AUTH_CODE_TTL,
            subject=None,  # not yet resolved — filled in at consent
            resource=params.resource,
            # Persist the client's CSRF state server-side. It is read back from
            # this row when minting the code — NEVER from the (forgeable) consent
            # form body — so a tampered form can't swap the state the client uses
            # to validate the authorization response.
            state=params.state,
        )

        # Relative redirect keeps the browser on the host the client used to
        # reach /authorize (important when AGNES_BASE_URL is unset behind a
        # TLS proxy).
        return f"/api/mcp/oauth/consent?pending={pending_token}"

    # ------------------------------------------------------------------
    # Authorization code exchange (step 3)
    # ------------------------------------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        from src.repositories import oauth_clients_repo

        row = oauth_clients_repo().get_auth_code(authorization_code)
        if row is None:
            return None
        if row["client_id"] != client.client_id:
            return None
        if row["expires_at"] < time.time():
            return None
        return AuthorizationCode(
            # Carry the RAW code the caller passed, NOT row["code"] (the stored
            # SHA-256 digest, audit M4): the SDK passes this value straight back
            # into delete_auth_code(), which hashes again — using the digest
            # here would double-hash (sha256(sha256(raw))) and the delete would
            # silently no-op, breaking one-time-use enforcement (Devin #863).
            code=authorization_code,
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row.get("resource"),
            subject=row.get("subject"),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        from datetime import timedelta

        from app.auth.jwt import create_access_token
        from src.repositories import oauth_clients_repo

        # Delete code immediately (one-time use).
        oauth_clients_repo().delete_auth_code(authorization_code.code)

        subject = authorization_code.subject
        if not subject:
            raise TokenError(error="invalid_grant", error_description="No subject on code")

        # Resolve user to get email for the JWT.
        from src.repositories import users_repo

        user = users_repo().get_by_id(subject)
        if not user:
            raise TokenError(error="invalid_grant", error_description="User not found")

        jti = uuid.uuid4().hex
        access_jwt = create_access_token(
            user_id=subject,
            email=user["email"],
            expires_delta=timedelta(seconds=_ACCESS_TOKEN_TTL),
            token_id=jti,
            typ="session",
            # v106 follow-up: mark MCP-OAuth access tokens so
            # resolve_token_to_user can stamp the stack data-read surface —
            # a remote MCP connector (Claude Desktop / claude.ai) is an
            # AGENT surface, so an admin's connector follows their stack
            # like the CLI workspace, instead of inheriting catalog
            # god-mode. Browser session JWTs carry no scope claim and are
            # unaffected.
            extra_claims={"scope": "mcp-oauth"},
        )

        oauth_clients_repo().save_access_token(
            token=access_jwt,
            client_id=client.client_id,
            scopes=list(authorization_code.scopes),
            expires_at=int(time.time()) + _ACCESS_TOKEN_TTL,
            subject=subject,
            resource=authorization_code.resource,
        )

        # Mint a refresh token so clients can renew without re-authorizing.
        refresh_token_str = secrets.token_urlsafe(48)
        oauth_clients_repo().save_refresh_token(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=list(authorization_code.scopes),
            subject=subject,
            expires_at=int(time.time()) + _REFRESH_TOKEN_TTL,
            resource=authorization_code.resource,
        )

        return OAuthToken(
            access_token=access_jwt,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL,
            refresh_token=refresh_token_str,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    # ------------------------------------------------------------------
    # Refresh token exchange
    # ------------------------------------------------------------------

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        from src.repositories import oauth_clients_repo

        row = oauth_clients_repo().get_refresh_token(refresh_token)
        if row is None or row.get("revoked_at") is not None:
            return None
        if row["client_id"] != client.client_id:
            return None
        exp = row.get("expires_at")
        if exp is not None and exp < int(time.time()):
            return None
        return RefreshToken(
            # RAW token, not row["token"] (the stored digest): the SDK passes it
            # back into get_refresh_token()/revoke_refresh_token() which hash
            # again — the digest would double-hash and rotation/revoke would
            # silently no-op (audit M4 / Devin #863).
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=row.get("expires_at"),
            subject=row.get("subject"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        from datetime import timedelta

        from app.auth.jwt import create_access_token
        from src.repositories import oauth_clients_repo, users_repo

        subject = refresh_token.subject
        if not subject:
            raise TokenError(error="invalid_grant", error_description="No subject")
        user = users_repo().get_by_id(subject)
        if not user:
            raise TokenError(error="invalid_grant", error_description="User not found")

        # Preserve the resource binding (RFC 8707) across rotation: read it
        # off the stored refresh-token row before revoking. The SDK's
        # RefreshToken type doesn't carry `resource`, so go back to the repo.
        old_row = oauth_clients_repo().get_refresh_token(refresh_token.token)
        resource = old_row.get("resource") if old_row else None

        # Revoke old refresh token (rotation).
        oauth_clients_repo().revoke_refresh_token(refresh_token.token)

        effective_scopes = scopes or list(refresh_token.scopes)
        jti = uuid.uuid4().hex
        access_jwt = create_access_token(
            user_id=subject,
            email=user["email"],
            expires_delta=timedelta(seconds=_ACCESS_TOKEN_TTL),
            token_id=jti,
            typ="session",
            # v106 follow-up: mark MCP-OAuth access tokens so
            # resolve_token_to_user can stamp the stack data-read surface —
            # a remote MCP connector (Claude Desktop / claude.ai) is an
            # AGENT surface, so an admin's connector follows their stack
            # like the CLI workspace, instead of inheriting catalog
            # god-mode. Browser session JWTs carry no scope claim and are
            # unaffected.
            extra_claims={"scope": "mcp-oauth"},
        )
        oauth_clients_repo().save_access_token(
            token=access_jwt,
            client_id=client.client_id,
            scopes=effective_scopes,
            expires_at=int(time.time()) + _ACCESS_TOKEN_TTL,
            subject=subject,
            resource=resource,
        )

        new_refresh = secrets.token_urlsafe(48)
        oauth_clients_repo().save_refresh_token(
            token=new_refresh,
            client_id=client.client_id,
            scopes=effective_scopes,
            subject=subject,
            expires_at=int(time.time()) + _REFRESH_TOKEN_TTL,
            resource=resource,
        )

        return OAuthToken(
            access_token=access_jwt,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL,
            refresh_token=new_refresh,
            scope=" ".join(effective_scopes) if effective_scopes else None,
        )

    # ------------------------------------------------------------------
    # Token verification (used by SDK's ProviderTokenVerifier)
    # ------------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        from src.repositories import oauth_clients_repo

        row = oauth_clients_repo().get_access_token(token)
        if row is None or row.get("revoked_at") is not None:
            return None
        exp = row.get("expires_at")
        if exp is not None and exp < int(time.time()):
            return None
        return AccessToken(
            # RAW token, not row["token"] (the stored digest): the SDK passes it
            # back into revoke_access_token() which hashes again (audit M4 /
            # Devin #863).
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=row.get("expires_at"),
            subject=row.get("subject"),
            resource=row.get("resource"),
        )

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    async def revoke_token(
        self,
        token: AccessToken | RefreshToken,
    ) -> None:
        from src.repositories import oauth_clients_repo

        repo = oauth_clients_repo()
        if isinstance(token, AccessToken):
            repo.revoke_access_token(token.token)
        else:
            repo.revoke_refresh_token(token.token)


# ---------------------------------------------------------------------------
# Consent / login bridge — FastAPI router mounted by main.py
# ---------------------------------------------------------------------------


async def _consent_page(request: Request) -> Response:
    """GET handler: show the consent screen, or redirect to login first."""
    from src.repositories import oauth_clients_repo

    pending = request.query_params.get("pending", "")

    # Validate that the pending code exists and hasn't expired.
    pending_row = oauth_clients_repo().get_auth_code(pending)
    if pending_row is None or pending_row["expires_at"] < time.time():
        return _finished_or_expired_response(pending)

    # Check if the user is logged in (Agnes session cookie / header).
    user = _get_session_user(request)
    if user is None:
        # Not logged in — redirect to Google/email login then come back.
        login_url = _login_url(request, pending)
        return RedirectResponse(url=login_url, status_code=302)

    client_row = oauth_clients_repo().get_client(pending_row["client_id"])
    client_name = (client_row or {}).get("client_name") or pending_row["client_id"]
    scopes = pending_row["scopes"]
    html = _render_consent_page(
        user_email=user.get("email", ""),
        client_name=client_name,
        scopes=scopes,
        pending=pending,
    )
    return HTMLResponse(html)


async def _consent_submit(request: Request) -> Response:
    """POST handler: user clicked Allow/Deny — mint the code and redirect."""
    from src.repositories import oauth_clients_repo

    # CSRF defense: this POST mints an OAuth authorization code off the user's
    # active session, so reject cross-origin submits. The consent form is always
    # served from this same origin, so a missing/foreign Origin (or Referer, for
    # clients that omit Origin) is not a legitimate request.
    if not _same_origin(request):
        return HTMLResponse("<h2>Cross-origin request rejected.</h2>", status_code=403)

    form = await request.form()
    pending = str(form.get("pending", ""))
    action = str(form.get("action", "allow"))

    pending_row = oauth_clients_repo().get_auth_code(pending)
    if pending_row is None or pending_row["expires_at"] < time.time():
        return _finished_or_expired_response(pending)

    redirect_uri = pending_row["redirect_uri"]
    # Authoritative state is the value persisted at authorize() time, never the
    # form body — so a forged/tampered form cannot swap the client's CSRF state.
    state = pending_row.get("state") or ""

    client_name = _client_display_name(pending_row["client_id"])

    if action != "allow":
        # User denied — redirect with error.
        _record_consent_outcome(pending, action="deny", client_name=client_name)
        oauth_clients_repo().delete_auth_code(pending)
        sep = "&" if "?" in redirect_uri else "?"
        deny_url = f"{redirect_uri}{sep}error=access_denied"
        if state:
            deny_url += f"&state={state}"
        return RedirectResponse(url=deny_url, status_code=302)

    user = _get_session_user(request)
    if user is None:
        return HTMLResponse("<h2>Not authenticated.</h2>", status_code=401)

    # Replace pending code with the real authorization code.
    real_code = secrets.token_urlsafe(32)
    oauth_clients_repo().save_auth_code(
        code=real_code,
        client_id=pending_row["client_id"],
        scopes=pending_row["scopes"],
        code_challenge=pending_row["code_challenge"],
        redirect_uri=redirect_uri,
        redirect_uri_provided_explicitly=pending_row["redirect_uri_provided_explicitly"],
        expires_at=time.time() + _AUTH_CODE_TTL,
        subject=user["id"],
        resource=pending_row.get("resource"),
    )
    _record_consent_outcome(pending, action="allow", client_name=client_name)
    oauth_clients_repo().delete_auth_code(pending)

    sep = "&" if "?" in redirect_uri else "?"
    final_url = f"{redirect_uri}{sep}code={real_code}"
    if state:
        final_url += f"&state={state}"
    return RedirectResponse(url=final_url, status_code=302)


def make_consent_routes() -> list:
    """Return Starlette routes for the OAuth consent + login bridge.

    These are deliberately plain Starlette routes (not a FastAPI router) so
    the OAuth *browser* flow stays off the documented JSON-API surface —
    exactly like the SDK's own ``/authorize`` / ``/token`` / ``/register``
    endpoints, which live in the mounted streamable sub-app and never appear
    in ``app.openapi()``. Appended to ``app.router.routes`` in ``app/main.py``.
    """
    from starlette.routing import Route

    return [
        Route("/api/mcp/oauth/consent", _consent_page, methods=["GET"]),
        Route("/api/mcp/oauth/consent", _consent_submit, methods=["POST"]),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_url(*, request: Request | None = None) -> str:
    """Public base URL for this Agnes instance (no trailing slash)."""
    from app.auth.public_url import public_base_url

    return public_base_url(request=request)


def _get_session_user(request: Request) -> dict | None:
    """Return the INTERACTIVE Agnes session behind this request, or None.

    "Interactive" is the load-bearing word, and it is enforced, not assumed.
    Both callers sit on the consent bridge, whose POST mints an OAuth
    authorization code that the client immediately exchanges for an access
    token **and a 30-day refresh token**. A credential accepted here is
    therefore laundered into a durable successor that outlives revoking the
    original — the same escalation class ``require_session_token`` exists to
    stop on ``POST /auth/tokens``, ``/api/mcp-connect/token`` and agent-PAT
    issuance.

    Two independent rejections, both fail-closed:

    1. ``non_interactive_credential_kind`` — the verbatim
       ``require_session_token`` classification (plain PAT, agent PAT,
       scheduler shared secret, ``X-StorageApi-Token``). Applied BEFORE
       resolution, so a PAT never even reaches the consent page. Previously
       this function handed the ``Authorization: Bearer`` header straight to
       ``resolve_token_to_user``, which happily accepts a plain PAT and
       returns a user dict carrying ``"id"`` — so the only filter here (the
       ``"id" not in user`` co-session exclusion) passed it through, and a
       stolen PAT could mint a refresh token that survived revoking it.
    2. ``credential_surface`` — a credential resolved onto a NARROWED
       data-read surface (``'stack'``) is an AGENT credential, not a person
       at a browser: the web-chat sandbox JWT (``scope="chat"``), the
       brokered replay identity, and an MCP-OAuth connector token itself
       (``scope="mcp-oauth"``) all carry it. Without this an 8-hour
       connector token could re-consent itself an endless chain of fresh
       30-day refresh tokens, and a chat sandbox could mint a durable
       connector credential for its user. A genuine browser session JWT
       carries no ``scope`` claim, hence no ``credential_surface`` key at
       all, which reads as ``'all'`` — the same convention as
       ``src/rbac.py``'s ``_credential_surface`` — so the real login flow is
       untouched.

    This mirrors ``require_session_token`` rather than depending on it
    because these are plain Starlette routes (an HTML consent page and a
    302), deliberately kept off the FastAPI JSON-API surface, and a
    Starlette handler cannot take a ``Depends(...)``. The classification
    itself is imported, not re-implemented, so the two cannot drift.
    """
    from app.auth.dependencies import non_interactive_credential_kind
    from app.auth.pat_resolver import resolve_token_to_user

    if non_interactive_credential_kind(request) is not None:
        return None

    # Try Authorization header first (API clients).
    auth = request.headers.get("authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:]
    # Try the session cookie set by the Google/email/password login flows.
    # The canonical Agnes session cookie is "access_token" (see
    # app/auth/dependencies.py and every provider's set_cookie call) — reading
    # any other name here means the consent page never sees the logged-in user
    # and bounces the browser back to login in an endless loop.
    if not token:
        token = request.cookies.get("access_token", "")
    if not token:
        return None
    user, _ = resolve_token_to_user(None, token, request)
    if user is None:
        return None
    # SessionPrincipal has no "id" key; exclude co-session tokens.
    if not isinstance(user, dict) or "id" not in user:
        return None
    surface = user.get("credential_surface")
    if surface is not None and surface != "all":
        return None
    return user


def _same_origin(request: Request) -> bool:
    """Return True if the request originates from this Agnes instance.

    Checks the Origin header (sent on cross-site form POSTs by modern browsers)
    against the public base URL, falling back to Referer for clients that omit
    Origin. A request with neither header set is treated as same-origin only on
    the local-dev host, since browsers always send one of them on a genuine
    cross-origin POST.
    """
    from urllib.parse import urlparse

    from app.auth.public_url import public_base_url

    base_host = urlparse(public_base_url(request=request)).netloc
    origin = request.headers.get("origin") or ""
    referer = request.headers.get("referer") or ""
    candidate = origin or referer
    if not candidate:
        # No Origin/Referer — a genuine cross-origin browser POST always sends
        # one, so trust only the local-dev host. In production `base_host` is
        # the request-derived public host (e.g. agnes.example.com), so such a
        # header-less POST is rejected — do NOT blanket-trust when unpinned.
        return base_host in ("", "localhost:8000")
    return urlparse(candidate).netloc == base_host


def _login_url(request: Request, pending: str) -> str:
    """Build the login redirect URL that returns to the consent page.

    The OAuth ``state`` is persisted server-side in the pending auth-code row,
    so it is intentionally NOT threaded through the login round-trip URL.
    """
    base = _base_url(request=request)
    consent_path = f"/api/mcp/oauth/consent?pending={pending}"
    # Prefer Google OAuth if available, fall back to email magic-link.
    from app.auth.provider_registry import provider_allowed
    from app.auth.providers.google import is_available as google_available

    if google_available() and provider_allowed("google"):
        from urllib.parse import quote

        return f"{base}/auth/google/login?next={quote(consent_path)}"

    from app.auth.providers.keboola import is_available as keboola_available

    if keboola_available() and provider_allowed("keboola"):
        from urllib.parse import quote

        return f"{base}/auth/keboola/login?next={quote(consent_path)}"
    from urllib.parse import quote

    return f"{base}/login?next={quote(consent_path)}"


def _row_to_client_info(row: dict) -> OAuthClientInformationFull:
    """Convert a repo row to an ``OAuthClientInformationFull`` instance."""
    from app.secrets import decrypt_client_secret

    meta: dict = row.get("client_metadata") or {}
    redirect_uris = row.get("redirect_uris") or []
    return OAuthClientInformationFull(
        client_id=row["client_id"],
        # #869: client_secret is stored encrypted at rest; decrypt to the raw
        # value the SDK's client-auth path compares by equality. Legacy
        # plaintext rows pass through unchanged.
        client_secret=decrypt_client_secret(row.get("client_secret")),
        redirect_uris=[AnyUrl(u) for u in redirect_uris],
        client_name=row.get("client_name"),
        **{k: v for k, v in meta.items() if k not in {"client_id", "client_secret", "redirect_uris", "client_name"}},
    )


def _client_display_name(client_id: str) -> str:
    """Human name of a registered client, falling back to its id."""
    from src.repositories import oauth_clients_repo

    row = oauth_clients_repo().get_client(client_id)
    return (row or {}).get("client_name") or client_id


def _record_consent_outcome(pending: str, *, action: str, client_name: str) -> None:
    """Remember how a consent ended, so a replay of the page can explain itself.

    Stored as an ordinary auth-code row under the ``consent_outcome_`` prefix
    with ``subject=None``: ``exchange_authorization_code`` refuses a subject-less
    code, so the marker is inert as a credential even if its key is guessed.
    The decision lives in ``state`` and the client's name in ``redirect_uri`` —
    both plain text columns — because this row is never used as a grant.
    """
    from src.repositories import oauth_clients_repo

    try:
        oauth_clients_repo().save_auth_code(
            code=_CONSENT_OUTCOME_PREFIX + pending,
            client_id="",
            scopes=[],
            code_challenge="",
            redirect_uri=client_name,
            redirect_uri_provided_explicitly=False,
            expires_at=time.time() + _CONSENT_OUTCOME_TTL,
            subject=None,
            resource=None,
            state=action,
        )
    except Exception:  # pragma: no cover - a marker is never worth failing consent over
        logger.warning("failed to record MCP consent outcome", exc_info=True)


def _finished_or_expired_response(pending: str) -> Response:
    """Response for a pending token that is no longer live.

    A consent link is single-use, and the browser keeps offering it after the
    flow is over: the client takes the redirect (often into a custom scheme, so
    the tab never navigates), the user reloads or clicks Allow twice, and the
    second request finds the row gone. Reporting that as "Authorization request
    expired" told users their connection had failed when it had just succeeded.
    """
    from src.repositories import oauth_clients_repo

    outcome = None
    if pending:
        row = oauth_clients_repo().get_auth_code(_CONSENT_OUTCOME_PREFIX + pending)
        if row is not None and row["expires_at"] >= time.time():
            outcome = row

    if outcome is not None and (outcome.get("state") or "") == "allow":
        client_name = outcome.get("redirect_uri") or "the application"
        return HTMLResponse(
            _render_notice_page(
                title="Connected to Agnes",
                body=(
                    f"Access to Agnes was granted to <span class='app-name'>{_esc(client_name)}</span>. "
                    "You can close this window and continue in the app."
                ),
            )
        )
    if outcome is not None:
        client_name = outcome.get("redirect_uri") or "the application"
        return HTMLResponse(
            _render_notice_page(
                title="Access denied",
                body=(
                    f"<span class='app-name'>{_esc(client_name)}</span> was not given access to Agnes. "
                    "You can close this window."
                ),
            )
        )
    return HTMLResponse(
        _render_notice_page(
            title="This authorization link is no longer valid",
            body=(
                "It was already used, or it sat unused for more than five minutes. "
                "If the app is already connected, nothing is wrong — just close this window. "
                "Otherwise start the connection again from the app."
            ),
        ),
        status_code=400,
    )


def _esc(value: str) -> str:
    import html

    return html.escape(value or "")


def _page_shell(title: str, inner_html: str) -> str:
    """Return a self-contained HTML document sharing the consent-card styling."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)}</title>
  <style>
    :root {{
      --ds-primary: #6366f1;
      --ds-bg: #f8fafc;
      --ds-surface: #ffffff;
      --ds-text: #1e293b;
      --ds-muted: #64748b;
      --ds-border: #e2e8f0;
      --ds-danger: #ef4444;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: system-ui, sans-serif;
      background: var(--ds-bg);
      color: var(--ds-text);
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 1.5rem;
    }}
    .card {{
      background: var(--ds-surface);
      border: 1px solid var(--ds-border);
      border-radius: 0.75rem;
      padding: 2rem;
      max-width: 440px;
      width: 100%;
      box-shadow: 0 4px 24px rgba(0,0,0,.06);
    }}
    h1 {{ font-size: 1.25rem; font-weight: 600; margin-bottom: .5rem; }}
    .sub {{ color: var(--ds-muted); font-size: .875rem; margin-bottom: 1.5rem; }}
    .app-name {{ font-weight: 600; color: var(--ds-text); }}
    .scope-list {{ list-style: none; margin-bottom: 1rem; }}
    .scope-list li {{
      padding: .375rem .75rem;
      background: var(--ds-bg);
      border-radius: .375rem;
      font-size: .875rem;
      margin-bottom: .375rem;
      border: 1px solid var(--ds-border);
    }}
    .note {{ color: var(--ds-muted); font-size: .8rem; margin-bottom: 1.5rem; }}
    .actions {{ display: flex; gap: .75rem; justify-content: flex-end; }}
    button {{
      cursor: pointer;
      border: none;
      border-radius: .5rem;
      padding: .5rem 1.25rem;
      font-size: .9rem;
      font-weight: 500;
    }}
    button[disabled] {{ opacity: .6; cursor: default; }}
    .btn-allow {{
      background: var(--ds-primary);
      color: #fff;
    }}
    .btn-deny {{
      background: var(--ds-bg);
      border: 1px solid var(--ds-border);
      color: var(--ds-text);
    }}
    .user {{ font-size: .8rem; color: var(--ds-muted); margin-top: 1.25rem; }}
  </style>
</head>
<body>
  <div class="card">
{inner_html}
  </div>
</body>
</html>"""


def _render_notice_page(title: str, body: str) -> str:
    """Terminal-state page (already connected / denied / link no longer valid).

    ``body`` may contain markup produced here; every caller-supplied value in it
    is escaped by the caller.
    """
    return _page_shell(
        f"{title} — Agnes",
        f"""    <h1>{_esc(title)}</h1>
    <p class="sub">{body}</p>""",
    )


def _access_summary(scopes: list[str]) -> list[str]:
    """Plain-language capability lines for the consent screen."""
    lines = [_SCOPE_DESCRIPTIONS[s] for s in scopes if s in _SCOPE_DESCRIPTIONS]
    if not lines:
        lines.append(_SCOPE_DESCRIPTIONS["read"])
    lines.append(_WRITE_CAPABILITY)
    lines.append(_RBAC_CAPABILITY)
    return lines


def _render_consent_page(
    user_email: str,
    client_name: str,
    scopes: list[str],
    pending: str,
) -> str:
    """Return the HTML consent page as a self-contained HTML document.

    SECURITY: every interpolated value is HTML-escaped. ``client_name`` comes
    from RFC 7591 dynamic client registration (unauthenticated on the streamable
    MCP endpoint), so it is fully attacker-controlled; rendered unescaped it was
    a stored-XSS sink executing on the Agnes origin in a logged-in victim's
    session, and there is no app-wide CSP to fall back on — escaping here is the
    control.
    """
    import html

    esc_client = html.escape(client_name or "")
    esc_email = html.escape(user_email or "")
    esc_pending = html.escape(pending or "", quote=True)
    capability_items = "".join(f"<li>{html.escape(line)}</li>" for line in _access_summary(list(scopes or [])))
    return _page_shell(
        f"Authorize {esc_client} — Agnes",
        f"""    <h1>Authorize access</h1>
    <p class="sub">
      <span class="app-name">{esc_client}</span>
      is asking to connect to Agnes as you. If you allow it, it will be able to:
    </p>
    <ul class="scope-list">
      {capability_items}
    </ul>
    <p class="note">
      Agnes issues one connection for the whole MCP toolset, so the app sees both
      read and write tools. Access ends when you disconnect the app.
    </p>
    <form method="post" action="/api/mcp/oauth/consent" onsubmit="this.dataset.sent && event.preventDefault(); this.dataset.sent = 1;">
      <input type="hidden" name="pending" value="{esc_pending}">
      <div class="actions">
        <button type="submit" name="action" value="deny" class="btn-deny">Deny</button>
        <button type="submit" name="action" value="allow" class="btn-allow">Allow</button>
      </div>
    </form>
    <p class="user">Signed in as {esc_email}</p>""",
    )
