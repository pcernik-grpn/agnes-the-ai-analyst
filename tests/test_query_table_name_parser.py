"""Table-name extraction for audit resource tagging (`_first_table_from_sql`).

The parsed name is not just an audit curiosity: it is the group-by key of the
query-telemetry top-tables ranking, so an identifier that is not a table shows
up in a user-facing dashboard as if it were real usage.

Each failure class below was observed in a production audit log before being
turned into a test.
"""

import pytest

from app.api.query import _first_table_from_sql


class TestPlainTableReferences:
    """The cases that already worked must keep working."""

    @pytest.mark.parametrize("sql,expected", [
        ("SELECT a FROM orders", "orders"),
        ("select a from orders", "orders"),
        ("SELECT a FROM orders WHERE x = 1", "orders"),
        ("SELECT a FROM my_schema.orders", "my_schema.orders"),
        ('SELECT a FROM "orders"', "orders"),
        ("SELECT a FROM `orders`", "orders"),
        ("SELECT a FROM orders o JOIN items i ON o.id = i.order_id", "orders"),
        ("SELECT a FROM orders\nWHERE x = 1", "orders"),
    ])
    def test_extracts_the_table(self, sql, expected):
        assert _first_table_from_sql(sql) == expected

    def test_join_when_no_from(self):
        assert _first_table_from_sql("UPDATE x JOIN orders ON 1=1") == "orders"

    def test_returns_none_without_a_table_reference(self):
        assert _first_table_from_sql("SELECT 1") is None

    def test_returns_none_for_empty_sql(self):
        assert _first_table_from_sql("") is None


class TestQualifiedPathsAreNotTruncated:
    """A dash in a fully-qualified path must not cut the identifier short.

    Observed: 105 queries in a 30-day window tagged `prj`, because the
    character class excluded `-` and a backticked BigQuery FQN terminated at
    the first dash.
    """

    def test_backticked_bq_fqn_yields_the_table_not_the_project(self):
        sql = "SELECT a FROM `my-project-123.analytics.orders` WHERE x = 1"
        assert _first_table_from_sql(sql) == "orders"

    def test_unquoted_dashed_project_is_not_truncated(self):
        sql = "SELECT a FROM my-project-123.analytics.orders"
        assert _first_table_from_sql(sql) == "orders"

    def test_mixed_quoting_path_yields_the_table(self):
        """Observed as `bq.` — the quote terminated the match immediately."""
        sql = 'SELECT a FROM bq."finance"."ledger" WHERE x = 1'
        assert _first_table_from_sql(sql) == "ledger"

    def test_two_part_qualified_name_is_preserved(self):
        """Schema-qualified names stay qualified — only the FQN head is dropped."""
        assert _first_table_from_sql("SELECT a FROM analytics.orders") == "analytics.orders"


class TestExtractDoesNotLeakColumnNames:
    """`EXTRACT(<part> FROM <column>)` puts a column right after FROM.

    Observed: 37 queries tagged with column names (`event_date`,
    `order_created_ts`, `operational_view_date`, ...) or with the cast
    functions `DATE` / `TIMESTAMP`.
    """

    def test_extract_does_not_shadow_the_real_table(self):
        sql = "SELECT EXTRACT(YEAR FROM event_date) AS yr FROM orders"
        assert _first_table_from_sql(sql) == "orders"

    def test_extract_over_a_qualified_column(self):
        sql = "SELECT EXTRACT(MONTH FROM ue.view_date) AS mo FROM revenue ue"
        assert _first_table_from_sql(sql) == "revenue"

    def test_extract_wrapping_a_cast_function(self):
        """Observed as `DATE` and `TIMESTAMP`."""
        sql = "SELECT EXTRACT(YEAR FROM DATE(created_at)) AS yr FROM orders"
        assert _first_table_from_sql(sql) == "orders"

    def test_substring_from_does_not_shadow_the_table(self):
        sql = "SELECT SUBSTRING(name FROM 2) AS n FROM customers"
        assert _first_table_from_sql(sql) == "customers"

    def test_trim_from_does_not_shadow_the_table(self):
        sql = "SELECT TRIM(LEADING '0' FROM code) AS c FROM products"
        assert _first_table_from_sql(sql) == "products"

    def test_extract_only_query_has_no_table(self):
        assert _first_table_from_sql("SELECT EXTRACT(YEAR FROM event_date)") is None


class TestTableValuedFunctionsAreNotTables:
    """`FROM UNNEST([...])` is an inline literal, not a table.

    Observed: 46 queries tagged `UNNEST`.
    """

    def test_unnest_is_skipped_in_favour_of_the_real_table(self):
        sql = (
            "WITH codes AS (SELECT c FROM UNNEST([STRUCT('A' AS c)])) "
            "SELECT * FROM orders JOIN codes USING (c)"
        )
        assert _first_table_from_sql(sql) == "orders"

    def test_unnest_only_query_has_no_table(self):
        sql = "SELECT c FROM UNNEST([STRUCT('A' AS c)])"
        assert _first_table_from_sql(sql) is None

    def test_generate_series_is_not_a_table(self):
        assert _first_table_from_sql("SELECT * FROM generate_series(1, 10)") is None


class TestOutputContract:
    """Callers write the result into `resource=table:<id>`, capped at 256 chars."""

    def test_result_is_length_capped(self):
        long_name = "a" * 500
        out = _first_table_from_sql(f"SELECT 1 FROM {long_name}")
        assert out is not None
        assert len(out) <= 200

    def test_no_surrounding_quotes_survive(self):
        for sql in ['SELECT a FROM "orders"', "SELECT a FROM `orders`"]:
            assert _first_table_from_sql(sql) == "orders"
