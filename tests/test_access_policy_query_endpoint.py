"""Task 7 -- POST /api/query enforces table access policies live (table
access policies design doc; plan Task 7).

End-to-end over a real ``TestClient`` + real DuckDB (no mocks): a
``server_only`` table carries a row+column policy filtering by
``$user_groups`` and excluding a column. Two non-admin users, each in their
own group, see disjoint row slices and never the masked column; the admin
(full-surface credential) sees the raw, unfiltered table; a caller CTE
aliased identically to the policied table collides and 400s rather than
silently picking one interpretation (§5.2 rule 4, §16).

This is the FIRST test that exercises enforcement through the live HTTP
endpoint -- Tasks 1-6 (already committed) only reach the resolver/rewrite
helpers directly.
"""

from __future__ import annotations

import contextlib

import pytest

POLICY_SQL = "SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit)"


@pytest.fixture
def policied_orders(seeded_app, mock_extract_factory, monkeypatch):
    """One ``server_only`` ``orders`` table carrying ``POLICY_SQL``, granted
    to two non-admin users each in their own group (``TeamA`` / ``TeamB`` --
    also the ``unit`` values the policy filters on), plus the seeded admin.
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
            }
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="tbl_orders",
            name="orders",
            source_type="keboola",
            query_mode="local",
            server_only=True,
        )
        registry.set_access_policy("tbl_orders", sql=POLICY_SQL, note="unit filter", updated_by="admin")

        users = UserRepository(conn)
        users.create(id="u_team_a", email="team-a@example.com", name="Team A")
        users.create(id="u_team_b", email="team-b@example.com", name="Team B")

        # Each grant also creates the group the policy's $user_groups reads --
        # "TeamA"/"TeamB" are simultaneously the RBAC-visibility group and the
        # row-filter value, which is the ordinary shape for this feature.
        grant_table_via_package(conn, "tbl_orders", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "tbl_orders", "u_team_b", group_name="TeamB")
    finally:
        conn.close()

    return {
        **seeded_app,
        "team_a_token": create_access_token("u_team_a", "team-a@example.com"),
        "team_b_token": create_access_token("u_team_b", "team-b@example.com"),
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_user_sees_only_their_unit_and_not_the_masked_column(policied_orders):
    c = policied_orders["client"]
    r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(policied_orders["team_a_token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "secret" not in body["columns"], body["columns"]
    assert body["row_count"] == 2, body
    id_idx = body["columns"].index("id")
    assert {row[id_idx] for row in body["rows"]} == {"1", "2"}


def test_other_user_sees_only_their_own_unit_slice(policied_orders):
    c = policied_orders["client"]
    r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(policied_orders["team_b_token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "secret" not in body["columns"], body["columns"]
    assert body["row_count"] == 1, body
    id_idx = body["columns"].index("id")
    assert {row[id_idx] for row in body["rows"]} == {"3"}


def test_admin_sees_all_rows_and_all_columns_unfiltered(policied_orders):
    c = policied_orders["client"]
    r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(policied_orders["admin_token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "secret" in body["columns"], body["columns"]
    assert body["row_count"] == 3, body


def test_cte_alias_colliding_with_policied_table_name_is_400(policied_orders):
    c = policied_orders["client"]
    r = c.post(
        "/api/query",
        json={"sql": "WITH orders AS (SELECT 1 AS x) SELECT * FROM orders"},
        headers=_auth(policied_orders["team_a_token"]),
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "policy_name_collision"
    assert detail["table"] == "tbl_orders"
    assert "cte" in detail["fix"].lower() or "rename" in detail["fix"].lower()


class _FakeQuota:
    """Minimal quota double for direct ``run_remote_select_to_arrow`` calls
    (mirrors ``tests/test_remote_select_bq_labels.py``). The policied table
    here is ``query_mode='local'``, so ``dry_run_set`` stays empty and none
    of the byte-budget bookkeeping this stubs out is ever exercised.
    """

    def check_daily_budget(self, user=None):
        pass

    def acquire(self, user=None):
        return contextlib.nullcontext()

    def record_bytes(self, user=None, n=0):
        pass


class TestRunRemoteSelectToArrowSharesTheSameEnforcement:
    """``run_remote_select_to_arrow`` backs the snapshot ``from_query`` path
    (``agnes query --remote --auto-snapshot``) -- it must not be a route
    around a policy /api/query would have enforced for the same SQL."""

    @staticmethod
    def _call(policied_orders, user: dict, sql: str):
        from src.db import get_system_db

        from app.api.query import run_remote_select_to_arrow

        conn = get_system_db()
        try:
            return run_remote_select_to_arrow(conn, user, sql, bq=None, quota=_FakeQuota())
        finally:
            conn.close()

    def test_filters_rows_and_masks_column_for_a_non_admin_caller(self, policied_orders):
        result = self._call(
            policied_orders,
            {"id": "u_team_a", "email": "team-a@example.com"},
            "SELECT * FROM orders",
        )
        # DuckDB 1.5.2's `.execute(...).arrow()` hands back a
        # RecordBatchReader, not an already-materialized Table -- read it
        # out fully before inspecting columns/rows.
        table = result.read_all() if hasattr(result, "read_all") else result
        assert "secret" not in table.column_names, table.column_names
        assert table.num_rows == 2
        assert set(table.column("id").to_pylist()) == {"1", "2"}

    def test_cte_alias_collision_maps_to_400_policy_name_collision(self, policied_orders):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            self._call(
                policied_orders,
                {"id": "u_team_a", "email": "team-a@example.com"},
                "WITH orders AS (SELECT 1 AS x) SELECT * FROM orders",
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["reason"] == "policy_name_collision"
        assert exc_info.value.detail["table"] == "tbl_orders"


BROKEN_POLICY_SQL = "SELECT * FROM orders WHERE ((("


class TestAResolutionRefusalFailsClosed:
    """#1979: a policy that FAILS TO RESOLVE must deny, not disappear.

    ``rewrite_sql`` swallowed every ``PolicyError`` its per-name ``resolve``
    raised, because that one type also carried the registry's benign "no
    such table" signal. A refusal on a table that IS registered and IS
    policied therefore left the reference unsubstituted and served the raw
    base view with a 200 -- the fail-OPEN shape §17 forbids.

    Reproduced here through the resolver's real failure path rather than a
    monkeypatch: an ``access_policy_sql`` that no longer parses (a
    hand-edited registry row, or a body saved by an older, laxer validator)
    makes ``policied_relation`` raise ``PolicyError`` from
    ``_referenced_variables`` -- a genuine refusal that is NOT
    ``PolicyUnknownTable``.
    """

    @pytest.fixture
    def broken_policy(self, policied_orders):
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).set_access_policy(
                "tbl_orders",
                sql=BROKEN_POLICY_SQL,
                note="hand-edited, no longer parses",
                updated_by="admin",
            )
        finally:
            conn.close()
        return policied_orders

    def test_query_returns_structured_policy_error_and_no_rows(self, broken_policy):
        c = broken_policy["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(broken_policy["team_a_token"]))
        assert r.status_code == 500, r.text
        assert r.json()["detail"] == {"reason": "policy_error", "table": "tbl_orders"}
        assert "unit" not in r.text and "secret" not in r.text

    def test_the_admin_bypass_is_unaffected(self, broken_policy):
        # §12: the bypass is decided before the policy body is ever parsed,
        # so a broken policy must not lock an admin out of their own table.
        c = broken_policy["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(broken_policy["admin_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["row_count"] == 3

    def test_a_query_that_never_touches_the_broken_table_still_runs(self, broken_policy):
        # The swallow existed to keep unrelated queries working; narrowing it
        # must not turn an unregistered name into a failure.
        c = broken_policy["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT 1 AS n"},
            headers=_auth(broken_policy["team_a_token"]),
        )
        assert r.status_code == 200, r.text
