"""Tests for incremental Jira parquet transform (upsert_dataframe and friends)."""

import json

import duckdb
import pandas as pd
import pytest

from connectors.jira.incremental_transform import (
    load_parquet_month,
    save_parquet_month,
    transform_single_issue,
    upsert_dataframe,
)
from connectors.jira.transform import COMMENTS_SCHEMA, REMOTE_LINKS_SCHEMA


# Minimal schema compatible with ISSUES_SCHEMA for testing purposes
_SIMPLE_SCHEMA = {
    "issue_key": "string",
    "summary": "string",
}


@pytest.fixture
def parquet_dir(tmp_path):
    d = tmp_path / "parquet_data"
    d.mkdir()
    return d


def _make_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestUpsertDataframe:
    def test_insert_into_empty(self):
        """Upserting into None/empty creates a new DataFrame."""
        new_records = [{"issue_key": "PROJ-1", "summary": "Bug A"}]
        result = upsert_dataframe(None, new_records, "issue_key", "PROJ-1")
        assert len(result) == 1
        assert result.iloc[0]["issue_key"] == "PROJ-1"

    def test_insert_new_issue(self):
        """Upserting a new issue_key adds a new row."""
        existing = _make_df([{"issue_key": "PROJ-1", "summary": "Existing"}])
        new_records = [{"issue_key": "PROJ-2", "summary": "New issue"}]
        result = upsert_dataframe(existing, new_records, "issue_key", "PROJ-2")
        assert len(result) == 2
        keys = set(result["issue_key"].tolist())
        assert keys == {"PROJ-1", "PROJ-2"}

    def test_update_existing_issue(self):
        """Upserting an existing issue_key replaces the old row."""
        existing = _make_df(
            [
                {"issue_key": "PROJ-1", "summary": "Old summary"},
                {"issue_key": "PROJ-2", "summary": "Other issue"},
            ]
        )
        new_records = [{"issue_key": "PROJ-1", "summary": "Updated summary"}]
        result = upsert_dataframe(existing, new_records, "issue_key", "PROJ-1")
        assert len(result) == 2
        proj1 = result[result["issue_key"] == "PROJ-1"]
        assert proj1.iloc[0]["summary"] == "Updated summary"

    def test_delete_issue(self):
        """Upserting with empty records removes the issue (deletion case)."""
        existing = _make_df(
            [
                {"issue_key": "PROJ-1", "summary": "To be deleted"},
                {"issue_key": "PROJ-2", "summary": "Keep this"},
            ]
        )
        result = upsert_dataframe(existing, [], "issue_key", "PROJ-1")
        assert len(result) == 1
        assert result.iloc[0]["issue_key"] == "PROJ-2"

    def test_upsert_empty_existing_df(self):
        """Upserting into an empty (non-None) DataFrame works correctly."""
        existing = pd.DataFrame(columns=["issue_key", "summary"])
        new_records = [{"issue_key": "PROJ-5", "summary": "First issue"}]
        result = upsert_dataframe(existing, new_records, "issue_key", "PROJ-5")
        assert len(result) == 1
        assert result.iloc[0]["issue_key"] == "PROJ-5"

    def test_upsert_multiple_records_same_issue(self):
        """Multiple records for the same issue_key are all replaced."""
        existing = _make_df(
            [
                {"issue_key": "PROJ-1", "summary": "Comment 1"},
                {"issue_key": "PROJ-1", "summary": "Comment 2"},
                {"issue_key": "PROJ-2", "summary": "Other"},
            ]
        )
        new_records = [{"issue_key": "PROJ-1", "summary": "Updated comment"}]
        result = upsert_dataframe(existing, new_records, "issue_key", "PROJ-1")
        proj1_rows = result[result["issue_key"] == "PROJ-1"]
        assert len(proj1_rows) == 1  # Only the updated record
        assert proj1_rows.iloc[0]["summary"] == "Updated comment"


class TestParquetMonthlyPartitioning:
    def test_save_and_load_parquet(self, parquet_dir):
        """save_parquet_month writes and load_parquet_month reads correctly."""
        df = _make_df(
            [
                {"issue_key": "PROJ-1", "summary": "Test issue"},
            ]
        )
        save_parquet_month(df, _SIMPLE_SCHEMA, parquet_dir, "2026-04")
        loaded = load_parquet_month(parquet_dir, "2026-04")
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded.iloc[0]["issue_key"] == "PROJ-1"

    def test_load_nonexistent_returns_none(self, parquet_dir):
        """load_parquet_month returns None if the file doesn't exist."""
        result = load_parquet_month(parquet_dir, "2099-01")
        assert result is None

    def test_save_empty_df_removes_file(self, parquet_dir):
        """save_parquet_month with empty df removes the hive dir for that month."""
        # First write a file
        df = _make_df([{"issue_key": "PROJ-1", "summary": "Test"}])
        save_parquet_month(df, _SIMPLE_SCHEMA, parquet_dir, "2026-01")
        assert (parquet_dir / "month=2026-01" / "data.parquet").exists()

        # Save empty df — hive dir should be removed
        empty = pd.DataFrame()
        save_parquet_month(empty, _SIMPLE_SCHEMA, parquet_dir, "2026-01")
        assert not (parquet_dir / "month=2026-01").exists()

    def test_separate_months_independent_files(self, parquet_dir):
        """Different month_keys write to separate hive partition directories."""
        df_april = _make_df([{"issue_key": "PROJ-A", "summary": "April issue"}])
        df_may = _make_df([{"issue_key": "PROJ-B", "summary": "May issue"}])

        save_parquet_month(df_april, _SIMPLE_SCHEMA, parquet_dir, "2026-04")
        save_parquet_month(df_may, _SIMPLE_SCHEMA, parquet_dir, "2026-05")

        assert (parquet_dir / "month=2026-04" / "data.parquet").exists()
        assert (parquet_dir / "month=2026-05" / "data.parquet").exists()

        april_loaded = load_parquet_month(parquet_dir, "2026-04")
        may_loaded = load_parquet_month(parquet_dir, "2026-05")

        assert april_loaded.iloc[0]["issue_key"] == "PROJ-A"
        assert may_loaded.iloc[0]["issue_key"] == "PROJ-B"

    def test_parquet_readable_by_duckdb(self, parquet_dir):
        """Parquet files written by save_parquet_month are readable by DuckDB."""
        df = _make_df(
            [
                {"issue_key": "PROJ-1", "summary": "DuckDB readable"},
                {"issue_key": "PROJ-2", "summary": "Also readable"},
            ]
        )
        save_parquet_month(df, _SIMPLE_SCHEMA, parquet_dir, "2026-04")

        pq_file = str(parquet_dir / "month=2026-04" / "data.parquet")
        conn = duckdb.connect()
        rows = conn.execute(f"SELECT count(*) FROM read_parquet('{pq_file}')").fetchone()
        conn.close()
        assert rows[0] == 2

    def test_upsert_round_trip_with_real_parquet(self, parquet_dir):
        """Full upsert round trip: write, load, upsert, save, verify."""
        # Initial write
        initial = _make_df(
            [
                {"issue_key": "PROJ-1", "summary": "Original"},
                {"issue_key": "PROJ-2", "summary": "Keep"},
            ]
        )
        save_parquet_month(initial, _SIMPLE_SCHEMA, parquet_dir, "2026-04")

        # Load existing
        existing = load_parquet_month(parquet_dir, "2026-04")

        # Upsert update for PROJ-1
        updated = upsert_dataframe(
            existing,
            [{"issue_key": "PROJ-1", "summary": "Updated"}],
            "issue_key",
            "PROJ-1",
        )

        # Save back
        save_parquet_month(updated, _SIMPLE_SCHEMA, parquet_dir, "2026-04")

        # Reload and verify
        final = load_parquet_month(parquet_dir, "2026-04")
        assert len(final) == 2
        proj1 = final[final["issue_key"] == "PROJ-1"]
        assert proj1.iloc[0]["summary"] == "Updated"
        proj2 = final[final["issue_key"] == "PROJ-2"]
        assert proj2.iloc[0]["summary"] == "Keep"


def _seed_remote_links_parquet(parquet_root, month_key, rows):
    """Write a starter remote_links parquet so we can assert preservation."""
    df = pd.DataFrame(rows)
    target = parquet_root / "remote_links"
    target.mkdir(parents=True, exist_ok=True)
    save_parquet_month(df, REMOTE_LINKS_SCHEMA, target, month_key)


def _write_raw_issue(raw_dir, issue_key, payload):
    """Seed raw issue JSON at the path transform_single_issue reads from."""
    issues_dir = raw_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    (issues_dir / f"{issue_key}.json").write_text(json.dumps(payload))


def test_incremental_preserves_remote_links_when_overlay_absent(tmp_path):
    """When the _remote_links key is absent from the raw JSON (the writer
    skipped the overlay due to a Jira fetch failure), transform_single_issue
    must NOT wipe existing parquet rows for that issue. Existing rows must
    remain untouched and the function must report success."""
    raw_dir = tmp_path / "raw"
    output_dir = tmp_path / "parquet"
    attachments_dir = tmp_path / "attachments"
    output_dir.mkdir()
    attachments_dir.mkdir()

    # Pre-seed an existing remote-link row for PROJ-1 in month 2026-05.
    _seed_remote_links_parquet(
        output_dir,
        "2026-05",
        [
            {
                "issue_key": "PROJ-1",
                "remote_link_id": "rl-existing",
                "url": "https://example.com/old",
                "title": "Pre-existing link",
                "application_name": "X",
                "application_type": "x",
            }
        ],
    )

    # Raw issue WITHOUT _remote_links key — overlay was skipped upstream.
    _write_raw_issue(
        raw_dir,
        "PROJ-1",
        {
            "key": "PROJ-1",
            "id": "10001",
            "fields": {
                "summary": "test",
                "status": {"name": "Open"},
                "issuetype": {"name": "Bug"},
                "attachment": [],
                "comment": {"comments": []},
                "created": "2026-05-15T00:00:00.000+0000",
                "updated": "2026-05-15T00:00:00.000+0000",
            },
            # NOTE: no _remote_links key — that is the test condition.
        },
    )

    ok = transform_single_issue(
        issue_key="PROJ-1",
        raw_dir=raw_dir,
        output_dir=output_dir,
        attachments_dir=attachments_dir,
    )
    assert ok is True

    df = load_parquet_month(output_dir / "remote_links", "2026-05")
    assert df is not None and len(df) == 1, "Existing remote-link row was wiped — overlay-absent signal not honored"
    assert df.iloc[0]["remote_link_id"] == "rl-existing"


def test_incremental_wipes_remote_links_when_overlay_present_but_empty(tmp_path):
    """The mirror-image case: when _remote_links IS present but the list is
    empty, that's a successful fetch confirming the issue legitimately has no
    remote links right now. The transform MUST wipe any existing parquet rows
    for that issue — keeping them would be stale data.

    Together with test_incremental_preserves_remote_links_when_overlay_absent,
    this locks the absent-vs-empty contract end-to-end. A future regression
    that 'simplifies' the transform to treat [] the same as absent (i.e.,
    skip upsert) would be caught here."""
    raw_dir = tmp_path / "raw"
    output_dir = tmp_path / "parquet"
    attachments_dir = tmp_path / "attachments"
    output_dir.mkdir()
    attachments_dir.mkdir()

    # Pre-seed a stale row that should be wiped.
    _seed_remote_links_parquet(
        output_dir,
        "2026-05",
        [
            {
                "issue_key": "PROJ-2",
                "remote_link_id": "rl-stale",
                "url": "https://example.com/stale",
                "title": "Stale link to be wiped",
                "application_name": "X",
                "application_type": "x",
            }
        ],
    )

    # Raw issue WITH _remote_links: [] — fresh fetch confirmed empty.
    _write_raw_issue(
        raw_dir,
        "PROJ-2",
        {
            "key": "PROJ-2",
            "id": "10002",
            "fields": {
                "summary": "test",
                "status": {"name": "Open"},
                "issuetype": {"name": "Bug"},
                "attachment": [],
                "comment": {"comments": []},
                "created": "2026-05-15T00:00:00.000+0000",
                "updated": "2026-05-15T00:00:00.000+0000",
            },
            "_remote_links": [],
        },
    )

    ok = transform_single_issue(
        issue_key="PROJ-2",
        raw_dir=raw_dir,
        output_dir=output_dir,
        attachments_dir=attachments_dir,
    )
    assert ok is True

    df = load_parquet_month(output_dir / "remote_links", "2026-05")
    # Either the file was unlinked (df is None) or the row was removed.
    # Both outcomes satisfy the contract — the stale row must not survive.
    if df is not None:
        remaining = df[df["issue_key"] == "PROJ-2"]
        assert len(remaining) == 0, (
            "Stale remote-link row survived a successful empty-list fetch — "
            "the empty-list (legitimate) signal was misinterpreted as preserve"
        )


def _seed_comments_parquet(parquet_root, month_key, rows):
    """Write a starter comments parquet so we can assert preservation."""
    df = pd.DataFrame(rows)
    target = parquet_root / "comments"
    target.mkdir(parents=True, exist_ok=True)
    save_parquet_month(df, COMMENTS_SCHEMA, target, month_key)


def test_incremental_preserves_comments_when_pagination_incomplete(tmp_path):
    """A previously successful fetch stored the issue's full 190-comment
    thread. A later refetch hits a transient pagination failure mid-fetch
    (``complete_issue_comments`` marks ``_comments_incomplete``) and only
    managed to re-embed 124 of them. Because the comments upsert is an
    issue-scoped delete-then-insert, overlaying that known-truncated list
    would regress 190 stored comments down to 124 — a regression the
    pre-pagination code could not cause. transform_single_issue must instead
    preserve the previously stored rows untouched."""
    raw_dir = tmp_path / "raw"
    output_dir = tmp_path / "parquet"
    attachments_dir = tmp_path / "attachments"
    output_dir.mkdir()
    attachments_dir.mkdir()

    # Pre-seed 190 previously-stored comment rows for PROJ-3 in month 2026-05.
    _seed_comments_parquet(
        output_dir,
        "2026-05",
        [{"comment_id": f"c{i}", "issue_key": "PROJ-3", "body": f"comment {i}"} for i in range(190)],
    )

    # Raw issue with a partial re-fetch (124 comments) marked incomplete —
    # the shape complete_issue_comments leaves behind on a mid-pagination
    # failure.
    _write_raw_issue(
        raw_dir,
        "PROJ-3",
        {
            "key": "PROJ-3",
            "id": "10003",
            "fields": {
                "summary": "test",
                "status": {"name": "Open"},
                "issuetype": {"name": "Bug"},
                "attachment": [],
                "comment": {
                    "total": 190,
                    "comments": [{"id": f"c{i}", "author": {}, "updateAuthor": {}, "body": {}} for i in range(124)],
                },
                "created": "2026-05-15T00:00:00.000+0000",
                "updated": "2026-05-15T00:00:00.000+0000",
            },
            "_comments_incomplete": True,
        },
    )

    ok = transform_single_issue(
        issue_key="PROJ-3",
        raw_dir=raw_dir,
        output_dir=output_dir,
        attachments_dir=attachments_dir,
    )
    assert ok is True

    df = load_parquet_month(output_dir / "comments", "2026-05")
    assert df is not None and len(df) == 190, (
        "Stored comment thread was overwritten by a known-truncated pagination "
        "retry — _comments_incomplete was not honored"
    )


def test_incremental_writes_partial_comments_when_nothing_stored(tmp_path):
    """The marker is persisted in the raw JSON, so every later re-transform
    reads it again. On an issue's FIRST fetch there are no stored rows to
    preserve, and skipping would mean the issue never gets a comment row at
    all — the partial list cannot regress anything, so it is written."""
    raw_dir = tmp_path / "raw"
    output_dir = tmp_path / "parquet"
    attachments_dir = tmp_path / "attachments"
    output_dir.mkdir()
    attachments_dir.mkdir()

    _write_raw_issue(
        raw_dir,
        "PROJ-4",
        {
            "key": "PROJ-4",
            "id": "10004",
            "fields": {
                "summary": "test",
                "status": {"name": "Open"},
                "issuetype": {"name": "Bug"},
                "attachment": [],
                "comment": {
                    "total": 190,
                    "comments": [{"id": f"c{i}", "author": {}, "updateAuthor": {}, "body": {}} for i in range(124)],
                },
                "created": "2026-05-15T00:00:00.000+0000",
                "updated": "2026-05-15T00:00:00.000+0000",
            },
            "_comments_incomplete": True,
        },
    )

    ok = transform_single_issue(
        issue_key="PROJ-4",
        raw_dir=raw_dir,
        output_dir=output_dir,
        attachments_dir=attachments_dir,
    )
    assert ok is True

    df = load_parquet_month(output_dir / "comments", "2026-05")
    assert df is not None and len(df) == 124, (
        "A first fetch that hit a pagination failure wrote no comments at all — "
        "preserve semantics fired with nothing to preserve"
    )


def test_incremental_skips_partial_comments_when_created_at_unparseable(tmp_path):
    """A ``_comments_incomplete`` payload with no parseable ``created_at``
    (realistic via the webhook fallback path) has no meaningful month to
    probe: ``get_month_key(None)`` falls back to the CURRENT month, which is
    not necessarily where the issue's real comments live. Probing that
    (empty) month would read "nothing stored" and write the partial list
    THERE, while the genuine thread sits in the true creation month — same
    issue_key with comment rows in two partitions (views glob ``month=*``,
    so they'd double-count). Without a reliable month to probe, this must
    behave the same as "something is stored" and skip the write."""
    raw_dir = tmp_path / "raw"
    output_dir = tmp_path / "parquet"
    attachments_dir = tmp_path / "attachments"
    output_dir.mkdir()
    attachments_dir.mkdir()

    _write_raw_issue(
        raw_dir,
        "PROJ-5",
        {
            "key": "PROJ-5",
            "id": "10005",
            "fields": {
                "summary": "test",
                "status": {"name": "Open"},
                "issuetype": {"name": "Bug"},
                "attachment": [],
                "comment": {
                    "total": 190,
                    "comments": [{"id": f"c{i}", "author": {}, "updateAuthor": {}, "body": {}} for i in range(124)],
                },
                # NOTE: no "created" field at all — created_at will not parse.
                "updated": "2026-05-15T00:00:00.000+0000",
            },
            "_comments_incomplete": True,
        },
    )

    ok = transform_single_issue(
        issue_key="PROJ-5",
        raw_dir=raw_dir,
        output_dir=output_dir,
        attachments_dir=attachments_dir,
    )
    assert ok is True

    from connectors.jira.incremental_transform import get_month_key

    fallback_month = get_month_key(None)
    df = load_parquet_month(output_dir / "comments", fallback_month)
    assert df is None or df.empty, (
        "A _comments_incomplete payload with no parseable created_at wrote its "
        "partial comment list into the fallback (current) month — this can "
        "double-count the thread once the real creation month is known"
    )


# --------------------------------------------------------------------------------
# Changelog: absent overlay preserves, present-but-empty wipes.
# --------------------------------------------------------------------------------
#
# The exact contract `_remote_links` has carried since #203, applied to the one
# other table whose overlay can go missing. `fetch_issue` and the backfill both
# request `expand=renderedFields,changelog`, so a successful fetch ALWAYS carries
# a `changelog` key — the only writer that does not is the webhook fetch-failure
# fallback, whose payload is the event body Jira embedded, and Jira has never
# embedded a changelog there. Every such save therefore wrote zero changelog rows
# through an issue-scoped delete-then-insert, silently deleting the issue's whole
# stored history.


def _seed_changelog_parquet(parquet_root, month_key, rows):
    from connectors.jira.transform import CHANGELOG_SCHEMA

    target = parquet_root / "changelog"
    target.mkdir(parents=True, exist_ok=True)
    save_parquet_month(pd.DataFrame(rows), CHANGELOG_SCHEMA, target, month_key)


def _issue_without_changelog(issue_key: str) -> dict:
    return {
        "key": issue_key,
        "id": "10001",
        "fields": {
            "summary": "test",
            "status": {"name": "Open"},
            "issuetype": {"name": "Bug"},
            "attachment": [],
            "comment": {"comments": [], "total": 0},
            "created": "2026-05-15T00:00:00.000+0000",
            "updated": "2026-05-15T00:00:00.000+0000",
        },
        "_remote_links": [],
        # NOTE: no `changelog` key — that is the test condition.
    }


def _stored_change_ids(output_dir, issue_key):
    df = load_parquet_month(output_dir / "changelog", "2026-05")
    if df is None:
        return []
    return df[df["issue_key"] == issue_key]["change_id"].tolist()


def test_incremental_preserves_changelog_when_the_key_is_absent(tmp_path):
    """A payload with no `changelog` key carries no fresh history — it is not
    evidence that the issue has none. The webhook fetch-failure fallback is
    exactly this shape, so before the preserve selector every such save wiped the
    issue's stored changelog rows."""
    raw_dir, output_dir, attachments_dir = tmp_path / "raw", tmp_path / "parquet", tmp_path / "attachments"
    output_dir.mkdir()
    attachments_dir.mkdir()

    _seed_changelog_parquet(
        output_dir,
        "2026-05",
        [
            {
                "change_id": "ch-existing",
                "issue_key": "PROJ-1",
                "author_email": "a@example.com",
                "author_name": "A",
                "field_name": "status",
                "field_type": "jira",
                "from_value": "New",
                "to_value": "In Progress",
                "changed_at": "2026-05-15T00:00:00+00:00",
            }
        ],
    )
    _write_raw_issue(raw_dir, "PROJ-1", _issue_without_changelog("PROJ-1"))

    ok = transform_single_issue(
        issue_key="PROJ-1", raw_dir=raw_dir, output_dir=output_dir, attachments_dir=attachments_dir
    )

    assert ok is True
    assert _stored_change_ids(output_dir, "PROJ-1") == ["ch-existing"], (
        "stored changelog rows were wiped by a payload that carried no changelog overlay"
    )


def test_incremental_wipes_changelog_when_histories_is_present_but_empty(tmp_path):
    """The mirror image, and the reason the check is on the KEY rather than on
    emptiness: `{'histories': []}` is a successful fetch confirming the issue has
    no history right now, so stale rows must go. Collapsing the two would make
    the preserve rule unfalsifiable."""
    raw_dir, output_dir, attachments_dir = tmp_path / "raw", tmp_path / "parquet", tmp_path / "attachments"
    output_dir.mkdir()
    attachments_dir.mkdir()

    _seed_changelog_parquet(
        output_dir,
        "2026-05",
        [
            {
                "change_id": "ch-stale",
                "issue_key": "PROJ-2",
                "author_email": "a@example.com",
                "author_name": "A",
                "field_name": "status",
                "field_type": "jira",
                "from_value": "New",
                "to_value": "In Progress",
                "changed_at": "2026-05-15T00:00:00+00:00",
            }
        ],
    )
    issue = _issue_without_changelog("PROJ-2")
    issue["changelog"] = {"histories": []}
    _write_raw_issue(raw_dir, "PROJ-2", issue)

    ok = transform_single_issue(
        issue_key="PROJ-2", raw_dir=raw_dir, output_dir=output_dir, attachments_dir=attachments_dir
    )

    assert ok is True
    assert _stored_change_ids(output_dir, "PROJ-2") == [], (
        "a successful empty-history fetch left stale changelog rows behind"
    )


def test_a_full_rebuild_survives_an_issue_whose_changelog_overlay_is_absent(tmp_path):
    """`transform_all` extends a list with the selector's return value. Widening
    the return type to `list | None` without guarding that extend crashes the
    rebuild mid-issue — and the per-file handler is a blind `except`, so the run
    would finish "successfully" having silently dropped every table's rows for
    that issue."""
    from connectors.jira.transform import transform_all

    raw_dir, output_dir = tmp_path / "raw", tmp_path / "parquet"
    output_dir.mkdir()
    _write_raw_issue(raw_dir, "PROJ-3", _issue_without_changelog("PROJ-3"))

    counts = transform_all(raw_dir=raw_dir, output_dir=output_dir)

    assert counts["issues"] == 1, "the issue was dropped by the blind per-file except — a partial-bucket rebuild"
    assert counts["changelog"] == 0
