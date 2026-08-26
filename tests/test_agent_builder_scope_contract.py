"""The /agents builder's declaration is the agent's ENFORCED scope.

Before this, ``POST``/``PATCH /api/agents`` (the builder's now-deleted
adapter router, folded into ``/api/v1/agents`` by the remediation-program's
"one agent model" Track C1) wrote only the ``knowledge`` / ``plugins`` JSON
columns and left all four ``*_mode`` columns at the repository default
``'all'`` — the passthrough shape. A builder agent the page showed as scoped
to one data package therefore ran with its owner's ENTIRE stack, and could
not be issued a PAT at all (``agents_admin.py::create_agent_token`` requires
all four modes ``'selected'``). This suite is the contract for the fix,
driven end to end through the real surfaces (now ``/api/v1/agents``, the
builder's own wire shape): the endpoint writes the scope, the broker mints
an ``AgentPrincipal`` for it, and a brokered query outside the declared
scope is refused.

Deliberately different from ``tests/test_agent_scope_e2e.py`` in two ways
that matter:

1. The agent is created by calling the BUILDER endpoint, not by writing an
   ``agents`` row with modes pre-set. That is what regressed, so that is what
   is exercised.
2. The owner reaches their tables ONLY through data packages — no per-table
   ``resource_grants`` rows. This is the shape real instances have (the
   unified-stack model routes analyst table access through packages), and a
   grants-only intersection denied such an agent every table while the
   catalog still listed them.

Reverting either half of the fix fails a test here: dropping the mode/scope
write in ``app/api/agents_admin.py`` makes the out-of-scope query return 200
(passthrough), and dropping the package expansion in
``src/agent_scope_intersection.py`` makes the in-scope query 403 (the agent
loses the tables its owner only holds via a package).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token
from app.chat.types import Surface
from src.db import get_system_db


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _keboola_instance(monkeypatch):
    """``/api/admin/register-table`` refuses ``source_type='keboola'`` on the
    default unconfigured test instance — mirrors
    ``tests/test_agent_scope_e2e.py``."""
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
    # ``**_`` matters: the loader is also called with ``strict=`` and a
    # signature-mismatched stub is swallowed into a "Could not load instance
    # config" warning, leaving the test running against the default local
    # instance it meant to replace.
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda *_a, **_kw: fake_cfg, raising=False)
    from app.instance_config import reset_cache

    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def builder_env(e2e_env, mock_extract_factory, shared_app):
    """Owner holds t1 and t2 through two SEPARATE data packages and no
    per-table grants. A builder agent is then created through
    ``POST /api/agents`` declaring only the package holding t1.
    """
    from fastapi.testclient import TestClient as _TC  # noqa: F401  (clarity at the call site)
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

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

    assert set(SyncOrchestrator(analytics_db_path=e2e_env["analytics_db"]).rebuild().get("keboola", [])) == {
        "t1",
        "t2",
    }

    conn = get_system_db()
    t1_id = TableRegistryRepository(conn).get_by_name("t1")["id"]
    t2_id = TableRegistryRepository(conn).get_by_name("t2")["id"]
    # Package-only access, one package per table, NO direct table grants.
    pkg1 = grant_table_via_package(conn, t1_id, "owner1", group_name="builder-scope-pkg1")
    pkg2 = grant_table_via_package(conn, t2_id, "owner1", group_name="builder-scope-pkg2")
    conn.close()

    # The builder call under test: declare the package holding t1 only.
    created = client.post(
        "/api/v1/agents",
        json={"name": "Scoped Builder Agent", "knowledge": [pkg1], "status": "ready"},
        headers=_auth(owner_jwt),
    )
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]

    from src.repositories import chat_session_repo, ticket_repo

    session = chat_session_repo().create_session(user_email="owner@test.com", surface=Surface.WEB, agent_id=agent_id)
    tok = ticket_repo().mint(session.id, "main", ttl_seconds=60)

    return {
        "app": app,
        "client": client,
        "owner_jwt": owner_jwt,
        "admin_token": admin_token,
        "agent_id": agent_id,
        "t1_id": t1_id,
        "t2_id": t2_id,
        "pkg1": pkg1,
        "pkg2": pkg2,
        "tok": tok,
        "session_id": session.id,
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
# What the builder writes
# ---------------------------------------------------------------------------


def test_builder_create_writes_enforced_scope(builder_env):
    """The declaration lands in ``agent_scope`` and every mode is
    'selected' — an agent created through the builder is never a
    passthrough riding its owner's whole stack."""
    from src.agent_scope_intersection import agent_is_passthrough
    from src.repositories import agents_repo

    row = agents_repo().get_by_id(builder_env["agent_id"])
    assert [row[f] for f in ("tables_mode", "plugins_mode", "connections_mode", "memory_mode")] == ["selected"] * 4
    assert agent_is_passthrough(row) is False
    scope = {(i["item_type"], i["item_id"]) for i in agents_repo().get_scope(builder_env["agent_id"])}
    assert scope == {("data_package", builder_env["pkg1"])}


def test_builder_patch_rewrites_scope_and_preserves_governance_rows(builder_env):
    """Editing the declaration replaces the builder-owned rows only — a
    Slack binding (or any governance-set row) must survive, or the channel
    silently unroutes and its turns fall back to the mentioning user's own
    authority."""
    from src.repositories import agents_repo

    agent_id = builder_env["agent_id"]
    repo = agents_repo()
    existing = [(i["item_type"], i["item_id"]) for i in repo.get_scope(agent_id)]
    repo.set_scope(agent_id, existing + [("slack_channel", "C123"), ("connection", "conn-1")])

    r = builder_env["client"].put(
        f"/api/v1/agents/{agent_id}",
        json={"knowledge": [builder_env["pkg2"]]},
        headers=_auth(builder_env["owner_jwt"]),
    )
    assert r.status_code == 200, r.text

    scope = {(i["item_type"], i["item_id"]) for i in repo.get_scope(agent_id)}
    assert ("data_package", builder_env["pkg2"]) in scope
    assert ("data_package", builder_env["pkg1"]) not in scope  # replaced, not merged
    assert ("slack_channel", "C123") in scope  # governance-owned, preserved
    assert ("connection", "conn-1") in scope


def test_patch_that_does_not_touch_the_declaration_leaves_scope_alone(builder_env):
    """A rename must not wipe the enforced scope — ``set_scope`` replaces
    the whole set, so re-deriving on every PATCH would drop everything a
    payload happened not to carry."""
    from src.repositories import agents_repo

    agent_id = builder_env["agent_id"]
    before = {(i["item_type"], i["item_id"]) for i in agents_repo().get_scope(agent_id)}
    r = builder_env["client"].put(
        f"/api/v1/agents/{agent_id}",
        json={"name": "Renamed"},
        headers=_auth(builder_env["owner_jwt"]),
    )
    assert r.status_code == 200, r.text
    assert {(i["item_type"], i["item_id"]) for i in agents_repo().get_scope(agent_id)} == before


def test_builder_read_shows_cli_set_scope_so_an_edit_cannot_silently_drop_it(builder_env):
    """A CLI/governance-set builder-axis row must be VISIBLE in the builder.

    `agnes agent scope set --plugin X --memory-domain Y` writes `agent_scope`
    rows without touching the `knowledge`/`plugins` JSON columns, and the same
    agent is listed in `/agents` (`list_for_user` returns every owned agent).
    So the builder rendered "0 sources · 0 tools" for an agent that in fact had
    scope, and the first declaration edit wiped those four builder-owned axes —
    `plugin`, `memory_domain`, `data_package`, `collection` — with the user
    never having seen what they were destroying.

    Failing closed makes it a capability loss rather than a leak, but it is
    still the same class of bug this whole change exists to remove: a builder
    screen that disagrees with what the runtime holds. The read projection
    therefore hydrates both lists from `agent_scope` when the JSON columns are
    empty, so the screen shows the truth and a save round-trips.
    """
    from src.repositories import agents_repo

    agent_id = builder_env["agent_id"]
    pkg = builder_env["pkg1"]
    repo = agents_repo()
    # Governance-shaped state: scope rows exist, JSON columns say nothing.
    repo.set_scope(agent_id, [("plugin", "plug-a"), ("data_package", pkg)])
    repo.update(agent_id, knowledge="[]", plugins="[]")

    r = builder_env["client"].get(f"/api/v1/agents/{agent_id}", headers=_auth(builder_env["owner_jwt"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "plug-a" in body["plugins"], "builder must show the CLI-set plugin scope"
    assert pkg in body["knowledge"], "builder must show the CLI-set data package"

    # And the round trip is lossless: saving back what the screen showed keeps
    # it. (`_classify_knowledge` drops an id matching no registry, by design —
    # so this asserts on ids that really exist, which is the case that matters.)
    r = builder_env["client"].put(
        f"/api/v1/agents/{agent_id}",
        json={"knowledge": body["knowledge"], "plugins": body["plugins"]},
        headers=_auth(builder_env["owner_jwt"]),
    )
    assert r.status_code == 200, r.text
    scope = {(i["item_type"], i["item_id"]) for i in repo.get_scope(agent_id)}
    assert ("plugin", "plug-a") in scope
    assert ("data_package", pkg) in scope


def test_partial_declaration_patch_keeps_the_axis_it_did_not_send(builder_env):
    """A PATCH carrying only one axis must not wipe the other one.

    `set_scope` replaces the builder-owned set wholesale, so the half the
    payload did not send has to be read back off the agent. Reading it from
    the raw `knowledge`/`plugins` JSON columns is wrong for exactly the agents
    the read-side hydration exists for: scoped through `agnes agent scope set`,
    they hold real `agent_scope` rows while those columns stay empty. A save
    touching only `plugins` then replaced the knowledge axis with nothing.

    Worse after the read fix, not better — `GET` now shows the true scope, so
    the screen the user acts on looks correct right up until the partial save
    silently drops half of it. The unsent axis is therefore derived from the
    same hydrated view the builder was shown.
    """
    from src.repositories import agents_repo

    agent_id = builder_env["agent_id"]
    pkg = builder_env["pkg1"]
    repo = agents_repo()
    repo.set_scope(agent_id, [("plugin", "plug-a"), ("data_package", pkg)])
    repo.update(agent_id, knowledge="[]", plugins="[]")

    # Touch ONLY the capabilities axis.
    r = builder_env["client"].put(
        f"/api/v1/agents/{agent_id}",
        json={"plugins": ["plug-b"]},
        headers=_auth(builder_env["owner_jwt"]),
    )
    assert r.status_code == 200, r.text

    scope = {(i["item_type"], i["item_id"]) for i in repo.get_scope(agent_id)}
    assert ("plugin", "plug-b") in scope, "the sent axis is replaced"
    assert ("plugin", "plug-a") not in scope, "replaced, not merged"
    assert ("data_package", pkg) in scope, "the UNSENT axis must survive the save"


def test_listing_reads_scope_once_for_the_whole_page(builder_env, monkeypatch):
    """Hydrating the declaration must not turn the listing into an N+1.

    `_agent_out` fills an empty `knowledge`/`plugins` axis from `agent_scope`,
    and `list_agents` projects every agent the caller can see — so a per-agent
    `get_scope` would scale the listing with the agent count, and it fires for
    the common shape where only one of the two axes is populated. The page
    takes one batched read instead.
    """
    from src.repositories import agents_repo

    repo = agents_repo()
    # Three more agents with scope rows and empty declarations — the shape
    # that makes hydration fire.
    for n in ("n1", "n2", "n3"):
        repo.create(id=n, owner_user_id="owner1", name=n.upper(), slug=n)
        repo.set_scope(n, [("plugin", f"p-{n}")])

    calls: list = []
    real_single = type(repo).get_scope

    def counting_single(self, agent_id):
        calls.append(agent_id)
        return real_single(self, agent_id)

    monkeypatch.setattr(type(repo), "get_scope", counting_single)

    r = builder_env["client"].get("/api/v1/agents", headers=_auth(builder_env["owner_jwt"]))
    assert r.status_code == 200, r.text
    listed = {a["id"] for a in r.json()["data"]}
    assert {"n1", "n2", "n3"} <= listed

    assert calls == [], f"listing must not read scope per agent, got {calls}"
    # …and the batched read really did hydrate: the declaration is not empty.
    by_id = {a["id"]: a for a in r.json()["data"]}
    assert by_id["n1"]["plugins"] == ["p-n1"]


# ---------------------------------------------------------------------------
# What the runtime enforces
# ---------------------------------------------------------------------------


def test_builder_agent_cannot_reach_outside_its_declared_scope(builder_env):
    """The contract: a brokered query for the undeclared table is refused,
    the declared one succeeds, and the owner reads both directly.

    The owner control is what proves the denial is the agent's own
    restriction rather than a missing grant — and, because the owner holds
    both tables through data packages only, the in-scope 200 also proves a
    declared package confers its member tables.
    """
    env = builder_env

    async def _run():
        transport = httpx.ASGITransport(app=env["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return (
                await _broker_query(c, env["tok"], "SELECT * FROM t2"),
                await _broker_query(c, env["tok"], "SELECT * FROM t1"),
                await _direct_query(c, env["owner_jwt"], "SELECT * FROM t2"),
                await _direct_query(c, env["owner_jwt"], "SELECT * FROM t1"),
            )

    agent_t2, agent_t1, owner_t2, owner_t1 = asyncio.run(_run())

    assert agent_t2.status_code == 403, agent_t2.text
    assert agent_t1.status_code == 200, agent_t1.text
    assert agent_t1.json()["row_count"] == 1
    assert owner_t2.status_code == 200, owner_t2.text
    assert owner_t1.status_code == 200, owner_t1.text


def test_intersection_is_exactly_the_declaration(builder_env):
    """Unit-level cross-check of the same property, so a failure localizes:
    the enforced table set is the declared package's members, nothing more."""
    from src.agent_scope_intersection import resolve_agent_authority

    enforced = resolve_agent_authority(builder_env["agent_id"])
    assert enforced.get("table") == frozenset({builder_env["t1_id"]})
    assert builder_env["t2_id"] not in enforced.get("table", frozenset())
    assert enforced.get("data_package") == frozenset({builder_env["pkg1"]})


def test_agent_cannot_reach_past_the_owners_own_stack_in_classic_mode(builder_env, monkeypatch):
    """An agent must never reach a table its OWNER cannot query.

    ``can_access_table`` authorizes a table through ``StackResolver.stack``,
    whose classic formula (``features.stack_auto_membership: false``) is
    ``required ∪ (subscribed ∩ available)`` — an *available* package the
    owner never subscribed to is NOT in their stack, and a direct query for
    its tables is refused. Deriving the owner side from raw grants
    (required ∪ available, subscription ignored) would hand the agent
    exactly that package's tables: strictly more than its owner has, which
    is the one thing the intersection exists to prevent.
    """
    from tests.conftest import grant_table_via_package

    monkeypatch.setattr("app.instance_config.get_stack_auto_membership", lambda: False, raising=False)
    monkeypatch.setattr("app.services.stack_resolver.get_stack_auto_membership", lambda: False, raising=False)

    # A table reachable ONLY through an AVAILABLE, never-subscribed package.
    # It needs no extract or view: the assertions below are grant-level
    # (can_access_table / the intersection), not query-level.
    reg = builder_env["client"].post(
        "/api/admin/register-table",
        json={"name": "t3", "source_type": "keboola", "query_mode": "local", "description": "t3"},
        headers=_auth(builder_env["admin_token"]),
    )
    assert reg.status_code == 201, reg.text

    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    t3_id = TableRegistryRepository(conn).get_by_name("t3")["id"]
    pkg_avail = grant_table_via_package(
        conn,
        t3_id,
        "owner1",
        group_name="builder-scope-available",
        requirement="available",
    )
    conn.close()

    agent_id = builder_env["agent_id"]
    r = builder_env["client"].put(
        f"/api/v1/agents/{agent_id}",
        json={"knowledge": [pkg_avail]},
        headers=_auth(builder_env["owner_jwt"]),
    )
    assert r.status_code == 200, r.text

    # The owner's own authority is the ceiling: confirm the control first,
    # so a failure below cannot be mistaken for a broken grant.
    from src.rbac import can_access_table

    owner_may = can_access_table({"id": "owner1", "email": "owner@test.com"}, t3_id)
    assert owner_may is False, "fixture is wrong — the owner must NOT reach this table in classic mode"

    from src.agent_scope_intersection import resolve_agent_authority

    enforced = resolve_agent_authority(agent_id)
    assert t3_id not in enforced.get("table", frozenset()), (
        "the agent reached a table its owner cannot query — the owner side must honour the "
        "stack formula, not raw grants"
    )


def test_builder_agent_can_be_issued_a_pat(builder_env):
    """A builder agent used to be API-unreachable by construction: PAT
    issuance requires all four modes 'selected', which the builder never
    set (403 ``agent_not_selected_mode``). With the declaration enforced,
    the token issues — the agent-as-API surface is open to builder agents."""
    r = builder_env["client"].post(
        f"/api/v1/agents/{builder_env['agent_id']}/tokens",
        json={"name": "ci-token", "expires_in_days": 1},
        headers=_auth(builder_env["owner_jwt"]),
    )
    assert r.status_code in (200, 201), r.text
    assert r.json().get("token")
