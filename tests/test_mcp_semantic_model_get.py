"""MCP `semantic_model_get` (foundation tool) — issue #2153.

The export endpoint it wraps (`GET /api/semantic-models/{slug}.yaml`)
returns only the raw document; the only JSON surface exposing
`content_hash` is admin-only (`GET /api/admin/semantic-models/{model_id}`),
so a non-admin MCP caller had no way to obtain the authoritative hash for
provenance pinning. The export endpoint now carries it as an `ETag`
response header (RFC 7232, quoted) plus `X-Semantic-Model-Updated-At`; this
tool reads both and folds them into its response dict.

Uses the same ASGI-replay idiom as `tests/test_agent_profiles_mcp_parity.py`
(`mcp_env` fixture there) — the tool's internal `httpx.AsyncClient()`
self-call is routed into the same in-process `seeded_app` via
`ASGITransport`, so no real network hop and no second server.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

pytest.importorskip("mcp", reason="mcp package not installed")

# Schema-valid Ossie document (`datasets` is required, minItems 1 — see the
# note in tests/test_semantic_models_api.py).
DOC = (
    "version: '0.2.0.dev0'\n"
    "semantic_model:\n"
    "  - name: retail\n"
    "    datasets:\n"
    "      - name: orders\n"
    "        source: db.public.orders\n"
    "        fields: []\n"
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def mcp_env(seeded_app, monkeypatch):
    """`(call_tool)` — invokes a registered foundation tool directly, with
    its internal `httpx.AsyncClient()` self-call routed into the SAME
    in-process `seeded_app` via `ASGITransport`. Mirrors the fixture in
    `tests/test_agent_profiles_mcp_parity.py`."""
    app = seeded_app["client"].app

    _RealAsyncClient = httpx.AsyncClient

    def _asgi_async_client(*args, **kwargs):
        return _RealAsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    monkeypatch.setattr(httpx, "AsyncClient", _asgi_async_client)

    import app.api.mcp_http as mcp_mod

    def call_tool(name: str, token: str, **kwargs):
        tok = mcp_mod._current_token.set(token)
        try:
            fn = getattr(mcp_mod, name)
            return asyncio.run(fn(**kwargs))
        finally:
            mcp_mod._current_token.reset(tok)

    return call_tool


def test_semantic_model_get_content_hash_matches_the_store(mcp_env, seeded_app):
    from src.repositories import semantic_model_repo

    c = seeded_app["client"]
    created = c.post(
        "/api/admin/semantic-models",
        json={"document": DOC},
        headers=_auth(seeded_app["admin_token"]),
    ).json()

    result = mcp_env("semantic_model_get", seeded_app["admin_token"], slug="retail")

    row = semantic_model_repo().get(created["id"])
    assert result["slug"] == "retail"
    assert result["document"] == DOC
    assert result["content_hash"] == row["content_hash"]
    assert result["content_hash"] == hashlib.sha256(DOC.encode()).hexdigest()
    assert result["updated_at"] == row["updated_at"].isoformat()


def test_semantic_model_get_falls_back_to_computing_hash_when_no_etag(monkeypatch):
    """An older server that doesn't send the `ETag` header still yields a
    usable hash — export is byte-for-byte, so hashing the response body
    equals the stored `content_hash` the header would have carried."""
    import app.api.mcp_http as mcp_mod

    document = "version: '0.2.0.dev0'\nsemantic_model: []\n"

    class _FakeResponse:
        status_code = 200
        text = document
        content = document.encode()
        headers: dict = {}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeClient())

    tok = mcp_mod._current_token.set("fake-token")
    try:
        result = asyncio.run(mcp_mod.semantic_model_get(slug="retail"))
    finally:
        mcp_mod._current_token.reset(tok)

    assert result["content_hash"] == hashlib.sha256(document.encode()).hexdigest()
    assert "updated_at" not in result
