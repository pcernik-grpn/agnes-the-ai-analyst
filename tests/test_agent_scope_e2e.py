"""V1d Task 6 — the end-to-end proof that agent scope is enforced, not just
computed.

This is the test that closes the reviewer's HIGH finding: it drives a REAL
brokered ``/api/query`` request (ticket -> ``_mint_identity_jwt`` ->
``AgentPrincipal`` -> ``get_accessible_tables`` -> the master-view-name RBAC
denylist), not a unit-level fake. Unlike ``tests/test_agent_scope_seams.py``
(which pins each seam's behaviour against a hand-built ``AgentPrincipal``),
this suite proves the *whole wire* — real tables, real grants, real ticket,
real HTTP dispatch through the broker.

Scenario: owner has real access to two local tables (t1, t2). An agent
scoped to t1 only (``tables_mode='selected'``) is bound to a chat session.
A brokered ``/api/query`` for t2 as the agent must be denied; the SAME query
as the owner directly must succeed — that control is what proves the
restriction is the agent's own, not a broken grant elsewhere. A companion
test proves the default all-``'all'`` agent (every user's lazily-seeded
default) behaves byte-identically to the owner's plain session — the web-chat
regression guard. A final test cross-checks the audit snapshot
(``agent_scope_snapshots``) against the live-enforced intersection.

Verified against the current tree (V1d Tasks 1-5 already landed): every test
below passes. Reverting to pre-Task-3 (``_mint_identity_jwt`` with no agent
branch — the owner's identity JWT minted unconditionally) makes the primary
denial assertion fail: the agent-bound ticket would run under the owner's
full identity and read t2 (200, not 403) — exactly the confused-deputy gap
the design doc describes. This was confirmed by inspecting
``app/api/broker.py`` at ``0facd237^`` (pre-V1d): ``_mint_identity_jwt`` had
only the co-session/plain-owner branches, no ``agent_session`` branch at all.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from app.auth.jwt import create_access_token
from app.chat.types import Surface
from src.db import get_system_db


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _keboola_instance(monkeypatch):
    """``/api/admin/register-table`` refuses ``source_type='keboola'`` on the
    default unconfigured test instance (``get_data_source_type()`` ==
    ``'local'``) — mirrors ``tests/test_journey_sync_query.py``."""
    fake_cfg = {
        "data_source": {
            "type": "keboola",
            "keboola": {
                "stack_url": "https://connection.keboola.com",
                "project_id": "1234",
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
        },
    }
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda: fake_cfg, raising=False)
    from app.instance_config import reset_cache

    reset_cache()
    yield
    reset_cache()


def _grant_table_direct(conn, table_id: str, user_id: str, group_name: str) -> None:
    """Direct ``resource_grants(resource_type='table', ...)`` row — the
    no-admin-short-circuit grant primitive ``_allowed_ids_for_user`` (and
    therefore ``compute_agent_intersection``) reads. NOT the stack-gated
    data_package model an owner's own direct table access goes through (see
    ``grant_table_via_package`` in ``tests/conftest.py``) — this is the
    same simplification the co-session intersection
    (``src/grant_intersection.py``) already relies on (mirrors
    ``tests/test_copresence_datapath.py::_grant_table_direct``); V1d reuses
    that exact choke point rather than inventing a new one.
    """
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name)
    if not grp:
        grp = groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "table", table_id):
        grants.create(
            group_id=grp["id"],
            resource_type="table",
            resource_id=table_id,
            assigned_by="test",
            requirement="required",
        )


@pytest.fixture
def scoped_agent_env(e2e_env, mock_extract_factory, shared_app):
    """Owner has real local tables t1 + t2 (registered, extracted, rebuilt
    into analytics.duckdb as real views with real rows) and real access to
    both. An agent owned by them is scoped to t1 only. A second, default
    (all-'all') agent is bound to a sibling session for the regression test.
    """
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from fastapi.testclient import TestClient

    conn = get_system_db()
    UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")
    conn.close()

    app = shared_app
    client = TestClient(app)
    admin_token = create_access_token("admin1", "admin@test.com")
    owner_jwt = create_access_token("owner1", "owner@test.com")

    for name in ("t1", "t2"):
        r = client.post(
            "/api/admin/register-table",
            json={"name": name, "source_type": "keboola", "query_mode": "local", "description": name},
            headers=_auth(admin_token),
        )
        assert r.status_code == 201, r.text

    mock_extract_factory(
        "keboola",
        [
            {"name": "t1", "data": [{"id": "1", "region": "eu"}]},
            {"name": "t2", "data": [{"id": "1", "region": "secret"}]},
        ],
    )
    from src.orchestrator import SyncOrchestrator

    result = SyncOrchestrator(analytics_db_path=e2e_env["analytics_db"]).rebuild()
    assert set(result.get("keboola", [])) == {"t1", "t2"}

    conn = get_system_db()
    t1_id = TableRegistryRepository(conn).get_by_name("t1")["id"]
    t2_id = TableRegistryRepository(conn).get_by_name("t2")["id"]

    # Owner's REAL access to both tables — stack-gated (data_package) model,
    # exactly how a real admin grants an analyst visibility.
    from tests.conftest import grant_table_via_package

    grant_table_via_package(conn, t1_id, "owner1", group_name="e2e-agent-scope-pkg")
    grant_table_via_package(conn, t2_id, "owner1", group_name="e2e-agent-scope-pkg")

    # The raw resource_grants(resource_type='table') rows the intersection
    # primitive reads (see _grant_table_direct docstring above).
    _grant_table_direct(conn, t1_id, "owner1", "e2e-agent-scope-direct")
    _grant_table_direct(conn, t2_id, "owner1", "e2e-agent-scope-direct")

    from src.repositories import agents_repo, chat_session_repo, ticket_repo

    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id="owner1",
        name="Scoped Agent",
        slug="scoped-agent-e2e",
        tables_mode="selected",
        plugins_mode="all",
        connections_mode="all",
        memory_mode="all",
    )
    agents_repo().set_scope(agent_id, [("table", t1_id)])

    session = chat_session_repo().create_session(user_email="owner@test.com", surface=Surface.WEB, agent_id=agent_id)
    tok = ticket_repo().mint(session.id, "main", ttl_seconds=60)

    default_agent = agents_repo().get_or_create_default("owner1")
    default_session = chat_session_repo().create_session(
        user_email="owner@test.com", surface=Surface.WEB, agent_id=default_agent["id"]
    )
    default_tok = ticket_repo().mint(default_session.id, "main", ttl_seconds=60)

    conn.close()

    return {
        "app": app,
        "owner_id": "owner1",
        "owner_jwt": owner_jwt,
        "t1_id": t1_id,
        "t2_id": t2_id,
        "agent_id": agent_id,
        "session_id": session.id,
        "tok": tok,
        "default_agent_id": default_agent["id"],
        "default_session_id": default_session.id,
        "default_tok": default_tok,
    }


def _broker_query(client: httpx.AsyncClient, tok: str, sql: str):
    return client.post(
        "/api/broker/agnes-api",
        headers=_auth(tok),
        json={"method": "POST", "path": "/api/query", "body": {"sql": sql}},
    )


def _direct_query(client: httpx.AsyncClient, jwt: str, sql: str):
    return client.post("/api/query", json={"sql": sql}, headers=_auth(jwt))


# ---------------------------------------------------------------------------
# The finding-closing proof
# ---------------------------------------------------------------------------


def test_scoped_agent_cannot_query_a_table_outside_its_scope(scoped_agent_env):
    """The reviewer's HIGH finding, closed: a 'selected'-scoped agent's
    brokered request is authorized against (owner grants ∩ agent scope), not
    the owner's full grants.

    - brokered /api/query for t2 (outside the agent's scope) -> 403
    - the SAME query as the owner directly -> 200 (the control: t2 is a
      real, granted table — the denial above is the agent's own
      restriction, not a broken/missing grant).
    - brokered /api/query for t1 (inside the agent's scope) -> 200 (the
      agent is restricted, not fully locked out).
    """
    env = scoped_agent_env

    async def _run():
        transport = httpx.ASGITransport(app=env["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            agent_t2 = await _broker_query(c, env["tok"], "SELECT * FROM t2")
            agent_t1 = await _broker_query(c, env["tok"], "SELECT * FROM t1")
            owner_t2 = await _direct_query(c, env["owner_jwt"], "SELECT * FROM t2")
            return agent_t2, agent_t1, owner_t2

    agent_t2, agent_t1, owner_t2 = asyncio.run(_run())

    assert agent_t2.status_code == 403, agent_t2.text
    assert "t2" in agent_t2.text

    assert agent_t1.status_code == 200, agent_t1.text
    assert agent_t1.json()["row_count"] == 1

    assert owner_t2.status_code == 200, owner_t2.text
    assert owner_t2.json()["row_count"] == 1


def test_owner_directly_retains_full_access_to_both_tables(scoped_agent_env):
    """Second half of the control, isolated: the owner (no agent in the
    picture at all) can read both t1 and t2 — the grants themselves are
    fine; only the scoped agent is restricted."""
    env = scoped_agent_env

    async def _run():
        transport = httpx.ASGITransport(app=env["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r1 = await _direct_query(c, env["owner_jwt"], "SELECT * FROM t1")
            r2 = await _direct_query(c, env["owner_jwt"], "SELECT * FROM t2")
            return r1, r2

    r1, r2 = asyncio.run(_run())
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text


# ---------------------------------------------------------------------------
# Web-chat regression: the default all-'all' agent changes nothing
# ---------------------------------------------------------------------------


def test_default_all_agent_matches_owner_directly(scoped_agent_env):
    """Every user's lazily-seeded default agent has every mode at 'all' —
    it must not narrow anything. The broker's default-agent carve-out keeps
    it on the plain owner-identity JWT path (never mints an AgentPrincipal),
    so a brokered request under the default agent must be byte-identical to
    the owner's own direct request — including for t2, which the scoped
    agent above was denied."""
    env = scoped_agent_env

    async def _run():
        transport = httpx.ASGITransport(app=env["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            replayed = await _broker_query(c, env["default_tok"], "SELECT * FROM t2")
            direct = await _direct_query(c, env["owner_jwt"], "SELECT * FROM t2")
            return replayed, direct

    replayed, direct = asyncio.run(_run())
    assert replayed.status_code == 200, replayed.text
    assert direct.status_code == 200, direct.text
    assert replayed.json() == direct.json()


# ---------------------------------------------------------------------------
# Audit snapshot now matches what is enforced
# ---------------------------------------------------------------------------


def test_audit_snapshot_matches_enforced_intersection(scoped_agent_env):
    """`agent_scope_snapshots` (written at spawn time by
    `app.chat.agent_profile.record_snapshot`) must describe exactly what
    `compute_agent_intersection` enforces at request time — the audit trail
    and the live seam can never disagree."""
    import src.agent_scope_intersection as intersection_mod
    from app.chat import agent_profile
    from src.repositories import agents_repo

    env = scoped_agent_env
    agent_row = agents_repo().get_by_id(env["agent_id"])

    agent_profile.record_snapshot(env["session_id"], agent_row)

    snapshots = agents_repo().list_scope_snapshots(env["session_id"])
    assert len(snapshots) == 1
    effective = json.loads(snapshots[0]["effective_scope"])
    assert effective["tables"] == [env["t1_id"]]

    enforced = intersection_mod.compute_agent_intersection(env["owner_id"], agent_row)
    assert enforced.get("table") == frozenset({env["t1_id"]})
    assert set(effective["tables"]) == enforced["table"]
    assert env["t2_id"] not in enforced.get("table", frozenset())


# ---------------------------------------------------------------------------
# The v2 read endpoints under an AgentPrincipal
# ---------------------------------------------------------------------------


def _broker_call(client: httpx.AsyncClient, tok: str, method: str, path: str, body=None):
    payload = {"method": method, "path": path}
    if body is not None:
        payload["body"] = body
    return client.post("/api/broker/agnes-api", headers=_auth(tok), json=payload)


def _audit_user_ids(action: str) -> list:
    conn = get_system_db()
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT user_id FROM audit_log WHERE action = ? AND result = 'success' ORDER BY timestamp",
                [action],
            ).fetchall()
        ]
    finally:
        conn.close()


def test_agent_reads_an_in_scope_table_through_the_v2_endpoints(scoped_agent_env):
    """An AgentPrincipal is a frozen dataclass with no ``.get``. Every one of
    these handlers writes an audit row keyed off the caller identity, and the
    write sits inside a `try/except Exception` that swallows failures — so a
    `.get` on the principal either 500'd the request (``/api/v2/scan``, whose
    quota-key derivation is outside every `except`) or silently dropped the
    row. Only denial paths were covered before, and a denial never reaches
    these call sites. Assert the ALLOWED path end to end, plus the audit row
    attributed to the owner (an agent runs on its owner's behalf)."""
    env = scoped_agent_env
    t1 = env["t1_id"]

    async def _run():
        transport = httpx.ASGITransport(app=env["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return (
                await _broker_call(c, env["tok"], "GET", f"/api/v2/sample/{t1}"),
                await _broker_call(c, env["tok"], "GET", f"/api/v2/schema/{t1}"),
                await _broker_call(c, env["tok"], "POST", "/api/v2/scan/estimate", {"table_id": t1}),
                await _broker_call(c, env["tok"], "GET", f"/api/data/{t1}/check-access"),
            )

    sample, schema, estimate, access = asyncio.run(_run())

    assert sample.status_code == 200, sample.text
    assert schema.status_code == 200, schema.text
    assert estimate.status_code == 200, estimate.text
    assert access.status_code in (200, 204), access.text

    for action in ("catalog.sample", "catalog.schema", "snapshot.estimate", "data.access_check"):
        ids = _audit_user_ids(action)
        assert ids, f"no successful audit_log row for {action}"
        assert ids[-1] == env["owner_id"], (action, ids[-1])
