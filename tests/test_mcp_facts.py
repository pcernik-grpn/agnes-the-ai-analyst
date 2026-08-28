"""MCP-surface flag/auth behavior for `fact_search`/`fact_neighbors`/
`fact_claims` (build order step 6 of
docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md).

These tools call `src.repositories.facts_repo()` directly rather than
following the HTTP-self-call pattern every other foundation tool uses (see
`_facts_caller`'s docstring in `app/api/mcp/foundation_tools.py`), so no
ASGITransport/httpx.AsyncClient patching is needed here — just set
`mcp_http._current_token` and call the tool function.

RBAC-narrowing behavior (the restricted-`AgentPrincipal` case, spec §5's
S5 acceptance test) needs a live Postgres backend (`facts_pg.py` is
PG-only, A3 ratchet) and lives in `tests/db_pg/test_facts_mcp_pg.py`.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp", reason="mcp package not installed")

from fastapi import HTTPException


@pytest.fixture
def mcp_env(e2e_env, monkeypatch):
    """Isolated DATA_DIR/env, no DB needed for these tests — every path
    below fails before touching the repository (flag-off short-circuits at
    the router-level-equivalent check; a bad token fails token
    verification, which needs no DB read)."""
    import app.api.mcp_http as mcp_mod

    def call_tool(name: str, token: str, **kwargs):
        tok = mcp_mod._current_token.set(token)
        try:
            fn = getattr(mcp_mod, name)
            return asyncio.run(fn(**kwargs))
        finally:
            mcp_mod._current_token.reset(tok)

    return call_tool


def test_fact_search_flag_off_raises_404(mcp_env, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "0")
    with pytest.raises(HTTPException) as excinfo:
        mcp_env("fact_search", "irrelevant-token")
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "facts_disabled"


def test_fact_neighbors_flag_off_raises_404(mcp_env, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "0")
    with pytest.raises(HTTPException) as excinfo:
        mcp_env("fact_neighbors", "irrelevant-token", subject_id="f_1")
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "facts_disabled"


def test_fact_claims_flag_off_raises_404(mcp_env, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "0")
    with pytest.raises(HTTPException) as excinfo:
        mcp_env("fact_claims", "irrelevant-token", subject_id="f_1")
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "facts_disabled"


def test_fact_search_bad_token_raises_permission_error_when_flag_on(mcp_env, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    with pytest.raises(PermissionError) as excinfo:
        mcp_env("fact_search", "not-a-real-token")
    assert "could not authenticate" in str(excinfo.value)


def test_facts_disabled_is_checked_before_authentication(mcp_env, monkeypatch):
    """Mirrors the REST router: `require_facts_enabled` is the FIRST gate,
    so a caller with no credential at all still sees `facts_disabled`
    rather than an auth error — same "close the whole surface" posture as
    `require_agent_profiles_enabled` (app/auth/access.py)."""
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "0")
    with pytest.raises(HTTPException) as excinfo:
        mcp_env("fact_claims", "", subject_id="f_1")
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "facts_disabled"
