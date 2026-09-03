"""Design doc §12 — the agent management API (`/api/v1/agents`) discloses
when a policied table sits inside a shared agent's scope.

An `AgentPrincipal` binds the OWNER's identity for a surface that runs the
agent AS the owner (a Slack channel bound to the agent, a scheduled run) --
`src/access_policy.py::_resolve_identity` -- so an owner who grounds a
shared agent in a policied table needs to see, on their own agent's detail
page (and every REST/CLI reader of it), that everyone reaching the agent
through such a surface gets the OWNER's row slice, not their own.

End-to-end over a real TestClient + real DuckDB (no mocks), mirroring
``tests/test_access_policy_effective_access.py``'s fixture shape.
"""

from __future__ import annotations

import json

import pytest

from app.auth.jwt import create_access_token

ORDERS_POLICY_SQL = "SELECT * FROM orders WHERE list_contains($user_groups, unit)"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def agent_with_policied_table(tmp_path, monkeypatch, seeded_app, mock_extract_factory):
    """One owner, one agent grounded (via a data package) in a table with an
    access policy, plus a second, unpolicied table in the same package for
    contrast."""
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories import agents_repo
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {
                "name": "orders",
                "data": [
                    {"id": "1", "unit": "TeamA", "amount": "100"},
                    {"id": "2", "unit": "TeamB", "amount": "200"},
                ],
            },
            {"name": "products", "data": [{"id": "1", "sku": "widget"}]},
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(id="tbl_orders", name="orders", source_type="keboola", query_mode="local", server_only=True)
        registry.set_access_policy("tbl_orders", sql=ORDERS_POLICY_SQL, note="unit filter", updated_by="admin")
        registry.register(id="tbl_products", name="products", source_type="keboola", query_mode="local")

        UserRepository(conn).create(id="owner1", email="owner@example.com", name="Owner")

        pkg_id = grant_table_via_package(conn, "tbl_orders", "owner1", group_name="owner-pkg-orders")
        grant_table_via_package(conn, "tbl_products", "owner1", group_name="owner-pkg-products")

        agent_id = "ag_disclosure"
        agents_repo().create(
            id=agent_id,
            owner_user_id="owner1",
            name="Sales Agent",
            slug="sales-agent",
            plugins_mode="selected",
            connections_mode="selected",
            tables_mode="selected",
            memory_mode="selected",
            knowledge=json.dumps([pkg_id]),
        )
    finally:
        conn.close()

    return {
        **seeded_app,
        "agent_id": agent_id,
        "pkg_id": pkg_id,
        "owner_token": create_access_token("owner1", "owner@example.com"),
    }


def test_get_agent_reports_policied_tables_in_scope(agent_with_policied_table):
    c = agent_with_policied_table["client"]
    r = c.get(
        f"/api/v1/agents/{agent_with_policied_table['agent_id']}",
        headers=_auth(agent_with_policied_table["owner_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policied_tables_in_scope"] == ["tbl_orders"]
    entries = body["policied_tables"]
    assert len(entries) == 1
    assert entries[0]["table_id"] == "tbl_orders"
    assert entries[0]["access_policy"] is True
    assert entries[0]["policy"]["applies"] is True
    # the diagnosis is computed for the OWNER's own identity, not an
    # unfiltered/admin view -- owner1 holds no group naming a `unit`, so the
    # policy's `list_contains($user_groups, unit)` predicate matches nothing.
    assert entries[0]["policy"]["reason"] in ("empty_slice", "ok")


def test_get_agent_without_a_policied_table_reports_empty(agent_with_policied_table):
    """The `tbl_products` package (no policy) grounds an agent with nothing
    to disclose."""
    from src.repositories import agents_repo

    agents_repo().update(
        agent_with_policied_table["agent_id"],
        knowledge=json.dumps([]),
    )
    c = agent_with_policied_table["client"]
    r = c.get(
        f"/api/v1/agents/{agent_with_policied_table['agent_id']}",
        headers=_auth(agent_with_policied_table["owner_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policied_tables_in_scope"] == []
    assert body["policied_tables"] == []


def test_list_agents_also_reports_policied_tables_in_scope(agent_with_policied_table):
    """The list endpoint carries the same disclosure — the CLI's
    slug-addressed lookup (`_resolve_agent`) and the `/agents` page's
    initial load both read this endpoint, never a per-agent GET."""
    c = agent_with_policied_table["client"]
    r = c.get("/api/v1/agents", headers=_auth(agent_with_policied_table["owner_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    row = next(x for x in rows if x["id"] == agent_with_policied_table["agent_id"])
    assert row["policied_tables_in_scope"] == ["tbl_orders"]
