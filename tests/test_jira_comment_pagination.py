"""Tests for Jira comment-pagination completion (issue #1257).

Jira's issue endpoint embeds ``fields.comment.comments`` capped at 100, and
the window is the NEWEST 100 (the payload's own ``fields.comment.startAt`` is
``total - 100``). An issue with more than 100 comments therefore arrives
missing its OLDEST comments unless the fetch layer pages through the comment
endpoint from the thread head — and because every later full-refetch
(``fields=*all``) re-hits the same 100-comment cap, the gap never heals on
its own.

Covers both ingestion paths that share the ``complete_issue_comments`` fetch
seam: the batch/full extract (``JiraBackfill.fetch_issue``) and the webhook
full-refetch (``JiraService.fetch_issue``).
"""

import re
from unittest.mock import MagicMock, patch

import httpx

from connectors.jira.service import complete_issue_comments
from connectors.jira.transform import transform_comments, transform_issue

BASE_URL = "https://mycompany.atlassian.net/rest/api/3"
AUTH = ("bot@mycompany.com", "test-token")


def _comment(comment_id: str, *, jsd_public: bool | None = True) -> dict:
    """A comment as Jira serializes one.

    ``jsdPublic`` is part of that shape — a Jira Cloud platform field, present on
    every comment across every fetch shape measured for the `public_visibility`
    column — and this fixture predates the column, so it used to omit it. A
    fixture without it models a payload Jira does not actually send. Pass
    ``jsd_public=None`` to model one that lacks it deliberately.

    Timestamps are distinct and id-ordered (cN -> N seconds past midnight) so
    any order-sensitivity in the code under test is observable — identical
    timestamps would hide a reordering from every assertion.
    """
    digits = re.search(r"(\d+)$", comment_id)
    n = int(digits.group(1)) if digits else 0
    created = f"2026-01-01T{n // 3600:02d}:{n % 3600 // 60:02d}:{n % 60:02d}.000+0000"
    comment = {
        "id": comment_id,
        "author": {"emailAddress": f"{comment_id}@example.com", "displayName": comment_id},
        "updateAuthor": {"emailAddress": f"{comment_id}@example.com", "displayName": comment_id},
        "body": {"type": "doc", "content": []},
        "created": created,
        "updated": created,
    }
    if jsd_public is not None:
        comment["jsdPublic"] = jsd_public
    return comment


def _issue_with_comments(total: int, embedded: int, issue_key: str = "PROJ-1") -> dict:
    """Issue payload whose embed carries the NEWEST ``embedded`` of ``total``
    comments — the window Jira actually embeds (``startAt = total - embedded``,
    comment ids ``c{total-embedded}`` .. ``c{total-1}``)."""
    start_at = max(0, total - embedded)
    return {
        "key": issue_key,
        "id": "10001",
        "fields": {
            "comment": {
                "total": total,
                "startAt": start_at,
                "comments": [_comment(f"c{i}") for i in range(start_at, total)],
            }
        },
    }


def _comment_page(start: int, stop: int) -> list[dict]:
    """One page of ``GET /issue/{key}/comment`` — ids ``c{start}``..``c{stop-1}``,
    the oldest-first order the paged endpoint serves."""
    return [_comment(f"c{i}") for i in range(start, stop)]


def _mock_client(pages: list[list[dict]] | None = None, status_code: int = 200) -> MagicMock:
    """MagicMock httpx.Client whose .get() returns successive comment pages."""
    client = MagicMock()
    if pages is None:
        responses = []
    else:
        responses = []
        for page in pages:
            resp = MagicMock()
            resp.status_code = status_code
            resp.json.return_value = {"comments": page}
            responses.append(resp)
    client.get.side_effect = responses
    return client


class TestCompleteIssueComments:
    """Unit tests for the shared fetch-layer completion helper."""

    def test_pages_from_the_thread_head(self):
        """total=124, embed carries the newest 100 (c24..c123) -> ONE paginated
        call at startAt=0 recovers the oldest 24 (c0..c23), and the merged
        thread is stored oldest-first."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = _mock_client([_comment_page(0, 100)])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        comments = issue_data["fields"]["comment"]["comments"]
        assert [c["id"] for c in comments] == [f"c{i}" for i in range(124)]
        created = [c["created"] for c in comments]
        assert created == sorted(created), "merged thread must stay chronological"
        client.get.assert_called_once()
        _, kwargs = client.get.call_args
        assert kwargs["params"]["startAt"] == 0
        assert kwargs["params"]["maxResults"] == 100

    def test_under_cap_issues_no_extra_http_call(self):
        """total == embedded (issue under the 100-comment page cap) -> no extra HTTP call."""
        issue_data = _issue_with_comments(total=42, embedded=42)
        client = _mock_client([])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        client.get.assert_not_called()
        assert len(issue_data["fields"]["comment"]["comments"]) == 42

    def test_transform_stores_full_comment_set_and_comment_count(self):
        """After completion, transform_issue/transform_comments see the full set."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = _mock_client([_comment_page(0, 100)])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert transform_issue(issue_data)["comment_count"] == 124
        assert len(transform_comments(issue_data)) == 124

    def test_pages_multiple_times_when_gap_exceeds_one_page(self):
        """total=250, embed carries the newest 100 (c150..c249) -> two paginated
        calls (c0..c99, then c100..c199 overlapping the embed's head)."""
        issue_data = _issue_with_comments(total=250, embedded=100)
        client = _mock_client([_comment_page(0, 100), _comment_page(100, 200)])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        comments = issue_data["fields"]["comment"]["comments"]
        assert [c["id"] for c in comments] == [f"c{i}" for i in range(250)]
        assert client.get.call_count == 2
        first_call_start_at = client.get.call_args_list[0].kwargs["params"]["startAt"]
        second_call_start_at = client.get.call_args_list[1].kwargs["params"]["startAt"]
        assert first_call_start_at == 0
        assert second_call_start_at == 100

    def test_short_page_advances_start_at_by_page_length(self):
        """The next offset is the count of items actually received, not a
        fixed page-size stride — a server that clamps maxResults (or any
        short page) must not make the walk skip comments."""
        issue_data = _issue_with_comments(total=250, embedded=100)  # embeds c150..c249
        client = _mock_client([_comment_page(0, 50), _comment_page(50, 150)])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        comments = issue_data["fields"]["comment"]["comments"]
        assert [c["id"] for c in comments] == [f"c{i}" for i in range(250)]
        offsets = [call.kwargs["params"]["startAt"] for call in client.get.call_args_list]
        assert offsets == [0, 50], "a 50-item page must advance startAt by 50, not by the page size"

    def test_logs_warning_when_still_short_after_completion(self, caplog):
        """A page-fetch failure leaves stored < total -> WARNING, not an exception,
        and the shortfall is attributed to the failed pagination."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        failing_response = MagicMock()
        failing_response.status_code = 500
        client = MagicMock()
        client.get.side_effect = [failing_response]

        with caplog.at_level("WARNING"):
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert len(issue_data["fields"]["comment"]["comments"]) == 100
        shortfall = [r.message for r in caplog.records if "comment.total" in r.message]
        assert shortfall and "gave up early" in shortfall[0]

    def test_shortfall_after_stale_total_is_attributed_to_deletion(self, caplog):
        """An empty page with no failure means total was stale — the WARNING
        must say so, not blame the pagination."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        empty_response = MagicMock()
        empty_response.status_code = 200
        empty_response.json.return_value = {"comments": []}
        client = MagicMock()
        client.get.side_effect = [empty_response]

        with caplog.at_level("WARNING"):
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        shortfall = [r.message for r in caplog.records if "comment.total" in r.message]
        assert shortfall and "deleted mid-fetch" in shortfall[0]
        assert issue_data.get("_comments_incomplete") is True

    def test_shortfall_with_no_issue_key_is_attributed_to_skipped_pagination(self, caplog):
        """A payload without a key never paginates — the WARNING must not
        claim anything happened mid-fetch when no fetch was attempted."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        del issue_data["key"]
        client = _mock_client([])

        with caplog.at_level("WARNING"):
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        client.get.assert_not_called()
        shortfall = [r.message for r in caplog.records if "comment.total" in r.message]
        assert shortfall and "never attempted" in shortfall[0]

    def test_no_warning_when_completion_succeeds(self, caplog):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = _mock_client([_comment_page(0, 100)])

        with caplog.at_level("WARNING"):
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert not any("comment.total" in record.message for record in caplog.records)


class TestPaginationFailureMarksIncomplete:
    """A page-fetch FAILURE must be distinguishable from a completed fetch.

    The incremental transform performs an issue-scoped delete-then-insert on
    the comments parquet, so overlaying a known-truncated list would delete
    previously stored rows. On failure, ``complete_issue_comments`` marks the
    issue with the ``_comments_incomplete`` sidecar key (a sibling of the
    ``_remote_links`` overlay-absent contract) so the transform preserves
    existing rows instead.
    """

    def test_non_200_page_marks_comments_incomplete(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        failing_response = MagicMock()
        failing_response.status_code = 500
        client = MagicMock()
        client.get.side_effect = [failing_response]

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert issue_data.get("_comments_incomplete") is True

    def test_request_error_marks_comments_incomplete(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = httpx.RequestError("connection dropped")

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert issue_data.get("_comments_incomplete") is True

    def test_successful_completion_sets_no_marker(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = _mock_client([_comment_page(0, 100)])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert "_comments_incomplete" not in issue_data

    def test_under_cap_issue_sets_no_marker(self):
        issue_data = _issue_with_comments(total=42, embedded=42)
        client = _mock_client([])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert "_comments_incomplete" not in issue_data

    def test_any_shortfall_marks_incomplete_even_without_a_page_failure(self):
        """An empty page ends the walk without being a page failure, but it
        cannot prove completeness: a mid-walk deletion shifts offsets and can
        hide a LIVE comment below startAt. Any stored < total therefore marks
        the issue incomplete — the incremental transform preserves the stored
        rows and the sidecar schedules a refetch whose consistent snapshot
        either recovers the skipped comment or propagates the real deletion."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        empty_response = MagicMock()
        empty_response.status_code = 200
        empty_response.json.return_value = {"comments": []}
        client = MagicMock()
        client.get.side_effect = [empty_response]

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert issue_data.get("_comments_incomplete") is True

    def test_mid_walk_deletion_that_skips_a_live_comment_marks_incomplete(self):
        """The concrete race: total=250, embed carries c150..c249; page 0
        returns c0..c99, then 50 old comments are deleted so the page at
        startAt=100 serves c150..c249 — live comments c100..c149 are never
        fetched, startAt=200 comes back empty. The short merged list must NOT
        be publishable as complete."""
        issue_data = _issue_with_comments(total=250, embedded=100)
        # page 0 (pre-deletion), page at startAt=100 (post-deletion, offsets
        # shifted: serves what is now items 100..199 = c150..c249), then empty.
        client = _mock_client([_comment_page(0, 100), _comment_page(150, 250), []])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert issue_data.get("_comments_incomplete") is True, (
            "a union stuck below total after an empty page means comments may "
            "have been skipped by offset shift — publishing the short list "
            "would silently drop live rows via the delete-then-insert"
        )


class TestPaginationDeduplicatesByCommentId:
    """The paged walk starts at the thread head while the embed carries the
    newest comments, so the two overlap by design — the same id arrives from
    both requests. Concatenation must de-duplicate by comment id, keep the
    merged thread oldest-first (pages first, embed's unseen tail after), and
    on a duplicate id keep the paged copy — it was fetched after the embed,
    so it is the fresher one."""

    def test_overlapping_page_produces_no_duplicate_ids_and_stays_chronological(self):
        issue_data = _issue_with_comments(total=4, embedded=2)  # embeds c2, c3
        overlap_page = [_comment("c0"), _comment("c1"), _comment("c2")]
        client = _mock_client([overlap_page])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        comments = issue_data["fields"]["comment"]["comments"]
        ids = [c["id"] for c in comments]
        assert ids == ["c0", "c1", "c2", "c3"]

    def test_paged_copy_wins_on_duplicate_id(self):
        issue_data = _issue_with_comments(total=3, embedded=2)  # embeds c1, c2
        embedded_c1 = issue_data["fields"]["comment"]["comments"][0]
        embedded_c1["body"] = {"type": "doc", "content": [], "marker": "embedded"}
        paged_c1 = _comment("c1")
        paged_c1["body"] = {"type": "doc", "content": [], "marker": "paged"}
        client = _mock_client([[_comment("c0"), paged_c1]])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        comments = issue_data["fields"]["comment"]["comments"]
        c1 = next(c for c in comments if c["id"] == "c1")
        assert c1["body"].get("marker") == "paged", (
            "the paged copy is fetched after the embed — a comment edited "
            "between the two requests must keep its fresher body"
        )


class TestPaginationIsWindowPositionAgnostic:
    """The loop stops when the id-deduplicated union of embed + pages reaches
    ``total`` — never on a raw count of fetched items, which silently
    under-fetches when the count includes duplicates of the embed — so
    completion holds regardless of where Jira anchors the embed window."""

    def test_oldest_window_embed_still_completes(self):
        """If Jira ever anchored the embed at the thread head instead, page 0
        duplicates the embed entirely — the union condition keeps paging until
        the newest comments arrive rather than stopping on fetched-item count."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        issue_data["fields"]["comment"]["startAt"] = 0
        issue_data["fields"]["comment"]["comments"] = _comment_page(0, 100)
        client = _mock_client([_comment_page(0, 100), _comment_page(100, 124)])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        ids = [c["id"] for c in issue_data["fields"]["comment"]["comments"]]
        assert ids == [f"c{i}" for i in range(124)]
        offsets = [call.kwargs["params"]["startAt"] for call in client.get.call_args_list]
        assert offsets == [0, 100], "the walk must advance through the endpoint, not re-request page 0"

    def test_endpoint_that_ignores_start_at_terminates_and_marks_incomplete(self, caplog):
        """The union-based stop condition has no intrinsic progress guarantee:
        a server/proxy that serves the SAME non-empty page for every startAt
        would freeze the union below total forever. The walk must cut itself
        off once startAt passes total — bounded requests, marked incomplete —
        instead of looping unboundedly inside a webhook request."""
        issue_data = _issue_with_comments(total=250, embedded=100)
        same_page = MagicMock()
        same_page.status_code = 200
        same_page.json.return_value = {"comments": _comment_page(0, 100)}
        client = MagicMock()
        client.get.return_value = same_page  # identical page for EVERY offset

        with caplog.at_level("WARNING"):
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert client.get.call_count == 3, "startAt 0/100/200, then the guard fires at 300 >= 250"
        assert issue_data.get("_comments_incomplete") is True
        assert any("non-conforming pagination" in r.message for r in caplog.records)


class TestServiceFetchIssuePaginates:
    """The webhook full-refetch path (JiraService.fetch_issue) gets the same completion."""

    def _make_service(self, jira_env):
        from connectors.jira import service as svc

        svc.Config.JIRA_DOMAIN = "mycompany.atlassian.net"
        svc.Config.JIRA_EMAIL = "bot@mycompany.com"
        svc.Config.JIRA_API_TOKEN = "test-token-xyz"
        svc.Config.JIRA_DATA_DIR = jira_env
        svc._jira_service = None
        return svc.JiraService()

    def test_webhook_full_refetch_completes_truncated_comments(self, tmp_path):
        service = self._make_service(tmp_path)

        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = _issue_with_comments(total=124, embedded=100, issue_key="PROJ-777")

        page_response = MagicMock()
        page_response.status_code = 200
        page_response.json.return_value = {"comments": [_comment(f"extra{i}") for i in range(24)]}

        client = MagicMock()
        client.get.side_effect = [issue_response, page_response]
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.service.httpx.Client", return_value=client):
            result = service.fetch_issue("PROJ-777")

        assert result is not None
        assert len(result["fields"]["comment"]["comments"]) == 124
        assert client.get.call_count == 2

    def test_webhook_full_refetch_under_cap_makes_single_call(self, tmp_path):
        service = self._make_service(tmp_path)

        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = _issue_with_comments(total=5, embedded=5, issue_key="PROJ-778")

        client = MagicMock()
        client.get.side_effect = [issue_response]
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.service.httpx.Client", return_value=client):
            result = service.fetch_issue("PROJ-778")

        assert result is not None
        assert len(result["fields"]["comment"]["comments"]) == 5
        client.get.assert_called_once()


class TestBackfillFetchIssuePaginates:
    """The batch/full extract path (JiraBackfill.fetch_issue) gets the same completion."""

    def _make_backfill(self, tmp_path):
        from connectors.jira.scripts.backfill import Config as BackfillConfig
        from connectors.jira.scripts.backfill import JiraBackfill

        config = BackfillConfig(
            jira_domain="mycompany.atlassian.net",
            jira_email="bot@mycompany.com",
            jira_api_token="test-token-xyz",
            data_dir=tmp_path,
        )
        return JiraBackfill(config)

    def test_batch_full_extract_completes_truncated_comments(self, tmp_path):
        backfill = self._make_backfill(tmp_path)

        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = _issue_with_comments(total=124, embedded=100, issue_key="PROJ-999")

        page_response = MagicMock()
        page_response.status_code = 200
        page_response.json.return_value = {"comments": [_comment(f"extra{i}") for i in range(24)]}

        client = MagicMock()
        client.get.side_effect = [issue_response, page_response]
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.scripts.backfill.httpx.Client", return_value=client):
            result = backfill.fetch_issue("PROJ-999")

        assert result is not None
        assert len(result["fields"]["comment"]["comments"]) == 124
        assert client.get.call_count == 2

    def test_batch_full_extract_under_cap_makes_single_call(self, tmp_path):
        backfill = self._make_backfill(tmp_path)

        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = _issue_with_comments(total=3, embedded=3, issue_key="PROJ-1000")

        client = MagicMock()
        client.get.side_effect = [issue_response]
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.scripts.backfill.httpx.Client", return_value=client):
            result = backfill.fetch_issue("PROJ-1000")

        assert result is not None
        assert len(result["fields"]["comment"]["comments"]) == 3
        client.get.assert_called_once()


class TestFullRebuildEmitsPartialComments:
    """A ``_comments_incomplete`` issue must still contribute its partial
    comment list to the batch/full rebuild.

    The preserve-existing-rows contract only makes sense where the write is
    an issue-scoped delete-then-insert onto rows that are already there — the
    incremental path. ``transform_all`` rebuilds the monthly parquets from
    scratch, so returning ``None`` there means the issue contributes ZERO
    comment rows: strictly worse than writing the partially-fetched list the
    JSON already carries.
    """

    def test_transform_comments_emits_partial_list_for_full_rebuild(self):
        issue = _issue_with_comments(total=190, embedded=124)
        issue["_comments_incomplete"] = True

        records = transform_comments(issue, preserve_on_incomplete=False)

        assert records is not None
        assert len(records) == 124

    def test_transform_comments_default_preserves_for_incremental(self):
        """Default (incremental) behaviour is unchanged: None = skip the upsert."""
        issue = _issue_with_comments(total=190, embedded=124)
        issue["_comments_incomplete"] = True

        assert transform_comments(issue) is None

    def test_transform_all_writes_partial_comments_of_marked_issue(self, tmp_path):
        import json as _json

        from connectors.jira.transform import transform_all

        raw_dir = tmp_path / "raw"
        issues_dir = raw_dir / "issues"
        issues_dir.mkdir(parents=True)
        output_dir = tmp_path / "parquet"

        issue = _issue_with_comments(total=190, embedded=124, issue_key="PROJ-500")
        issue["_comments_incomplete"] = True
        issue["fields"]["summary"] = "marked incomplete"
        issue["fields"]["status"] = {"name": "Open"}
        issue["fields"]["issuetype"] = {"name": "Bug"}
        issue["fields"]["attachment"] = []
        issue["fields"]["created"] = "2026-05-15T00:00:00.000+0000"
        issue["fields"]["updated"] = "2026-05-15T00:00:00.000+0000"
        (issues_dir / "PROJ-500.json").write_text(_json.dumps(issue))

        counts = transform_all(raw_dir=raw_dir, output_dir=output_dir)

        assert counts["issues"] == 1
        assert counts["comments"] == 124, (
            "A full rebuild dropped every comment of an issue marked "
            "_comments_incomplete — the partial list in the JSON is strictly "
            "better than nothing when nothing is being preserved"
        )


class TestBackfillRefetchesIncompleteJson:
    """``--skip-existing`` must not make the marker permanent.

    ``process_issue`` skips any issue whose JSON already exists, so an issue
    marked ``_comments_incomplete`` by a failed pagination would never be
    re-fetched and never heal.
    """

    def _make_backfill(self, tmp_path):
        from connectors.jira.scripts.backfill import Config as BackfillConfig
        from connectors.jira.scripts.backfill import JiraBackfill

        config = BackfillConfig(
            jira_domain="mycompany.atlassian.net",
            jira_email="bot@mycompany.com",
            jira_api_token="test-token-xyz",
            data_dir=tmp_path,
        )
        return JiraBackfill(config)

    def _write_existing(self, backfill, issue_key: str, payload: dict) -> None:
        import json as _json

        backfill.issues_dir.mkdir(parents=True, exist_ok=True)
        (backfill.issues_dir / f"{issue_key}.json").write_text(_json.dumps(payload))

    def test_marked_json_is_refetched_under_skip_existing(self, tmp_path):
        backfill = self._make_backfill(tmp_path)
        marked = _issue_with_comments(total=190, embedded=124, issue_key="PROJ-600")
        marked["_comments_incomplete"] = True
        self._write_existing(backfill, "PROJ-600", marked)
        # The sidecar marker save_issue leaves next to a JSON whose
        # _comments_incomplete is True — _needs_refetch is now a stat on
        # this file, not a json.load() of the JSON body (Devin Review on
        # #1283, finding 3).
        (backfill.issues_dir / "PROJ-600.json.incomplete").touch()

        healed = _issue_with_comments(total=190, embedded=190, issue_key="PROJ-600")
        with (
            patch.object(backfill, "fetch_issue", return_value=healed) as fetch,
            patch.object(backfill, "fetch_remote_links", return_value=[]),
            patch.object(backfill, "download_issue_attachments", return_value=0),
        ):
            ok = backfill.process_issue("PROJ-600", skip_existing=True)

        assert ok is True
        fetch.assert_called_once_with("PROJ-600")
        assert backfill.stats["skipped"] == 0
        assert not (backfill.issues_dir / "PROJ-600.json.incomplete").exists(), (
            "healing the issue (re-fetch with a complete comment set) must clear "
            "the sidecar marker, or a later run would refetch it forever"
        )

    def test_unmarked_json_is_still_skipped(self, tmp_path):
        backfill = self._make_backfill(tmp_path)
        self._write_existing(backfill, "PROJ-601", _issue_with_comments(total=3, embedded=3, issue_key="PROJ-601"))

        with patch.object(backfill, "fetch_issue") as fetch:
            ok = backfill.process_issue("PROJ-601", skip_existing=True)

        assert ok is True
        fetch.assert_not_called()
        assert backfill.stats["skipped"] == 1

    def test_unreadable_json_is_refetched(self, tmp_path):
        """A JSON we cannot parse is not evidence the issue was downloaded."""
        backfill = self._make_backfill(tmp_path)
        backfill.issues_dir.mkdir(parents=True, exist_ok=True)
        (backfill.issues_dir / "PROJ-602.json").write_text("{ truncated")

        healed = _issue_with_comments(total=2, embedded=2, issue_key="PROJ-602")
        with (
            patch.object(backfill, "fetch_issue", return_value=healed) as fetch,
            patch.object(backfill, "fetch_remote_links", return_value=[]),
            patch.object(backfill, "download_issue_attachments", return_value=0),
        ):
            ok = backfill.process_issue("PROJ-602", skip_existing=True)

        assert ok is True
        fetch.assert_called_once_with("PROJ-602")


class TestCommentPaginationHonoursRetryAfter:
    """429 on a comment page is the likeliest failure in the batch path
    (parallel workers, an extra request per >100-comment issue). Without a
    retry, rate limiting routinely leaves issues marked incomplete. Mirror
    the surrounding fetchers: honour ``Retry-After``, bounded."""

    def _rate_limited(self, retry_after: str = "3") -> MagicMock:
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": retry_after}
        return resp

    def _page(self, comments: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"comments": comments}
        return resp

    def test_retries_after_429_and_completes(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [
            self._rate_limited("3"),
            self._page([_comment(f"extra{i}") for i in range(24)]),
        ]

        with patch("connectors.jira.service.time.sleep") as sleep:
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert len(issue_data["fields"]["comment"]["comments"]) == 124
        assert "_comments_incomplete" not in issue_data
        sleep.assert_called_once_with(3)

    def test_retry_is_bounded_and_marks_incomplete(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [self._rate_limited("1") for _ in range(20)]

        with patch("connectors.jira.service.time.sleep"):
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert issue_data.get("_comments_incomplete") is True
        assert client.get.call_count <= 5, "429 retry is unbounded"

    def test_unparseable_retry_after_falls_back_to_a_default_wait(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [
            self._rate_limited("Wed, 21 Oct 2026 07:28:00 GMT"),
            self._page([_comment(f"extra{i}") for i in range(24)]),
        ]

        with patch("connectors.jira.service.time.sleep") as sleep:
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert len(issue_data["fields"]["comment"]["comments"]) == 124
        sleep.assert_called_once()
        assert sleep.call_args.args[0] > 0

    def test_retry_after_wait_is_capped(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [
            self._rate_limited("86400"),
            self._page([_comment(f"extra{i}") for i in range(24)]),
        ]

        with patch("connectors.jira.service.time.sleep") as sleep:
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert sleep.call_args.args[0] <= 300, "an hours-long Retry-After stalls the whole worker"


class TestCommentPaginationCallerSuppliedRetryBudget:
    """``complete_issue_comments`` must accept a caller-supplied retry budget
    so the webhook path (an active FastAPI request) can pass a much smaller
    one than the batch path's generous default — the 429 retry loop's
    ``time.sleep()`` (up to 3 x 300s) otherwise blocks whatever thread runs
    it for up to 15 minutes. (Devin Review on #1283)
    """

    def _rate_limited(self, retry_after: str = "3") -> MagicMock:
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": retry_after}
        return resp

    def test_max_retries_zero_marks_incomplete_on_first_429_without_sleeping(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [self._rate_limited("3")]

        with patch("connectors.jira.service.time.sleep") as sleep:
            complete_issue_comments(issue_data, BASE_URL, AUTH, client, max_retries=0)

        sleep.assert_not_called()
        assert issue_data.get("_comments_incomplete") is True
        assert client.get.call_count == 1

    def test_max_retries_omitted_keeps_the_module_default_budget(self):
        """Existing callers (backfill.py, direct calls) that don't pass
        max_retries must see today's unchanged, generous retry count."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [self._rate_limited("1") for _ in range(20)]

        with patch("connectors.jira.service.time.sleep"):
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert issue_data.get("_comments_incomplete") is True
        assert client.get.call_count <= 5, "omitting max_retries changed the default budget"

    def test_max_retries_positive_bound_is_honoured(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [self._rate_limited("0") for _ in range(20)]

        with patch("connectors.jira.service.time.sleep"):
            complete_issue_comments(issue_data, BASE_URL, AUTH, client, max_retries=1)

        assert issue_data.get("_comments_incomplete") is True
        assert client.get.call_count == 2, "max_retries=1 should allow exactly one retry (2 attempts total)"


class TestWebhookFetchIssueUsesAZeroCommentRetryBudget:
    """``process_webhook_event``'s full-refetch runs off an active FastAPI
    request. It must pass a zero comment-retry budget to ``fetch_issue``,
    relying on the ``_comments_incomplete`` marker plus a later backfill
    heal (``_needs_refetch``) rather than sleeping in-request. (Devin
    Review on #1283)
    """

    def _make_service(self, tmp_path):
        from connectors.jira import service as svc

        svc.Config.JIRA_DOMAIN = "mycompany.atlassian.net"
        svc.Config.JIRA_EMAIL = "bot@mycompany.com"
        svc.Config.JIRA_API_TOKEN = "test-token-xyz"
        svc.Config.JIRA_DATA_DIR = tmp_path
        svc._jira_service = None
        return svc.JiraService()

    def test_fetch_issue_forwards_comment_max_retries_to_complete_issue_comments(self, tmp_path):
        service = self._make_service(tmp_path)

        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = _issue_with_comments(total=124, embedded=100, issue_key="PROJ-801")

        client = MagicMock()
        client.get.side_effect = [issue_response]
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        with (
            patch("connectors.jira.service.httpx.Client", return_value=client),
            patch("connectors.jira.service.complete_issue_comments") as mock_complete,
        ):
            service.fetch_issue("PROJ-801", comment_max_retries=0)

        assert mock_complete.call_args.kwargs.get("max_retries") == 0

    def test_webhook_path_does_not_sleep_on_a_rate_limited_comment_page(self, tmp_path):
        service = self._make_service(tmp_path)

        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = _issue_with_comments(total=124, embedded=100, issue_key="PROJ-800")

        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "3"}

        client = MagicMock()
        client.get.side_effect = [issue_response, rate_limited]
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        saved: dict = {}

        def _capture(issue_data):
            saved["payload"] = issue_data
            return tmp_path / "issues" / "x.json"

        with (
            patch("connectors.jira.service.httpx.Client", return_value=client),
            patch.object(service, "save_issue", side_effect=_capture),
            patch("connectors.jira.service.time.sleep") as sleep,
        ):
            ok = service.process_webhook_event({"webhookEvent": "jira:issue_updated", "issue": {"key": "PROJ-800"}})

        assert ok is True
        sleep.assert_not_called()
        assert saved["payload"].get("_comments_incomplete") is True


class TestBackfillFetchIssueRateLimitRetryIsBounded:
    """Before this PR, the 429 branch lived AFTER the ``with
    httpx.Client(...)`` block; the PR moved the 200 branch inside it (so it
    could hand ``client`` to ``complete_issue_comments``) and left the 429
    branch's recursive retry inside too — so N consecutive 429s held N open
    httpx.Client instances plus N stack frames, unbounded (no retry cap on
    this path). Fixed by converting the recursion into a bounded loop where
    only a successful response is read inside ``with``; the retry sleeps
    OUTSIDE a closed client. (Devin Review on #1283)
    """

    def _make_backfill(self, tmp_path):
        from connectors.jira.scripts.backfill import Config as BackfillConfig
        from connectors.jira.scripts.backfill import JiraBackfill

        config = BackfillConfig(
            jira_domain="mycompany.atlassian.net",
            jira_email="bot@mycompany.com",
            jira_api_token="test-token-xyz",
            data_dir=tmp_path,
        )
        return JiraBackfill(config)

    def _rate_limited(self, retry_after: str = "0") -> MagicMock:
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": retry_after}
        return resp

    def _ok_response(self, issue_key: str) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _issue_with_comments(total=1, embedded=1, issue_key=issue_key)
        return resp

    def test_retry_is_bounded_and_gives_up_instead_of_recursing_forever(self, tmp_path):
        backfill = self._make_backfill(tmp_path)

        with (
            patch("connectors.jira.scripts.backfill.httpx.Client") as mock_client_cls,
            patch("connectors.jira.scripts.backfill.time.sleep") as sleep,
        ):
            entered_client = mock_client_cls.return_value.__enter__.return_value
            entered_client.get.side_effect = [self._rate_limited() for _ in range(20)]

            result = backfill.fetch_issue("PROJ-RL")

        assert result is None
        assert sleep.call_count == backfill.ISSUE_FETCH_RATE_LIMIT_RETRIES, (
            "429 retry on the issue fetch must be bounded, not recurse forever"
        )

    def test_each_retry_closes_its_client_before_the_next_attempt_opens(self, tmp_path):
        """The pre-fix recursion opened client N+1 while client N's ``with``
        block (and thus its ``__exit__``) had not yet run. A correctly
        bounded loop closes each client before sleeping and opening the
        next one."""
        backfill = self._make_backfill(tmp_path)
        events: list[tuple] = []
        responses = [self._rate_limited(), self._rate_limited(), self._ok_response("PROJ-RL2")]
        counter = {"n": 0}

        class _FakeClient:
            def __init__(self, index):
                self.index = index

            def __enter__(self):
                events.append(("enter", self.index))
                return self

            def __exit__(self, *exc):
                events.append(("exit", self.index))
                return False

            def get(self, *a, **kw):
                return responses[self.index]

        def _make_client(*a, **kw):
            client = _FakeClient(counter["n"])
            counter["n"] += 1
            return client

        with (
            patch("connectors.jira.scripts.backfill.httpx.Client", side_effect=_make_client),
            patch("connectors.jira.scripts.backfill.time.sleep"),
            patch("connectors.jira.scripts.backfill.complete_issue_comments"),
        ):
            result = backfill.fetch_issue("PROJ-RL2")

        assert result is not None
        assert events == [("enter", 0), ("exit", 0), ("enter", 1), ("exit", 1), ("enter", 2), ("exit", 2)], (
            "a client must be fully closed before the next retry attempt opens a new one "
            "— the pre-fix recursion nested them instead"
        )

    def test_eventual_success_after_retries_returns_the_issue(self, tmp_path):
        backfill = self._make_backfill(tmp_path)

        with (
            patch("connectors.jira.scripts.backfill.httpx.Client") as mock_client_cls,
            patch("connectors.jira.scripts.backfill.time.sleep"),
            patch("connectors.jira.scripts.backfill.complete_issue_comments"),
        ):
            entered_client = mock_client_cls.return_value.__enter__.return_value
            entered_client.get.side_effect = [
                self._rate_limited(),
                self._rate_limited(),
                self._ok_response("PROJ-RL3"),
            ]

            result = backfill.fetch_issue("PROJ-RL3")

        assert result is not None
        assert result["key"] == "PROJ-RL3"


class TestNeedsRefetchSidecarMarker:
    """``_needs_refetch`` must decide via a cheap stat (a sidecar marker),
    not ``open()`` + ``json.load()`` on every existing file — at six-figure
    issue counts with hundreds-of-KB ``fields=*all`` payloads, doing so on
    every ``--skip-existing`` run is tens of GB of reads/parses just to
    skip files. (Devin Review on #1283)
    """

    def _make_backfill(self, tmp_path):
        from connectors.jira.scripts.backfill import Config as BackfillConfig
        from connectors.jira.scripts.backfill import JiraBackfill

        config = BackfillConfig(
            jira_domain="mycompany.atlassian.net",
            jira_email="bot@mycompany.com",
            jira_api_token="test-token-xyz",
            data_dir=tmp_path,
        )
        return JiraBackfill(config)

    def test_sidecar_present_means_refetch_without_reading_json_body(self, tmp_path):
        from connectors.jira.scripts.backfill import _needs_refetch

        json_path = tmp_path / "PROJ-1.json"
        json_path.write_text("this is not valid json at all")
        (tmp_path / "PROJ-1.json.incomplete").touch()

        assert _needs_refetch(json_path) is True

    def test_sidecar_absent_and_well_formed_json_means_no_refetch(self, tmp_path):
        import json as _json

        from connectors.jira.scripts.backfill import _needs_refetch

        json_path = tmp_path / "PROJ-2.json"
        json_path.write_text(_json.dumps({"key": "PROJ-2"}))

        assert _needs_refetch(json_path) is False

    def test_needs_refetch_does_not_parse_the_json_body(self, tmp_path, monkeypatch):
        """The whole point: the skip-existing check must cost a stat, not a parse."""
        import json as _json

        from connectors.jira.scripts import backfill as backfill_module

        json_path = tmp_path / "PROJ-3.json"
        json_path.write_text(_json.dumps({"key": "PROJ-3", "fields": {"summary": "x" * 10_000}}))

        called = {"n": 0}
        real_load = backfill_module.json.load

        def spy_load(*a, **kw):
            called["n"] += 1
            return real_load(*a, **kw)

        monkeypatch.setattr(backfill_module.json, "load", spy_load)

        assert backfill_module._needs_refetch(json_path) is False
        assert called["n"] == 0, "a stat-based skip check must not parse the JSON body"

    def test_empty_json_file_means_refetch(self, tmp_path):
        from connectors.jira.scripts.backfill import _needs_refetch

        json_path = tmp_path / "PROJ-4.json"
        json_path.write_text("")

        assert _needs_refetch(json_path) is True

    def test_save_issue_writes_sidecar_when_comments_incomplete(self, tmp_path):
        backfill = self._make_backfill(tmp_path)
        issue = _issue_with_comments(total=190, embedded=124, issue_key="PROJ-5")
        issue["_comments_incomplete"] = True

        backfill.save_issue(issue)

        assert (backfill.issues_dir / "PROJ-5.json.incomplete").exists()

    def test_save_issue_clears_sidecar_when_comments_now_complete(self, tmp_path):
        backfill = self._make_backfill(tmp_path)
        backfill.issues_dir.mkdir(parents=True, exist_ok=True)
        (backfill.issues_dir / "PROJ-6.json.incomplete").touch()

        healed = _issue_with_comments(total=3, embedded=3, issue_key="PROJ-6")
        backfill.save_issue(healed)

        assert not (backfill.issues_dir / "PROJ-6.json.incomplete").exists()

    def test_backfill_marker_failure_does_not_fail_the_saved_issue(self, tmp_path):
        """Sibling of the webhook-path guard: the JSON is already written, so
        an OSError from the marker sync must not make save_issue report the
        issue as failed."""
        backfill = self._make_backfill(tmp_path)
        backfill.issues_dir.mkdir(parents=True, exist_ok=True)
        issue = _issue_with_comments(total=3, embedded=3, issue_key="PROJ-7")

        with patch(
            "connectors.jira.scripts.backfill._sync_incomplete_marker",
            side_effect=OSError("permission denied"),
        ):
            saved = backfill.save_issue(issue)

        assert saved is not None
        assert (backfill.issues_dir / "PROJ-7.json").exists()


class TestWebhookSaveIssueSyncsSidecarMarker:
    """``JiraService.save_issue`` (the webhook path) must keep the sidecar
    marker in sync exactly like ``JiraBackfill.save_issue`` does — both save
    paths write the SAME ``issues/{key}.json`` files, and ``_needs_refetch``
    is a stat on the sidecar only. Without this, a webhook refetch that
    failed mid-pagination (one 429 under the zero in-request retry budget)
    persists a truncated JSON that every ``--skip-existing`` backfill skips
    forever, and a webhook save could also leave a stale sidecar behind after
    healing an issue the backfill had marked."""

    def _make_service(self, tmp_path):
        from connectors.jira import service as svc

        svc.Config.JIRA_DOMAIN = "mycompany.atlassian.net"
        svc.Config.JIRA_EMAIL = "bot@mycompany.com"
        svc.Config.JIRA_API_TOKEN = "test-token-xyz"
        svc.Config.JIRA_DATA_DIR = tmp_path
        svc._jira_service = None
        return svc.JiraService()

    def _save(self, service, issue):
        with (
            patch("connectors.jira.service.trigger_incremental_transform", return_value=True),
            patch.object(service, "fetch_remote_links", return_value=[]),
            patch.object(service, "fetch_refresh_fields", return_value=None),
            patch.object(service, "download_all_attachments", return_value=[]),
        ):
            return service.save_issue(issue)

    def test_incomplete_issue_writes_sidecar(self, tmp_path):
        service = self._make_service(tmp_path)
        issue = _issue_with_comments(total=190, embedded=124, issue_key="PROJ-900")
        issue["_comments_incomplete"] = True

        saved = self._save(service, issue)

        assert saved is not None
        assert (tmp_path / "issues" / "PROJ-900.json.incomplete").exists(), (
            "a webhook save of an incomplete fetch must be visible to the "
            "backfill's --skip-existing stat, or the issue only heals on "
            "further webhook activity"
        )

    def test_complete_issue_clears_stale_sidecar(self, tmp_path):
        service = self._make_service(tmp_path)
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir(parents=True, exist_ok=True)
        (issues_dir / "PROJ-901.json.incomplete").touch()

        healed = _issue_with_comments(total=3, embedded=3, issue_key="PROJ-901")
        saved = self._save(service, healed)

        assert saved is not None
        assert not (issues_dir / "PROJ-901.json.incomplete").exists(), (
            "a complete webhook save must clear the marker, or the next "
            "--skip-existing backfill refetches a healed issue forever"
        )

    def test_marker_failure_does_not_abort_the_publish(self, tmp_path):
        """The marker only schedules a heal — best-effort bookkeeping. An
        OSError from it (e.g. a marker file owned by the backfill's OS user
        that the webhook process cannot touch) must not abort the save or
        skip the parquet transform: the JSON is already replaced."""
        service = self._make_service(tmp_path)
        issue = _issue_with_comments(total=3, embedded=3, issue_key="PROJ-902")

        with (
            patch("connectors.jira.service.trigger_incremental_transform", return_value=True) as transform,
            patch.object(service, "fetch_remote_links", return_value=[]),
            patch.object(service, "fetch_refresh_fields", return_value=None),
            patch.object(service, "download_all_attachments", return_value=[]),
            patch(
                "connectors.jira.service._sync_incomplete_marker",
                side_effect=OSError("permission denied"),
            ),
        ):
            saved = service.save_issue(issue)

        assert saved is not None, "a marker failure must not turn a successful save into a failure"
        transform.assert_called_once()
        assert (tmp_path / "issues" / "PROJ-902.json").exists()


class TestDryRunCountsMatchRealSkipDecision:
    """``--dry-run``'s "already downloaded" counters must use the same
    decision ``process_issue`` makes under ``--skip-existing`` (marker
    aware), not a bare ``.exists()`` — otherwise a run with
    ``_comments_incomplete``-marked issues under-reports how many issues a
    real run would actually re-fetch. (Devin Review on #1283)
    """

    def test_marked_issue_is_not_counted_as_already_downloaded(self, tmp_path):
        import json as _json

        from connectors.jira.scripts.backfill import _count_already_downloaded

        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()
        (issues_dir / "PROJ-1.json").write_text(_json.dumps({"key": "PROJ-1"}))
        (issues_dir / "PROJ-2.json").write_text(_json.dumps({"key": "PROJ-2"}))
        (issues_dir / "PROJ-2.json.incomplete").touch()

        existing = _count_already_downloaded(issues_dir, ["PROJ-1", "PROJ-2", "PROJ-3"])

        assert existing == 1, "the marked (incomplete) issue must not count as already downloaded"

    def test_missing_issue_is_not_counted(self, tmp_path):
        import json as _json

        from connectors.jira.scripts.backfill import _count_already_downloaded

        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()
        (issues_dir / "PROJ-1.json").write_text(_json.dumps({"key": "PROJ-1"}))

        existing = _count_already_downloaded(issues_dir, ["PROJ-1", "PROJ-404"])

        assert existing == 1


class TestWebhookFallbackPayloadIsNotAuthoritative:
    """When ``fetch_issue`` returns None the webhook's embedded issue payload
    is used as-is. It never passes through ``complete_issue_comments``, so its
    ``fields.comment.comments`` is whatever Jira chose to embed — and the
    incremental delete-then-insert would replace a complete stored thread with
    that shorter list. Mark it incomplete unless it demonstrably carries the
    whole thread."""

    def _make_service(self, tmp_path):
        from connectors.jira import service as svc

        svc.Config.JIRA_DOMAIN = "mycompany.atlassian.net"
        svc.Config.JIRA_EMAIL = "bot@mycompany.com"
        svc.Config.JIRA_API_TOKEN = "test-token-xyz"
        svc.Config.JIRA_DATA_DIR = tmp_path
        svc._jira_service = None
        return svc.JiraService()

    def _run_fallback(self, tmp_path, embedded_issue):
        service = self._make_service(tmp_path)
        saved: dict = {}

        def _capture(issue_data):
            saved["payload"] = issue_data
            return tmp_path / "issues" / "x.json"

        with (
            patch.object(service, "fetch_issue", return_value=None),
            patch.object(service, "save_issue", side_effect=_capture),
        ):
            ok = service.process_webhook_event({"webhookEvent": "jira:issue_updated", "issue": embedded_issue})
        assert ok is True
        return saved["payload"]

    def test_short_embedded_thread_is_marked_incomplete(self, tmp_path):
        embedded = _issue_with_comments(total=190, embedded=3, issue_key="PROJ-700")

        payload = self._run_fallback(tmp_path, embedded)

        assert payload.get("_comments_incomplete") is True

    def test_complete_embedded_thread_is_not_marked(self, tmp_path):
        embedded = _issue_with_comments(total=3, embedded=3, issue_key="PROJ-701")

        payload = self._run_fallback(tmp_path, embedded)

        assert "_comments_incomplete" not in payload

    def test_complete_thread_whose_comments_lack_the_visibility_flag_is_applied(self, tmp_path):
        """Completeness is about the THREAD, not about the fields on one comment.

        v0.83.70 briefly widened it: a comment with no boolean `jsdPublic` marked
        the whole issue incomplete, because the delete-then-insert would replace
        an already-observed `public_visibility` with NULL. That protection now
        lives at the write layer — `_comment_records` carries a stored
        same-version value forward — so the update can be applied here instead of
        deferred to the next successful refetch. The value half is pinned
        end-to-end in `tests/test_jira_comment_visibility.py`.
        """
        embedded = {
            "key": "PROJ-703",
            "id": "10003",
            "fields": {
                "comment": {
                    "total": 2,
                    "comments": [_comment("c0"), _comment("c1", jsd_public=None)],
                }
            },
        }

        payload = self._run_fallback(tmp_path, embedded)

        assert "_comments_incomplete" not in payload

    def test_payload_without_comment_field_is_marked_incomplete(self, tmp_path):
        embedded = {"key": "PROJ-702", "id": "10002", "fields": {"summary": "no comment field"}}

        payload = self._run_fallback(tmp_path, embedded)

        assert payload.get("_comments_incomplete") is True, (
            "A payload carrying no comment field at all is not evidence the issue "
            "has no comments — the transform would delete the stored thread"
        )
