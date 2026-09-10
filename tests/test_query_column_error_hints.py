"""``POST /api/query`` — DuckDB binder/catalog errors get a hint that
names the relation involved and points at the next command, on top of
(never instead of) the original DuckDB text.

Production finding (2026-09-09): `query` is the most-used and most
error-prone tool on a live instance -- 27 failures in one day, nearly
all an agent guessing a column name. The message a caller actually
received was a bare DuckDB error naming only an ALIAS ("Values list
\"s\" does not have a column named ...") or no relation at all
("Referenced column ... not found in FROM clause!"), never the next
step (`schema <table>`). These tests assert on the HTTP response body a
caller receives -- REST directly, and via `render_error`/
`_raise_for_status_with_detail` for the CLI and MCP surfaces that relay
this same `detail` string unchanged.
"""

from __future__ import annotations

from src.orchestrator import SyncOrchestrator
from src.repositories.table_registry import TableRegistryRepository


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_customers_and_subtypes(seeded_app, mock_extract_factory):
    """Two real local views: `customers` (subtype_id FK) and
    `ref_customer_subtype` (id + subtype_name -- deliberately NOT named
    `CUSTOMER_SUBTYPE`, so a query aliasing it `s` and asking for
    `s.CUSTOMER_SUBTYPE` reproduces the real production Binder Error)."""
    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {"name": "customers", "data": [{"id": "1", "subtype_id": "10"}]},
            {"name": "ref_customer_subtype", "data": [{"id": "10", "subtype_name": "Gold"}]},
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()


class TestColumnNotFoundViaAlias:
    def test_names_the_real_table_and_keeps_the_original_duckdb_error(self, seeded_app, mock_extract_factory):
        _seed_customers_and_subtypes(seeded_app, mock_extract_factory)
        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={
                "sql": (
                    "SELECT s.CUSTOMER_SUBTYPE AS subtype, COUNT(*) AS current_customers "
                    "FROM customers c LEFT JOIN ref_customer_subtype s ON c.subtype_id = s.id "
                    "GROUP BY 1"
                )
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 400, r.json()
        detail = str(r.json().get("detail", ""))
        # Original DuckDB text is never hidden -- an agent debugging its
        # own SQL still needs it.
        assert "does not have a column named" in detail
        assert "CUSTOMER_SUBTYPE" in detail
        # The alias "s" alone never tells anyone which relation is meant --
        # the hint must name the real table.
        assert "ref_customer_subtype" in detail
        assert "schema ref_customer_subtype" in detail


class TestColumnNotFoundNoRelationNamed:
    def test_single_table_query_names_it(self, seeded_app, mock_extract_factory):
        env = seeded_app["env"]
        mock_extract_factory("keboola", [{"name": "projects", "data": [{"project_id": "1"}]}])
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT PROJECT_START_DATE FROM projects"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 400, r.json()
        detail = str(r.json().get("detail", ""))
        assert "not found in FROM clause" in detail
        assert "projects" in detail
        assert "schema projects" in detail


class TestUnregisteredTableHint:
    def test_plain_typo_points_at_catalog(self, seeded_app):
        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM totally_unknown_table"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 400, r.json()
        detail = str(r.json().get("detail", ""))
        assert "does not exist" in detail
        assert "catalog" in detail
        assert "source-system" not in detail

    def test_source_system_bucket_path_names_the_real_problem(self, seeded_app):
        """The real 2026-09-09 finding: an agent used the Keboola bucket
        path from the semantic layer (`in.c-MODEL_02.BI_FINANCE_REVENUE`)
        as a table id. DuckDB's own "Did you mean" suggestion names an
        unrelated table -- the response must say what actually happened."""
        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": 'SELECT * FROM "in.c-MODEL_02.BI_FINANCE_REVENUE"'},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 400, r.json()
        detail = str(r.json().get("detail", ""))
        assert "in.c-MODEL_02.BI_FINANCE_REVENUE" in detail
        assert "source-system" in detail
        assert "catalog" in detail


class TestMaterializedHintTakesPriority:
    def test_a_registered_but_not_yet_materialized_table_still_gets_its_own_hint(self, seeded_app):
        """Sanity: the pre-existing materialize-aware hint
        (`_materialized_hint_for_query_error`) must still win over the new
        generic "unregistered table" hint for a table that IS registered,
        just not materialized yet -- that case has a more specific,
        actionable answer (`agnes pull` / direct query) than "not
        registered"."""
        from src.db import get_system_db

        sys_conn = get_system_db()
        try:
            TableRegistryRepository(sys_conn).register(
                id="not_yet_materialized_2",
                name="not_yet_materialized_2",
                source_type="bigquery",
                query_mode="materialized",
                source_query='SELECT 1 FROM bq."ds"."t"',
                bucket="ds",
                source_table="t",
            )
        finally:
            sys_conn.close()

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM not_yet_materialized_2 LIMIT 5"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 400, r.json()
        detail = str(r.json().get("detail", "")).lower()
        assert "materialized" in detail
        assert "source-system" not in detail
