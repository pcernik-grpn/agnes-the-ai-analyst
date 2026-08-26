"""`POST /api/v1/sessions/{id}/memories` — the "remember" tool (V1c Task 4).

Covers write-mode branching (off/propose/auto), the size/rate/pending
guards, and the C2 binding correction: a broker-minted `chat_session_id`
claim must match the path `{id}` or the request is `403 session_mismatch`
— regardless of whether the two sessions share the same owner. Also covers
`app.chat.agent_profile._context_skill` advertising the remember tool only
when `memory_write_mode != "off"`.

Session rows are real (`chat_session_repo()`), same posture as
`tests/test_agent_sessions_api.py` — `require_session_principal` exercises
its actual DB-backed ownership check, not a mock. This endpoint never
touches the chat manager, so no `FakeManager` is needed here.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _chat_claim_token(chat_session_id: str) -> str:
    """Mint the token through the broker's real `_mint_identity_jwt` (solo
    path), not a hand-rolled shape — this is what
    `app.auth.dependencies._stash_chat_session_id_from_token` parks on
    `request.state.chat_session_id` — the in-sandbox-agent call shape.
    Exercising the actual mint function (rather than reconstructing its
    claim shape by hand) means a refactor that silently drops
    `chat_session_id` from the solo mint fails THIS test, not just the
    broker's own narrower assertion (M1)."""
    from app.api.broker import _mint_identity_jwt

    return _mint_identity_jwt(chat_session_id)


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from app.chat.types import Surface
    from src.db import SYSTEM_EVERYONE_GROUP, get_system_db
    from src.repositories import (
        agents_repo,
        chat_session_repo,
        resource_grants_repo,
        user_group_members_repo,
        user_groups_repo,
    )
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="other1", email="other@test.com", name="Other")
    conn.close()

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    user_group_members_repo().add_member("owner1", everyone["id"], source="system_seed")
    user_group_members_repo().add_member("other1", everyone["id"], source="system_seed")
    resource_grants_repo().create(everyone["id"], "chat", "chat")

    def _make_agent(slug: str, mode: str) -> str:
        agent_id = str(uuid.uuid4())
        agents_repo().create(
            id=agent_id,
            owner_user_id="owner1",
            name=slug,
            slug=slug,
            memory_write_mode=mode,
        )
        return agent_id

    off_agent = _make_agent("off-agent", "off")
    propose_agent = _make_agent("propose-agent", "propose")
    auto_agent = _make_agent("auto-agent", "auto")

    def _make_session(agent_id: str, user_email: str = "owner@test.com") -> str:
        session = chat_session_repo().create_session(user_email=user_email, surface=Surface.API, agent_id=agent_id)
        return session.id

    off_session = _make_session(off_agent)
    propose_session = _make_session(propose_agent)
    auto_session = _make_session(auto_agent)

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "other_token": create_access_token("other1", "other@test.com"),
        "off_agent": off_agent,
        "propose_agent": propose_agent,
        "auto_agent": auto_agent,
        "off_session": off_session,
        "propose_session": propose_session,
        "auto_session": auto_session,
        "make_agent": _make_agent,
        "make_session": _make_session,
    }


# ---------------------------------------------------------------------------
# memory_write_mode branching
# ---------------------------------------------------------------------------


def test_off_mode_returns_403(env):
    r = env["client"].post(
        f"/api/v1/sessions/{env['off_session']}/memories",
        json={"content": "note"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "memory_writes_disabled"

    from src.repositories import agent_memories_repo

    assert agent_memories_repo().list_for_agent(env["off_agent"]) == []


def test_propose_mode_returns_201_pending_and_not_active(env):
    r = env["client"].post(
        f"/api/v1/sessions/{env['propose_session']}/memories",
        json={"content": "remember this"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["id"]

    from src.repositories import agent_memories_repo

    repo = agent_memories_repo()
    pending = repo.list_for_agent(env["propose_agent"], status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] == body["id"]
    assert pending[0]["content"] == "remember this"
    assert pending[0]["activated_at"] is None

    assert repo.list_active(env["propose_agent"]) == []


def test_auto_mode_returns_201_active_and_in_list_active(env):
    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "remember this too"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "active"
    assert body["id"]

    from src.repositories import agent_memories_repo

    repo = agent_memories_repo()
    active = repo.list_active(env["auto_agent"])
    assert len(active) == 1
    assert active[0]["id"] == body["id"]
    assert active[0]["activated_at"] is not None


# ---------------------------------------------------------------------------
# Guards (all modes)
# ---------------------------------------------------------------------------


def test_empty_content_returns_422(env):
    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "   "},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 422


def test_oversize_content_returns_413(env):
    # `request.app.state.chat_config` is what the endpoint reads its limits
    # from (`app.api.agent_memory._chat_config`), falling back to
    # `ChatConfig()` defaults when unset — CHAT-INIT only populates it on a
    # real app *startup* (lifespan), which a bare `TestClient(create_app())`
    # (no `with` block) never triggers, same as the other chat-config tests
    # (e.g. `tests/test_chat_readiness.py`) set it explicitly.
    from app.chat.config import ChatConfig

    env["client"].app.state.chat_config = ChatConfig(agent_memory_max_chars=10)

    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "this content is definitely over ten characters"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "memory_too_large"


def test_over_hourly_rate_limit_returns_429(env):
    from app.chat.config import ChatConfig

    env["client"].app.state.chat_config = ChatConfig(agent_memory_writes_per_hour=2)

    for _ in range(2):
        r = env["client"].post(
            f"/api/v1/sessions/{env['auto_session']}/memories",
            json={"content": "note"},
            headers=_auth(env["owner_token"]),
        )
        assert r.status_code == 201

    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "one too many"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "memory_rate_limited"


def test_over_pending_cap_returns_429(env):
    from app.chat.config import ChatConfig

    env["client"].app.state.chat_config = ChatConfig(agent_memory_max_pending=2)
    # Rate limit default (20/hr) is well above the pending cap (2) here, so
    # the pending-cap guard is the one that trips.

    for _ in range(2):
        r = env["client"].post(
            f"/api/v1/sessions/{env['propose_session']}/memories",
            json={"content": "note"},
            headers=_auth(env["owner_token"]),
        )
        assert r.status_code == 201
        assert r.json()["status"] == "pending"

    r = env["client"].post(
        f"/api/v1/sessions/{env['propose_session']}/memories",
        json={"content": "one too many"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "memory_pending_full"


# ---------------------------------------------------------------------------
# C2: bind the write to the CALLING session, never the path {id}
# ---------------------------------------------------------------------------


def test_c2_broker_claim_for_different_agent_same_owner_returns_403_and_leaves_target_unchanged(env):
    """Agent A (auto) and agent B (off) both belong to owner1. A broker-
    minted token identifies the CALLING session as A's session
    (`chat_session_id=auto_session`), but the request path targets B's
    session. `require_session_principal` alone would authorize this (same
    owner) — the C2 check must still reject it, and B's notebook must stay
    untouched."""
    token = _chat_claim_token(env["auto_session"])

    r = env["client"].post(
        f"/api/v1/sessions/{env['off_session']}/memories",
        json={"content": "poison B's notebook"},
        headers=_auth(token),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "session_mismatch"

    from src.repositories import agent_memories_repo

    assert agent_memories_repo().list_for_agent(env["off_agent"]) == []
    # A's own notebook is untouched too — the call never targeted it either.
    assert agent_memories_repo().list_for_agent(env["auto_agent"]) == []


def test_c2_matching_broker_claim_succeeds(env):
    """Sanity counterpart: when the claim DOES match the path id, the call
    proceeds normally (uses the calling/path agent's own memory_write_mode)."""
    token = _chat_claim_token(env["auto_session"])

    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "legit note"},
        headers=_auth(token),
    )
    assert r.status_code == 201
    assert r.json()["status"] == "active"


def test_c2_no_claim_direct_owner_call_uses_path_session_normally(env):
    """No broker claim (plain interactive token) — the path id IS the
    calling session, already ownership-verified by require_session_principal;
    the C2 check must not interfere."""
    r = env["client"].post(
        f"/api/v1/sessions/{env['propose_session']}/memories",
        json={"content": "note via owner token"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201
    assert r.json()["status"] == "pending"


def test_cross_owner_direct_call_returns_404(env):
    """Baseline: an entirely different owner gets 404 (require_session_principal's
    own ownership check), before C2 even needs to run."""
    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "not yours"},
        headers=_auth(env["other_token"]),
    )
    assert r.status_code == 404


def test_unknown_session_returns_404(env):
    r = env["client"].post(
        "/api/v1/sessions/does-not-exist/memories",
        json={"content": "note"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# L5: audit trail — success and deny paths, especially the C2 prompt-
# injection signal (session_mismatch)
# ---------------------------------------------------------------------------


def _audit_params(row: dict) -> dict:
    """`audit_repo().query()` returns `params` as the raw stored JSON
    string, not a parsed dict (only some other repo methods parse it) —
    decode it here so tests can assert on the structured fields."""
    import json

    v = row.get("params")
    return json.loads(v) if isinstance(v, str) else (v or {})


def test_c2_mismatch_denial_is_audited(env):
    """The session_mismatch deny is a prompt-injection signal — it must
    leave a durable audit row, not just a 403 response."""
    from src.repositories import audit_repo

    token = _chat_claim_token(env["auto_session"])
    r = env["client"].post(
        f"/api/v1/sessions/{env['off_session']}/memories",
        json={"content": "poison B's notebook"},
        headers=_auth(token),
    )
    assert r.status_code == 403

    rows, _ = audit_repo().query(action="agent.memory.write", limit=50)
    matches = [row for row in rows if row["result"] == "denied:session_mismatch"]
    assert len(matches) == 1
    params = _audit_params(matches[0])
    assert params["agent_id"] == env["off_agent"]
    assert params["session_id"] == env["off_session"]
    assert params["calling_session_id"] == env["auto_session"]


def test_successful_write_is_audited_without_content(env):
    """A successful write is audited with content length, never the raw
    content (that would duplicate sensitive/PII notebook content into the
    audit log)."""
    from src.repositories import audit_repo

    r = env["client"].post(
        f"/api/v1/sessions/{env['auto_session']}/memories",
        json={"content": "remember this secret"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 201
    memory_id = r.json()["id"]

    rows, _ = audit_repo().query(action="agent.memory.write", limit=50)
    matches = [row for row in rows if _audit_params(row).get("memory_id") == memory_id]
    assert len(matches) == 1
    row = matches[0]
    params = _audit_params(row)
    assert row["result"] == "success:active"
    assert params["agent_id"] == env["auto_agent"]
    assert params["content_length"] == len("remember this secret")
    assert "content" not in params
    assert "remember this secret" not in str(params)


def test_off_mode_denial_is_audited(env):
    from src.repositories import audit_repo

    r = env["client"].post(
        f"/api/v1/sessions/{env['off_session']}/memories",
        json={"content": "note"},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 403

    rows, _ = audit_repo().query(action="agent.memory.write", limit=50)
    matches = [row for row in rows if row["result"] == "denied:memory_writes_disabled"]
    assert len(matches) == 1
    assert _audit_params(matches[0])["agent_id"] == env["off_agent"]


# ---------------------------------------------------------------------------
# _context_skill advertises the remember tool only when mode != off
# ---------------------------------------------------------------------------


def test_context_skill_advertises_remember_tool_when_mode_propose():
    from app.chat.agent_profile import _context_skill

    row = {"id": "a1", "slug": "s", "name": "S", "memory_write_mode": "propose"}
    body = _context_skill(row)
    assert "Remember" in body
    assert "/api/v1/sessions/{session_id}/memories" in body


def test_context_skill_advertises_remember_tool_when_mode_auto():
    from app.chat.agent_profile import _context_skill

    row = {"id": "a1", "slug": "s", "name": "S", "memory_write_mode": "auto"}
    assert "Remember" in _context_skill(row)


def test_context_skill_remember_tool_has_concrete_callable_invocation():
    """M2: a bare route path with no host or session-id source isn't
    actually callable from in-sandbox. The skill must spell out the two env
    vars `app/chat/runner.py` sets in the sandbox (`AGNES_SERVER` — rewritten
    to the loopback relay by `_spawn_runner` — and `AGNES_SESSION_ID`) plus a
    concrete curl invocation using them."""
    from app.chat.agent_profile import _context_skill

    row = {"id": "a1", "slug": "s", "name": "S", "memory_write_mode": "auto"}
    body = _context_skill(row)
    assert "$AGNES_SERVER" in body
    assert "$AGNES_SESSION_ID" in body
    assert 'curl -X POST "$AGNES_SERVER/api/v1/sessions/$AGNES_SESSION_ID/memories"' in body


def test_context_skill_omits_remember_tool_when_mode_off():
    from app.chat.agent_profile import _context_skill

    row = {"id": "a1", "slug": "s", "name": "S", "memory_write_mode": "off"}
    body = _context_skill(row)
    assert "Remember" not in body
    assert "$AGNES_SERVER" not in body
    assert "/api/v1/sessions/{session_id}/memories" not in body


def test_context_skill_omits_remember_tool_when_agent_profiles_disabled(monkeypatch):
    """The remember endpoint sits on the flag-guarded agent_memory router, so
    with agent profiles disabled every call would 403 — the skill must not
    advertise it, same posture as memory_write_mode='off'."""
    import app.instance_config as ic

    from app.chat.agent_profile import _context_skill

    monkeypatch.setattr(ic, "get_agent_profiles_enabled", lambda: False)
    row = {"id": "a1", "slug": "s", "name": "S", "memory_write_mode": "auto"}
    body = _context_skill(row)
    assert "Remember" not in body
    assert "/api/v1/sessions/{session_id}/memories" not in body
