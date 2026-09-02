"""Unit tests for ``connectors.mcp.oauth_client`` (2026-07-30 outbound MCP
OAuth sources spec, PR 1 — no network; every HTTP call rides an
``httpx.MockTransport``).

Covers:
* RFC 9728 protected-resource discovery — primary well-known hop + the
  401 ``WWW-Authenticate: resource_metadata=`` fallback.
* RFC 8414 authorization-server metadata discovery.
* PKCE S256 fail-closed (never downgrades to plain / no PKCE).
* RFC 7591 dynamic client registration (+ best-effort deregistration).
* Token exchange / refresh, including the AS-error → ``OAuthTokenError``
  path and the ``invalid_grant`` classifier.
* Mix-up defense: exchange/refresh only ever use the caller-supplied
  ``token_endpoint``/``client_id``/``client_secret`` — never anything
  echoed back by the AS in its own response body.

This repo runs async tests via plain ``asyncio.run(...)`` inside a sync
``def test_*`` (no ``pytest-asyncio`` plugin — see
``tests/test_agent_sse_contract.py``), not ``@pytest.mark.asyncio``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from connectors.mcp.oauth_client import (
    OAuthDiscoveryError,
    OAuthTokenError,
    best_effort_revoke_registration,
    build_oauth_http_client,
    discover_as_metadata,
    discover_protected_resource_metadata,
    exchange_code_for_token,
    generate_pkce_pair,
    is_invalid_grant_error,
    refresh_access_token,
    register_dynamic_client,
    require_https_endpoints,
    require_pkce_s256,
    resolve_issuer,
)
from src.net.ssrf_safe_client import SSRFGuardAsyncTransport


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# build_oauth_http_client
# ---------------------------------------------------------------------------


def test_build_oauth_http_client_uses_ssrf_guard_transport():
    client = build_oauth_http_client()
    assert isinstance(client._transport, SSRFGuardAsyncTransport)
    assert client._transport._https_only is True


# ---------------------------------------------------------------------------
# RFC 9728 protected-resource discovery
# ---------------------------------------------------------------------------


def test_discover_protected_resource_primary_hop_is_rfc9728_path_insertion():
    """RFC 9728 §3.1: the well-known segment goes between the authority and
    the resource path — that form is tried FIRST (Devin Review on #1124)."""

    def handler(request):
        assert str(request.url) == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
        return httpx.Response(
            200, json={"resource": "https://mcp.example.com", "authorization_servers": ["https://as.example.com"]}
        )

    async def _impl():
        async with _client(handler) as client:
            return await discover_protected_resource_metadata("https://mcp.example.com/mcp", client=client)

    meta = run(_impl())
    assert meta["authorization_servers"] == ["https://as.example.com"]


def test_discover_protected_resource_falls_back_to_suffix_form():
    """Servers publishing only the lenient suffix form ({url}/.well-known/…)
    still discover — second candidate after the RFC 9728 location."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if str(request.url) == "https://mcp.example.com/mcp/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        return httpx.Response(404)

    async def _impl():
        async with _client(handler) as client:
            return await discover_protected_resource_metadata("https://mcp.example.com/mcp", client=client)

    meta = run(_impl())
    assert meta["authorization_servers"] == ["https://as.example.com"]
    assert calls[0] == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
    assert calls[1] == "https://mcp.example.com/mcp/.well-known/oauth-protected-resource"


def test_junk_json_at_the_advertised_metadata_url_is_translated():
    """A 200 with a non-JSON body must raise OAuthDiscoveryError, not a raw
    ValueError that would surface as a 500 (Devin Review on #1124).

    Asserted at the URL the server ADVERTISED via its 401 challenge: there a
    junk body is the actionable answer and there is nothing left to try. On a
    probed well-known path it means "not the document" instead — see
    test_a_catch_all_html_host_still_reaches_the_401_fallback.
    """

    def handler(request):
        if str(request.url) == "https://mcp.example.com/mcp":
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/rm"'},
            )
        if str(request.url) == "https://mcp.example.com/.well-known/rm":
            return httpx.Response(200, text="<html>not json</html>")
        return httpx.Response(404)

    async def _impl():
        async with _client(handler) as client:
            return await discover_protected_resource_metadata("https://mcp.example.com/mcp", client=client)

    with pytest.raises(OAuthDiscoveryError, match="not valid JSON"):
        run(_impl())


def test_a_catch_all_html_host_still_reaches_the_401_fallback():
    """A host that answers unknown paths with a catch-all 200 HTML page (SPA,
    edge proxy) used to abort discovery at the first probed well-known URL: the
    junk body raised out of the candidate loop, so the second candidate AND the
    401-challenge fallback were never tried. That shape is precisely the one
    the design spec names as the observed real-world case, so the servers most
    likely to need the fallback were the ones that never reached it
    (Devin Review on #1124).
    """
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if str(request.url) == "https://mcp.example.com/mcp":
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/rm"'},
            )
        if str(request.url) == "https://mcp.example.com/.well-known/rm":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        # Every other path: the catch-all landing page.
        return httpx.Response(200, text="<!doctype html><title>Our API</title>")

    async def _impl():
        async with _client(handler) as client:
            return await discover_protected_resource_metadata("https://mcp.example.com/mcp", client=client)

    meta = run(_impl())
    assert meta["authorization_servers"] == ["https://as.example.com"]
    # Both well-known candidates were probed and rejected before the fallback.
    assert sum(".well-known/oauth-protected-resource" in c for c in calls) == 2


def test_discover_protected_resource_falls_back_to_401_challenge():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if (
            ".well-known" in request.url.path
            and str(request.url) != "https://mcp.example.com/.well-known/oauth-protected-resource/custom"
        ):
            return httpx.Response(404)
        if str(request.url) == "https://mcp.example.com/mcp":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/custom"'
                    )
                },
            )
        return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})

    async def _impl():
        async with _client(handler) as client:
            return await discover_protected_resource_metadata("https://mcp.example.com/mcp", client=client)

    meta = run(_impl())
    assert meta["authorization_servers"] == ["https://as.example.com"]
    assert any("custom" in c for c in calls)


def test_discover_protected_resource_no_metadata_no_challenge_raises():
    def handler(request):
        return httpx.Response(404)

    async def _impl():
        async with _client(handler) as client:
            await discover_protected_resource_metadata("https://mcp.example.com/mcp", client=client)

    with pytest.raises(OAuthDiscoveryError):
        run(_impl())


def test_resolve_issuer_picks_first_authorization_server():
    assert (
        resolve_issuer({"authorization_servers": ["https://as1.example.com", "https://as2.example.com"]})
        == "https://as1.example.com"
    )


def test_resolve_issuer_raises_when_missing():
    with pytest.raises(OAuthDiscoveryError):
        resolve_issuer({})


# ---------------------------------------------------------------------------
# RFC 8414 authorization-server metadata discovery
# ---------------------------------------------------------------------------


_AS_METADATA = {
    "issuer": "https://as.example.com",
    "authorization_endpoint": "https://as.example.com/authorize",
    "token_endpoint": "https://as.example.com/token",
    "registration_endpoint": "https://as.example.com/register",
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
}


def test_discover_as_metadata_well_known_path():
    def handler(request):
        assert str(request.url) == "https://as.example.com/.well-known/oauth-authorization-server"
        return httpx.Response(200, json=_AS_METADATA)

    async def _impl():
        async with _client(handler) as client:
            return await discover_as_metadata("https://as.example.com", client=client)

    assert run(_impl()) == _AS_METADATA


def test_discover_as_metadata_non_200_raises():
    def handler(request):
        return httpx.Response(500)

    async def _impl():
        async with _client(handler) as client:
            await discover_as_metadata("https://as.example.com", client=client)

    with pytest.raises(OAuthDiscoveryError):
        run(_impl())


# ---------------------------------------------------------------------------
# PKCE fail-closed
# ---------------------------------------------------------------------------


def test_require_pkce_s256_accepts_s256():
    require_pkce_s256({"code_challenge_methods_supported": ["plain", "S256"]})  # no raise


def test_require_pkce_s256_rejects_missing_s256():
    with pytest.raises(OAuthDiscoveryError, match="S256"):
        require_pkce_s256({"code_challenge_methods_supported": ["plain"]})


def test_require_pkce_s256_rejects_absent_field():
    with pytest.raises(OAuthDiscoveryError):
        require_pkce_s256({})


def test_generate_pkce_pair_is_s256():
    from authlib.oauth2.rfc7636 import create_s256_code_challenge

    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == create_s256_code_challenge(verifier)


def test_require_https_endpoints_rejects_http():
    with pytest.raises(OAuthDiscoveryError, match="not https"):
        require_https_endpoints({**_AS_METADATA, "token_endpoint": "http://as.example.com/token"})


# ---------------------------------------------------------------------------
# RFC 7591 dynamic client registration
# ---------------------------------------------------------------------------


def test_register_dynamic_client_success():
    def handler(request):
        assert request.url == "https://as.example.com/register"
        payload = json.loads(request.read())
        assert payload["redirect_uris"] == ["https://agnes.example.com/api/mcp/oauth-client/callback"]
        assert payload["token_endpoint_auth_method"] == "client_secret_basic"
        return httpx.Response(
            201,
            json={
                "client_id": "abc123",
                "client_secret": "s3cr3t",
                "registration_access_token": "rat-token",
            },
        )

    async def _impl():
        async with _client(handler) as client:
            return await register_dynamic_client(
                _AS_METADATA,
                redirect_uri="https://agnes.example.com/api/mcp/oauth-client/callback",
                client=client,
            )

    result = run(_impl())
    assert result.client_id == "abc123"
    assert result.client_secret == "s3cr3t"
    assert result.registration_access_token == "rat-token"
    assert result.authorization_endpoint == _AS_METADATA["authorization_endpoint"]
    assert result.token_endpoint == _AS_METADATA["token_endpoint"]


def test_register_dynamic_client_no_registration_endpoint_raises():
    meta = {**_AS_METADATA}
    meta.pop("registration_endpoint")

    async def _unused(request):
        raise AssertionError("should not make an HTTP call")

    async def _impl():
        async with _client(_unused) as client:
            await register_dynamic_client(meta, redirect_uri="https://agnes.example.com/cb", client=client)

    with pytest.raises(OAuthDiscoveryError, match="registration_endpoint"):
        run(_impl())


def test_register_dynamic_client_missing_client_id_raises():
    def handler(request):
        return httpx.Response(201, json={})

    async def _impl():
        async with _client(handler) as client:
            await register_dynamic_client(_AS_METADATA, redirect_uri="https://agnes.example.com/cb", client=client)

    with pytest.raises(OAuthDiscoveryError, match="client_id"):
        run(_impl())


def test_best_effort_revoke_registration_swallows_errors():
    def handler(request):
        raise httpx.ConnectError("boom")

    async def _impl():
        async with _client(handler) as client:
            # Must not raise.
            await best_effort_revoke_registration(
                registration_endpoint="https://as.example.com/register",
                client_id="old-client",
                registration_access_token="old-rat",
                client=client,
            )

    run(_impl())


def test_best_effort_revoke_registration_no_op_without_prerequisites():
    async def _unused(request):
        raise AssertionError("should not make an HTTP call")

    async def _impl():
        async with _client(_unused) as client:
            await best_effort_revoke_registration(
                registration_endpoint=None,
                client_id="old-client",
                registration_access_token=None,
                client=client,
            )

    run(_impl())


# ---------------------------------------------------------------------------
# Token exchange / refresh
# ---------------------------------------------------------------------------


def test_exchange_code_for_token_success_with_basic_auth():
    def handler(request):
        assert request.url == "https://as.example.com/token"
        # httpx encodes basic auth in the Authorization header.
        assert request.headers.get("Authorization", "").startswith("Basic ")
        return httpx.Response(
            200, json={"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600, "scope": "read"}
        )

    async def _impl():
        async with _client(handler) as client:
            return await exchange_code_for_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret="csecret",
                code="authcode",
                redirect_uri="https://agnes.example.com/cb",
                code_verifier="verifier",
                client=client,
            )

    tok = run(_impl())
    assert tok.access_token == "at1"
    assert tok.refresh_token == "rt1"
    assert tok.expires_in == 3600
    assert tok.scopes == "read"


def test_exchange_code_for_token_public_client_no_basic_auth():
    def handler(request):
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"access_token": "at1"})

    async def _impl():
        async with _client(handler) as client:
            return await exchange_code_for_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret=None,
                code="authcode",
                redirect_uri="https://agnes.example.com/cb",
                code_verifier="verifier",
                client=client,
            )

    tok = run(_impl())
    assert tok.access_token == "at1"


def test_exchange_code_for_token_never_follows_redirects():
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://attacker.example/steal"})

    async def _impl():
        async with _client(handler) as client:
            await exchange_code_for_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret=None,
                code="authcode",
                redirect_uri="https://agnes.example.com/cb",
                code_verifier="verifier",
                client=client,
            )

    with pytest.raises(OAuthTokenError):
        run(_impl())


def test_exchange_code_for_token_as_error_is_flattened():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "code expired"})

    async def _impl():
        async with _client(handler) as client:
            await exchange_code_for_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret=None,
                code="authcode",
                redirect_uri="https://agnes.example.com/cb",
                code_verifier="verifier",
                client=client,
            )

    with pytest.raises(OAuthTokenError) as excinfo:
        run(_impl())
    assert "invalid_grant" in str(excinfo.value)
    assert is_invalid_grant_error(excinfo.value)


def test_refresh_access_token_success():
    def handler(request):
        body = request.read().decode()
        assert "grant_type=refresh_token" in body
        assert "refresh_token=old-rt" in body
        return httpx.Response(200, json={"access_token": "at2", "refresh_token": "rt2", "expires_in": 60})

    async def _impl():
        async with _client(handler) as client:
            return await refresh_access_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret="csecret",
                refresh_token="old-rt",
                client=client,
            )

    tok = run(_impl())
    assert tok.access_token == "at2"
    assert tok.refresh_token == "rt2"


def test_refresh_access_token_invalid_grant_is_classified():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    async def _impl():
        async with _client(handler) as client:
            await refresh_access_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret=None,
                refresh_token="dead-rt",
                client=client,
            )

    with pytest.raises(OAuthTokenError) as excinfo:
        run(_impl())
    assert is_invalid_grant_error(excinfo.value)


def test_is_invalid_grant_error_false_for_other_errors():
    assert not is_invalid_grant_error(OAuthTokenError("server_error: boom"))
    assert not is_invalid_grant_error(ValueError("invalid_grant"))


# ---------------------------------------------------------------------------
# Mix-up defense: no function accepts a token endpoint / client id from an
# AS-controlled response body — only from explicit caller kwargs.
# ---------------------------------------------------------------------------


def test_exchange_ignores_any_endpoint_hint_in_as_response_body():
    """Even if a (malicious) AS response body carries fields that LOOK like
    endpoint overrides, exchange_code_for_token has no code path that reads
    them — it only ever POSTs to the caller-supplied ``token_endpoint``."""
    seen_urls = []

    def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "access_token": "at1",
                "token_endpoint": "https://attacker.example/token",
                "client_id": "attacker-client",
            },
        )

    async def _impl():
        async with _client(handler) as client:
            return await exchange_code_for_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret=None,
                code="authcode",
                redirect_uri="https://agnes.example.com/cb",
                code_verifier="verifier",
                client=client,
            )

    tok = run(_impl())
    assert seen_urls == ["https://as.example.com/token"]
    assert tok.access_token == "at1"


def test_as_metadata_non_object_json_is_translated():
    """Valid JSON that is not an object (e.g. a bare list) must raise
    OAuthDiscoveryError, not crash downstream (Devin Review on #1124)."""

    def handler(request):
        return httpx.Response(200, json=["not", "an", "object"])

    async def _impl():
        async with _client(handler) as client:
            return await discover_as_metadata("https://as.example.com", client=client)

    with pytest.raises(OAuthDiscoveryError, match="not a JSON object"):
        run(_impl())


def test_as_metadata_missing_issuer_is_rejected():
    def handler(request):
        return httpx.Response(200, json={"authorization_endpoint": "https://as.example.com/authorize"})

    async def _impl():
        async with _client(handler) as client:
            return await discover_as_metadata("https://as.example.com", client=client)

    with pytest.raises(OAuthDiscoveryError, match="carries no 'issuer'"):
        run(_impl())


def test_as_metadata_issuer_mismatch_is_rejected():
    def handler(request):
        return httpx.Response(200, json={"issuer": "https://evil.example.net"})

    async def _impl():
        async with _client(handler) as client:
            return await discover_as_metadata("https://as.example.com", client=client)

    with pytest.raises(OAuthDiscoveryError, match="issuer mismatch"):
        run(_impl())


def test_as_metadata_issuer_trailing_slash_is_tolerated():
    def handler(request):
        return httpx.Response(200, json={"issuer": "https://as.example.com/"})

    async def _impl():
        async with _client(handler) as client:
            return await discover_as_metadata("https://as.example.com", client=client)

    meta = run(_impl())
    assert meta["issuer"] == "https://as.example.com/"


def test_registration_fails_closed_on_a_client_auth_method_the_token_calls_cannot_send():
    """An AS supporting only a method the token calls never use must be
    refused at registration — announcing it would get every exchange and
    refresh rejected (Devin Review on #1124).

    ``client_secret_post`` used to be such a method and no longer is; the
    fail-closed contract itself still holds for the rest.
    """
    meta = dict(_AS_METADATA)
    meta["token_endpoint_auth_methods_supported"] = ["private_key_jwt"]

    async def _impl():
        async with _client(lambda request: httpx.Response(500)) as client:
            return await register_dynamic_client(meta, redirect_uri="https://agnes.example.com/cb", client=client)

    with pytest.raises(OAuthDiscoveryError, match="private_key_jwt"):
        run(_impl())


def test_async_ssrf_transport_does_not_block_the_event_loop():
    """resolve_safe() calls a blocking socket.getaddrinfo(); the async
    transport must off-load it, or one slow upstream DNS stalls every other
    concurrent request in the process (architecture review on #1124)."""
    import asyncio
    import threading

    import httpx

    from src.net import ssrf_safe_client

    loop_thread_id = {}
    resolve_thread_id = {}

    def _fake_resolve(url, *, https_only=False):
        resolve_thread_id["id"] = threading.get_ident()
        return True, "", "203.0.113.10"

    async def _fake_parent(self, request):
        # Stand in for the real connection so the test never touches the
        # network — only the resolve step is under test here.
        return httpx.Response(200, request=request)

    async def _drive():
        loop_thread_id["id"] = threading.get_ident()
        transport = ssrf_safe_client.SSRFGuardAsyncTransport()
        resp = await transport.handle_async_request(httpx.Request("GET", "https://example.com/x"))
        assert resp.status_code == 200

    monkey = pytest.MonkeyPatch()
    monkey.setattr(ssrf_safe_client, "resolve_safe", _fake_resolve)
    monkey.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_parent)
    try:
        asyncio.run(_drive())
    finally:
        monkey.undo()

    assert resolve_thread_id.get("id") is not None, "resolve_safe was never reached"
    assert resolve_thread_id["id"] != loop_thread_id["id"], "resolve_safe ran on the event loop thread"


def test_register_rejects_response_granting_an_unperformable_auth_method():
    """RFC 7591 §3.2.1 lets the AS register a different method than the one
    requested, and its response is authoritative — the metadata-side check
    cannot see this case (Devin Review on #1124)."""

    def handler(request):
        return httpx.Response(
            201,
            json={
                "client_id": "abc123",
                "client_secret": "s3cr3t",
                "token_endpoint_auth_method": "private_key_jwt",
            },
        )

    async def _impl():
        async with _client(handler) as client:
            await register_dynamic_client(
                _AS_METADATA,
                redirect_uri="https://agnes.example.com/api/mcp/oauth-client/callback",
                client=client,
            )

    with pytest.raises(OAuthDiscoveryError, match="private_key_jwt"):
        run(_impl())


def test_a_json_envelope_that_is_not_metadata_also_falls_through():
    """The other half of the same class: a probed well-known URL answering 200
    with a JSON *object* that is not RFC 9728 metadata — the `{"error": ...}`
    envelope API gateways return for unknown paths — used to be accepted as the
    document, skipping the remaining candidate and the 401 fallback. The admin
    then saw resolve_issuer's "carries no 'authorization_servers'", which points
    at the document rather than at the missing discovery (Devin Review on
    #1124)."""

    def handler(request):
        if str(request.url) == "https://mcp.example.com/mcp":
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/rm"'},
            )
        if str(request.url) == "https://mcp.example.com/.well-known/rm":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        return httpx.Response(200, json={"error": "not_found"})

    async def _impl():
        async with _client(handler) as client:
            return await discover_protected_resource_metadata("https://mcp.example.com/mcp", client=client)

    meta = run(_impl())
    assert meta["authorization_servers"] == ["https://as.example.com"]


def _dcr(body, *, status=201, scopes=None, meta=None):
    def handler(request):
        return httpx.Response(status, json=body)

    async def _impl():
        async with _client(handler) as client:
            return await register_dynamic_client(
                meta or _AS_METADATA,
                redirect_uri="https://agnes.example.com/cb",
                client=client,
                scopes=scopes,
            )

    return run(_impl())


def test_a_confidential_registration_without_a_secret_is_refused():
    """`_post_token_request` keys off secret PRESENCE, so a client the AS
    recorded as `client_secret_basic` but issued no secret for would send no
    client authentication at all — every exchange and refresh coming back
    `invalid_client`. Fail at registration, where the message can say what to
    do, instead of at every token call with no explanation (Devin Review on
    #1124)."""
    with pytest.raises(OAuthDiscoveryError, match="issued no client_secret"):
        _dcr({"client_id": "abc", "token_endpoint_auth_method": "client_secret_basic"})

    # Omitting the field entirely means the RFC 7591 default, which IS
    # client_secret_basic — same broken shape, same refusal.
    with pytest.raises(OAuthDiscoveryError, match="issued no client_secret"):
        _dcr({"client_id": "abc"})


def test_a_public_registration_without_a_secret_is_fine():
    """The counterpart: an AS that explicitly registers the client as `none`
    is a correct public client and must not be refused."""
    result = _dcr({"client_id": "abc", "token_endpoint_auth_method": "none"})
    assert result.client_id == "abc"
    assert result.client_secret is None


def test_the_scope_the_as_granted_wins_over_the_one_requested():
    """RFC 7591 §3.2.1 lets the AS register a different `scope` than asked for,
    and its answer is authoritative. Storing the requested value would put a
    scope the client does not hold into the authorize URL, where the AS answers
    `invalid_scope` (Devin Review on #1124)."""
    result = _dcr(
        {"client_id": "abc", "client_secret": "s", "scope": "read"},
        scopes="read write admin",
    )
    assert result.scopes == "read"

    # No `scope` in the response: the requested value stands.
    assert _dcr({"client_id": "abc", "client_secret": "s"}, scopes="read write").scopes == "read write"


def test_a_public_registration_that_returns_a_secret_drops_it(caplog):
    """Mirror of the refusal above, and the reason both matter: token calls
    select client auth by secret PRESENCE, not by the registered method. A
    stray secret on a registration the AS recorded as public would send HTTP
    Basic to a public client — `invalid_client`, with nothing pointing at why.

    Dropped rather than refused, because the registration is perfectly usable
    as what the AS says it is; logged, because silently discarding credential
    material is its own trap (Devin Review on #1124).
    """
    with caplog.at_level("WARNING"):
        result = _dcr(
            {"client_id": "abc", "token_endpoint_auth_method": "none", "client_secret": "stray"},
        )
    assert result.client_secret is None
    assert "discarding" in caplog.text


def test_secret_presence_agrees_with_the_registered_method():
    """The invariant the two guards establish together: after registration, a
    stored secret exists iff the AS registered the client as confidential — so
    selecting client auth on presence alone is correct by construction."""
    assert _dcr({"client_id": "a", "token_endpoint_auth_method": "none"}).client_secret is None
    assert _dcr({"client_id": "a", "token_endpoint_auth_method": "none", "client_secret": "x"}).client_secret is None
    assert (
        _dcr(
            {"client_id": "a", "token_endpoint_auth_method": "client_secret_basic", "client_secret": "x"}
        ).client_secret
        == "x"
    )
    with pytest.raises(OAuthDiscoveryError):
        _dcr({"client_id": "a", "token_endpoint_auth_method": "client_secret_basic"})


# ---------------------------------------------------------------------------
# client_secret_post — the fallback for an AS that refuses HTTP Basic
# ---------------------------------------------------------------------------
#
# RFC 6749 §2.3.1 lets a confidential client present its secret either as
# HTTP Basic or as body parameters, and says the AS decides which it takes.
# Agnes leads with Basic (the method §2.3.1 requires every AS to support)
# and falls back to `client_secret_post` exactly once, on the AS's own
# `invalid_client` answer — so an AS advertising only `client_secret_post`
# works without a stored per-client auth-method setting.


def _form(request) -> dict:
    return dict(httpx.QueryParams(request.read().decode()))


def test_exchange_code_for_token_retries_with_client_secret_post_when_basic_is_rejected():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            assert request.headers.get("Authorization", "").startswith("Basic ")
            assert "client_secret" not in _form(request)
            return httpx.Response(401, json={"error": "invalid_client"})
        assert "Authorization" not in request.headers
        assert _form(request)["client_secret"] == "csecret"
        assert _form(request)["client_id"] == "cid"
        return httpx.Response(200, json={"access_token": "at1"})

    async def _impl():
        async with _client(handler) as client:
            return await exchange_code_for_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret="csecret",
                code="authcode",
                redirect_uri="https://agnes.example.com/cb",
                code_verifier="verifier",
                client=client,
            )

    tok = run(_impl())
    assert tok.access_token == "at1"
    assert len(seen) == 2


def test_refresh_access_token_retries_with_client_secret_post_when_basic_is_rejected():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(400, json={"error": "invalid_client"})
        assert _form(request)["client_secret"] == "csecret"
        return httpx.Response(200, json={"access_token": "at2", "refresh_token": "rt2"})

    async def _impl():
        async with _client(handler) as client:
            return await refresh_access_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret="csecret",
                refresh_token="rt1",
                client=client,
            )

    tok = run(_impl())
    assert tok.access_token == "at2"
    assert len(seen) == 2


def test_exchange_code_for_token_does_not_retry_a_failure_that_is_not_client_auth():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(400, json={"error": "invalid_grant"})

    async def _impl():
        async with _client(handler) as client:
            await exchange_code_for_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret="csecret",
                code="authcode",
                redirect_uri="https://agnes.example.com/cb",
                code_verifier="verifier",
                client=client,
            )

    with pytest.raises(OAuthTokenError, match="invalid_grant"):
        run(_impl())
    assert len(seen) == 1


def test_public_client_never_retries_because_it_has_no_secret_to_post():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(401, json={"error": "invalid_client"})

    async def _impl():
        async with _client(handler) as client:
            await exchange_code_for_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret=None,
                code="authcode",
                redirect_uri="https://agnes.example.com/cb",
                code_verifier="verifier",
                client=client,
            )

    with pytest.raises(OAuthTokenError, match="invalid_client"):
        run(_impl())
    assert len(seen) == 1


def test_register_dynamic_client_announces_client_secret_post_when_basic_is_unsupported():
    meta = {**_AS_METADATA, "token_endpoint_auth_methods_supported": ["client_secret_post"]}

    def handler(request):
        assert json.loads(request.read())["token_endpoint_auth_method"] == "client_secret_post"
        return httpx.Response(201, json={"client_id": "abc123", "client_secret": "s3cr3t"})

    async def _impl():
        async with _client(handler) as client:
            return await register_dynamic_client(meta, redirect_uri="https://agnes.example.com/cb", client=client)

    assert run(_impl()).client_id == "abc123"


def test_register_dynamic_client_accepts_a_granted_client_secret_post():
    meta = {**_AS_METADATA, "token_endpoint_auth_methods_supported": ["client_secret_post"]}

    def handler(request):
        return httpx.Response(
            201,
            json={
                "client_id": "abc123",
                "client_secret": "s3cr3t",
                "token_endpoint_auth_method": "client_secret_post",
            },
        )

    async def _impl():
        async with _client(handler) as client:
            return await register_dynamic_client(meta, redirect_uri="https://agnes.example.com/cb", client=client)

    assert run(_impl()).client_secret == "s3cr3t"


def test_a_token_error_never_carries_the_client_secret_back_into_the_log():
    """The AS's own words go into the raised error, which reaches the logs and
    an admin-facing message.

    That was safe while the secret only ever travelled in the `Authorization`
    header — no response body could contain it. `client_secret_post` puts it
    in the POST body, and a server that echoes the submitted parameters into
    its error page (plenty do) would hand the secret straight back to us to
    log. Both detail paths are scrubbed: the JSON `error_description` and the
    raw-text fallback.
    """
    secret = "s3cr3t-do-not-log"

    def echoing_handler(request):
        # First answer rejects Basic so the retry puts the secret in the body;
        # the second echoes that body back, the way a chatty AS error page does.
        if request.headers.get("Authorization", "").startswith("Basic "):
            return httpx.Response(401, json={"error": "invalid_client"})
        return httpx.Response(400, text=f"Bad request: {request.content.decode()}")

    async def _impl():
        async with _client(echoing_handler) as client:
            return await exchange_code_for_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret=secret,
                code="authcode",
                redirect_uri="https://agnes.example.com/cb",
                code_verifier="verifier",
                client=client,
            )

    with pytest.raises(OAuthTokenError) as exc:
        run(_impl())
    assert secret not in str(exc.value), "the client secret reached the error message"
    assert "***" in str(exc.value)


def test_the_secret_is_scrubbed_from_a_json_error_description_too():
    """The JSON path is the one a well-behaved AS uses, and it can echo the
    parameter just as easily."""
    secret = "s3cr3t-do-not-log"

    def handler(request):
        if request.headers.get("Authorization", "").startswith("Basic "):
            return httpx.Response(401, json={"error": "invalid_client"})
        return httpx.Response(
            400,
            json={
                "error": "invalid_request",
                "error_description": f"unexpected parameter client_secret={secret}",
            },
        )

    async def _impl():
        async with _client(handler) as client:
            return await exchange_code_for_token(
                token_endpoint="https://as.example.com/token",
                client_id="cid",
                client_secret=secret,
                code="authcode",
                redirect_uri="https://agnes.example.com/cb",
                code_verifier="verifier",
                client=client,
            )

    with pytest.raises(OAuthTokenError) as exc:
        run(_impl())
    assert secret not in str(exc.value)
