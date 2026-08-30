"""Per-transport credential parity + distinguishable MCP auth failures.

Two findings from the #1707 Set-4 audit, both about the auth layer in front of
the two server-hosted MCP transports:

A19 — the transports authenticated differently and the modules documented the
wrong contract. ``/api/mcp`` (SSE, ``app/api/mcp_http.py``) sits behind Agnes
``_AuthMiddleware`` and accepts a PAT; ``/api/mcp/http`` (Streamable-HTTP,
``app/api/mcp_streamable.py``) sits behind the MCP SDK's own bearer middleware,
which only ever accepted tokens issued by the OAuth provider in
``app/auth/mcp_oauth.py``. A valid Agnes PAT therefore got 200 on one transport
and ``401 invalid_token`` on the other. The credential matrix below pins the
fix: a PAT authenticates BOTH transports, an OAuth connector token keeps
working on the streamable one, and revoking either kind still revokes it.

A20 — ``_AuthMiddleware`` collapsed four different outcomes (missing header,
internal exception, credential rejected, by-design principal refusal) into one
fixed ``{"detail": "Not authenticated"}`` 401, so an auth-path crash read to
the caller as a bad token. The outcomes must be distinguishable, and only the
credential-shaped ones may be 401.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

MCP_MOUNT = "/api/mcp/http"


# ── helpers ─────────────────────────────────────────────────────────────────


def _mint_pat(user_id: str = "analyst1", email: str = "analyst@test.com") -> tuple[str, str]:
    """Create a real, live PAT for ``user_id``; return ``(jwt, token_id)``.

    The stored ``token_hash`` must be sha256 of the JWT or
    ``resolve_token_to_user`` rejects it as ``pat_mismatch`` long before the
    code under test runs.
    """
    from app.auth.jwt import create_access_token
    from src.repositories import access_token_repo

    tid = str(uuid.uuid4())
    pat = create_access_token(user_id=user_id, email=email, token_id=tid, typ="pat")
    access_token_repo().create(
        id=tid,
        user_id=user_id,
        name="mcp-transport-test",
        token_hash=hashlib.sha256(pat.encode()).hexdigest(),
        prefix=tid.replace("-", "")[:8],
        expires_at=datetime.now(UTC) + timedelta(days=90),
    )
    return pat, tid


def _sse_response(token: str | None, *, path: str = "/api/mcp/sse") -> tuple[int, dict]:
    """Drive ``_AuthMiddleware`` directly; return ``(status, parsed_body)``.

    ``(0, {})`` when the middleware let the request through to the inner app —
    the SSE stream itself is not what these tests are about.
    """
    from app.api.mcp_http import _AuthMiddleware

    sent: list = []
    passed: list = []

    async def _inner_app(scope, receive, send):
        passed.append(True)

    async def _send(msg):
        sent.append(msg)

    headers = [(b"authorization", f"Bearer {token}".encode())] if token is not None else []
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": headers,
    }
    asyncio.run(_AuthMiddleware(_inner_app)(scope, None, _send))
    if passed:
        return 0, {}
    start = next(m for m in sent if m.get("type") == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m.get("type") == "http.response.body")
    return start["status"], json.loads(body.decode())


def _verify_on_streamable(token: str):
    """What the SDK's bearer middleware sees for ``token`` on /api/mcp/http.

    FastMCP wraps the OAuth provider in ``ProviderTokenVerifier``, whose whole
    body is ``provider.load_access_token(token)`` — so this is the streamable
    transport's accept/reject decision, minus the transport plumbing.
    """
    from app.auth.mcp_oauth import AgnesMCPOAuthProvider

    return asyncio.run(AgnesMCPOAuthProvider().load_access_token(token))


def _save_oauth_access_token(subject: str = "analyst1") -> tuple[str, str]:
    """Mint + persist an OAuth connector access token; return ``(jwt, client_id)``."""
    import time

    from app.auth.jwt import create_access_token
    from src.repositories import oauth_clients_repo

    client_id = str(uuid.uuid4())
    repo = oauth_clients_repo()
    repo.upsert_client(
        client_id=client_id,
        client_secret=None,
        redirect_uris=["http://localhost:9999/callback"],
        client_name="Parity Test Client",
        client_metadata={},
    )
    jwt_token = create_access_token(
        user_id=subject,
        email="analyst@test.com",
        token_id=uuid.uuid4().hex,
        typ="session",
        extra_claims={"scope": "mcp-oauth"},
    )
    repo.save_access_token(
        token=jwt_token,
        client_id=client_id,
        scopes=["read"],
        expires_at=int(time.time()) + 3600,
        subject=subject,
        resource=None,
    )
    return jwt_token, client_id


# ── A19: per-transport credential matrix ─────────────────────────────────────


class TestCredentialMatrixPerTransport:
    def test_pat_is_accepted_on_the_sse_transport(self, seeded_app):
        pat, _ = _mint_pat()
        status, _body = _sse_response(pat)
        assert status == 0, "a live PAT must authenticate the SSE transport"

    def test_pat_is_accepted_on_the_streamable_transport(self, seeded_app):
        """A19: the documented 'Bearer <PAT>' promise must hold on BOTH
        transports, not only the one whose middleware Agnes owns."""
        pat, _ = _mint_pat()
        verified = _verify_on_streamable(pat)
        assert verified is not None, "a live PAT must authenticate the streamable transport"
        assert verified.subject == "analyst1"
        assert "read" in verified.scopes, "must carry the scope the streamable transport requires"

    def test_pat_drives_a_real_jsonrpc_call_over_the_streamable_mount(self, seeded_app_fresh):
        """End-to-end at the advertised connector URL — before the fix this was
        the ``401 invalid_token`` the audit observed live.

        Needs the ASGI lifespan (the SDK session manager), hence
        ``seeded_app_fresh``; see tests/conftest.py::seeded_app.
        """
        with seeded_app_fresh["client"] as client:
            pat, _ = _mint_pat()
            r = client.post(
                MCP_MOUNT,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "pat-parity-test", "version": "0"},
                    },
                },
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/json, text/event-stream",
                },
                follow_redirects=True,
            )
        assert r.status_code == 200, f"PAT refused on the streamable mount: {r.status_code} {r.text[:200]}"
        assert "serverInfo" in r.text

    def test_oauth_connector_token_still_verifies_on_the_streamable_transport(self, seeded_app):
        """The PAT fallback must not displace OAuth — it runs only after it."""
        token, client_id = _save_oauth_access_token()
        verified = _verify_on_streamable(token)
        assert verified is not None, "an OAuth connector token must still authenticate"
        assert verified.client_id == client_id, "must resolve as the OAuth client, not the PAT identity"

    def test_revoked_oauth_token_is_not_resurrected_by_the_pat_fallback(self, seeded_app):
        """The regression the fallback could have introduced.

        An OAuth access token is itself a signed Agnes session JWT, so a
        fallback that accepted any resolvable token would keep honouring one
        after RFC 7009 revocation, for the rest of its 8-hour TTL.
        """
        from src.repositories import oauth_clients_repo

        token, _ = _save_oauth_access_token()
        oauth_clients_repo().revoke_access_token(token)
        assert _verify_on_streamable(token) is None, "revoked OAuth token must stay revoked"

    def test_revoked_pat_is_refused_on_both_transports(self, seeded_app):
        from src.repositories import access_token_repo

        pat, tid = _mint_pat()
        access_token_repo().revoke(tid)

        status, body = _sse_response(pat)
        assert status == 401
        assert body["reason"] == "pat_revoked"
        assert _verify_on_streamable(pat) is None

    def test_garbage_token_is_refused_on_both_transports(self, seeded_app):
        status, body = _sse_response("not-a-real-jwt")
        assert status == 401
        assert body["reason"] == "invalid_token"
        assert _verify_on_streamable("not-a-real-jwt") is None

    def test_streamable_widening_is_scoped_to_pat_typed_credentials(self, seeded_app):
        """A browser session JWT is deliberately NOT a streamable credential.

        The widening exists to honour the documented PAT contract; accepting
        every token ``resolve_token_to_user`` likes would also hand the
        connector surface to a captured browser session cookie value and
        (see above) undo OAuth revocation.
        """
        assert _verify_on_streamable(seeded_app["analyst_token"]) is None


# ── A20: four distinguishable auth outcomes ──────────────────────────────────


class TestAuthFailureOutcomesAreDistinguishable:
    def test_missing_header(self, seeded_app):
        status, body = _sse_response(None)
        assert status == 401
        assert body["reason"] == "no_token"

    def test_malformed_token(self, seeded_app):
        status, body = _sse_response("garbage")
        assert status == 401
        assert body["reason"] == "invalid_token"

    def test_unknown_pat(self, seeded_app):
        """A PAT-typed JWT with no row behind it — never issued, or hard-deleted."""
        from app.auth.jwt import create_access_token

        orphan = create_access_token(
            user_id="analyst1",
            email="analyst@test.com",
            token_id=uuid.uuid4().hex,
            typ="pat",
        )
        status, body = _sse_response(orphan)
        assert status == 401
        assert body["reason"] == "pat_unknown"

    def test_internal_exception_is_a_500_not_a_401(self, seeded_app):
        """An auth-path crash must not read to the caller as a bad credential."""
        with patch(
            "app.auth.pat_resolver.resolve_token_to_user",
            side_effect=RuntimeError("boom"),
        ):
            status, body = _sse_response("any-token")
        assert status == 500
        assert body["reason"] == "auth_internal_error"
        assert "boom" not in json.dumps(body), "internal error text must not leak to the caller"

    def test_principal_refusal_has_its_own_reason(self, seeded_app):
        """A co-session / agent-session principal is refused BY DESIGN on this
        transport (its passthrough closures identify a caller by bare user id),
        which is a different statement from 'your token is bad'."""
        from app.auth.session_principal import SessionPrincipal

        principal = SessionPrincipal(
            session_id="sess1",
            participant_user_ids=["analyst1"],
            participant_emails=["analyst@test.com"],
            intersection={},
        )
        with patch(
            "app.auth.pat_resolver.resolve_token_to_user",
            return_value=(principal, None),
        ):
            status, body = _sse_response("co-session-token")
        assert status == 401
        assert body["reason"] == "principal_wrong_surface"

    def test_the_four_outcomes_are_mutually_distinguishable(self, seeded_app):
        """The acceptance criterion, asserted as one statement."""
        from src.repositories import access_token_repo

        pat, tid = _mint_pat()
        access_token_repo().revoke(tid)

        outcomes = [
            _sse_response(None),  # 1. missing header
            _sse_response("garbage"),  # 2. credential rejected (undecodable)
            _sse_response(pat),  # 3. credential rejected (revoked PAT)
        ]
        with patch(
            "app.auth.pat_resolver.resolve_token_to_user",
            side_effect=RuntimeError("boom"),
        ):
            outcomes.append(_sse_response("any-token"))  # 4. internal exception

        signatures = [(status, body.get("reason")) for status, body in outcomes]
        assert len(set(signatures)) == 4, f"outcomes are not distinguishable: {signatures}"
        assert [s for s, _ in signatures] == [401, 401, 401, 500]
        # Every response still names WHAT to do about it, in the REST 401
        # vocabulary — never the credential itself.
        for (_status, body), token in zip(outcomes, [None, "garbage", pat, "any-token"]):
            assert body["detail"], "every auth failure must carry a human detail"
            if token:
                assert token not in json.dumps(body), "token material must never be echoed"

        # The by-design principal refusal is a fifth, distinct signature —
        # asserted on its own in test_principal_refusal_has_its_own_reason.
