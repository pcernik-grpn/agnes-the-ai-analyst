"""S3 (RLS review, #1979) -- POST /api/query surfaces an empty/unsynced
``policy_mapping`` dependency as a structured ``policy_mapping_empty`` error
instead of a silent, indistinguishable-from-legitimate empty result.

``GET /api/me/effective-access`` already raises this exact condition as
``reason: mapping_empty`` (`app/api/access.py::_table_policy_diagnosis`,
which calls the shared `src.access_policy.raise_if_policy_mapping_empty`).
Before this change, the live query path (`POST /api/query`) had no
equivalent check at all -- a policy that joins a broken mapping table just
returned ``row_count: 0`` for every caller, forever, with nothing to tell
them apart from "you legitimately have no data" (docs/table-access-
policies.md v1 limitation #3).

Three tables:

- ``tbl_orders`` (server_only): policied, joins ``user_access`` -- registered
  as ``policy_mapping=True`` but NEVER extracted/synced (no ``sync_state``
  row at all, the "sync never ran" half of the trap). Every query touching
  it must 500 with ``reason: policy_mapping_empty`` rather than a filtered
  0-row 200.
- ``tbl_invoices`` (server_only): policied, joins ``user_access2`` -- a
  mapping table that DID sync, with a real matching row. A live query
  through it must behave exactly as before this change: an ordinary
  filtered 200.
- ``tbl_products``: no policy at all -- completely unaffected, control case.
- ``tbl_shipments`` (server_only): policied, joins ``User_Access3`` -- a
  mapping table that DID sync (a real ``sync_state`` row) but has zero rows,
  referenced in mixed case. Because the table DID sync, its view genuinely
  exists in DuckDB (case-insensitively), so pre-fix this executed normally
  and silently returned 0 rows instead of tripping the empty-mapping check
  (finding 1, PR #2023 review).
"""

from __future__ import annotations

import pytest

ORDERS_POLICY_SQL = "SELECT * FROM orders WHERE unit IN (SELECT unit FROM user_access WHERE email = $user_email)"
INVOICES_POLICY_SQL = "SELECT * FROM invoices WHERE unit IN (SELECT unit FROM user_access2 WHERE email = $user_email)"
# Deliberately mixed-case reference to the synced-but-empty `user_access3`
# mapping table.
SHIPMENTS_POLICY_SQL = "SELECT * FROM shipments WHERE unit IN (SELECT unit FROM User_Access3 WHERE email = $user_email)"


@pytest.fixture
def mapping_workspace(seeded_app, mock_extract_factory, monkeypatch):
    import duckdb

    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

    env = seeded_app["env"]
    db_path = mock_extract_factory(
        "keboola",
        [
            {
                "name": "orders",
                "data": [
                    {"id": "1", "unit": "TeamA", "amount": "100"},
                    {"id": "2", "unit": "TeamB", "amount": "200"},
                ],
            },
            {
                "name": "invoices",
                "data": [
                    {"id": "1", "unit": "TeamA", "total": "10"},
                    {"id": "2", "unit": "TeamB", "total": "20"},
                ],
            },
            {"name": "products", "data": [{"id": "1", "sku": "widget"}]},
            {
                "name": "shipments",
                "data": [
                    {"id": "1", "unit": "TeamA", "qty": "5"},
                    {"id": "2", "unit": "TeamB", "qty": "7"},
                ],
            },
            # user_access2 IS synced -- a real matching row for team-a.
            {
                "name": "user_access2",
                "data": [{"email": "team-a@example.com", "unit": "TeamA"}],
            },
            # user_access3 IS synced (real sync_state row / last_sync), but
            # the extract itself is empty -- "sync ran, mapping table is
            # just empty" rather than "sync never ran". `create_mock_extract`
            # treats an empty `data` list as "remote or empty table" and
            # stubs in an `(id VARCHAR)`-only table -- overwritten below with
            # the real `email`/`unit` columns the policy body references, so
            # the mapping table's view genuinely resolves (case-insensitively
            # too) with zero rows, rather than erroring on a missing column.
            {"name": "user_access3", "data": []},
            # user_access is registered as policy_mapping below but is
            # deliberately absent from this extract batch -- never synced,
            # no sync_state row at all.
        ],
    )
    _conn = duckdb.connect(str(db_path))
    _conn.execute('CREATE OR REPLACE TABLE "user_access3" (email VARCHAR, unit VARCHAR)')
    _conn.close()

    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)

        registry.register(id="tbl_orders", name="orders", source_type="keboola", query_mode="local", server_only=True)
        registry.set_access_policy("tbl_orders", sql=ORDERS_POLICY_SQL, note="mapping filter", updated_by="admin")

        registry.register(
            id="tbl_invoices", name="invoices", source_type="keboola", query_mode="local", server_only=True
        )
        registry.set_access_policy("tbl_invoices", sql=INVOICES_POLICY_SQL, note="mapping filter", updated_by="admin")

        registry.register(id="tbl_products", name="products", source_type="keboola", query_mode="local")

        registry.register(
            id="tbl_shipments", name="shipments", source_type="keboola", query_mode="local", server_only=True
        )
        registry.set_access_policy("tbl_shipments", sql=SHIPMENTS_POLICY_SQL, note="mapping filter", updated_by="admin")

        # Registered as a mapping table, but never extracted/synced.
        registry.register(id="user_access", name="user_access", source_type="keboola", query_mode="local")
        registry.set_policy_mapping("user_access", True)

        # Registered AND synced (see mock_extract_factory batch above).
        registry.register(id="user_access2", name="user_access2", source_type="keboola", query_mode="local")
        registry.set_policy_mapping("user_access2", True)

        # Registered AND synced, but the synced extract itself is empty.
        registry.register(id="user_access3", name="user_access3", source_type="keboola", query_mode="local")
        registry.set_policy_mapping("user_access3", True)

        users = UserRepository(conn)
        users.create(id="u_team_a", email="team-a@example.com", name="Team A")

        grant_table_via_package(conn, "tbl_orders", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "tbl_invoices", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "tbl_products", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "tbl_shipments", "u_team_a", group_name="TeamA")
    finally:
        conn.close()

    return {
        **seeded_app,
        "team_a_token": create_access_token("u_team_a", "team-a@example.com"),
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_empty_mapping_table_returns_structured_error_not_empty_rows(mapping_workspace):
    c = mapping_workspace["client"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM orders"},
        headers=_auth(mapping_workspace["team_a_token"]),
    )
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "policy_mapping_empty"
    assert detail["table"] == "tbl_orders"
    assert detail["mapping_table"] == "user_access"
    assert "user_access" in detail["note"]


def test_mixed_case_mapping_table_reference_is_still_detected(mapping_workspace):
    """finding 1 (PR #2023 review): the policy body joins `User_Access3` --
    DuckDB identifiers are case-insensitive, and so must the mapping-table
    match be. Before the fix this silently skipped the empty-mapping check
    and returned a filtered (but wrong) 200 -- the mapping table's view
    genuinely exists (it synced, just with zero rows), so the query executed
    fine and just quietly filtered everything out."""
    c = mapping_workspace["client"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM shipments"},
        headers=_auth(mapping_workspace["team_a_token"]),
    )
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "policy_mapping_empty"
    assert detail["table"] == "tbl_shipments"
    assert detail["mapping_table"] == "user_access3"


def test_non_empty_mapping_table_returns_normal_filtered_result(mapping_workspace):
    c = mapping_workspace["client"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM invoices"},
        headers=_auth(mapping_workspace["team_a_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 1, body
    id_idx = body["columns"].index("id")
    assert {row[id_idx] for row in body["rows"]} == {"1"}
    assert body["row_scope"] is not None
    assert "tbl_invoices" in body["row_scope"]["policied_tables"]


def test_table_without_mapping_referencing_policy_is_unaffected(mapping_workspace):
    c = mapping_workspace["client"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM products"},
        headers=_auth(mapping_workspace["team_a_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 1, body
    assert body["row_scope"] is None


def test_admin_bypass_is_unaffected_by_empty_mapping(mapping_workspace):
    """§12/§15.1: the admin bypass reads unfiltered -- it never joins the
    mapping table at all, so an empty mapping is irrelevant to what an
    unrestricted admin sees. Mirrors the diagnostic endpoint's own
    `if relation.policied:` gate on this same check."""
    c = mapping_workspace["client"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM orders"},
        headers=_auth(mapping_workspace["admin_token"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 2


def test_helper_matches_mapping_table_name_case_insensitively(e2e_env):
    """Unit test directly on `raise_if_policy_mapping_empty` (finding 1,
    PR #2023 review). DuckDB identifiers are case-insensitive, so a policy
    body joining `Cost_Centres` must still match a registry row named
    `cost_centres` -- before the fix the case-sensitive comparison missed
    it and the empty-mapping check silently no-opped."""
    from src.access_policy import PolicyMappingEmpty, raise_if_policy_mapping_empty
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(id="cost_centres", name="cost_centres", source_type="keboola", query_mode="local")
        registry.set_policy_mapping("cost_centres", True)
        # Deliberately no sync_state row -- never synced.
    finally:
        conn.close()

    policy_sql = "SELECT * FROM orders WHERE unit IN (SELECT unit FROM Cost_Centres WHERE email = $user_email)"
    with pytest.raises(PolicyMappingEmpty) as exc_info:
        raise_if_policy_mapping_empty(policy_sql)
    assert exc_info.value.mapping_table == "cost_centres"
