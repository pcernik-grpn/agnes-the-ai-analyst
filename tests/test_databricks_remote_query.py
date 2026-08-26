"""`agnes query --remote` against a Databricks SQL warehouse (phase 2).

Three layers, cheapest first:

1. **Rewrite** — pure-function tests over the identifier substitution, which is
   the part a bug corrupts silently (a mis-rewritten statement is still valid
   SQL, just against the wrong table).
2. **Gate** — registry + RBAC refusals, driven through the real repository so
   the id-vs-name distinction that has bitten this codebase repeatedly is
   actually exercised.
3. **Wire** — a real HTTPS round-trip against `tests/databricks_fake_warehouse`,
   including the full `/api/query` request, so the cost cap and the "truncated
   is not an answer" rule are proven end to end rather than at a seam.
"""

from __future__ import annotations

import pytest

from connectors.databricks.remote import (
    DatabricksRemoteError,
    execute_select,
    guardrail_inputs,
    rewrite_to_native,
    wrap_with_limit,
)
from src.remote_engines import CrossEngineError, referenced_remote_rows, resolve_single_engine


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(
    *,
    id: str,
    name: str,
    source_type: str,
    bucket: str,
    source_table: str,
    query_mode: str = "remote",
) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=id,
            name=name,
            source_type=source_type,
            bucket=bucket,
            source_table=source_table,
            query_mode=query_mode,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Rewrite
# ---------------------------------------------------------------------------


class TestRewrite:
    def test_bare_name_becomes_backticked_three_part_path(self):
        out = rewrite_to_native("SELECT * FROM orders", [("orders", "main", "sales", "orders_raw")], "main")
        assert out == "SELECT * FROM `main`.`sales`.`orders_raw`"

    def test_dbx_path_becomes_backticked_three_part_path(self):
        out = rewrite_to_native('SELECT * FROM dbx."sales"."orders_raw"', [], "main")
        assert out == "SELECT * FROM `main`.`sales`.`orders_raw`"

    def test_dotted_bucket_in_dbx_path_overrides_default_catalog(self):
        out = rewrite_to_native('SELECT * FROM dbx."prod.sales"."orders_raw"', [], "main")
        assert out == "SELECT * FROM `prod`.`sales`.`orders_raw`"

    def test_existing_backtick_path_is_left_alone(self):
        """A statement copied out of the Databricks UI already names its
        tables natively — rewriting inside those backticks would produce
        nested quoting, the exact issue-#201 corruption."""
        sql = "SELECT * FROM `prod`.`sales`.`orders`"
        assert rewrite_to_native(sql, [("orders", "main", "sales", "o")], "main") == sql

    def test_keyword_named_table_only_rewrites_in_table_position(self):
        """A table registered as `order` must not eat the ORDER BY keyword."""
        out = rewrite_to_native(
            "SELECT x FROM order ORDER BY x",
            [("order", "main", "sales", "orders_raw")],
            "main",
        )
        assert out == "SELECT x FROM `main`.`sales`.`orders_raw` ORDER BY x"

    def test_longest_name_wins_over_shared_prefix(self):
        out = rewrite_to_native(
            "SELECT * FROM orders_daily",
            [("orders", "main", "s", "a"), ("orders_daily", "main", "s", "b")],
            "main",
        )
        assert out == "SELECT * FROM `main`.`s`.`b`"

    def test_unsafe_identifier_is_refused_not_escaped(self):
        """A registry row whose segments could break out of the backtick
        quoting is rejected outright — escaping it would put the decision in
        the hands of whoever wrote the row."""
        with pytest.raises(DatabricksRemoteError) as exc:
            rewrite_to_native("SELECT * FROM t", [("t", "main", "s", "a`; DROP TABLE x; --")], "main")
        assert exc.value.reason == "databricks_unsafe_identifier"

    def test_wrap_with_limit_bounds_the_result(self):
        assert wrap_with_limit("SELECT 1", 10).endswith("LIMIT 10")
        assert "SELECT 1" in wrap_with_limit("SELECT 1", 10)

    def test_wrap_with_limit_strips_one_trailing_semicolon(self):
        # A trailing `;` is a legal top-level statement terminator but breaks
        # Spark SQL once embedded inside this subquery wrap; /api/query's
        # SELECT-only guard tolerates it, so the wrap must too.
        wrapped = wrap_with_limit("SELECT 1;", 10)
        assert ";" not in wrapped
        assert wrapped == wrap_with_limit("SELECT 1", 10)


# ---------------------------------------------------------------------------
# 2. Registry gate + engine arbitration
# ---------------------------------------------------------------------------


class TestGate:
    def test_unregistered_dbx_path_is_refused(self, e2e_env):
        lookups, blocked = guardrail_inputs(
            'SELECT * FROM dbx."nope"."missing"',
            'select * from dbx."nope"."missing"',
            allowed=None,
            is_admin=True,
            default_catalog="main",
        )
        assert lookups == []
        assert blocked["reason"] == "databricks_table_not_registered"
        assert "register-table" in blocked["hint"]

    def test_registered_path_passes_for_admin(self, e2e_env):
        _register(id="dbx.sales.orders", name="orders", source_type="databricks", bucket="sales", source_table="o")
        _, blocked = guardrail_inputs(
            'SELECT * FROM dbx."sales"."o"',
            'select * from dbx."sales"."o"',
            allowed=None,
            is_admin=True,
            default_catalog="main",
        )
        assert blocked is None

    def test_bare_name_without_grant_is_refused(self, e2e_env):
        """The shared master-view denylist cannot cover a remote row with no
        local view, so this gate has to fail closed on its own — a skipped
        reference would run the statement without it."""
        _register(id="dbx.sales.orders", name="orders", source_type="databricks", bucket="sales", source_table="o")
        lookups, blocked = guardrail_inputs(
            "SELECT * FROM orders",
            "select * from orders",
            allowed=[],  # non-admin holding no grants
            is_admin=False,
            default_catalog="main",
        )
        assert lookups == []
        assert blocked["reason"] == "databricks_access_denied"

    def test_direct_path_without_grant_is_refused(self, e2e_env):
        _register(id="dbx.sales.orders", name="orders", source_type="databricks", bucket="sales", source_table="o")
        _, blocked = guardrail_inputs(
            'SELECT * FROM dbx."sales"."o"',
            'select * from dbx."sales"."o"',
            allowed=[],
            is_admin=False,
            default_catalog="main",
        )
        assert blocked["reason"] == "databricks_access_denied"
        assert blocked["registered_as"] == "orders"

    def test_grant_check_keys_on_id_not_display_name(self, e2e_env):
        """`get_accessible_tables` returns registry ids; matching those against
        display names silently denies every row whose id != name."""
        _register(id="dbx.finance.ue", name="ue", source_type="databricks", bucket="finance", source_table="ue")
        lookups, blocked = guardrail_inputs(
            "SELECT * FROM ue",
            "select * from ue",
            allowed=["dbx.finance.ue"],
            is_admin=False,
            default_catalog="main",
        )
        assert blocked is None
        assert lookups == [("ue", "main", "finance", "ue")]

    def test_materialized_rows_are_not_remote_references(self, e2e_env):
        """A materialized Databricks row has a local parquet — it must keep
        running locally, not get shipped back to the warehouse per query."""
        _register(
            id="dbx.sales.m",
            name="m_orders",
            source_type="databricks",
            bucket="sales",
            source_table="o",
            query_mode="materialized",
        )
        assert referenced_remote_rows("SELECT * FROM m_orders", "select * from m_orders") == {}


class TestEngineArbitration:
    def test_no_remote_rows_means_no_engine(self, e2e_env):
        assert resolve_single_engine(referenced_remote_rows("SELECT 1", "select 1")) is None

    def test_single_engine_resolves(self, e2e_env):
        _register(id="dbx.sales.o", name="orders", source_type="databricks", bucket="sales", source_table="o")
        refs = referenced_remote_rows("SELECT * FROM orders", "select * from orders")
        assert resolve_single_engine(refs) == "databricks"

    def test_two_engines_in_one_statement_is_refused(self, e2e_env):
        _register(id="dbx.sales.o", name="dbx_orders", source_type="databricks", bucket="sales", source_table="o")
        _register(id="bq.fin.ue", name="bq_ue", source_type="bigquery", bucket="fin", source_table="ue")
        sql = "SELECT * FROM dbx_orders JOIN bq_ue USING (id)"
        with pytest.raises(CrossEngineError) as exc:
            resolve_single_engine(referenced_remote_rows(sql, sql.lower()))
        detail = exc.value.detail()
        assert detail["reason"] == "remote_cross_engine_unsupported"
        assert detail["engines"] == ["bigquery", "databricks"]
        assert "snapshot create" in detail["hint"]


# ---------------------------------------------------------------------------
# 3. Wire — real HTTPS against the fake warehouse
# ---------------------------------------------------------------------------


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """A fake warehouse plus the settings dict the remote path expects."""
    import pyarrow as pa

    from tests.databricks_fake_warehouse import Route, start_fake_warehouse

    table = pa.table({"country": ["CZ", "SK", "PL"], "n": ["1", "2", "3"]})
    routes = [
        Route(match="orders_raw", arrow_table=table),
        Route(match="huge_table", arrow_table=table, truncated=True),
    ]
    wh = start_fake_warehouse(tmp_path, routes, monkeypatch)
    try:
        yield (
            wh,
            {
                "host": wh.host,
                "token": "dapi-test-token",
                "warehouse_id": "wh-1",
                "catalog": "main",
            },
        )
    finally:
        wh.close()


class TestWire:
    def test_rows_come_back_over_real_tls(self, warehouse):
        _wh, settings = warehouse
        columns, rows, truncated, byte_count = execute_select(
            "SELECT country, n FROM `main`.`sales`.`orders_raw`",
            settings=settings,
            limit=10,
            cap_bytes=1_000_000,
            timeout_s=30,
        )
        assert columns == ["country", "n"]
        assert rows == [["CZ", "1"], ["SK", "2"], ["PL", "3"]]
        assert truncated is False
        assert byte_count > 0

    def test_limit_probe_sets_truncated_and_trims(self, warehouse):
        _wh, settings = warehouse
        _cols, rows, truncated, _b = execute_select(
            "SELECT country, n FROM `main`.`sales`.`orders_raw`",
            settings=settings,
            limit=2,
            cap_bytes=1_000_000,
            timeout_s=30,
        )
        assert rows == [["CZ", "1"], ["SK", "2"]]
        assert truncated is True

    def test_byte_limit_is_sent_to_the_warehouse(self, warehouse):
        """The cap is only real if it reaches the API — a client-side check
        after the bytes already crossed the wire would be theatre."""
        wh, settings = warehouse
        execute_select(
            "SELECT country, n FROM `main`.`sales`.`orders_raw`",
            settings=settings,
            limit=10,
            cap_bytes=4242,
            timeout_s=30,
        )
        assert wh.payloads, "no statement was submitted"
        assert wh.payloads[0]["byte_limit"] == 4242

    def test_truncated_result_is_refused_never_returned(self, warehouse):
        """The failure mode this guards against is the worst one available:
        a plausible number that is quietly missing rows."""
        _wh, settings = warehouse
        with pytest.raises(DatabricksRemoteError) as exc:
            execute_select(
                "SELECT * FROM `main`.`sales`.`huge_table`",
                settings=settings,
                limit=10,
                cap_bytes=64,
                timeout_s=30,
            )
        assert exc.value.reason == "remote_scan_too_large"
        assert exc.value.status == 400
        assert "materialized" in exc.value.detail()["hint"]

    def test_presigned_download_carries_no_workspace_token(self, warehouse):
        """The presigned URL is itself the credential; sending the workspace
        bearer to the storage host would hand a third party a live token."""
        wh, settings = warehouse
        execute_select(
            "SELECT country, n FROM `main`.`sales`.`orders_raw`",
            settings=settings,
            limit=10,
            cap_bytes=1_000_000,
            timeout_s=30,
        )
        externals = wh.requests_for("/external/")
        assert externals, "no presigned chunk was fetched"
        for req in externals:
            assert "authorization" not in {k.lower() for k in req.headers}


# ---------------------------------------------------------------------------
# 4. End-to-end through POST /api/query
# ---------------------------------------------------------------------------


class TestQueryEndpoint:
    def test_registered_remote_row_executes_on_the_warehouse(self, seeded_app, warehouse, monkeypatch):
        wh, settings = warehouse
        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda: settings,
        )
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw", "limit": 100},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["columns"] == ["country", "n"]
        assert body["row_count"] == 3

        # The bare registered name must have reached the warehouse as a
        # backticked three-part path, not as `orders_raw`.
        assert wh.statements, "nothing was submitted to the warehouse"
        statement = wh.statements[0]
        assert "`main`.`sales`.`orders_raw`" in statement
        assert "FROM orders_raw" not in statement
        # …and bounded, so a SELECT * against a huge table cannot be pulled
        # into the worker process in full.
        assert "LIMIT 101" in statement

    def test_unconfigured_instance_says_so(self, seeded_app, monkeypatch):
        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda: None,
        )
        _register(id="dbx.sales.o", name="orders_raw", source_type="databricks", bucket="sales", source_table="o")
        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM orders_raw"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 503, r.text
        assert r.json()["detail"]["reason"] == "databricks_not_configured"

    def test_cross_engine_query_is_refused(self, seeded_app, monkeypatch):
        _register(id="dbx.sales.o", name="dbx_orders", source_type="databricks", bucket="sales", source_table="o")
        _register(id="bq.fin.ue", name="bq_ue", source_type="bigquery", bucket="fin", source_table="ue")
        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM dbx_orders JOIN bq_ue USING (id)"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["reason"] == "remote_cross_engine_unsupported"

    def test_local_query_is_untouched_by_the_new_branch(self, seeded_app):
        """The dispatch must be inert for every statement that names no remote
        row — this is the regression that would hit every existing instance."""
        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT 1 AS x"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["rows"] == [[1]]


class TestAccessPolicyInterlock:
    def test_policied_databricks_table_is_filtered_not_shipped_unfiltered(self, seeded_app, warehouse, monkeypatch):
        """The policy travels to the warehouse instead of denying the query.

        This used to assert a refusal (`policy_unsupported_on_remote_engine`):
        the only two options were "deny" and "ship the caller's unfiltered
        statement", and §17 makes that choice for you. There is now a third —
        substitute the policy body into the statement and bind its values as
        API parameters — so the correct assertion is inverted: a 200 whose
        submitted SQL contains the policy predicate.
        """
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        _wh, settings = warehouse
        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda: settings,
        )
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        from tests.conftest import grant_table_via_package

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).set_access_policy(
                "dbx.sales.orders_raw",
                sql="SELECT * FROM orders_raw WHERE country = 'CZ'",
                note="country filter",
                updated_by="admin",
            )
            # The caller must be a granted NON-admin: an admin holds a
            # full-surface credential, for which the policy resolver returns the
            # passthrough relation, so the interlock would never be reached and
            # the test would pass for the wrong reason.
            grant_table_via_package(conn, "dbx.sales.orders_raw", "analyst1")
        finally:
            conn.close()

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text

        submitted = _wh.statements[-1]
        # The policy body was substituted in, and BOTH its own `FROM` and the
        # caller's reference resolved to the physical path.
        assert "country = 'CZ'" in submitted
        assert "`main`.`sales`.`orders_raw`" in submitted
        # ...and the caller's projection still wraps it, so this is the policy
        # applied to their query rather than the policy replacing it.
        assert submitted.count("orders_raw") >= 2


class TestSnapshotInterlock:
    def test_snapshot_from_query_materializes_a_databricks_statement(self, seeded_app, warehouse, monkeypatch):
        """`--from-query` now materializes on Databricks too.

        This used to assert `snapshot_engine_unsupported`. The refusal existed
        because the path would otherwise skip a registry gate that only knew BQ
        rows and then die inside DuckDB; it now routes through the Databricks
        planner, which has a gate of its own.
        """
        from app.api.query import run_remote_select_to_arrow
        from app.api.v2_quota import _build_quota_tracker

        _wh, settings = warehouse
        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda: settings,
        )
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        from src.db import get_system_db

        conn = get_system_db()
        try:
            table = run_remote_select_to_arrow(
                conn,
                {"id": "admin1", "email": "admin@test.com"},
                "SELECT * FROM orders_raw",
                None,
                quota=_build_quota_tracker(),
            )
        finally:
            conn.close()

        assert table.num_rows == 3
        assert set(table.column_names) == {"country", "n"}
        # No `LIMIT n + 1` probe wrap: a materialize wants every row, and the
        # interactive path's truncation signal would silently cap the snapshot.
        assert "agnes_remote_q" not in _wh.statements[-1]

    def test_snapshot_from_query_still_refuses_an_engine_with_no_materialize_path(self, seeded_app, monkeypatch):
        """The refusal is not gone, only narrowed to engines that really cannot."""
        from fastapi import HTTPException

        from app.api.query import run_remote_select_to_arrow

        # The function imports this at call time, so patching the definition
        # site is what the call actually sees.
        monkeypatch.setattr("src.remote_engines.resolve_single_engine", lambda _refs: "snowflake")
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        from src.db import get_system_db

        conn = get_system_db()
        try:
            with pytest.raises(HTTPException) as exc:
                run_remote_select_to_arrow(
                    conn,
                    {"id": "admin1", "email": "admin@test.com"},
                    "SELECT * FROM orders_raw",
                    None,
                    None,
                )
        finally:
            conn.close()
        assert exc.value.status_code == 400
        assert exc.value.detail["reason"] == "snapshot_engine_unsupported"
        assert "agnes query --remote" in exc.value.detail["hint"]


class TestSchemaSurface:
    """`agnes schema <table>` must work on a remote row.

    The repo's own agent rails say "run `agnes schema` before writing any
    query". A remote Databricks row has no parquet, so without a dedicated
    branch the schema endpoint 404s — which reads as "not synced yet" and
    sends the caller hunting for a sync that will never happen.
    """

    def test_columns_come_from_unity_catalog(self, e2e_env, tmp_path, monkeypatch):
        import pyarrow as pa  # noqa: F401  (fixture parity — routes are JSON here)

        from connectors.databricks.remote import fetch_schema
        from tests.databricks_fake_warehouse import Route, start_fake_warehouse

        warehouse = start_fake_warehouse(
            tmp_path,
            [
                Route(
                    match="information_schema.columns",
                    columns=["column_name", "full_data_type", "is_nullable", "comment"],
                    rows=[["country", "string", "YES", "ISO code"], ["n", "bigint", "NO", None]],
                )
            ],
            monkeypatch,
        )
        try:
            cols = fetch_schema(
                {"bucket": "sales", "source_table": "orders_raw"},
                settings={
                    "host": warehouse.host,
                    "token": "dapi-test",
                    "warehouse_id": "wh-1",
                    "catalog": "main",
                },
            )
        finally:
            warehouse.close()

        assert cols == [
            {"name": "country", "type": "STRING", "nullable": True, "description": "ISO code"},
            {"name": "n", "type": "BIGINT", "nullable": False, "description": ""},
        ]

    def test_endpoint_advertises_the_databricks_dialect(self, seeded_app, tmp_path, monkeypatch):
        """An agent taught `sql_flavor` writes `DATE_SUB(CURRENT_DATE(), 30)`,
        not the BigQuery or DuckDB spelling."""
        from tests.databricks_fake_warehouse import Route, start_fake_warehouse

        warehouse = start_fake_warehouse(
            tmp_path,
            [
                Route(
                    match="information_schema.columns",
                    columns=["column_name", "full_data_type", "is_nullable", "comment"],
                    rows=[["country", "string", "YES", None]],
                )
            ],
            monkeypatch,
        )
        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda: {
                "host": warehouse.host,
                "token": "dapi-test",
                "warehouse_id": "wh-1",
                "catalog": "main",
            },
        )
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        try:
            c = seeded_app["client"]
            r = c.get("/api/v2/schema/dbx.sales.orders_raw", headers=_auth(seeded_app["admin_token"]))
        finally:
            warehouse.close()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sql_flavor"] == "databricks"
        assert [col["name"] for col in body["columns"]] == ["country"]
        assert "CURRENT_DATE()" in body["where_dialect_hints"]["interval_subtract"]


class TestCatalogSurface:
    """What `agnes catalog` tells an agent about a remote Databricks row.

    Both fields here route behaviour: `fetch_via` decides which command the
    agent reaches for, and `sql_flavor` decides which dialect it writes. Wrong
    values do not fail loudly — they send the agent down a path that fails
    later, for a reason that looks like something else.
    """

    def test_fetch_via_does_not_claim_the_row_is_local(self, e2e_env):
        from app.api.v2_catalog import _fetch_hint

        hint = _fetch_hint("dbx.sales.orders", "databricks", False, "remote")
        assert "already local" not in hint
        assert "agnes query --remote" in hint

    def test_materialized_row_still_reads_as_local(self, e2e_env):
        """The fix must not overreach: a materialized Databricks row DOES have
        a local parquet and is queried exactly like any other local table."""
        from app.api.v2_catalog import _fetch_hint

        assert "already local" in _fetch_hint("dbx.sales.m", "databricks", False, "materialized")

    def test_sql_flavor_follows_the_execution_engine(self, e2e_env):
        from app.api.v2_catalog import _flavor_for

        assert _flavor_for("databricks", "remote") == "databricks"
        # Materialized → the statement runs against a parquet in DuckDB.
        assert _flavor_for("databricks", "materialized") == "duckdb"
        assert _flavor_for("bigquery", "remote") == "bigquery"
        assert _flavor_for("keboola", "local") == "duckdb"


class TestServiceCredentialContainment:
    """The statement runs under a workspace PAT that can typically read the
    whole workspace. Every table it names must therefore be one Agnes
    recognises — "unrecognised" is an unauthorised read, not a typo.

    These are the shapes a bare-name/`dbx.*` regex gate let through: they are
    valid Databricks SQL, they are left verbatim by the rewriter (correctly —
    they are already warehouse-native), and each rides along with a
    legitimate registered name so the statement still routes to Databricks.
    """

    def _gate(self, sql, *, allowed=None, is_admin=True):
        return guardrail_inputs(sql, sql.lower(), allowed=allowed, is_admin=is_admin, default_catalog="main")

    @pytest.fixture(autouse=True)
    def _registered(self, e2e_env):
        _register(
            id="dbx.sales.orders",
            name="orders",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )

    def test_backticked_three_part_path_cannot_ride_along(self):
        _lookups, blocked = self._gate("SELECT * FROM orders JOIN `main`.`hr`.`payroll` USING (id)")
        assert blocked is not None
        assert blocked["reason"] == "databricks_table_not_registered"
        assert "payroll" in blocked["table"]

    def test_bare_two_part_path_cannot_ride_along(self):
        """Two-part `schema.table` resolves against the warehouse's default
        catalog — no backticks needed to reach an unregistered table."""
        _lookups, blocked = self._gate("SELECT * FROM orders JOIN hr.payroll USING (id)")
        assert blocked is not None
        assert blocked["reason"] == "databricks_table_not_registered"

    def test_unqualified_unregistered_name_cannot_ride_along(self):
        _lookups, blocked = self._gate("SELECT * FROM orders JOIN payroll USING (id)")
        assert blocked is not None
        assert blocked["reason"] == "databricks_table_not_registered"

    def test_admin_is_not_exempt_from_registration(self):
        """The admin bypass covers *grants*, never registration: an admin may
        read every registered table, not every table in the workspace."""
        _lookups, blocked = self._gate("SELECT * FROM orders JOIN `main`.`hr`.`payroll` USING (id)", is_admin=True)
        assert blocked is not None

    def test_registered_row_reachable_by_its_full_path(self):
        """Gating references must not break the legitimate spelling: the
        registered row's own fully-qualified path resolves."""
        _lookups, blocked = self._gate("SELECT * FROM `main`.`sales`.`orders_raw`")
        assert blocked is None

    def test_ctes_are_legal_references(self):
        _lookups, blocked = self._gate("WITH recent AS (SELECT * FROM orders) SELECT * FROM recent")
        assert blocked is None

    def test_unparseable_statement_is_refused_not_forwarded(self):
        """An unparseable statement is exactly the one whose references cannot
        be checked, so it is the last thing that should be waved through."""
        _lookups, blocked = self._gate("SELECT * FROM orders WHERE ((((")
        assert blocked is not None
        assert blocked["reason"] == "databricks_sql_unparseable"

    def test_endpoint_returns_400_for_unparseable_and_403_for_unauthorised(self, seeded_app, warehouse, monkeypatch):
        _wh, settings = warehouse
        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda: settings,
        )
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])

        r = c.post("/api/query", json={"sql": "SELECT * FROM orders WHERE (((("}, headers=headers)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["reason"] == "databricks_sql_unparseable"

        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM orders JOIN `main`.`hr`.`payroll` USING (id)"},
            headers=headers,
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["reason"] == "databricks_table_not_registered"


class TestDialectSurvivesTheGate:
    """The gate fails closed, so anything it cannot parse is refused.

    That makes sqlglot's Databricks coverage a hard dependency of the feature's
    headline use case — `MEASURE()` over a Unity Catalog metric view is the one
    query that CANNOT be answered any other way, and a sqlglot upgrade that
    stopped parsing it would turn the flagship path into a 400. These pin the
    syntax that must keep working.
    """

    @pytest.fixture(autouse=True)
    def _registered(self, e2e_env):
        _register(
            id="dbx.sales.kpis",
            name="revenue_kpis",
            source_type="databricks",
            bucket="sales",
            source_table="kpis",
        )

    @pytest.mark.parametrize(
        "sql",
        [
            # The reason this connector exists.
            "SELECT o_date, MEASURE(`Total Revenue`) FROM revenue_kpis GROUP BY o_date",
            # Spark-only syntax an analyst will reasonably reach for.
            "SELECT * FROM revenue_kpis QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY d) = 1",
            "SELECT date_sub(current_date(), 30) FROM revenue_kpis WHERE c RLIKE 'p'",
            "SELECT * FROM revenue_kpis TIMESTAMP AS OF '2026-01-01'",
            "WITH r AS (SELECT * FROM revenue_kpis) SELECT COUNT(*) FROM r",
        ],
    )
    def test_databricks_syntax_is_not_refused(self, sql):
        lookups, blocked = guardrail_inputs(sql, sql.lower(), allowed=None, is_admin=True, default_catalog="main")
        assert blocked is None, blocked
        assert ("revenue_kpis", "main", "sales", "kpis") in lookups

    def test_measure_query_reaches_the_warehouse_rewritten(self):
        sql = "SELECT o_date, MEASURE(`Total Revenue`) FROM revenue_kpis GROUP BY o_date"
        lookups, blocked = guardrail_inputs(sql, sql.lower(), allowed=None, is_admin=True, default_catalog="main")
        assert blocked is None
        out = rewrite_to_native(sql, lookups, "main")
        # The metric view is addressed by its real path; MEASURE() and the
        # backticked measure name pass through untouched.
        assert "`main`.`sales`.`kpis`" in out
        assert "MEASURE(`Total Revenue`)" in out


class TestResultFetchFailure:
    def test_mid_stream_transport_failure_is_translated(self):
        """Chunks are fetched lazily as the loop reaches them, and presigned
        links expire in minutes — so a transport failure surfaces from
        `iter_batches`, not from the submit call. Unwrapped, it escapes as a
        500 carrying a raw vendor message.
        """
        from connectors.databricks.client import DatabricksApiError

        class _Result:
            truncated = False
            total_byte_count = 10
            schema_columns = [{"name": "a"}]

            def iter_batches(self):
                raise DatabricksApiError("presigned link expired", status=403)
                yield  # pragma: no cover - generator marker

        class _Client:
            def execute_to_arrow_batches(self, *_a, **_kw):
                return _Result()

        with pytest.raises(DatabricksRemoteError) as exc:
            execute_select(
                "SELECT 1",
                settings={"host": "https://h.example", "token": "t", "warehouse_id": "w"},
                limit=10,
                cap_bytes=1000,
                timeout_s=5,
                client=_Client(),
            )
        assert exc.value.reason == "databricks_result_fetch_failed"
        assert exc.value.status == 502
