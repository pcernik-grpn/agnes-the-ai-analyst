"""App-tier tests for the chat sandbox secret broker routes (Task 6 of the
2026-07-14 chat-sandbox-secret-broker plan).

Exercises ``app/api/broker.py``: ticket-scope enforcement, admin-path
rejection, and that the in-process ASGI replay produces identical results
to a direct call under the same resolved identity (live RBAC, no broker
privilege of its own).

Uses ``asyncio.run`` rather than ``@pytest.mark.asyncio`` — this repo does
not depend on pytest-asyncio (see tests/test_cache_warmup.py for the same
pattern).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from app.auth.jwt import create_access_token
from app.chat.types import Surface
from src.db import get_system_db
from src.repositories import agents_repo, chat_session_repo, ticket_repo
from src.repositories.users import UserRepository


def _shim_response(resp):
    """Give a canned fake response the async surface the broker's stream-open
    forward now uses (``aread``/``aclose``); a no-op for responses that
    already have it (the real httpx ones)."""
    if not hasattr(resp, "aread"):

        async def _aread():
            return resp.content

        resp.aread = _aread
    if not hasattr(resp, "aclose"):

        async def _aclose():
            return None

        resp.aclose = _aclose
    return resp


class _StreamShimMixin:
    """Bridge the broker's stream-open call shape (``build_request`` +
    ``send(stream=True)`` + ``aclose``) back onto the fakes' legacy
    ``request()`` capture methods, so every existing capture/stub keeps
    working unchanged against the streaming forward."""

    def build_request(self, method, url, *, content=None, headers=None, params=None):
        return {"method": method, "url": url, "content": content, "headers": headers, "params": params}

    async def send(self, req, stream=False):
        resp = await self.request(
            req["method"],
            req["url"],
            content=req["content"],
            headers=req["headers"],
            params=req["params"],
        )
        return _shim_response(resp)

    async def aclose(self):
        return None


@pytest.fixture
def broker_session(e2e_env):
    """A seeded user + chat session, standing in for a spawned sandbox."""
    conn = get_system_db()
    UserRepository(conn).create(id="broker_user1", email="broker@test.com", name="Broker User")
    conn.close()

    session = chat_session_repo().create_session(user_email="broker@test.com", surface=Surface.WEB)
    jwt_token = create_access_token(user_id="broker_user1", email="broker@test.com")
    return {"session_id": session.id, "jwt": jwt_token}


@pytest.fixture
def broker_app(e2e_env, shared_app):

    return shared_app


@pytest.fixture
def broker_agent_session(e2e_env):
    """Factory for a seeded user + agent (with a pinned model / budget) +
    chat session bound to that agent — standing in for a spawned sandbox
    running under an agent profile with model-policy/budget enforcement
    active (Task 8, agent-profiles V1a)."""

    def _make(*, model="claude-opus-4-7", token_budget_monthly=None):
        tag = uuid.uuid4().hex[:8]
        email = f"broker_agent_{tag}@test.com"
        user_id = f"broker_agent_user_{tag}"
        agent_id = str(uuid.uuid4())

        conn = get_system_db()
        UserRepository(conn).create(id=user_id, email=email, name="Broker Agent User")
        conn.close()

        agents_repo().create(
            id=agent_id,
            owner_user_id=user_id,
            name="Broker Agent",
            slug=f"broker-agent-{tag}",
            model=model,
            token_budget_monthly=token_budget_monthly,
        )
        session = chat_session_repo().create_session(user_email=email, surface=Surface.WEB, agent_id=agent_id)
        tok = ticket_repo().mint(session.id, "main", ttl_seconds=60)
        return {"session_id": session.id, "agent_id": agent_id, "tok": tok, "user_id": user_id}

    return _make


def test_expired_ticket_401(broker_app):
    tok = ticket_repo().mint("chat_x", "main", ttl_seconds=-1)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "GET", "path": "/api/me/home-stats", "body": None},
            )

    r = asyncio.run(_run())
    assert r.status_code == 401


def test_mcp_ticket_cannot_use_main_route(broker_app):
    tok = ticket_repo().mint("chat_y", "mcp")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "GET", "path": "/api/me/home-stats", "body": None},
            )

    r = asyncio.run(_run())
    assert r.status_code == 401  # scope mismatch


def test_admin_mutation_rejected(broker_app, broker_session):
    tok = ticket_repo().mint(broker_session["session_id"], "main")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "POST", "path": "/api/admin/grant", "body": {}},
            )

    r = asyncio.run(_run())
    assert r.status_code in (403, 401)


def test_agnes_api_replay_uses_live_rbac(broker_app, broker_session):
    tok = ticket_repo().mint(broker_session["session_id"], "main")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            replayed = await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "GET", "path": "/api/me/home-stats", "body": None},
            )
            direct = await c.get(
                "/api/me/home-stats",
                headers={"Authorization": f"Bearer {broker_session['jwt']}"},
            )
            return replayed, direct

    replayed, direct = asyncio.run(_run())
    assert replayed.status_code == 200, replayed.text
    assert direct.status_code == 200, direct.text
    assert replayed.json() == direct.json()


def test_admin_route_off_admin_prefix_rejected(broker_app, e2e_env):
    """A require_admin route that is NOT under /api/admin/ (here /api/sync/trigger)
    must be rejected by the broker's route-introspection gate — even when the
    resolved identity is itself an admin (so downstream require_admin would pass).
    Proves the fix over the old path-prefix check, which missed such routes (§11).
    """
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    UserRepository(conn).create(id="broker_admin1", email="broker_admin@test.com", name="Broker Admin")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("broker_admin1", admin_gid, source="system_seed")
    conn.close()
    session = chat_session_repo().create_session(user_email="broker_admin@test.com", surface=Surface.WEB)
    tok = ticket_repo().mint(session.id, "main")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "POST", "path": "/api/sync/trigger", "body": {}},
            )

    r = asyncio.run(_run())
    assert r.status_code == 403, r.text
    # the broker's OWN gate fired (not downstream require_admin), proven by the detail
    assert r.json().get("detail") == "admin_mutations_require_interactive_auth"


def test_anthropic_route_accepts_subpath(broker_app):
    """The Anthropic proxy must match sub-paths — the SDK appends
    ``/v1/messages`` to its base URL, so the real request arrives at
    ``/api/broker/anthropic/v1/messages``. An exact-path-only route 404s every
    real model call (Devin review on #849)."""
    from fastapi.routing import APIRoute

    paths = {r.path for r in broker_app.routes if isinstance(r, APIRoute)}
    assert "/api/broker/anthropic/{subpath:path}" in paths, sorted(p for p in paths if "anthropic" in p)
    assert "/api/broker/anthropic" in paths  # bare path still served


def test_anthropic_proxy_uses_generous_read_timeout(broker_app, monkeypatch):
    """Regression: httpx's 5s default read timeout aborts every real LLM
    completion with ReadTimeout, leaving the sandbox agent an empty response.
    The proxy must build its client with a generous read timeout.

    Captures the ``timeout`` passed to ``httpx.AsyncClient`` on the anthropic
    leg and asserts the read budget is well above the 5s default.
    """
    import app.api.broker as broker_mod

    captured: dict = {}
    real_cls = httpx.AsyncClient

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b"{}"

    class _FakeClient(_StreamShimMixin):
        """Delegates to the real client for the test harness's own
        transport-backed client; fakes only the broker's outbound anthropic
        client (constructed with ``timeout=`` and no transport)."""

        def __init__(self, *a, **k):
            self._real = real_cls(*a, **k) if "transport" in k else None
            if self._real is None:
                captured["timeout"] = k.get("timeout")

        async def __aenter__(self):
            return await self._real.__aenter__() if self._real else self

        async def __aexit__(self, *a):
            return await self._real.__aexit__(*a) if self._real else False

        async def request(self, *a, **k):
            if self._real:
                return await self._real.request(*a, **k)
            return _FakeResp()

        def __getattr__(self, name):
            # Proxy any other method (e.g. .post) to the real delegate.
            return getattr(self._real, name)

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _FakeClient)
    tok = ticket_repo().mint("chat_ay", "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                content=b'{"model":"x"}',
            )

    r = asyncio.run(_run())
    assert r.status_code == 200
    t = captured["timeout"]
    assert isinstance(t, httpx.Timeout)
    # Well above httpx's 5s default read timeout.
    assert t.read is not None and t.read >= 60.0, t


class _HeaderCapturingClient(_StreamShimMixin):
    """Fake httpx.AsyncClient that delegates the harness's transport-backed
    client to the real one and captures the headers the broker's outbound
    anthropic client sends. Shared by the auth-mode tests below."""

    _captured: dict = {}
    _real_cls = httpx.AsyncClient

    def __init__(self, *a, **k):
        self._real = self._real_cls(*a, **k) if "transport" in k else None

    async def __aenter__(self):
        return await self._real.__aenter__() if self._real else self

    async def __aexit__(self, *a):
        return await self._real.__aexit__(*a) if self._real else False

    async def request(self, *a, **k):
        if self._real:
            return await self._real.request(*a, **k)
        _HeaderCapturingClient._captured = dict(k.get("headers") or {})

        class _R:
            status_code = 200
            headers = {"content-type": "application/json"}
            content = b"{}"

        return _R()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _lower_keys(d: dict) -> dict:
    return {k.lower(): v for k, v in d.items()}


def test_anthropic_proxy_api_key_mode_injects_x_api_key(broker_app, monkeypatch):
    """AC-1: default (api_key) mode is unchanged — inject x-api-key, no Authorization."""
    import app.api.broker as broker_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-KEY")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _HeaderCapturingClient)
    tok = ticket_repo().mint("chat_apikey", "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                content=b'{"model":"x"}',
            )

    r = asyncio.run(_run())
    assert r.status_code == 200
    h = _lower_keys(_HeaderCapturingClient._captured)
    assert h.get("x-api-key") == "sk-ant-static-KEY"
    assert "authorization" not in h


def test_anthropic_proxy_workload_identity_injects_bearer_not_key(broker_app, monkeypatch):
    """AC-2: workload_identity mode injects a federated Bearer token + the oauth
    beta header, and sends NO static x-api-key."""
    import types

    import app.api.broker as broker_mod
    import app.auth.wif as wif

    # Flip the app into workload_identity mode (ChatConfig is frozen; a duck-typed
    # stand-in with the one attribute the broker reads is enough).
    broker_app.state.chat_config = types.SimpleNamespace(llm_auth="workload_identity")
    monkeypatch.setattr(wif, "get_federated_access_token", lambda: "sk-ant-oat01-FED")
    # A static key is present but must be ignored in this mode.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-SHOULD-NOT-BE-USED")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _HeaderCapturingClient)
    tok = ticket_repo().mint("chat_wif", "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}", "anthropic-version": "2023-06-01"},
                content=b'{"model":"x"}',
            )

    r = asyncio.run(_run())
    assert r.status_code == 200
    h = _lower_keys(_HeaderCapturingClient._captured)
    assert h.get("authorization") == "Bearer sk-ant-oat01-FED"
    assert "x-api-key" not in h
    assert "oauth-2025-04-20" in h.get("anthropic-beta", "")
    # sanity: the sandbox's SDK header survived
    assert h.get("anthropic-version") == "2023-06-01"


def test_anthropic_proxy_wif_failure_returns_generic_detail(broker_app, monkeypatch):
    """A WIF exchange failure must NOT echo Anthropic's raw error text (which can
    carry org/rule/service-account ids) across the sandbox boundary — the caller
    gets a generic 502, the detail is only in the server-side audit trail."""
    import types

    import app.auth.wif as wif

    broker_app.state.chat_config = types.SimpleNamespace(llm_auth="workload_identity")

    def _boom():
        raise wif.WIFAuthError('token exchange failed: HTTP 400 {"error":"invalid_grant","org":"org_SECRET123"}')

    monkeypatch.setattr(wif, "get_federated_access_token", _boom)
    tok = ticket_repo().mint("chat_wif_fail", "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                content=b'{"model":"x"}',
            )

    r = asyncio.run(_run())
    assert r.status_code == 502
    body = r.text
    assert "org_SECRET123" not in body
    assert "invalid_grant" not in body
    assert "workload_identity token exchange failed" in body


class _StubResponseClient(_StreamShimMixin):
    """Fake httpx.AsyncClient whose outbound forward returns a canned upstream
    response, so we can drive the broker's LLM-credential health signal (#884).

    Follows the ``_HeaderCapturingClient`` pattern: when constructed with a
    ``transport`` kwarg it is the harness's ASGI-driving client and delegates to
    the real ``httpx.AsyncClient``; otherwise it is the broker's outbound
    anthropic client and returns the canned ``status_code`` / ``body``. Without
    this delegation, monkeypatching ``httpx.AsyncClient`` also breaks the test's
    own request into the app (no ``.post``) and leaks the stub across tests."""

    status_code = 200
    body = b"{}"
    _real_cls = httpx.AsyncClient

    def __init__(self, *a, **k):
        self._real = self._real_cls(*a, **k) if "transport" in k else None

    async def __aenter__(self):
        return await self._real.__aenter__() if self._real else self

    async def __aexit__(self, *a):
        return await self._real.__aexit__(*a) if self._real else False

    async def request(self, *a, **k):
        if self._real:
            return await self._real.request(*a, **k)
        cls = type(self)

        class _R:
            status_code = cls.status_code
            headers = {"content-type": "application/json"}
            content = cls.body
            text = cls.body.decode()

            def json(self):
                import json as _json

                return _json.loads(cls.body)

        return _R()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _forward_anthropic(broker_app, tok):
    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                content=b'{"model":"x"}',
            )

    return asyncio.run(_run())


def test_anthropic_proxy_records_credit_diagnostic(broker_app, monkeypatch):
    """A 400 'credit balance too low' upstream response is classified and
    recorded on app.state so the admin readiness banner can surface it (#884)."""
    import app.api.broker as broker_mod
    from app.chat.readiness import LLM_REASON_CREDIT, get_llm_runtime_diagnostic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static")
    _StubResponseClient.status_code = 400
    _StubResponseClient.body = (
        b'{"error":{"type":"invalid_request_error","message":"Your credit balance is too low to access the API."}}'
    )
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _StubResponseClient)
    tok = ticket_repo().mint("chat_credit", "main", ttl_seconds=60)

    r = _forward_anthropic(broker_app, tok)
    assert r.status_code == 400  # the upstream status is passed through unchanged
    diag = get_llm_runtime_diagnostic(broker_app.state)
    assert diag is not None and diag["reason"] == LLM_REASON_CREDIT


def test_anthropic_proxy_success_clears_diagnostic(broker_app, monkeypatch):
    """A healthy (2xx) forward clears any stale LLM-credential signal."""
    import app.api.broker as broker_mod
    from app.chat.readiness import get_llm_runtime_diagnostic, record_llm_runtime_failure

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static")
    record_llm_runtime_failure(broker_app.state, 401, "stale")
    _StubResponseClient.status_code = 200
    _StubResponseClient.body = b"{}"
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _StubResponseClient)
    tok = ticket_repo().mint("chat_ok", "main", ttl_seconds=60)

    r = _forward_anthropic(broker_app, tok)
    assert r.status_code == 200
    assert get_llm_runtime_diagnostic(broker_app.state) is None


def test_normalize_broker_path_rejects_smuggling():
    """Unit: the path canonicalizer returns the EXACT URL the ASGI dispatch
    routes on (percent-decoded, dot-segments collapsed) and rejects authority
    smuggling (§11, RBAC review #849)."""
    from fastapi import HTTPException

    from app.api.broker import _normalize_broker_path

    # accepted, query preserved; .path is the real dispatch target
    assert _normalize_broker_path("/api/me/home-stats").path == "/api/me/home-stats"
    got = _normalize_broker_path("/api/x?a=1&b=2")
    assert got.path == "/api/x" and got.query == b"a=1&b=2"

    # canonicalization: interior percent-encoding and dot-segment traversal
    # resolve to the SAME path the gate must guard (both = /api/sync/trigger),
    # so the gate can no longer be fooled into reading them as non-admin.
    assert _normalize_broker_path("/api/sync/tri%67ger").path == "/api/sync/trigger"
    assert _normalize_broker_path("/api/foo/../sync/trigger").path == "/api/sync/trigger"

    for bad in (
        "http://evil.example/api/sync/trigger",
        "https://evil.example/api/sync/trigger",
        "//evil.example/api/sync/trigger",
        "http://broker-replay/api/sync/trigger",
        "\\\\evil.example\\api\\sync\\trigger",
        "/%2f%2fevil/api/sync/trigger",
        "relative/no/leading/slash",
        "",
    ):
        with pytest.raises(HTTPException) as ei:
            _normalize_broker_path(bad)
        assert ei.value.status_code == 400, bad
        assert ei.value.detail == "broker_path_must_be_local", bad


def test_normalize_upstream_path_strips_trailing_and_collapses_duplicate_slashes():
    """Unit: the model-policy/ledger gate and the `use_dispatcher` check in
    ``anthropic_proxy`` must agree on the SAME normalized upstream path — a
    literal `== "/v1/messages"` against the `{subpath:path}` wildcard
    diverges for trailing/duplicate-slash variants."""
    from app.api.broker import _normalize_upstream_path

    assert _normalize_upstream_path("/v1/messages") == "/v1/messages"
    assert _normalize_upstream_path("/v1/messages/") == "/v1/messages"
    assert _normalize_upstream_path("//v1//messages") == "/v1/messages"
    assert _normalize_upstream_path("/v1/messages/count_tokens") == "/v1/messages/count_tokens"
    assert _normalize_upstream_path("/") == "/"
    assert _normalize_upstream_path("") == "/"


def test_admin_route_path_smuggling_rejected(broker_app, e2e_env):
    """A smuggled absolute-URL / protocol-relative / encoded path that the
    ASGI transport would still dispatch to an admin route (/api/sync/trigger)
    must NOT bypass the broker's admin gate — proven with an admin-owner
    ticket, so downstream require_admin would otherwise pass (RBAC review #849).
    """
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    UserRepository(conn).create(id="broker_admin_sm", email="broker_admin_sm@test.com", name="Broker Admin SM")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("broker_admin_sm", admin_gid, source="system_seed")
    conn.close()
    session = chat_session_repo().create_session(user_email="broker_admin_sm@test.com", surface=Surface.WEB)
    tok = ticket_repo().mint(session.id, "main")

    smuggled = [
        "http://evil.example/api/sync/trigger",
        "//evil.example/api/sync/trigger",
        "http://broker-replay/api/sync/trigger",
        "\\\\evil.example\\api\\sync\\trigger",
        "/%2f%2fevil/api/sync/trigger",
        # canonicalization-divergence vectors (RBAC review #849 round 2): the
        # ASGI transport decodes %67 -> 'g' and collapses '..', so these reach
        # /api/sync/trigger unless the gate guards the SAME canonical path.
        "/api/sync/tri%67ger",
        "/api/foo/../sync/trigger",
        "/api/sync/%2e%2e/sync/trigger",
    ]

    async def _run(p):
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": "POST", "path": p, "body": {}},
            )

    for p in smuggled:
        r = asyncio.run(_run(p))
        # Security invariant: the admin-gated handler must NEVER execute under a
        # smuggled path. Acceptable outcomes: 400 (rejected as authority
        # smuggling), 403 (canonical path guarded by the admin gate), or a
        # 404/405 misroute — never a 200 that actually triggers the sync.
        assert r.status_code != 200, f"{p} -> 200 (admin handler executed): {r.text}"
        body = r.json()
        assert body.get("status") != "triggered", f"{p} REACHED the admin handler: {r.text}"


def test_cosession_ticket_mints_cosession_jwt(broker_app, e2e_env):
    """A co-session's broker replay must mint a co_session JWT (live
    grant-intersection), not resolve to the single stored owner (§11)."""
    from app.api.broker import _mint_identity_jwt
    from app.auth.jwt import verify_token
    from src.db import get_system_db

    conn = get_system_db()
    UserRepository(conn).create(id="co_owner1", email="co_owner@test.com", name="Co Owner")
    conn.close()
    solo = chat_session_repo().create_session(user_email="co_owner@test.com", surface=Surface.WEB)
    co = chat_session_repo().create_session(user_email="co_owner@test.com", surface=Surface.WEB)
    # flip the co-session flag directly (a co-session is otherwise created via fork)
    conn = get_system_db()
    conn.execute("UPDATE chat_sessions SET is_co_session = TRUE WHERE id = ?", [co.id])
    conn.close()

    solo_payload = verify_token(_mint_identity_jwt(solo.id))
    co_payload = verify_token(_mint_identity_jwt(co.id))
    assert solo_payload.get("typ") == "session"
    assert co_payload.get("typ") == "co_session"
    # the co-session JWT carries no real user identity (synthetic sub), only the session
    assert co_payload.get("sub") == f"session:{co.id}"
    assert co_payload.get("chat_session_id") == co.id
    # The solo mint must ALSO carry chat_session_id bound to the resolved
    # session — this is the claim `app.api.agent_memory` compares against the
    # path {session_id} (C2 binding). A refactor that drops it from the solo
    # mint would keep every scope-only assertion green while silently
    # degrading the memory-write endpoint's session binding to "trust the
    # URL path" (M1).
    assert solo_payload.get("chat_session_id") == solo.id
    # BOTH broker mints must carry scope="chat" so the per-session BigQuery
    # scan-budget stash (`_stash_chat_session_id_from_token`) fires — it ignores
    # the chat_session_id claim without that scope, silently disabling the cap
    # for brokered chat traffic (security review on #849).
    assert solo_payload.get("scope") == "chat"
    assert co_payload.get("scope") == "chat"


class _UrlCapturingClient(_HeaderCapturingClient):
    """_HeaderCapturingClient that additionally records the outbound URL, so
    the dispatcher opt-in tests can assert WHERE the broker forwarded."""

    _captured_url: str = ""

    async def request(self, method, url, *a, **k):
        if self._real:
            return await self._real.request(method, url, *a, **k)
        _UrlCapturingClient._captured_url = str(url)
        return await super().request(method, url, *a, **k)


def _post_broker_anthropic(broker_app, subpath, ticket_label):
    # Clear captured state from earlier tests so every assertion proves THIS
    # request was forwarded — stale class attributes could otherwise satisfy
    # the URL/header checks even if the broker never made the outbound call.
    # NB: headers live on the BASE class (its request() assigns
    # `_HeaderCapturingClient._captured` explicitly); resetting via the
    # subclass would shadow that attribute and break the read-back.
    _HeaderCapturingClient._captured = {}
    _UrlCapturingClient._captured_url = ""
    tok = ticket_repo().mint(ticket_label, "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                f"/api/broker/anthropic{subpath}",
                headers={"Authorization": f"Bearer {tok}"},
                content=b'{"model":"x"}',
            )

    return asyncio.run(_run())


def test_dispatcher_optin_routes_v1_messages(broker_app, monkeypatch):
    """LLM_DISPATCHER_URL set → POST /v1/messages goes to the dispatcher with
    the dispatcher key; the static Anthropic key is NOT sent."""
    import app.api.broker as broker_mod

    monkeypatch.setenv("LLM_DISPATCHER_URL", "http://127.0.0.1:8600")
    monkeypatch.setenv("LLM_DISPATCHER_API_KEY", "agnes-team-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-KEY")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_broker_anthropic(broker_app, "/v1/messages", "chat_disp1")
    assert r.status_code == 200
    assert _UrlCapturingClient._captured_url == "http://127.0.0.1:8600/v1/messages"
    h = _lower_keys(_UrlCapturingClient._captured)
    assert h.get("x-api-key") == "agnes-team-key"


def test_dispatcher_optin_other_subpaths_stay_on_anthropic(broker_app, monkeypatch):
    """count_tokens (and any non-/v1/messages subpath) keeps the pinned
    Anthropic upstream + static key even while opted in — the dispatcher
    only implements /v1/messages."""
    import app.api.broker as broker_mod

    monkeypatch.setenv("LLM_DISPATCHER_URL", "http://127.0.0.1:8600")
    monkeypatch.setenv("LLM_DISPATCHER_API_KEY", "agnes-team-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-KEY")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_broker_anthropic(broker_app, "/v1/messages/count_tokens", "chat_disp2")
    assert r.status_code == 200
    assert _UrlCapturingClient._captured_url == ("https://api.anthropic.com/v1/messages/count_tokens")
    h = _lower_keys(_UrlCapturingClient._captured)
    assert h.get("x-api-key") == "sk-ant-static-KEY"


def test_dispatcher_unset_default_upstream_unchanged(broker_app, monkeypatch):
    """No LLM_DISPATCHER_URL → today's pinned-Anthropic behavior."""
    import app.api.broker as broker_mod

    monkeypatch.delenv("LLM_DISPATCHER_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-KEY")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_broker_anthropic(broker_app, "/v1/messages", "chat_disp3")
    assert r.status_code == 200
    assert _UrlCapturingClient._captured_url == "https://api.anthropic.com/v1/messages"
    h = _lower_keys(_UrlCapturingClient._captured)
    assert h.get("x-api-key") == "sk-ant-static-KEY"


def test_dispatcher_optin_takes_precedence_over_wif(broker_app, monkeypatch):
    """Explicit dispatcher opt-in wins over workload_identity for /v1/messages:
    dispatcher key auth, no Bearer, and the WIF exchange is never attempted."""
    import types

    import app.api.broker as broker_mod
    import app.auth.wif as wif

    broker_app.state.chat_config = types.SimpleNamespace(llm_auth="workload_identity")

    def _must_not_be_called():
        raise AssertionError("WIF exchange must not run when dispatcher is opted in")

    monkeypatch.setattr(wif, "get_federated_access_token", _must_not_be_called)
    monkeypatch.setenv("LLM_DISPATCHER_URL", "http://127.0.0.1:8600")
    monkeypatch.setenv("LLM_DISPATCHER_API_KEY", "agnes-team-key")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_broker_anthropic(broker_app, "/v1/messages", "chat_disp4")
    assert r.status_code == 200
    h = _lower_keys(_UrlCapturingClient._captured)
    assert h.get("x-api-key") == "agnes-team-key"
    assert "authorization" not in h


def test_dispatcher_optin_empty_key_logs_warning(broker_app, monkeypatch, caplog):
    """URL set but key unset is a deployment misconfig: the request is still
    forwarded to the dispatcher (no fallback) and the broker logs a
    server-side warning naming the cause. This test asserts the forwarding
    and the warning; the eventual 401 is the real dispatcher's behavior, not
    something the fake outbound client here reproduces."""
    import logging

    import app.api.broker as broker_mod

    monkeypatch.setenv("LLM_DISPATCHER_URL", "http://127.0.0.1:8600")
    monkeypatch.delenv("LLM_DISPATCHER_API_KEY", raising=False)
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    with caplog.at_level(logging.WARNING, logger="app.api.broker"):
        r = _post_broker_anthropic(broker_app, "/v1/messages", "chat_disp5")
    assert r.status_code == 200
    assert _UrlCapturingClient._captured_url == "http://127.0.0.1:8600/v1/messages"
    assert any("LLM_DISPATCHER_API_KEY is empty" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Task 8 wiring: per-agent model policy / usage ledger / budget, exercised
# end-to-end through anthropic_proxy (not just the pure-logic unit tests in
# tests/test_broker_agent_policy.py).
# ---------------------------------------------------------------------------


def test_anthropic_proxy_pinned_model_rejects_foreign_model(broker_app, broker_agent_session):
    """(a) A pinned-model agent's session posting a body with a foreign
    model gets 403 model_not_allowed, BEFORE any upstream call — and the
    budget headers are present because this agent has a budget configured."""
    ctx = broker_agent_session(model="claude-opus-4-7", token_budget_monthly=100_000)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {ctx['tok']}"},
                json={"model": "some-other-vendor-model", "messages": []},
            )

    r = asyncio.run(_run())
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "model_not_allowed"
    assert r.headers.get("x-agnes-budget-limit") == "100000"
    assert r.headers.get("x-agnes-budget-used") == "0"


def test_anthropic_proxy_budget_exhausted_no_retry_after(broker_app, broker_agent_session):
    """(b) An agent with a tiny monthly budget, already over it per the
    llm_usage ledger, gets 429 budget_exhausted with NO Retry-After header
    (SDKs must not auto-retry a budget exhaustion) but WITH the budget
    headers — raised before any upstream call."""
    from src.repositories import llm_usage_repo

    ctx = broker_agent_session(model="claude-opus-4-7", token_budget_monthly=10)

    llm_usage_repo().insert_batch(
        [
            {
                "id": str(uuid.uuid4()),
                "agent_id": ctx["agent_id"],
                "user_id": ctx["user_id"],
                "session_id": ctx["session_id"],
                "model": "claude-opus-4-7",
                "input_tokens": 50,
                "output_tokens": 50,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
        ]
    )

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {ctx['tok']}"},
                json={"model": "claude-opus-4-7", "messages": []},
            )

    r = asyncio.run(_run())
    assert r.status_code == 429, r.text
    assert r.json()["detail"]["code"] == "budget_exhausted"
    assert "retry-after" not in {k.lower() for k in r.headers.keys()}
    assert r.headers.get("x-agnes-budget-limit") == "10"
    assert r.headers.get("x-agnes-budget-used") == "100"


def test_anthropic_proxy_happy_path_records_usage_and_budget_headers(broker_app, broker_agent_session, monkeypatch):
    """(c) A pinned-model agent's matching-model request forwards to the
    (mocked) upstream, returns 200 with x-agnes-budget-* headers, and the
    usage row lands in the llm_usage ledger once the accumulator is
    flushed. Mirrors the existing _StubResponseClient fake-upstream pattern
    used by the credit/health-diagnostic tests above."""
    import json

    import app.api.broker as broker_mod
    from app.api.broker_agent_policy import usage_accumulator
    from src.repositories import llm_usage_repo

    ctx = broker_agent_session(model="claude-opus-4-7", token_budget_monthly=100_000)

    _StubResponseClient.status_code = 200
    _StubResponseClient.body = json.dumps(
        {
            "id": "msg_happy",
            "model": "claude-opus-4-7",
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }
    ).encode()
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _StubResponseClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {ctx['tok']}"},
                json={"model": "claude-opus-4-7", "messages": []},
            )

    r = asyncio.run(_run())
    assert r.status_code == 200, r.text
    assert r.headers.get("x-agnes-budget-limit") == "100000"
    assert r.headers.get("x-agnes-budget-used") == "0"

    usage_accumulator.flush()
    rows = llm_usage_repo().list_for_agent(ctx["agent_id"])
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 11
    assert rows[0]["output_tokens"] == 7


# ---------------------------------------------------------------------------
# C2.4 — per-caller usage attribution (remediation Track C,
# docs/superpowers/plans/2026-08-26-one-agent-model.md §C2.4). A shared
# agent (C2.3) can be run by many callers; usage rows must be attributed to
# WHICH caller incurred them, while the agent-level budget stays shared.
# ---------------------------------------------------------------------------


def _grantee_session_ticket(agent_id: str) -> dict:
    """A second user's own session bound to the SAME agent as
    ``broker_agent_session`` — standing in for a grantee (C2.3, shared-agent
    runtime) driving a turn against an agent they don't own. The broker
    layer under test here doesn't itself check the `ResourceType.AGENT`
    grant (that's enforced earlier, at session creation — `app/api/chat.py`
    / `app/api/agent_sessions.py`); this fixture only needs a real session
    row naming a DIFFERENT `user_email` than the owner's, exactly what
    those routes would have produced for an authorized grantee."""
    tag = uuid.uuid4().hex[:8]
    email = f"broker_grantee_{tag}@test.com"
    user_id = f"broker_grantee_user_{tag}"

    conn = get_system_db()
    UserRepository(conn).create(id=user_id, email=email, name="Broker Grantee")
    conn.close()

    session = chat_session_repo().create_session(user_email=email, surface=Surface.WEB, agent_id=agent_id)
    tok = ticket_repo().mint(session.id, "main", ttl_seconds=60)
    return {"session_id": session.id, "tok": tok, "user_id": user_id}


def test_two_callers_on_one_shared_agent_produce_distinguishable_usage_rows(
    broker_app, broker_agent_session, monkeypatch
):
    """C2.4: the owner and a grantee each run a turn against the SAME
    shared agent — the rows handed to `llm_usage_repo().insert_batch` carry
    each caller's OWN `caller_user_id`, never the agent owner's, for both
    calls. Asserted against the row dicts the accumulator actually builds
    (backend-agnostic — DuckDB has no column to persist `caller_user_id`
    into, see `tests/db_pg/test_llm_usage_contract.py` for that half)."""
    import json

    import app.api.broker as broker_mod
    from app.api import broker_agent_policy as pol

    ctx = broker_agent_session(model="claude-opus-4-7", token_budget_monthly=100_000)
    grantee = _grantee_session_ticket(ctx["agent_id"])

    captured: list = []

    class _CapturingLlmUsageRepo:
        def insert_batch(self, rows):
            captured.extend(rows)

        def month_total_tokens(self, agent_id, year_month):
            return 0

    monkeypatch.setattr(pol, "llm_usage_repo", lambda: _CapturingLlmUsageRepo())

    _StubResponseClient.status_code = 200
    _StubResponseClient.body = json.dumps(
        {"id": "msg1", "model": "claude-opus-4-7", "usage": {"input_tokens": 11, "output_tokens": 7}}
    ).encode()
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _StubResponseClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static")

    async def _call(tok):
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                json={"model": "claude-opus-4-7", "messages": []},
            )

    r_owner = asyncio.run(_call(ctx["tok"]))
    r_grantee = asyncio.run(_call(grantee["tok"]))
    assert r_owner.status_code == 200, r_owner.text
    assert r_grantee.status_code == 200, r_grantee.text

    pol.usage_accumulator.flush()
    assert len(captured) == 2
    by_agent = [row for row in captured if row["agent_id"] == ctx["agent_id"]]
    assert len(by_agent) == 2
    caller_ids = {row["caller_user_id"] for row in by_agent}
    assert caller_ids == {ctx["user_id"], grantee["user_id"]}
    # `user_id` (unchanged, pre-C2.4 meaning) stays the agent's OWNER for
    # BOTH rows -- only `caller_user_id` distinguishes who actually spent
    # the tokens.
    assert {row["user_id"] for row in by_agent} == {ctx["user_id"]}


def test_shared_agent_budget_enforced_across_callers_not_per_caller(broker_app, broker_agent_session, monkeypatch):
    """Budget enforcement is UNCHANGED by C2.4 — still keyed on `agent_id`
    alone. The owner's turn pushing a shared agent over its
    `token_budget_monthly` must 429 a DIFFERENT caller's very next turn on
    that SAME agent, not just the owner's own."""
    import json

    import app.api.broker as broker_mod

    ctx = broker_agent_session(model="claude-opus-4-7", token_budget_monthly=15)
    grantee = _grantee_session_ticket(ctx["agent_id"])

    _StubResponseClient.status_code = 200
    _StubResponseClient.body = json.dumps(
        {"id": "msg1", "model": "claude-opus-4-7", "usage": {"input_tokens": 11, "output_tokens": 7}}
    ).encode()
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _StubResponseClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static")

    async def _call(tok):
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                json={"model": "claude-opus-4-7", "messages": []},
            )

    # Owner's turn: 11 + 7 = 18 tokens, pushing the agent's 15-token budget
    # over the top (recorded synchronously into the shared budget cache by
    # `UsageAccumulator._incr_budget_counter`, no DB flush needed).
    r_owner = asyncio.run(_call(ctx["tok"]))
    assert r_owner.status_code == 200, r_owner.text
    assert r_owner.headers.get("x-agnes-budget-used") == "0"  # pre-call total

    # The GRANTEE's very next turn on the SAME agent -- not the owner's --
    # is refused. Enforcement is per-AGENT, not per-caller.
    r_grantee = asyncio.run(_call(grantee["tok"]))
    assert r_grantee.status_code == 429, r_grantee.text
    assert r_grantee.json()["detail"]["code"] == "budget_exhausted"
    assert r_grantee.headers.get("x-agnes-budget-used") == "18"


# --- POST /api/broker/data-apps (Task 7, wave 3B) ---------------------------
#
# Mirrors the `agnes-api`/`agnes-mcp` twin-endpoint pattern: a `data_apps`
# scoped ticket, minted at chat spawn, lets the sandboxed authoring agent
# replay `/api/data-apps/*` requests under its resolved identity instead of
# carrying a raw PAT. The route additionally confines the replayed path to
# the `/api/data-apps` prefix — every other path (even a non-admin one) is
# rejected with `path_not_allowed`, on top of (not instead of) the generic
# `_replay` admin-route gate.


@pytest.fixture
def broker_env(e2e_env, shared_app):
    """A seeded user + chat session with a data_apps-scoped ticket, data_apps
    feature enabled, and a real TestClient(app) — standing in for the
    sandboxed authoring agent's broker call."""
    import yaml
    from fastapi.testclient import TestClient

    import app.instance_config as instance_config

    data_dir = e2e_env["data_dir"]
    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    instance_config._instance_config = None

    conn = get_system_db()
    UserRepository(conn).create(id="broker_da_user1", email="broker_da@test.com", name="Broker DA User")
    conn.close()

    session = chat_session_repo().create_session(user_email="broker_da@test.com", surface=Surface.WEB)
    tok = ticket_repo().mint(session.id, "data_apps")

    client = TestClient(shared_app)
    return client, tok


@pytest.fixture
def broker_env_main_scope(e2e_env, shared_app):
    """Same as `broker_env`, but the ticket is minted with the `main` scope —
    used to prove a wrong-scope ticket cannot authenticate the data-apps
    broker route."""
    import yaml
    from fastapi.testclient import TestClient

    import app.instance_config as instance_config

    data_dir = e2e_env["data_dir"]
    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    instance_config._instance_config = None

    conn = get_system_db()
    UserRepository(conn).create(id="broker_da_user2", email="broker_da_main@test.com", name="Broker DA Main User")
    conn.close()

    session = chat_session_repo().create_session(user_email="broker_da_main@test.com", surface=Surface.WEB)
    tok = ticket_repo().mint(session.id, "main")

    client = TestClient(shared_app)
    return client, tok


def test_broker_data_apps_scope(broker_env):
    client, ticket = broker_env
    r = client.post(
        "/api/broker/data-apps",
        headers={"Authorization": f"Bearer {ticket}"},
        json={"path": "/api/data-apps", "method": "GET"},
    )
    assert r.status_code == 200, r.text


def test_broker_data_apps_wrong_scope_rejected(broker_env_main_scope):
    client, ticket = broker_env_main_scope
    r = client.post(
        "/api/broker/data-apps",
        headers={"Authorization": f"Bearer {ticket}"},
        json={"path": "/api/data-apps", "method": "GET"},
    )
    assert r.status_code == 401 and r.json()["detail"] == "ticket_scope_mismatch"


def test_broker_data_apps_path_confined(broker_env):
    client, ticket = broker_env
    r = client.post(
        "/api/broker/data-apps",
        headers={"Authorization": f"Bearer {ticket}"},
        json={"path": "/api/admin/users", "method": "GET"},
    )
    assert r.status_code == 403 and r.json()["detail"] == "path_not_allowed"


def test_broker_data_apps_dot_segment_traversal_rejected(broker_env):
    """A literal `..` segment collapses (via the same `_normalize_broker_path`
    canonicalizer `_replay` uses) to a real, non-admin, out-of-prefix route —
    `/api/data-apps/../catalog` resolves to `/api/catalog`. A raw-string
    prefix check on the agent-supplied path would pass this through; the gate
    must decide on the canonicalized path instead (mirrors the admin-route
    gate hardening on #849)."""
    client, ticket = broker_env
    r = client.post(
        "/api/broker/data-apps",
        headers={"Authorization": f"Bearer {ticket}"},
        json={"path": "/api/data-apps/../catalog", "method": "GET"},
    )
    assert r.status_code == 403 and r.json()["detail"] == "path_not_allowed"


@pytest.mark.parametrize(
    "evil_path",
    [
        "/api/data-apps/%2e%2e/catalog",
        "/api/data-apps/..%2fcatalog",
    ],
)
def test_broker_data_apps_percent_encoded_traversal_rejected(broker_env, evil_path):
    """Percent-encoded dot-segments survive `_normalize_broker_path`'s decode
    without being collapsed (httpx only collapses *literal* `..` at URL
    construction time), so the canonicalized path still starts with
    `/api/data-apps/` while carrying a literal `..` segment. No legitimate
    `/api/data-apps/*` call needs a `..` segment, so these are rejected
    outright rather than trusted to 404 harmlessly."""
    client, ticket = broker_env
    r = client.post(
        "/api/broker/data-apps",
        headers={"Authorization": f"Bearer {ticket}"},
        json={"path": evil_path, "method": "GET"},
    )
    assert r.status_code == 403 and r.json()["detail"] == "path_not_allowed"


def test_broker_data_apps_prefix_boundary_rejected(broker_env):
    """`/api/data-apps-evil` shares the `/api/data-apps` string prefix but is
    a different (hypothetical) route, not a sub-path — the confinement check
    must be an exact-or-slash-boundary match, not a bare `str.startswith`."""
    client, ticket = broker_env
    r = client.post(
        "/api/broker/data-apps",
        headers={"Authorization": f"Bearer {ticket}"},
        json={"path": "/api/data-apps-evil", "method": "GET"},
    )
    assert r.status_code == 403 and r.json()["detail"] == "path_not_allowed"


def test_anthropic_sse_streams_through_without_buffering(broker_app, monkeypatch):
    """A 2xx ``text/event-stream`` completion must flow through the broker as
    a stream: the outbound forward opens with ``stream=True``, the body is
    NEVER buffered server-side (``aread`` not called — buffering here
    collapsed every token delta into one end-of-turn burst), the SSE
    content-type reaches the caller, and the upstream response + client are
    closed once the stream is consumed."""
    import app.api.broker as broker_mod

    calls: dict = {}
    real_cls = httpx.AsyncClient

    class _SSEClient(_StreamShimMixin):
        def __init__(self, *a, **k):
            self._real = real_cls(*a, **k) if "transport" in k else None

        async def __aenter__(self):
            return await self._real.__aenter__() if self._real else self

        async def __aexit__(self, *a):
            return await self._real.__aexit__(*a) if self._real else False

        async def send(self, req, stream=False):
            calls["stream"] = stream

            class _R:
                status_code = 200
                headers = {"content-type": "text/event-stream"}

                async def aiter_bytes(self):
                    yield b"event: message_start\n\n"
                    yield b"event: content_block_delta\n\n"

                async def aread(self):
                    calls["aread"] = True
                    return b""

                async def aclose(self):
                    calls["closed"] = True

            return _R()

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _SSEClient)
    tok = ticket_repo().mint("chat_sse", "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                content=b'{"model":"x","stream":true}',
            )

    r = asyncio.run(_run())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert b"message_start" in r.content and b"content_block_delta" in r.content
    assert calls["stream"] is True, "outbound forward must open with stream=True"
    assert "aread" not in calls, "SSE body must not be buffered server-side"
    assert calls.get("closed") is True, "upstream must be closed after the stream drains"


def test_anthropic_sse_upstream_closed_even_when_stream_breaks(broker_app, monkeypatch):
    """Regression (RBAC review on #1020): Starlette's ``background=`` callback
    only runs on the happy path — if the SSE body iterator raises mid-stream
    (upstream drop) or the client walks away, a background-task cleanup never
    fires and the upstream response + per-request client leak. Cleanup lives
    in the pass-through iterator's ``finally``, which runs even when the
    stream breaks."""
    import app.api.broker as broker_mod

    calls: dict = {}
    real_cls = httpx.AsyncClient

    class _BreakingSSEClient(_StreamShimMixin):
        def __init__(self, *a, **k):
            self._real = real_cls(*a, **k) if "transport" in k else None

        async def __aenter__(self):
            return await self._real.__aenter__() if self._real else self

        async def __aexit__(self, *a):
            return await self._real.__aexit__(*a) if self._real else False

        async def send(self, req, stream=False):
            class _R:
                status_code = 200
                headers = {"content-type": "text/event-stream"}

                async def aiter_bytes(self):
                    yield b"event: message_start\n\n"
                    raise RuntimeError("simulated upstream drop mid-stream")

                async def aread(self):
                    return b""

                async def aclose(self):
                    calls["resp_closed"] = True

            return _R()

        async def aclose(self):
            calls["client_closed"] = True

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _BreakingSSEClient)
    tok = ticket_repo().mint("chat_sse_break", "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            try:
                await c.post(
                    "/api/broker/anthropic/v1/messages",
                    headers={"Authorization": f"Bearer {tok}"},
                    content=b'{"model":"x","stream":true}',
                )
            except Exception:
                # The mid-stream break propagates through the ASGI transport —
                # expected; the assertion is about cleanup, not the error.
                pass

    asyncio.run(_run())
    assert calls.get("resp_closed") is True, "upstream response must close when the stream breaks"
    assert calls.get("client_closed") is True, "per-request client must close when the stream breaks"


def test_anthropic_sse_stream_records_agent_usage(broker_app, broker_agent_session, monkeypatch):
    """Streaming counterpart of the buffered happy-path usage test: a 2xx
    ``text/event-stream`` completion for an agent-attributed session must
    land in the llm_usage ledger once the stream drains — the buffered
    recording branch never runs for SSE, so the passthrough iterator's
    finally-block mirror does the recording (Devin review: budgets never
    fired for ordinary streamed turns)."""
    import app.api.broker as broker_mod
    from app.api.broker_agent_policy import usage_accumulator
    from src.repositories import llm_usage_repo

    ctx = broker_agent_session(model="claude-opus-4-7", token_budget_monthly=100_000)

    real_cls = httpx.AsyncClient

    class _SSEUsageClient(_StreamShimMixin):
        def __init__(self, *a, **k):
            self._real = real_cls(*a, **k) if "transport" in k else None

        async def __aenter__(self):
            return await self._real.__aenter__() if self._real else self

        async def __aexit__(self, *a):
            return await self._real.__aexit__(*a) if self._real else False

        async def send(self, req, stream=False):
            class _R:
                status_code = 200
                headers = {"content-type": "text/event-stream"}

                async def aiter_bytes(self):
                    yield (
                        b"event: message_start\n"
                        b'data: {"type":"message_start","message":{"model":"claude-opus-4-7",'
                        b'"usage":{"input_tokens":11,"output_tokens":0}}}\n\n'
                    )
                    yield b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n'
                    yield (b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n')

                async def aclose(self):
                    pass

            return _R()

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _SSEUsageClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {ctx['tok']}"},
                json={"model": "claude-opus-4-7", "messages": [], "stream": True},
            )

    r = asyncio.run(_run())
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert b"message_delta" in r.content

    usage_accumulator.flush()
    rows = llm_usage_repo().list_for_agent(ctx["agent_id"])
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 11
    assert rows[0]["output_tokens"] == 7
