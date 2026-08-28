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


def _replay_via_broker(broker_app, tok: str, method: str, path: str):
    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={"method": method, "path": path, "body": None},
            )

    return asyncio.run(_run())


def _seed_admin_session(email: str, user_id: str) -> str:
    """A user in the Admin group + a web chat session; returns a main ticket."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    UserRepository(conn).create(id=user_id, email=email, name="Broker Admin")
    admin_row = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()
    assert admin_row is not None
    UserGroupMembersRepository(conn).add_member(user_id, admin_row[0], source="system_seed")
    conn.close()
    session = chat_session_repo().create_session(user_email=email, surface=Surface.WEB)
    return ticket_repo().mint(session.id, "main")


def test_admin_read_route_replayed_for_admin(broker_app, e2e_env):
    """Read-only (GET) admin routes ARE brokered: the replay runs under the
    resolved identity and the route's own require_admin passes for a real
    admin — so `agnes admin list-users` (GET /api/users, off-prefix) and
    `agnes admin list-tables` (GET /api/admin/registry, on-prefix) both work
    from a chat sandbox. Mutations stay refused (test above)."""
    tok = _seed_admin_session("broker_admin_r@test.com", "broker_admin_r1")

    r = _replay_via_broker(broker_app, tok, "GET", "/api/users")
    assert r.status_code == 200, r.text
    assert any(u.get("email") == "broker_admin_r@test.com" for u in r.json())

    r2 = _replay_via_broker(broker_app, tok, "GET", "/api/admin/registry")
    assert r2.status_code == 200, r2.text


def test_admin_read_route_still_requires_admin_downstream(broker_app, e2e_env):
    """The broker adds no privilege on the read path: a NON-admin identity's
    GET replay reaches the route and gets the route's own require_admin 403
    ("Admin access required"), not the broker's refusal detail."""
    conn = get_system_db()
    UserRepository(conn).create(id="broker_plain1", email="broker_plain@test.com", name="Plain User")
    conn.close()
    session = chat_session_repo().create_session(user_email="broker_plain@test.com", surface=Surface.WEB)
    tok = ticket_repo().mint(session.id, "main")

    r = _replay_via_broker(broker_app, tok, "GET", "/api/users")
    assert r.status_code == 403, r.text
    assert r.json().get("detail") == "Admin access required"


def test_admin_read_switch_off_refuses_even_get(broker_app, e2e_env, monkeypatch):
    """`chat.broker_admin_reads` off (env kill-switch) restores the old
    behavior: even a read-only admin GET from an actual admin is refused by
    the broker's own gate."""
    monkeypatch.setenv("AGNES_CHAT_BROKER_ADMIN_READS", "0")
    tok = _seed_admin_session("broker_admin_off@test.com", "broker_admin_off1")

    r = _replay_via_broker(broker_app, tok, "GET", "/api/users")
    assert r.status_code == 403, r.text
    assert r.json().get("detail") == "admin_mutations_require_interactive_auth"


def test_no_admin_get_route_mints_a_credential(broker_app):
    """The brokered admin-READ surface is safe only because "never mutate on
    GET" holds: `_replay` lets every GET/HEAD admin route through and leans on
    the method as the read/write boundary. A GET that MINTS something breaks
    that premise, and breaks it in the worst direction — the chat sandbox runs
    an agent any document it reads can prompt-inject, so a brokered GET that
    returns a credential is an exfiltration path, not a theoretical one.

    `GET /admin/chat/{chat_id}/tail-ticket` was exactly that: it minted a live
    one-shot ticket for `/admin/chat/{chat_id}/tail`, the WebSocket that tails
    ANY user's session. It is a POST now. This guard is for the next one.
    """
    import inspect
    import re

    from app.api.broker import _ADMIN_READ_METHODS, _ADMIN_PATH_PREFIX, _route_requires_admin

    # Anything that hands a caller a fresh bearer-ish secret. Deliberately
    # narrow: a CSRF token minted for a rendered form is not this (it is
    # inert without the session cookie it is bound to).
    MINTS = re.compile(r"_issue_\w*ticket|_mint_(?!web_csrf)\w+|\bmint_\w*(?:jwt|token|ticket)|create_access_token")

    offenders = []
    for route in broker_app.routes:
        methods = getattr(route, "methods", set()) or set()
        readable = _ADMIN_READ_METHODS & {str(m).upper() for m in methods}
        if not readable:
            continue
        path = getattr(route, "path", "")
        if not (path.startswith(_ADMIN_PATH_PREFIX) or _route_requires_admin(broker_app, "GET", path)):
            continue
        fn = getattr(route, "endpoint", None)
        if fn is None:
            continue
        try:
            body = inspect.getsource(fn)
        except (OSError, TypeError):  # pragma: no cover — C/builtin endpoint
            continue
        found = MINTS.findall(body)
        if found:
            offenders.append(f"{sorted(readable)} {path} -> {sorted(set(found))}")

    assert not offenders, (
        "these admin routes mint a credential on a READ method, which the "
        "brokered admin-read surface in app/api/broker.py would hand to a chat "
        "sandbox; make them POST:\n  " + "\n  ".join(offenders)
    )


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
    from app.auth import wif

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

    from app.auth import wif

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
    from app.auth import wif

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
# Guard/destination-mismatch fixes: the value used to CLASSIFY the upstream
# subpath (model allowlist / budget / dispatcher selection) and the value used
# to BUILD the outbound URL must be one and the same canonical path. httpx
# collapses dot-segments and duplicate slashes at send time, so any divergence
# lets a bound agent slip past its pinned-model allowlist + monthly budget.
# ---------------------------------------------------------------------------


def test_normalize_upstream_path_rejects_dot_segments_and_backslash():
    """Unit (Finding A): a literal ``.``/``..`` dot-segment or backslash in the
    Anthropic subpath is REFUSED (400), not silently canonicalized. httpx
    collapses ``/v1/./messages`` -> ``/v1/messages`` at send time, so accepting
    the dot-segment form (which the old strip-empty-only normalizer classified
    as NON-message) would reach the real messages operation while skipping the
    per-agent model allowlist and monthly budget."""
    from fastapi import HTTPException

    from app.api.broker import _normalize_upstream_path

    # legitimate slash normalization still works (unchanged behavior)
    assert _normalize_upstream_path("/v1/messages") == "/v1/messages"
    assert _normalize_upstream_path("/v1/messages/") == "/v1/messages"
    assert _normalize_upstream_path("//v1//messages") == "/v1/messages"
    assert _normalize_upstream_path("/") == "/"

    for bad in (
        "/v1/./messages",
        "/v1/../messages",
        "/./v1/messages",
        "/v1/messages/..",
        "/v1/messages/.",
        "/v1/%2e/messages",
        "/v1/%2e%2e/messages",
        "/v1\\messages",
    ):
        with pytest.raises(HTTPException) as ei:
            _normalize_upstream_path(bad)
        assert ei.value.status_code == 400, bad
        assert ei.value.detail == "broker_upstream_path_invalid", bad


def test_anthropic_proxy_dot_segment_path_refused_before_forward(broker_app, monkeypatch):
    """Finding A end-to-end: a dot-segment subpath reaching the handler (which
    httpx would canonicalize to /v1/messages at send time) is REFUSED with 400
    before any upstream call — it can never classify as non-message and slip
    past the model allowlist / budget / dispatcher gate.

    Built with a hand-crafted ASGI scope because httpx's ASGITransport collapses
    the dot-segment client-side, so a normal TestClient request would never let
    the raw form reach the handler."""
    from fastapi import HTTPException
    from starlette.requests import Request

    import app.api.broker as broker_mod

    def _must_not_construct(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("outbound client must not be built for a dot-segment path")

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _must_not_construct)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/broker/anthropic/v1/./messages",
        "raw_path": b"/api/broker/anthropic/v1/./messages",
        "query_string": b"",
        "headers": [],
        "app": broker_app,
    }

    async def _receive():
        return {"type": "http.request", "body": b'{"model":"x"}', "more_body": False}

    request = Request(scope, _receive)
    row = {"scope": "main", "session_id": "dot-seg-session"}

    with pytest.raises(HTTPException) as ei:
        asyncio.run(broker_mod.anthropic_proxy(request, row))
    assert ei.value.status_code == 400
    assert ei.value.detail == "broker_upstream_path_invalid"


def test_dispatcher_trailing_slash_classification_and_url_agree(broker_app, monkeypatch):
    """Finding B: a trailing-slash message path (``/v1/messages/``) classifies
    as the dispatcher route AND the built outbound URL is the SAME canonical
    ``/v1/messages`` (no trailing slash). Before the fix the outbound URL was
    built from the un-normalized subpath, so authorization/dispatcher selection
    and the final destination disagreed in shape."""
    import app.api.broker as broker_mod

    monkeypatch.setenv("LLM_DISPATCHER_URL", "http://127.0.0.1:8600")
    monkeypatch.setenv("LLM_DISPATCHER_API_KEY", "agnes-team-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-KEY")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_broker_anthropic(broker_app, "/v1/messages/", "chat_disp_slash")
    assert r.status_code == 200
    # classified as the dispatcher route (trailing slash collapsed for the gate)
    h = _lower_keys(_UrlCapturingClient._captured)
    assert h.get("x-api-key") == "agnes-team-key"
    # ...and the destination matches that classification, canonicalized
    assert _UrlCapturingClient._captured_url == "http://127.0.0.1:8600/v1/messages"


def test_duplicate_slash_message_url_canonical(broker_app, monkeypatch):
    """Finding B: a duplicate-slash message path forwards to the canonical
    ``/v1/messages`` destination, not the raw ``//v1//messages`` string."""
    import app.api.broker as broker_mod

    monkeypatch.delenv("LLM_DISPATCHER_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-KEY")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_broker_anthropic(broker_app, "//v1//messages", "chat_dup_slash")
    assert r.status_code == 200
    assert _UrlCapturingClient._captured_url == "https://api.anthropic.com/v1/messages"


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


def test_agentless_session_costs_no_caller_lookup(broker_app, broker_agent_session, e2e_env, monkeypatch):
    """C2.4 must not tax sessions it does not serve. A Slack/legacy session
    with no bound agent discards both halves of the result (every
    `caller_user_id` consumer sits behind `agent_row is not None`), so the
    user lookup must not run at all for it — the cost promised by
    `_agent_and_caller_for_ticket`'s docstring.

    The agent-bound half is asserted too, so the test fails if the lookup is
    dropped entirely rather than merely made conditional."""
    import app.api.broker as broker_mod

    real_users_repo = broker_mod.users_repo
    calls: list = []

    def _counting_users_repo():
        calls.append(1)
        return real_users_repo()

    monkeypatch.setattr(broker_mod, "users_repo", _counting_users_repo)

    # No bound agent -> zero user lookups.
    tag = uuid.uuid4().hex[:8]
    email = f"broker_agentless_{tag}@test.com"
    conn = get_system_db()
    UserRepository(conn).create(id=f"broker_agentless_user_{tag}", email=email, name="Agentless")
    conn.close()
    plain = chat_session_repo().create_session(user_email=email, surface=Surface.WEB)

    agent_row, caller_user_id = broker_mod._agent_and_caller_for_ticket({"session_id": plain.id})
    assert agent_row is None
    assert caller_user_id is None
    assert calls == [], "agent-less session must not pay for a caller lookup"

    # Bound agent -> the lookup still happens and still attributes.
    ctx = broker_agent_session()
    agent_row, caller_user_id = broker_mod._agent_and_caller_for_ticket({"session_id": ctx["session_id"]})
    assert agent_row is not None and agent_row["id"] == ctx["agent_id"]
    assert caller_user_id == ctx["user_id"]
    assert len(calls) == 1


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

    from app import instance_config

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

    from app import instance_config

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


# ---------------------------------------------------------------------------
# Vertex mode (chat.llm.provider: vertex)
# ---------------------------------------------------------------------------

_VERTEX_NATIVE_PATH = (
    "/v1/projects/proj-1/locations/europe-west1/publishers/anthropic/models/claude-sonnet-4-5@20250929:streamRawPredict"
)


class _BodyCapturingClient(_UrlCapturingClient):
    """_UrlCapturingClient that additionally records the outbound body, so the
    vertex Messages-compat tests can assert the rewrite."""

    _captured_body: bytes = b""

    async def request(self, method, url, *a, **k):
        if not self._real:
            _BodyCapturingClient._captured_body = k.get("content") or b""
        return await super().request(method, url, *a, **k)


@pytest.fixture
def vertex_chat_config(broker_app, monkeypatch):
    """Flip the shared app into vertex mode (restored afterwards) and stub the
    Google token mint — no ADC anywhere in tests."""
    import types

    from app.auth import vertex_gcp

    prev = getattr(broker_app.state, "chat_config", None)
    broker_app.state.chat_config = types.SimpleNamespace(
        llm_auth="api_key",
        llm_provider="vertex",
        vertex_project_id="proj-1",
        vertex_region="europe-west1",
        agent_api_utility_models=[],
        agent_api_budget_cache_ttl_s=60,
    )
    monkeypatch.setattr(vertex_gcp, "get_vertex_access_token", lambda: "gcp-tok-1")
    yield broker_app.state.chat_config
    broker_app.state.chat_config = prev


def _post_vertex(broker_app, subpath, ticket_label, body: bytes = b'{"model":"x"}'):
    _HeaderCapturingClient._captured = {}
    _UrlCapturingClient._captured_url = ""
    _BodyCapturingClient._captured_body = b""
    tok = ticket_repo().mint(ticket_label, "main", ttl_seconds=60)

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                f"/api/broker/anthropic{subpath}",
                headers={"Authorization": f"Bearer {tok}"},
                content=body,
            )

    return asyncio.run(_run())


def test_vertex_native_path_forwards_with_google_bearer(broker_app, monkeypatch, vertex_chat_config):
    """A native Vertex model path forwards to the regional Vertex host with a
    Google Bearer token — no x-api-key, no oauth beta header, no Anthropic
    credential involved at all."""
    import app.api.broker as broker_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-KEY")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_vertex(broker_app, _VERTEX_NATIVE_PATH, "chat_vx1")
    assert r.status_code == 200, r.text
    assert _UrlCapturingClient._captured_url == ("https://europe-west1-aiplatform.googleapis.com" + _VERTEX_NATIVE_PATH)
    h = _lower_keys(_UrlCapturingClient._captured)
    assert h.get("authorization") == "Bearer gcp-tok-1"
    assert "x-api-key" not in h
    assert "oauth-2025-04-20" not in h.get("anthropic-beta", "")


def test_vertex_native_path_without_v1_prefix_is_canonicalized(broker_app, monkeypatch, vertex_chat_config):
    """The Anthropic SDK's Vertex client emits /projects/... against a /v1
    base; the broker rebuilds the canonical /v1 form."""
    import app.api.broker as broker_mod

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_vertex(broker_app, _VERTEX_NATIVE_PATH.removeprefix("/v1"), "chat_vx2")
    assert r.status_code == 200, r.text
    assert _UrlCapturingClient._captured_url == ("https://europe-west1-aiplatform.googleapis.com" + _VERTEX_NATIVE_PATH)


def test_vertex_global_region_host(broker_app, monkeypatch, vertex_chat_config):
    import app.api.broker as broker_mod

    vertex_chat_config.vertex_region = "global"
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    path = _VERTEX_NATIVE_PATH.replace("europe-west1", "global")
    r = _post_vertex(broker_app, path, "chat_vx3")
    assert r.status_code == 200, r.text
    assert _UrlCapturingClient._captured_url == "https://aiplatform.googleapis.com" + path


def test_vertex_foreign_project_rejected_403(broker_app, monkeypatch, vertex_chat_config):
    """The sandbox can never point spend at another project/region — the
    broker pins them by equality against instance config."""
    import app.api.broker as broker_mod

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_vertex(broker_app, _VERTEX_NATIVE_PATH.replace("proj-1", "attacker-proj"), "chat_vx4")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "vertex_target_not_allowed"
    assert _UrlCapturingClient._captured_url == ""  # nothing forwarded


def test_vertex_unsupported_subpath_404(broker_app, monkeypatch, vertex_chat_config):
    """Vertex mode is fail-closed on subpaths, unlike anthropic mode's open
    forward."""
    import app.api.broker as broker_mod

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_vertex(broker_app, "/v1/models", "chat_vx5")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "vertex_path_not_supported"
    assert _UrlCapturingClient._captured_url == ""


def test_vertex_messages_compat_rewrite_streaming(broker_app, monkeypatch, vertex_chat_config):
    """A plain Messages-format POST (the kai-agent engine) is rewritten into
    the Vertex shape: model moves body→URL in the @-form, anthropic_version
    is injected, stream:true selects :streamRawPredict."""
    import json as _json

    import app.api.broker as broker_mod

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _BodyCapturingClient)

    body = _json.dumps({"model": "claude-sonnet-4-5-20250929", "stream": True, "max_tokens": 4}).encode()
    r = _post_vertex(broker_app, "/v1/messages", "chat_vx6", body=body)
    assert r.status_code == 200, r.text
    assert _UrlCapturingClient._captured_url == ("https://europe-west1-aiplatform.googleapis.com" + _VERTEX_NATIVE_PATH)
    sent = _json.loads(_BodyCapturingClient._captured_body)
    assert "model" not in sent
    assert sent["anthropic_version"] == "vertex-2023-10-16"
    assert sent["max_tokens"] == 4


def test_vertex_messages_compat_non_streaming_rawpredict(broker_app, monkeypatch, vertex_chat_config):
    import json as _json

    import app.api.broker as broker_mod

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _BodyCapturingClient)

    body = _json.dumps({"model": "claude-sonnet-4-6"}).encode()
    r = _post_vertex(broker_app, "/v1/messages", "chat_vx7", body=body)
    assert r.status_code == 200, r.text
    assert _UrlCapturingClient._captured_url.endswith("/models/claude-sonnet-4-6:rawPredict")


def test_vertex_messages_compat_invalid_body_400(broker_app, monkeypatch, vertex_chat_config):
    import app.api.broker as broker_mod

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _BodyCapturingClient)

    r = _post_vertex(broker_app, "/v1/messages", "chat_vx8", body=b'{"no_model": true}')
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "vertex_body_invalid"


def test_vertex_count_tokens_rewrite(broker_app, monkeypatch, vertex_chat_config):
    """count_tokens keeps the model IN the body (translated) and targets the
    count-tokens:rawPredict pseudo-model path."""
    import json as _json

    import app.api.broker as broker_mod

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _BodyCapturingClient)

    body = _json.dumps({"model": "claude-haiku-4-5-20251001", "messages": []}).encode()
    r = _post_vertex(broker_app, "/v1/messages/count_tokens", "chat_vx9", body=body)
    assert r.status_code == 200, r.text
    assert _UrlCapturingClient._captured_url == (
        "https://europe-west1-aiplatform.googleapis.com/v1/projects/proj-1/locations/europe-west1"
        "/publishers/anthropic/models/count-tokens:rawPredict"
    )
    sent = _json.loads(_BodyCapturingClient._captured_body)
    assert sent["model"] == "claude-haiku-4-5@20251001"


def test_vertex_dispatcher_env_is_ignored(broker_app, monkeypatch, vertex_chat_config, caplog):
    """LLM_DISPATCHER_URL must never divert vertex traffic — the dispatcher
    speaks the first-party Messages API only."""
    import app.api.broker as broker_mod

    monkeypatch.setenv("LLM_DISPATCHER_URL", "http://127.0.0.1:8600")
    monkeypatch.setenv("LLM_DISPATCHER_API_KEY", "agnes-team-key")
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_vertex(broker_app, _VERTEX_NATIVE_PATH, "chat_vx10")
    assert r.status_code == 200, r.text
    assert _UrlCapturingClient._captured_url.startswith("https://europe-west1-aiplatform.googleapis.com")
    h = _lower_keys(_UrlCapturingClient._captured)
    assert h.get("authorization") == "Bearer gcp-tok-1"
    assert h.get("x-api-key") is None


def test_vertex_token_failure_is_502_with_generic_detail(broker_app, monkeypatch, vertex_chat_config):
    import app.api.broker as broker_mod
    from app.auth import vertex_gcp

    def _boom():
        raise vertex_gcp.VertexAuthError("ADC chain detail that must not leak")

    monkeypatch.setattr(vertex_gcp, "get_vertex_access_token", _boom)
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)

    r = _post_vertex(broker_app, _VERTEX_NATIVE_PATH, "chat_vx11")
    assert r.status_code == 502
    assert "ADC chain detail" not in r.text
    assert _UrlCapturingClient._captured_url == ""


def test_vertex_401_clears_google_token_cache(broker_app, monkeypatch, vertex_chat_config):
    import app.api.broker as broker_mod
    from app.auth import vertex_gcp

    class _Unauthorized(_UrlCapturingClient):
        async def request(self, method, url, *a, **k):
            if self._real:
                return await self._real.request(method, url, *a, **k)
            _UrlCapturingClient._captured_url = str(url)

            class _R:
                status_code = 401
                headers = {"content-type": "application/json"}
                content = b'{"error": {"message": "expired"}}'

            return _R()

    cleared = {"n": 0}
    monkeypatch.setattr(vertex_gcp, "clear_token_cache", lambda: cleared.__setitem__("n", cleared["n"] + 1))
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _Unauthorized)

    r = _post_vertex(broker_app, _VERTEX_NATIVE_PATH, "chat_vx12")
    assert r.status_code == 401
    assert cleared["n"] == 1


def test_vertex_model_policy_from_url(broker_app, monkeypatch, vertex_chat_config, broker_agent_session):
    """On a native Vertex path the pinned-model gate reads the model from the
    URL — and both id spellings compare equal."""
    import app.api.broker as broker_mod

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)
    ctx = broker_agent_session(model="claude-sonnet-4-5-20250929")

    async def _run(path):
        _UrlCapturingClient._captured_url = ""
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                f"/api/broker/anthropic{path}",
                headers={"Authorization": f"Bearer {ctx['tok']}"},
                content=b"{}",
            )

    r = asyncio.run(_run(_VERTEX_NATIVE_PATH))  # @-form of the pinned dash-form
    assert r.status_code == 200, r.text

    foreign = _VERTEX_NATIVE_PATH.replace("claude-sonnet-4-5@20250929", "claude-opus-4-7")
    r = asyncio.run(_run(foreign))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "model_not_allowed"
    assert _UrlCapturingClient._captured_url == ""


def test_vertex_budget_exhausted_429_on_native_path(broker_app, monkeypatch, vertex_chat_config, broker_agent_session):
    """A native Vertex completion is a token-spending call — the monthly
    budget gate fires before anything is forwarded."""
    import app.api.broker as broker_mod

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _UrlCapturingClient)
    ctx = broker_agent_session(model="claude-sonnet-4-5-20250929", token_budget_monthly=0)
    _UrlCapturingClient._captured_url = ""

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                f"/api/broker/anthropic{_VERTEX_NATIVE_PATH}",
                headers={"Authorization": f"Bearer {ctx['tok']}"},
                content=b"{}",
            )

    r = asyncio.run(_run())
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "budget_exhausted"
    assert _UrlCapturingClient._captured_url == ""


def test_vertex_messages_compat_traversal_model_rejected_400(broker_app, monkeypatch, vertex_chat_config):
    """A body model crafted to escape publishers/anthropic via dot-segments
    (httpx collapses '../' in the outbound URL) is refused before anything is
    forwarded — the broker's signed Google credential must never ride a
    caller-chosen path."""
    import json as _json

    import app.api.broker as broker_mod

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _BodyCapturingClient)

    body = _json.dumps({"model": "claude-x/../../../../publishers/google/models/gemini-pro", "stream": True}).encode()
    r = _post_vertex(broker_app, "/v1/messages", "chat_vx13", body=body)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "vertex_body_invalid"
    assert _UrlCapturingClient._captured_url == ""  # nothing forwarded
