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
