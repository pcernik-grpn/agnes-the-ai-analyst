"""S2 (RLS review, issue #1979) -- binding an access policy's identity
values through Snowflake's own parameter mechanism. Mirrors
`tests/test_databricks_scan_and_policies.py::TestParameterBinding`, adapted
for the ONE way Snowflake's bind mechanism differs from Databricks': see
`connectors/snowflake/policy_params.py`'s module docstring for why every
variable -- not just the array-valued one -- has to be renumbered here.
"""

from __future__ import annotations

import pytest

from connectors.snowflake.policy_params import (
    SnowflakePolicyBindingError,
    bind_policy_parameters,
)


class TestScalarBinding:
    def test_a_single_scalar_becomes_a_numbered_marker(self):
        sql, values = bind_policy_parameters("SELECT * FROM t WHERE owner = :user_email", {"user_email": "a@b.com"})
        assert sql == "SELECT * FROM t WHERE owner = :1"
        assert values == ["a@b.com"]

    def test_two_scalars_are_numbered_in_order_encountered(self):
        sql, values = bind_policy_parameters(
            "SELECT * FROM t WHERE owner = :user_email AND id = :user_id",
            {"user_email": "a@b.com", "user_id": "u1"},
        )
        assert ":user_email" not in sql
        assert ":user_id" not in sql
        assert ":1" in sql and ":2" in sql
        # Whichever order the two markers appear in the rewritten text, the
        # values list matches it positionally.
        first, second = (":1", ":2") if sql.index(":1") < sql.index(":2") else (":2", ":1")
        expected = {"a@b.com", "u1"}
        assert set(values) == expected

    def test_none_binds_as_a_raw_none_not_a_string(self):
        """Snowflake's DB-API driver binds SQL NULL for a plain Python
        `None` in the positional values sequence -- the same fail-closed
        distinction the Databricks module documents (`owner_id = :n` with a
        NULL bind matches no row; `''` would match any empty-string row)."""
        sql, values = bind_policy_parameters("SELECT * FROM t WHERE owner_id = :user_id", {"user_id": None})
        assert sql == "SELECT * FROM t WHERE owner_id = :1"
        assert values == [None]

    def test_a_marker_repeated_in_the_body_is_bound_at_every_occurrence(self):
        """Unlike Databricks' NAMED markers (one value, referenced by name
        wherever it appears), Snowflake bind variables are POSITIONAL --
        every occurrence of the same policy variable needs its own numbered
        marker and its own copy of the value."""
        sql, values = bind_policy_parameters(
            "SELECT * FROM t WHERE owner = :user_email OR backup_owner = :user_email",
            {"user_email": "a@b.com"},
        )
        assert ":user_email" not in sql
        assert values == ["a@b.com", "a@b.com"]

    def test_unparseable_body_denies(self):
        with pytest.raises(SnowflakePolicyBindingError):
            bind_policy_parameters("!!! not sql", {"user_email": "a@b.com"})


class TestArrayBinding:
    def test_array_variable_expands_to_a_bracket_literal_of_numbered_markers(self):
        """Snowflake accepts `[v1, v2, ...]` as an array literal (the same
        construct `ARRAY_CONSTRUCT(...)` builds) -- values still travel as
        bound parameters; only the arity becomes visible in the text."""
        groups = ["eu-field-analysts", "latam-partners"]
        sql, values = bind_policy_parameters(
            "SELECT * FROM t WHERE ARRAY_CONTAINS(CAST(region AS VARIANT), :user_groups)",
            {"user_groups": groups},
        )
        assert "[:1, :2]" in sql
        assert values == groups
        # The decisive assertion: no group NAME appears in the SQL text.
        for name in groups:
            assert name not in sql

    def test_empty_group_list_becomes_an_empty_array_literal(self):
        """A caller in no groups must match nothing -- a legitimate state,
        not an error. Unlike Databricks' bare `ARRAY()` (ambiguous
        `ARRAY<VOID>`), Snowflake's `[]` needs no type cast to fail closed
        against `ARRAY_CONTAINS`."""
        sql, values = bind_policy_parameters(
            "SELECT * FROM t WHERE ARRAY_CONTAINS(CAST(region AS VARIANT), :user_groups)", {"user_groups": []}
        )
        assert "ARRAY_CONTAINS(CAST(region AS VARIANT), [])" in sql
        assert values == []

    def test_marker_inside_a_string_literal_is_not_rewritten(self):
        """Why the substitution is AST-level and not a regex -- a policy
        body is exactly the kind of SQL that carries literals."""
        sql, _values = bind_policy_parameters(
            "SELECT * FROM t WHERE note = ':user_groups' AND ARRAY_CONTAINS(CAST(r AS VARIANT), :user_groups)",
            {"user_groups": ["x"]},
        )
        assert "note = ':user_groups'" in sql
        assert sql.count("[") == 1

    def test_no_list_survives_into_the_bound_values(self):
        _sql, values = bind_policy_parameters(
            "SELECT * FROM t WHERE ARRAY_CONTAINS(CAST(r AS VARIANT), :user_groups) AND o = :user_email",
            {"user_groups": ["a", "b"], "user_email": "x@y.z"},
        )
        assert all(not isinstance(v, (list, tuple, set)) for v in values)

    def test_missing_marker_denies_rather_than_dropping_the_filter(self):
        """If the marker is not where we are about to bind it, the group
        filter has silently vanished. Deny."""
        with pytest.raises(SnowflakePolicyBindingError):
            bind_policy_parameters("SELECT * FROM t", {"user_groups": ["a"]})


class TestNoOpShapes:
    def test_no_params_is_a_no_op(self):
        sql, values = bind_policy_parameters("SELECT * FROM t", {})
        assert sql == "SELECT * FROM t"
        assert values == []


class TestSqlglotCoupling:
    """Pin the two sqlglot behaviours group-based Snowflake policies rest
    on -- mirrors `tests/test_databricks_scan_and_policies.py::
    TestSqlglotCoupling` for the sibling engine (N4: `pyproject.toml` pins
    only a floor, so a minor bump can land at any time). Asserted in BOTH
    directions separately, not just through the end-to-end path, so a bump
    that breaks one fails with a message naming which.
    """

    def test_write_direction_dollar_becomes_colon(self):
        import sqlglot

        out = sqlglot.transpile(
            "SELECT * FROM t WHERE list_contains($user_groups, region)",
            read="duckdb",
            write="snowflake",
        )[0]
        assert ":user_groups" in out, f"sqlglot no longer writes $name as :name — got {out!r}"
        assert "$user_groups" not in out

    def test_read_direction_colon_parses_back_as_a_named_placeholder(self):
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(
            "SELECT * FROM t WHERE ARRAY_CONTAINS(CAST(region AS VARIANT), :user_groups)", dialect="snowflake"
        )
        placeholders = list(tree.find_all(exp.Placeholder))
        assert placeholders, "sqlglot no longer parses :name as a Placeholder in the snowflake dialect"
        assert [p.name for p in placeholders] == ["user_groups"]

    def test_the_round_trip_holds_end_to_end(self):
        from src.access_policy import _transpile_policy_to_snowflake

        body = _transpile_policy_to_snowflake("SELECT * FROM t WHERE list_contains($user_groups, region)", table_id="t")
        sql, values = bind_policy_parameters(body, {"user_groups": ["eu-team"]})
        assert "[:1]" in sql
        assert values == ["eu-team"]

    def test_a_broken_round_trip_denies_rather_than_dropping_the_filter(self):
        with pytest.raises(SnowflakePolicyBindingError):
            bind_policy_parameters("SELECT * FROM t WHERE 1 = 1", {"user_groups": ["eu-team"]})
