"""Memory management API — `/api/v1/agents/{id}/memories` (agent-api V1c
Task 5). The owner-facing inspect/approve/archive/delete surface over the
per-agent memory notebook whose write side is the "remember" tool
(`app/api/agent_memory.py`, Task 4) and whose read side is the pre-spawn
materialization (`app.chat.agent_profile.materialize_memories`, Task 3).

Covers `app/api/agents_admin.py`'s three new routes. Auth/ownership mirrors
every other `_load_agent`-gated route in that module: `require_session_token`
(no PAT flavor accepted), 404 for a non-owner/non-admin caller (existence of
another owner's agent/memory is never leaked).

**C4 (binding addition).** The management list must mark each *active*
memory `in_budget: true/false`, computed via `app.chat.agent_profile.
select_in_budget` against the same `_MEMORY_BUDGET_CHARS` cap
`materialize_memories` uses at spawn time — so an owner who just approved a
memory isn't misled into thinking it's live when it's actually shadowed
behind enough newer active content.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


class _AuthedClient:
    def __init__(self, client: TestClient, token: str):
        self._client = client
        self._token = token

    def _headers(self, headers):
        merged = {"Authorization": f"Bearer {self._token}"}
        if headers:
            merged.update(headers)
        return merged

    def get(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.get(url, **kw)

    def patch(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.patch(url, **kw)

    def delete(self, url, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.delete(url, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import get_system_db
    from src.repositories import agents_repo
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="other1", email="other@test.com", name="Other")
    conn.close()

    agent_id = str(uuid.uuid4())
    agents_repo().create(id=agent_id, owner_user_id="owner1", name="Support Bot", slug="support-bot")
    other_agent_id = str(uuid.uuid4())
    agents_repo().create(id=other_agent_id, owner_user_id="other1", name="Other's Bot", slug="others-bot")

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "other_token": create_access_token("other1", "other@test.com"),
        "agent_id": agent_id,
        "other_agent_id": other_agent_id,
    }


@pytest.fixture
def owner_client(env):
    return _AuthedClient(env["client"], env["owner_token"])


@pytest.fixture
def other_client(env):
    return _AuthedClient(env["client"], env["other_token"])


def _create_memory(agent_id, *, content="note", status="pending", owner_user_id="owner1"):
    from src.repositories import agent_memories_repo

    memory_id = str(uuid.uuid4())
    agent_memories_repo().create(
        id=memory_id,
        agent_id=agent_id,
        owner_user_id=owner_user_id,
        content=content,
        source_session_id=None,
        status=status,
    )
    return memory_id


# ---------------------------------------------------------------------------
# GET — list
# ---------------------------------------------------------------------------


def test_list_memories_returns_owner_rows(owner_client, env):
    pending_id = _create_memory(env["agent_id"], content="pending note", status="pending")
    active_id = _create_memory(env["agent_id"], content="active note", status="active")

    resp = owner_client.get(f"/api/v1/agents/{env['agent_id']}/memories")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {row["id"] for row in body["data"]}
    assert ids == {pending_id, active_id}
    assert body["has_more"] is False
    assert body["next_cursor"] is None


def test_list_memories_status_filter(owner_client, env):
    _create_memory(env["agent_id"], content="pending note", status="pending")
    active_id = _create_memory(env["agent_id"], content="active note", status="active")

    resp = owner_client.get(f"/api/v1/agents/{env['agent_id']}/memories", params={"status": "active"})

    assert resp.status_code == 200
    body = resp.json()
    assert [row["id"] for row in body["data"]] == [active_id]
    assert body["data"][0]["status"] == "active"


def test_list_memories_marks_in_budget_and_shadowed(owner_client, env, monkeypatch):
    """C4: seed active memories that together exceed a (monkeypatched, tiny)
    budget and assert the split between in-budget and shadowed rows."""
    from app.chat import agent_profile

    monkeypatch.setattr(agent_profile, "_MEMORY_BUDGET_CHARS", 10)

    # list_active/list_for_agent order newest-first (created_at DESC); create
    # in order oldest -> newest so the LAST created id is "newest" and wins
    # the budget.
    older_id = _create_memory(env["agent_id"], content="x" * 8, status="active")
    newer_id = _create_memory(env["agent_id"], content="y" * 8, status="active")

    resp = owner_client.get(f"/api/v1/agents/{env['agent_id']}/memories")

    assert resp.status_code == 200
    by_id = {row["id"]: row for row in resp.json()["data"]}
    assert by_id[newer_id]["in_budget"] is True
    assert by_id[older_id]["in_budget"] is False


def test_list_memories_pending_rows_have_no_in_budget_key(owner_client, env):
    pending_id = _create_memory(env["agent_id"], content="note", status="pending")

    resp = owner_client.get(f"/api/v1/agents/{env['agent_id']}/memories")

    row = next(r for r in resp.json()["data"] if r["id"] == pending_id)
    assert row["status"] == "pending"
    assert "in_budget" not in row


def test_list_memories_cross_owner_returns_404(other_client, env):
    resp = other_client.get(f"/api/v1/agents/{env['agent_id']}/memories")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_not_found"


# ---------------------------------------------------------------------------
# GET — list, owner-vs-grantee (private notebook, not a runnable-share surface)
# ---------------------------------------------------------------------------


@pytest.fixture
def grantee_env(env):
    """Shares `env["agent_id"]` (owned by owner1) to a group holding a THIRD
    user, `grantee1` — a runnable grantee (C2.3: a `ResourceType.AGENT` row
    via the caller's group, the same reach `GET /api/v1/agents` and the
    session-creation routes honor for READ/RUN). Also seeds an admin user
    in the system admin group, for the admin-still-reads assertion.

    Deliberately does NOT touch `agent_scope`/data packages/tables — the
    memory notebook is private regardless of what data scope the agent
    carries.
    """
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="grantee1", email="grantee@test.com", name="Grantee")
    UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")

    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")

    shared_group = UserGroupsRepository(conn).create(name="grantee-shared-agent-group", created_by="owner1")
    UserGroupMembersRepository(conn).add_member("grantee1", shared_group["id"], source="admin", added_by="owner1")
    # Library "Share" action: the exact grant C2.3 introduced.
    ResourceGrantsRepository(conn).create(shared_group["id"], "agent", env["agent_id"], assigned_by="owner1")
    conn.close()

    env = dict(env)
    env["grantee_token"] = create_access_token("grantee1", "grantee@test.com")
    env["admin_token"] = create_access_token("admin1", "admin@test.com")
    return env


def test_list_memories_grantee_returns_404_not_owner_content(grantee_env):
    """The bug: a runnable grantee (shared the agent, C2.3) could read the
    owner's private memory notebook verbatim via this same list route that
    admits them for `GET /api/v1/agents/{id}`. The notebook is private —
    owner/admin only — so a grantee must 404 exactly like any other
    non-owner, non-admin caller, never see the content."""
    _create_memory(
        grantee_env["agent_id"],
        content="OWNER PRIVATE: prefer margin over revenue for Q3 board deck",
        status="active",
    )
    client = _AuthedClient(grantee_env["client"], grantee_env["grantee_token"])

    resp = client.get(f"/api/v1/agents/{grantee_env['agent_id']}/memories")

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "agent_not_found"
    assert "OWNER PRIVATE" not in resp.text


def test_list_memories_owner_still_reads_their_own(grantee_env):
    client = _AuthedClient(grantee_env["client"], grantee_env["owner_token"])
    memory_id = _create_memory(grantee_env["agent_id"], content="owner note", status="active")

    resp = client.get(f"/api/v1/agents/{grantee_env['agent_id']}/memories")

    assert resp.status_code == 200, resp.text
    assert {row["id"] for row in resp.json()["data"]} == {memory_id}


def test_list_memories_admin_still_reads_god_mode(grantee_env):
    client = _AuthedClient(grantee_env["client"], grantee_env["admin_token"])
    memory_id = _create_memory(grantee_env["agent_id"], content="owner note", status="active")

    resp = client.get(f"/api/v1/agents/{grantee_env['agent_id']}/memories")

    assert resp.status_code == 200, resp.text
    assert {row["id"] for row in resp.json()["data"]} == {memory_id}


def test_grantee_still_sees_the_agent_itself(grantee_env):
    """Control: only the private notebook tightens — the agent-list/detail
    surface a runnable grantee legitimately relies on (and can run) must
    stay reachable."""
    client = _AuthedClient(grantee_env["client"], grantee_env["grantee_token"])

    resp = client.get(f"/api/v1/agents/{grantee_env['agent_id']}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == grantee_env["agent_id"]


# ---------------------------------------------------------------------------
# PATCH — approve / archive
# ---------------------------------------------------------------------------


def test_patch_approve_flips_pending_to_active(owner_client, env):
    memory_id = _create_memory(env["agent_id"], status="pending")

    resp = owner_client.patch(
        f"/api/v1/agents/{env['agent_id']}/memories/{memory_id}",
        json={"action": "approve"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    from src.repositories import agent_memories_repo

    row = agent_memories_repo().get(memory_id)
    assert row["status"] == "active"
    assert row["activated_at"] is not None


def test_patch_archive_active_memory(owner_client, env):
    memory_id = _create_memory(env["agent_id"], status="active")

    resp = owner_client.patch(
        f"/api/v1/agents/{env['agent_id']}/memories/{memory_id}",
        json={"action": "archive"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "archived"


def test_patch_invalid_action_returns_400(owner_client, env):
    memory_id = _create_memory(env["agent_id"], status="pending")

    resp = owner_client.patch(
        f"/api/v1/agents/{env['agent_id']}/memories/{memory_id}",
        json={"action": "bogus"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_action"


def test_patch_unknown_memory_returns_404(owner_client, env):
    resp = owner_client.patch(
        f"/api/v1/agents/{env['agent_id']}/memories/does-not-exist",
        json={"action": "approve"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "memory_not_found"


def test_patch_cross_owner_agent_returns_404(other_client, env):
    memory_id = _create_memory(env["agent_id"], status="pending")

    resp = other_client.patch(
        f"/api/v1/agents/{env['agent_id']}/memories/{memory_id}",
        json={"action": "approve"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_not_found"


def test_patch_memory_belonging_to_different_agent_returns_404(owner_client, env):
    """Same owner, but the memory id belongs to a DIFFERENT agent than the
    one named in the path — must not be reachable through it."""
    from src.repositories import agents_repo

    second_agent_id = str(uuid.uuid4())
    agents_repo().create(id=second_agent_id, owner_user_id="owner1", name="Second Bot", slug="second-bot")
    memory_id = _create_memory(second_agent_id, status="pending")

    resp = owner_client.patch(
        f"/api/v1/agents/{env['agent_id']}/memories/{memory_id}",
        json={"action": "approve"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "memory_not_found"


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


def test_delete_memory_returns_204(owner_client, env):
    memory_id = _create_memory(env["agent_id"], status="pending")

    resp = owner_client.delete(f"/api/v1/agents/{env['agent_id']}/memories/{memory_id}")
    assert resp.status_code == 204

    from src.repositories import agent_memories_repo

    assert agent_memories_repo().get(memory_id) is None


def test_delete_unknown_memory_returns_404(owner_client, env):
    resp = owner_client.delete(f"/api/v1/agents/{env['agent_id']}/memories/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "memory_not_found"


def test_delete_cross_owner_agent_returns_404(other_client, env):
    memory_id = _create_memory(env["agent_id"], status="pending")

    resp = other_client.delete(f"/api/v1/agents/{env['agent_id']}/memories/{memory_id}")

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_not_found"

    from src.repositories import agent_memories_repo

    assert agent_memories_repo().get(memory_id) is not None
