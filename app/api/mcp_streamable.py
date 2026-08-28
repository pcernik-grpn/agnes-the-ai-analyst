"""Agnes Streamable-HTTP MCP server — OAuth 2.1 transport.

Mounted at /api/mcp/http in app/main.py.  Exposes the same tools as the
SSE MCP server (mcp_http.py) but over the modern Streamable-HTTP transport
that remote MCP connectors (Claude Desktop, Claude.ai, Cursor, Cline, …)
prefer, protected by native OAuth 2.1 + PKCE.

The SSE app continues to live at /api/mcp/sse for Cowork back-compat —
this module does NOT replace it.

Authentication path
-------------------
1. MCP client discovers  GET /.well-known/oauth-protected-resource
   which points to the authorization server at /api/mcp/http.
2. Client registers via POST /api/mcp/http/register (RFC 7591).
3. User browser is redirected through /api/mcp/oauth/consent (our
   bridge, mounted by main.py) which checks the Agnes session and shows
   a consent screen before minting a short-lived authorization code.
4. Client exchanges code for a JWT at POST /api/mcp/http/token.
5. All subsequent MCP requests carry  Authorization: Bearer <JWT>.
   The JWT is a standard Agnes session JWT — resolve_token_to_user
   accepts it and all RBAC applies unchanged.

Tools
-----
Registers the same 24 foundation tools as the SSE server (mcp_http.py) via
the shared ``app.api.mcp.foundation_tools.register_foundation_tools`` — the
single source of truth for tool definitions, so the two transports cannot
drift out of parity again.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import AsyncIterator

from mcp.server.auth.handlers.revoke import RevocationHandler, RevocationRequest
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.mcp.foundation_tools import SERVER_INSTRUCTIONS, register_foundation_tools
from app.auth.mcp_oauth import AgnesMCPOAuthProvider
from app.auth.public_url import mcp_issuer_url, pinned_public_base_url, public_base_url

logger = logging.getLogger(__name__)

_BASE = os.environ.get("AGNES_MCP_INTERNAL_URL", "http://localhost:8000").rstrip("/")


@contextlib.asynccontextmanager
async def streamable_session_manager_lifespan(app) -> AsyncIterator[None]:
    """Run the streamable MCP session manager for the lifetime of the app.

    Wire this into the main app lifespan (``async with …(app): yield``).
    Starlette does NOT run a mounted sub-app's lifespan, so without this the
    streamable endpoint raises "Task group is not initialized" on the first
    request.

    The FastMCP instance is read from ``app.state`` (set by create_app when
    the streamable app is mounted), never a module global — each app gets its
    own instance, so the SDK's "session_manager.run() once per instance" rule
    holds across repeated create_app() calls in tests. No-op if the streamable
    app was never mounted.
    """
    mcp = getattr(app.state, "mcp_streamable_instance", None)
    if mcp is None:
        yield
        return
    # The SDK's StreamableHTTPSessionManager.run() may be called at most once
    # per instance (and the mounted ASGI app captures that single manager, so
    # it can't be swapped). A real process enters the lifespan exactly once;
    # only tests re-enter it on the same app singleton to simulate a restart.
    # Guard so the second entry is a no-op rather than a hard RuntimeError.
    if getattr(mcp, "_agnes_session_manager_started", False):
        yield
        return
    mcp._agnes_session_manager_started = True
    async with mcp.session_manager.run():
        yield


def _headers() -> dict[str, str]:
    """Forward the caller's verified OAuth access token to Agnes self-calls.

    The SDK's auth middleware authenticates the bearer token and exposes the
    resulting ``AccessToken`` via ``get_access_token()``.  Its ``.token`` is
    the raw JWT minted by ``exchange_authorization_code`` — a standard Agnes
    session JWT that ``resolve_token_to_user`` accepts unchanged.
    """
    access = get_access_token()
    if access is None or not access.token:
        raise RuntimeError("No authentication token in current MCP context")
    return {"Authorization": f"Bearer {access.token}"}


def _current_caller_id() -> str | None:
    """Resolve the current request's caller user id from the verified access
    token, for passthrough tools to forward per-user identity into
    ``call_tool_async``. Returns None when unresolvable (no token / bad token)
    — the passthrough then fails closed for a ``scope='per_user'`` source
    rather than borrowing the shared credential.

    The token here is a standard Agnes session JWT (already signature-verified
    by the SDK's token verifier before any tool runs), whose ``sub`` claim is
    the user id. We decode it locally rather than hitting the system DB —
    ``mcp_streamable`` is intentionally free of raw system-DB callers
    (backend-split ratchet), and a JWT decode needs no backend.
    """
    access = get_access_token()
    if access is None or not access.token:
        return None
    try:
        from app.auth.jwt import verify_token

        payload = verify_token(access.token) or {}
    except Exception:
        logger.exception("Streamable MCP: caller id resolution failed")
        return None
    sub = payload.get("sub")
    return str(sub) if sub else None


def _oauth_client_registration_options() -> ClientRegistrationOptions:
    return ClientRegistrationOptions(enabled=True, valid_scopes=["read"], default_scopes=["read"])


def _oauth_revocation_options() -> RevocationOptions:
    return RevocationOptions(enabled=True)


class _PublicClientRevocationRequest(RevocationRequest):
    """RFC 7009 §2.1 revocation request with ``client_secret`` truly optional.

    The SDK model declares ``client_secret: str | None`` with no default —
    pydantic v2 reads that as required-but-nullable — so a public client
    (``token_endpoint_auth_method='none'``: Claude Code, VS Code, claude.ai)
    posting ``token=…&client_id=…`` is rejected 400 "client_secret: Field
    required" before revocation runs, and its tokens live out their full TTL.
    Still broken upstream as of mcp 2.0.0.
    """

    client_secret: str | None = None


class _PublicClientRevocationHandler(RevocationHandler):
    """The SDK's RevocationHandler with the lenient request model above.

    ``handle`` mirrors the SDK implementation except for the model swap —
    client *authentication* is already auth-method-aware
    (``ClientAuthenticator.authenticate_request`` skips the secret check for
    public clients), so only the post-auth form validation needed fixing.
    Confidential clients are unaffected: a stored secret is still enforced by
    the authenticator before the form is ever validated.

    Maintenance: the ``mcp>=1.28.1`` floor in pyproject.toml is load-bearing
    here — older SDK lines have no ``authenticate_request`` (only
    ``authenticate(client_id, client_secret)``, with form validation running
    *before* client auth). On an SDK bump, re-diff this method against the
    SDK's ``RevocationHandler.handle`` (kept 1:1 minus the model) so semantics
    the SDK adds are not silently dropped, and delete the whole patch once
    upstream makes ``client_secret`` truly optional.
    """

    async def handle(self, request: Request) -> Response:
        from collections.abc import Awaitable, Callable
        from functools import partial

        from mcp.server.auth.errors import stringify_pydantic_error
        from mcp.server.auth.handlers.revoke import RevocationErrorResponse
        from mcp.server.auth.json_response import PydanticJSONResponse
        from mcp.server.auth.middleware.client_auth import AuthenticationError
        from mcp.server.auth.provider import AccessToken, RefreshToken
        from pydantic import ValidationError

        try:
            client = await self.client_authenticator.authenticate_request(request)
        except AuthenticationError as e:
            return PydanticJSONResponse(
                status_code=401,
                content=RevocationErrorResponse(
                    error="unauthorized_client",
                    error_description=e.message,
                ),
            )

        try:
            form_data = await request.form()
            revocation_request = _PublicClientRevocationRequest.model_validate(dict(form_data))
        except ValidationError as e:
            return PydanticJSONResponse(
                status_code=400,
                content=RevocationErrorResponse(
                    error="invalid_request",
                    error_description=stringify_pydantic_error(e),
                ),
            )

        loaders: list[Callable[[str], Awaitable[AccessToken | RefreshToken | None]]] = [
            self.provider.load_access_token,
            partial(self.provider.load_refresh_token, client),
        ]
        if revocation_request.token_type_hint == "refresh_token":
            loaders = list(reversed(loaders))

        token: AccessToken | RefreshToken | None = None
        for loader in loaders:
            token = await loader(revocation_request.token)
            if token is not None:
                break

        # Unknown token → still 200, per RFC 7009 §2.2.
        if token and token.client_id == client.client_id:
            await self.provider.revoke_token(token)

        return Response(status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _patch_revocation_route_for_public_clients(app: Starlette, provider: AgnesMCPOAuthProvider) -> None:
    """Swap the SDK's /revoke endpoint for the public-client-aware handler.

    Route-level (not a middleware layer): the fix changes request validation,
    so the clean seam is the handler itself. If an SDK bump moves the route
    and the patch stops applying, the handshake test asserting a 200
    public-client revoke catches it.
    """
    from mcp.server.auth.middleware.client_auth import ClientAuthenticator
    from mcp.server.auth.routes import REVOCATION_PATH, cors_middleware
    from starlette.routing import Route

    handler = _PublicClientRevocationHandler(provider, ClientAuthenticator(provider))
    routes = app.router.routes
    for i, route in enumerate(routes):
        if isinstance(route, Route) and route.path == REVOCATION_PATH:
            routes[i] = Route(
                REVOCATION_PATH,
                endpoint=cors_middleware(handler.handle, ["POST", "OPTIONS"]),
                methods=["POST", "OPTIONS"],
            )
            return
    logger.error("Streamable MCP: /revoke route not found — public-client token revocation patch not applied")


def _oauth_metadata_for_request(request: Request):
    """Build OAuth AS + protected-resource metadata for the incoming host."""
    from urllib.parse import urlparse

    from mcp.server.auth.routes import build_metadata, build_resource_metadata_url
    from mcp.shared.auth import ProtectedResourceMetadata

    issuer = AnyHttpUrl(mcp_issuer_url(request=request))
    as_metadata = build_metadata(
        issuer_url=issuer,
        service_documentation_url=None,
        client_registration_options=_oauth_client_registration_options(),
        revocation_options=_oauth_revocation_options(),
    )
    # RFC 8252: public clients (VS Code, native apps) use
    # token_endpoint_auth_method=none. build_metadata() hardcodes only
    # confidential auth methods — extend the list so VS Code's discovery
    # check passes and it proceeds with Dynamic Client Registration instead
    # of showing the manual client-ID dialog.
    _supported = list(as_metadata.token_endpoint_auth_methods_supported or [])
    if "none" not in _supported:
        as_metadata.token_endpoint_auth_methods_supported = _supported + ["none"]
    # Same RFC 8252 rationale for revocation: /revoke accepts public clients
    # (see _PublicClientRevocationHandler), so advertise 'none' there too.
    if as_metadata.revocation_endpoint:
        _rev_supported = list(as_metadata.revocation_endpoint_auth_methods_supported or [])
        if "none" not in _rev_supported:
            as_metadata.revocation_endpoint_auth_methods_supported = _rev_supported + ["none"]

    pr_metadata = ProtectedResourceMetadata(
        resource=issuer,
        authorization_servers=[issuer],
        scopes_supported=["read"],
        resource_name="Agnes",
    )
    pr_path = urlparse(str(build_resource_metadata_url(issuer))).path
    return as_metadata, pr_metadata, pr_path


_MCP_OAUTH_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource/api/mcp/http"


def _mcp_oauth_discovery_routes() -> list:
    """Return root-level OAuth discovery routes (RFC 8414 + RFC 9728).

    The streamable sub-app already serves these relative to its mount at
    ``/api/mcp/http``, but standards-compliant MCP clients probe the origin
    root — ``GET https://host/.well-known/oauth-authorization-server`` and
    ``GET https://host/.well-known/oauth-protected-resource/api/mcp/http`` —
    so we publish identical documents there too. Endpoint URLs inside the
    documents are derived from the request host when ``AGNES_BASE_URL`` /
    ``SERVER_URL`` are unset, so production behind a TLS proxy advertises the
    public connector URL without requiring a separate env var.

    Because the authorization-server issuer carries a path component
    (``/api/mcp/http``), RFC 8414 §3 says a compliant client builds the
    metadata URL by inserting the well-known segment *between host and path*:
    ``/.well-known/oauth-authorization-server/api/mcp/http``. Lenient clients
    (Claude) fall back to the bare root document, but stricter ones (Cursor,
    GitHub Copilot, ChatGPT web) probe only the path-aware location and 404 →
    they never discover the authorize/token/register endpoints and surface
    "authentication required" before OAuth can even start. We therefore serve
    the *same* AS document at the path-aware ``oauth-authorization-server`` and
    OpenID-Connect ``openid-configuration`` locations as well. The root
    protected-resource document already uses the path-aware form, so no
    sibling is needed there.
    """
    from mcp.server.auth.handlers.metadata import (
        MetadataHandler,
        ProtectedResourceMetadataHandler,
    )
    from starlette.routing import Route

    async def oauth_authorization_server(request: Request):
        as_metadata, _, _ = _oauth_metadata_for_request(request)
        return await MetadataHandler(as_metadata).handle(request)

    async def oauth_protected_resource(request: Request):
        _, pr_metadata, _ = _oauth_metadata_for_request(request)
        return await ProtectedResourceMetadataHandler(pr_metadata).handle(request)

    # The AS document is published at the bare root *and* at the path-aware
    # RFC 8414 / OIDC locations that carry the issuer's ``/api/mcp/http`` path
    # suffix, so strict clients (Cursor, Copilot, ChatGPT web) discover it.
    as_paths = [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/api/mcp/http",
        "/.well-known/openid-configuration",
        "/.well-known/openid-configuration/api/mcp/http",
    ]
    routes = [Route(p, endpoint=oauth_authorization_server, methods=["GET", "OPTIONS"]) for p in as_paths]
    routes.append(
        Route(
            _MCP_OAUTH_PROTECTED_RESOURCE_PATH,
            endpoint=oauth_protected_resource,
            methods=["GET", "OPTIONS"],
        )
    )
    return routes


class _ServeStreamableAtMountRootMiddleware:
    """Serve the streamable MCP endpoint at the sub-app's mount root.

    FastMCP routes the streamable transport at its internal ``/mcp`` path, so
    after mounting at ``/api/mcp/http`` the JSON-RPC endpoint physically lives
    at ``/api/mcp/http/mcp`` — but the advertised connector URL (and the OAuth
    ``resource``) is the mount itself. Clients POST to the URL they were given
    verbatim, hit a 404, and surface it as "MCP endpoint not found" right
    after a successful OAuth. Rewrite mount-root requests to the transport
    path; ``/api/mcp/http/mcp`` keeps working unchanged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            # Starlette's Mount keeps the full URL in scope["path"] and records
            # the mount prefix in root_path — compare the mount-relative part.
            root_path = scope.get("root_path", "")
            route_path = scope.get("path", "")
            if route_path.startswith(root_path):
                route_path = route_path[len(root_path) :]
            if route_path in ("", "/"):
                new_path = f"{root_path}/mcp"
                scope = {**scope, "path": new_path, "raw_path": new_path.encode()}
        await self._app(scope, receive, send)


def mount_root_route(streamable_app: ASGIApp, mount_path: str = "/api/mcp/http"):
    """Exact-path route for the bare (slash-less) advertised connector URL.

    Starlette's ``Mount`` only matches ``<mount>/…`` — the bare URL, which is
    exactly what users paste into their MCP client, falls through to the next
    matching route: the broader SSE mount at ``/api/mcp``, whose router 404s
    it once auth passes. Forward it into the streamable app as a mount-root
    request so ``_ServeStreamableAtMountRootMiddleware`` lands it on the
    transport path. Register this on the main app next to the mount.
    """
    from starlette.routing import Route

    class _ForwardToStreamable:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            path = f"{mount_path}/"
            await streamable_app(
                {
                    **scope,
                    "path": path,
                    "raw_path": path.encode(),
                    "root_path": mount_path,
                    # Mirror Starlette's Mount: pin app_root_path to the
                    # top-level app root, else Request.base_url absorbs the
                    # connector mount path and the WWW-Authenticate
                    # resource_metadata URL comes out doubled.
                    "app_root_path": scope.get("app_root_path", scope.get("root_path", "")),
                },
                receive,
                send,
            )

    return Route(mount_path, endpoint=_ForwardToStreamable(), methods=["GET", "POST", "DELETE", "OPTIONS"])


class _FixMcpOAuthResourceMetadataMiddleware:
    """Rewrite ``WWW-Authenticate`` resource_metadata for proxied deployments.

    The MCP SDK pins ``resource_metadata`` at app-build time from
    ``AuthSettings.issuer_url``. When neither ``AGNES_BASE_URL`` nor
    ``SERVER_URL`` is set, that defaults to ``http://localhost:8000`` even
    though clients reach us at the public host. Derive the correct URL from
    the incoming ASGI scope instead.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or pinned_public_base_url() is not None:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        correct_metadata_url = f"{public_base_url(request=request)}/.well-known/oauth-protected-resource/api/mcp/http"

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = []
                for name, value in message.get("headers", []):
                    if name.lower() == b"www-authenticate":
                        text = value.decode("latin-1")
                        if "resource_metadata=" in text:
                            text = re.sub(
                                r'resource_metadata="[^"]*"',
                                f'resource_metadata="{correct_metadata_url}"',
                                text,
                            )
                        value = text.encode("latin-1")
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


class _PatchPublicClientDiscoveryMiddleware:
    """Append 'none' to the advertised auth-method lists in FastMCP's OAuth discovery.

    Patches both token_endpoint_auth_methods_supported and (when the endpoint
    is advertised) revocation_endpoint_auth_methods_supported.
    build_metadata() in the MCP SDK hardcodes only confidential auth methods.
    VS Code native MCP is a public client (method=none, no client_secret + PKCE)
    and skips Dynamic Client Registration when it doesn't see 'none' in this list.
    This middleware patches the JSON body served by the FastMCP sub-app's own
    /.well-known/oauth-authorization-server endpoint (distinct from the root-level
    route patched in _oauth_metadata_for_request).
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].endswith("/.well-known/oauth-authorization-server"):
            await self._app(scope, receive, send)
            return

        # Collect the response from the inner app then patch the body.
        response_started = False
        status_code = 200
        headers: list = []
        body_chunks: list[bytes] = []

        async def _patched_send(message):  # type: ignore[no-untyped-def]
            nonlocal response_started, status_code, headers
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = list(message.get("headers", []))
                return
            elif message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    import json as _json

                    raw = b"".join(body_chunks)
                    try:
                        data = _json.loads(raw)
                        methods = data.get("token_endpoint_auth_methods_supported") or []
                        if "none" not in methods:
                            data["token_endpoint_auth_methods_supported"] = methods + ["none"]
                        if data.get("revocation_endpoint"):
                            rev_methods = data.get("revocation_endpoint_auth_methods_supported") or []
                            if "none" not in rev_methods:
                                data["revocation_endpoint_auth_methods_supported"] = rev_methods + ["none"]
                        patched = _json.dumps(data).encode()
                    except Exception:
                        patched = raw
                    new_headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
                    new_headers.append((b"content-length", str(len(patched)).encode()))
                    await send(
                        {
                            "type": "http.response.start",
                            "status": status_code,
                            "headers": new_headers,
                        }
                    )
                    await send({"type": "http.response.body", "body": patched, "more_body": False})
                return
            # Forward any other ASGI message types unchanged.
            await send(message)

        await self._app(scope, receive, _patched_send)


def _make_streamable_app() -> ASGIApp:
    """Build and return the Streamable-HTTP MCP ASGI app with OAuth 2.1."""
    # The MCP endpoint URL — clients paste this into their connector config.
    mcp_url = mcp_issuer_url()

    provider = AgnesMCPOAuthProvider()

    auth = AuthSettings(
        issuer_url=AnyHttpUrl(mcp_url),
        resource_server_url=AnyHttpUrl(mcp_url),
        client_registration_options=_oauth_client_registration_options(),
        revocation_options=_oauth_revocation_options(),
        required_scopes=["read"],
    )

    mcp = FastMCP(
        "Agnes",
        instructions=SERVER_INSTRUCTIONS,
        # DNS-rebinding/Host-header protection is disabled deliberately: this is
        # a REMOTE connector reached through a TLS-terminating reverse proxy on a
        # fixed FQDN (operators set AGNES_BASE_URL to that host), and the proxy
        # rewrites Host. The SDK's allowed-hosts check would otherwise reject the
        # legitimate proxied Host. Every request is still OAuth-bearer-gated
        # before reaching a tool, so a rebound origin gains nothing without a
        # valid token. Mirrors the SSE server's stance in mcp_http.py.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        auth=auth,
        auth_server_provider=provider,
        stateless_http=True,
    )

    # ── tools ─────────────────────────────────────────────────────────────
    # Shared with the SSE server (mcp_http.py) — see foundation_tools.py.
    register_foundation_tools(mcp, base_url=_BASE, headers_fn=_headers)

    _register_dynamic_tools(mcp)

    # Stash the FastMCP instance on the returned app's state so create_app can
    # lift it onto the main app and run its session manager in the lifespan.
    inner = mcp.streamable_http_app()
    inner.state.mcp_streamable_instance = mcp
    # Fix the SDK's /revoke for public clients (RFC 7009) before wrapping —
    # see _PublicClientRevocationHandler.
    _patch_revocation_route_for_public_clients(inner, provider)
    # Layer 0: serve the MCP endpoint at the mount root — the URL clients
    # actually paste — not only at the SDK-internal /mcp sub-path.
    rooted = _ServeStreamableAtMountRootMiddleware(inner)
    # Layer 1: patch WWW-Authenticate resource_metadata for proxied deployments.
    wrapped = _FixMcpOAuthResourceMetadataMiddleware(rooted)
    wrapped.state = inner.state
    # Layer 2: patch the FastMCP sub-app's own OAuth discovery to add 'none'
    # so VS Code native MCP proceeds with Dynamic Client Registration.
    patched = _PatchPublicClientDiscoveryMiddleware(wrapped)
    patched.state = inner.state  # type: ignore[attr-defined]
    return patched


def _register_dynamic_tools(mcp: FastMCP) -> None:
    """Best-effort registration of passthrough tools from tool_registry."""
    try:
        from app.api.mcp.tools_generator import (
            install_grant_filtered_list_tools,
            register_passthrough_tools,
        )
    except Exception:
        logger.exception("Streamable MCP: dynamic tool imports unavailable")
        return
    names: list[str] = []
    try:
        names = register_passthrough_tools(mcp, caller_id_fn=_current_caller_id)
        if names:
            logger.info("Streamable MCP: registered %d passthrough tools", len(names))
    except Exception:
        logger.exception("Streamable MCP: passthrough tool registration failed")
    # Hide passthrough tools the caller isn't granted from tools/list (their
    # invocation is already gated; this matches the REST listing's visibility).
    # Pass the registered names so the filter's hide-set is fixed at install
    # time and a runtime grant-resolution error fails closed (see the helper).
    try:
        install_grant_filtered_list_tools(mcp, caller_id_fn=_current_caller_id, passthrough_names=names)
    except Exception:
        logger.exception("Streamable MCP: grant-filtered tools/list install failed")
