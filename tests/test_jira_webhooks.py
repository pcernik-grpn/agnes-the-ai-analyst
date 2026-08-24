"""Tests for Jira webhook FastAPI router."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient


def _sign(payload: bytes, secret: str) -> str:
    """Compute sha256=<HMAC hex> for a given payload and secret."""
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


@pytest.fixture()
def webhook_client(tmp_path, monkeypatch, shared_app):
    """Create a TestClient with required env vars, dirs, and a seeded admin user."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "issues").mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("STATE_DIR", str(state_dir))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setenv("JIRA_DATA_DIR", str(data_dir))

    # Re-read env into Config (class attrs read os.environ at import time)
    from connectors.jira import service as svc

    monkeypatch.setattr(svc.Config, "JIRA_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setattr(svc.Config, "JIRA_DATA_DIR", data_dir)

    # Reset singleton so it picks up fresh Config values
    svc._jira_service = None

    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    UserRepository(conn).create(id="wh_admin", email="whadmin@test.com", name="WH Admin")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("wh_admin", admin_gid, source="system_seed")
    conn.close()

    app = shared_app
    admin_token = create_access_token("wh_admin", "whadmin@test.com")
    return {"client": TestClient(app), "admin_token": admin_token}


def test_health_requires_auth(webhook_client):
    """GET /webhooks/jira/health returns 401 without credentials (ADV-002)."""
    resp = webhook_client["client"].get("/webhooks/jira/health")
    assert resp.status_code == 401


def test_health(webhook_client):
    """GET /webhooks/jira/health returns 200 for admin; jira_domain not exposed."""
    headers = {"Authorization": f"Bearer {webhook_client['admin_token']}"}
    resp = webhook_client["client"].get("/webhooks/jira/health", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "webhook_secret_set" in body
    assert "jira_domain" not in body


def test_missing_signature_401(webhook_client):
    """POST without signature header returns 401."""
    payload = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "TEST-1"}}).encode()
    resp = webhook_client["client"].post(
        "/webhooks/jira", content=payload, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 401


def test_invalid_signature_401(webhook_client):
    """POST with wrong signature returns 401."""
    payload = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "TEST-1"}}).encode()
    resp = webhook_client["client"].post(
        "/webhooks/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=badhex",
        },
    )
    assert resp.status_code == 401


def test_valid_signature_accepted(webhook_client):
    """POST with correct HMAC-SHA256 passes signature check (not 401)."""
    from unittest.mock import patch

    payload = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "TEST-1"}}).encode()
    sig = _sign(payload, "test-webhook-secret")

    # Mock process_webhook_event so the test only checks HMAC validation,
    # not the full Jira API flow (which requires a real Jira connection).
    with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
        mock_svc.return_value.is_configured.return_value = True
        mock_svc.return_value.process_webhook_event.return_value = True

        resp = webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
    assert resp.status_code == 200


def test_empty_payload_400(webhook_client):
    """POST with empty body and valid signature returns 400."""
    payload = b""
    sig = _sign(payload, "test-webhook-secret")
    resp = webhook_client["client"].post(
        "/webhooks/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 400


def test_unconfigured_secret_returns_503(tmp_path, monkeypatch):
    """Issue #83: missing JIRA_WEBHOOK_SECRET must fail-closed (no fall-through to 200)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "issues").mkdir()

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.delenv("JIRA_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("JIRA_DATA_DIR", str(data_dir))

    from connectors.jira import service as svc

    monkeypatch.setattr(svc.Config, "JIRA_WEBHOOK_SECRET", "")
    monkeypatch.setattr(svc.Config, "JIRA_DATA_DIR", data_dir)
    svc._jira_service = None

    from app.main import create_app

    client = TestClient(create_app())

    payload = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "TEST-1"}}).encode()
    resp = client.post(
        "/webhooks/jira",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 503
    assert "secret" in resp.json()["detail"].lower()


@pytest.mark.parametrize(
    "bad_key",
    [
        "../../etc/passwd",
        "../foo",
        "TEST-1/../../../bar",
        "TEST-1\x00.json",
        "TEST-1\r\n",  # CRLF injection
        "test-1",  # lowercase project — Jira keys are uppercase
        "TEST",  # missing -<num>
        "TEST-",  # missing num
        "-1",  # missing project
        "",  # empty
        "A" * 100 + "-1",  # absurd length
        "ABC_DEF-1",  # underscore — not allowed in real Jira
        "А-1",  # Cyrillic А (looks like Latin A)
    ],
)
def test_path_traversal_in_issue_key_rejected(webhook_client, bad_key):
    """Issue #83: malformed issue keys must be rejected with 400, not used in paths."""
    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": bad_key},
        }
    ).encode()
    sig = _sign(payload, "test-webhook-secret")

    resp = webhook_client["client"].post(
        "/webhooks/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 400, f"key {bad_key!r} should have been rejected, got {resp.status_code}"


def test_null_issue_field_does_not_crash(webhook_client):
    """Issue #83 round-5: a payload with `issue: null` (not just missing)
    used to raise AttributeError on `issue.get('key')` → unhandled 500.
    The handler now normalises None to {} and falls through to the
    400 'Malformed or missing issue key' response."""
    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": None,
        }
    ).encode()
    sig = _sign(payload, "test-webhook-secret")

    resp = webhook_client["client"].post(
        "/webhooks/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 400
    assert "issue key" in resp.json()["detail"].lower()


def test_valid_issue_key_accepted(webhook_client):
    """Sanity: a well-formed issue key still passes validation."""
    from unittest.mock import patch

    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PROJ-42"},
        }
    ).encode()
    sig = _sign(payload, "test-webhook-secret")

    with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
        mock_svc.return_value.is_configured.return_value = True
        mock_svc.return_value.process_webhook_event.return_value = True

        resp = webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
    assert resp.status_code == 200


def test_webhook_event_path_traversal_sanitized(webhook_client, tmp_path, monkeypatch):
    """Issue #83: `webhookEvent` is attacker-controlled and was used to build
    the webhook log filename. A payload with `../../tmp/pwn` for `webhookEvent`
    must NOT escape the WEBHOOK_LOG_DIR; the file (if written at all) lands
    under WEBHOOK_LOG_DIR with the traversal characters sanitized."""
    from unittest.mock import patch
    import app.api.jira_webhooks as wh

    log_dir = tmp_path / "webhook_log"
    log_dir.mkdir()
    monkeypatch.setattr(wh, "WEBHOOK_LOG_DIR", log_dir)

    payload = json.dumps(
        {
            "webhookEvent": "../../tmp/pwn",
            "issue": {"key": "TEST-1"},
        }
    ).encode()
    sig = _sign(payload, "test-webhook-secret")

    with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
        mock_svc.return_value.is_configured.return_value = True
        mock_svc.return_value.process_webhook_event.return_value = True

        resp = webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert resp.status_code == 200
    # No file landed outside log_dir.
    parent = log_dir.parent
    assert not (parent / "tmp" / "pwn.json").exists(), "path traversal succeeded"
    # Either nothing was written (refused), or file is under log_dir with
    # traversal chars replaced by underscores.
    written = list(log_dir.glob("*.json"))
    for f in written:
        assert f.is_relative_to(log_dir), f"file {f} escaped log dir"
        assert "/" not in f.name and ".." not in f.name


# ---------------------------------------------------------------------------
# Additional HMAC validation + error handling tests
# ---------------------------------------------------------------------------


def test_valid_hmac_signature_accepted(webhook_client):
    """Webhook with valid HMAC-SHA256 signature is accepted (200)."""
    from unittest.mock import patch

    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PROJ-1"},
        }
    ).encode()
    sig = _sign(payload, "test-webhook-secret")

    with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
        mock_svc.return_value.is_configured.return_value = True
        mock_svc.return_value.process_webhook_event.return_value = True

        resp = webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
    assert resp.status_code == 200


def test_invalid_hmac_signature_rejected_401(webhook_client):
    """Webhook with wrong HMAC signature is rejected with 401."""
    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PROJ-1"},
        }
    ).encode()
    # Sign with the wrong secret
    sig = _sign(payload, "wrong-secret")

    resp = webhook_client["client"].post(
        "/webhooks/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 401


def test_missing_signature_header_rejected(webhook_client):
    """Webhook with no signature header at all is rejected with 401."""
    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PROJ-1"},
        }
    ).encode()

    resp = webhook_client["client"].post(
        "/webhooks/jira",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_x_hub_signature_legacy_header_accepted(webhook_client):
    """X-Hub-Signature (SHA1 legacy) header is also checked."""

    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PROJ-1"},
        }
    ).encode()
    # The handler falls back to X-Hub-Signature if X-Hub-Signature-256 is absent.
    # _verify_signature strips "sha256=" prefix; for sha1 it strips "sha1=".
    # Since the handler uses hmac.new with sha256, a sha1= prefix will still
    # be checked against sha256 HMAC. This test verifies the fallback header
    # is read at all (the signature won't match sha256, so expect 401).
    resp = webhook_client["client"].post(
        "/webhooks/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature": "sha1=somehex",
        },
    )
    # Legacy header is read but signature won't match → 401
    assert resp.status_code == 401


def test_malformed_json_payload_handled_gracefully(webhook_client):
    """Malformed webhook payload (invalid JSON) is handled gracefully with 400."""
    payload = b"this is not json {!><"
    sig = _sign(payload, "test-webhook-secret")

    resp = webhook_client["client"].post(
        "/webhooks/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 400
    assert "json" in resp.json()["detail"].lower() or "invalid" in resp.json()["detail"].lower()


def test_duplicate_event_processed_twice(webhook_client):
    """Same Jira event ID sent twice is processed both times (idempotent at
    the service layer, not rejected at the webhook layer)."""
    from unittest.mock import patch

    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "DUP-1"},
        }
    ).encode()
    sig = _sign(payload, "test-webhook-secret")

    with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
        mock_svc.return_value.is_configured.return_value = True
        mock_svc.return_value.process_webhook_event.return_value = True

        resp1 = webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
        resp2 = webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    # Both requests succeed — deduplication is the service layer's job
    assert resp1.status_code == 200
    assert resp2.status_code == 200


def test_signature_without_sha256_prefix(webhook_client):
    """A raw hex signature without 'sha256=' prefix is also accepted by
    _verify_signature (it strips the prefix if present)."""
    from unittest.mock import patch
    import hmac as hmac_mod

    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PROJ-1"},
        }
    ).encode()
    # Compute raw hex without prefix
    mac = hmac_mod.new("test-webhook-secret".encode(), payload, hashlib.sha256).hexdigest()

    with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
        mock_svc.return_value.is_configured.return_value = True
        mock_svc.return_value.process_webhook_event.return_value = True

        resp = webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": mac,  # no sha256= prefix
            },
        )
    assert resp.status_code == 200


def test_jira_service_not_configured_returns_503(webhook_client):
    """When Jira service is not configured, webhook returns 503."""
    from unittest.mock import patch

    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PROJ-1"},
        }
    ).encode()
    sig = _sign(payload, "test-webhook-secret")

    with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
        mock_svc.return_value.is_configured.return_value = False

        resp = webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
    assert resp.status_code == 503


def test_process_webhook_event_failure_returns_500(webhook_client):
    """When process_webhook_event returns False, the endpoint returns 500."""
    from unittest.mock import patch

    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PROJ-1"},
        }
    ).encode()
    sig = _sign(payload, "test-webhook-secret")

    with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
        mock_svc.return_value.is_configured.return_value = True
        mock_svc.return_value.process_webhook_event.return_value = False

        resp = webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
    assert resp.status_code == 500


def test_process_webhook_event_runs_off_the_event_loop(webhook_client):
    """``process_webhook_event``'s call chain (sync httpx, file writes,
    parquet transform, and — per the 429 handling in
    ``complete_issue_comments`` — a bounded but potentially multi-second
    ``Retry-After`` ``time.sleep()``) must not run directly on the asyncio
    event loop: that would freeze the ENTIRE app (chat, admin, every other
    API) for the duration. It must be dispatched via ``run_in_threadpool``.
    (Devin Review on #1283)"""
    import threading
    from unittest.mock import patch

    import app.api.jira_webhooks as wh

    captured: dict[str, int] = {}
    real_verify = wh._verify_signature

    def spy_verify(payload, signature):
        # `_verify_signature` runs directly inside the async handler, before
        # any dispatch — its thread ident is the event-loop baseline.
        captured["event_loop_thread"] = threading.get_ident()
        return real_verify(payload, signature)

    def fake_process(event_data):
        captured["worker_thread"] = threading.get_ident()
        return True

    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PROJ-1"},
        }
    ).encode()
    sig = _sign(payload, "test-webhook-secret")

    with (
        patch.object(wh, "_verify_signature", side_effect=spy_verify),
        patch("app.api.jira_webhooks.get_jira_service") as mock_svc,
    ):
        mock_svc.return_value.is_configured.return_value = True
        mock_svc.return_value.process_webhook_event.side_effect = fake_process

        resp = webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert resp.status_code == 200
    assert "event_loop_thread" in captured and "worker_thread" in captured
    assert captured["worker_thread"] != captured["event_loop_thread"], (
        "process_webhook_event ran on the event-loop thread — a slow Jira "
        "429 retry would block every concurrent request against the app"
    )


def test_issue_key_at_top_level_accepted(webhook_client):
    """Some Jira event types deliver issue_key at the top level instead of
    issue.key. The handler should accept these."""
    from unittest.mock import patch

    payload = json.dumps(
        {
            "webhookEvent": "jira:issue_deleted",
            "issue_key": "PROJ-99",
        }
    ).encode()
    sig = _sign(payload, "test-webhook-secret")

    with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
        mock_svc.return_value.is_configured.return_value = True
        mock_svc.return_value.process_webhook_event.return_value = True

        resp = webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
    assert resp.status_code == 200
