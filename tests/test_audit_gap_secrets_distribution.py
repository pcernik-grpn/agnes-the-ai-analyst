"""Audit coverage — secrets, admin config, distribution channels, ingress
(F2e — audit-full-coverage plan, Task 7).

One test group per closed surface. Secret-value non-leakage is asserted
explicitly wherever a handler stores/rotates a credential — the audit row's
params must never contain the raw secret.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests
from src.repositories import audit_repo


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    """Every secrets test in this module gets a working vault by default;
    tests that need the 409-without-vault path delete it explicitly."""
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


# ---------------------------------------------------------------------------
# app.api.admin_source_connections — full CRUD + secret set/clear + test
# ---------------------------------------------------------------------------

BASE_CONN = "/api/admin/source-connections"


class TestSourceConnectionAudit:
    def test_create_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.post(
            BASE_CONN,
            json={"name": "audit-create", "source_type": "keboola", "config": {"stack_url": "https://x.example.com"}},
            headers=admin_user,
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        rows, _ = audit_repo().query(
            action="source_connection.create", resource=f"source_connection:{conn_id}", limit=5
        )
        assert rows
        assert rows[0]["params"] and json.loads(rows[0]["params"])["source_type"] == "keboola"

    def test_update_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        conn_id = c.post(
            BASE_CONN,
            json={"name": "audit-update", "source_type": "keboola", "config": {"stack_url": "https://x.example.com"}},
            headers=admin_user,
        ).json()["id"]
        resp = c.put(f"{BASE_CONN}/{conn_id}", json={"name": "audit-update-renamed"}, headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(
            action="source_connection.update", resource=f"source_connection:{conn_id}", limit=5
        )
        assert rows

    def test_delete_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        conn_id = c.post(
            BASE_CONN,
            json={"name": "audit-delete", "source_type": "keboola", "config": {"stack_url": "https://x.example.com"}},
            headers=admin_user,
        ).json()["id"]
        resp = c.delete(f"{BASE_CONN}/{conn_id}", headers=admin_user)
        assert resp.status_code == 204
        rows, _ = audit_repo().query(
            action="source_connection.delete", resource=f"source_connection:{conn_id}", limit=5
        )
        assert rows

    def test_secret_set_writes_audit_log_without_the_value(self, seeded_app, admin_user):
        c = seeded_app["client"]
        conn_id = c.post(
            BASE_CONN,
            json={
                "name": "audit-secret-set",
                "source_type": "keboola",
                "config": {"stack_url": "https://x.example.com"},
            },
            headers=admin_user,
        ).json()["id"]

        secret_value = "sapi-super-secret-token"
        with (
            patch("app.api.admin_source_connections.KeboolaStorageClient.verify_token") as verify,
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            verify.return_value = {"isMasterToken": False, "owner": {"id": 1, "name": "proj"}}
            resp = c.put(f"{BASE_CONN}/{conn_id}/secret", json={"value": secret_value}, headers=admin_user)
        assert resp.status_code == 204, resp.text

        rows, _ = audit_repo().query(
            action="source_connection.secret.set", resource=f"source_connection:{conn_id}", limit=5
        )
        assert rows
        params = json.loads(rows[0]["params"]) if rows[0]["params"] else {}
        assert secret_value not in json.dumps(params)
        assert all(v != secret_value for v in params.values())

    def test_secret_clear_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        conn_id = c.post(
            BASE_CONN,
            json={
                "name": "audit-secret-clear",
                "source_type": "keboola",
                "config": {"stack_url": "https://x.example.com"},
            },
            headers=admin_user,
        ).json()["id"]
        resp = c.delete(f"{BASE_CONN}/{conn_id}/secret", headers=admin_user)
        assert resp.status_code == 204
        rows, _ = audit_repo().query(
            action="source_connection.secret.clear", resource=f"source_connection:{conn_id}", limit=5
        )
        assert rows

    def test_test_endpoint_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        conn_id = c.post(
            BASE_CONN,
            json={
                "name": "audit-test-conn",
                "source_type": "keboola",
                "config": {"stack_url": "https://x.example.com"},
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
            headers=admin_user,
        ).json()["id"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"isMasterToken": True, "owner": {"id": 1, "name": "proj"}}
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("app.api.admin_source_connections.httpx.AsyncClient", return_value=mock_client),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.post(f"{BASE_CONN}/{conn_id}/test", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="source_connection.test", resource=f"source_connection:{conn_id}", limit=5)
        assert rows


# ---------------------------------------------------------------------------
# app.api.mcp_user_secrets — set/clear/test/read
# ---------------------------------------------------------------------------


def _seed_per_user_mcp_source(source_id: str) -> None:
    from src.db import get_system_db
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name=f"pu-{source_id}",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="bearer",
        scope="per_user",
    )
    conn.close()


class TestMcpUserSecretAudit:
    def test_set_writes_audit_log_without_the_value(self, seeded_app, admin_user):
        _seed_per_user_mcp_source("src_audit_set")
        c = seeded_app["client"]
        secret_value = "notion-personal-token"
        resp = c.put("/api/mcp/sources/src_audit_set/my-secret", json={"value": secret_value}, headers=admin_user)
        assert resp.status_code == 204
        rows, _ = audit_repo().query(action="mcp_user_secret.set", resource="mcp_source:src_audit_set", limit=5)
        assert rows
        params = json.loads(rows[0]["params"]) if rows[0]["params"] else {}
        assert secret_value not in json.dumps(params)

    def test_clear_writes_audit_log(self, seeded_app, admin_user):
        _seed_per_user_mcp_source("src_audit_clear")
        c = seeded_app["client"]
        resp = c.delete("/api/mcp/sources/src_audit_clear/my-secret", headers=admin_user)
        assert resp.status_code == 204
        rows, _ = audit_repo().query(action="mcp_user_secret.clear", resource="mcp_source:src_audit_clear", limit=5)
        assert rows

    def test_read_writes_audit_log(self, seeded_app, admin_user):
        _seed_per_user_mcp_source("src_audit_read")
        c = seeded_app["client"]
        resp = c.get("/api/mcp/sources/src_audit_read/my-secret", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="mcp_user_secret.read", resource="mcp_source:src_audit_read", limit=5)
        assert rows

    def test_test_writes_audit_log(self, seeded_app, admin_user):
        _seed_per_user_mcp_source("src_audit_test")
        c = seeded_app["client"]
        c.put("/api/mcp/sources/src_audit_test/my-secret", json={"value": "tok"}, headers=admin_user)

        async def _fake_list_tools_async(source, *, caller_user_id=None):
            return []

        with patch("connectors.mcp.client.list_tools_async", _fake_list_tools_async):
            resp = c.post("/api/mcp/sources/src_audit_test/my-secret/test", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="mcp_user_secret.test", resource="mcp_source:src_audit_test", limit=5)
        assert rows
        assert rows[0]["result"] == "success"


# ---------------------------------------------------------------------------
# app.api.admin_datasource_secrets / app.api.admin_slack_secrets — GET reads
# ---------------------------------------------------------------------------


class TestDatasourceAndSlackSecretReads:
    def test_datasource_secrets_get_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.get("/api/admin/datasource-secrets", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="datasource.secret.read", limit=5)
        assert rows
        # never a value, only the presence/source status envelope
        assert all("value" not in (r.get("params") or "") for r in rows)

    def test_slack_secrets_get_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.get("/api/admin/slack-secrets", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="slack.secret.read", limit=5)
        assert rows

    def test_datasource_secret_set_and_clear_write_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.put(
            "/api/admin/datasource-secrets/AGNES_GWS_CLIENT_SECRET",
            json={"value": "gws-client-secret-value"},
            headers=admin_user,
        )
        assert resp.status_code == 204
        rows, _ = audit_repo().query(action="datasource.secret.set", limit=5)
        assert rows

        resp2 = c.delete("/api/admin/datasource-secrets/AGNES_GWS_CLIENT_SECRET", headers=admin_user)
        assert resp2.status_code == 204
        rows2, _ = audit_repo().query(action="datasource.secret.clear", limit=5)
        assert rows2

    def test_slack_secret_set_and_clear_write_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.put("/api/admin/slack-secrets/SLACK_BOT_TOKEN", json={"value": "xoxb-x"}, headers=admin_user)
        assert resp.status_code == 204
        rows, _ = audit_repo().query(action="slack.secret.set", limit=5)
        assert rows

        resp2 = c.delete("/api/admin/slack-secrets/SLACK_BOT_TOKEN", headers=admin_user)
        assert resp2.status_code == 204
        rows2, _ = audit_repo().query(action="slack.secret.clear", limit=5)
        assert rows2


# ---------------------------------------------------------------------------
# app.api.admin — /api/admin/configure + server-config/overlay GETs
# ---------------------------------------------------------------------------


class TestInstanceConfigureAndServerConfigReads:
    def test_configure_writes_only_changed_key_names(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "local", "instance_name": "Audited Instance"},
            headers=admin_user,
        )
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="instance.configure", limit=5)
        assert rows
        params = json.loads(rows[0]["params"]) if rows[0]["params"] else {}
        assert params["keys"] == sorted({"data_source", "instance"})
        # never a value — "Audited Instance" must not appear anywhere in params
        assert "Audited Instance" not in json.dumps(params)

    def test_server_config_get_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="server_config.read", resource="instance.yaml", limit=5)
        assert any(json.loads(r["params"] or "{}").get("view") == "redacted" for r in rows)

    def test_server_config_overlay_get_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.get("/api/admin/server-config/overlay", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="server_config.read", resource="instance.yaml", limit=5)
        assert any(json.loads(r["params"] or "{}").get("view") == "overlay" for r in rows)


# ---------------------------------------------------------------------------
# app.api.scripts — deploy/run/delete
# ---------------------------------------------------------------------------


class TestScriptsAudit:
    def test_deploy_writes_audit_log_without_source(self, seeded_app, admin_user):
        c = seeded_app["client"]
        source = "print('hello from an audited script')"
        resp = c.post(
            "/api/scripts/deploy",
            json={"name": "audited-script", "source": source},
            headers=admin_user,
        )
        assert resp.status_code == 201
        script_id = resp.json()["id"]
        rows, _ = audit_repo().query(action="script.deploy", resource=f"script:{script_id}", limit=5)
        assert rows
        params = json.loads(rows[0]["params"]) if rows[0]["params"] else {}
        assert source not in json.dumps(params)

    def test_run_deployed_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        script_id = c.post(
            "/api/scripts/deploy",
            json={"name": "audited-run-script", "source": "print('x')"},
            headers=admin_user,
        ).json()["id"]
        resp = c.post(f"/api/scripts/{script_id}/run", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="script.run", resource=f"script:{script_id}", limit=5)
        assert rows
        assert rows[0]["result"] == "success"

    def test_run_adhoc_writes_audit_log_without_source(self, seeded_app, admin_user):
        c = seeded_app["client"]
        source = "print('adhoc')"
        resp = c.post("/api/scripts/run", json={"source": source, "name": "adhoc-audited"}, headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="script.run", limit=5)
        matches = [r for r in rows if r["resource"] == "script:adhoc-audited"]
        assert matches
        params = json.loads(matches[0]["params"]) if matches[0]["params"] else {}
        assert source not in json.dumps(params)

    def test_delete_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        script_id = c.post(
            "/api/scripts/deploy",
            json={"name": "audited-delete-script", "source": "print('x')"},
            headers=admin_user,
        ).json()["id"]
        resp = c.delete(f"/api/scripts/{script_id}", headers=admin_user)
        assert resp.status_code == 204
        rows, _ = audit_repo().query(action="script.delete", resource=f"script:{script_id}", limit=5)
        assert rows


# ---------------------------------------------------------------------------
# Marketplace distribution — zip endpoints + git smart-HTTP
# ---------------------------------------------------------------------------

from tests.test_marketplace_server_zip import marketplace_env  # noqa: E402,F401
from tests.test_marketplace_server_git import _basic, git_env  # noqa: E402,F401


class TestMarketplaceDistributionAudit:
    def test_marketplace_zip_writes_audit_log(self, marketplace_env):  # noqa: F811
        c = marketplace_env["client"]
        resp = c.get("/marketplace.zip", headers=_auth(marketplace_env["admin_token"]))
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="marketplace.bundle_download", resource="marketplace.zip", limit=5)
        assert rows

    def test_marketplace_git_fetch_writes_audit_log(self, git_env):  # noqa: F811
        c = git_env["client"]
        resp = c.get(
            "/marketplace.git/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", git_env["admin_pat"])},
        )
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="marketplace.git_fetch", limit=5)
        assert any(r["user_id"] == "admin1" for r in rows)

    def test_marketplace_git_push_path_classified_as_push(self, git_env):  # noqa: F811
        """POST .../git-receive-pack is classified `marketplace.git_push` —
        logged right after PAT resolution, independent of whatever
        `git http-backend` itself does with the (empty, unsigned) body."""
        c = git_env["client"]
        c.post(
            "/marketplace.git/git-receive-pack",
            headers={
                "Authorization": _basic("x", git_env["admin_pat"]),
                "Content-Type": "application/x-git-receive-pack-request",
            },
            content=b"",
        )
        rows, _ = audit_repo().query(action="marketplace.git_push", limit=5)
        assert any(r["user_id"] == "admin1" for r in rows)


# ---------------------------------------------------------------------------
# app.api.store — GET /bundle.zip
# ---------------------------------------------------------------------------


class TestStoreBundleAudit:
    def test_bundle_zip_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.get("/api/store/bundle.zip", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="store.bundle_download", resource="store:bundle.zip", limit=5)
        assert rows


# ---------------------------------------------------------------------------
# app.api.memory — GET /bundle (default + per-domain markdown)
# ---------------------------------------------------------------------------

from tests.test_api_memory_bundle_per_domain import _create_domain  # noqa: E402


class TestMemoryBundleAudit:
    def test_default_bundle_writes_audit_log(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        resp = c.get("/api/memory/bundle", headers=analyst_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="memory.bundle_download", resource="memory:bundle", limit=5)
        assert rows

    def test_per_domain_bundle_writes_audit_log(self, seeded_app, admin_user):
        domain_id = _create_domain("audit-gap-domain")
        c = seeded_app["client"]
        resp = c.get("/api/memory/bundle?domain=audit-gap-domain", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="memory.bundle_download", resource=f"memory_domain:{domain_id}", limit=5)
        assert rows


# ---------------------------------------------------------------------------
# app.api.jira_webhooks — received / rejected
# ---------------------------------------------------------------------------

from tests.test_jira_webhooks import webhook_client  # noqa: E402,F401


def _sign(payload: bytes, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


class TestJiraWebhookAudit:
    def test_valid_signature_writes_received(self, webhook_client):  # noqa: F811
        payload = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "TEST-1"}}).encode()
        sig = _sign(payload, "test-webhook-secret")
        with patch("app.api.jira_webhooks.get_jira_service") as mock_svc:
            mock_svc.return_value.is_configured.return_value = True
            mock_svc.return_value.process_webhook_event.return_value = True
            webhook_client["client"].post(
                "/webhooks/jira",
                content=payload,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
            )
        rows, _ = audit_repo().query(action="webhook.jira_received", limit=5)
        assert rows
        params = json.loads(rows[0]["params"]) if rows[0]["params"] else {}
        assert params.get("event") == "jira:issue_updated"
        assert rows[0]["user_id"] is None

    def test_invalid_signature_writes_rejected(self, webhook_client):  # noqa: F811
        payload = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "TEST-1"}}).encode()
        webhook_client["client"].post(
            "/webhooks/jira",
            content=payload,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=not-the-right-mac"},
        )
        rows, _ = audit_repo().query(action="webhook.jira_rejected", limit=5)
        assert rows
        assert rows[0]["result"] == "denied"
        assert rows[0]["user_id"] is None


# ---------------------------------------------------------------------------
# app.api.upload — artifacts + local-md
# ---------------------------------------------------------------------------


class TestUploadAudit:
    def test_artifact_upload_writes_audit_log(self, seeded_app, admin_user):
        c = seeded_app["client"]
        content = b"<html><body>Audited report</body></html>"
        resp = c.post(
            "/api/upload/artifacts",
            files={"file": ("audit-report.html", io.BytesIO(content), "text/html")},
            headers=admin_user,
        )
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="artifact.upload", limit=5)
        matches = [r for r in rows if json.loads(r["params"] or "{}").get("filename") == "audit-report.html"]
        assert matches
        assert json.loads(matches[0]["params"])["bytes"] == len(content)

    def test_local_md_upload_writes_audit_log_without_content(self, seeded_app, admin_user):
        c = seeded_app["client"]
        content = "# Secret notes\n\nsomething private"
        resp = c.post("/api/upload/local-md", json={"content": content}, headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="local_md.upload", limit=5)
        assert rows
        params = json.loads(rows[0]["params"]) if rows[0]["params"] else {}
        assert params.get("bytes") == len(content)
        assert content not in json.dumps(params)


# ---------------------------------------------------------------------------
# app.api.sync — pull-confirm / settings / table-subscriptions
# ---------------------------------------------------------------------------


class TestSyncAudit:
    def test_pull_confirm_writes_audit_log(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        resp = c.post("/api/sync/pull-confirm", json={"duration_ms": 999, "errors": 0}, headers=analyst_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="sync.pull_confirmed", resource="sync:pull", limit=5)
        assert rows

    def test_settings_update_writes_audit_log(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        resp = c.post("/api/sync/settings", json={"datasets": {}}, headers=analyst_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="sync.settings_update", resource="sync:settings", limit=5)
        assert rows

    def test_table_subscriptions_update_writes_audit_log(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        resp = c.post(
            "/api/sync/table-subscriptions",
            json={"table_mode": "all", "tables": {}},
            headers=analyst_user,
        )
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="sync.subscriptions_update", resource="sync:table_subscriptions", limit=5)
        assert rows


# ---------------------------------------------------------------------------
# app.api.observability — facets/kpis self-audit reuses activity.read
# ---------------------------------------------------------------------------


class TestObservabilitySelfAudit:
    def test_facets_reuses_activity_read_action(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.get("/api/admin/observability/facets", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="activity.read", limit=20)
        assert any(json.loads(r["params"] or "{}").get("endpoint") == "facets" for r in rows)

    def test_kpis_reuses_activity_read_action(self, seeded_app, admin_user):
        c = seeded_app["client"]
        resp = c.get("/api/admin/observability/kpis", headers=admin_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="activity.read", limit=20)
        assert any(json.loads(r["params"] or "{}").get("endpoint") == "kpis" for r in rows)
