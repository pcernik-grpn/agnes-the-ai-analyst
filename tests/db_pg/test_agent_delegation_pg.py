"""Track C7 MVP — the caller-row-filtering laundering assertion against a
REAL Postgres backend (adversarial RBAC hardening review, PR #1742, Fix 3).

``tests/test_agent_delegation.py::TestDelegationLaundering`` proves THE
SECURITY INVARIANT of delegation — a delegated child session's row-level
access policy binds to the ORIGINAL CALLER, never A's owner or B's owner —
but only against DuckDB-backed app-state (its ``e2e_env`` fixture sets no
``DATABASE_URL``). ``tests/db_pg/test_endpoints_smoke.py``'s PG
status-parity sweep exempts the delegate route entirely, citing that
DuckDB-only test as coverage. This file closes the gap: the SAME assertion,
driven through a real Postgres backend for every app-state table the
delegation path touches (users, groups, resource_grants, table_registry's
access-policy column, agents/agent_scope, chat_sessions) — mirroring the
alembic-head + system-group-seed idiom from
``tests/db_pg/_parity_sweep_util.py`` / ``test_resolve_agent_authority_pg.py``.

The row DATA itself and the SQL access-policy evaluation still run through
``extract.duckdb``/``analytics.duckdb`` — those are the analytics engine
regardless of app-state backend (the extract.duckdb contract in
``docs/architecture.md``), so exercising them via DuckDB here is the
correct, unchanged split — not a parity gap.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Mirrors tests/test_agent_delegation.py's OWN_ROWS_POLICY exactly — the
# row-level assertion the laundering test hinges on.
OWN_ROWS_POLICY = "SELECT * FROM deleg_reports_pg WHERE owner_email = $user_email"


def _migrate_pg(pg_engine, monkeypatch) -> None:
    """Alembic-upgrade the test Postgres + point the repo factory at it,
    then seed the Admin/Everyone system groups alembic itself doesn't seed
    (mirrors ``tests/db_pg/_parity_sweep_util.py``'s ``build_seeded_client``)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)


def _make_workdir_mgr(tmp_path: Path, repo):
    from app.chat.workdir import WorkdirManager

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


def _register_a_live_session(mgr, chat_id: str, caller_email: str, *, delegation_depth: int = 0):
    """Mirrors ``tests/test_agent_delegation.py``'s helper of the same
    name — register a bare LiveSession for the DELEGATING session (A)."""
    from app.chat.manager import LiveSession
    from app.chat.types import SessionState

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


async def _drive_one_child_to_answer(mgr, exclude_chat_id: str, answer_text: str):
    """Mirrors ``tests/test_agent_delegation.py``'s helper of the same
    name — wait for the delegated child session to go live and answer."""
    from tests.chat_fakes import _wait_until

    ok = await _wait_until(lambda: any(live.chat_id != exclude_chat_id for live in mgr.list_live()))
    assert ok, "child session never went live"
    child = next(live for live in mgr.list_live() if live.chat_id != exclude_chat_id)
    ok = await _wait_until(lambda: child.handle is not None)
    assert ok, "child session never got a handle"
    child.handle.emit({"type": "assistant_message", "content": answer_text, "tokens_in": 1, "tokens_out": 1})
    child.handle.emit({"type": "done"})
    return child


@pytest.fixture
def deleg_env_pg(e2e_env, mock_extract_factory, monkeypatch, pg_engine, tmp_path):
    """PG-flavored twin of ``tests/test_agent_delegation.py``'s
    ``deleg_env`` fixture — same scenario (admin owns B, shares it to a
    grantee's group, a table access policy on the data B's scope reaches),
    every app-state write routed through the ``*_repo()`` factory (backend
    = Postgres) instead of a raw DuckDB conn."""
    _migrate_pg(pg_engine, monkeypatch)

    from app.auth.jwt import create_access_token
    from app.chat.config import ChatConfig
    from app.chat.manager import ChatManager
    from app.chat.persistence import ChatRepository
    from app.main import create_app
    from fastapi.testclient import TestClient
    from src.db import SYSTEM_ADMIN_GROUP, _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories import (
        agents_repo,
        data_packages_repo,
        resource_grants_repo,
        table_registry_repo,
        user_group_members_repo,
        user_groups_repo,
        users_repo,
    )
    from tests.chat_fakes import FakeHandle

    users_repo().create(id="admin1", email="admin@test.com", name="Admin")
    users_repo().create(id="grantee1", email="grantee@test.com", name="Grantee")

    admin_gid = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)["id"]
    everyone_gid = user_groups_repo().get_by_name("Everyone")["id"]

    user_group_members_repo().add_member("admin1", admin_gid, source="system_seed")
    user_group_members_repo().add_member("admin1", everyone_gid, source="system_seed")
    user_group_members_repo().add_member("grantee1", everyone_gid, source="system_seed")
    resource_grants_repo().create(everyone_gid, "chat", "chat", assigned_by="test")

    # The group B is SHARED to — holds the grantee, NOT B's owner.
    shared_group = user_groups_repo().create(name="c7-deleg-group-pg", created_by="admin1")
    user_group_members_repo().add_member("grantee1", shared_group["id"], source="admin", added_by="admin1")

    app = create_app()
    client = TestClient(app)
    admin_token = create_access_token("admin1", "admin@test.com")
    r = client.post(
        "/api/admin/register-table",
        json={"name": "deleg_reports_pg", "source_type": "keboola", "query_mode": "local", "description": "r"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 201, r.text

    mock_extract_factory(
        "keboola",
        [
            {
                "name": "deleg_reports_pg",
                "data": [
                    {"id": "1", "owner_email": "admin@test.com"},
                    {"id": "2", "owner_email": "grantee@test.com"},
                ],
            }
        ],
    )
    from src.orchestrator import SyncOrchestrator

    result = SyncOrchestrator(analytics_db_path=e2e_env["analytics_db"]).rebuild()
    assert "deleg_reports_pg" in result.get("keboola", [])

    table = table_registry_repo().get_by_name("deleg_reports_pg")
    table_registry_repo().set_access_policy(table["id"], sql=OWN_ROWS_POLICY, note="own rows only", updated_by="admin1")

    # Wrap the table in a data_package and grant it to a group holding B's
    # OWNER (admin1) — the same "package the owner is entitled to" shape as
    # tests/conftest.py's grant_table_via_package, hand-rolled here through
    # the backend-aware factory instead of a raw DuckDB conn.
    pkg_group = user_groups_repo().create(name="c7-deleg-pkg-pg", created_by="admin1")
    user_group_members_repo().add_member("admin1", pkg_group["id"], source="admin", added_by="admin1")
    pkg_id = data_packages_repo().create(
        name="c7 deleg pkg pg",
        slug="c7-deleg-pkg-pg",
        description=None,
        icon=None,
        color=None,
        created_by="admin1",
    )
    data_packages_repo().add_table(pkg_id, table["id"], added_by="admin1")
    resource_grants_repo().create(pkg_group["id"], "data_package", pkg_id, assigned_by="admin1", requirement="required")

    b_id = str(uuid.uuid4())
    agents_repo().create(
        id=b_id,
        owner_user_id="admin1",
        name="Delegate Reports Agent PG",
        slug="deleg-reports-agent-pg",
        tables_mode="selected",
        plugins_mode="all",
        connections_mode="all",
        memory_mode="all",
    )
    agents_repo().set_scope(b_id, [("data_package", pkg_id)], granted_by="admin1")

    # Share B to the grantee (C2.3 mechanism) — the grant that makes
    # `get_runnable_by_slug("grantee1", b_id)` succeed.
    resource_grants_repo().create(shared_group["id"], "agent", b_id, assigned_by="admin1")

    # Chat state (sessions/messages) also rides Postgres via ChatRepository's
    # own backend detection (`app/chat/persistence.py`) — the DuckDB conn it
    # takes is unused once `use_pg()` is True, kept only for constructor
    # shape. `get_system_db()` itself HARD-REFUSES on a PG instance (the
    # sanctioned system-DuckDB callers all gate on `not use_pg()`), so this
    # mirrors `tests/db_pg/test_chat_pg.py`'s `_chat_env` PG leg: a private,
    # schema-bearing in-memory DuckDB conn ChatRepository never actually
    # reads from once `use_pg()` is True.
    conn = _open_duckdb(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock(side_effect=lambda **kwargs: FakeHandle())
    manager = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=5, provider="docker"),
    )

    return {"app": app, "b_id": b_id, "manager": manager}


def test_caller_sees_only_their_own_row_through_the_delegated_agent_pg(deleg_env_pg):
    """Postgres leg of ``TestDelegationLaundering::
    test_caller_sees_only_their_own_row_through_the_delegated_agent``: A
    (driven by grantee1) delegates to B (owned by admin1, shared to
    grantee1). The row-level access policy on the table B's scope reaches
    must filter by grantee1's OWN identity — never admin1's (B's owner),
    never ALL rows — with every piece of app-state actually living in
    Postgres, not DuckDB."""
    mgr = deleg_env_pg["manager"]
    a_live = _register_a_live_session(mgr, "chat_a_deleg_pg", "grantee@test.com")

    async def _run():
        # get_runnable_by_slug resolves a NON-owned (shared) agent by its
        # globally-unique id, not its owner's human slug — grantee1 does
        # not own "deleg-reports-agent-pg", so it must address B by id.
        task = asyncio.create_task(
            mgr.handle_delegation(
                a_live.chat_id,
                target_slug=deleg_env_pg["b_id"],
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
        transport = httpx.ASGITransport(app=deleg_env_pg["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/agnes-api",
                headers={"Authorization": f"Bearer {tok}"},
                json={
                    "method": "POST",
                    "path": "/api/query",
                    "body": {"sql": "SELECT owner_email FROM deleg_reports_pg"},
                },
            )

    resp = asyncio.run(_query())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"] == [["grantee@test.com"]], (
        "the delegated agent's child session saw more than (or other than) the CALLER's own row — "
        "a delegation laundering bug (Postgres leg)"
    )
