from src.semantic.dialect import resolve_expression, resolve_expression_any


def _expr(*pairs):
    return {"dialects": [{"dialect": d, "expression": e} for d, e in pairs]}


def test_duckdb_dialect_wins_when_present():
    sql, reason = resolve_expression(_expr(("ANSI_SQL", "SUM(a)"), ("DUCKDB", "sum(a)")))
    assert (sql, reason) == ("sum(a)", None)


def test_ansi_sql_is_the_fallback():
    sql, reason = resolve_expression(_expr(("ANSI_SQL", "SUM(a)"), ("SNOWFLAKE", "SUM(a)")))
    assert (sql, reason) == ("SUM(a)", None)


def test_warehouse_only_expression_is_unusable_not_spliced():
    sql, reason = resolve_expression(_expr(("SNOWFLAKE", "TRY_CAST(a AS NUMBER)")))
    assert sql is None
    assert "SNOWFLAKE" in reason


def test_empty_expression_is_unusable():
    sql, reason = resolve_expression({"dialects": []})
    assert sql is None
    assert reason


def test_dialect_entry_without_a_name_is_ignored_not_a_crash():
    sql, reason = resolve_expression({"dialects": [{"dialect": None, "expression": "SUM(a)"}]})
    assert sql is None
    assert reason


# ---------------------------------------------------------------------------
# resolve_expression_any: falls back to a warehouse-specific dialect instead
# of reporting "unusable", so a caller (the projector) can still store the
# raw expression rather than dropping the metric entirely.
# ---------------------------------------------------------------------------


def test_any_prefers_duckdb_over_a_warehouse_dialect():
    sql, dialect, locally_runnable = resolve_expression_any(
        _expr(("SNOWFLAKE", "TRY_CAST(a AS NUMBER)"), ("DUCKDB", "CAST(a AS DOUBLE)"))
    )
    assert (sql, dialect, locally_runnable) == ("CAST(a AS DOUBLE)", "DUCKDB", True)


def test_any_prefers_ansi_sql_over_a_warehouse_dialect():
    sql, dialect, locally_runnable = resolve_expression_any(_expr(("SNOWFLAKE", "SUM(a)"), ("ANSI_SQL", "SUM(a)")))
    assert (sql, dialect, locally_runnable) == ("SUM(a)", "ANSI_SQL", True)


def test_any_falls_back_to_the_only_offered_warehouse_dialect():
    sql, dialect, locally_runnable = resolve_expression_any(_expr(("SNOWFLAKE", "TRY_CAST(a AS NUMBER)")))
    assert (sql, dialect, locally_runnable) == ("TRY_CAST(a AS NUMBER)", "SNOWFLAKE", False)


def test_any_falls_back_to_a_databricks_only_dialect():
    sql, dialect, locally_runnable = resolve_expression_any(_expr(("DATABRICKS", "SUM(a)")))
    assert (sql, dialect, locally_runnable) == ("SUM(a)", "DATABRICKS", False)


def test_any_with_no_expression_at_all_returns_nothing():
    sql, dialect, locally_runnable = resolve_expression_any({"dialects": []})
    assert (sql, dialect, locally_runnable) == (None, None, False)


def test_any_dialect_entry_without_a_name_is_ignored_not_a_crash():
    sql, dialect, locally_runnable = resolve_expression_any({"dialects": [{"dialect": None, "expression": "SUM(a)"}]})
    assert (sql, dialect, locally_runnable) == (None, None, False)
