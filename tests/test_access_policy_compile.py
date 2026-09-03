"""Unit tests for the no-SQL builder's structured-spec -> SQL compiler.

The compiler is the safety-critical core of the access-policy builder: it must
never emit the two-column plaintext leak (e.g. ``SELECT *, md5(col) AS col``),
must never produce ``SELECT *`` (so a newly-added source column cannot leak),
and must keep the original column type for allowed ``unmask`` callers while
returning a safe fallback for everyone else.
"""

import pytest

from src.access_policy_compile import compile_policy

COLS = [
    {"name": "invoice_id", "type": "BIGINT"},
    {"name": "cost_center", "type": "VARCHAR"},
    {"name": "email", "type": "VARCHAR"},
    {"name": "national_id", "type": "VARCHAR"},
    {"name": "amount_eur", "type": "DOUBLE"},
]


def _projected(sql: str) -> str:
    """Return the projection clause between SELECT and FROM."""
    return sql.split(" FROM ")[0].replace("SELECT ", "")


def test_hash_mask_uses_explicit_projection_and_single_alias():
    spec = {
        "table": "invoices",
        "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
        "row_combine": "and",
        "column_masks": {"national_id": "hide", "email": "hash"},
    }
    out = compile_policy(spec, COLS)
    # Hidden and re-derived columns are still tracked.
    assert set(out.excluded) == {"national_id", "email"}
    assert out.derived == ["email"]
    # No star projection: a newly-added source column cannot leak by default.
    assert "SELECT *" not in out.sql
    assert "EXCLUDE" not in out.sql
    # The hidden column is omitted entirely.
    assert '"national_id"' not in _projected(out.sql)
    # Exactly one output column named email, and it is the hash.
    assert out.sql.count('AS "email"') == 1
    assert 'md5("email") AS "email"' in out.sql
    # The row rule uses the transpile-safe group-membership idiom, not IN.
    assert 'list_contains($user_groups, "cost_center")' in out.sql


def test_show_only_is_explicit_projection_and_warns():
    spec = {"table": "invoices", "row_rules": [], "row_combine": "and", "column_masks": {}}
    out = compile_policy(spec, COLS)
    assert out.sql == ('SELECT "invoice_id", "cost_center", "email", "national_id", "amount_eur" FROM "invoices"')
    assert out.excluded == []
    # a no-op policy is flagged, not silently accepted
    assert any("full table" in w for w in out.warnings)


def test_nullify_and_unmask_masks():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "amount_eur": "nullify",
            "email": {"choice": "unmask", "group": "Finance"},
        },
    }
    out = compile_policy(spec, COLS)
    # nullify preserves the numeric type
    assert 'CAST(NULL AS DOUBLE) AS "amount_eur"' in out.sql
    # unmask for text callers keeps the original type: the THEN branch is the
    # raw column; the ELSE branch is the fixed redaction string.
    assert "CASE WHEN list_contains($user_groups, 'Finance') THEN \"email\" ELSE '*****' END AS \"email\"" in out.sql
    assert set(out.excluded) == {"amount_eur", "email"}


def test_unmask_for_non_text_uses_null_with_type_cast():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "amount_eur": {"choice": "unmask", "groups": ["Finance"]},
        },
    }
    out = compile_policy(spec, COLS)
    assert (
        'CASE WHEN list_contains($user_groups, \'Finance\') THEN "amount_eur" ELSE CAST(NULL AS DOUBLE) END AS "amount_eur"'
        in out.sql
    )


def test_unmask_with_multiple_groups_uses_or():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "email": {"choice": "unmask", "groups": ["Finance", "Legal"]},
        },
    }
    out = compile_policy(spec, COLS)
    assert (
        "CASE WHEN list_contains($user_groups, 'Finance') OR list_contains($user_groups, 'Legal') THEN \"email\""
        in out.sql
    )
    assert " ELSE '*****' END AS \"email\"" in out.sql


def test_unmask_empty_allowlist_always_masks():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "email": {"choice": "unmask", "groups": []},
        },
    }
    out = compile_policy(spec, COLS)
    assert 'CASE WHEN FALSE THEN "email" ELSE \'*****\' END AS "email"' in out.sql


def test_unknown_mask_columns_are_dropped_with_a_warning():
    """A mask on a column the table no longer has is fail-CLOSED, so dropping
    it with a warning is safe: the projection is assembled from the DESCRIBEd
    column list only, so a column the mask names but the table does not have
    is never projected in the first place -- there is no plaintext copy left
    behind for the dropped mask to have covered."""
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {"ghost": "hide"},
    }
    out = compile_policy(spec, COLS)
    assert "ghost" not in out.sql
    assert any("ghost" in w for w in out.warnings)


def test_unknown_row_rule_column_is_refused_not_dropped():
    """A row rule on an unknown column is fail-OPEN if dropped: a spec whose
    only rule references a renamed column would compile to a WHERE-less
    policy that hands every caller the whole table. Refuse the compile
    instead, naming the column and the op so the admin can fix the rule."""
    spec = {
        "table": "invoices",
        "row_rules": [{"column": "does_not_exist", "op": "in_caller_groups"}],
        "row_combine": "and",
        "column_masks": {},
    }
    with pytest.raises(ValueError) as exc:
        compile_policy(spec, COLS)
    assert "does_not_exist" in str(exc.value)
    assert "in_caller_groups" in str(exc.value)


def test_unknown_row_rule_column_is_refused_even_beside_a_valid_rule():
    """The whole-table failure mode needs only ONE surviving rule to hide it:
    a dropped rule beside a kept one silently WIDENS the policy instead of
    emptying the WHERE clause, which is harder to notice, not easier."""
    spec = {
        "table": "invoices",
        "row_rules": [
            {"column": "cost_center", "op": "in_caller_groups"},
            {"column": "renamed_away", "op": "eq_caller_email"},
        ],
        "row_combine": "and",
        "column_masks": {},
    }
    with pytest.raises(ValueError, match="renamed_away"):
        compile_policy(spec, COLS)


def test_eq_and_in_row_ops_use_literals():
    spec = {
        "table": "invoices",
        "row_rules": [
            {"column": "cost_center", "op": "eq", "value": "FIN-EU"},
            {"column": "amount_eur", "op": "in", "value": [10, 20]},
        ],
        "row_combine": "or",
        "column_masks": {},
    }
    out = compile_policy(spec, COLS)
    assert "\"cost_center\" = 'FIN-EU'" in out.sql
    assert '"amount_eur" IN (10, 20)' in out.sql
    assert " OR " in out.sql


def test_self_owned_rows_bind_the_identity_placeholder():
    spec = {
        "table": "invoices",
        "row_rules": [{"column": "email", "op": "eq_caller_email"}],
        "row_combine": "and",
        "column_masks": {},
    }
    out = compile_policy(spec, COLS)
    assert '"email" = $user_email' in out.sql


def test_compiled_sql_passes_the_real_validator():
    from src.access_policy_validate import validate_policy_sql

    cols = [
        {"name": "invoice_id", "type": "BIGINT"},
        {"name": "cost_center", "type": "VARCHAR"},
        {"name": "email", "type": "VARCHAR"},
        {"name": "national_id", "type": "VARCHAR"},
        {"name": "amount_eur", "type": "DOUBLE"},
        {"name": "unit_price", "type": "DECIMAL(18,2)"},
        {"name": "tags", "type": "VARCHAR[]"},
        {"name": "nested", "type": "STRUCT(v VARCHAR, i BIGINT)"},
    ]
    spec = {
        "table": "invoices",
        "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
        "row_combine": "and",
        "column_masks": {
            "email": {"choice": "unmask", "groups": ["Finance", "Legal"]},
            "national_id": "hide",
            "amount_eur": "nullify",
            "unit_price": "nullify",
            "tags": {"choice": "unmask", "groups": ["Finance"]},
            "nested": {"choice": "unmask", "groups": ["Finance"]},
        },
    }
    out = compile_policy(spec, cols)
    # The builder's own output must never be rejected by the gate every save runs,
    # including the remote transpile path.
    validate_policy_sql(
        out.sql,
        table_id="invoices",
        table_name="invoices",
        mapping_table_names=set(),
        for_remote=False,
    )
    validate_policy_sql(
        out.sql,
        table_id="invoices",
        table_name="invoices",
        mapping_table_names=set(),
        for_remote=True,
    )


def test_explicit_projection_is_fixed_and_omits_hidden_columns():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {"national_id": "hide"},
    }
    out = compile_policy(spec, COLS)
    assert "SELECT *" not in out.sql
    assert '"national_id"' not in out.sql
    # remaining columns appear in the input order
    expected = '"invoice_id", "cost_center", "email", "amount_eur"'
    assert expected in out.sql


def test_backwards_compatible_string_columns_default_to_text():
    """Until all callers pass typed descriptors, plain column names are accepted."""
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {"amount_eur": {"choice": "unmask", "group": "Finance"}},
    }
    out = compile_policy(spec, ["invoice_id", "cost_center", "email", "amount_eur"])
    # Without a known type we fall back to treating the column as text-like.
    assert "ELSE '*****'" in out.sql


def test_show_columns_are_not_duplicated():
    """Explicit 'show' choices must not produce duplicate output columns."""
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "email": "show",
            "national_id": "hide",
            "amount_eur": {"choice": "unmask", "groups": ["Finance"]},
        },
    }
    out = compile_policy(spec, COLS)
    # Each output column appears exactly once and in the original table order.
    assert out.sql.count('AS "email"') == 0  # show columns need no alias
    assert out.sql.count('AS "amount_eur"') == 1
    proj = _projected(out.sql)
    expected = (
        '"invoice_id", "cost_center", "email", '
        "CASE WHEN list_contains($user_groups, 'Finance') THEN \"amount_eur\" "
        'ELSE CAST(NULL AS DOUBLE) END AS "amount_eur"'
    )
    assert proj == expected
    assert '"national_id"' not in out.sql


def test_hiding_all_columns_fails_closed():
    """A policy that would project no columns must not fall back to SELECT *."""
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {c["name"]: "hide" for c in COLS},
    }
    with pytest.raises(ValueError, match="select no columns"):
        compile_policy(spec, COLS)


def test_masked_columns_preserve_input_order():
    """The projection order matches the table's column order, not the mask spec order."""
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        # Spec dict order is email, invoice_id, amount_eur.
        "column_masks": {
            "email": {"choice": "unmask", "groups": ["Legal"]},
            "invoice_id": "hash",
            "amount_eur": "nullify",
        },
    }
    out = compile_policy(spec, COLS)
    proj = _projected(out.sql)
    expected = (
        'md5("invoice_id") AS "invoice_id", "cost_center", '
        "CASE WHEN list_contains($user_groups, 'Legal') THEN \"email\" ELSE '*****' END AS \"email\", "
        '"national_id", CAST(NULL AS DOUBLE) AS "amount_eur"'
    )
    assert proj == expected


def test_composite_text_types_do_not_use_string_redaction():
    """Arrays/structs containing VARCHAR must keep the composite type, not '*****'."""
    cols = [
        {"name": "tags", "type": "VARCHAR[]"},
        {"name": "nested", "type": "STRUCT(v VARCHAR, i BIGINT)"},
        {"name": "amount_eur", "type": "VARCHAR"},
    ]
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {
            "tags": {"choice": "unmask", "groups": ["Finance"]},
            "nested": {"choice": "unmask", "groups": ["Finance"]},
            "amount_eur": {"choice": "unmask", "groups": ["Finance"]},
        },
    }
    out = compile_policy(spec, cols)
    # VARCHAR[] and STRUCT must fall back to a type-preserving NULL.
    assert "ELSE CAST(NULL AS VARCHAR[])" in out.sql
    assert "ELSE CAST(NULL AS STRUCT(v VARCHAR, i BIGINT))" in out.sql
    # Plain VARCHAR still gets the fixed redaction string.
    assert "ELSE '*****'" in out.sql


# ── The compiler's output for a composite column must survive the save gate ──

_STRUCT_COLS = [
    {"name": "invoice_id", "type": "BIGINT"},
    {"name": "cost_center", "type": "VARCHAR"},
    {"name": "tags", "type": "VARCHAR[]"},
    {"name": "attrs", "type": "MAP(VARCHAR, VARCHAR)"},
    {"name": "payer", "type": "STRUCT(name VARCHAR, id BIGINT)"},
]


def test_composite_column_masks_pass_the_real_validator():
    """`CAST(NULL AS <type>)` is the fallback for every non-text column, and for
    a STRUCT that type spells its fields as `ColumnDef` nodes — which the save
    gate's node allowlist rejected, so the builder handed the admin SQL the PUT
    then refused with `policy_disallowed_construct`. A fail-closed dead end for
    struct columns, and a regression for `nullify` on one (previously a bare
    `NULL AS col`, which carries no type node at all).

    Verified against the real validator rather than by inspecting the string:
    `MAP(...)` and `VARCHAR[]` never introduced the extra node, only STRUCT did,
    which is exactly the kind of distinction a string assertion misses.
    """
    from src.access_policy_validate import validate_policy_sql

    spec = {
        "table": "invoices",
        "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
        "row_combine": "and",
        "column_masks": {
            "tags": "hide",
            "attrs": "nullify",
            "payer": {"choice": "unmask", "groups": ["Finance"]},
        },
    }
    out = compile_policy(spec, _STRUCT_COLS)
    for for_remote in (False, True):
        validate_policy_sql(
            out.sql,
            table_id="invoices",
            table_name="invoices",
            mapping_table_names=set(),
            for_remote=for_remote,
        )


def test_a_column_definition_outside_a_type_is_still_refused():
    """The allowlist was widened for `ColumnDef` under a `DataType` only — a
    `ColumnDef` anywhere else is still an unrecognized construct, so the
    widening cannot be read as "DDL is allowed now"."""
    import sqlglot
    from sqlglot import exp

    from src.access_policy_validate import _is_inside_data_type

    tree = sqlglot.parse_one('SELECT CAST(NULL AS STRUCT(name VARCHAR)) AS payer FROM "invoices"', read="duckdb")
    defs = list(tree.find_all(exp.ColumnDef))
    assert defs, "the STRUCT type no longer parses into a ColumnDef — the allowlist note is stale"
    assert all(_is_inside_data_type(d) for d in defs)

    ddl = sqlglot.parse_one("CREATE TABLE t (a VARCHAR)", read="duckdb")
    stray = list(ddl.find_all(exp.ColumnDef))
    assert stray, "expected a ColumnDef in a CREATE TABLE"
    assert not any(_is_inside_data_type(d) for d in stray)


# ── Partial masks: `last4` and `email_partial` ─────────────────────────────
#
# Both are TEXT-ONLY, type-preserving (VARCHAR in -> VARCHAR out, same output
# column name) and expressed entirely within the save-time validator's existing
# function allowlist -- no widening of `_ALLOWED_FUNCTION_NAMES` was needed, and
# none is acceptable: every name added there widens what an admin's arbitrary
# SQL may do on every analyst request.

_LAST4_SQL = (
    'CASE WHEN "national_id" IS NULL THEN NULL '
    "WHEN LENGTH(\"national_id\") <= 4 THEN '****' "
    'ELSE CONCAT(\'****\', SUBSTRING("national_id", -4)) END AS "national_id"'
)

_EMAIL_PARTIAL_SQL = (
    'CASE WHEN "email" IS NULL THEN NULL '
    "WHEN \"email\" LIKE '_%@%' "
    "THEN CONCAT(SUBSTRING(\"email\", 1, 1), '*****', REGEXP_REPLACE(\"email\", '^[^@]*', '')) "
    "ELSE '*****' END AS \"email\""
)


def test_last4_mask_sql_snapshot():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {"national_id": "last4"},
    }
    out = compile_policy(spec, COLS)
    assert _LAST4_SQL in out.sql
    # The masked column is projected exactly once -- no plaintext sibling.
    assert out.sql.count('"national_id"') == _LAST4_SQL.count('"national_id"')
    assert out.excluded == ["national_id"]
    assert out.derived == ["national_id"]


def test_email_partial_mask_sql_snapshot():
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {"email": "email_partial"},
    }
    out = compile_policy(spec, COLS)
    assert _EMAIL_PARTIAL_SQL in out.sql
    assert out.sql.count('"email"') == _EMAIL_PARTIAL_SQL.count('"email"')
    assert out.excluded == ["email"]
    assert out.derived == ["email"]


@pytest.mark.parametrize("choice", ["last4", "email_partial"])
def test_partial_masks_refuse_non_text_columns(choice):
    """Both masks are string surgery. Applying one to a BIGINT/DOUBLE/STRUCT
    column would either change the output column's type (breaking the
    compiler's type-preservation invariant, which downstream `DESCRIBE`-based
    schema surfaces depend on) or silently CAST -- so it is refused at compile
    time, naming the column and its type, exactly like an unknown mask."""
    cols = COLS + [{"name": "tags", "type": "VARCHAR[]"}]
    for col in ("invoice_id", "amount_eur", "tags"):
        spec = {"table": "invoices", "row_rules": [], "row_combine": "and", "column_masks": {col: choice}}
        with pytest.raises(ValueError) as exc:
            compile_policy(spec, cols)
        assert col in str(exc.value)
        assert choice in str(exc.value)


@pytest.mark.parametrize("choice", ["last4", "email_partial"])
def test_partial_masks_pass_the_real_validator_including_remote(choice):
    from src.access_policy_validate import validate_policy_sql

    spec = {
        "table": "invoices",
        "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
        "row_combine": "and",
        "column_masks": {"email": choice, "national_id": choice},
    }
    out = compile_policy(spec, COLS)
    for for_remote in (False, True):
        validate_policy_sql(
            out.sql,
            table_id="invoices",
            table_name="invoices",
            mapping_table_names=set(),
            for_remote=for_remote,
        )


def _run(sql: str, values: list):
    """Execute a compiled policy body over an in-memory single-column table."""
    import duckdb

    conn = duckdb.connect()
    try:
        conn.execute('CREATE TABLE "invoices" ("email" VARCHAR, "national_id" VARCHAR)')
        conn.executemany('INSERT INTO "invoices" VALUES (?, ?)', [(v, v) for v in values])
        return [r[0] for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def test_last4_executes_on_duckdb_with_the_documented_edge_cases():
    """`****1234` for a long value; a value of four characters or fewer is
    fully redacted rather than shown whole; NULL stays NULL (a CONCAT-only
    form would turn it into `****`, since DuckDB's CONCAT ignores NULLs)."""
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {"national_id": "last4", "email": "hide"},
    }
    out = compile_policy(spec, [{"name": "email", "type": "VARCHAR"}, {"name": "national_id", "type": "VARCHAR"}])
    values = ["123456789", "abcde", "abcd", "abc", "", None]
    assert _run(out.sql, values) == ["****6789", "****bcde", "****", "****", "****", None]


def test_email_partial_executes_on_duckdb_with_the_documented_edge_cases():
    """First character, a FIXED five-asterisk run (a run that tracked the local
    part's length would leak that length), then the domain verbatim. Anything
    without a local part AND an `@` is fully redacted -- never partially."""
    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {"email": "email_partial", "national_id": "hide"},
    }
    out = compile_policy(spec, [{"name": "email", "type": "VARCHAR"}, {"name": "national_id", "type": "VARCHAR"}])
    values = ["john.doe@example.com", "a@b.co", "no-at-sign", "@example.com", "", None]
    assert _run(out.sql, values) == [
        "j*****@example.com",
        "a*****@b.co",
        "*****",
        "*****",
        "*****",
        None,
    ]


def test_partial_masks_transpile_to_both_remote_engines():
    """Tripwire on the ACTUAL remote form, not just "it transpiles".

    Both masks were chosen for expressions whose semantics are identical on
    all three engines: a negative `SUBSTRING` start counts from the end on
    DuckDB, BigQuery and Databricks alike, and the `REGEXP_REPLACE` pattern
    carries no capture group, so none of the three engines' incompatible
    backreference spellings (`\\1` / `\\\\1` / `$1`) or `REGEXP_EXTRACT`
    group-index conventions can be reached. If a sqlglot upgrade starts
    emitting a different shape, this fails loudly instead of silently
    changing what a remote caller sees.
    """
    import sqlglot

    spec = {
        "table": "invoices",
        "row_rules": [],
        "row_combine": "and",
        "column_masks": {"email": "email_partial", "national_id": "last4"},
    }
    out = compile_policy(spec, [{"name": "email", "type": "VARCHAR"}, {"name": "national_id", "type": "VARCHAR"}])

    bq = sqlglot.transpile(out.sql, read="duckdb", write="bigquery")[0]
    assert bq == (
        "SELECT CASE WHEN `email` IS NULL THEN NULL WHEN `email` LIKE '_%@%' "
        "THEN CONCAT(COALESCE(SUBSTRING(`email`, 1, 1), ''), '*****', "
        "COALESCE(REGEXP_REPLACE(`email`, '^[^@]*', ''), '')) ELSE '*****' END AS `email`, "
        "CASE WHEN `national_id` IS NULL THEN NULL WHEN LENGTH(`national_id`) <= 4 THEN '****' "
        "ELSE CONCAT('****', COALESCE(SUBSTRING(`national_id`, -4), '')) END AS `national_id` "
        "FROM `invoices`"
    )

    dbx = sqlglot.transpile(out.sql, read="duckdb", write="databricks")[0]
    assert dbx == (
        "SELECT CASE WHEN `email` IS NULL THEN NULL WHEN `email` LIKE '_%@%' "
        "THEN CONCAT(COALESCE(SUBSTRING(`email`, 1, 1), ''), '*****', "
        "COALESCE(REGEXP_REPLACE(`email`, '^[^@]*', ''), '')) ELSE '*****' END AS `email`, "
        "CASE WHEN `national_id` IS NULL THEN NULL WHEN LENGTH(`national_id`) <= 4 THEN '****' "
        "ELSE CONCAT('****', COALESCE(SUBSTRING(`national_id`, -4), '')) END AS `national_id` "
        "FROM `invoices`"
    )
    # Neither remote form may reach for a backreference or a group index.
    for form in (bq, dbx):
        assert "REGEXP_EXTRACT" not in form.upper()
        assert "\\1" not in form and "$1" not in form
