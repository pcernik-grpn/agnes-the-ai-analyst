"""Full tests for the Jira service (JiraService.process_webhook_event and friends)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers.factories import WebhookEventFactory

from connectors.jira.scripts.consistency_check import JiraConsistencyChecker
from connectors.jira.service import JiraFetchError
from connectors.jira.transform import transform_remote_links


@pytest.fixture
def jira_env(tmp_path, monkeypatch):
    """Set up a Jira environment with required dirs and env vars."""
    data_dir = tmp_path / "jira_data"
    data_dir.mkdir()
    (data_dir / "issues").mkdir()

    monkeypatch.setenv("JIRA_DOMAIN", "mycompany.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@mycompany.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-token-xyz")
    monkeypatch.setenv("JIRA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "webhook-secret-123")

    return data_dir


def _make_jira_service(jira_env):
    """Create a fresh JiraService with test configuration."""
    from connectors.jira import service as svc

    svc.Config.JIRA_DOMAIN = "mycompany.atlassian.net"
    svc.Config.JIRA_EMAIL = "bot@mycompany.com"
    svc.Config.JIRA_API_TOKEN = "test-token-xyz"
    svc.Config.JIRA_DATA_DIR = jira_env
    svc.Config.JIRA_WEBHOOK_SECRET = "webhook-secret-123"
    svc._jira_service = None
    return svc.JiraService()


def _fake_issue_data(issue_key: str = "TEST-1") -> dict:
    return {
        "key": issue_key,
        "id": "10001",
        "fields": {
            "summary": "Test issue summary",
            "status": {"name": "Open"},
            "issuetype": {"name": "Bug"},
            "attachment": [],
            "comment": {"comments": []},
        },
    }


class TestJiraServiceWebhookProcessing:
    def test_process_issue_updated_calls_fetch_and_save(self, jira_env):
        """process_webhook_event for issue_updated fetches fresh data from API."""
        service = _make_jira_service(jira_env)
        event_data, _, _ = WebhookEventFactory.issue_updated("PROJ-100")
        issue_data = _fake_issue_data("PROJ-100")

        with (
            patch.object(service, "fetch_issue", return_value=issue_data),
            patch.object(service, "fetch_remote_links", return_value=[]),
            patch.object(service, "fetch_refresh_fields", return_value=None),
            patch.object(service, "download_all_attachments", return_value=[]),
            patch("connectors.jira.service.trigger_incremental_transform", return_value=True),
        ):
            result = service.process_webhook_event(event_data)

        assert result is True
        saved_file = jira_env / "issues" / "PROJ-100.json"
        assert saved_file.exists()
        with open(saved_file) as f:
            saved = json.load(f)
        assert saved["key"] == "PROJ-100"

    def test_process_issue_deleted_marks_file(self, jira_env):
        """process_webhook_event for issue_deleted marks existing JSON with _deleted_at."""
        service = _make_jira_service(jira_env)

        # Pre-create the issue JSON
        issue_file = jira_env / "issues" / "PROJ-200.json"
        issue_file.write_text(json.dumps({"key": "PROJ-200", "fields": {}}))

        event_data, _, _ = WebhookEventFactory.issue_deleted("PROJ-200")

        with patch("connectors.jira.service.trigger_incremental_transform", return_value=True):
            result = service.process_webhook_event(event_data)

        assert result is True
        with open(issue_file) as f:
            saved = json.load(f)
        assert "_deleted_at" in saved

    def test_a_comment_deleted_event_does_not_tombstone_the_whole_issue(self, jira_env):
        """`comment_deleted` is an ordinary content change, not an issue deletion.

        The routing used to match any ``webhookEvent`` containing "deleted", so a
        single deleted comment stamped `_deleted_at` on the issue and dropped its
        rows from all six parquet tables. Not permanent — `save_issue` replaces
        the stored JSON wholesale, so the next event for that issue rebuilt it —
        but an issue that then went quiet stayed missing until a backfill, with
        nothing logged to say why (Devin on #1435).

        The correct handling is the ordinary path: refetch the issue, whose
        comment thread no longer contains the deleted comment.
        """
        service = _make_jira_service(jira_env)
        issue_file = jira_env / "issues" / "PROJ-201.json"
        issue_file.write_text(json.dumps({"key": "PROJ-201", "fields": {}}))

        event_data = {
            "webhookEvent": "comment_deleted",
            "issue": {"key": "PROJ-201", "id": "10003", "fields": {"summary": "still here"}},
        }
        with (
            patch("connectors.jira.service.trigger_incremental_transform", return_value=True),
            patch.object(service, "fetch_issue", return_value={"key": "PROJ-201", "fields": {"summary": "refetched"}}),
            patch.object(service, "fetch_remote_links", return_value=[]),
            patch.object(service, "fetch_refresh_fields", return_value=None),
            patch.object(service, "download_all_attachments", return_value=[]),
        ):
            result = service.process_webhook_event(event_data)

        assert result is True
        saved = json.loads(issue_file.read_text())
        assert "_deleted_at" not in saved, "a deleted COMMENT tombstoned the whole issue"
        assert saved["fields"]["summary"] == "refetched", "the issue was not refetched and re-saved"

    def test_an_attachment_deleted_event_does_not_tombstone_the_whole_issue(self, jira_env):
        service = _make_jira_service(jira_env)
        issue_file = jira_env / "issues" / "PROJ-202.json"
        issue_file.write_text(json.dumps({"key": "PROJ-202", "fields": {}}))

        with (
            patch("connectors.jira.service.trigger_incremental_transform", return_value=True),
            patch.object(service, "fetch_issue", return_value={"key": "PROJ-202", "fields": {}}),
            patch.object(service, "fetch_remote_links", return_value=[]),
            patch.object(service, "fetch_refresh_fields", return_value=None),
            patch.object(service, "download_all_attachments", return_value=[]),
        ):
            service.process_webhook_event(
                {"webhookEvent": "attachment_deleted", "issue": {"key": "PROJ-202", "id": "1", "fields": {}}}
            )

        assert "_deleted_at" not in json.loads(issue_file.read_text())

    def test_a_bare_issue_deleted_spelling_still_tombstones(self, jira_env):
        """The allowlist carries both spellings on purpose: failing to tombstone a
        REAL issue deletion is the permanent direction — deletion webhooks fire
        once and nothing ever re-deletes the row — while tombstoning too eagerly
        only costs rows until the issue's next event."""
        service = _make_jira_service(jira_env)
        issue_file = jira_env / "issues" / "PROJ-203.json"
        issue_file.write_text(json.dumps({"key": "PROJ-203", "fields": {}}))

        with patch("connectors.jira.service.trigger_incremental_transform", return_value=True):
            result = service.process_webhook_event(
                {"webhookEvent": "issue_deleted", "issue": {"key": "PROJ-203", "id": "1", "fields": {}}}
            )

        assert result is True
        assert "_deleted_at" in json.loads(issue_file.read_text())

    def test_process_missing_issue_key_returns_false(self, jira_env):
        """Webhook event without issue key returns False."""
        service = _make_jira_service(jira_env)
        result = service.process_webhook_event({"webhookEvent": "jira:issue_updated"})
        assert result is False

    def test_process_uses_embedded_data_when_fetch_fails(self, jira_env):
        """Falls back to embedded issue data in webhook payload when API fetch fails."""
        service = _make_jira_service(jira_env)
        event_data = {
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": "PROJ-300",
                "id": "10003",
                "fields": {
                    "summary": "Embedded issue",
                    "attachment": [],
                    "comment": {"comments": []},
                },
            },
        }

        with (
            patch.object(service, "fetch_issue", return_value=None),
            patch.object(service, "fetch_remote_links", return_value=[]),
            patch.object(service, "fetch_refresh_fields", return_value=None),
            patch.object(service, "download_all_attachments", return_value=[]),
            patch("connectors.jira.service.trigger_incremental_transform", return_value=True),
        ):
            result = service.process_webhook_event(event_data)

        assert result is True
        saved_file = jira_env / "issues" / "PROJ-300.json"
        assert saved_file.exists()

    def test_deletion_of_nonexistent_issue_returns_true(self, jira_env):
        """Deleting an issue that has no local file returns True (idempotent)."""
        service = _make_jira_service(jira_env)
        event_data, _, _ = WebhookEventFactory.issue_deleted("PROJ-99999")

        result = service.process_webhook_event(event_data)
        assert result is True

    def test_fetch_issue_returns_none_on_404(self, jira_env):
        """fetch_issue returns None when Jira returns 404."""

        service = _make_jira_service(jira_env)

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.service.httpx.Client", return_value=mock_client):
            result = service.fetch_issue("PROJ-MISSING")

        assert result is None

    def test_fetch_issue_returns_data_on_200(self, jira_env):
        """fetch_issue returns parsed JSON on HTTP 200."""
        service = _make_jira_service(jira_env)
        issue_data = _fake_issue_data("PROJ-42")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = issue_data
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.service.httpx.Client", return_value=mock_client):
            result = service.fetch_issue("PROJ-42")

        assert result is not None
        assert result["key"] == "PROJ-42"

    def test_webhook_event_factory_signature_verification(self):
        """WebhookEventFactory produces correct HMAC-SHA256 signatures."""
        import hashlib
        import hmac

        secret = "test-secret"
        event_data, payload, sig = WebhookEventFactory.issue_updated("TEST-1", secret)

        expected_mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        assert sig == f"sha256={expected_mac}"


class TestFetchRemoteLinks:
    """fetch_remote_links must raise on auth/server failure to prevent the
    save_issue overlay from writing [] into cached JSON, which downstream
    would interpret as 'delete existing remote_links rows for this issue'."""

    def _mock_http(self, status_code, json_body=None):
        """Build a MagicMock httpx.Client context manager returning a fixed response."""
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = json_body or []
        client = MagicMock()
        client.get.return_value = response
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)
        return client

    def test_returns_list_on_200(self, jira_env):
        service = _make_jira_service(jira_env)
        with patch("connectors.jira.service.httpx.Client", return_value=self._mock_http(200, [{"id": "1"}])):
            assert service.fetch_remote_links("PROJ-1") == [{"id": "1"}]

    def test_returns_empty_on_404(self, jira_env):
        service = _make_jira_service(jira_env)
        with patch("connectors.jira.service.httpx.Client", return_value=self._mock_http(404)):
            assert service.fetch_remote_links("PROJ-1") == []

    def test_raises_on_401(self, jira_env):
        service = _make_jira_service(jira_env)
        with patch("connectors.jira.service.httpx.Client", return_value=self._mock_http(401)):
            with pytest.raises(JiraFetchError, match="auth"):
                service.fetch_remote_links("PROJ-1")

    def test_raises_on_403(self, jira_env):
        service = _make_jira_service(jira_env)
        with patch("connectors.jira.service.httpx.Client", return_value=self._mock_http(403)):
            with pytest.raises(JiraFetchError, match="auth"):
                service.fetch_remote_links("PROJ-1")

    def test_raises_on_500(self, jira_env):
        service = _make_jira_service(jira_env)
        with patch("connectors.jira.service.httpx.Client", return_value=self._mock_http(500)):
            with pytest.raises(JiraFetchError, match="server"):
                service.fetch_remote_links("PROJ-1")

    def test_raises_on_429_rate_limit(self, jira_env):
        # 429 in the webhook hot path must raise (not silently return []).
        # A webhook burst hitting Jira's rate limiter is the most likely
        # production scenario; returning [] would re-trigger the wipe bug.
        service = _make_jira_service(jira_env)
        with patch("connectors.jira.service.httpx.Client", return_value=self._mock_http(429)):
            with pytest.raises(JiraFetchError, match="rate limited"):
                service.fetch_remote_links("PROJ-1")

    def test_raises_on_unexpected_status(self, jira_env):
        # Any non-success/non-404 status raises — covers 400, 405, 418, etc.
        # No silent fall-through.
        service = _make_jira_service(jira_env)
        with patch("connectors.jira.service.httpx.Client", return_value=self._mock_http(418)):
            with pytest.raises(JiraFetchError, match="unexpected status"):
                service.fetch_remote_links("PROJ-1")

    def test_raises_when_unconfigured(self, jira_env):
        """Regression guard for the Devin adversarial-review finding:
        when the Jira service is unconfigured (missing API credentials)
        the fetch must raise JiraFetchError, NOT silently return [].
        Silent [] would overlay an empty list onto cached issue JSON via
        save_issue and wipe existing remote_links parquet rows the next
        time a webhook fires while creds happen to be missing — the
        exact scenario this PR closes for the 401 / 429 / 5xx paths.
        The webhook hot path passes HMAC verification with
        JIRA_WEBHOOK_SECRET (separate from API creds), so a
        missing-creds + webhook-arrives combo is realistic, not
        theoretical."""
        from connectors.jira import service as svc

        # Mirror _make_jira_service except DROP the API credentials so
        # is_configured() returns False.
        svc.Config.JIRA_DOMAIN = ""
        svc.Config.JIRA_EMAIL = ""
        svc.Config.JIRA_API_TOKEN = ""
        svc.Config.JIRA_DATA_DIR = jira_env
        svc.Config.JIRA_WEBHOOK_SECRET = "webhook-secret-123"
        svc._jira_service = None
        service = svc.get_jira_service()
        assert not service.is_configured()
        with pytest.raises(JiraFetchError, match="not configured"):
            service.fetch_remote_links("PROJ-1")

    def test_raises_on_request_error(self, jira_env):
        import httpx

        service = _make_jira_service(jira_env)
        client = MagicMock()
        client.get.side_effect = httpx.RequestError("connection reset")
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)
        with patch("connectors.jira.service.httpx.Client", return_value=client):
            with pytest.raises(JiraFetchError, match="connection"):
                service.fetch_remote_links("PROJ-1")


class TestSaveIssueRemoteLinksOverlay:
    """save_issue must NOT set _remote_links when fetch_remote_links raises.
    The absent key is the contract with transform_remote_links: it signals
    'preserve existing rows'. A present-but-empty list would wipe them."""

    def test_sets_remote_links_on_success(self, jira_env):
        service = _make_jira_service(jira_env)
        with (
            patch.object(service, "fetch_remote_links", return_value=[{"id": "rl-1"}]),
            patch.object(service, "fetch_refresh_fields", return_value=None),
        ):
            path = service.save_issue(_fake_issue_data("PROJ-1"))
        with open(path) as f:
            data = json.load(f)
        assert data["_remote_links"] == [{"id": "rl-1"}]

    def test_sets_empty_remote_links_on_404(self, jira_env):
        # 404 stays as [] — legitimately means "issue has no remote links".
        service = _make_jira_service(jira_env)
        with (
            patch.object(service, "fetch_remote_links", return_value=[]),
            patch.object(service, "fetch_refresh_fields", return_value=None),
        ):
            path = service.save_issue(_fake_issue_data("PROJ-1"))
        with open(path) as f:
            data = json.load(f)
        assert data["_remote_links"] == []

    def test_omits_remote_links_key_on_fetch_error(self, jira_env):
        # When fetch raises, the key MUST be absent — that's the signal
        # to the transform that this isn't fresh data and should be skipped.
        service = _make_jira_service(jira_env)
        with (
            patch.object(service, "fetch_remote_links", side_effect=JiraFetchError("auth")),
            patch.object(service, "fetch_refresh_fields", return_value=None),
        ):
            path = service.save_issue(_fake_issue_data("PROJ-1"))
        with open(path) as f:
            data = json.load(f)
        assert "_remote_links" not in data, (
            "Absent key is the contract with transform_remote_links — do not change to an empty list."
        )


class TestTransformRemoteLinks:
    """transform_remote_links returns None when _remote_links is absent
    (preserve-existing signal) and [] when present-but-empty (legitimate 'none')."""

    def test_returns_list_when_links_present(self):
        result = transform_remote_links(
            {
                "key": "PROJ-1",
                "_remote_links": [
                    {
                        "id": "rl-1",
                        "object": {"url": "https://x", "title": "X"},
                        "application": {"name": "App", "type": "type"},
                    }
                ],
            }
        )
        assert result is not None
        assert len(result) == 1
        assert result[0]["remote_link_id"] == "rl-1"

    def test_returns_empty_list_when_key_present_but_empty(self):
        result = transform_remote_links({"key": "PROJ-1", "_remote_links": []})
        assert result == []

    def test_returns_none_when_key_absent(self):
        # Absent key = save_issue skipped the overlay because fetch failed.
        # Signal to caller: preserve existing parquet rows for this issue.
        result = transform_remote_links({"key": "PROJ-1"})
        assert result is None

    def test_returns_none_when_key_is_explicit_null(self):
        # Defensive: a JSON with `_remote_links: null` (from older buggy code
        # or a manual edit) would otherwise blow up on `for rl in None`.
        # Treat null the same as absent — both mean "no fresh data".
        result = transform_remote_links({"key": "PROJ-1", "_remote_links": None})
        assert result is None


class TestAutoFixThreshold:
    """The auto-fix threshold determines at what gap size we auto-backfill
    vs require manual review. Legacy bumped it from 10 to 20 — small enough
    to stay safe, big enough to absorb a typical SLA-poller hiccup."""

    def test_threshold_is_twenty(self):
        assert JiraConsistencyChecker.AUTO_FIX_THRESHOLD == 20

    def test_alert_level_warning_at_threshold(self):
        # 20 missing should still be WARNING (auto-fix territory), not ERROR.
        config = MagicMock()
        checker = JiraConsistencyChecker(config)
        assert checker.get_alert_level(20) == "WARNING"

    def test_alert_level_error_above_threshold(self):
        config = MagicMock()
        checker = JiraConsistencyChecker(config)
        assert checker.get_alert_level(21) == "ERROR"
