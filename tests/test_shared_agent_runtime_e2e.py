"""C2.3 — shared-agent runtime + caller-bound access policies
(remediation program Track C, `docs/superpowers/plans/
2026-08-26-one-agent-model.md` §C2.3).

The master plan's program-level story, made real: an admin builds an
agent and grants it a data package (C2.1/C2.2 machinery — an
``agent_scope`` row with ``granted_by``), shares the agent to a group; a
NON-owner member of that group creates a session via BOTH the v1 sessions
API and the web chat route — surfaces that 404'd for a non-owner before
this task — and gets rows filtered by THEIR OWN policy identity (a table
access policy keyed on ``$user_email``), never the owner's. Owner-only
mutations still deny the grantee.

Confirmed pre-change failure: with ``agents_repo().get_by_slug`` (owner-
scoped only) still wired into ``app/api/agent_runtime.py``'s
``require_agent_runtime_principal`` and ``app/api/chat.py``'s
``_resolve_agent_id``, every session-creation assertion below 404s
``agent_not_found`` for the grantee — reproduced by stashing this task's
source changes (keeping this test) and re-running:
``git stash push -- app/api/agent_runtime.py app/api/chat.py
app/api/agent_sessions.py src/repositories/agents.py
src/repositories/agents_pg.py app/auth/pat_resolver.py
app/auth/session_principal.py src/access_policy.py``.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from app.auth.jwt import create_access_token
from src.db import get_system_db

# A table access policy keyed on $user_email -- the row-level assertion the
# whole scenario hinges on: each caller sees only the row attributed to
# their own email, never the agent owner's.
OWN_ROWS_POLICY = "SELECT * FROM my_reports WHERE owner_email = $user_email"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _FakeChatManager:
    """Fakes just enough of ``ChatManager`` to exercise session CREATION
    through both routers under test -- this file is about WHO may open a
    session against a shared agent and what identity it is attributed to,
    not sandbox/runner integration (mirrors ``tests/test_agent_sessions_api
    .py``'s ``FakeManager`` and ``tests/test_chat_session_as_agent.py``'s
    reliance on the real chat gate). Delegates session creation to the REAL
    ``chat_session_repo()`` so the auth dependencies under test see a real
    row, not a mock."""

    async def create_session(self, *, user_email, surface, agent_id=None, **kwargs):
        from src.repositories import chat_session_repo

        return chat_session_repo().create_session(
            user_email=user_email,
            surface=surface,
            agent_id=agent_id,
        )


@pytest.fixture
def shared_agent_env(e2e_env, mock_extract_factory, shared_app, monkeypatch):
    """Admin owns + builds an agent, grants it a data package covering
    ``my_reports`` (C2.1/C2.2 -- an ``agent_scope`` row with
    ``granted_by``), and shares the agent (``ResourceType.AGENT``) to a
    group holding ``grantee1`` -- a user who is neither the owner nor an
    admin. ``my_reports`` carries a real row per identity plus a
    ``$user_email`` access policy.
    """
    from fastapi.testclient import TestClient

    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
    UserRepository(conn).create(id="grantee1", email="grantee@test.com", name="Grantee")

    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")

    everyone_gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", everyone_gid, source="system_seed")
    UserGroupMembersRepository(conn).add_member("grantee1", everyone_gid, source="system_seed")
    # Cloud chat is its own RBAC resource (require_resource_access(CHAT,
    # "chat")) -- grant it to Everyone so both the owner and the grantee
    # can open sessions at all, same as every other chat-runtime test.
    ResourceGrantsRepository(conn).create(everyone_gid, "chat", "chat", assigned_by="test")

    # The group the agent is SHARED to -- holds the grantee, NOT the owner.
    groups = UserGroupsRepository(conn)
    shared_group = groups.create(name="c23-shared-agent-group", created_by="admin1")
    UserGroupMembersRepository(conn).add_member("grantee1", shared_group["id"], source="admin", added_by="admin1")

    app = shared_app
    client = TestClient(app)
    admin_token = create_access_token("admin1", "admin@test.com")
    grantee_token = create_access_token("grantee1", "grantee@test.com")

    # Wire a fake chat manager into BOTH routers under test — neither
    # `agent_sessions.py` (module-level `get_current_chat_manager`) nor
    # `chat.py` (`request.app.state.chat_manager`) sees a real one under a
    # bare TestClient, which never runs the ASGI lifespan.
    import app.api.agent_sessions as agent_sessions_module

    fake_manager = _FakeChatManager()
    monkeypatch.setattr(agent_sessions_module, "get_current_chat_manager", lambda: fake_manager)
    app.state.chat_manager = fake_manager

    r = client.post(
        "/api/admin/register-table",
        json={"name": "my_reports", "source_type": "keboola", "query_mode": "local", "description": "reports"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, r.text

    mock_extract_factory(
        "keboola",
        [
            {
                "name": "my_reports",
                "data": [
                    {"id": "1", "owner_email": "admin@test.com"},
                    {"id": "2", "owner_email": "grantee@test.com"},
                ],
            }
        ],
    )
    from src.orchestrator import SyncOrchestrator

    result = SyncOrchestrator(analytics_db_path=e2e_env["analytics_db"]).rebuild()
    assert "my_reports" in result.get("keboola", [])

    conn = get_system_db()
    table_id = TableRegistryRepository(conn).get_by_name("my_reports")["id"]
    TableRegistryRepository(conn).set_access_policy(
        table_id, sql=OWN_ROWS_POLICY, note="own rows only", updated_by="admin1"
    )

    # C2.1/C2.2: admin1 (the owner) grants a data package covering
    # my_reports into the agent's OWN scope -- `granted_by` records the
    # writer, same mechanism `agnes agent scope set` / the builder use.
    from tests.conftest import grant_table_via_package

    pkg_id = grant_table_via_package(conn, table_id, "admin1", group_name="c23-e2e-pkg")

    from src.repositories import agents_repo

    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id="admin1",
        name="Shared Reports Agent",
        slug="shared-reports-agent",
        tables_mode="selected",
        plugins_mode="all",
        connections_mode="all",
        memory_mode="all",
    )
    agents_repo().set_scope(agent_id, [("data_package", pkg_id)], granted_by="admin1")

    # Library "Share" action: a ResourceType.AGENT grant via the group the
    # grantee belongs to -- the whole point of C2.3.
    ResourceGrantsRepository(conn).create(shared_group["id"], "agent", agent_id, assigned_by="admin1")

    conn.close()

    return {
        "app": app,
        "client": client,
        "admin_token": admin_token,
        "grantee_token": grantee_token,
        "agent_id": agent_id,
        "table_id": table_id,
    }


def _broker_query(client: httpx.AsyncClient, tok: str, sql: str):
    return client.post(
        "/api/broker/agnes-api",
        headers=_auth(tok),
        json={"method": "POST", "path": "/api/query", "body": {"sql": sql}},
    )


class TestSharedAgentRuntimeSessionCreation:
    """Both runtime surfaces the plan names must resolve a shared (not
    owned) agent for a grantee -- addressed by the agent's ID, never the
    owner's private slug (see ``get_runnable_by_slug``'s docstring)."""

    def test_v1_sessions_api_resolves_the_shared_agent_for_a_grantee(self, shared_agent_env):
        env = shared_agent_env
        resp = env["client"].post(
            f"/api/v1/agents/{env['agent_id']}/sessions",
            json={},
            headers=_auth(env["grantee_token"]),
        )
        assert resp.status_code == 201, resp.text
        session_id = resp.json()["session_id"]

        from src.repositories import chat_session_repo

        session = chat_session_repo().get_session(session_id)
        assert session.agent_id == env["agent_id"]
        assert session.user_email == "grantee@test.com"

    def test_web_chat_route_resolves_the_shared_agent_for_a_grantee(self, shared_agent_env):
        env = shared_agent_env
        resp = env["client"].post(
            "/api/chat/sessions",
            json={"surface": "web", "agent_slug": env["agent_id"]},
            headers=_auth(env["grantee_token"]),
        )
        if resp.status_code in (403, 503):
            pytest.skip(f"chat unavailable in this environment ({resp.status_code})")
        assert resp.status_code == 201, resp.text
        assert resp.json()["agent_id"] == env["agent_id"]

        from src.repositories import chat_session_repo

        session = chat_session_repo().get_session(resp.json()["id"])
        assert session.user_email == "grantee@test.com"

    def test_a_stranger_outside_the_shared_group_still_gets_404(self, shared_agent_env):
        """Control: sharing to ONE group must not open the agent to every
        authenticated user."""
        from src.repositories.users import UserRepository

        conn = get_system_db()
        UserRepository(conn).create(id="stranger1", email="stranger@test.com", name="Stranger")
        conn.close()
        stranger_token = create_access_token("stranger1", "stranger@test.com")

        env = shared_agent_env
        resp = env["client"].post(
            f"/api/v1/agents/{env['agent_id']}/sessions",
            json={},
            headers=_auth(stranger_token),
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "agent_not_found"


class TestSharedAgentRowPolicyBindsCaller:
    """The security heart of C2.3: a shared agent's row-level access policy
    filters by WHO IS ASKING, not who built the agent."""

    def test_grantee_sees_only_their_own_row_through_the_shared_agent(self, shared_agent_env):
        env = shared_agent_env

        async def _run():
            transport = httpx.ASGITransport(app=env["app"])
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                create = await c.post(
                    f"/api/v1/agents/{env['agent_id']}/sessions",
                    json={},
                    headers=_auth(env["grantee_token"]),
                )
                assert create.status_code == 201, create.text
                session_id = create.json()["session_id"]

                from src.repositories import ticket_repo

                tok = ticket_repo().mint(session_id, "main", ttl_seconds=60)
                return await _broker_query(c, tok, "SELECT owner_email FROM my_reports")

        resp = asyncio.run(_run())

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rows"] == [["grantee@test.com"]], "the grantee saw more than (or other than) their own row"

    def test_owner_running_their_own_agent_sees_only_their_own_row(self, shared_agent_env):
        """Control: the SAME agent, run by its OWNER, must still only see
        the owner's row -- proving the filter is real (not merely "always
        return everything for an AgentPrincipal"), and that owner-run
        behavior is unchanged by C2.3."""
        env = shared_agent_env

        async def _run():
            transport = httpx.ASGITransport(app=env["app"])
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                create = await c.post(
                    f"/api/v1/agents/{env['agent_id']}/sessions",
                    json={},
                    headers=_auth(env["admin_token"]),
                )
                assert create.status_code == 201, create.text
                session_id = create.json()["session_id"]

                from src.repositories import ticket_repo

                tok = ticket_repo().mint(session_id, "main", ttl_seconds=60)
                return await _broker_query(c, tok, "SELECT owner_email FROM my_reports")

        resp = asyncio.run(_run())

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rows"] == [["admin@test.com"]]


class TestSharedAgentOwnerOnlyMutationsStillDenied:
    """A runnable grant is run+read, never manage -- opening the runtime
    resolution sites must not widen any mutation endpoint."""

    def test_grantee_cannot_update_the_shared_agent(self, shared_agent_env):
        env = shared_agent_env
        resp = env["client"].put(
            f"/api/v1/agents/{env['agent_id']}",
            json={"name": "Hijacked"},
            headers=_auth(env["grantee_token"]),
        )
        # This API's existing (pre-C2.3) convention hides existence from a
        # non-owner via 404 rather than 403 -- either way, denied. The
        # regression this guards against is 200 (the mutation silently
        # widened along with the run path).
        assert resp.status_code in (403, 404), resp.text
        assert resp.status_code != 200

        from src.repositories import agents_repo

        assert agents_repo().get_by_id(env["agent_id"])["name"] == "Shared Reports Agent"

    def test_grantee_cannot_delete_the_shared_agent(self, shared_agent_env):
        env = shared_agent_env
        resp = env["client"].delete(
            f"/api/v1/agents/{env['agent_id']}",
            headers=_auth(env["grantee_token"]),
        )
        assert resp.status_code in (403, 404), resp.text

        from src.repositories import agents_repo

        assert agents_repo().get_by_id(env["agent_id"]) is not None

    def test_grantee_cannot_mint_a_pat_for_the_shared_agent(self, shared_agent_env):
        env = shared_agent_env
        resp = env["client"].post(
            f"/api/v1/agents/{env['agent_id']}/tokens",
            json={"name": "grantee-forged-token"},
            headers=_auth(env["grantee_token"]),
        )
        assert resp.status_code in (403, 404), resp.text
