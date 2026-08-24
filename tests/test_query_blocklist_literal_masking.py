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
