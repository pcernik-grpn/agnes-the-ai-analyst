"""End-to-end tests for the native OAuth 2.1 remote MCP connector.

Covers the surface a remote MCP client (Claude Desktop, Claude.ai, Cursor,
Cline, ChatGPT connectors, custom MCP SDK clients) drives when adding Agnes
as a custom connector:

  * OAuth discovery metadata published at the ORIGIN ROOT (RFC 8414 + 9728).
  * Unauthenticated MCP request → 401 with a ``WWW-Authenticate: Bearer``
    challenge that points at the protected-resource metadata.
  * RFC 7591 dynamic client registration.
  * The full authorization-code + PKCE flow: register → authorize → consent
    (bridged to the existing Agnes session) → token exchange → a usable
    access token that the provider verifies.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse


MCP_MOUNT = "/api/mcp/http"
MCP_ENDPOINT = f"{MCP_MOUNT}/mcp"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def test_discovery_metadata_at_origin_root(seeded_app):
    client = seeded_app["client"]

    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["authorization_endpoint"].endswith("/api/mcp/http/authorize")
    assert meta["token_endpoint"].endswith("/api/mcp/http/token")
    assert meta["registration_endpoint"].endswith("/api/mcp/http/register")
    assert "S256" in meta["code_challenge_methods_supported"]

    r = client.get("/.well-known/oauth-protected-resource/api/mcp/http")
    assert r.status_code == 200, r.text
    pr = r.json()
    assert pr["resource"].endswith("/api/mcp/http")
    assert any(s.endswith("/api/mcp/http") for s in pr["authorization_servers"])


def test_discovery_metadata_at_path_aware_locations(seeded_app):
    """Strict clients (Cursor, Copilot, ChatGPT web) build the AS metadata URL
    per RFC 8414 §3 by inserting the well-known segment between host and the
    issuer's ``/api/mcp/http`` path. Lenient clients (Claude) fall back to the
    bare root, so only path-aware probers regressed before this fix."""
    client = seeded_app["client"]

    for path in (
        "/.well-known/oauth-authorization-server/api/mcp/http",
        "/.well-known/openid-configuration",
        "/.well-known/openid-configuration/api/mcp/http",
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.text}"
        meta = r.json()
        assert meta["authorization_endpoint"].endswith("/api/mcp/http/authorize")
        assert meta["token_endpoint"].endswith("/api/mcp/http/token")
        assert meta["registration_endpoint"].endswith("/api/mcp/http/register")


def test_subapp_discovery_includes_none_and_no_double_send(seeded_app):
    """The MCP sub-app's own /.well-known/oauth-authorization-server endpoint is
    patched by _PatchPublicClientDiscoveryMiddleware to add 'none' to
    token_endpoint_auth_methods_supported. The middleware must not double-send
    the http.response.start ASGI message (which would cause uvicorn to raise
    RuntimeError('Response already started'))."""
    client = seeded_app["client"]

    r = client.get("/api/mcp/http/.well-known/oauth-authorization-server")
    assert r.status_code == 200, r.text
    meta = r.json()
    assert "none" in meta.get("token_endpoint_auth_methods_supported", [])


def test_discovery_metadata_uses_request_host_when_env_unset(seeded_app, monkeypatch):
    """Production behind a TLS proxy must advertise the public host even when
    AGNES_BASE_URL / SERVER_URL are unset."""
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    monkeypatch.delenv("SERVER_URL", raising=False)

    from app.main import create_app
    from starlette.testclient import TestClient

    client = TestClient(create_app(), base_url="https://agnes.example.com")
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["issuer"] == "https://agnes.example.com/api/mcp/http"
    assert meta["authorization_endpoint"] == "https://agnes.example.com/api/mcp/http/authorize"

    r = client.get("/.well-known/oauth-protected-resource/api/mcp/http")
    assert r.status_code == 200, r.text
    pr = r.json()
    assert pr["resource"] == "https://agnes.example.com/api/mcp/http"


def test_unauthenticated_mcp_www_authenticate_uses_request_host(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    monkeypatch.delenv("SERVER_URL", raising=False)

    from app.main import create_app
    from starlette.testclient import TestClient

    client = TestClient(create_app(), base_url="https://agnes.example.com")
    r = client.post(
        MCP_ENDPOINT,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    www = r.headers.get("www-authenticate", "")
    assert 'resource_metadata="https://agnes.example.com/.well-known/oauth-protected-resource/api/mcp/http"' in www


def test_unauthenticated_mcp_returns_401_challenge(seeded_app):
    client = seeded_app["client"]
    r = client.post(
        MCP_ENDPOINT,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    www = r.headers.get("www-authenticate", "")
    assert www.lower().startswith("bearer")
    assert "resource_metadata" in www


def test_advertised_connector_url_reaches_mcp_data_plane(seeded_app):
    """The connector URL users paste is the mount root (``/api/mcp/http``),
    while the SDK routes the transport at the internal ``/mcp`` sub-path.
    Without the mount-root rewrite the data plane 404s — which clients
    surface as "MCP endpoint not found" immediately after a successful
    OAuth. An unauthenticated POST must reach the SDK's auth layer (401
    challenge), never the router's 404.
    """
    client = seeded_app["client"]
    for path in (MCP_MOUNT, MCP_MOUNT + "/"):
        r = client.post(
            path,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
            follow_redirects=True,
        )
        assert r.status_code == 401, f"{path}: expected 401 challenge, got {r.status_code}"
        www = r.headers.get("www-authenticate", "")
        assert www.lower().startswith("bearer")
        # The resource_metadata URL must sit at the origin root — a mount
        # path leaking into the base URL doubles it and breaks discovery.
        assert "/.well-known/oauth-protected-resource/api/mcp/http" in www
        assert "/api/mcp/http/.well-known" not in www, f"{path}: doubled resource_metadata URL: {www}"


def _register_client(client, auth_method: str = "client_secret_post") -> dict:
    r = client.post(
        f"{MCP_MOUNT}/register",
        json={
            "client_name": "Test MCP Client",
            "redirect_uris": ["http://localhost:9999/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": auth_method,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_dynamic_client_registration(seeded_app):
    reg = _register_client(seeded_app["client"])
    assert reg["client_id"]
    assert "http://localhost:9999/callback" in reg["redirect_uris"]


def test_full_authorization_code_flow(seeded_app_fresh):
    admin_token = seeded_app_fresh["admin_token"]
    redirect_uri = "http://localhost:9999/callback"

    # Enter the TestClient context so the app lifespan runs — the streamable
    # MCP session manager must be active for the step-5 JSON-RPC call. Needs
    # its own `create_app()` (seeded_app_fresh, not the shared seeded_app):
    # the SDK's session manager can only run() once per instance (see
    # tests/conftest.py::seeded_app's docstring).
    with seeded_app_fresh["client"] as client:
        _run_full_flow(client, admin_token, redirect_uri)


def _run_full_flow(client, admin_token, redirect_uri):
    reg = _register_client(client)
    client_id = reg["client_id"]
    verifier, challenge = _pkce()

    # 1. authorize — the SDK validates params then redirects to our consent
    #    bridge. follow_redirects=False so we can read the Location chain.
    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), r.text
    consent_loc = r.headers["location"]
    assert "/api/mcp/oauth/consent" in consent_loc
    pending = parse_qs(urlparse(consent_loc).query)["pending"][0]

    # 2. consent GET with an authenticated Agnes session → shows the page
    #    (not a redirect to login). We carry the session via Bearer header.
    auth_hdr = {"Authorization": f"Bearer {admin_token}"}
    r = client.get(
        "/api/mcp/oauth/consent",
        params={"pending": pending, "state": "xyz"},
        headers=auth_hdr,
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    assert "Authorize access" in r.text

    # 3. consent POST allow → redirect back to the client with ?code=.
    #    Send a TAMPERED state in the form body to prove it is ignored: the
    #    authoritative state is the one persisted server-side at authorize().
    #    A genuine browser same-origin form POST carries an Origin header; send
    #    one so the consent CSRF gate (_same_origin) sees it as same-origin.
    r = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "state": "TAMPERED", "action": "allow"},
        headers={**auth_hdr, "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), r.text
    final = r.headers["location"]
    assert final.startswith(redirect_uri)
    qs = parse_qs(urlparse(final).query)
    assert qs["state"][0] == "xyz", "form-body state must not override the persisted state"
    code = qs["code"][0]

    # 4. token exchange with the PKCE verifier
    r = client.post(
        f"{MCP_MOUNT}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": reg.get("client_secret", ""),
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    tok = r.json()
    access_token = tok["access_token"]
    assert tok["token_type"].lower() == "bearer"
    assert tok.get("refresh_token")

    # 5. the minted token authenticates: the OAuth provider verifies it as a
    #    live access token bound to the authorizing user. (Deterministic — does
    #    not depend on the streamable session manager being warm under load.)
    import asyncio

    from app.auth.mcp_oauth import AgnesMCPOAuthProvider

    verified = asyncio.run(AgnesMCPOAuthProvider().load_access_token(access_token))
    assert verified is not None, "minted access token must verify"
    assert verified.client_id == client_id
    assert verified.subject  # bound to the authorizing Agnes user

    # 6. the minted token drives a real JSON-RPC call at the ADVERTISED
    #    connector URL (the mount root, exactly what the user pasted) — the
    #    post-OAuth step where a routing gap would 404 ("MCP endpoint not
    #    found"). Requires the session manager, hence the lifespan context.
    r = client.post(
        MCP_MOUNT,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "handshake-test", "version": "0"},
            },
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200, f"initialize at the advertised URL failed: {r.status_code} {r.text[:200]}"
    assert "serverInfo" in r.text


def test_consent_reads_session_from_access_token_cookie(seeded_app):
    """The browser drives consent with the Agnes session COOKIE, not a Bearer
    header. The canonical session cookie is ``access_token`` (set by every
    login provider) — if the consent bridge reads any other cookie name it
    never sees the logged-in user and bounces back to login forever. This
    exercises the cookie branch the Bearer-header happy-path test misses.
    """
    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    reg = _register_client(client)
    _, challenge = _pkce()

    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]

    # Consent GET carrying the session as the access_token cookie (no Bearer
    # header) must render the consent page, NOT redirect to login.
    r = client.get(
        "/api/mcp/oauth/consent",
        params={"pending": pending, "state": "xyz"},
        cookies={"access_token": admin_token},
        follow_redirects=False,
    )
    assert r.status_code == 200, (
        f"consent must read the session from the access_token cookie "
        f"(got {r.status_code}, location={r.headers.get('location')})"
    )
    assert "Authorize access" in r.text


def test_consent_post_rejects_cross_origin(seeded_app):
    """The consent POST mints an auth code off the session — cross-origin
    submits (CSRF) must be rejected with 403."""
    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    reg = _register_client(client)
    _, challenge = _pkce()

    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]

    r = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        headers={
            "Authorization": f"Bearer {admin_token}",
            "Origin": "https://evil.example.com",
        },
        follow_redirects=False,
    )
    assert r.status_code == 403, r.text


def test_consent_post_rejects_missing_origin_when_unpinned(seeded_app, monkeypatch):
    """A header-less consent POST must be rejected even when no public base URL
    is pinned (AGNES_BASE_URL/SERVER_URL unset). A genuine browser always sends
    Origin/Referer on a cross-origin form POST, so a header-less submit is a
    CSRF signal — the gate must not blanket-trust it just because the host is
    request-derived rather than env-pinned."""
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    monkeypatch.delenv("SERVER_URL", raising=False)

    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    reg = _register_client(client)
    _, challenge = _pkce()

    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]

    r = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        headers={"Authorization": f"Bearer {admin_token}"},  # no Origin/Referer
        follow_redirects=False,
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# The consent bridge is INTERACTIVE-SESSION-ONLY
#
# Its POST mints an authorization code that the client exchanges for an access
# token AND a 30-day refresh token. Accepting a non-interactive credential
# there launders it into a durable successor that outlives revoking the
# original — so `_get_session_user` must reject exactly what
# `require_session_token` rejects on `POST /auth/tokens`.
# ---------------------------------------------------------------------------


def _mint_full_surface_pat(user_id: str, email: str) -> str:
    """Create a real, live, FULL-surface PAT for ``user_id`` and return the JWT.

    Full surface (the `surface="all"` default) on purpose: it proves the guard
    rejects on the PAT `typ` claim itself, not merely on a narrowed
    `credential_surface`. The token_hash must be the sha256 of the JWT or the
    defense-in-depth check in `resolve_token_to_user` rejects it as
    `pat_mismatch` before the guard under test ever runs.
    """
    import hashlib
    import uuid
    from datetime import datetime, timedelta, timezone

    from app.auth.jwt import create_access_token
    from src.repositories import access_token_repo

    tid = str(uuid.uuid4())
    pat = create_access_token(user_id=user_id, email=email, token_id=tid, typ="pat")
    access_token_repo().create(
        id=tid,
        user_id=user_id,
        name="stolen-pat",
        token_hash=hashlib.sha256(pat.encode()).hexdigest(),
        prefix=tid.replace("-", "")[:8],
        expires_at=datetime.now(timezone.utc) + timedelta(days=90),
    )
    return pat


def _pending_for_new_client(client) -> str:
    """Register a client, drive /authorize, return the pending consent token."""
    reg = _register_client(client)
    _, challenge = _pkce()
    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), r.text
    return parse_qs(urlparse(r.headers["location"]).query)["pending"][0]


def test_consent_post_rejects_pat(seeded_app):
    """A caller holding only a stolen PAT must not be able to mint an OAuth
    authorization code — the code exchanges for a 30-day refresh token, i.e. a
    durable credential that would survive revoking the PAT it was minted from.

    `_same_origin` does NOT cover this: it is a CSRF control against a tricked
    *browser*, and an Origin header is trivially forged by the programmatic
    caller a PAT holder is — so this request sends the correct same-origin
    Origin and must still be refused on authentication grounds.
    """
    client = seeded_app["client"]
    pat = _mint_full_surface_pat("admin1", "admin@test.com")
    pending = _pending_for_new_client(client)

    r = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        headers={"Authorization": f"Bearer {pat}", "Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert r.status_code == 401, f"a PAT must not mint an authorization code (got {r.status_code})"
    # Belt and braces: whatever the status, no code may have been handed out.
    assert "code=" not in r.headers.get("location", ""), "PAT-authenticated consent leaked an authorization code"


def test_consent_get_rejects_pat(seeded_app):
    """The consent GET is the same door: a PAT holder is not a logged-in
    browser, so they get bounced to login rather than shown the page (which
    leaks the account's email and the requesting client's name)."""
    client = seeded_app["client"]
    pat = _mint_full_surface_pat("admin1", "admin@test.com")
    pending = _pending_for_new_client(client)

    r = client.get(
        "/api/mcp/oauth/consent",
        params={"pending": pending},
        headers={"Authorization": f"Bearer {pat}"},
        follow_redirects=False,
    )

    assert r.status_code == 302, f"a PAT must not be shown the consent page (got {r.status_code})"
    assert "Authorize access" not in r.text


def test_consent_post_rejects_agent_surface_session_jwt(seeded_app):
    """An MCP-OAuth connector token (`scope="mcp-oauth"`) is a session JWT, not
    a PAT — but it is an AGENT credential, resolved onto the narrowed 'stack'
    surface. Letting it consent would let an 8-hour connector token re-mint
    itself an endless chain of fresh 30-day refresh tokens. Same rule applies
    to the web-chat sandbox JWT (`scope="chat"`)."""
    from app.auth.jwt import create_access_token

    client = seeded_app["client"]
    connector_jwt = create_access_token("admin1", "admin@test.com", extra_claims={"scope": "mcp-oauth"})
    pending = _pending_for_new_client(client)

    r = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        headers={"Authorization": f"Bearer {connector_jwt}", "Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert r.status_code == 401, f"an agent-surface session JWT must not consent (got {r.status_code})"
    assert "code=" not in r.headers.get("location", "")


def test_consent_post_still_accepts_browser_cookie_session(seeded_app):
    """The counterpart the fix must NOT break: a real browser session — the
    `access_token` cookie set by the login providers, no Authorization header —
    still mints the authorization code."""
    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    pending = _pending_for_new_client(client)

    r = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        cookies={"access_token": admin_token},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert r.status_code in (302, 307), r.text
    assert "code=" in r.headers["location"], "the real browser consent flow must keep working"


def test_consent_page_describes_write_access_not_the_raw_scope(seeded_app):
    """The only scope Agnes issues is the coarse ``read``, but the connection
    exposes the write/delete MCP tools too. Listing the raw scope token made the
    consent screen claim read-only access the client immediately contradicted."""
    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    pending = _pending_for_new_client(client)

    r = client.get(
        "/api/mcp/oauth/consent",
        params={"pending": pending},
        headers={"Authorization": f"Bearer {admin_token}"},
        follow_redirects=False,
    )

    assert r.status_code == 200, r.text
    body = r.text
    assert "not read-only" in body, "consent must say the connection can write, not just 'read'"
    assert "create, update and delete" in body
    assert "your own Agnes permissions allow" in body
    assert "<li>read</li>" not in body, "the raw scope token must not be the whole story"


def test_consent_replay_after_allow_reports_success_not_expiry(seeded_app):
    """A consent link is single-use, but the tab that submitted it stays on the
    consent URL, so a reload, a double-clicked Allow, or Back-then-Allow re-issues
    it. That replay must explain the connection succeeded instead of reporting
    "Authorization request expired" on a connection that actually worked."""
    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    pending = _pending_for_new_client(client)

    first = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        headers={"Authorization": f"Bearer {admin_token}", "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert first.status_code in (302, 307), first.text

    replay = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        headers={"Authorization": f"Bearer {admin_token}", "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert replay.status_code == 200, f"a replayed consent must not read as an error (got {replay.status_code})"
    assert "Connected to Agnes" in replay.text
    assert "expired" not in replay.text.lower()

    reload_get = client.get(
        "/api/mcp/oauth/consent",
        params={"pending": pending},
        headers={"Authorization": f"Bearer {admin_token}"},
        follow_redirects=False,
    )
    assert reload_get.status_code == 200
    assert "Connected to Agnes" in reload_get.text


def test_consent_replay_after_deny_reports_denial(seeded_app):
    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    pending = _pending_for_new_client(client)

    first = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "deny"},
        headers={"Authorization": f"Bearer {admin_token}", "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert first.status_code in (302, 307)
    assert "error=access_denied" in first.headers["location"]

    replay = client.get(
        "/api/mcp/oauth/consent",
        params={"pending": pending},
        headers={"Authorization": f"Bearer {admin_token}"},
        follow_redirects=False,
    )
    assert replay.status_code == 200
    assert "Access denied" in replay.text


def test_unknown_pending_token_still_reports_an_invalid_link(seeded_app):
    """No outcome marker → the link really is unusable: keep the 4xx, but say
    what to do about it."""
    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]

    r = client.get(
        "/api/mcp/oauth/consent",
        params={"pending": "pending_never-existed"},
        headers={"Authorization": f"Bearer {admin_token}"},
        follow_redirects=False,
    )

    assert r.status_code == 400
    assert "no longer valid" in r.text
    assert "start the connection again" in r.text


def test_consent_outcome_marker_cannot_be_exchanged_for_a_token(seeded_app):
    """The replay marker is stored as an auth-code row, so prove it is inert as
    a grant by actually presenting it at ``/token`` — not merely by asserting
    the shape of the row this same module wrote."""
    from app.auth.mcp_oauth import _CONSENT_OUTCOME_PREFIX
    from src.repositories import oauth_clients_repo

    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]

    reg = _register_client(client)
    verifier, challenge = _pkce()
    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]
    client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        headers={"Authorization": f"Bearer {admin_token}", "Origin": "http://testserver"},
        follow_redirects=False,
    )

    marker = oauth_clients_repo().get_auth_code(_CONSENT_OUTCOME_PREFIX + pending)
    assert marker is not None, "the allow outcome must be remembered for the replay page"
    assert marker.get("subject") is None, "a subject on the marker would make it exchangeable for a token"
    assert marker.get("client_id") == ""

    # The part that matters: presenting the marker's key as an authorization
    # code must not yield a token.
    exchanged = client.post(
        f"{MCP_MOUNT}/token",
        data={
            "grant_type": "authorization_code",
            "code": _CONSENT_OUTCOME_PREFIX + pending,
            "redirect_uri": "http://localhost:9999/callback",
            "client_id": reg["client_id"],
            "client_secret": reg.get("client_secret", ""),
            "code_verifier": verifier,
        },
    )
    assert exchanged.status_code != 200, "the consent outcome marker was accepted as a grant"
    assert "access_token" not in exchanged.text


def test_consent_page_lists_an_unrecognised_scope_instead_of_dropping_it():
    """Agnes issues only ``read`` today. If that ever widens, an undescribed
    scope must still reach the page: silently omitting it is the exact bug this
    screen was fixed for."""
    from app.auth.mcp_oauth import _access_summary

    lines = _access_summary(["read", "admin:everything"])

    assert any("admin:everything" in line for line in lines), "an unrecognised scope must be shown, not swallowed"
    assert any("not read-only" in line for line in lines)


def test_consent_page_title_escapes_the_client_name_exactly_once(seeded_app):
    """``client_name`` is attacker-controlled and must be escaped — but escaping
    it twice showed users a literal "&amp;" in the browser tab."""
    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]

    reg = client.post(
        f"{MCP_MOUNT}/register",
        json={
            "client_name": "Ben & Jerry's",
            "redirect_uris": ["http://localhost:9999/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    ).json()
    _, challenge = _pkce()
    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]

    page = client.get(
        "/api/mcp/oauth/consent",
        params={"pending": pending},
        headers={"Authorization": f"Bearer {admin_token}"},
        follow_redirects=False,
    )

    assert page.status_code == 200, page.text
    assert "<title>Authorize Ben &amp; Jerry&#x27;s — Agnes</title>" in page.text
    assert "&amp;amp;" not in page.text, "the client name was escaped twice"


def test_consent_redirect_percent_encodes_the_client_state(seeded_app):
    """``state`` is opaque to us and echoed verbatim. Unencoded, an "&" in it
    splits into extra query parameters on the client's callback."""
    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    tricky_state = "a&code=injected b=c"

    reg = _register_client(client)
    _, challenge = _pkce()
    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": tricky_state,
            "scope": "read",
        },
        follow_redirects=False,
    )
    pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]

    allowed = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        headers={"Authorization": f"Bearer {admin_token}", "Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert allowed.status_code in (302, 307), allowed.text
    qs = parse_qs(urlparse(allowed.headers["location"]).query)
    assert qs["state"] == [tricky_state], "state must survive the round trip intact"
    assert len(qs["code"]) == 1, "the state must not be able to forge a second code parameter"


def test_provider_rejects_unknown_token(seeded_app):
    """A token that was never issued must not verify."""
    import asyncio

    from app.auth.mcp_oauth import AgnesMCPOAuthProvider

    provider = AgnesMCPOAuthProvider()
    assert asyncio.run(provider.load_access_token("not-a-real-token")) is None


# ---------------------------------------------------------------------------
# VS Code native MCP OAuth fixes (RFC 8252)
# ---------------------------------------------------------------------------


def test_discovery_metadata_includes_none_auth_method(seeded_app):
    """RFC 8252: VS Code is a public client (token_endpoint_auth_method=none).

    The root-level OAuth discovery document must include 'none' in
    token_endpoint_auth_methods_supported so VS Code does not skip
    Dynamic Client Registration and show the manual client-ID dialog.
    """
    client = seeded_app["client"]
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200, r.text
    meta = r.json()
    methods = meta.get("token_endpoint_auth_methods_supported", [])
    assert "none" in methods, f"'none' missing from token_endpoint_auth_methods_supported: {methods}"


def test_vscode_mcp_client_seeded_in_db(seeded_app):
    """The 'vscode-mcp' public OAuth client must be pre-seeded in the DB.

    VS Code users who see the manual registration dialog can enter
    'vscode-mcp' as the client ID without running dynamic registration.
    """
    import asyncio

    from app.auth.mcp_oauth import AgnesMCPOAuthProvider

    provider = AgnesMCPOAuthProvider()
    client = asyncio.run(provider.get_client("vscode-mcp"))
    assert client is not None, "vscode-mcp client must be pre-seeded"
    assert client.client_secret is None, "vscode-mcp must be a public client (no secret)"


def test_loopback_redirect_uri_different_port_accepted(seeded_app):
    """RFC 8252 §7.3: loopback redirect URI port must be ignored.

    VS Code uses http://127.0.0.1:<random-port>/callback.  Registering
    with one port and authorizing with a different port must succeed.
    """
    client = seeded_app["client"]

    # Register with the canonical vscode.dev redirect URI (as seeded)
    # but authorize with a random loopback port — simulates VS Code behaviour.
    r = client.post(
        f"{MCP_MOUNT}/register",
        json={
            "client_name": "VS Code Loopback Test",
            "redirect_uris": ["http://127.0.0.1:9100/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert r.status_code in (200, 201), r.text
    reg = r.json()
    client_id = reg["client_id"]

    verifier, challenge = _pkce()
    # Use a different port than was registered — this is the VS Code scenario.
    loopback_uri = "http://127.0.0.1:33418/callback"

    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": loopback_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "loopback_test",
            "scope": "read",
        },
        follow_redirects=False,
    )
    # The SDK validates redirect_uri against registered URIs before calling
    # authorize() — with RFC 8252 loopback matching the 400 turns into a 302.
    assert r.status_code in (302, 307), (
        f"loopback redirect_uri with different port must be accepted (got {r.status_code}): {r.text}"
    )


def test_create_app_boots_with_plain_http_server_url(seeded_app, monkeypatch):
    """A plain-HTTP deployment (tls_mode=none) that sets SERVER_URL to its
    public http:// address must still boot. The MCP SDK rejects a
    non-localhost http:// OAuth issuer (RFC 8414 requires HTTPS), so the
    streamable connector degrades — loud ERROR log, endpoint not mounted —
    instead of crashing create_app() in a boot loop."""
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    monkeypatch.setenv("SERVER_URL", "http://203.0.113.10")

    from app.main import create_app
    from starlette.testclient import TestClient

    app = create_app()  # must not raise
    assert app.state.mcp_streamable_instance is None

    client = TestClient(app, base_url="http://203.0.113.10")
    # The rest of the app is fully alive.
    r = client.get("/api/health")
    assert r.status_code == 200, r.text
    # Degraded mode: the streamable connector's OAuth discovery documents are
    # not advertised (they would point at endpoints that are not mounted).
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404


def test_create_app_keeps_streamable_mcp_on_localhost_http(seeded_app, monkeypatch):
    """The SDK explicitly allows an http://localhost issuer for local dev —
    the degradation guard must not over-trigger there."""
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    monkeypatch.setenv("SERVER_URL", "http://localhost:8000")

    from app.main import create_app

    app = create_app()
    assert app.state.mcp_streamable_instance is not None


def test_oauth_load_exposes_raw_not_hash_so_delete_and_revoke_work(seeded_app_fresh):
    """Double-hash guard (audit M4 / Devin #863).

    Codes/tokens are hashed at rest, but the provider's load_* methods must
    hand the SDK back the RAW value — the SDK passes it straight into
    delete_auth_code / revoke_refresh_token, which hash again. If load_*
    returned the stored digest, the follow-up would hash a hash
    (sha256(sha256(raw))) and match nothing — one-time-use + rotation would
    silently no-op. This drives the provider objects through that round-trip.
    """
    import asyncio
    import secrets
    import time

    from app.auth.mcp_oauth import AgnesMCPOAuthProvider
    from src.repositories import oauth_clients_repo

    with seeded_app_fresh["client"]:
        repo = oauth_clients_repo()
        prov = AgnesMCPOAuthProvider()
        repo.upsert_client(
            client_id="dh-int",
            client_secret="s",
            redirect_uris=["http://x/cb"],
            client_name="DH",
        )
        client = asyncio.run(prov.get_client("dh-int"))
        assert client is not None

        # Authorization code: load hands back the RAW code; delete then matches.
        raw_code = secrets.token_urlsafe(16)
        repo.save_auth_code(
            code=raw_code,
            client_id="dh-int",
            scopes=["read"],
            code_challenge="chal",
            redirect_uri="http://x/cb",
            redirect_uri_provided_explicitly=True,
            expires_at=time.time() + 300,
        )
        ac = asyncio.run(prov.load_authorization_code(client, raw_code))
        assert ac is not None and ac.code == raw_code, "load must expose the raw code, not the digest"
        repo.delete_auth_code(ac.code)  # SDK hands ac.code straight back
        assert repo.get_auth_code(raw_code) is None, "used code must be deleted (one-time-use)"

        # Refresh token: load hands back the RAW token; revoke then matches.
        raw_rt = secrets.token_urlsafe(16)
        repo.save_refresh_token(token=raw_rt, client_id="dh-int", scopes=["read"])
        rt = asyncio.run(prov.load_refresh_token(client, raw_rt))
        assert rt is not None and rt.token == raw_rt, "load must expose the raw refresh token, not the digest"
        repo.revoke_refresh_token(rt.token)
        assert asyncio.run(prov.load_refresh_token(client, raw_rt)) is None, "revoked refresh token must not load"


# ---------------------------------------------------------------------------
# RFC 7009 token revocation — public PKCE clients (Claude Code, VS Code, …)
# ---------------------------------------------------------------------------


def _authorize_and_mint(client, admin_token, reg, redirect_uri="http://localhost:9999/callback") -> dict:
    """Drive authorize → consent → token exchange for a registered client and
    return the token-endpoint JSON. A public client (no client_secret issued)
    exchanges without a ``client_secret`` field, exactly like a real RFC 8252
    native app."""
    verifier, challenge = _pkce()

    r = client.get(
        f"{MCP_MOUNT}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), r.text
    pending = parse_qs(urlparse(r.headers["location"]).query)["pending"][0]

    auth_hdr = {"Authorization": f"Bearer {admin_token}"}
    r = client.get(
        "/api/mcp/oauth/consent",
        params={"pending": pending},
        headers=auth_hdr,
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text

    r = client.post(
        "/api/mcp/oauth/consent",
        data={"pending": pending, "action": "allow"},
        headers={**auth_hdr, "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), r.text
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": reg["client_id"],
        "code_verifier": verifier,
    }
    if reg.get("client_secret"):
        data["client_secret"] = reg["client_secret"]
    r = client.post(f"{MCP_MOUNT}/token", data=data)
    assert r.status_code == 200, r.text
    return r.json()


def _initialize_call(client, access_token):
    return client.post(
        MCP_MOUNT,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "revoke-test", "version": "0"},
            },
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
        },
        follow_redirects=True,
    )


def test_registration_with_auth_method_none_issues_no_secret(seeded_app):
    """RFC 7591: a public client (token_endpoint_auth_method='none') must not
    be issued a client_secret."""
    reg = _register_client(seeded_app["client"], auth_method="none")
    assert not reg.get("client_secret"), reg
    assert reg.get("token_endpoint_auth_method") == "none"


def test_public_client_revokes_access_token(seeded_app_fresh):
    """RFC 7009: a public PKCE client (auth method 'none' — what Claude Code
    registers as) revokes its own access token with ``token=…&client_id=…``.

    The SDK's RevocationRequest declares ``client_secret: str | None`` WITHOUT
    a default — required-but-nullable in pydantic v2 — which 400s the request
    ("client_secret: Field required") before revocation runs; still broken
    upstream as of mcp 2.0.0, hence the patched route in mcp_streamable.py.
    After revocation the token must stop authenticating MCP JSON-RPC calls.

    Uses ``seeded_app_fresh`` (own ``create_app()``), not the shared
    ``seeded_app`` — this drives a live JSON-RPC call, which needs the
    streamable MCP session manager's lifespan to actually run, and the SDK
    only allows that once per instance (see tests/conftest.py).
    """
    import asyncio

    from app.auth.mcp_oauth import AgnesMCPOAuthProvider

    admin_token = seeded_app_fresh["admin_token"]
    with seeded_app_fresh["client"] as client:
        reg = _register_client(client, auth_method="none")
        tok = _authorize_and_mint(client, admin_token, reg)
        access_token = tok["access_token"]

        # Sanity: the token drives an authenticated JSON-RPC call.
        r = _initialize_call(client, access_token)
        assert r.status_code == 200, r.text

        r = client.post(
            f"{MCP_MOUNT}/revoke",
            data={"token": access_token, "client_id": reg["client_id"]},
        )
        assert r.status_code == 200, f"public-client revocation must succeed: {r.status_code} {r.text}"

        # The provider no longer verifies the token…
        assert asyncio.run(AgnesMCPOAuthProvider().load_access_token(access_token)) is None

        # …and the MCP endpoint rejects it.
        r = _initialize_call(client, access_token)
        assert r.status_code == 401, f"revoked token must not authenticate: {r.status_code}"


def test_public_client_revocation_accepts_empty_client_secret(seeded_app_fresh):
    """Some clients post ``client_secret=`` (empty) rather than omitting it.

    The lenient model fixes the *absent* key; this pins the empty-string
    case, which the SDK's ``ClientAuthenticator`` handles by forcing
    ``request_client_secret = None`` for ``token_endpoint_auth_method="none"``
    and only comparing when the stored client actually has a secret — a
    public client has none, so the empty value never reaches a comparison.
    Pinned because "" vs absent is a real client-behavior difference and the
    existing token-flow test already posts it against /token.

    Uses ``seeded_app_fresh`` for the same reason as
    ``test_public_client_revokes_access_token`` above — see its docstring.
    """
    import asyncio

    from app.auth.mcp_oauth import AgnesMCPOAuthProvider

    admin_token = seeded_app_fresh["admin_token"]
    with seeded_app_fresh["client"] as client:
        reg = _register_client(client, auth_method="none")
        tok = _authorize_and_mint(client, admin_token, reg)
        access_token = tok["access_token"]

        r = client.post(
            f"{MCP_MOUNT}/revoke",
            data={"token": access_token, "client_id": reg["client_id"], "client_secret": ""},
        )
        assert r.status_code == 200, f"empty client_secret must not 401 a public client: {r.status_code} {r.text}"
        assert asyncio.run(AgnesMCPOAuthProvider().load_access_token(access_token)) is None


def test_public_client_revokes_refresh_token(seeded_app):
    """RFC 7009 with token_type_hint=refresh_token: the grant can no longer be
    renewed after revocation."""
    import asyncio

    from app.auth.mcp_oauth import AgnesMCPOAuthProvider

    client = seeded_app["client"]
    reg = _register_client(client, auth_method="none")
    tok = _authorize_and_mint(client, seeded_app["admin_token"], reg)
    refresh_token = tok["refresh_token"]

    r = client.post(
        f"{MCP_MOUNT}/revoke",
        data={
            "token": refresh_token,
            "token_type_hint": "refresh_token",
            "client_id": reg["client_id"],
        },
    )
    assert r.status_code == 200, f"public-client refresh revocation must succeed: {r.status_code} {r.text}"

    provider = AgnesMCPOAuthProvider()
    sdk_client = asyncio.run(provider.get_client(reg["client_id"]))
    assert asyncio.run(provider.load_refresh_token(sdk_client, refresh_token)) is None


def test_confidential_client_revocation_requires_secret(seeded_app):
    """The lenient revocation request model must NOT weaken confidential
    clients: with a stored secret, revocation without (or with a wrong)
    client_secret is rejected by client authentication and the token stays
    live; with the correct secret it succeeds."""
    import asyncio

    from app.auth.mcp_oauth import AgnesMCPOAuthProvider

    client = seeded_app["client"]
    reg = _register_client(client)  # client_secret_post
    assert reg.get("client_secret")
    tok = _authorize_and_mint(client, seeded_app["admin_token"], reg)
    access_token = tok["access_token"]

    # Missing secret → rejected, token still verifies.
    r = client.post(f"{MCP_MOUNT}/revoke", data={"token": access_token, "client_id": reg["client_id"]})
    assert r.status_code == 401, r.text
    assert asyncio.run(AgnesMCPOAuthProvider().load_access_token(access_token)) is not None

    # Wrong secret → rejected, token still verifies.
    r = client.post(
        f"{MCP_MOUNT}/revoke",
        data={"token": access_token, "client_id": reg["client_id"], "client_secret": "wrong"},
    )
    assert r.status_code == 401, r.text
    assert asyncio.run(AgnesMCPOAuthProvider().load_access_token(access_token)) is not None

    # Correct secret → revoked.
    r = client.post(
        f"{MCP_MOUNT}/revoke",
        data={"token": access_token, "client_id": reg["client_id"], "client_secret": reg["client_secret"]},
    )
    assert r.status_code == 200, r.text
    assert asyncio.run(AgnesMCPOAuthProvider().load_access_token(access_token)) is None


def test_discovery_advertises_public_client_revocation(seeded_app):
    """Both discovery documents must advertise 'none' in
    revocation_endpoint_auth_methods_supported — /revoke accepts public
    clients, and strict clients consult this list before calling it."""
    client = seeded_app["client"]
    for path in (
        "/.well-known/oauth-authorization-server",
        f"{MCP_MOUNT}/.well-known/oauth-authorization-server",
    ):
        r = client.get(path)
        assert r.status_code == 200, r.text
        meta = r.json()
        methods = meta.get("revocation_endpoint_auth_methods_supported") or []
        assert "none" in methods, f"{path}: {methods}"


def test_non_numeric_expires_in_does_not_break_the_refresh():
    """RFC 6749 says expires_in is a number; real servers ship strings.

    The raw value reached `timedelta(seconds=...)` AFTER the refresh had
    already rotated the tokens, and the TypeError escaped the
    OAuthTokenError boundary — so the rotated refresh token was lost and
    the connection broke permanently rather than for one call.
    """
    from connectors.mcp.oauth_client import _coerce_expires_in

    assert _coerce_expires_in(3600) == 3600
    assert _coerce_expires_in("3600") == 3600
    assert _coerce_expires_in("3600.0") == 3600
    assert _coerce_expires_in(0) == 0
    # unusable values mean "no known expiry", never a crash
    for bad in (None, "", "abc", True, [], {}):
        assert _coerce_expires_in(bad) is None, bad
