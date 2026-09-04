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
- ``tbl_returns`` (server_only): policied, joins ``user_access3`` (exact
  case) -- a mapping table that DID sync (a real ``sync_state`` row) but has
  zero rows. Exercises the "sync ran, mapping table is just empty" half of
  the trap, which is also the case with a non-``None`` ``last_sync``
  (finding 2).
- ``tbl_shipments`` (server_only): policied, joins ``User_Access3`` -- the
  SAME synced-but-empty mapping table as ``tbl_returns``, referenced in a
  different case. Because the table DID sync, its view genuinely exists in
  DuckDB (case-insensitively), so pre-fix this executed normally and
  silently returned 0 rows instead of tripping the empty-mapping check
  (finding 1, PR #2023 review).
"""

from __future__ import annotations

import pytest

ORDERS_POLICY_SQL = "SELECT * FROM orders WHERE unit IN (SELECT unit FROM user_access WHERE email = $user_email)"
INVOICES_POLICY_SQL = "SELECT * FROM invoices WHERE unit IN (SELECT unit FROM user_access2 WHERE email = $user_email)"
RETURNS_POLICY_SQL = "SELECT * FROM returns WHERE unit IN (SELECT unit FROM user_access3 WHERE email = $user_email)"
# Deliberately mixed-case reference to the SAME synced-but-empty
# `user_access3` mapping table `tbl_returns` uses above.
SHIPMENTS_POLICY_SQL = "SELECT * FROM shipments WHERE unit IN (SELECT unit FROM User_Access3 WHERE email = $user_email)"
# `ledger` is BOTH policied and itself referenceable from other policies
# (`policy_mapping=True`), and it is genuinely empty. Its policy body -- like
# every policy body -- references its OWN table, which pre-fix made that
# mandatory self-reference look like an empty mapping dependency and failed
# every read of the table (finding 2, second follow-up review of PR #2023).
LEDGER_POLICY_SQL = "SELECT * FROM ledger WHERE owner_email = $user_email"


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
            {
                "name": "returns",
                "data": [
                    {"id": "1", "unit": "TeamA", "qty": "1"},
                    {"id": "2", "unit": "TeamB", "qty": "2"},
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
            # the real `email`/`unit` columns the policy bodies reference, so
            # the mapping table's view genuinely resolves (case-insensitively
            # too) with zero rows, rather than erroring on a missing column.
            {"name": "user_access3", "data": []},
            # `ledger` synced fine -- it is simply an empty table (brand new,
            # or genuinely without rows yet). Columns rewritten below for the
            # same reason as `user_access3`.
            {"name": "ledger", "data": []},
            # user_access is registered as policy_mapping below but is
            # deliberately absent from this extract batch -- never synced,
            # no sync_state row at all.
        ],
    )
    _conn = duckdb.connect(str(db_path))
    _conn.execute('CREATE OR REPLACE TABLE "user_access3" (email VARCHAR, unit VARCHAR)')
    _conn.execute('CREATE OR REPLACE TABLE "ledger" (id VARCHAR, owner_email VARCHAR)')
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

        registry.register(id="tbl_returns", name="returns", source_type="keboola", query_mode="local", server_only=True)
        registry.set_access_policy("tbl_returns", sql=RETURNS_POLICY_SQL, note="mapping filter", updated_by="admin")

        # Policied AND referenceable from other policies AND empty -- the
        # exact three-way overlap finding 2 is about.
        registry.register(id="ledger", name="ledger", source_type="keboola", query_mode="local", server_only=True)
        registry.set_access_policy("ledger", sql=LEDGER_POLICY_SQL, note="own rows only", updated_by="admin")
        registry.set_policy_mapping("ledger", True)

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
        grant_table_via_package(conn, "tbl_returns", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "ledger", "u_team_a", group_name="TeamA")
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
    # finding 2 (PR #2023 review): the diagnostic includes `last_sync` --
    # `None` here because `user_access` never synced at all (no sync_state
    # row), matching the diagnosis this exception itself carries.
    assert "last_sync" in detail
    assert detail["last_sync"] is None


def test_synced_but_empty_mapping_table_returns_a_non_null_last_sync(mapping_workspace):
    """finding 2 (PR #2023 review): unlike `user_access` above, `user_access3`
    DID sync -- a real `sync_state` row exists, it is just empty -- so the
    error's `last_sync` must be a real timestamp, not `None`."""
    c = mapping_workspace["client"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM returns"},
        headers=_auth(mapping_workspace["team_a_token"]),
    )
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "policy_mapping_empty"
    assert detail["mapping_table"] == "user_access3"
    assert detail["last_sync"] is not None


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


def test_helper_skips_remote_mapping_tables(e2e_env):
    """A ``query_mode='remote'`` mapping table has no local materialization,
    so its ``sync_state.rows`` (0 / NULL -> 0, published by the orchestrator
    as metadata) is not a count -- the JOIN reads upstream rows live. The
    guard must not refuse on it (PR #2023 review follow-up)."""
    from src.access_policy import raise_if_policy_mapping_empty
    from src.db import get_system_db
    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="remote_map",
            name="remote_map",
            source_type="bigquery",
            query_mode="remote",
            bucket="ds",
            source_table="m",
        )
        registry.set_policy_mapping("remote_map", True)
        SyncStateRepository(conn).update_sync("remote_map", rows=0, file_size_bytes=0, hash="")
    finally:
        conn.close()

    raise_if_policy_mapping_empty(
        "SELECT * FROM orders WHERE unit IN (SELECT unit FROM remote_map WHERE email = $user_email)"
    )


def test_helper_treats_count_unavailable_as_unknown_not_empty(e2e_env):
    """#1364: the orchestrator publishes ``rows=0`` plus a dedicated sync
    error when the extractor could not count a table this pass; the data
    previously synced is still served, so the guard must not refuse on that
    placeholder zero -- while a verified zero (no such error) still raises."""
    from src.access_policy import PolicyMappingEmpty, raise_if_policy_mapping_empty
    from src.db import get_system_db
    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        for tid in ("uncounted_map", "counted_empty_map"):
            registry.register(id=tid, name=tid, source_type="keboola", query_mode="local")
            registry.set_policy_mapping(tid, True)
        state_repo = SyncStateRepository(conn)
        state_repo.update_sync("uncounted_map", rows=0, file_size_bytes=10, hash="abc")
        state_repo.set_error(
            "uncounted_map",
            "Row count unavailable for table 'uncounted_map' in source 'x' -- the published rows=0 is NOT a "
            "verified empty table. See #1364.",
        )
        state_repo.update_sync("counted_empty_map", rows=0, file_size_bytes=10, hash="def")
    finally:
        conn.close()

    raise_if_policy_mapping_empty(
        "SELECT * FROM orders WHERE unit IN (SELECT unit FROM uncounted_map WHERE email = $user_email)"
    )
    with pytest.raises(PolicyMappingEmpty, match="counted_empty_map"):
        raise_if_policy_mapping_empty(
            "SELECT * FROM orders WHERE unit IN (SELECT unit FROM counted_empty_map WHERE email = $user_email)"
        )


def test_orchestrator_error_messages_keep_the_count_marker_when_combined():
    """#1364 x PR #2023 review: `set_error` replaces the row's error and the
    corrupt-parts message is written AFTER the count-unavailable one, so
    when both fire on one pass the corrupt-parts text must itself carry
    the marker the guard keys on."""
    from src.orchestrator import _corrupt_parts_message, _count_unavailable_message
    from src.sync_state_key import COUNT_UNAVAILABLE_MARKER

    assert _count_unavailable_message("t", "s").startswith(COUNT_UNAVAILABLE_MARKER)
    alone = _corrupt_parts_message("t", "s", {"p1.parquet"}, count_unavailable=False)
    assert COUNT_UNAVAILABLE_MARKER not in alone and "p1.parquet" in alone
    combined = _corrupt_parts_message("t", "s", {"p1.parquet"}, count_unavailable=True)
    assert COUNT_UNAVAILABLE_MARKER in combined and "p1.parquet" in combined


def test_count_marker_only_when_something_is_served():
    """An all-rejected FIRST sync (no frozen prior manifest) publishes
    nothing, so the corrupt-parts error must not carry the marker -- the
    guard has to keep reading that table as never-synced."""
    from src.orchestrator import _corrupt_parts_message, _count_marker_applies
    from src.sync_state_key import COUNT_UNAVAILABLE_MARKER

    assert _count_marker_applies(True, [{"path": "part-0.parquet", "size_bytes": 1}]) is True
    assert _count_marker_applies(True, None) is False
    assert _count_marker_applies(True, []) is False
    assert _count_marker_applies(False, [{"path": "p", "size_bytes": 1}]) is False
    unusable = _corrupt_parts_message("t", "s", {"part-0.parquet"}, count_unavailable=_count_marker_applies(True, None))
    assert COUNT_UNAVAILABLE_MARKER not in unusable


def test_helper_refuses_an_all_rejected_first_sync(e2e_env):
    """The mirror of the frozen case: nothing served -> the corrupt-parts
    error has no marker -> rows=0 is a real "no data" and the guard raises."""
    from src.access_policy import PolicyMappingEmpty, raise_if_policy_mapping_empty
    from src.db import get_system_db
    from src.orchestrator import _corrupt_parts_message
    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(id="unusable_map", name="unusable_map", source_type="keboola", query_mode="local")
        registry.set_policy_mapping("unusable_map", True)
        state_repo = SyncStateRepository(conn)
        state_repo.update_sync("unusable_map", rows=0, file_size_bytes=0, hash="")
        state_repo.set_error(
            "unusable_map", _corrupt_parts_message("unusable_map", "x", {"part-0.parquet"}, count_unavailable=False)
        )
    finally:
        conn.close()

    with pytest.raises(PolicyMappingEmpty, match="unusable_map"):
        raise_if_policy_mapping_empty(
            "SELECT * FROM orders WHERE unit IN (SELECT unit FROM unusable_map WHERE email = $user_email)"
        )


def test_helper_treats_combined_corrupt_parts_and_uncounted_as_unknown(e2e_env):
    """The combined message (corrupt parts + count unavailable) still reads
    as "unknown", so a frozen-but-served mapping table is not refused."""
    from src.access_policy import raise_if_policy_mapping_empty
    from src.db import get_system_db
    from src.orchestrator import _corrupt_parts_message
    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(id="frozen_map", name="frozen_map", source_type="keboola", query_mode="local")
        registry.set_policy_mapping("frozen_map", True)
        state_repo = SyncStateRepository(conn)
        state_repo.update_sync("frozen_map", rows=0, file_size_bytes=10, hash="abc")
        state_repo.set_error(
            "frozen_map", _corrupt_parts_message("frozen_map", "x", {"part-0.parquet"}, count_unavailable=True)
        )
    finally:
        conn.close()

    raise_if_policy_mapping_empty(
        "SELECT * FROM orders WHERE unit IN (SELECT unit FROM frozen_map WHERE email = $user_email)"
    )


def test_helper_cte_exclusion_is_scope_aware(e2e_env):
    """The CTE exclusion must not hide a PHYSICAL read of a same-named
    mapping table (PR #2023 review, second round on this guard): a
    qualified reference, a reference inside the CTE's own non-recursive
    body, and a reference in an EARLIER CTE all resolve to the table, not
    the alias -- so an empty mapping table is still refused there -- while
    a reference after the declaration (final query or a later CTE) resolves
    to the alias and is not."""
    from src.access_policy import PolicyMappingEmpty, raise_if_policy_mapping_empty
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(id="cost_centres", name="cost_centres", source_type="keboola", query_mode="local")
        registry.set_policy_mapping("cost_centres", True)
    finally:
        conn.close()

    physical_shapes = [
        # qualified inside the same-named CTE body
        "WITH cost_centres AS (SELECT * FROM main.cost_centres) "
        "SELECT * FROM orders WHERE unit IN (SELECT unit FROM cost_centres)",
        # unqualified inside its own (non-recursive) body
        "WITH cost_centres AS (SELECT * FROM cost_centres WHERE email = $user_email) "
        "SELECT * FROM orders WHERE unit IN (SELECT unit FROM cost_centres)",
        # read in an EARLIER CTE, before the alias is declared
        "WITH a AS (SELECT unit FROM cost_centres WHERE email = $user_email), cost_centres AS (SELECT 1 AS unit) "
        "SELECT * FROM orders WHERE unit IN (SELECT unit FROM a)",
    ]
    for sql in physical_shapes:
        with pytest.raises(PolicyMappingEmpty, match="cost_centres"):
            raise_if_policy_mapping_empty(sql)

    alias_shapes = [
        # final query reads the alias
        "WITH cost_centres AS (SELECT $user_email AS email, 'A' AS unit) "
        "SELECT * FROM orders WHERE unit IN (SELECT unit FROM cost_centres)",
        # a LATER CTE reads the alias
        "WITH cost_centres AS (SELECT $user_email AS email, 'A' AS unit), b AS (SELECT unit FROM cost_centres) "
        "SELECT * FROM orders WHERE unit IN (SELECT unit FROM b)",
    ]
    for sql in alias_shapes:
        raise_if_policy_mapping_empty(sql)


def test_helper_ignores_cte_aliases_that_shadow_a_mapping_table_name(e2e_env):
    """A CTE alias is not a physical dependency (PR #2023 review follow-up):
    a body whose CTE happens to be named like an empty ``policy_mapping``
    row reads the CTE, never that row, so the guard must not raise on it --
    while a body that ALSO reads the real empty mapping table still must."""
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

    # Only the CTE is read: no dependency on the registry row of that name.
    raise_if_policy_mapping_empty(
        "WITH cost_centres AS (SELECT $user_email AS email, 'A' AS unit) "
        "SELECT * FROM orders WHERE unit IN (SELECT unit FROM cost_centres)"
    )

    # The real table is read under another CTE's name: still refused.
    with pytest.raises(PolicyMappingEmpty) as exc_info:
        raise_if_policy_mapping_empty(
            "WITH me AS (SELECT $user_email AS email) "
            "SELECT * FROM orders WHERE unit IN (SELECT unit FROM cost_centres WHERE email IN (SELECT email FROM me))"
        )
    assert exc_info.value.mapping_table == "cost_centres"


def _find_table_entry(payload: dict, table_id: str):
    return next((t for t in payload.get("tables") or [] if t["table_id"] == table_id), None)


class TestPoliciedTableIsNotItsOwnEmptyMapping:
    """finding 2 (second follow-up review of PR #2023): a policy body ALWAYS
    references its own table (``SELECT ... FROM <table> WHERE ...``). When
    that table is also marked ``policy_mapping=True`` (referenceable from
    OTHER policies) and currently has zero rows, the mandatory
    self-reference read as an empty mapping dependency and every read of the
    table failed with ``policy_mapping_empty`` -- an empty table is a
    legitimate answer, not a broken upstream sync. The protected table
    excludes ITSELF from the check; a reference to any OTHER empty
    referenceable table still trips it, on all three surfaces.
    """

    def test_query_returns_an_empty_result_not_a_mapping_error(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM ledger"},
            headers=_auth(mapping_workspace["team_a_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 0, body
        # Still disclosed as filtered -- an empty slice is a slice.
        assert body["row_scope"] is not None
        assert "ledger" in body["row_scope"]["policied_tables"]

    def test_effective_access_reports_empty_slice_not_mapping_empty(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.get("/api/me/effective-access", headers=_auth(mapping_workspace["team_a_token"]))
        assert r.status_code == 200, r.text
        entry = _find_table_entry(r.json(), "ledger")
        assert entry is not None, r.json()
        assert entry["policy"]["applies"] is True
        assert entry["policy"]["reason"] != "mapping_empty", entry
        assert entry["policy"]["reason"] == "empty_slice", entry
        assert entry["policy"]["rows_visible"] == 0

    def test_admin_preview_shows_no_mapping_warning(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.post(
            "/api/admin/registry/ledger/policy/preview",
            json={"as_user": "team-a@example.com"},
            headers=_auth(mapping_workspace["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["mapping_warning"] is None, r.text

    def test_admin_preview_groups_shows_no_mapping_warning(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.post(
            "/api/admin/registry/ledger/policy/preview-groups",
            json={},
            headers=_auth(mapping_workspace["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["mapping_warning"] is None, r.text

    def test_another_empty_mapping_table_still_trips_effective_access(self, mapping_workspace):
        """The control: ``tbl_orders`` joins ``user_access`` -- a DIFFERENT
        referenceable table that never synced -- and must still be reported
        as ``mapping_empty``."""
        c = mapping_workspace["client"]
        r = c.get("/api/me/effective-access", headers=_auth(mapping_workspace["team_a_token"]))
        assert r.status_code == 200, r.text
        entry = _find_table_entry(r.json(), "tbl_orders")
        assert entry is not None, r.json()
        assert entry["policy"]["reason"] == "mapping_empty", entry

    def test_another_empty_mapping_table_still_warns_in_the_admin_preview(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.post(
            "/api/admin/registry/tbl_orders/policy/preview",
            json={"as_user": "team-a@example.com"},
            headers=_auth(mapping_workspace["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["mapping_warning"], r.text
        assert "user_access" in r.json()["mapping_warning"]


class TestHelperSelfReferenceExclusion:
    """Unit tests on ``raise_if_policy_mapping_empty`` itself -- the ONE
    implementation all three surfaces call, so the exclusion contract is
    pinned here rather than only through the routes."""

    @pytest.fixture
    def two_empty_mapping_tables(self, e2e_env):
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            registry = TableRegistryRepository(conn)
            for tid in ("ledger", "cost_centres"):
                registry.register(id=tid, name=tid, source_type="keboola", query_mode="local")
                registry.set_policy_mapping(tid, True)
                # Deliberately no sync_state row -- never synced.
        finally:
            conn.close()

    def test_self_reference_is_excluded(self, two_empty_mapping_tables):
        from src.access_policy import raise_if_policy_mapping_empty

        raise_if_policy_mapping_empty("SELECT * FROM ledger WHERE owner = $user_email", table_name="ledger")

    def test_self_reference_exclusion_is_case_insensitive(self, two_empty_mapping_tables):
        from src.access_policy import raise_if_policy_mapping_empty

        raise_if_policy_mapping_empty("SELECT * FROM Ledger WHERE owner = $user_email", table_name="LEDGER")

    def test_self_reference_can_be_named_by_table_id(self, two_empty_mapping_tables):
        from src.access_policy import raise_if_policy_mapping_empty

        raise_if_policy_mapping_empty("SELECT * FROM ledger WHERE owner = $user_email", table_id="ledger")

    def test_another_empty_mapping_table_still_raises(self, two_empty_mapping_tables):
        from src.access_policy import PolicyMappingEmpty, raise_if_policy_mapping_empty

        with pytest.raises(PolicyMappingEmpty) as exc_info:
            raise_if_policy_mapping_empty(
                "SELECT * FROM ledger WHERE unit IN (SELECT unit FROM cost_centres WHERE email = $user_email)",
                table_name="ledger",
            )
        assert exc_info.value.mapping_table == "cost_centres"

    def test_without_the_self_name_the_check_is_unchanged(self, two_empty_mapping_tables):
        """No ``table_name``/``table_id`` given (the pre-existing signature)
        keeps the old behavior -- every referenced mapping table counts."""
        from src.access_policy import PolicyMappingEmpty, raise_if_policy_mapping_empty

        with pytest.raises(PolicyMappingEmpty):
            raise_if_policy_mapping_empty("SELECT * FROM ledger WHERE owner = $user_email")


class TestHelperNameKeyedSyncState:
    """Review follow-up (#1979, PR #2023): ``sync_state.table_id`` may still
    be keyed by the mapping table's ``name`` rather than its registry ``id``
    -- the pre-B1 convention (``src.sync_state_key``) every writer used
    before a matching ``table_registry`` row existed at sync time. Looking
    the mapping row up by ``id`` alone made a populated, name-keyed mapping
    table read as "never synced" and refused every query a policy joins it
    from. The helper must try both keys, name first (the same order
    ``app/api/v2_sample.py::_not_synced_detail`` uses for the identical
    reason), and take whichever row actually exists.
    """

    def _seed_mapping_row(self, conn, *, table_id: str, name: str):
        from src.repositories.table_registry import TableRegistryRepository

        registry = TableRegistryRepository(conn)
        registry.register(id=table_id, name=name, source_type="keboola", query_mode="local")
        registry.set_policy_mapping(table_id, True)

    def _write_sync_state(self, conn, *, key: str, rows: int):
        from src.repositories.sync_state import SyncStateRepository

        state_repo = SyncStateRepository(conn)
        state_repo.update_sync(key, rows=rows, file_size_bytes=0, hash="deadbeef")
        return state_repo.get_table_state(key)

    def test_name_keyed_row_with_rows_does_not_raise(self, e2e_env):
        """(a) registry ``id`` differs from ``name``; ``sync_state`` is keyed
        by the NAME with rows > 0 -- a populated legacy row must read as
        synced, not empty."""
        from src.access_policy import raise_if_policy_mapping_empty
        from src.db import get_system_db

        conn = get_system_db()
        try:
            self._seed_mapping_row(conn, table_id="mapping_a_id", name="mapping_a_name")
            self._write_sync_state(conn, key="mapping_a_name", rows=5)
        finally:
            conn.close()

        # No exception -- a populated name-keyed mapping table is not empty.
        raise_if_policy_mapping_empty(
            "SELECT * FROM orders WHERE unit IN (SELECT unit FROM mapping_a_name WHERE email = $user_email)"
        )

    def test_name_keyed_row_with_zero_rows_raises_with_its_last_sync(self, e2e_env):
        """(b) same shape as (a), but the name-keyed row has zero rows --
        must still raise, and the reported ``last_sync`` must be the one
        recorded on that name-keyed row, not ``None``."""
        from src.access_policy import PolicyMappingEmpty, raise_if_policy_mapping_empty
        from src.db import get_system_db

        conn = get_system_db()
        try:
            self._seed_mapping_row(conn, table_id="mapping_b_id", name="mapping_b_name")
            state = self._write_sync_state(conn, key="mapping_b_name", rows=0)
        finally:
            conn.close()

        assert state is not None and state["last_sync"] is not None

        with pytest.raises(PolicyMappingEmpty) as exc_info:
            raise_if_policy_mapping_empty(
                "SELECT * FROM orders WHERE unit IN (SELECT unit FROM mapping_b_name WHERE email = $user_email)"
            )
        assert exc_info.value.mapping_table == "mapping_b_name"
        assert exc_info.value.last_sync == state["last_sync"]

    def test_id_keyed_row_still_works(self, e2e_env):
        """(c) regression: the current (B1) convention -- ``sync_state`` keyed
        by the registry ``id`` -- must keep working exactly as before."""
        from src.access_policy import raise_if_policy_mapping_empty
        from src.db import get_system_db

        conn = get_system_db()
        try:
            self._seed_mapping_row(conn, table_id="mapping_c_id", name="mapping_c_name")
            self._write_sync_state(conn, key="mapping_c_id", rows=3)
        finally:
            conn.close()

        raise_if_policy_mapping_empty(
            "SELECT * FROM orders WHERE unit IN (SELECT unit FROM mapping_c_name WHERE email = $user_email)"
        )

    def test_duplicate_rows_prefer_the_populated_id_keyed_row(self, e2e_env):
        """(d) BOTH rows exist -- migration 0072 deliberately leaves the
        legacy name-keyed row in place when an id-keyed row already occupies
        the target primary key. The ID row is the one every writer
        (``src.sync_state_key``) keeps current, so a stale name-keyed row
        showing zero rows must NOT veto a healthy mapping table."""
        from src.access_policy import raise_if_policy_mapping_empty
        from src.db import get_system_db

        conn = get_system_db()
        try:
            self._seed_mapping_row(conn, table_id="mapping_d_id", name="mapping_d_name")
            self._write_sync_state(conn, key="mapping_d_name", rows=0)
            self._write_sync_state(conn, key="mapping_d_id", rows=7)
        finally:
            conn.close()

        raise_if_policy_mapping_empty(
            "SELECT * FROM orders WHERE unit IN (SELECT unit FROM mapping_d_name WHERE email = $user_email)"
        )

    def test_duplicate_rows_report_the_id_keyed_rows_last_sync(self, e2e_env):
        """(e) the mirror of (d): a stale name-keyed row still claiming rows
        must not HIDE a mapping table the canonical id-keyed row records as
        empty -- and the reported ``last_sync`` must come from that ID row."""
        from src.access_policy import PolicyMappingEmpty, raise_if_policy_mapping_empty
        from src.db import get_system_db

        conn = get_system_db()
        try:
            self._seed_mapping_row(conn, table_id="mapping_e_id", name="mapping_e_name")
            self._write_sync_state(conn, key="mapping_e_name", rows=9)
            id_state = self._write_sync_state(conn, key="mapping_e_id", rows=0)
        finally:
            conn.close()

        assert id_state is not None

        with pytest.raises(PolicyMappingEmpty) as exc_info:
            raise_if_policy_mapping_empty(
                "SELECT * FROM orders WHERE unit IN (SELECT unit FROM mapping_e_name WHERE email = $user_email)"
            )
        assert exc_info.value.mapping_table == "mapping_e_name"
        assert exc_info.value.last_sync == id_state["last_sync"]


class TestQueryEndpointWithNameKeyedMappingSyncState:
    """(a), end to end: a policy joining a populated but name-keyed mapping
    table must succeed through ``POST /api/query`` too, not just the bare
    helper -- the same guarantee ``mapping_workspace`` pins for the id-keyed
    case above."""

    @pytest.fixture
    def name_keyed_mapping_workspace(self, seeded_app, mock_extract_factory, monkeypatch):
        from app.auth.jwt import create_access_token
        from src.db import get_system_db
        from src.orchestrator import SyncOrchestrator
        from src.repositories.table_registry import TableRegistryRepository
        from src.repositories.users import UserRepository
        from tests.conftest import grant_table_via_package

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

        env = seeded_app["env"]
        mock_extract_factory(
            "keboola",
            [
                {
                    "name": "alerts",
                    "data": [
                        {"id": "1", "unit": "TeamA", "sev": "high"},
                        {"id": "2", "unit": "TeamB", "sev": "low"},
                    ],
                },
                {
                    "name": "access_by_name",
                    "data": [{"email": "name-keyed@example.com", "unit": "TeamA"}],
                },
            ],
        )

        # Sync BEFORE the mapping table is registered: `resolve_sync_state_key`
        # (B1) finds no matching `table_registry` row for `access_by_name` yet,
        # so this write lands keyed by NAME -- the pre-B1 legacy shape this
        # fix reads. Registering it only afterwards, under a DIFFERENT id,
        # reproduces "populated mapping table, but its registry id and its
        # sync_state key disagree".
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        conn = get_system_db()
        try:
            registry = TableRegistryRepository(conn)

            registry.register(
                id="tbl_alerts_nk", name="alerts", source_type="keboola", query_mode="local", server_only=True
            )
            registry.set_access_policy(
                "tbl_alerts_nk",
                sql=("SELECT * FROM alerts WHERE unit IN (SELECT unit FROM access_by_name WHERE email = $user_email)"),
                note="mapping filter",
                updated_by="admin",
            )

            registry.register(id="mapping_nk_id", name="access_by_name", source_type="keboola", query_mode="local")
            registry.set_policy_mapping("mapping_nk_id", True)

            users = UserRepository(conn)
            users.create(id="u_name_keyed", email="name-keyed@example.com", name="Name Keyed")

            grant_table_via_package(conn, "tbl_alerts_nk", "u_name_keyed", group_name="TeamNameKeyed")
        finally:
            conn.close()

        return {
            **seeded_app,
            "name_keyed_token": create_access_token("u_name_keyed", "name-keyed@example.com"),
        }

    def test_query_through_name_keyed_mapping_table_succeeds(self, name_keyed_mapping_workspace):
        c = name_keyed_mapping_workspace["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM alerts"},
            headers=_auth(name_keyed_mapping_workspace["name_keyed_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 1, body
        id_idx = body["columns"].index("id")
        assert {row[id_idx] for row in body["rows"]} == {"1"}


class TestSourceTableExclusionIsNarrow:
    """F4 (security review, #1979): the protected row's ``source_table`` is
    subtracted from the referenced names so a body naming the physical
    ``bucket.source_table`` form still counts as a self-reference. But the
    subtraction was unconditional -- a protected table whose ``source_table``
    happens to equal a REAL ``policy_mapping=true`` row's name suppressed the
    empty-mapping refusal for that genuine dependency, and the read fell back
    to the silent 0-row answer this whole check exists to prevent.

    So ``source_table`` is excluded only when NO registry row with
    ``policy_mapping=true`` carries that name (lower-cased). ``name`` and
    ``id`` are always excluded -- a body's mandatory ``FROM <itself>`` is
    never a mapping dependency.
    """

    @pytest.fixture
    def collision(self, e2e_env):
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            registry = TableRegistryRepository(conn)
            # The empty mapping table a policy legitimately joins.
            registry.register(id="cost_centres", name="cost_centres", source_type="keboola", query_mode="local")
            registry.set_policy_mapping("cost_centres", True)
            # The protected table -- its PHYSICAL source_table collides with
            # the mapping table's registered name.
            registry.register(
                id="tbl_ledger",
                name="ledger",
                source_type="keboola",
                query_mode="local",
                bucket="in.c-fin",
                source_table="cost_centres",
                server_only=True,
            )
        finally:
            conn.close()

    def test_a_real_mapping_dependency_is_not_masked_by_source_table(self, collision):
        from src.access_policy import PolicyMappingEmpty, raise_if_policy_mapping_empty

        with pytest.raises(PolicyMappingEmpty) as exc_info:
            raise_if_policy_mapping_empty(
                "SELECT * FROM ledger WHERE unit IN (SELECT unit FROM cost_centres WHERE email = $user_email)",
                table_name="ledger",
                table_id="tbl_ledger",
            )
        assert exc_info.value.mapping_table == "cost_centres"

    def test_the_self_reference_by_name_is_still_excluded(self, collision):
        from src.access_policy import raise_if_policy_mapping_empty

        raise_if_policy_mapping_empty(
            "SELECT * FROM ledger WHERE owner = $user_email",
            table_name="ledger",
            table_id="tbl_ledger",
        )

    def test_source_table_is_still_excluded_when_no_mapping_row_claims_it(self, e2e_env):
        """The case the exclusion was added for is untouched: a body naming
        the physical ``bucket.source_table`` form of its OWN table, where
        nothing marks that name as a mapping table."""
        from src.access_policy import raise_if_policy_mapping_empty
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            registry = TableRegistryRepository(conn)
            registry.register(
                id="tbl_ledger2",
                name="ledger2",
                source_type="keboola",
                query_mode="local",
                bucket="in.c-fin",
                source_table="raw_ledger",
                server_only=True,
            )
            registry.register(id="raw_ledger", name="raw_ledger", source_type="keboola", query_mode="local")
        finally:
            conn.close()

        raise_if_policy_mapping_empty(
            'SELECT * FROM "in.c-fin".raw_ledger WHERE owner = $user_email',
            table_name="ledger2",
            table_id="tbl_ledger2",
        )


# ---------------------------------------------------------------------------
# #2147 -- the SAME structured `policy_mapping_empty` refusal, on every OTHER
# read surface that actually executes a policied relation (not just
# `POST /api/query`): `GET /api/v2/sample`, `POST /api/v2/scan` (local-parquet
# branch and its `--from-query` sibling), and `POST /api/mcp/query-table/{id}`.
# One shared implementation (`app.api.access_policy_http.
# assert_no_empty_policy_mapping`), so these can never drift from `/api/query`
# on the reason code or the `last_sync` shape -- reuses `mapping_workspace`.
# ---------------------------------------------------------------------------


class TestSampleSurfaceGetsTheSameRefusal:
    def test_empty_mapping_table_returns_structured_error(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.get("/api/v2/sample/tbl_orders?n=5", headers=_auth(mapping_workspace["team_a_token"]))
        assert r.status_code == 500, r.text
        detail = r.json()["detail"]
        assert detail["reason"] == "policy_mapping_empty"
        assert detail["table"] == "tbl_orders"
        assert detail["mapping_table"] == "user_access"
        assert detail["last_sync"] is None

    def test_populated_mapping_table_is_never_misreported_as_empty(self, mapping_workspace):
        """`/api/v2/sample`'s local-parquet branch resolves its base table
        as a throwaway `read_parquet(...)` in a fresh `:memory:` connection
        with nothing ELSE attached, so it cannot execute a genuinely
        cross-table `policy_mapping` JOIN at all -- a pre-existing,
        documented limitation independent of this check (see
        `tests/test_access_policy_table_id_surfaces.py`'s own note on the
        same limitation). This pins the piece THIS check owns: a real,
        POPULATED `policy_mapping` dependency (`tbl_invoices` -> `user_access2`,
        the exact fixture `/api/query`'s own happy-path test uses) is never
        misreported as empty by the shared helper every surface calls."""
        from app.api.access_policy_http import assert_no_empty_policy_mapping
        from src.repositories import table_registry_repo

        row = table_registry_repo().get("tbl_invoices")
        assert_no_empty_policy_mapping(table_id="tbl_invoices", row=row)  # must not raise

    def test_admin_bypass_is_unaffected(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.get("/api/v2/sample/tbl_orders?n=5", headers=_auth(mapping_workspace["admin_token"]))
        assert r.status_code == 200, r.text
        assert len(r.json()["rows"]) == 2


class TestScanSurfaceGetsTheSameRefusal:
    def test_synced_but_empty_mapping_table_returns_structured_error(self, mapping_workspace):
        """`tbl_orders` (never-synced `user_access`) trips
        `run_scan`'s own PRE-EXISTING schema resolution
        (`_resolve_schema` -> `effective_schema`, which DESCRIBEs the
        wrapped relation against the FULL analytics connection) before this
        check even runs, since `user_access` never synced at all and so has
        no view there either -- that surfaces as the pre-existing
        `policy_error`, not this one. `tbl_returns` (`user_access3`, SYNCED
        but zero rows) has a real view there, so schema resolution succeeds
        and this check is what actually trips, inside the local-execution
        branch -- the shape this test pins."""
        c = mapping_workspace["client"]
        r = c.post(
            "/api/v2/scan",
            json={"table_id": "tbl_returns"},
            headers=_auth(mapping_workspace["team_a_token"]),
        )
        assert r.status_code == 500, r.text
        detail = r.json()["detail"]
        assert detail["reason"] == "policy_mapping_empty"
        assert detail["table"] == "tbl_returns"
        assert detail["mapping_table"] == "user_access3"
        assert detail["last_sync"] is not None

    def test_populated_mapping_table_is_never_misreported_as_empty(self, mapping_workspace):
        """Same pre-existing cross-table-JOIN limitation as `/api/v2/sample`
        (see that class's own note) -- pins that THIS check does not
        misreport a real, populated dependency as empty."""
        from app.api.access_policy_http import assert_no_empty_policy_mapping
        from src.repositories import table_registry_repo

        row = table_registry_repo().get("tbl_invoices")
        assert_no_empty_policy_mapping(table_id="tbl_invoices", row=row)  # must not raise

    def test_from_query_snapshot_gets_the_same_refusal(self, mapping_workspace):
        """The `--from-query` snapshot-materialize path shares `/api/query`'s
        own `rewrite_sql` AND its full analytics connection (unlike the
        table_id-shaped branch above), so `tbl_orders`'s never-synced
        `user_access` reaches this check directly, exactly like
        `POST /api/query` itself (it did not, before #2147 -- see
        `app.api.query.run_remote_select_to_arrow`)."""
        c = mapping_workspace["client"]
        r = c.post(
            "/api/v2/scan",
            json={"from_query": "SELECT * FROM orders"},
            headers=_auth(mapping_workspace["team_a_token"]),
        )
        assert r.status_code == 500, r.text
        detail = r.json()["detail"]
        assert detail["reason"] == "policy_mapping_empty"
        assert detail["mapping_table"] == "user_access"


class TestMcpPerTableSurfaceGetsTheSameRefusal:
    """`POST /api/mcp/query-table/{id}` resolves its analytics view by the
    registry ``id`` directly (``view_name = table_id``, no name lookup) --
    unlike `/api/query`, which resolves by name. `mapping_workspace`'s rows
    deliberately have `id != name` (`tbl_invoices` / `invoices`), so this
    needs its own small workspace where the two coincide -- but it DOES run
    on the full analytics connection (like `/api/query`, unlike sample/scan's
    throwaway single-parquet one), so a genuine cross-table `policy_mapping`
    JOIN executes here and both the refusal AND the happy path are testable
    end to end.
    """

    @pytest.fixture
    def mcp_mapping_workspace(self, seeded_app, mock_extract_factory, monkeypatch):
        from app.auth.jwt import create_access_token
        from src.db import get_system_db
        from src.orchestrator import SyncOrchestrator
        from src.repositories.table_registry import TableRegistryRepository
        from src.repositories.users import UserRepository
        from tests.conftest import grant_table_via_package

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

        env = seeded_app["env"]
        mock_extract_factory(
            "keboola",
            [
                {
                    "name": "alerts_never",
                    "data": [{"id": "1", "unit": "TeamA", "sev": "high"}, {"id": "2", "unit": "TeamB", "sev": "low"}],
                },
                {
                    "name": "alerts_ok",
                    "data": [{"id": "1", "unit": "TeamA", "sev": "high"}, {"id": "2", "unit": "TeamB", "sev": "low"}],
                },
                {"name": "access_mcp_ok", "data": [{"email": "mcp-a@example.com", "unit": "TeamA"}]},
                # `access_mcp_never` is deliberately absent -- never synced.
            ],
        )
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        conn = get_system_db()
        try:
            registry = TableRegistryRepository(conn)

            registry.register(
                id="alerts_never", name="alerts_never", source_type="keboola", query_mode="local", server_only=True
            )
            registry.set_access_policy(
                "alerts_never",
                sql="SELECT * FROM alerts_never WHERE unit IN (SELECT unit FROM access_mcp_never WHERE email = $user_email)",
                note="mapping filter",
                updated_by="admin",
            )
            registry.register(
                id="access_mcp_never", name="access_mcp_never", source_type="keboola", query_mode="local"
            )
            registry.set_policy_mapping("access_mcp_never", True)

            registry.register(
                id="alerts_ok", name="alerts_ok", source_type="keboola", query_mode="local", server_only=True
            )
            registry.set_access_policy(
                "alerts_ok",
                sql="SELECT * FROM alerts_ok WHERE unit IN (SELECT unit FROM access_mcp_ok WHERE email = $user_email)",
                note="mapping filter",
                updated_by="admin",
            )
            registry.register(id="access_mcp_ok", name="access_mcp_ok", source_type="keboola", query_mode="local")
            registry.set_policy_mapping("access_mcp_ok", True)

            users = UserRepository(conn)
            users.create(id="u_mcp", email="mcp-a@example.com", name="MCP Team A")
            grant_table_via_package(conn, "alerts_never", "u_mcp", group_name="TeamMcp")
            grant_table_via_package(conn, "alerts_ok", "u_mcp", group_name="TeamMcp")
        finally:
            conn.close()

        return {**seeded_app, "mcp_token": create_access_token("u_mcp", "mcp-a@example.com")}

    def test_empty_mapping_table_returns_structured_error(self, mcp_mapping_workspace):
        c = mcp_mapping_workspace["client"]
        r = c.post(
            "/api/mcp/query-table/alerts_never",
            json={"filter": {}, "limit": 50},
            headers=_auth(mcp_mapping_workspace["mcp_token"]),
        )
        assert r.status_code == 500, r.text
        detail = r.json()["detail"]
        assert detail["reason"] == "policy_mapping_empty"
        assert detail["table"] == "alerts_never"
        assert detail["mapping_table"] == "access_mcp_never"

    def test_non_empty_mapping_table_returns_a_normal_result(self, mcp_mapping_workspace):
        c = mcp_mapping_workspace["client"]
        r = c.post(
            "/api/mcp/query-table/alerts_ok",
            json={"filter": {}, "limit": 50},
            headers=_auth(mcp_mapping_workspace["mcp_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 1
        assert body["row_scope"] is not None


# ---------------------------------------------------------------------------
# #2147 -- `GET /api/admin/registry`'s read-only `policy_mapping_status`
# field: derived from the SAME per-mapping-table state
# (`src.access_policy.policy_mapping_statuses`) the fail-closed checks above
# use, so an admin scanning the registry list sees the identical diagnosis a
# live read would trip.
# ---------------------------------------------------------------------------


class TestAdminRegistryMappingStatusField:
    def _entry(self, tables, table_id):
        return next(t for t in tables if t["id"] == table_id)

    def test_never_synced_mapping_table(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.get("/api/admin/registry", headers=_auth(mapping_workspace["admin_token"]))
        assert r.status_code == 200, r.text
        entry = self._entry(r.json()["tables"], "tbl_orders")
        assert entry["policy_mapping_status"] == [
            {"mapping_table": "user_access", "state": "never_synced", "last_sync": None}
        ]

    def test_synced_but_empty_mapping_table(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.get("/api/admin/registry", headers=_auth(mapping_workspace["admin_token"]))
        assert r.status_code == 200, r.text
        entry = self._entry(r.json()["tables"], "tbl_returns")
        statuses = entry["policy_mapping_status"]
        assert len(statuses) == 1
        assert statuses[0]["mapping_table"] == "user_access3"
        assert statuses[0]["state"] == "empty"
        assert statuses[0]["last_sync"] is not None

    def test_mixed_case_reference_resolves_to_the_same_mapping_table(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.get("/api/admin/registry", headers=_auth(mapping_workspace["admin_token"]))
        assert r.status_code == 200, r.text
        entry = self._entry(r.json()["tables"], "tbl_shipments")
        statuses = entry["policy_mapping_status"]
        assert len(statuses) == 1
        assert statuses[0]["mapping_table"] == "user_access3"
        assert statuses[0]["state"] == "empty"

    def test_healthy_mapping_table(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.get("/api/admin/registry", headers=_auth(mapping_workspace["admin_token"]))
        assert r.status_code == 200, r.text
        entry = self._entry(r.json()["tables"], "tbl_invoices")
        assert entry["policy_mapping_status"] == [
            {"mapping_table": "user_access2", "state": "ok", "last_sync": entry["policy_mapping_status"][0]["last_sync"]}
        ]
        assert entry["policy_mapping_status"][0]["last_sync"] is not None

    def test_unpolicied_table_carries_no_field(self, mapping_workspace):
        c = mapping_workspace["client"]
        r = c.get("/api/admin/registry", headers=_auth(mapping_workspace["admin_token"]))
        assert r.status_code == 200, r.text
        entry = self._entry(r.json()["tables"], "tbl_products")
        assert "policy_mapping_status" not in entry

    def test_self_referencing_policied_table_has_an_empty_status_list(self, mapping_workspace):
        """`ledger` is policied AND `policy_mapping=true`, but its own policy
        body only references itself -- the field is present (it IS policied)
        but empty (it has no OTHER mapping dependency)."""
        c = mapping_workspace["client"]
        r = c.get("/api/admin/registry", headers=_auth(mapping_workspace["admin_token"]))
        assert r.status_code == 200, r.text
        entry = self._entry(r.json()["tables"], "ledger")
        assert entry["policy_mapping_status"] == []


class TestPolicyMappingStatusesUnit:
    """Unit tests directly on `src.access_policy.policy_mapping_statuses` --
    the ONE implementation every surface (including the registry list above)
    derives its diagnosis from."""

    def test_remote_mapping_table_is_remote_unknown(self, e2e_env):
        from src.access_policy import policy_mapping_statuses
        from src.db import get_system_db
        from src.repositories.sync_state import SyncStateRepository
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            registry = TableRegistryRepository(conn)
            registry.register(
                id="remote_map2",
                name="remote_map2",
                source_type="bigquery",
                query_mode="remote",
                bucket="ds",
                source_table="m",
            )
            registry.set_policy_mapping("remote_map2", True)
            SyncStateRepository(conn).update_sync("remote_map2", rows=0, file_size_bytes=0, hash="")
        finally:
            conn.close()

        statuses = policy_mapping_statuses(
            "SELECT * FROM orders WHERE unit IN (SELECT unit FROM remote_map2 WHERE email = $user_email)"
        )
        assert statuses == [{"mapping_table": "remote_map2", "state": "remote_unknown", "last_sync": None}]

    def test_never_synced_is_case_insensitive(self, e2e_env):
        from src.access_policy import policy_mapping_statuses
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            registry = TableRegistryRepository(conn)
            registry.register(id="cost_centres", name="cost_centres", source_type="keboola", query_mode="local")
            registry.set_policy_mapping("cost_centres", True)
        finally:
            conn.close()

        statuses = policy_mapping_statuses(
            "SELECT * FROM orders WHERE unit IN (SELECT unit FROM Cost_Centres WHERE email = $user_email)"
        )
        assert statuses == [{"mapping_table": "cost_centres", "state": "never_synced", "last_sync": None}]

    def test_no_dependency_returns_empty_list(self, e2e_env):
        from src.access_policy import policy_mapping_statuses

        assert policy_mapping_statuses("SELECT * FROM orders WHERE owner = $user_email") == []

    def test_self_reference_is_excluded(self, e2e_env):
        from src.access_policy import policy_mapping_statuses
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            registry = TableRegistryRepository(conn)
            registry.register(id="ledger9", name="ledger9", source_type="keboola", query_mode="local")
            registry.set_policy_mapping("ledger9", True)
        finally:
            conn.close()

        assert (
            policy_mapping_statuses("SELECT * FROM ledger9 WHERE owner = $user_email", table_name="ledger9") == []
        )

    def test_count_unavailable_reads_as_ok_not_empty(self, e2e_env):
        from src.access_policy import policy_mapping_statuses
        from src.db import get_system_db
        from src.repositories.sync_state import SyncStateRepository
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            registry = TableRegistryRepository(conn)
            registry.register(id="uncounted_map2", name="uncounted_map2", source_type="keboola", query_mode="local")
            registry.set_policy_mapping("uncounted_map2", True)
            state_repo = SyncStateRepository(conn)
            state_repo.update_sync("uncounted_map2", rows=0, file_size_bytes=10, hash="abc")
            state_repo.set_error(
                "uncounted_map2",
                "Row count unavailable for table 'uncounted_map2' in source 'x' -- the published rows=0 is NOT a "
                "verified empty table. See #1364.",
            )
        finally:
            conn.close()

        statuses = policy_mapping_statuses(
            "SELECT * FROM orders WHERE unit IN (SELECT unit FROM uncounted_map2 WHERE email = $user_email)"
        )
        assert statuses[0]["state"] == "ok"
