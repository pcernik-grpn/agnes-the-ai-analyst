"""Unit tests for issue #1395 — unrecognized dtypes in a Jira schema dict.

``get_pyarrow_schema`` mapped any unrecognized dtype string to ``pa.string()``
via its ``else`` branch, and ``apply_schema``'s conversion loop had no
``else`` at all, so an unknown dtype silently skipped type conversion. A
typo'd dtype in any of the schema dicts therefore produced a wrong-typed
parquet column with no error anywhere in the pipeline — see the issue for the
full reproduction table.

Both functions now reject an unrecognized dtype with a ``ValueError`` naming
the offending column, the bad value, and the accepted set — at schema-mapping
time, before any row data is touched, so a typo in one schema dict fails the
one table that carries it rather than corrupting a column silently.
"""

import pandas as pd
import pyarrow as pa
import pytest

from connectors.jira.transform import (
    ATTACHMENTS_SCHEMA,
    CHANGELOG_SCHEMA,
    COMMENTS_SCHEMA,
    ISSUELINKS_SCHEMA,
    ISSUES_SCHEMA,
    ORGANIZATIONS_SCHEMA,
    REMOTE_LINKS_SCHEMA,
    apply_schema,
    get_pyarrow_schema,
    issues_schema,
    organizations_schema,
)

ALL_SCHEMA_DICTS = {
    "ISSUES_SCHEMA": ISSUES_SCHEMA,
    "ORGANIZATIONS_SCHEMA": ORGANIZATIONS_SCHEMA,
    "COMMENTS_SCHEMA": COMMENTS_SCHEMA,
    "ATTACHMENTS_SCHEMA": ATTACHMENTS_SCHEMA,
    "CHANGELOG_SCHEMA": CHANGELOG_SCHEMA,
    "ISSUELINKS_SCHEMA": ISSUELINKS_SCHEMA,
    "REMOTE_LINKS_SCHEMA": REMOTE_LINKS_SCHEMA,
}


class TestUnrecognizedDtypeRejected:
    """The typo flavors from the issue's reproduction table."""

    @pytest.mark.parametrize(
        "dtype",
        [
            "boolean",  # typo for "bool"
            "datetime",  # typo for "datetime64[ns, UTC]"
            "int64",  # typo for "Int64"
            "does-not-exist",
        ],
    )
    def test_get_pyarrow_schema_raises(self, dtype):
        with pytest.raises(ValueError, match="unrecognized dtype"):
            get_pyarrow_schema({"my_col": dtype})

    @pytest.mark.parametrize(
        "dtype",
        [
            "boolean",
            "datetime",
            "int64",
            "does-not-exist",
        ],
    )
    def test_apply_schema_raises(self, dtype):
        df = pd.DataFrame({"my_col": ["1", "2"]})
        with pytest.raises(ValueError, match="unrecognized dtype"):
            apply_schema(df, {"my_col": dtype})

    def test_message_names_column_dtype_and_accepted_set(self):
        with pytest.raises(ValueError) as exc_info:
            get_pyarrow_schema({"weird_col": "boooool"})
        message = str(exc_info.value)
        assert "weird_col" in message
        assert "boooool" in message
        for accepted in ("string", "Int64", "bool", "datetime64"):
            assert accepted in message

    def test_apply_schema_raises_before_mutating_other_columns(self):
        """A typo'd column must not let earlier, valid columns get silently
        converted and returned — the whole call fails, not just one column."""
        df = pd.DataFrame({"good": ["1"], "bad": ["x"]})
        with pytest.raises(ValueError):
            apply_schema(df, {"good": "string", "bad": "boolean"})

    def test_silent_reproduction_from_the_issue_now_raises(self):
        """The exact snippet from the issue body: a 'datetime' typo must now
        raise instead of silently producing a string column."""
        df = pd.DataFrame({"k": ["1", "2"], "c": ["2026-01-02T03:04:05.000+0000", None]})
        with pytest.raises(ValueError, match="unrecognized dtype"):
            apply_schema(df, {"k": "string", "c": "datetime"})


class TestRecognizedDtypesUnchanged:
    """Every dtype the connector actually uses must keep mapping exactly as
    before — the stricter check must not reject anything legitimate."""

    def test_string_maps_to_pa_string(self):
        assert get_pyarrow_schema({"c": "string"}).field("c").type == pa.string()

    def test_int64_maps_to_pa_int64(self):
        assert get_pyarrow_schema({"c": "Int64"}).field("c").type == pa.int64()

    def test_bool_maps_to_pa_bool(self):
        assert get_pyarrow_schema({"c": "bool"}).field("c").type == pa.bool_()

    def test_datetime64_maps_to_pa_timestamp(self):
        schema = get_pyarrow_schema({"c": "datetime64[ns, UTC]"})
        assert schema.field("c").type == pa.timestamp("us", tz="UTC")

    def test_apply_schema_string_conversion_unchanged(self):
        df = pd.DataFrame({"c": ["1", None]})
        table = apply_schema(df, {"c": "string"})
        assert table.column("c").to_pylist() == ["1", None]

    def test_apply_schema_int64_conversion_unchanged(self):
        df = pd.DataFrame({"c": ["1", "x", None]})
        table = apply_schema(df, {"c": "Int64"})
        assert table.column("c").to_pylist() == [1, None, None]

    def test_apply_schema_bool_conversion_unchanged(self):
        df = pd.DataFrame({"c": [True, False, None]})
        table = apply_schema(df, {"c": "bool"})
        assert table.column("c").to_pylist() == [True, False, None]

    def test_apply_schema_datetime_conversion_unchanged(self):
        df = pd.DataFrame({"c": ["2026-01-02T03:04:05.000+0000", None]})
        table = apply_schema(df, {"c": "datetime64[ns, UTC]"})
        assert table.column("c").to_pylist()[1] is None
        assert table.column("c").to_pylist()[0] is not None

    @pytest.mark.parametrize("name,schema_dict", ALL_SCHEMA_DICTS.items())
    def test_every_static_schema_dict_dtype_is_accepted(self, name, schema_dict):
        """Every dtype in every shipped schema dict must pass the stricter
        check — guards against the fix rejecting something legitimate."""
        get_pyarrow_schema(schema_dict)

    def test_issues_schema_with_refresh_fields_is_accepted(self):
        """``issues_schema()`` appends refresh-field columns as ``string`` —
        confirm the dynamic extension stays valid too."""
        get_pyarrow_schema(issues_schema())

    def test_organizations_schema_with_detail_fields_is_accepted(self):
        get_pyarrow_schema(organizations_schema())
