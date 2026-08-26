"""Tests for the ``comments.public_visibility`` column.

Jira Service Management separates customer-facing replies from internal notes.
The platform API exposes that state as ``jsdPublic`` on every comment — present
on the comments embedded in a plain ``GET /issue/{key}``, so no ``expand`` and
no second request are needed. JSM's own storage, the ``sd.public.comment``
entity property, is deliberately not consulted: it needs ``expand=properties``,
and any payload carrying it carries ``jsdPublic`` too, so a property branch
would be unreachable by construction.

The column is three-valued on purpose. A missing flag is written as NULL and
counted, never defaulted: a boolean that is confidently wrong is worse than one
that admits the gap, because nothing downstream can distinguish a defaulted
``true`` from an observed one. The same strictness applies to typing: only a
JSON boolean is trusted — ``bool("false")`` is ``True``, so a mistyped flag
resolves to NULL rather than risking an inverted value.
"""

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pyarrow as pa

from connectors.jira.transform import (
    COMMENTS_SCHEMA,
    apply_schema,
    get_pyarrow_schema,
    transform_comments,
)


def _issue(*comments: dict) -> dict:
    return {"key": "SUPPORT-1", "fields": {"comment": {"comments": list(comments), "total": len(comments)}}}


def _comment(comment_id: str = "1", **extra) -> dict:
    base = {
        "id": comment_id,
        "author": {"emailAddress": "agent@example.com", "displayName": "Agent"},
        "updateAuthor": {"emailAddress": "agent@example.com", "displayName": "Agent"},
        "body": {"type": "doc", "content": [{"type": "text", "text": "hello"}]},
        "created": "2026-01-01T10:00:00.000+0000",
        "updated": "2026-01-01T10:00:00.000+0000",
    }
    base.update(extra)
    return base


def _visibility(comment: dict):
    records = transform_comments(_issue(comment), preserve_on_incomplete=False)
    assert records is not None and len(records) == 1
    return records[0]["public_visibility"]


class TestPublicVisibilityExtraction:
    """The states a comment can arrive in, and what each must produce."""

    def test_jsd_public_true_is_customer_facing(self):
        assert _visibility(_comment(jsdPublic=True)) is True

    def test_jsd_public_false_is_internal(self):
        assert _visibility(_comment(jsdPublic=False)) is False

    def test_absent_flag_is_null_never_defaulted(self):
        assert _visibility(_comment()) is None

    def test_explicit_null_flag_is_null(self):
        assert _visibility(_comment(jsdPublic=None)) is None

    def test_string_false_is_null_never_inverted(self):
        """``bool("false")`` is ``True`` — the exact miscoercion the sibling
        dataset shipped for ``value.internal``. A mistyped flag must resolve to
        NULL, never to an inverted boolean."""
        assert _visibility(_comment(jsdPublic="false")) is None

    def test_string_true_is_null_not_trusted(self):
        assert _visibility(_comment(jsdPublic="true")) is None

    def test_numeric_flag_is_null_not_trusted(self):
        assert _visibility(_comment(jsdPublic=1)) is None
        assert _visibility(_comment(jsdPublic=0)) is None

    def test_unresolved_comments_are_counted_in_a_warning(self, caplog):
        issue = _issue(_comment("1", jsdPublic=True), _comment("2"), _comment("3"))
        with caplog.at_level(logging.WARNING, logger="connectors.jira.transform"):
            transform_comments(issue, preserve_on_incomplete=False)
        assert "2 of 3 comments" in caplog.text
        assert "arrived without a boolean jsdPublic" in caplog.text

    def test_mistyped_flags_are_counted_in_the_warning(self, caplog):
        issue = _issue(_comment("1", jsdPublic=True), _comment("2", jsdPublic="false"))
        with caplog.at_level(logging.WARNING, logger="connectors.jira.transform"):
            transform_comments(issue, preserve_on_incomplete=False)
        assert "1 of 2 comments" in caplog.text

    def test_no_warning_when_every_comment_resolves(self, caplog):
        with caplog.at_level(logging.WARNING, logger="connectors.jira.transform"):
            transform_comments(_issue(_comment("1", jsdPublic=True)), preserve_on_incomplete=False)
        assert "public_visibility" not in caplog.text

    def test_warning_suppressed_on_throwaway_passes(self, caplog):
        """``transform_issues``' grouping pass discards its payloads and rebuilds
        them under the month lock; it passes ``warn_unresolved=False`` so the
        same gap is not logged twice per issue per cycle."""
        with caplog.at_level(logging.WARNING, logger="connectors.jira.transform"):
            transform_comments(_issue(_comment("1")), preserve_on_incomplete=False, warn_unresolved=False)
        assert "public_visibility" not in caplog.text


class TestBoolSchemaSupport:
    """``bool`` was not a supported dtype before this column existed.

    ``get_pyarrow_schema`` fell through to ``pa.string()`` and ``apply_schema``
    raised ``ArrowTypeError`` on a real boolean, so both needed a branch.
    """

    def test_pyarrow_schema_emits_bool(self):
        assert get_pyarrow_schema(COMMENTS_SCHEMA).field("public_visibility").type == pa.bool_()

    def test_comments_schema_declares_the_column_as_bool(self):
        assert COMMENTS_SCHEMA["public_visibility"] == "bool"

    def test_mixed_true_false_null_round_trips(self):
        df = pd.DataFrame(
            [
                {"comment_id": "1", "public_visibility": True},
                {"comment_id": "2", "public_visibility": False},
                {"comment_id": "3", "public_visibility": None},
            ]
        )
        table = apply_schema(df, COMMENTS_SCHEMA)
        assert table.schema.field("public_visibility").type == pa.bool_()
        assert table.column("public_visibility").to_pylist() == [True, False, None]

    def test_all_null_column_stays_bool_typed(self):
        """An issue whose comments carry no signal must not degrade the column
        to string — the monthly parquets are read with ``union_by_name``, and a
        type that varies per month breaks the union."""
        table = apply_schema(pd.DataFrame([{"comment_id": "1"}]), COMMENTS_SCHEMA)
        assert table.schema.field("public_visibility").type == pa.bool_()
        assert table.column("public_visibility").to_pylist() == [None]

    def test_nan_from_a_concat_with_older_rows_becomes_null(self):
        """The incremental path concatenates new rows onto parquet rows written
        before this column existed; the missing side arrives as NaN."""
        old = pd.DataFrame([{"comment_id": "1"}])
        new = pd.DataFrame([{"comment_id": "2", "public_visibility": False}])
        table = apply_schema(pd.concat([old, new], ignore_index=True), COMMENTS_SCHEMA)
        assert table.column("public_visibility").to_pylist() == [None, False]


class TestBothTransformPathsCarryTheColumn:
    """Both production callers pass ``preserve_on_incomplete=False``, so the
    column flows from the one place it is populated."""

    def test_full_rebuild_path(self):
        records = transform_comments(_issue(_comment(jsdPublic=False)), preserve_on_incomplete=False)
        assert records[0]["public_visibility"] is False

    def test_incremental_payload_path(self, tmp_path):
        from connectors.jira.incremental_transform import _build_issue_payload

        issue = _issue(_comment(jsdPublic=False))
        issue["fields"]["created"] = "2026-01-01T09:00:00.000+0000"
        (tmp_path / "issues").mkdir()
        (tmp_path / "issues" / "SUPPORT-1.json").write_text(json.dumps(issue))

        payload = _build_issue_payload("SUPPORT-1", tmp_path, tmp_path / "attachments")

        assert payload is not None
        assert payload.comments[0]["public_visibility"] is False

    def test_preserve_contract_is_untouched(self):
        """A ``_comments_incomplete`` issue must still preserve, not rewrite."""
        issue = _issue(_comment(jsdPublic=True))
        issue["_comments_incomplete"] = True
        assert transform_comments(issue) is None


class TestEmbeddedThreadCompletenessIsStructural:
    """``_embedded_comments_are_complete`` answers exactly one question: does this
    payload demonstrably carry the whole comment thread?

    v0.83.70 added a second requirement — a boolean ``jsdPublic`` on every embedded
    comment — so that the webhook fetch-failure fallback could not replace an
    observed ``public_visibility`` with NULL. Measurement since then MOVED that
    protection rather than dropping it. ``jsdPublic`` is a Jira Cloud *platform*
    field: live-verified serialized on 100% of comments on JSM-licensed and
    JSM-UNLICENSED sites alike, and the only deployment that lacks it entirely
    (Data Center / Server) cannot run this connector at all — it has no
    ``/rest/api/3``. What remains unsampled is the webhook body's own
    serialization, which is exactly why the value-protection moved to the write
    layer (``incremental_transform._carry_forward_public_visibility``), where it
    holds on EVERY write path instead of refusing to act on this one.

    So structure is all this predicate judges again — the property its name
    promises. The two ship together: reverting the gate without the carry-forward
    would reopen the #1385 data-loss class.
    """

    @staticmethod
    def _payload(comments):
        return {"fields": {"comment": {"comments": comments, "total": len(comments)}}}

    def test_a_length_complete_thread_is_complete(self):
        from connectors.jira.service import _embedded_comments_are_complete

        payload = self._payload([{"id": "1", "jsdPublic": True}, {"id": "2", "jsdPublic": False}])
        assert _embedded_comments_are_complete(payload) is True

    def test_a_comment_missing_the_flag_no_longer_makes_the_thread_incomplete(self):
        """The inverse of the v0.83.70 assertion. Deferring the whole issue's
        comment update because one comment lacks a field is the wrong lever: the
        write layer now protects the stored value directly, so this payload can be
        applied without any observed value being lost."""
        from connectors.jira.service import _embedded_comments_are_complete

        payload = self._payload([{"id": "1", "jsdPublic": True}, {"id": "2"}])
        assert _embedded_comments_are_complete(payload) is True

    def test_a_mistyped_or_null_flag_does_not_make_the_thread_incomplete(self):
        from connectors.jira.service import _embedded_comments_are_complete

        assert _embedded_comments_are_complete(self._payload([{"id": "1", "jsdPublic": "false"}])) is True
        assert _embedded_comments_are_complete(self._payload([{"id": "1", "jsdPublic": None}])) is True

    def test_an_empty_thread_is_complete(self):
        """No comments, nothing to lose — and ``total == 0`` is a real state that
        must not be turned into a permanent incomplete marker."""
        from connectors.jira.service import _embedded_comments_are_complete

        assert _embedded_comments_are_complete(self._payload([])) is True

    def test_a_short_thread_is_incomplete(self):
        from connectors.jira.service import _embedded_comments_are_complete

        payload = {"fields": {"comment": {"comments": [{"id": "1", "jsdPublic": True}], "total": 5}}}
        assert _embedded_comments_are_complete(payload) is False

    def test_a_payload_with_no_comment_field_is_incomplete(self):
        """Not evidence the issue has no comments — an issue-scoped
        delete-then-insert would erase the stored thread."""
        from connectors.jira.service import _embedded_comments_are_complete

        assert _embedded_comments_are_complete({"fields": {"summary": "no comment field"}}) is False

    def test_a_thread_with_no_total_is_incomplete(self):
        from connectors.jira.service import _embedded_comments_are_complete

        assert _embedded_comments_are_complete({"fields": {"comment": {"comments": [{"id": "1"}]}}}) is False

    def test_a_non_list_comments_value_is_incomplete(self):
        from connectors.jira.service import _embedded_comments_are_complete

        assert _embedded_comments_are_complete({"fields": {"comment": {"comments": None, "total": 0}}}) is False


# --------------------------------------------------------------------------------
# Write-layer carry-forward (the protection that superseded the flag gate).
# --------------------------------------------------------------------------------

_MONTH = "2026-01"
_CREATED = "2026-01-15T09:00:00.000+0000"


def _raw_issue(issue_key: str, *comments: dict) -> dict:
    """One issue's raw JSON, shaped like what the writers persist."""
    return {
        "key": issue_key,
        "id": "10001",
        "fields": {
            "summary": f"summary for {issue_key}",
            "created": _CREATED,
            "updated": _CREATED,
            "status": {"name": "Open", "statusCategory": {"name": "To Do"}},
            "issuetype": {"name": "Task"},
            "project": {"key": "SUPPORT", "name": "Support"},
            "attachment": [],
            "comment": {"total": len(comments), "startAt": 0, "comments": list(comments)},
        },
        "changelog": {"histories": []},
        "_remote_links": [],
    }


def _land(tmp_path, issue: dict) -> None:
    """Run one issue through the REAL incremental write path.

    Fixtures for this behaviour must round-trip ``apply_schema`` + parquet +
    ``load_parquet_month``, never a hand-built DataFrame: what the carry-forward
    actually reads back is a pandas-nullable column (``boolean`` extension dtype
    on pandas 3.x, object dtype on the 2.x floor) holding ``numpy.bool_`` and
    ``pd.NA``, and a hand-built frame of plain Python bools would sail past every
    dtype trap this code exists to survive.
    """
    from connectors.jira.incremental_transform import transform_single_issue

    raw_dir = tmp_path / "raw"
    (raw_dir / "issues").mkdir(parents=True, exist_ok=True)
    (raw_dir / "issues" / f"{issue['key']}.json").write_text(json.dumps(issue))
    assert (
        transform_single_issue(
            issue_key=issue["key"],
            raw_dir=raw_dir,
            output_dir=tmp_path / "data",
            attachments_dir=tmp_path / "attachments",
        )
        is True
    )


def _stored_comments(tmp_path, issue_key: str | None = None) -> pd.DataFrame:
    from connectors.jira.incremental_transform import load_parquet_month

    df = load_parquet_month(tmp_path / "data" / "comments", _MONTH)
    assert df is not None
    df = df.sort_values("comment_id").reset_index(drop=True)
    return df if issue_key is None else df[df["issue_key"] == issue_key].reset_index(drop=True)


def _webhook_service(tmp_path, monkeypatch):
    """A `JiraService` whose writes land in the same tree `_land` reads.

    Both directories matter and they are set in different places: `save_issue`
    writes the raw JSON under `Config.JIRA_DATA_DIR`, while the parquet root is
    `incremental_transform.DEFAULT_OUTPUT_DIR` — `trigger_incremental_transform`
    deliberately does NOT forward an `output_dir`, so the module default is the
    only seam. Point either one somewhere else and the assertions read an empty
    partition and pass vacuously.
    """
    from connectors.jira import incremental_transform as inc
    from connectors.jira import service as svc

    monkeypatch.setattr(svc.Config, "JIRA_DOMAIN", "mycompany.atlassian.net")
    monkeypatch.setattr(svc.Config, "JIRA_EMAIL", "bot@mycompany.com")
    monkeypatch.setattr(svc.Config, "JIRA_API_TOKEN", "test-token-xyz")
    monkeypatch.setattr(svc.Config, "JIRA_DATA_DIR", tmp_path / "raw")
    monkeypatch.setattr(inc, "DEFAULT_OUTPUT_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "_jira_service", None)
    return svc.JiraService()


def _visibilities(df: pd.DataFrame) -> list:
    """``public_visibility`` as plain Python — ``None`` for every null flavour."""
    return [None if pd.isna(v) else bool(v) for v in df["public_visibility"]]


class TestPublicVisibilityCarryForward:
    """A comment that arrives without a boolean ``jsdPublic`` must not NULL an
    already-observed ``public_visibility`` for the SAME comment version.

    The comments upsert is an issue-scoped delete-then-insert, so an incoming
    NULL is not a no-op — it overwrites. Carrying the stored value forward makes
    the #1385 data-loss class impossible on every incremental write path, rather
    than only on the one path a completeness gate could refuse to take.

    Version scoping is the honesty constraint: a JSM visibility flip rides a
    comment EDIT, which bumps ``updated``. A differing ``updated_at`` therefore
    means the stored boolean may describe the previous version, and NULL ("not
    observed") is the truthful answer until the next successful refetch.
    """

    def test_a_same_version_null_carries_the_stored_boolean(self, tmp_path):
        _land(tmp_path, _raw_issue("SUPPORT-1", _comment("1", jsdPublic=False), _comment("2", jsdPublic=True)))
        _land(tmp_path, _raw_issue("SUPPORT-1", _comment("1"), _comment("2")))

        assert _visibilities(_stored_comments(tmp_path)) == [False, True]

    def test_a_differing_updated_at_stays_honestly_null(self, tmp_path):
        """The comment was edited between the two writes, and a visibility flip
        rides exactly such an edit. Carrying here could serve a pre-flip boolean
        as observed — the one direction this column was built never to repeat."""
        _land(tmp_path, _raw_issue("SUPPORT-2", _comment("1", jsdPublic=True)))
        _land(tmp_path, _raw_issue("SUPPORT-2", _comment("1", updated="2026-01-16T11:00:00.000+0000")))

        assert _visibilities(_stored_comments(tmp_path)) == [None]

    def test_an_incoming_boolean_is_never_overridden(self, tmp_path):
        _land(tmp_path, _raw_issue("SUPPORT-3", _comment("1", jsdPublic=True)))
        _land(tmp_path, _raw_issue("SUPPORT-3", _comment("1", jsdPublic=False)))

        assert _visibilities(_stored_comments(tmp_path)) == [False]

    def test_a_stored_null_stays_null(self, tmp_path):
        _land(tmp_path, _raw_issue("SUPPORT-4", _comment("1")))
        _land(tmp_path, _raw_issue("SUPPORT-4", _comment("1")))

        assert _visibilities(_stored_comments(tmp_path)) == [None]

    def test_a_partition_written_before_the_column_existed_does_not_break_the_write(self, tmp_path):
        """Partitions last written by pre-0.83.70 code have no
        ``public_visibility`` column at all. An unguarded lookup would ``KeyError``
        and take down the whole issue's write — every table, not just comments."""
        from connectors.jira.incremental_transform import save_parquet_month

        pre_column_schema = {k: v for k, v in COMMENTS_SCHEMA.items() if k != "public_visibility"}
        target = tmp_path / "data" / "comments"
        target.mkdir(parents=True)
        save_parquet_month(
            pd.DataFrame(
                [
                    {
                        "comment_id": "1",
                        "issue_key": "SUPPORT-5",
                        "author_email": "a@example.com",
                        "author_name": "A",
                        "body": "hello",
                        "created_at": "2026-01-15T09:00:00+00:00",
                        "updated_at": "2026-01-01T10:00:00+00:00",
                        "update_author_email": "a@example.com",
                    }
                ]
            ),
            pre_column_schema,
            target,
            _MONTH,
        )

        _land(tmp_path, _raw_issue("SUPPORT-5", _comment("1")))

        assert _visibilities(_stored_comments(tmp_path)) == [None]

    def test_a_stored_row_with_no_timestamp_does_not_carry(self, tmp_path):
        """``NaT`` on either side means the version cannot be established. No
        carry is always the safe direction — a NULL, never a wrong boolean."""
        _land(tmp_path, _raw_issue("SUPPORT-6", _comment("1", jsdPublic=True, updated=None)))
        _land(tmp_path, _raw_issue("SUPPORT-6", _comment("1", updated=None)))

        assert _visibilities(_stored_comments(tmp_path)) == [None]

    def test_a_deleted_comment_is_not_resurrected(self, tmp_path):
        """Carry-forward fills a field on an incoming row; it never adds a row."""
        _land(tmp_path, _raw_issue("SUPPORT-7", _comment("1", jsdPublic=True), _comment("2", jsdPublic=False)))
        _land(tmp_path, _raw_issue("SUPPORT-7", _comment("1")))

        stored = _stored_comments(tmp_path)
        assert stored["comment_id"].tolist() == ["1"]
        assert _visibilities(stored) == [True]

    def test_a_first_write_with_nothing_stored_is_a_no_op(self, tmp_path):
        _land(tmp_path, _raw_issue("SUPPORT-8", _comment("1"), _comment("2", jsdPublic=True)))

        assert _visibilities(_stored_comments(tmp_path)) == [None, True]

    def test_carry_is_scoped_to_the_issue(self, tmp_path):
        """``existing`` is the whole month partition — every issue's rows. Comment
        ids are unique per Jira instance, but keying on the id alone would make
        that an assumption instead of a guarantee."""
        _land(tmp_path, _raw_issue("SUPPORT-9", _comment("1", jsdPublic=True)))
        _land(tmp_path, _raw_issue("SUPPORT-10", _comment("1")))

        assert _visibilities(_stored_comments(tmp_path, "SUPPORT-9")) == [True]
        assert _visibilities(_stored_comments(tmp_path, "SUPPORT-10")) == [None]

    def test_it_says_what_it_carried(self, tmp_path, caplog):
        """A silent never-carry degrade — a dtype change, a timestamp unit drift —
        looks exactly like a deployment whose flags are all present. The count is
        the only thing that tells them apart."""
        _land(tmp_path, _raw_issue("SUPPORT-11", _comment("1", jsdPublic=True), _comment("2")))
        with caplog.at_level(logging.INFO, logger="connectors.jira.incremental_transform"):
            _land(tmp_path, _raw_issue("SUPPORT-11", _comment("1"), _comment("2")))

        assert "carried 1" in caplog.text
        assert "SUPPORT-11" in caplog.text
        assert "1 still NULL" in caplog.text


class TestCarryForwardSurvivesBothDtypeRegimes:
    """What ``load_parquet_month`` hands back is not a frame of Python bools.

    pandas 3.x reads the column as the ``boolean`` extension dtype, whose scalars
    are ``numpy.bool_`` and whose nulls are ``pd.NA``; the pandas 2.x floor round
    trips it as object dtype holding ``True``/``False``/``None``. Two traps follow,
    and both were live defects in earlier drafts: ``isinstance(v, bool)`` is False
    for ``numpy.bool_`` (so the extension regime would never carry anything), and
    plain truthiness RAISES on ``pd.NA`` (so the object regime would blow up the
    write). Realness is therefore ``not pd.isna(v)`` and the carry is ``bool(v)``.
    """

    @staticmethod
    def _existing(dtype):
        df = pd.DataFrame(
            [
                {
                    "issue_key": "SUPPORT-1",
                    "comment_id": "1",
                    "updated_at": pd.Timestamp("2026-01-01T10:00:00Z"),
                    "public_visibility": False,
                },
                {
                    "issue_key": "SUPPORT-1",
                    "comment_id": "2",
                    "updated_at": pd.Timestamp("2026-01-01T10:00:00Z"),
                    "public_visibility": None,
                },
            ]
        )
        df["public_visibility"] = df["public_visibility"].astype(dtype)
        return df

    @staticmethod
    def _incoming():
        from connectors.jira.incremental_transform import _IssuePayload

        records = transform_comments(_issue(_comment("1"), _comment("2")), preserve_on_incomplete=False)
        return _IssuePayload(
            issue_key="SUPPORT-1",
            month_key=_MONTH,
            created_at_missing=False,
            issue={},
            comments=records,
            comments_incomplete=False,
            attachments=[],
            changelog=[],
            issuelinks=[],
            remote_links=[],
        )

    def test_boolean_extension_dtype(self):
        from connectors.jira.incremental_transform import _comment_records

        rows = _comment_records(self._incoming(), self._existing("boolean"))

        assert [r["public_visibility"] for r in rows] == [False, None]

    def test_object_dtype(self):
        from connectors.jira.incremental_transform import _comment_records

        rows = _comment_records(self._incoming(), self._existing(object))

        assert [r["public_visibility"] for r in rows] == [False, None]

    def test_a_numeric_comment_id_matches_its_stored_string(self):
        """Ids are ``str()``-coerced on both sides: parquet stores them as strings,
        and a JSON integer id would otherwise never find its stored row."""
        from connectors.jira.incremental_transform import _comment_records

        payload = self._incoming()
        payload.comments[0]["comment_id"] = 1

        rows = _comment_records(payload, self._existing("boolean"))

        assert rows[0]["public_visibility"] is False

    def test_a_null_comment_id_is_skipped(self):
        from connectors.jira.incremental_transform import _comment_records

        payload = self._incoming()
        payload.comments[0]["comment_id"] = None

        rows = _comment_records(payload, self._existing("boolean"))

        assert rows[0]["public_visibility"] is None


class TestFlaglessWebhookFallbackNullsNothing:
    """The end-to-end pin that keeps the gate revert and the carry-forward
    atomic.

    Reverting ``_embedded_comments_are_complete`` to a structural check without
    the write-layer carry re-opens exactly the loss #1385 closed: a complete but
    flagless webhook-fallback embed would be applied, and its NULLs would replace
    stored booleans through the issue-scoped delete-then-insert. This drives the
    real path — ``process_webhook_event`` with a failed refetch, through
    ``save_issue`` and the real incremental transform — and asserts both halves:
    the update LANDS (no deferral) and no stored boolean is lost.
    """

    def _fallback(self, service, embedded, *, attachments=()):
        with (
            patch.object(service, "fetch_issue", return_value=None),
            patch.object(service, "fetch_remote_links", return_value=[]),
            patch.object(service, "fetch_refresh_fields", return_value=None),
            patch.object(service, "download_all_attachments", return_value=list(attachments)),
        ):
            assert service.process_webhook_event({"webhookEvent": "jira:issue_updated", "issue": embedded}) is True

    def test_no_stored_boolean_is_lost_and_the_update_still_lands(self, tmp_path, monkeypatch):
        _land(tmp_path, _raw_issue("SUPPORT-20", _comment("1", jsdPublic=False), _comment("2", jsdPublic=True)))
        service = _webhook_service(tmp_path, monkeypatch)

        edited = _raw_issue(
            "SUPPORT-20",
            _comment("1", body={"type": "doc", "content": [{"type": "text", "text": "edited"}]}),
            _comment("2"),
        )
        self._fallback(service, edited)

        stored = _stored_comments(tmp_path)
        assert _visibilities(stored) == [False, True], (
            "a flagless fallback embed nulled a stored public_visibility — the gate "
            "revert (D1) shipped without the write-layer carry-forward (D2)"
        )
        assert stored["body"].tolist() == ["edited", "hello"], (
            "the fallback update was deferred instead of applied — the structural "
            "completeness check is still gated on the flag"
        )

    def test_an_edited_comment_still_reports_an_honest_null(self, tmp_path, monkeypatch):
        _land(tmp_path, _raw_issue("SUPPORT-21", _comment("1", jsdPublic=True)))
        service = _webhook_service(tmp_path, monkeypatch)

        self._fallback(service, _raw_issue("SUPPORT-21", _comment("1", updated="2026-01-17T08:00:00.000+0000")))

        assert _visibilities(_stored_comments(tmp_path)) == [None]


class TestUnresolvedWarningIsLoggedOncePerEvent:
    """A webhook event that downloads at least one new attachment runs the full
    transform TWICE — once before the download (so parquet lands even if a large
    attachment kills the worker) and once after (so the freshly attached file gets
    a ``local_path``). Both passes ran ``transform_comments``, so any
    missing-``jsdPublic`` gap was reported twice for one event, doubling the count
    an operator uses to size the anomaly. The second pass now passes
    ``warn_unresolved=False``, the same suppression the batch path's throwaway
    grouping pass already used.
    """

    def _save_with_attachment(self, service, issue):
        with (
            patch.object(service, "fetch_remote_links", return_value=[]),
            patch.object(service, "fetch_refresh_fields", return_value=None),
            patch.object(service, "download_all_attachments", return_value=[Path("a.txt")]),
        ):
            assert service.save_issue(issue) is not None

    @staticmethod
    def _warnings(caplog):
        return [r for r in caplog.records if "jsdPublic" in r.getMessage()]

    def test_one_warning_even_when_an_attachment_re_transforms(self, tmp_path, monkeypatch, caplog):
        service = _webhook_service(tmp_path, monkeypatch)

        with caplog.at_level(logging.WARNING, logger="connectors.jira.transform"):
            self._save_with_attachment(service, _raw_issue("SUPPORT-30", _comment("1")))

        assert len(self._warnings(caplog)) == 1, "the post-attachment re-transform logged the same gap a second time"

    def test_the_deletion_path_keeps_the_default(self, tmp_path, monkeypatch):
        """Only the post-attachment re-transform is silenced. Threading the
        suppression any wider would hide the anomaly signal on the paths that are
        an operator's only view of it."""
        from connectors.jira import service as svc

        seen: list = []
        monkeypatch.setattr(
            svc, "trigger_incremental_transform", lambda *a, **kw: seen.append(kw.get("warn_unresolved", True)) or True
        )
        service = _webhook_service(tmp_path, monkeypatch)
        (tmp_path / "raw" / "issues").mkdir(parents=True, exist_ok=True)
        (tmp_path / "raw" / "issues" / "SUPPORT-31.json").write_text(json.dumps(_raw_issue("SUPPORT-31")))

        assert service._handle_deletion("SUPPORT-31") is True
        assert seen == [True]
