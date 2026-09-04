"""Task 8 -- table access policies enforced on the ``table_id``-shaped read
surfaces (table access policies design doc §5, §8; plan Task 8).

``/api/v2/sample``, ``/api/v2/scan`` (local branch) and
``POST /api/mcp/query-table/{id}`` never open the analytics catalog the way
``/api/query`` does -- they build their own ``FROM <source>`` (a throwaway
``read_parquet(...)`` in a fresh ``:memory:`` connection, or a bare view
reference) instead of parsing caller SQL, so ``rewrite_sql`` (Task 6) does
not reach them. This is the FIRST test that exercises these three surfaces
through the live HTTP endpoints with a policy attached.

End-to-end over a real ``TestClient`` + real DuckDB (no mocks): a
``server_only`` table carries a row+column policy filtering by
``$user_groups`` and excluding a column. Two non-admin users, each in their
own group, see disjoint row slices and never the masked column; the admin
(full-surface credential) sees the raw, unfiltered table. A sibling table
with NO policy attached is exercised the same way to pin that the inert case
is untouched.
"""

from __future__ import annotations

import pytest

POLICY_SQL = "SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit)"

# §15's flagship shape: a `policy_mapping` join is idiomatically authored as
# `WITH allowed AS (...) SELECT ... FROM <table> ...` -- a policy body that
# ALREADY opens with its own WITH clause, unlike POLICY_SQL above. Filters
# by `$user_groups` through the CTE rather than directly in the outer WHERE
# (mechanically different from POLICY_SQL, same resulting slice) so this
# stays a meaningful per-caller filter, not just a shape that happens to
# parse. Self-contained (the CTE reads the table's own rows rather than an
# external mapping table) so it resolves inside the throwaway `:memory:`
# connection `/api/v2/sample` and `/api/v2/scan`'s local branch open with
# only ONE parquet attached -- a genuine cross-table `policy_mapping` join
# is covered separately, directly against `policied_from_sql`, below.
WITH_SHAPED_POLICY_SQL = (
    "WITH allowed AS (SELECT DISTINCT unit FROM orders_cte WHERE list_contains($user_groups, unit)) "
    "SELECT * EXCLUDE (secret) FROM orders_cte WHERE unit IN (SELECT unit FROM allowed)"
)


class TestPoliciedFromSql:
    """Direct unit coverage for the shared FROM builder
    (``src.access_policy.policied_from_sql``) all three ``table_id``
    surfaces below route through -- pins its exact contract independent of
    any one endpoint's plumbing around it."""

    def test_wraps_the_relation_body_in_a_cte_over_the_source(self):
        from src.access_policy import PoliciedRelation, policied_from_sql

        relation = PoliciedRelation(
            relation_sql="SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit)",
            params={"user_groups": ["TeamA"]},
            policied=True,
            table_id="orders",
        )
        out = policied_from_sql(relation, table_name="orders", source_sql="read_parquet('/tmp/x.parquet')")
        assert out == (
            "(WITH \"orders\" AS (SELECT * FROM read_parquet('/tmp/x.parquet')) "
            "SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit))"
        )

    def test_refuses_a_non_policied_relation(self):
        from src.access_policy import PoliciedRelation, policied_from_sql
        from src.sql_ident import quote_ident

        relation = PoliciedRelation(
            relation_sql=f"SELECT * FROM {quote_ident('orders')}",
            params={},
            policied=False,
            table_id="orders",
        )
        with pytest.raises(ValueError):
            policied_from_sql(relation, table_name="orders", source_sql="read_parquet('/tmp/x.parquet')")

    def test_the_non_with_case_is_still_byte_identical(self):
        """Regression guard: a plain (non-WITH) policy body must produce
        EXACTLY the same string this function has always produced -- the
        WITH-shaped merge path below must never touch this branch."""
        from src.access_policy import PoliciedRelation, policied_from_sql

        relation = PoliciedRelation(
            relation_sql="SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit)",
            params={"user_groups": ["TeamA"]},
            policied=True,
            table_id="orders",
        )
        out = policied_from_sql(relation, table_name="orders", source_sql="read_parquet('/tmp/x.parquet')")
        assert out == (
            "(WITH \"orders\" AS (SELECT * FROM read_parquet('/tmp/x.parquet')) "
            "SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit))"
        )

    def test_with_shaped_policy_body_merges_cte_lists_instead_of_stacking_two_withs(self):
        """The bug this test pins: the prior implementation string-
        concatenated a second ``WITH <table_name> AS (...)`` in FRONT of a
        policy body that ALREADY opens with its own ``WITH`` clause --
        §15's flagship ``policy_mapping`` join idiom
        (``WITH allowed AS (...) SELECT ... FROM <table> JOIN allowed ...``).
        Two adjacent ``WITH`` keywords is a DuckDB syntax error. The fix
        merges both CTE lists into ONE ``WITH`` clause -- this asserts the
        produced SQL both PARSES and EXECUTES correctly against real
        DuckDB tables standing in for the base table and an external
        ``policy_mapping`` table (the genuinely cross-table shape the
        design doc leads with -- not the self-contained variant the HTTP
        endpoint tests below use, which sidesteps a separate, pre-existing
        limitation of their own throwaway connections)."""
        import duckdb

        from src.access_policy import PoliciedRelation, policied_from_sql

        relation = PoliciedRelation(
            relation_sql=(
                "WITH allowed AS (SELECT unit FROM cost_center_map) "
                "SELECT * FROM orders WHERE unit IN (SELECT unit FROM allowed)"
            ),
            params={},
            policied=True,
            table_id="orders",
        )
        out = policied_from_sql(relation, table_name="orders", source_sql="orders_src")

        conn = duckdb.connect(":memory:")
        try:
            conn.execute("CREATE TABLE orders_src (id INTEGER, unit VARCHAR)")
            conn.execute("INSERT INTO orders_src VALUES (1, 'TeamA'), (2, 'TeamB')")
            conn.execute("CREATE TABLE cost_center_map (unit VARCHAR)")
            conn.execute("INSERT INTO cost_center_map VALUES ('TeamA')")
            rows = conn.execute(f"SELECT * FROM {out} ORDER BY id").fetchall()
        finally:
            conn.close()
        assert rows == [(1, "TeamA")]

    def test_with_shaped_recursive_policy_body_also_merges_correctly(self):
        """`WITH RECURSIVE` is a distinct grammar shape (the RECURSIVE
        keyword sits between WITH and the CTE list, not per-CTE) -- this
        pins that the merge keeps it in the right place rather than
        dropping it or misplacing it inside the merged CTE list."""
        import duckdb

        from src.access_policy import PoliciedRelation, policied_from_sql

        relation = PoliciedRelation(
            relation_sql=(
                "WITH RECURSIVE counted(n) AS ("
                "SELECT 1 UNION ALL SELECT n + 1 FROM counted WHERE n < 2"
                ") SELECT * FROM orders WHERE id IN (SELECT n FROM counted)"
            ),
            params={},
            policied=True,
            table_id="orders",
        )
        out = policied_from_sql(relation, table_name="orders", source_sql="orders_src")

        conn = duckdb.connect(":memory:")
        try:
            conn.execute("CREATE TABLE orders_src (id INTEGER, unit VARCHAR)")
            conn.execute("INSERT INTO orders_src VALUES (1, 'TeamA'), (2, 'TeamB'), (3, 'TeamC')")
            rows = conn.execute(f"SELECT * FROM {out} ORDER BY id").fetchall()
        finally:
            conn.close()
        assert rows == [(1, "TeamA"), (2, "TeamB")]


@pytest.fixture
def policied_orders(seeded_app, mock_extract_factory, monkeypatch):
    """A ``server_only`` ``orders`` table carrying ``POLICY_SQL``, a sibling
    ``products`` table with no policy attached at all, and a second
    ``server_only`` ``orders_cte`` table carrying ``WITH_SHAPED_POLICY_SQL``
    (the CTE-desync regression, §15's flagship shape) -- all granted to two
    non-admin users each in their own group (``TeamA`` / ``TeamB``, also the
    ``unit`` values both policies filter on), plus the seeded admin.

    ``id == name`` for every table (deliberately, unlike Task 7's fixture)
    -- ``/api/v2/sample`` and ``/api/v2/scan``'s local branch resolve their
    parquet by registry ``id`` (``app/utils.py::resolve_local_parquet``),
    while the mock extract writes the parquet filename from the per-table
    ``name`` key and the master view is created under that same name (§5.3)
    -- so id and name must coincide here for the parquet to actually be
    found by every surface under test, the same convention
    ``tests/test_mcp_per_table.py`` uses.
    """
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
                "name": "orders",
                "data": [
                    {"id": "1", "unit": "TeamA", "secret": "s1", "amount": "100"},
                    {"id": "2", "unit": "TeamA", "secret": "s2", "amount": "150"},
                    {"id": "3", "unit": "TeamB", "secret": "s3", "amount": "300"},
                ],
            },
            {
                "name": "products",
                "data": [
                    {"id": "1", "sku": "A1"},
                    {"id": "2", "sku": "A2"},
                ],
            },
            {
                "name": "orders_cte",
                "data": [
                    {"id": "1", "unit": "TeamA", "secret": "s1", "amount": "100"},
                    {"id": "2", "unit": "TeamA", "secret": "s2", "amount": "150"},
                    {"id": "3", "unit": "TeamB", "secret": "s3", "amount": "300"},
                ],
            },
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="orders",
            name="orders",
            source_type="keboola",
            query_mode="local",
            server_only=True,
        )
        registry.set_access_policy("orders", sql=POLICY_SQL, note="unit filter", updated_by="admin")

        # No access_policy_sql on this one -- the inert-case control.
        registry.register(
            id="products",
            name="products",
            source_type="keboola",
            query_mode="local",
        )

        registry.register(
            id="orders_cte",
            name="orders_cte",
            source_type="keboola",
            query_mode="local",
            server_only=True,
        )
        registry.set_access_policy(
            "orders_cte", sql=WITH_SHAPED_POLICY_SQL, note="unit filter via CTE", updated_by="admin"
        )

        users = UserRepository(conn)
        users.create(id="u_team_a", email="team-a@example.com", name="Team A")
        users.create(id="u_team_b", email="team-b@example.com", name="Team B")

        # Each grant also creates the group the policy's $user_groups reads --
        # "TeamA"/"TeamB" are simultaneously the RBAC-visibility group and the
        # row-filter value, mirroring Task 7's fixture.
        grant_table_via_package(conn, "orders", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "orders", "u_team_b", group_name="TeamB")
        grant_table_via_package(conn, "products", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "orders_cte", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "orders_cte", "u_team_b", group_name="TeamB")
    finally:
        conn.close()

    return {
        **seeded_app,
        "team_a_token": create_access_token("u_team_a", "team-a@example.com"),
        "team_b_token": create_access_token("u_team_b", "team-b@example.com"),
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── /api/v2/sample ──────────────────────────────────────────────────────


def test_v2_sample_user_sees_only_their_unit_and_not_the_masked_column(policied_orders):
    c = policied_orders["client"]
    r = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["team_a_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 2
    assert {row["id"] for row in rows} == {"1", "2"}
    assert all("secret" not in row for row in rows)


def test_v2_sample_other_user_sees_only_their_own_unit_slice(policied_orders):
    c = policied_orders["client"]
    r = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["team_b_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 1
    assert {row["id"] for row in rows} == {"3"}
    assert all("secret" not in row for row in rows)


def test_v2_sample_admin_sees_all_rows_and_all_columns_unfiltered(policied_orders):
    c = policied_orders["client"]
    r = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["admin_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 3
    assert any("secret" in row for row in rows)


def test_v2_sample_non_policied_sibling_table_is_untouched(policied_orders):
    """The inert case: a table with no access_policy_sql behaves exactly
    like `/api/v2/sample` did before this feature existed -- every row,
    every column, for a non-admin grantee."""
    c = policied_orders["client"]
    r = c.get("/api/v2/sample/products?n=10", headers=_auth(policied_orders["team_a_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 2
    assert {row["id"] for row in rows} == {"1", "2"}
    assert all("sku" in row for row in rows)


def test_v2_sample_with_shaped_policy_user_sees_only_their_unit_and_not_the_masked_column(policied_orders):
    """The CTE-desync regression (§15's flagship shape): a policy body that
    ALREADY opens with its own WITH clause must still resolve, filter, and
    mask correctly through `/api/v2/sample` -- not 500 with an unhandled
    parser error."""
    c = policied_orders["client"]
    r = c.get("/api/v2/sample/orders_cte?n=10", headers=_auth(policied_orders["team_a_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 2
    assert {row["id"] for row in rows} == {"1", "2"}
    assert all("secret" not in row for row in rows)


def test_v2_sample_with_shaped_policy_other_user_sees_only_their_own_unit_slice(policied_orders):
    c = policied_orders["client"]
    r = c.get("/api/v2/sample/orders_cte?n=10", headers=_auth(policied_orders["team_b_token"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 1
    assert {row["id"] for row in rows} == {"3"}
    assert all("secret" not in row for row in rows)


# ── /api/v2/scan (local branch) ─────────────────────────────────────────


def test_v2_scan_user_sees_only_their_unit_and_not_the_masked_column(policied_orders):
    from app.api.v2_arrow import parse_ipc_bytes

    c = policied_orders["client"]
    r = c.post(
        "/api/v2/scan",
        json={"table_id": "orders"},
        headers=_auth(policied_orders["team_a_token"]),
    )
    assert r.status_code == 200, r.text
    table = parse_ipc_bytes(r.content)
    assert "secret" not in table.column_names
    assert table.num_rows == 2
    assert set(table.column("id").to_pylist()) == {"1", "2"}


def test_v2_scan_admin_sees_all_rows_and_all_columns_unfiltered(policied_orders):
    from app.api.v2_arrow import parse_ipc_bytes

    c = policied_orders["client"]
    r = c.post(
        "/api/v2/scan",
        json={"table_id": "orders"},
        headers=_auth(policied_orders["admin_token"]),
    )
    assert r.status_code == 200, r.text
    table = parse_ipc_bytes(r.content)
    assert "secret" in table.column_names
    assert table.num_rows == 3


def test_v2_scan_non_policied_sibling_table_is_untouched(policied_orders):
    from app.api.v2_arrow import parse_ipc_bytes

    c = policied_orders["client"]
    r = c.post(
        "/api/v2/scan",
        json={"table_id": "products"},
        headers=_auth(policied_orders["team_a_token"]),
    )
    assert r.status_code == 200, r.text
    table = parse_ipc_bytes(r.content)
    assert table.num_rows == 2
    assert "sku" in table.column_names


def test_v2_scan_with_shaped_policy_user_sees_only_their_unit_and_not_the_masked_column(policied_orders):
    """The CTE-desync regression on `/api/v2/scan`: `run_scan` resolves the
    schema via `effective_schema` (Task 9 -- ALSO built through
    `policied_from_sql`) before it ever reaches its own local-execution
    branch's separate `policied_from_sql` call, so today this request
    fails at the EARLIER schema-resolution step with the two adjacent WITH
    keywords already breaking `effective_schema`'s own DESCRIBE (an
    uncaught `PolicyError`) -- the local branch's own leak-prone
    `except (duckdb.DataError, duckdb.ProgrammingError)` -> `ValueError`
    -> 400 (which WOULD embed the raw DuckDB parser text, violating §16's
    "a policy failure never quotes the underlying engine message") is
    never actually reached for this request shape. Fixing
    `policied_from_sql` once fixes BOTH call sites -- this asserts a clean
    200 with the correctly filtered/masked rows, confirming neither one
    fails any more."""
    from app.api.v2_arrow import parse_ipc_bytes

    c = policied_orders["client"]
    r = c.post(
        "/api/v2/scan",
        json={"table_id": "orders_cte"},
        headers=_auth(policied_orders["team_a_token"]),
    )
    assert r.status_code == 200, r.text
    assert "WITH" not in r.text and "Parser Error" not in r.text
    table = parse_ipc_bytes(r.content)
    assert "secret" not in table.column_names
    assert table.num_rows == 2
    assert set(table.column("id").to_pylist()) == {"1", "2"}


# ── POST /api/mcp/query-table/{id} ──────────────────────────────────────


def test_mcp_query_table_user_sees_only_their_unit_and_not_the_masked_column(policied_orders):
    c = policied_orders["client"]
    r = c.post(
        "/api/mcp/query-table/orders",
        headers=_auth(policied_orders["team_a_token"]),
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 2
    assert "secret" not in body["columns"]
    assert {row["id"] for row in body["rows"]} == {"1", "2"}
    assert all("secret" not in row for row in body["rows"])


def test_mcp_query_table_other_user_sees_only_their_own_unit_slice(policied_orders):
    c = policied_orders["client"]
    r = c.post(
        "/api/mcp/query-table/orders",
        headers=_auth(policied_orders["team_b_token"]),
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 1
    assert {row["id"] for row in body["rows"]} == {"3"}


def test_mcp_query_table_admin_sees_all_rows_and_the_secret_column(policied_orders):
    c = policied_orders["client"]
    r = c.post(
        "/api/mcp/query-table/orders",
        headers=_auth(policied_orders["admin_token"]),
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 3
    assert "secret" in body["columns"]


def test_mcp_query_table_masked_column_is_not_revealed_in_the_400(policied_orders):
    """A filter on the EXCLUDE'd column must not be silently honored (it
    would leak whether a hidden value matches) NOR list `secret` in the
    error's `allowed` column set (§8's instruction: never reveal an
    EXCLUDE'd column, including in an error message)."""
    c = policied_orders["client"]
    r = c.post(
        "/api/mcp/query-table/orders",
        headers=_auth(policied_orders["team_a_token"]),
        json={"filter": {"secret": "s1"}, "limit": 10},
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "unknown_filter_columns"
    assert "secret" in detail["unknown"]
    assert "secret" not in detail["allowed"]


def test_mcp_query_table_non_policied_sibling_table_is_untouched(policied_orders):
    c = policied_orders["client"]
    r = c.post(
        "/api/mcp/query-table/products",
        headers=_auth(policied_orders["team_a_token"]),
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 2
    assert "sku" in body["columns"]


def test_mcp_query_table_with_shaped_policy_user_sees_only_their_unit_and_not_the_masked_column(policied_orders):
    """The CTE-desync regression on `POST /api/mcp/query-table/{id}`:
    before the fix, `_describe_columns`'s bare `except Exception: return
    []` swallows the double-WITH parser error, producing an empty column
    list and a misleading 409 "sync may not have run yet" instead of the
    correctly filtered rows."""
    c = policied_orders["client"]
    r = c.post(
        "/api/mcp/query-table/orders_cte",
        headers=_auth(policied_orders["team_a_token"]),
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 2
    assert "secret" not in body["columns"]
    assert {row["id"] for row in body["rows"]} == {"1", "2"}
    assert all("secret" not in row for row in body["rows"])


# ── every surface answers a REFUSAL the same way (§16/§17) ──────────────


@pytest.fixture
def broken_policy_orders(policied_orders):
    """The same fixture, with ``orders``' stored policy replaced by a body
    that no longer parses — the shape a hand-edited registry row (or one
    written by an older authoring path) takes. ``policied_relation`` raises
    ``PolicyError`` for it on every surface."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).set_access_policy(
            "orders",
            sql="SELECT * FROM orders WHERE (((",
            note="hand-edited, no longer parses",
            updated_by="admin",
        )
    finally:
        conn.close()
    return policied_orders


def test_v2_scan_reports_a_policy_refusal_as_a_structured_policy_error(broken_policy_orders):
    """#1979: ``/api/v2/scan`` let ``PolicyError`` escape uncaught — its
    ``except`` tuple never listed it — so a refusal surfaced as an
    unstructured 500 traceback while ``/api/v2/sample`` and
    ``/api/mcp/query-table`` returned ``{"reason": "policy_error"}``. Same
    refusal, same shape, on every surface: fail-closed either way, but only
    one of them is something a CLI or an agent can render (§16)."""
    c = broken_policy_orders["client"]
    r = c.post("/api/v2/scan", json={"table_id": "orders"}, headers=_auth(broken_policy_orders["team_a_token"]))
    assert r.status_code == 500, r.text
    assert r.json()["detail"] == {"reason": "policy_error", "table": "orders"}
    assert "s1" not in r.text and "TeamA" not in r.text


def test_v2_sample_reports_the_same_refusal_the_same_way(broken_policy_orders):
    c = broken_policy_orders["client"]
    r = c.get("/api/v2/sample/orders", headers=_auth(broken_policy_orders["team_a_token"]))
    assert r.status_code == 500, r.text
    assert r.json()["detail"] == {"reason": "policy_error", "table": "orders"}


def test_v2_scan_of_the_unpolicied_sibling_is_unaffected(broken_policy_orders):
    c = broken_policy_orders["client"]
    r = c.post("/api/v2/scan", json={"table_id": "products"}, headers=_auth(broken_policy_orders["team_a_token"]))
    assert r.status_code == 200, r.text
