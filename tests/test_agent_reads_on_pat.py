"""READ-only agent-management endpoints accept a user PAT (B4).

`agnes agent list` — the command `agnes chat`'s own error text points a
caller at — is the discovery entry point into the agent-profile API. Before
this change, every `/api/v1/agents*` route (mutations and reads alike) ran
through `require_session_token`, which rejects every PAT flavor outright —
so a normally-logged-in analyst holding only a PAT (no fresh interactive
session) could never even list their own agents.

This module covers the `require_session_or_user_pat` dependency factory
(`app/auth/dependencies.py`) wired onto the READ-only routes:

- `GET /api/v1/agents`, `GET /api/v1/agents/{id}`,
  `GET /api/v1/agents/{slug}/schedules` accept BOTH a full-surface
  (`surface='all'`) PAT and a `surface='stack'` PAT (`allow_stack_surface=
  True`) — `agnes login` / `agnes init` mint `surface='stack'` by default
  (`app/api/cli_auth.py`), so accepting only `surface='all'` would not
  actually fix the common case. `surface='stack'` narrows *data reads*
  (`src/rbac.py`), never the caller's own agent metadata.
- `GET /api/v1/agents/{id}/memories` stays on the conservative default
  (`allow_stack_surface=False`) — a memory notebook can hold free-text
  content, the most sensitive of the four PAT-readable surfaces.

Mutations (`POST /api/v1/agents`, ...) stay session-token-only regardless
of PAT surface.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import get_system_db
    from src.repositories import agents_repo
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    conn.close()

    client = TestClient(shared_app)
    agent = agents_repo().get_or_create_default("owner1")
    return {"client": client, "user": {"id": "owner1", "email": "owner@test.com"}, "agent_id": agent["id"]}


def _mint_user_pat(user: dict, *, surface: str = "all", token_id: str | None = None) -> str:
    from src.repositories import access_token_repo

    token_id = token_id or str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        token_id=token_id,
        typ="pat",
    )
    access_token_repo().create(
        id=token_id,
        user_id=user["id"],
        name="test-pat",
        token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
        surface=surface,
    )
    return jwt_token


def _mint_agent_pat(user: dict, agent_id: str, *, token_id: str | None = None) -> str:
    from src.repositories import access_token_repo

    token_id = token_id or str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        token_id=token_id,
        typ="agent_pat",
        extra_claims={"agent_id": agent_id},
    )
    access_token_repo().create(
        id=token_id,
        user_id=user["id"],
        name="test-agent-pat",
        token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
        agent_id=agent_id,
    )
    return jwt_token


def _mint_sandbox_shaped_token(user: dict, *, chat_session_id: str = "sess-1") -> str:
    """Same shape as `app.auth.access.mint_session_jwt` (the chat-sandbox
    token injected into a spawned runner): NO `typ` claim at all,
    `scope="chat"` + `chat_session_id`. `resolve_token_to_user`
    (app/auth/pat_resolver.py) stashes `credential_surface="stack"` for any
    session-shaped credential carrying `scope in ("chat", "mcp-oauth")` —
    not just PATs."""
    return create_access_token(
        user_id=user["id"],
        email=user["email"],
        extra_claims={"scope": "chat", "chat_session_id": chat_session_id},
    )


def _mint_mcp_oauth_shaped_token(user: dict) -> str:
    """Same shape as an MCP-OAuth connector access token
    (app/auth/mcp_oauth.py): `typ="session"`, `scope="mcp-oauth"` — also
    stashed `credential_surface="stack"` by the resolver."""
    return create_access_token(
        user_id=user["id"],
        email=user["email"],
        typ="session",
        extra_claims={"scope": "mcp-oauth"},
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# A full-surface user PAT can read
# ---------------------------------------------------------------------------


def test_list_agents_with_user_pat_returns_200(env):
    token = _mint_user_pat(env["user"])
    r = env["client"].get("/api/v1/agents", headers=_auth(token))
    assert r.status_code == 200
    slugs = [a["slug"] for a in r.json()["data"]]
    assert "default" in slugs


def test_get_agent_with_user_pat_returns_200(env):
    token = _mint_user_pat(env["user"])
    r = env["client"].get(f"/api/v1/agents/{env['agent_id']}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["id"] == env["agent_id"]


def test_list_schedules_with_user_pat_returns_200(env):
    token = _mint_user_pat(env["user"])
    r = env["client"].get("/api/v1/agents/default/schedules", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"data": [], "has_more": False, "next_cursor": None}


def test_list_memories_with_user_pat_returns_200(env):
    token = _mint_user_pat(env["user"])
    r = env["client"].get(f"/api/v1/agents/{env['agent_id']}/memories", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"data": [], "has_more": False, "next_cursor": None}


# ---------------------------------------------------------------------------
# A surface='stack' PAT (the agnes login / agnes init default) can also
# read list/get/schedules — but NOT memories.
# ---------------------------------------------------------------------------


def test_list_agents_with_stack_pat_returns_200(env):
    token = _mint_user_pat(env["user"], surface="stack")
    r = env["client"].get("/api/v1/agents", headers=_auth(token))
    assert r.status_code == 200
    slugs = [a["slug"] for a in r.json()["data"]]
    assert "default" in slugs


def test_get_agent_with_stack_pat_returns_200(env):
    token = _mint_user_pat(env["user"], surface="stack")
    r = env["client"].get(f"/api/v1/agents/{env['agent_id']}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["id"] == env["agent_id"]


def test_list_schedules_with_stack_pat_returns_200(env):
    token = _mint_user_pat(env["user"], surface="stack")
    r = env["client"].get("/api/v1/agents/default/schedules", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"data": [], "has_more": False, "next_cursor": None}


# ---------------------------------------------------------------------------
# Non-PAT credentials tagged credential_surface='stack' (chat-sandbox
# mint_session_jwt tokens, MCP-OAuth connector tokens) must be held to the
# exact same rule as a surface='stack' PAT — allowed on list/get/schedules,
# denied on memories. The surface check must not be gated on typ="pat".
# ---------------------------------------------------------------------------


def test_list_agents_with_sandbox_shaped_token_returns_200(env):
    token = _mint_sandbox_shaped_token(env["user"])
    r = env["client"].get("/api/v1/agents", headers=_auth(token))
    assert r.status_code == 200
    slugs = [a["slug"] for a in r.json()["data"]]
    assert "default" in slugs


def test_memories_denied_for_sandbox_shaped_token(env):
    token = _mint_sandbox_shaped_token(env["user"])
    r = env["client"].get(f"/api/v1/agents/{env['agent_id']}/memories", headers=_auth(token))
    assert r.status_code == 403


def test_list_agents_with_mcp_oauth_shaped_token_returns_200(env):
    token = _mint_mcp_oauth_shaped_token(env["user"])
    r = env["client"].get("/api/v1/agents", headers=_auth(token))
    assert r.status_code == 200
    slugs = [a["slug"] for a in r.json()["data"]]
    assert "default" in slugs


def test_memories_denied_for_mcp_oauth_shaped_token(env):
    token = _mint_mcp_oauth_shaped_token(env["user"])
    r = env["client"].get(f"/api/v1/agents/{env['agent_id']}/memories", headers=_auth(token))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Everything that must NOT gain read access
# ---------------------------------------------------------------------------


def test_agent_scoped_pat_still_denied_on_agent_detail(env):
    token = _mint_agent_pat(env["user"], env["agent_id"])
    r = env["client"].get(f"/api/v1/agents/{env['agent_id']}", headers=_auth(token))
    assert r.status_code == 403


def test_stack_narrowed_pat_still_denied_on_memories(env):
    token = _mint_user_pat(env["user"], surface="stack")
    r = env["client"].get(f"/api/v1/agents/{env['agent_id']}/memories", headers=_auth(token))
    assert r.status_code == 403


def test_mutation_still_denied_for_full_surface_user_pat(env):
    token = _mint_user_pat(env["user"])
    r = env["client"].post("/api/v1/agents", json={"name": "Sales", "slug": "sales"}, headers=_auth(token))
    assert r.status_code == 403


def test_mutation_still_denied_for_stack_surface_user_pat(env):
    token = _mint_user_pat(env["user"], surface="stack")
    r = env["client"].post("/api/v1/agents", json={"name": "Sales", "slug": "sales"}, headers=_auth(token))
    assert r.status_code == 403


def test_delete_still_denied_for_full_surface_user_pat(env):
    token = _mint_user_pat(env["user"])
    r = env["client"].delete(f"/api/v1/agents/{env['agent_id']}", headers=_auth(token))
    assert r.status_code == 403


def test_list_agents_without_credential_still_401(env):
    r = env["client"].get("/api/v1/agents")
    assert r.status_code == 401
