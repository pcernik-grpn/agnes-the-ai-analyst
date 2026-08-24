"""MCP-surface parity for the Agent profiles feature flag.

`agent_list` / `agent_ask` (`app/api/mcp/foundation_tools.py`) are thin HTTP
proxies onto `GET /api/v1/agents` / `POST /api/v1/agents/{slug}/responses` —
both live on the router-level-guarded surface (`tests/test_agent_profiles_
flag.py`). Since APIRoute merges router-level `dependencies` in FRONT of a
route's own `Depends(...)` parameters (they run first), the flag guard fires
before the runtime-principal auth dependency that would otherwise reject a
PAT credential — so this holds even over the PAT-authenticated SSE transport
the foundation tools' docstrings otherwise call out as 403-for-a-different-
reason. Mirrors the `preview_env` ASGI-replay idiom in
`tests/test_data_apps_preview.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import httpx
import pytest


def _auth(pat: str) -> dict:
    return {"Authorization": f"Bearer {pat}"}


@pytest.fixture
def mcp_env(e2e_env, monkeypatch, shared_app):
    """`(call_tool)` — invokes a registered foundation tool directly, with
    its internal `httpx.AsyncClient()` self-calls routed into the SAME
    in-process app via `ASGITransport`."""
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        UserRepository(conn).create(id="owner1", email="owner@test.local", name="Owner")
        tid = str(uuid.uuid4())
        owner_pat = create_access_token("owner1", "owner@test.local", token_id=tid, typ="pat")
        AccessTokenRepository(conn).create(
            id=tid,
            user_id="owner1",
            name="owner1-pat",
            token_hash=hashlib.sha256(owner_pat.encode()).hexdigest(),
            prefix=tid.replace("-", "")[:8],
            expires_at=None,
        )
    finally:
        conn.close()

    app = shared_app

    _RealAsyncClient = httpx.AsyncClient

    def _asgi_async_client(*args, **kwargs):
        return _RealAsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    monkeypatch.setattr(httpx, "AsyncClient", _asgi_async_client)

    import app.api.mcp_http as mcp_mod

    def call_tool(name: str, **kwargs):
        token = mcp_mod._current_token.set(owner_pat)
        try:
            fn = getattr(mcp_mod, name)
            return asyncio.run(fn(**kwargs))
        finally:
            mcp_mod._current_token.reset(token)

    return call_tool


def test_agent_list_returns_agent_profiles_disabled_when_flag_off(mcp_env, monkeypatch):
    monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "0")
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        mcp_env("agent_list")
    resp = excinfo.value.response
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"kind": "agent_profiles_disabled"}


def test_agent_ask_returns_agent_profiles_disabled_when_flag_off(mcp_env, monkeypatch):
    monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "0")
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        mcp_env("agent_ask", slug="bot", prompt="hi")
    resp = excinfo.value.response
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"kind": "agent_profiles_disabled"}
