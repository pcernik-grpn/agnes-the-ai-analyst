"""Design doc §12 — the `/agents` builder page renders the same self-audit
disclosure the management API carries, for a data package the caller could
ground an agent in (`knowledge_sources_for`, embedded as the page's
``ag-knowledge-data`` script tag) — a candidate the JS picker offers is
"selectable for" an agent's scope exactly as much as an already-attached one.

Only the DATA (server-rendered JSON) side is asserted here — the client-side
callout it feeds (``policyDisclosureNote`` in ``agents.html``) is plain JS
with no server-side render to snapshot; the wiring is a straight ``if
(ids.length)`` read off this same payload.
"""

from __future__ import annotations

import json
import re

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _knowledge_data(body: str) -> list:
    m = re.search(
        r'<script type="application/json" id="ag-knowledge-data">(.*?)</script>',
        body,
        re.DOTALL,
    )
    assert m, "ag-knowledge-data script tag not found in /agents page"
    return json.loads(m.group(1))


ORDERS_POLICY_SQL = "SELECT * FROM orders WHERE list_contains($user_groups, unit)"


@pytest.fixture
def analyst_with_policied_package(seeded_app, mock_extract_factory):
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from tests.conftest import grant_table_via_package

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [{"name": "orders", "data": [{"id": "1", "unit": "TeamA", "amount": "100"}]}],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(id="tbl_orders", name="orders", source_type="keboola", query_mode="local", server_only=True)
        registry.set_access_policy("tbl_orders", sql=ORDERS_POLICY_SQL, note="unit filter", updated_by="admin")
        pkg_id = grant_table_via_package(conn, "tbl_orders", "analyst1", group_name="analyst-pkg-orders")
    finally:
        conn.close()

    return {**seeded_app, "pkg_id": pkg_id}


def test_agents_page_flags_a_policied_data_package(analyst_with_policied_package):
    c = analyst_with_policied_package["client"]
    resp = c.get("/agents", headers=_auth(analyst_with_policied_package["analyst_token"]))
    assert resp.status_code == 200, resp.text
    rows = _knowledge_data(resp.text)
    entry = next(r for r in rows if r["id"] == analyst_with_policied_package["pkg_id"])
    assert entry["access_policy"] is True
    assert entry["policied_tables"][0]["table_id"] == "tbl_orders"


def test_agents_page_does_not_flag_an_unpolicied_package(seeded_app, mock_extract_factory):
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from tests.conftest import grant_table_via_package

    env = seeded_app["env"]
    mock_extract_factory("keboola", [{"name": "products", "data": [{"id": "1", "sku": "widget"}]}])
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(id="tbl_products", name="products", source_type="keboola", query_mode="local")
        pkg_id = grant_table_via_package(conn, "tbl_products", "analyst1", group_name="analyst-pkg-products")
    finally:
        conn.close()

    c = seeded_app["client"]
    resp = c.get("/agents", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200, resp.text
    rows = _knowledge_data(resp.text)
    entry = next(r for r in rows if r["id"] == pkg_id)
    assert entry["access_policy"] is False
    assert entry["policied_tables"] == []
