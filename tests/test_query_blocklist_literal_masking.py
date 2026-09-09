"""#1513: /api/query's SELECT-only keyword blocklist (``_assert_select_only``)
scans RAW SQL text for ``_BLOCKED_SQL_TOKENS``, so a string literal that merely
CONTAINS an ordinary word like ``delete ``/``load `` or a URL is refused with
``400 Only single SELECT queries are allowed`` even though the query is a
plain single SELECT.

Companion issue #1394 (the non-admin catalog gate) shares the fix's masking
helper (``app.api.query._mask_sql_for_guard``) — those tests live in
``tests/test_api_query_rbac_cross_source_collision.py`` alongside the existing
catalog-gate fixture. This file covers #1513's guard plus the masking helper
itself, since both are exercised together (`_assert_select_only` is the
easier surface to drive the adversarial battery against, and both guards
must keep refusing everything they refuse today for REAL SQL, not just for
data sitting inside a literal).
"""

import pytest
from fastapi import HTTPException

from app.api.query import _assert_select_only


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# #1513 repro — string literals containing an ordinary word must pass.
# ---------------------------------------------------------------------------


class TestLiteralFalsePositivesFixed:
    def test_load_failed_literal_no_longer_blocked(self):
        # "load " (trailing space) is on _BLOCKED_SQL_TOKENS.
        _assert_select_only("select 'load failed' as x")

    def test_loadfailed_without_space_was_never_blocked(self):
        # Sanity: the un-spaced variant already passed before the fix too —
        # pins that the fix didn't change THIS half of the behavior.
        _assert_select_only("select 'loadfailed' as x")

    def test_delete_word_inside_literal_no_longer_blocked(self):
        _assert_select_only("select 'a delete b' as x")

    def test_https_url_inside_literal_no_longer_blocked(self):
        _assert_select_only("select 'https://x' as x")

    def test_semicolon_inside_literal_no_longer_blocked(self):
        # A `;` that is DATA (inside a real, terminated literal) can never be
        # read by DuckDB as a second statement, unlike a bare `;` in the SQL.
        _assert_select_only("select 'a;b' as x")

    def test_read_parquet_word_inside_literal_no_longer_blocked(self):
        _assert_select_only("select 'read_parquet' as x")

    def test_realistic_ilike_search_no_longer_blocked(self):
        _assert_select_only("select issue_key, summary from issues where summary ilike '%load failed%'")

    def test_realistic_url_search_no_longer_blocked(self):
        _assert_select_only("select issue_key from issues where summary ilike '%https://connection.keboola.com%'")

    def test_http_load_failed_literal_returns_200(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.post("/api/query", json={"sql": "SELECT 'load failed' AS x"}, headers=_auth(token))
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Attack battery — everything the guard refuses today must still be refused.
# ---------------------------------------------------------------------------


class TestStillRefusedAfterTheFix:
    def test_second_statement_after_select_still_blocked(self):
        with pytest.raises(HTTPException) as exc:
            _assert_select_only("select 1; drop table x")
        assert exc.value.status_code == 400

    def test_comment_hiding_a_second_statement_still_blocked(self):
        # The `'` inside the `--` comment must not desync the scanner into
        # reading the DELETE after the newline as still "inside a string".
        with pytest.raises(HTTPException):
            _assert_select_only("select 1 -- ' \n ; delete from y")

    def test_blocked_keyword_inside_block_comment_still_blocked(self):
        with pytest.raises(HTTPException):
            _assert_select_only("select 1 /* ; drop table x */ from t")

    def test_unterminated_string_literal_is_refused(self):
        # Decision (pinned): an unterminated literal is refused (fail
        # closed) rather than treated as "everything after the opening quote
        # is part of the string" — the latter would hide whatever real SQL
        # follows it from THIS guard, even though DuckDB itself refuses the
        # same malformed statement outright (a genuine `ParserException`).
        with pytest.raises(HTTPException) as exc:
            _assert_select_only("select 'abc")
        assert exc.value.status_code == 400

    def test_mixed_decoy_literal_and_real_keyword_real_one_still_caught(self):
        with pytest.raises(HTTPException):
            _assert_select_only("select 'load failed' as x; drop table y")

    def test_bigquery_query_function_call_still_blocked(self):
        # Regression: masking only removes the ARGUMENTS' content, never the
        # function-name text itself.
        with pytest.raises(HTTPException):
            _assert_select_only("select * from bigquery_query('proj', 'select 1')")

    def test_ungranted_reference_outside_any_literal_still_visible(self):
        # A literal that happens to contain "kbc." must not shield a REAL
        # catalog-qualified table reference elsewhere in the same query from
        # whatever downstream guard checks it (`_assert_select_only` itself
        # doesn't gate catalog access — this just pins that masking a
        # literal doesn't also eat unrelated real SQL around it).
        _assert_select_only("select * from kbc.main.my_view where note = 'kbc.other'")


# ---------------------------------------------------------------------------
# Direct coverage of the shared masking helper (`_mask_sql_for_guard`).
# ---------------------------------------------------------------------------


class TestMaskSqlForGuardHelper:
    def test_single_quote_literal_is_blanked_length_preserved(self):
        from app.api.query import _mask_sql_for_guard

        sql = "select 'abc' as x"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert len(masked) == len(sql)
        assert "abc" not in masked

    def test_doubled_quote_escape_handled(self):
        from app.api.query import _mask_sql_for_guard

        sql = "select 'it''s fine' as x"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert "it" not in masked and "fine" not in masked
        assert len(masked) == len(sql)

    def test_dollar_quoted_literal_is_blanked(self):
        from app.api.query import _mask_sql_for_guard

        sql = "select $$load failed$$ as x"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert "load" not in masked

    def test_dollar_quoted_literal_with_embedded_single_quote_does_not_desync(self):
        # DuckDB dollar-quoted bodies can contain an unescaped `'` — this must
        # not be read as opening a DIFFERENT (real) string literal that then
        # swallows the rest of the query.
        from app.api.query import _mask_sql_for_guard

        sql = "select $$it's a test; drop table x$$ as x from real_table"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert "drop" not in masked
        assert "real_table" in masked

    def test_tagged_dollar_quote(self):
        from app.api.query import _mask_sql_for_guard

        sql = "select $tag$delete this$tag$ as x"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert "delete" not in masked

    def test_digit_leading_tag_is_not_treated_as_dollar_quote(self):
        # DuckDB rejects `$1$...$1$` (a dollar-quote tag can't start with a
        # digit — verified against 1.5.2); the masker must agree, or it would
        # blank real SQL believing it's still "inside" a literal.
        from app.api.query import _mask_sql_for_guard

        sql = "select $1$ as x from real_table"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert "real_table" in masked

    def test_quoted_identifier_stays_visible(self):
        from app.api.query import _mask_sql_for_guard

        sql = 'select 1 as "keboola"."x"'
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert masked == sql

    def test_backtick_quoted_span_also_stays_visible_to_this_helper(self):
        # Backtick BQ-path masking is a SEPARATE, existing pass
        # (`_mask_backticks`) composed on top of this helper only in the
        # catalog-gate call site — this helper alone does not special-case
        # backticks.
        from app.api.query import _mask_sql_for_guard

        sql = "select * from `proj.ds.tbl`"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert masked == sql

    def test_nested_block_comment_masked_as_one_span(self):
        # DuckDB nests block comments (verified empirically against 1.5.2):
        # `/* a /* b */ c */` parses as ONE comment.
        from app.api.query import _mask_sql_for_guard

        sql = "select 1 /* outer /* inner */ still outer drop table x */ from t"
        masked = _mask_sql_for_guard(sql, mask_comments=True)
        assert "drop" not in masked
        assert "from t" in masked

    def test_line_comment_not_masked_when_mask_comments_false(self):
        from app.api.query import _mask_sql_for_guard

        sql = "select 1 -- drop table x\n"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert "drop table x" in masked

    def test_line_comment_masked_when_mask_comments_true(self):
        from app.api.query import _mask_sql_for_guard

        sql = "select 1 -- keboola.com\n as x"
        masked = _mask_sql_for_guard(sql, mask_comments=True)
        assert "keboola" not in masked

    def test_unterminated_string_literal_raises(self):
        from app.api.query import _mask_sql_for_guard

        with pytest.raises(HTTPException) as exc:
            _mask_sql_for_guard("select 'abc", mask_comments=False)
        assert exc.value.status_code == 400

    def test_unterminated_block_comment_raises(self):
        from app.api.query import _mask_sql_for_guard

        with pytest.raises(HTTPException):
            _mask_sql_for_guard("select 1 /* abc", mask_comments=True)

    def test_unterminated_quoted_identifier_raises(self):
        from app.api.query import _mask_sql_for_guard

        with pytest.raises(HTTPException):
            _mask_sql_for_guard('select 1 as "abc', mask_comments=False)

    def test_unterminated_dollar_quote_raises(self):
        from app.api.query import _mask_sql_for_guard

        with pytest.raises(HTTPException):
            _mask_sql_for_guard("select $$abc", mask_comments=False)

    def test_mask_sql_noise_stays_permissive_on_unterminated_input(self):
        # `_mask_sql_noise` (best-effort audit tagging, not a security guard)
        # keeps the old non-raising "mask to end of string" contract.
        from app.api.query import _mask_sql_noise

        result = _mask_sql_noise("select 'abc")
        assert isinstance(result, str)
        assert len(result) == len("select 'abc")


class TestEStringLiterals:
    """DuckDB E-strings (`E'...'` / `e'...'`) accept BACKSLASH escapes on top
    of the usual `''`. Every expectation below was verified against the engine
    itself before being pinned here:

        SELECT E'\\''                     -> "'"          (valid)
        SELECT E'a''b'                    -> "a'b"        (valid)
        SELECT E'\\'; DROP TABLE x --'    -> "'; DROP TABLE x --"  (ONE literal)
        SELECT E'a\\'                     -> Parser Error: unterminated

    A scanner blind to the `E` prefix reads the first as unterminated (400 on a
    valid query) and ends the third early (exposing text DuckDB treats as pure
    data). Both mismatches fail closed, but false positives are precisely what
    this masking exists to remove. Raised by Devin review on #1546.
    """

    def test_backslash_escaped_quote_is_not_read_as_unterminated(self):
        from app.api.query import _mask_sql_for_guard

        sql = r"SELECT E'\'' AS x"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert len(masked) == len(sql)
        assert masked.strip().startswith("SELECT")
        assert masked.rstrip().endswith("AS x")

    def test_doubled_quote_escape_also_works_inside_an_e_string(self):
        from app.api.query import _mask_sql_for_guard

        sql = "SELECT E'a''b' AS x"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert "a''b" not in masked
        assert len(masked) == len(sql)

    def test_e_string_body_is_masked_whole_matching_duckdb(self):
        # DuckDB parses this as a SINGLE string whose VALUE is
        # "'; DROP TABLE x --" — nothing is executed. Ending the literal
        # early would refuse a valid query.
        from app.api.query import _mask_sql_for_guard

        sql = r"SELECT E'\'; DROP TABLE x --' AS y"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert "DROP" not in masked
        assert len(masked) == len(sql)

    def test_unterminated_e_string_is_still_refused(self):
        # DuckDB itself rejects this (the \' escapes the quote, so the
        # literal never closes) — the guard must not guess otherwise.
        import pytest
        from fastapi import HTTPException

        from app.api.query import _mask_sql_for_guard

        with pytest.raises(HTTPException) as exc:
            _mask_sql_for_guard(r"SELECT E'a\'", mask_comments=False)
        assert exc.value.status_code == 400

    def test_trailing_e_of_an_identifier_does_not_start_an_e_string(self):
        from app.api.query import _mask_sql_for_guard

        sql = "SELECT case_e'x' AS y"
        masked = _mask_sql_for_guard(sql, mask_comments=False)
        assert "case_e" in masked, "the identifier must stay visible to the guards"
        assert "'x'" not in masked


# ---------------------------------------------------------------------------
# #2424 follow-up (production finding 2026-09-09): a caller who trips the
# blocklist used to get ONE generic "Only single SELECT queries are allowed"
# string regardless of which token matched -- false for every class below
# except the genuine multi-statement one. `_assert_select_only` now raises a
# message naming the REASON CLASS that matched, same standard the neighbouring
# file-path-table-source branch in the same function already meets.
# ---------------------------------------------------------------------------


class TestBlockedTokenClassMessages:
    def test_dml_ddl_keyword_names_its_class(self):
        with pytest.raises(HTTPException) as exc:
            _assert_select_only("drop table customers")
        detail = str(exc.value.detail)
        assert exc.value.status_code == 400
        assert "DDL" in detail or "data-modification" in detail
        # Must not claim the caller's real mistake is a different class.
        assert "catalog" not in detail.lower()
        assert "url" not in detail.lower()

    def test_file_access_function_names_its_class(self):
        # The real incident that motivated #160: a plain single SELECT
        # calling bigquery_query() used to 400 with "not a single SELECT",
        # which is false -- it IS one.
        with pytest.raises(HTTPException) as exc:
            _assert_select_only("select * from bigquery_query('proj', 'select 1')")
        detail = str(exc.value.detail)
        assert "read_parquet" in detail or "file" in detail.lower()
        assert "bigquery_query" in detail

    def test_url_scheme_alone_names_its_class(self):
        # A URL scheme literal INSIDE a real string value (not a table
        # source) is masked away before the blocklist scan and legitimately
        # passes -- see TestLiteralFalsePositivesFixed above. Comments are
        # scanned on purpose though (so `-- drop this` still gets caught,
        # see `_assert_select_only`'s docstring), so a scheme mentioned in a
        # comment is the one reachable way to trip ONLY this class with no
        # file-access token alongside it.
        with pytest.raises(HTTPException) as exc:
            _assert_select_only("select 1 as x -- fetch data from https://example.com/x\n from my_view")
        detail = str(exc.value.detail)
        assert "https://" in detail or "URL" in detail or "scheme" in detail.lower()

    def test_catalog_metadata_names_its_class_and_hints_schema_and_catalog(self):
        # The 2026-09-09 production finding, reproduced verbatim: a caller
        # ran a genuine single SELECT against information_schema and was
        # told "Only single SELECT queries are allowed" -- false, and no
        # next step. The class message must name the next step: `schema`
        # and `catalog`.
        with pytest.raises(HTTPException) as exc:
            _assert_select_only(
                "select column_name, data_type from information_schema.columns "
                "where table_name in ('a','b','c') order by table_name, ordinal_position"
            )
        detail = str(exc.value.detail)
        assert exc.value.status_code == 400
        assert "single SELECT" not in detail, "the statement IS a single SELECT -- the refusal must not claim otherwise"
        assert "information_schema" in detail or "catalog" in detail.lower()
        assert "schema" in detail
        assert "catalog" in detail
        # Must not dump the rest of the blocklist alongside the class name.
        assert "duckdb_tables" not in detail
        assert "sqlite_master" not in detail
        assert "read_parquet" not in detail

    def test_duckdb_catalog_view_also_names_the_metadata_class(self):
        with pytest.raises(HTTPException) as exc:
            _assert_select_only("select * from duckdb_tables()")
        detail = str(exc.value.detail)
        assert "schema" in detail
        assert "catalog" in detail

    def test_genuine_multi_statement_still_says_so(self):
        # Pin: a REAL multi-statement input keeps the accurate "not a single
        # SELECT" wording -- this is the one class for which that claim is
        # actually true.
        with pytest.raises(HTTPException) as exc:
            _assert_select_only("select 1; drop table x")
        detail = str(exc.value.detail)
        assert exc.value.status_code == 400
        assert "single SELECT" in detail
