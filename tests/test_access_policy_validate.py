"""Save-time validator for access-policy SQL (design doc §14, §5.2, §6.1, §6.3).

One test per rejection reason, plus the happy-path policy from the design
doc's §1 (which must pass with ``for_remote=True``), plus a check that the
`$user_groups`-via-``unnest`` idiom (§6.5) warns rather than rejects.
"""

import logging

import pytest

from src.access_policy_validate import PolicyValidationError, validate_policy_sql

TABLE_ID = "invoices"
TABLE_NAME = "invoices"

HAPPY_PATH_SQL = (
    "SELECT * EXCLUDE (national_id), md5(email) AS email FROM invoices WHERE list_contains($user_groups, cost_center)"
)


def _validate(sql, *, mapping_table_names=frozenset(), for_remote=False):
    return validate_policy_sql(
        sql,
        table_id=TABLE_ID,
        table_name=TABLE_NAME,
        mapping_table_names=set(mapping_table_names),
        for_remote=for_remote,
    )


class TestHappyPath:
    def test_accepts_row_and_column_policy(self):
        """The design doc's own §1 example: row filter + column mask,
        valid for a remote (BigQuery) table too."""
        assert _validate(HAPPY_PATH_SQL, for_remote=True) is None

    def test_accepts_mapping_table_join(self):
        """§15's mapping-table pattern: a subquery against a table marked
        policy_mapping=true, keyed on $user_email."""
        sql = (
            "SELECT * FROM invoices WHERE cost_center IN "
            "(SELECT cost_center FROM user_access WHERE email = $user_email)"
        )
        assert _validate(sql, mapping_table_names={"user_access"}) is None


class TestRuleOneSingleSelect:
    """Rule 1: parses as exactly one statement, and it is a SELECT."""

    def test_rejects_multiple_statements(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("SELECT 1 FROM invoices; SELECT 2 FROM invoices;")
        assert e.value.reason == "policy_not_single_select"

    def test_rejects_unparseable_sql(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("SELECT * FROM")
        assert e.value.reason == "policy_not_single_select"

    def test_rejects_non_select_statement_not_otherwise_named(self):
        """A statement that is neither SELECT nor one of rule 2's explicitly
        named forbidden types (e.g. SET) still falls through to the generic
        "must be a SELECT" reason."""
        with pytest.raises(PolicyValidationError) as e:
            _validate("SET memory_limit='1GB'")
        assert e.value.reason == "policy_not_single_select"


class TestRuleTwoForbiddenStatement:
    """Rule 2: no DDL/DML nodes, no ATTACH/DETACH/INSTALL/LOAD/PRAGMA/CALL."""

    def test_rejects_drop_table(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("DROP TABLE invoices")
        assert e.value.reason == "policy_forbidden_statement"

    def test_rejects_insert(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("INSERT INTO invoices VALUES (1)")
        assert e.value.reason == "policy_forbidden_statement"

    def test_rejects_attach(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("ATTACH 'evil.db'")
        assert e.value.reason == "policy_forbidden_statement"

    def test_rejects_pragma(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("PRAGMA database_list")
        assert e.value.reason == "policy_forbidden_statement"


class TestRuleThreeDisallowedConstruct:
    """Rule 3: node-type and function-name allowlist."""

    def test_rejects_aggregate_function(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("SELECT SUM(amount) FROM invoices")
        assert e.value.reason == "policy_disallowed_construct"

    def test_rejects_sql_as_string_table_function(self):
        """Reuses #1264's guard -- a policy body must not be able to reach
        this bypass independently of it staying complete on /api/query."""
        with pytest.raises(PolicyValidationError) as e:
            _validate("SELECT * FROM query('SELECT * FROM invoices')")
        assert e.value.reason == "policy_sql_string_function"


class TestRuleFourTableReferences:
    """Rule 4: every table reference is the policy's own table or a
    table marked policy_mapping=true."""

    def test_rejects_reference_to_unlisted_table(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("SELECT * FROM invoices JOIN secret ON secret.id = invoices.id")
        assert e.value.reason == "policy_unlisted_table_reference"
        assert "secret" in e.value.detail

    def test_rejects_unmarked_mapping_table(self):
        """The same join is rejected when the referenced table exists but
        was never marked policy_mapping=true (§15) -- marking is required,
        not merely the table being registered."""
        with pytest.raises(PolicyValidationError) as e:
            _validate(
                "SELECT * FROM invoices WHERE cost_center IN "
                "(SELECT cost_center FROM user_access WHERE email = $user_email)",
                mapping_table_names=set(),
            )
        assert e.value.reason == "policy_unlisted_table_reference"
        assert "user_access" in e.value.detail


class TestRuleFiveVariables:
    """Rule 5: $vars are known, value-position only, never a pattern."""

    def test_rejects_identity_var_in_like_position(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("SELECT * FROM invoices WHERE owner LIKE $user_email")
        assert e.value.reason == "policy_var_in_pattern_position"

    def test_rejects_identity_var_in_ilike_position(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("SELECT * FROM invoices WHERE owner ILIKE $user_email")
        assert e.value.reason == "policy_var_in_pattern_position"

    def test_rejects_identity_var_in_regex_function_position(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("SELECT * FROM invoices WHERE regexp_matches(owner, $user_email)")
        assert e.value.reason == "policy_var_in_pattern_position"

    def test_rejects_var_in_exclude_identifier_position(self):
        """§6.2: `EXCLUDE ($col)` -- a variable may only stand where a value
        stands; naming a column dynamically changes the query's shape."""
        with pytest.raises(PolicyValidationError) as e:
            _validate("SELECT * EXCLUDE ($col) FROM invoices")
        assert e.value.reason == "policy_var_in_identifier_position"

    def test_rejects_var_in_table_alias_position(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("SELECT * FROM invoices AS $alias")
        assert e.value.reason == "policy_var_in_identifier_position"

    def test_rejects_unknown_variable(self):
        with pytest.raises(PolicyValidationError) as e:
            _validate("SELECT * FROM invoices WHERE cost_center = $not_a_real_variable")
        assert e.value.reason == "policy_unknown_variable"
        assert "not_a_real_variable" in e.value.detail


class TestRuleSixRemoteTranspile:
    """Rule 6: for a remote table, the policy must transpile to every remote
    engine's SQL (BigQuery, Databricks, Snowflake); the unnest-in-IN
    group-membership idiom warns rather than rejects."""

    def test_rejects_when_untranspilable(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise Exception("boom")

        import sqlglot

        monkeypatch.setattr(sqlglot, "transpile", _raise)
        with pytest.raises(PolicyValidationError) as e:
            _validate(HAPPY_PATH_SQL, for_remote=True)
        assert e.value.reason == "policy_untranspilable"

    def test_checks_snowflake_too(self):
        """S2 (RLS review, issue #1979): a policy that transpiles to
        BigQuery/Databricks but not Snowflake would save clean and then
        DENY at read time on a Snowflake table -- an outage shipped as an
        access rule, exactly the failure mode the Databricks-parity test
        (`tests/test_databricks_scan_and_policies.py::
        test_save_time_validation_covers_databricks_too`) pins for that
        engine. The save is the cheap place to find out."""
        import src.access_policy_validate as v

        # Sanity: the ordinary shape passes every engine.
        v._reject_untranspilable(HAPPY_PATH_SQL)

        calls = []
        original = v.sqlglot.transpile

        def fake(sql, read, write):
            calls.append(write)
            return original(sql, read=read, write=write)

        v.sqlglot.transpile = fake
        try:
            v._reject_untranspilable(HAPPY_PATH_SQL)
        finally:
            v.sqlglot.transpile = original
        assert "snowflake" in calls

    def test_ignores_transpile_check_when_not_remote(self, monkeypatch):
        """The same broken transpile() must not affect a local/server_only
        (non-remote) policy -- rule 6 is gated on for_remote."""

        def _raise(*args, **kwargs):
            raise Exception("boom")

        import sqlglot

        monkeypatch.setattr(sqlglot, "transpile", _raise)
        assert _validate(HAPPY_PATH_SQL, for_remote=False) is None

    def test_warns_but_does_not_reject_unnest_group_membership(self, caplog):
        sql = "SELECT * FROM invoices WHERE id IN (SELECT unnest($user_groups))"
        with caplog.at_level(logging.WARNING):
            result = _validate(sql, for_remote=True)
        assert result is None
        assert any("list_contains" in record.getMessage() for record in caplog.records)

    def test_list_contains_idiom_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = _validate(HAPPY_PATH_SQL, for_remote=True)
        assert result is None
        assert not any("list_contains" in record.getMessage() for record in caplog.records)


def test_exotic_duckdb_types_survive_the_cast_null_redaction_path():
    """`_masked_fallback` interpolates the DESCRIBE type verbatim into
    `CAST(NULL AS <type>)`, which raised the question of whether an exotic type
    — an inline `ENUM('a','b')` or a `UNION(...)` — would fail the validator and
    make such a column unmaskable through the builder.

    It does not, on either half: DuckDB binds both casts, and the positional
    `ColumnDef`/`DataTypeParam` rule accepts them because they appear under a
    `DataType`. Locked here so the question does not have to be re-litigated
    from the type list, and so a future narrowing of that rule fails loudly
    instead of quietly making a column unmaskable.

    (Reachability is a separate matter: a parquet-backed extract turns an ENUM
    into VARCHAR on write, so these types only arrive from an attached source.)
    """
    for type_sql in (
        "ENUM('ok', 'bad')",
        "UNION(a INTEGER, b VARCHAR)",
        "STRUCT(a INTEGER, b VARCHAR)",
        "MAP(VARCHAR, INTEGER)",
        "DECIMAL(18,2)",
        "VARCHAR[]",
    ):
        _validate(f"SELECT id, CAST(NULL AS {type_sql}) AS masked FROM {TABLE_NAME}")
