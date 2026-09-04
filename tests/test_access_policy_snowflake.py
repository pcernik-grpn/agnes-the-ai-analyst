"""S2 (RLS review, issue #1979) -- the Snowflake arm of table access
policies: `policied_relation(dialect="snowflake")` transpiles a policy body
to Snowflake SQL, mirroring the BigQuery arm (`tests/test_access_policy_
bigquery.py`) and the Databricks arm (`tests/test_databricks_scan_and_
policies.py::TestPolicyDialect`).

Snowflake is architecturally different from the other two remote engines in
this codebase: a `query_mode='remote'` Snowflake row is registered as a
plain DuckDB VIEW over the `sf` ATTACHed catalog
(`connectors/snowflake/extract_init.py::_remote_view_sql`), so an ordinary
`SELECT ... FROM <registered_name>` already executes -- and is already
policy-filtered -- through the pre-existing `dialect="duckdb"` arm, with
DuckDB's own native named-parameter binding. This dialect arm exists for
the same reason the BigQuery/Databricks arms document their own transpile
contract independent of any one caller: any FUTURE surface that must send
Snowflake-*native* SQL text (a live `snowflake_query()` pass-through, a
Snowflake semantic-view `MEASURE()` query that cannot parse as DuckDB SQL
at all, or `/api/v2/sample`'s live remote branch once it is wired -- S1,
tracked separately) needs a transpiled body with the values still bound as
parameters (§6.2), never string-interpolated.
"""

from __future__ import annotations

import pytest

POLICY_SQL = (
    "SELECT * EXCLUDE (national_id), md5(email) AS email FROM invoices WHERE list_contains($user_groups, cost_center)"
)


@pytest.fixture
def transpile_env(e2e_env):
    """A solo, non-admin user with a policied table, seeded directly
    through the repositories -- no HTTP client needed for this arm.
    Mirrors `tests/test_access_policy_bigquery.py`'s fixture of the same
    name byte-for-byte in shape."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    users = UserRepository(conn)
    users.create(id="u_solo", email="solo@example.com", name="Solo")

    groups = UserGroupsRepository(conn)
    finance_gid = groups.create(name="Finance")["id"]
    members = UserGroupMembersRepository(conn)
    members.add_member("u_solo", finance_gid, source="admin")

    registry = TableRegistryRepository(conn)
    registry.register(
        id="tbl_invoices",
        name="invoices",
        source_type="keboola",
        query_mode="local",
        server_only=True,
    )
    registry.set_access_policy("tbl_invoices", sql=POLICY_SQL, note="cost-centre filter", updated_by="admin")
    conn.close()

    return {"id": "u_solo", "email": "solo@example.com"}


class TestSnowflakeTranspile:
    """`policied_relation(dialect="snowflake")` transpiles the policy body
    -- verified against sqlglot 30.17.0's actual output for this repo's
    canonical policy shape (§7.2's BigQuery worked example, mirrored)."""

    def test_exclude_stays_exclude(self, transpile_env):
        """Unlike BigQuery (`EXCEPT`), Snowflake natively supports `SELECT *
        EXCLUDE (col)` -- the same construct DuckDB uses -- so sqlglot keeps
        it as-is rather than rewriting it."""
        from src.access_policy import policied_relation

        result = policied_relation("tbl_invoices", transpile_env, dialect="snowflake")
        assert "EXCLUDE (national_id)" in result.relation_sql

    def test_md5_stays_md5(self, transpile_env):
        """Unlike BigQuery (`TO_HEX(MD5(x))`), Snowflake's own `MD5` already
        returns a hex string -- the same shape DuckDB's does -- so no
        rewrite is needed for the doc's pseudonymization idiom to survive
        the transpile unchanged in meaning."""
        from src.access_policy import policied_relation

        result = policied_relation("tbl_invoices", transpile_env, dialect="snowflake")
        assert "MD5(" in result.relation_sql

    def test_dollar_user_groups_becomes_colon_user_groups(self, transpile_env):
        """The property the whole feature rests on (§6.2): one authored
        policy keeps its values out of SQL text on every engine, because
        sqlglot renders `$name` as each engine's own named-parameter
        marker -- `:name` for Snowflake, same token Databricks uses."""
        from src.access_policy import policied_relation

        result = policied_relation("tbl_invoices", transpile_env, dialect="snowflake")
        assert "$user_groups" not in result.relation_sql
        assert ":user_groups" in result.relation_sql

    def test_group_membership_idiom_transpiles_to_array_contains(self, transpile_env):
        """Snowflake's `ARRAY_CONTAINS` takes (value, array) -- the OPPOSITE
        argument order from Databricks' `ARRAY_CONTAINS(array, value)` --
        so this is worth pinning explicitly rather than assuming parity
        with the sibling engine's shape."""
        from src.access_policy import policied_relation

        result = policied_relation("tbl_invoices", transpile_env, dialect="snowflake")
        assert "ARRAY_CONTAINS(CAST(cost_center AS VARIANT), :user_groups)" in result.relation_sql

    def test_policied_true_and_params_are_the_same_identity_values_as_duckdb_arm(self, transpile_env):
        """§7.2 (mirrored): one authored policy, same bind VALUES on both
        dialects -- only the SQL text differs, never the Python params
        dict shape."""
        from src.access_policy import policied_relation

        duckdb_result = policied_relation("tbl_invoices", transpile_env, dialect="duckdb")
        sf_result = policied_relation("tbl_invoices", transpile_env, dialect="snowflake")

        assert sf_result.policied is True
        assert sf_result.table_id == "tbl_invoices"
        assert sf_result.params == duckdb_result.params
        assert sf_result.relation_sql != duckdb_result.relation_sql

    def test_admin_bypass_holds_on_the_snowflake_dialect_too(self, seeded_app, transpile_env):
        """§12's admin bypass is identity resolution, not dialect-specific
        -- an admin gets the SAME unfiltered passthrough on every arm."""
        from src.db import SYSTEM_ADMIN_GROUP, get_system_db
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.users import UserRepository
        from src.access_policy import policied_relation

        conn = get_system_db()
        try:
            admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
            UserGroupMembersRepository(conn).add_member("u_admin_sf", admin_gid, source="system_seed")
            UserRepository(conn).create(id="u_admin_sf", email="admin-sf@example.com", name="Admin SF")
        finally:
            conn.close()

        result = policied_relation(
            "tbl_invoices", {"id": "u_admin_sf", "email": "admin-sf@example.com"}, dialect="snowflake"
        )
        assert result.policied is False
        assert result.params == {}

    def test_a_transpile_failure_is_a_policy_error_not_a_raw_sqlglot_exception(self, e2e_env):
        """§16: an unrecoverable resolution failure is always the SAME
        table-scoped reason code, never an engine-internal exception type
        leaking past the resolver."""
        from src.access_policy import PolicyError, policied_relation
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        from src.repositories.users import UserRepository

        conn = get_system_db()
        UserRepository(conn).create(id="u_x", email="x@example.com", name="X")
        registry = TableRegistryRepository(conn)
        registry.register(id="tbl_bad", name="bad_policy_tbl", source_type="keboola", query_mode="local")
        registry.set_access_policy("tbl_bad", sql="NOT VALID SQL (((", note="broken", updated_by="admin")
        conn.close()

        with pytest.raises(PolicyError) as exc_info:
            policied_relation("tbl_bad", {"id": "u_x", "email": "x@example.com"}, dialect="snowflake")
        assert exc_info.value.table_id == "tbl_bad"


class TestSnowflakeDirectTranspileFunction:
    """`_transpile_policy_to_snowflake` in isolation -- no DB fixtures
    needed, mirroring `tests/test_databricks_scan_and_policies.py::
    TestPolicyDialect`'s direct-function assertions for the sibling
    engine."""

    def test_placeholder_becomes_the_snowflake_parameter_marker(self):
        from src.access_policy import _transpile_policy_to_snowflake

        out = _transpile_policy_to_snowflake("SELECT * FROM t WHERE owner = $user_email", table_id="t")
        assert ":user_email" in out
        assert "$user_email" not in out

    def test_untranspilable_body_raises_policy_error_not_the_engine_message(self):
        from src.access_policy import PolicyError, _transpile_policy_to_snowflake

        with pytest.raises(PolicyError):
            _transpile_policy_to_snowflake("this is not sql at all !!", table_id="t")


def test_unknown_dialect_still_rejected():
    """The dialect allowlist stays closed -- adding "snowflake" must not
    accidentally widen it to accept an arbitrary string."""
    from src.access_policy import policied_relation

    with pytest.raises(ValueError, match="unknown dialect"):
        policied_relation("whatever", {"id": "u"}, dialect="postgres")
