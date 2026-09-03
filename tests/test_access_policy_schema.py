"""``src.access_policy_schema.masked_output_columns`` -- the static AST walk
behind the ``masked`` marker (table access policies design doc §11).

Reuses the design doc's own canonical policy body wherever a fixture would
otherwise invent one, matching the convention every other access-policy test
module follows.
"""

from __future__ import annotations

from src.access_policy_schema import masked_output_columns


def test_bare_column_is_not_masked():
    assert masked_output_columns("SELECT id, cost_center, amount FROM invoices") == frozenset()


def test_bare_star_is_not_masked():
    assert masked_output_columns("SELECT * FROM invoices") == frozenset()


def test_star_exclude_is_not_masked():
    """A ``* EXCLUDE (...)`` column is HIDDEN (``effective_schema``'s own
    DESCRIBE diff), never masked -- this walk has nothing to say about it."""
    assert masked_output_columns("SELECT * EXCLUDE (national_id) FROM invoices") == frozenset()


def test_self_rename_is_not_masked():
    assert masked_output_columns("SELECT id AS id FROM invoices") == frozenset()


def test_qualified_column_pass_through_is_not_masked():
    assert masked_output_columns("SELECT invoices.email FROM invoices") == frozenset()


def test_the_canonical_aliased_hash_example_is_masked():
    """The design doc's own leading example -- ``md5(email) AS email``:
    same name, same VARCHAR type on both sides of a DESCRIBE diff, no
    signal there at all. This is the whole reason the marker is computed
    from the AST rather than a runtime diff."""
    sql = (
        "SELECT * EXCLUDE (national_id), md5(email) AS email "
        "FROM invoices WHERE list_contains($user_groups, cost_center)"
    )
    assert masked_output_columns(sql) == frozenset({"email"})


def test_case_redaction_is_masked():
    sql = (
        "SELECT id, CASE WHEN list_contains($user_groups, 'finance_admin') "
        "THEN email ELSE NULL END AS email FROM invoices"
    )
    assert masked_output_columns(sql) == frozenset({"email"})


def test_cast_null_is_masked():
    assert masked_output_columns("SELECT id, CAST(NULL AS VARCHAR) AS ssn FROM invoices") == frozenset({"ssn"})


def test_keyed_pseudonym_udf_is_masked():
    assert masked_output_columns("SELECT id, agnes_hmac(email) AS email FROM invoices") == frozenset({"email"})


def test_rename_to_a_different_name_is_masked():
    """Strictly a bare column reference, but not to ITSELF -- the output
    column named ``masked_email`` is not the same name as the base column
    it reads, so it does not qualify as the self pass-through the rule
    carves out."""
    assert masked_output_columns("SELECT email AS masked_email FROM invoices") == frozenset({"masked_email"})


def test_unparseable_sql_returns_empty_rather_than_raising():
    assert masked_output_columns("SELECT * FROM invoices SAMPLE 50%") == frozenset()


def test_non_select_top_level_returns_empty():
    assert masked_output_columns("INSERT INTO invoices VALUES (1)") == frozenset()


def test_quoted_and_case_variant_names_are_normalized():
    sql = 'SELECT id, agnes_hmac("Email") AS "Email" FROM invoices'
    assert masked_output_columns(sql) == frozenset({"email"})


def test_masked_names_are_case_insensitive_lookup():
    sql = "SELECT id, md5(Email) AS EMAIL FROM invoices"
    assert masked_output_columns(sql) == frozenset({"email"})


def test_multiple_masked_columns():
    sql = "SELECT * EXCLUDE (national_id, email, ssn), md5(email) AS email, CAST(NULL AS VARCHAR) AS ssn FROM invoices"
    assert masked_output_columns(sql) == frozenset({"email", "ssn"})
