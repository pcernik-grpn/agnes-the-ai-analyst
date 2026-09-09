"""``src.query_error_hints`` — mapping a raw DuckDB binder/catalog error
into a message that names the relation involved and points at the next
command that resolves it.

Production finding (2026-09-09): the `query` tool is the most-used and
most error-prone surface on a live instance — 27 failures in one day,
almost all a guessed column name. Two DuckDB error shapes reach the
caller as-is today:

- ``Table "s" does not have a column named "X"`` (DuckDB also phrases
  this ``Values list "s" does not have a column named "X"`` for some
  query shapes) — names only the ALIAS, never the underlying table, so
  neither a human nor an agent can tell which relation is meant.
- ``Referenced column "X" not found in FROM clause!`` — names no
  relation at all.

Neither tells the caller to run ``schema <table>`` before retrying.
These tests pin the hint text `column_not_found_hint`/
`unregistered_table_hint` produce for each shape — the ORIGINAL DuckDB
text is never part of the return value; callers append the hint to it
(see `app/api/query.py`), so a test asserting these functions duplicate
the raw error would be testing the wrong contract.
"""

from __future__ import annotations

from src.query_error_hints import column_not_found_hint, unregistered_table_hint


# ---------------------------------------------------------------------------
# column_not_found_hint — alias/table-qualified column miss
# ---------------------------------------------------------------------------


class TestColumnNotFoundViaAlias:
    def test_resolves_the_alias_to_its_table_and_names_both(self):
        error = (
            'Binder Error: Table "s" does not have a column named "CUSTOMER_SUBTYPE"\n\n'
            'Candidate bindings: : "subtype_name"'
        )
        sql = (
            "SELECT s.CUSTOMER_SUBTYPE AS subtype, COUNT(*) AS current_customers\n"
            "FROM customers c\n"
            "LEFT JOIN ref_customer_subtype s ON c.subtype_id = s.id\n"
            "GROUP BY 1"
        )
        hint = column_not_found_hint(error, sql)
        assert hint is not None
        # Names the real table, not just the alias DuckDB gave.
        assert "ref_customer_subtype" in hint
        assert "schema ref_customer_subtype" in hint
        assert "CUSTOMER_SUBTYPE" in hint
        # Never repeats/replaces the original DuckDB text -- that's the
        # caller's job, so the hint alone must not look like the whole
        # error message.
        assert "Binder Error" not in hint

    def test_also_matches_the_values_list_wording(self):
        """Production quoted DuckDB phrasing this shape as `Values list
        "alias" does not have a column named "X"` for some query shapes
        (not reproduced by a plain JOIN locally, but real on a live
        instance) -- both wordings must resolve the same way."""
        error = 'Binder Error: Values list "s" does not have a column named "CUSTOMER_SUBTYPE"'
        sql = "SELECT s.CUSTOMER_SUBTYPE FROM customers c LEFT JOIN ref_customer_subtype s ON c.id = s.id"
        hint = column_not_found_hint(error, sql)
        assert hint is not None
        assert "ref_customer_subtype" in hint
        assert "schema ref_customer_subtype" in hint

    def test_unaliased_reference_names_the_table_itself(self):
        """`tbl.col` with no alias at all -- DuckDB still calls the
        relation by its own (bare) name in the error, and that name IS
        already the table id, so the hint must not claim it's an alias
        for something else."""
        error = 'Binder Error: Table "customers" does not have a column named "FOO"'
        sql = "SELECT customers.FOO FROM customers"
        hint = column_not_found_hint(error, sql)
        assert hint is not None
        assert "customers" in hint
        assert "schema customers" in hint
        assert "aliased" not in hint

    def test_falls_back_to_listing_every_table_when_the_alias_cannot_be_resolved(self):
        """The best-effort FROM/JOIN scan is not a full parser -- when it
        can't find the alias in `sql` at all, the hint must not suggest
        running `schema <alias>` (the alias is not a real table id)."""
        error = 'Binder Error: Table "zzz" does not have a column named "FOO"'
        sql = "SELECT zzz.FOO FROM customers c JOIN orders o ON c.id = o.customer_id"
        hint = column_not_found_hint(error, sql)
        assert hint is not None
        assert "customers" in hint
        assert "orders" in hint

    def test_returns_none_for_an_unrelated_error(self):
        assert column_not_found_hint("Parser Error: syntax error at or near \"selct\"", "selct 1") is None


class TestColumnNotFoundInFromClause:
    def test_single_table_names_it_directly(self):
        error = (
            'Binder Error: Referenced column "PROJECT_START_DATE" not found in FROM clause!\n'
            'Candidate bindings: "PROJECT_STATUS_ID", "PROJECT_ID", "PROJECT_NAME", '
            '"LATEST_COMMENT_DATE", "SOURCE"'
        )
        sql = "SELECT PROJECT_START_DATE FROM projects"
        hint = column_not_found_hint(error, sql)
        assert hint is not None
        assert "projects" in hint
        assert "schema projects" in hint
        assert "PROJECT_START_DATE" in hint

    def test_multi_table_lists_every_candidate(self):
        error = 'Binder Error: Referenced column "PROJECT_START_DATE" not found in FROM clause!'
        sql = "SELECT PROJECT_START_DATE FROM projects p JOIN tasks t ON p.id = t.project_id"
        hint = column_not_found_hint(error, sql)
        assert hint is not None
        assert "projects" in hint
        assert "tasks" in hint

    def test_returns_none_for_an_unrelated_error(self):
        assert column_not_found_hint("Catalog Error: Table with name foo does not exist!", "SELECT 1 FROM foo") is None


# ---------------------------------------------------------------------------
# unregistered_table_hint — Catalog Error: table does not exist at all
# ---------------------------------------------------------------------------


class TestUnregisteredTableHint:
    def test_plain_typo_points_at_catalog(self):
        error = 'Catalog Error: Table with name totally_unknown_table does not exist!\nDid you mean "orders"?'
        hint = unregistered_table_hint(error)
        assert hint is not None
        assert "totally_unknown_table" in hint
        assert "catalog" in hint
        assert "source-system" not in hint

    def test_source_system_bucket_path_says_so(self):
        """The real 2026-09-09 finding: an agent used the Keboola bucket
        path from the semantic layer (`in.c-MODEL_02.BI_FINANCE_REVENUE`)
        as if it were a registered table id. DuckDB's unrelated "Did you
        mean" suggestion doesn't help -- the hint must name what actually
        went wrong."""
        error = (
            "Catalog Error: Table with name in.c-MODEL_02.BI_FINANCE_REVENUE does not exist!\n"
            'Did you mean "bi_employee_rates"?'
        )
        hint = unregistered_table_hint(error)
        assert hint is not None
        assert "in.c-MODEL_02.BI_FINANCE_REVENUE" in hint
        assert "source-system" in hint
        assert "catalog" in hint

    def test_returns_none_for_an_unrelated_error(self):
        assert unregistered_table_hint('Binder Error: Referenced column "X" not found in FROM clause!') is None
