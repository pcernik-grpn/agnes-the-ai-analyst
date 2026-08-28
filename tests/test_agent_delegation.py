"""Track C7 MVP — @delegation between shared agents (server-side handoff).

Manager-level unit tests for ``ChatManager.handle_delegation`` (depth-1
guard, one-delegation-per-turn, RBAC denial, budget degrade, output
visible) plus the MANDATORY security test: a delegated child session's
row-level access policy binds to the ORIGINAL CALLER, never A's owner or
B's owner — the exact invariant that makes delegation safe. The security
test needs the real system DB + access-policy + broker machinery, mirroring
``tests/test_shared_agent_runtime_e2e.py``'s fixture shape; the rest are
pure ``ChatManager`` behavior, exercised with a real ``ChatManager`` wired
to the shared system DB (so ``agents_repo()``/``llm_usage_repo()`` see the
same rows) and a fake in-memory sandbox provider (``tests/chat_fakes.py``),
matching ``tests/test_chat_manager.py``'s own conventions.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, LiveSession
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState
from app.chat.workdir import WorkdirManager
from src.db import get_system_db
from tests.chat_fakes import FakeHandle, _wait_until

# A table access policy keyed on $user_email — the row-level assertion the
# laundering test hinges on: whoever actually queries through the delegated
# child session sees only the row attributed to THEIR OWN email.
OWN_ROWS_POLICY = "SELECT * FROM deleg_reports WHERE owner_email = $user_email"


def _make_workdir_mgr(tmp_path: Path, repo: ChatRepository) -> WorkdirManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("d")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )


@pytest.fixture
def delegation_manager(e2e_env, tmp_path):
    """A real ChatManager wired to the SAME system DB `agents_repo()` /
    the RBAC layer reads (``get_system_db()``) — needed so
    ``handle_delegation``'s RBAC gate, budget check, and child-session
    creation see the same rows a full-app test sets up. The provider's
    ``spawn()`` returns a FRESH ``FakeHandle`` per call so a delegated
    child's own turn can be driven by emitting frames onto it (its pump
    task is the REAL ``_spawn_live``-started one, not manually driven)."""
    conn = get_system_db()
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock(side_effect=lambda **kwargs: FakeHandle())
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=5, provider="docker"),
    )


def _seed_user(email: str, user_id: str) -> None:
    from src.repositories.users import UserRepository

    conn = get_system_db()
    if UserRepository(conn).get_by_email(email) is None:
        UserRepository(conn).create(id=user_id, email=email, name=email)
    conn.close()


def _seed_agent(*, agent_id: str, owner_id: str, slug: str, token_budget_monthly=None) -> None:
    from src.repositories import agents_repo

    agents_repo().create(
        id=agent_id,
        owner_user_id=owner_id,
        name=slug,
        slug=slug,
        tables_mode="all",
        plugins_mode="all",
        connections_mode="all",
        memory_mode="all",
        token_budget_monthly=token_budget_monthly,
    )


def _register_a_live_session(
    mgr: ChatManager, chat_id: str, caller_email: str, *, delegation_depth: int = 0
) -> LiveSession:
    """Register a bare LiveSession for the DELEGATING session (A) — no
    handle/pump needed: `handle_delegation` never touches A's own
    handle/stdin, only its bookkeeping fields (see the manager docstring)."""
    live = LiveSession(
        chat_id=chat_id,
        user_email=caller_email,
        state=SessionState.ACTIVE,
        handle=None,
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        delegation_depth=delegation_depth,
    )
    mgr._live[chat_id] = live
    return live


async def _drive_one_child_to_answer(mgr: ChatManager, exclude_chat_id: str, answer_text: str) -> LiveSession:
    """Wait for a freshly spawned child session to go live and emit an
    assistant_message + done onto its FakeHandle so its own real pump task
    (started by `_spawn_live`) completes the turn and the HeadlessSink
    `handle_delegation` is awaiting on sees `done_event`."""
    ok = await _wait_until(lambda: any(live.chat_id != exclude_chat_id for live in mgr.list_live()))
    assert ok, "child session never went live"
    child = next(live for live in mgr.list_live() if live.chat_id != exclude_chat_id)
    ok = await _wait_until(lambda: child.handle is not None)
    assert ok, "child session never got a handle"
    child.handle.emit({"type": "assistant_message", "content": answer_text, "tokens_in": 1, "tokens_out": 1})
    child.handle.emit({"type": "done"})
    return child


class TestDelegationOutputAndRbac:
    def test_successful_delegation_returns_bs_answer(self, delegation_manager, e2e_env):
        mgr = delegation_manager
        _seed_user("caller@test.com", "caller1")
        b_id = str(uuid.uuid4())
        _seed_agent(agent_id=b_id, owner_id="caller1", slug="b-agent")
        a_live = _register_a_live_session(mgr, "chat_a", "caller@test.com")

        async def _run():
            task = asyncio.create_task(
                mgr.handle_delegation(
                    a_live.chat_id,
                    target_slug="b-agent",
                    message="hello B",
                    caller_user_id="caller1",
                    caller_email="caller@test.com",
                )
            )
            child = await _drive_one_child_to_answer(mgr, a_live.chat_id, "hi from B")
            result = await task
            return result, child

        result, child = asyncio.run(_run())
        assert result["status"] == "ok"
        assert result["answer"] == "hi from B"
        assert result["agent_slug"] == "b-agent"
        # THE SECURITY INVARIANT (spot-check here; the dedicated laundering
        # test proves the row-filtering consequence end-to-end): the child
        # session is stored under the CALLER's email, never A's owner's.
        assert child.user_email == "caller@test.com"
        assert a_live.delegated_this_turn is True

    def test_delegating_to_an_unrunnable_agent_is_denied_and_spawns_nothing(self, delegation_manager, e2e_env):
        mgr = delegation_manager
        _seed_user("caller@test.com", "caller1")
        a_live = _register_a_live_session(mgr, "chat_a", "caller@test.com")

        async def _run():
            return await mgr.handle_delegation(
                a_live.chat_id,
                target_slug="no-such-agent",
                message="hello",
                caller_user_id="caller1",
                caller_email="caller@test.com",
            )

        result = asyncio.run(_run())
        assert result["status"] == "denied"
        assert result["reason"] == "agent_not_runnable"
        assert result["answer"] is None
        # No child session was ever created.
        assert mgr.list_live() == [a_live]


class TestDelegationDepthGuard:
    def test_a_session_already_at_depth_1_cannot_delegate(self, delegation_manager, e2e_env):
        mgr = delegation_manager
        _seed_user("caller@test.com", "caller1")
        b_id = str(uuid.uuid4())
        _seed_agent(agent_id=b_id, owner_id="caller1", slug="b-agent")
        # Simulates B's own live session — spawned AS a delegate target.
        b_live = _register_a_live_session(mgr, "chat_b", "caller@test.com", delegation_depth=1)

        async def _run():
            return await mgr.handle_delegation(
                b_live.chat_id,
                target_slug="b-agent",
                message="B tries to delegate to itself/another",
                caller_user_id="caller1",
                caller_email="caller@test.com",
            )

        result = asyncio.run(_run())
        assert result["status"] == "denied"
        assert result["reason"] == "depth_exceeded"
        # No child spawned — still only the one live session.
        assert mgr.list_live() == [b_live]

    def test_a_delegated_child_session_is_tagged_depth_1(self, delegation_manager, e2e_env):
        """End-to-end proof that `handle_delegation` itself tags the CHILD
        it spawns at depth 1 (not just that a pre-tagged depth-1 session
        refuses, per the test above)."""
        mgr = delegation_manager
        _seed_user("caller@test.com", "caller1")
        b_id = str(uuid.uuid4())
        _seed_agent(agent_id=b_id, owner_id="caller1", slug="b-agent")
        a_live = _register_a_live_session(mgr, "chat_a", "caller@test.com")

        async def _run():
            task = asyncio.create_task(
                mgr.handle_delegation(
                    a_live.chat_id,
                    target_slug="b-agent",
                    message="hello B",
                    caller_user_id="caller1",
                    caller_email="caller@test.com",
                )
            )
            child = await _drive_one_child_to_answer(mgr, a_live.chat_id, "hi")
            await task
            return child

        child = asyncio.run(_run())
        assert child.delegation_depth == 1


class TestDelegationOncePerTurn:
    def test_a_second_delegation_in_the_same_turn_is_refused(self, delegation_manager, e2e_env):
        mgr = delegation_manager
        _seed_user("caller@test.com", "caller1")
        b1_id, b2_id = str(uuid.uuid4()), str(uuid.uuid4())
        _seed_agent(agent_id=b1_id, owner_id="caller1", slug="b1-agent")
        _seed_agent(agent_id=b2_id, owner_id="caller1", slug="b2-agent")
        a_live = _register_a_live_session(mgr, "chat_a", "caller@test.com")

        async def _run():
            task = asyncio.create_task(
                mgr.handle_delegation(
                    a_live.chat_id,
                    target_slug="b1-agent",
                    message="first delegation",
                    caller_user_id="caller1",
                    caller_email="caller@test.com",
                )
            )
            await _drive_one_child_to_answer(mgr, a_live.chat_id, "first answer")
            first_result = await task

            second_result = await mgr.handle_delegation(
                a_live.chat_id,
                target_slug="b2-agent",
                message="second delegation, same turn",
                caller_user_id="caller1",
                caller_email="caller@test.com",
            )
            return first_result, second_result

        first_result, second_result = asyncio.run(_run())
        assert first_result["status"] == "ok"
        assert second_result["status"] == "denied"
        assert second_result["reason"] == "already_delegated_this_turn"
        # Exactly one child session was ever created (A + the first child).
        assert len(mgr.list_live()) == 2

    def test_a_new_turn_resets_the_one_delegation_budget(self, delegation_manager, e2e_env):
        mgr = delegation_manager
        _seed_user("caller@test.com", "caller1")
        b_id = str(uuid.uuid4())
        _seed_agent(agent_id=b_id, owner_id="caller1", slug="b-agent")
        a_live = _register_a_live_session(mgr, "chat_a", "caller@test.com")
        a_live.delegated_this_turn = True  # simulate "already used this turn"

        # A fresh user_msg on A's OWN session resets the flag — exercised
        # directly here (not via a full runner round trip) since
        # `_deliver_local_user_message` is the single choke point that
        # starts a new turn.
        async def _reset_and_check():
            a_live.handle = FakeHandle()
            await mgr._deliver_local_user_message(a_live, "a fresh user turn")
            return a_live.delegated_this_turn

        assert asyncio.run(_reset_and_check()) is False


class TestDelegationBudgetDegrade:
    def test_an_exhausted_budget_degrades_without_crashing_a(self, delegation_manager, e2e_env):
        from src.repositories import llm_usage_repo

        mgr = delegation_manager
        _seed_user("caller@test.com", "caller1")
        b_id = str(uuid.uuid4())
        _seed_agent(agent_id=b_id, owner_id="caller1", slug="b-agent", token_budget_monthly=100)
        # Pre-exhaust B's monthly budget.
        llm_usage_repo().insert_batch(
            [
                {
                    "id": str(uuid.uuid4()),
                    "agent_id": b_id,
                    "user_id": "caller1",
                    "session_id": "prior-session",
                    "model": "claude",
                    "input_tokens": 60,
                    "output_tokens": 60,
                }
            ]
        )
        a_live = _register_a_live_session(mgr, "chat_a", "caller@test.com")

        async def _run():
            return await mgr.handle_delegation(
                a_live.chat_id,
                target_slug="b-agent",
                message="hello",
                caller_user_id="caller1",
                caller_email="caller@test.com",
            )

        result = asyncio.run(_run())
        assert result["status"] == "degraded"
        assert result["reason"] == "budget_exhausted"
        assert result["answer"] is None
        # A's own session is untouched — no crash, still live.
        assert mgr._live.get(a_live.chat_id) is a_live
        # No child session was spawned for the exhausted agent.
        assert mgr.list_live() == [a_live]


class TestDelegationLaundering:
    """THE SECURITY HEART of Track C7: a delegated agent's row-level
    access policy filters by the ORIGINAL CALLER, never A's owner or B's
    owner. Mirrors ``tests/test_shared_agent_runtime_e2e.py``'s C2.3
    fixture shape — the same underlying mechanism, exercised through
    delegation instead of a directly-created agent session."""

    @pytest.fixture
    def deleg_env(self, e2e_env, mock_extract_factory, shared_app, delegation_manager):
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
        ResourceGrantsRepository(conn).create(everyone_gid, "chat", "chat", assigned_by="test")

        shared_group = UserGroupsRepository(conn).create(name="c7-deleg-group", created_by="admin1")
        UserGroupMembersRepository(conn).add_member("grantee1", shared_group["id"], source="admin", added_by="admin1")

        from fastapi.testclient import TestClient

        app = shared_app
        client = TestClient(app)
        from app.auth.jwt import create_access_token

        admin_token = create_access_token("admin1", "admin@test.com")
        r = client.post(
            "/api/admin/register-table",
            json={"name": "deleg_reports", "source_type": "keboola", "query_mode": "local", "description": "r"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 201, r.text

        mock_extract_factory(
            "keboola",
            [
                {
                    "name": "deleg_reports",
                    "data": [
                        {"id": "1", "owner_email": "admin@test.com"},
                        {"id": "2", "owner_email": "grantee@test.com"},
                    ],
                }
            ],
        )
        from src.orchestrator import SyncOrchestrator

        result = SyncOrchestrator(analytics_db_path=e2e_env["analytics_db"]).rebuild()
        assert "deleg_reports" in result.get("keboola", [])

        table_id = TableRegistryRepository(conn).get_by_name("deleg_reports")["id"]
        TableRegistryRepository(conn).set_access_policy(
            table_id, sql=OWN_ROWS_POLICY, note="own rows only", updated_by="admin1"
        )

        from tests.conftest import grant_table_via_package

        pkg_id = grant_table_via_package(conn, table_id, "admin1", group_name="c7-deleg-pkg")

        from src.repositories import agents_repo

        b_id = str(uuid.uuid4())
        agents_repo().create(
            id=b_id,
            owner_user_id="admin1",
            name="Delegate Reports Agent",
            slug="deleg-reports-agent",
            tables_mode="selected",
            plugins_mode="all",
            connections_mode="all",
            memory_mode="all",
        )
        agents_repo().set_scope(b_id, [("data_package", pkg_id)], granted_by="admin1")

        # Share B to the grantee (C2.3 mechanism) — the grant that makes
        # `get_runnable_by_slug("grantee1", "deleg-reports-agent")` succeed.
        ResourceGrantsRepository(conn).create(shared_group["id"], "agent", b_id, assigned_by="admin1")

        conn.close()

        return {"app": app, "b_id": b_id, "manager": delegation_manager}

    def test_caller_sees_only_their_own_row_through_the_delegated_agent(self, deleg_env):
        """A (driven by grantee1) delegates to B (owned by admin1, shared
        to grantee1). A row-level access policy on the table B's scope
        reaches must filter by grantee1's OWN identity — never admin1's
        (B's owner), never ALL rows."""
        mgr = deleg_env["manager"]
        a_live = _register_a_live_session(mgr, "chat_a_deleg", "grantee@test.com")

        async def _run():
            # get_runnable_by_slug resolves a NON-owned (shared) agent by
            # its globally-unique id, not its owner's human slug (same
            # contract every `/api/v1/agents/{slug}/...` runtime route
            # uses — see that method's docstring) — grantee1 does not own
            # "deleg-reports-agent", so it must address B by id here.
            task = asyncio.create_task(
                mgr.handle_delegation(
                    a_live.chat_id,
                    target_slug=deleg_env["b_id"],
                    message="what rows can you see?",
                    caller_user_id="grantee1",
                    caller_email="grantee@test.com",
                )
            )
            child = await _drive_one_child_to_answer(mgr, a_live.chat_id, "I can only see my own row")
            result = await task
            return result, child

        result, child = asyncio.run(_run())
        assert result["status"] == "ok"
        assert child.user_email == "grantee@test.com"

        async def _query():
            from src.repositories import ticket_repo

            tok = ticket_repo().mint(child.chat_id, "main", ttl_seconds=60)
            transport = httpx.ASGITransport(app=deleg_env["app"])
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                return await c.post(
                    "/api/broker/agnes-api",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={
                        "method": "POST",
                        "path": "/api/query",
                        "body": {"sql": "SELECT owner_email FROM deleg_reports"},
                    },
                )

        resp = asyncio.run(_query())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rows"] == [["grantee@test.com"]], (
            "the delegated agent's child session saw more than (or other than) the CALLER's own row — "
            "a delegation laundering bug"
        )

    def test_a_caller_outside_the_share_cannot_delegate_to_b(self, deleg_env):
        """Control: a caller B was never shared to gets a clean RBAC
        denial from handle_delegation, and no child session/data access
        of any kind happens."""
        from src.repositories.users import UserRepository

        conn = get_system_db()
        UserRepository(conn).create(id="stranger1", email="stranger@test.com", name="Stranger")
        conn.close()

        mgr = deleg_env["manager"]
        a_live = _register_a_live_session(mgr, "chat_a_stranger", "stranger@test.com")

        async def _run():
            return await mgr.handle_delegation(
                a_live.chat_id,
                target_slug="deleg-reports-agent",
                message="what rows can you see?",
                caller_user_id="stranger1",
                caller_email="stranger@test.com",
            )

        result = asyncio.run(_run())
        assert result["status"] == "denied"
        assert result["reason"] == "agent_not_runnable"
        assert result["answer"] is None
        assert mgr.list_live() == [a_live]
