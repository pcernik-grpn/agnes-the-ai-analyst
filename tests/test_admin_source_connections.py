"""Tests for the /api/admin/source-connections REST surface.

Covers:
- list empty → []
- create → 201 with id
- create duplicate name → 409
- get existing → 200
- get missing → 404
- update config → 200
- delete → 204
- test endpoint: mock httpx, return fake project info
- unauthenticated → 401
- non-admin → 403
- set secret → 204 (vault key required)
- set secret without vault key → 409
- master-token vault slot (kind=master): keboola-only, verify_token preflight,
  storage-api-outage redaction, cleanup on connection delete
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


BASE = "/api/admin/source-connections"


class TestSourceConnectionsList:
    def test_list_empty_returns_empty_list(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(BASE, headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        # May include a seeded default from instance.yaml — assert it's a list
        assert isinstance(data, list)

    def test_list_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get(BASE, headers=_auth(token))
        assert resp.status_code == 403

    def test_list_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(BASE)
        assert resp.status_code == 401


class TestSourceConnectionsCreate:
    def test_create_returns_201_with_id(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-create",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "id" in body
        assert body["name"] == "test-keboola-create"

    def test_create_duplicate_name_returns_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        payload = {
            "name": "test-keboola-dup",
            "source_type": "keboola",
            "config": {"stack_url": "https://connection.example.com"},
        }
        resp1 = c.post(BASE, json=payload, headers=_auth(token))
        assert resp1.status_code == 201
        resp2 = c.post(BASE, json=payload, headers=_auth(token))
        assert resp2.status_code == 409

    def test_create_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-x",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_create_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            BASE,
            json={
                "name": "test-x",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
        )
        assert resp.status_code == 401


class TestSeedFromInstanceCredentials:
    """``seed_from_instance_credentials=true`` on ``POST`` — the derived
    Keboola card's "Import as managed connection" button
    (`app/web/templates/admin_data_sources.html::importKeboolaConnection`).

    Copies the instance-level Keboola token into the new connection's own
    vault slot, but only when that token lives ONLY in the instance vault
    (an env var needs no copy — the new row's own ``_resolve_token`` already
    falls back to that same process-global env var).
    """

    @pytest.fixture(autouse=True)
    def _stable_vault_key(self, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        yield
        _reset_ephemeral_key_for_tests()

    def test_flag_absent_never_touches_the_resolver(self, seeded_app, monkeypatch):
        """Every existing caller (the "Add data source" wizard, the CLI,
        third-party API clients) leaves the flag unset and must see zero
        behavior change — no resolver call, no response fields."""
        called = []
        monkeypatch.setattr(
            "app.datasource_secrets.keboola_instance_token",
            lambda token_env: (called.append(token_env), (None, None))[1],
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-seed-flag-absent",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        assert called == []
        assert "token_seeded" not in resp.json()

    def test_vault_only_credential_is_seeded_and_verified(self, seeded_app, monkeypatch):
        """Case (3) from the review: the credential lives ONLY in the
        instance vault. It must land in the new connection's OWN vault slot
        — preflighted and identity-recorded exactly like a manual
        `PUT .../secret` — not just get a decorative "env" badge."""
        monkeypatch.setattr(
            "app.datasource_secrets.keboola_instance_token",
            lambda token_env: ("vault-sourced-token", "vault"),
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"isMasterToken": False, "owner": {"id": 555, "name": "Vault Co"}},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.post(
                BASE,
                json={
                    "name": "test-seed-vault-only",
                    "source_type": "keboola",
                    "config": {"stack_url": "https://connection.example.com"},
                    "token_env": "KEBOOLA_STORAGE_TOKEN",
                    "seed_from_instance_credentials": True,
                },
                headers=_auth(token),
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["token_seeded"] is True
        assert body["has_secret"] is True
        assert body["config"]["project_id"] == 555

        # The row itself is really usable, not just badged as such.
        row = c.get(f"{BASE}/{body['id']}", headers=_auth(token)).json()
        assert row["has_secret"] is True

    def test_env_sourced_credential_needs_no_seeding(self, seeded_app, monkeypatch):
        """Case (1) from the review: the value is resolvable under the
        row's own exact `token_env` name — an env var is process-global, so
        the new connection's own token resolver already finds it — no vault
        write, and no upstream preflight call either."""
        monkeypatch.setattr(
            "app.datasource_secrets.keboola_instance_token",
            lambda token_env: ("env-sourced-token", "env_token_env"),
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("app.api.admin_source_connections.KeboolaStorageClient.verify_token") as verify:
            resp = c.post(
                BASE,
                json={
                    "name": "test-seed-env-source",
                    "source_type": "keboola",
                    "config": {"stack_url": "https://connection.example.com"},
                    "token_env": "KEBOOLA_STORAGE_TOKEN",
                    "seed_from_instance_credentials": True,
                },
                headers=_auth(token),
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "token_seeded" not in body
        assert body["has_secret"] is False
        verify.assert_not_called()

    def test_generic_env_fallback_behind_a_custom_token_env_is_seeded(self, seeded_app, monkeypatch):
        """Case (2) from the review: the instance's configured `token_env`
        is a CUSTOM name that is itself unset, but the generic
        `KEBOOLA_STORAGE_TOKEN` env var holds the value. The new row
        inherits the custom name, and its own `_resolve_token` never falls
        back to the generic name — so unlike case (1), this DOES need
        seeding, exactly like the vault case."""
        monkeypatch.setattr(
            "app.datasource_secrets.keboola_instance_token",
            lambda token_env: ("generic-env-fallback-token", "env_generic"),
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"isMasterToken": False, "owner": {"id": 777, "name": "Generic Env Co"}},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.post(
                BASE,
                json={
                    "name": "test-seed-generic-env",
                    "source_type": "keboola",
                    "config": {"stack_url": "https://connection.example.com"},
                    "token_env": "KBC_TOKEN",
                    "seed_from_instance_credentials": True,
                },
                headers=_auth(token),
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["token_seeded"] is True
        assert body["has_secret"] is True
        assert body["config"]["project_id"] == 777

        row = c.get(f"{BASE}/{body['id']}", headers=_auth(token)).json()
        assert row["has_secret"] is True

    def test_a_stale_vault_token_reports_the_failure_without_failing_the_create(self, seeded_app, monkeypatch):
        """The instance vault holds a token, but it no longer verifies (e.g.
        revoked upstream). The import must not silently look like a success
        — the create itself still succeeds, but the response says exactly
        what went wrong so the client can toast something honest."""
        monkeypatch.setattr(
            "app.datasource_secrets.keboola_instance_token",
            lambda token_env: ("stale-vault-token", "vault"),
        )
        from connectors.keboola.storage_api import StorageApiError

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                side_effect=StorageApiError(
                    "GET https://connection.example.com/v2/storage/tokens/verify -> HTTP 401: "
                    '{"error": "Invalid access token", "code": "storage.tokenInvalid"}',
                    status=401,
                ),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.post(
                BASE,
                json={
                    "name": "test-seed-stale-vault",
                    "source_type": "keboola",
                    "config": {"stack_url": "https://connection.example.com"},
                    "token_env": "KEBOOLA_STORAGE_TOKEN",
                    "seed_from_instance_credentials": True,
                },
                headers=_auth(token),
            )
        # A bad instance-vault token must not fail the whole import — the
        # connection row itself was created fine.
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["token_seeded"] is False
        assert "storage.tokenInvalid" in body["token_seed_error"]
        assert body["has_secret"] is False

    def test_a_non_http_seed_error_also_reports_failure_without_failing_the_create(self, seeded_app, monkeypatch):
        """Devin Review: the seeding step's try/except only caught
        `HTTPException`, so a non-HTTPException failure inside
        `_store_connection_secret` (e.g. a `ValueError` from the Keboola
        client, or a vault failure that isn't `VaultKeyNotConfiguredError`)
        escaped and turned a connection that WAS created into a 500 the
        admin reads as total failure."""
        monkeypatch.setattr(
            "app.datasource_secrets.keboola_instance_token",
            lambda token_env: ("vault-sourced-token", "vault"),
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                side_effect=ValueError("boom"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.post(
                BASE,
                json={
                    "name": "test-seed-non-http-error",
                    "source_type": "keboola",
                    "config": {"stack_url": "https://connection.example.com"},
                    "token_env": "KEBOOLA_STORAGE_TOKEN",
                    "seed_from_instance_credentials": True,
                },
                headers=_auth(token),
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["token_seeded"] is False
        assert "boom" in body["token_seed_error"]
        assert body["has_secret"] is False

        row = c.get(f"{BASE}/{body['id']}", headers=_auth(token)).json()
        assert row["has_secret"] is False

    def test_flag_is_ignored_for_non_keboola_source_types(self, seeded_app, monkeypatch):
        called = []
        monkeypatch.setattr(
            "app.datasource_secrets.keboola_instance_token",
            lambda token_env: (called.append(token_env), (None, None))[1],
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-seed-non-keboola",
                "source_type": "bigquery",
                "config": {"project_id": "p"},
                "seed_from_instance_credentials": True,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        assert called == []
        assert "token_seeded" not in resp.json()


class TestSourceConnectionsGet:
    def test_get_existing_returns_200(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create first
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-get",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        # Now get
        resp2 = c.get(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["id"] == conn_id
        assert data["name"] == "test-keboola-get"

    def test_get_missing_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(f"{BASE}/nonexistent-id-xyz", headers=_auth(token))
        assert resp.status_code == 404


class TestSourceConnectionsUpdate:
    def test_update_config_returns_200(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-update",
                "source_type": "keboola",
                "config": {"stack_url": "https://old.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        # Update
        resp2 = c.put(
            f"{BASE}/{conn_id}",
            json={"config": {"stack_url": "https://new.example.com"}},
            headers=_auth(token),
        )
        assert resp2.status_code == 200
        # Verify
        resp3 = c.get(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp3.status_code == 200
        data = resp3.json()
        assert data["config"]["stack_url"] == "https://new.example.com"

    def test_update_missing_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.put(
            f"{BASE}/nonexistent-id-xyz",
            json={"config": {"stack_url": "https://new.example.com"}},
            headers=_auth(token),
        )
        assert resp.status_code == 404

    def test_update_renames_connection(self, seeded_app):
        # Backs the "Add data source" wizard's rename-after-test step (#755).
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "draft-rename-me",
                "source_type": "keboola",
                "config": {"stack_url": "https://a.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        resp2 = c.put(f"{BASE}/{conn_id}", json={"name": "Production"}, headers=_auth(token))
        assert resp2.status_code == 200
        assert resp2.json()["name"] == "Production"

        resp3 = c.get(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp3.json()["name"] == "Production"

    def test_update_rename_to_existing_name_returns_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.post(
            BASE,
            json={
                "name": "rename-conflict-a",
                "source_type": "keboola",
                "config": {"stack_url": "https://a.example.com"},
            },
            headers=_auth(token),
        )
        resp_b = c.post(
            BASE,
            json={
                "name": "rename-conflict-b",
                "source_type": "keboola",
                "config": {"stack_url": "https://b.example.com"},
            },
            headers=_auth(token),
        )
        conn_b_id = resp_b.json()["id"]

        resp = c.put(f"{BASE}/{conn_b_id}", json={"name": "rename-conflict-a"}, headers=_auth(token))
        assert resp.status_code == 409


class TestSourceConnectionsDelete:
    def test_delete_returns_204(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-delete",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        # Delete
        resp2 = c.delete(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp2.status_code == 204
        # Confirm gone
        resp3 = c.get(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp3.status_code == 404

    def test_delete_missing_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.delete(f"{BASE}/nonexistent-id-xyz", headers=_auth(token))
        assert resp.status_code == 404

    def test_delete_in_use_returns_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create a connection, then pin a registry table to it.
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-inuse",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        from src.repositories import table_registry_repo

        table_registry_repo().register(
            id="in.c-test.pinned_table",
            name="pinned_table",
            source_type="keboola",
            bucket="in.c-test",
            source_table="pinned_table",
            connection_id=conn_id,
        )

        # Deleting the still-referenced connection must be refused.
        resp2 = c.delete(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp2.status_code == 409
        detail = resp2.json()["detail"]
        assert detail["error"] == "connection_in_use"
        assert "in.c-test.pinned_table" in detail["tables"]
        # Connection still exists.
        assert c.get(f"{BASE}/{conn_id}", headers=_auth(token)).status_code == 200


class TestSourceConnectionsSecret:
    def test_set_secret_without_vault_key_returns_409(self, seeded_app, monkeypatch):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create connection
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-secret",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        # Pin BOTH halves of the intent rather than accepting whatever the
        # ambient env produces. The old `status in (204, 409)` passed either
        # way, and once storing a Keboola storage token gained an upstream
        # preflight, the 204 branch could only be reached by a real HTTPS call
        # to connection.example.com — so the test stayed hermetic purely
        # because AGNES_VAULT_KEY happens to be unset in the suite. Devin
        # Review on #1242.
        monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
        monkeypatch.setenv("LOCAL_DEV_MODE", "0")
        _reset_ephemeral_key_for_tests()

        with patch("app.api.admin_source_connections.KeboolaStorageClient.verify_token") as verify:
            resp2 = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "test-storage-token"},
                headers=_auth(token),
            )
        assert resp2.status_code == 409, resp2.text
        verify.assert_not_called()

    def test_delete_secret_returns_204(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create connection
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-secret-del",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        # Delete secret (idempotent even if no secret was set)
        resp2 = c.delete(f"{BASE}/{conn_id}/secret", headers=_auth(token))
        assert resp2.status_code == 204


class TestSourceConnectionsTest:
    def test_test_endpoint_success(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create connection
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-testconn",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        # Mock httpx to return a real `GET /v2/storage/tokens/verify` body —
        # project name and id live under `owner`. The previous fixture here
        # was `{"id": "123", "name": "Test Project"}`, a shape the Storage API
        # does not return from the endpoint this handler called: measured on a
        # live stack, `/v2/storage?exclude=components` is the unauthenticated
        # index (200 with NO token, no `owner` block at all), so the probe
        # validated nothing and `project_name` was always "".
        # The endpoint uses an async client (`async with httpx.AsyncClient(...)`
        # + `await client.get`), so the mock must honor the async
        # context-manager + awaitable-get protocol.
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "isMasterToken": True,
            "owner": {"id": 123, "name": "Test Project"},
        }

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("app.api.admin_source_connections.httpx.AsyncClient", return_value=mock_client),
            # example.com subdomains don't resolve; the SSRF validator is exercised
            # by its own test below, so no-op it here to test connectivity logic.
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp2 = c.post(f"{BASE}/{conn_id}/test", headers=_auth(token))

        assert resp2.status_code == 200
        data = resp2.json()
        assert data["ok"] is True
        assert data["project_name"] == "Test Project"
        # Pin the endpoint actually verifying the token, not pinging an index
        # that answers 200 for anyone.
        called_url = mock_client.get.call_args[0][0]
        assert called_url.endswith("/v2/storage/tokens/verify"), called_url
        # ...and that a successful test binds the connection to its project.
        row = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert row["config"]["project_id"] == 123
        assert row["config"]["project_name"] == "Test Project"

    def test_test_endpoint_rejects_private_stack_url(self, seeded_app):
        # SSRF guard: a stack_url pointing at the cloud metadata endpoint (or any
        # private/reserved/link-local host) is refused before any outbound call.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-ssrf",
                "source_type": "keboola",
                "config": {"stack_url": "https://169.254.169.254"},
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        with patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}):
            resp2 = c.post(f"{BASE}/{conn_id}/test", headers=_auth(token))
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["ok"] is False
        assert "private or reserved" in data["error"]

    def test_create_rejects_disallowed_token_env(self, seeded_app):
        # token_env allowlist: an admin cannot point a connection at an arbitrary
        # server-process env var (e.g. JWT_SECRET_KEY) to exfiltrate it via the
        # outbound token header.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-badenv",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
                "token_env": "JWT_SECRET_KEY",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "allowlist" in resp.json()["detail"].lower()

    def test_test_endpoint_logs_result(self, seeded_app, caplog):
        # /test was previously fully silent server-side; every outcome now
        # leaves one INFO line so repeated failures are visible in logs.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-testconn-log",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        # The `owner` shape the handler actually reads. With the pre-#1242
        # `{"id", "name"}` shape left here, `project_name` resolved to "" and
        # the redaction assertion below passed trivially — the string never
        # entered the handler at all. (Devin Review on this PR.)
        mock_response.json.return_value = {
            "id": "123",
            "owner": {"id": 987, "name": "Test Project"},
        }

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("app.api.admin_source_connections.httpx.AsyncClient", return_value=mock_client),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
            caplog.at_level("INFO", logger="app.api.admin_source_connections"),
        ):
            resp2 = c.post(f"{BASE}/{conn_id}/test", headers=_auth(token))

        assert resp2.status_code == 200
        assert resp2.json()["ok"] is True
        assert f"connection test for {conn_id}" in caplog.text
        assert ": ok" in caplog.text
        # Response-body content must never land in server logs — a fronting
        # proxy that echoes the token into the body would otherwise leak it.
        assert "Test Project" not in caplog.text

    def test_test_endpoint_exception_log_redacts_token(self, seeded_app, caplog):
        # The generic-exception outcome line must scrub the resolved token —
        # httpx exception reprs don't include headers today, but the log line
        # must not depend on that staying true.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-testconn-exc-log",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("connect failed; X-StorageApi-Token: fake-token"))

        with (
            patch("app.api.admin_source_connections.httpx.AsyncClient", return_value=mock_client),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
            caplog.at_level("INFO", logger="app.api.admin_source_connections"),
        ):
            resp2 = c.post(f"{BASE}/{conn_id}/test", headers=_auth(token))

        assert resp2.status_code == 200
        assert resp2.json()["ok"] is False
        assert f"connection test for {conn_id}" in caplog.text
        assert "fake-token" not in caplog.text
        assert "<redacted-storage-token>" in caplog.text

    def test_test_endpoint_missing_connection_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(f"{BASE}/nonexistent-id/test", headers=_auth(token))
        assert resp.status_code == 404


class TestSourceConnectionsTables:
    """GET /{id}/tables — the "Add data source" wizard's table-picker primitive (#755)."""

    def _create(self, c, token, *, name="test-kbc-tables", token_env="KEBOOLA_STORAGE_TOKEN"):
        resp = c.post(
            BASE,
            json={
                "name": name,
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
                "token_env": token_env,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_tables_endpoint_groups_by_bucket(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token)

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                return_value=[
                    {"id": "in.c-main", "name": "main", "stage": "in", "description": ""},
                ],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[
                    {
                        "id": "in.c-main.orders",
                        "name": "orders",
                        "bucket": {"id": "in.c-main"},
                        "rowsCount": 42,
                        "dataSizeBytes": 1024,
                    },
                ],
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "project"
        assert len(data["buckets"]) == 1
        bucket = data["buckets"][0]
        assert bucket["id"] == "in.c-main"
        assert bucket["tables"] == [{"id": "in.c-main.orders", "name": "orders", "rows": 42, "size_bytes": 1024}]

    def test_tables_endpoint_missing_connection_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(f"{BASE}/nonexistent-id/tables", headers=_auth(token))
        assert resp.status_code == 404

    def test_tables_endpoint_no_token_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-notoken", token_env="")
        # no-op the SSRF validator so the 400 comes from the no-token path, not
        # from example.com failing to resolve.
        with patch("app.api.admin._validate_url_not_private", return_value=None):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))
        assert resp.status_code == 400

    def test_tables_endpoint_non_keboola_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-bq-tables",
                "source_type": "bigquery",
                "config": {"project_id": "p"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        resp2 = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))
        assert resp2.status_code == 400

    def test_tables_endpoint_upstream_error_returns_502(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-upstream-error")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=StorageApiError("boom"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 502

    def test_an_empty_200_listing_also_falls_back_to_bucket_permissions(self, seeded_app):
        """Some token shapes get a 200 with an empty array, not a 403.

        That was indistinguishable from an empty project, so the picker reported
        "no buckets visible to this token" and the fallback never ran — the exact
        scenario the fallback exists for (Devin Review on #1189).
        """
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-empty200")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                return_value=[],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {"in.c-main": "read"}},
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[
                    {"id": "in.c-main.orders", "name": "orders", "rowsCount": 7, "dataSizeBytes": 64},
                ],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.get_bucket",
                return_value={"id": "in.c-main", "name": "main", "stage": "in", "description": ""},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["scope"] == "token_buckets"
        assert [b["id"] for b in data["buckets"]] == ["in.c-main"]

    def test_a_genuinely_empty_project_stays_empty_and_does_not_error(self, seeded_app):
        """The same empty-listing retry must not turn a real empty project into a
        502: with no bucketPermissions there is nothing to enumerate, so the
        empty project-wide answer stands."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-trulyempty")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                return_value=[],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {}},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["buckets"] == []
        assert data["scope"] == "project"

    def test_a_transient_failure_does_not_trigger_the_per_bucket_loop(self, seeded_app):
        """Only a refusal justifies the fallback, not any failure.

        `_scoped_listing` makes two upstream calls per bucket the token can see,
        so a brief blip on a full-access token would otherwise stall "Browse &
        register tables" for minutes on a large project and then label the token
        as bucket-scoped. A 5xx and a connection error must surface as themselves
        (Devin Review on #1189).
        """
        import requests as _requests

        from connectors.keboola.storage_api import StorageApiError

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-transient")

        for boom in (
            StorageApiError("upstream exploded", status=500),
            _requests.ConnectionError("connection reset"),
            # No status at all: the gate must fail CLOSED. An earlier version
            # read `status is not None and status not in (401, 403)`, so this
            # case fell through into the fallback (Devin Review on #1189).
            StorageApiError("no status stamped"),
        ):
            called = []
            with (
                patch(
                    "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                    side_effect=boom,
                ),
                patch(
                    "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                    side_effect=lambda: called.append("verify") or {"bucketPermissions": {"in.c-main": "read"}},
                ),
                patch("app.api.admin._validate_url_not_private", return_value=None),
                patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
            ):
                resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

            assert resp.status_code == 502, (boom, resp.text)
            assert called == [], f"per-bucket fallback was entered for {boom!r}"

    def test_tables_endpoint_scoped_token_falls_back_to_bucket_permissions(self, seeded_app):
        """Bucket-scoped (custom access) token: the project-wide listing is
        refused, but /tokens/verify names the permitted buckets — the
        endpoint must list per-bucket and mark the response scope."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-scoped")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=StorageApiError("accessDenied", status=403),
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {"in.c-main": "read"}},
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[
                    {"id": "in.c-main.orders", "name": "orders", "rowsCount": 42, "dataSizeBytes": 1024},
                ],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.get_bucket",
                return_value={"id": "in.c-main", "name": "main", "stage": "in", "description": ""},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "token_buckets"
        assert len(data["buckets"]) == 1
        bucket = data["buckets"][0]
        assert bucket["id"] == "in.c-main"
        assert bucket["name"] == "main"
        assert bucket["tables"] == [{"id": "in.c-main.orders", "name": "orders", "rows": 42, "size_bytes": 1024}]

    def test_tables_endpoint_scoped_fallback_survives_bucket_detail_failure(self, seeded_app):
        """get_bucket failing must not drop the bucket's tables — the bucket
        renders from its id instead."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-scoped-nodetail")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=StorageApiError("accessDenied", status=403),
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {"in.c-main": "read"}},
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[{"id": "in.c-main.orders", "name": "orders", "rowsCount": 1, "dataSizeBytes": 10}],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.get_bucket",
                side_effect=StorageApiError("accessDenied", status=403),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "token_buckets"
        assert data["buckets"][0]["id"] == "in.c-main"
        assert data["buckets"][0]["name"] == "c-main"  # synthesized from the id
        assert len(data["buckets"][0]["tables"]) == 1

    def test_tables_endpoint_no_bucket_permissions_surfaces_original_error(self, seeded_app):
        """Token with no bucketPermissions (e.g. component token): nothing to
        fall back to — the original project-wide failure is the story."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-noperms")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=StorageApiError("accessDenied original", status=403),
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {}},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 502
        assert "accessDenied original" in resp.json()["detail"]

    def test_tables_endpoint_network_error_maps_to_502(self, seeded_app):
        """requests-level failures (DNS, refused, TLS) must surface as the
        same clean 502 detail as Storage API errors — previously they fell
        through to the generic 500 handler."""
        import requests as _requests

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-network")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=_requests.ConnectionError("connection refused"),
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                side_effect=_requests.ConnectionError("connection refused"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 502
        assert resp.json()["detail"].startswith("keboola_storage_api_error:")

    def test_tables_endpoint_every_scoped_bucket_failing_returns_502(self, seeded_app):
        """Permissions exist but every per-bucket listing fails → a real 502,
        not a silently empty picker."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-allfail")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=StorageApiError("accessDenied", status=403),
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {"in.c-main": "read"}},
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                side_effect=StorageApiError("bucket listing broke", status=500),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 502
        assert "bucket listing broke" in resp.json()["detail"]

    def test_tables_endpoint_success_logs_counts(self, seeded_app, caplog):
        # Operator-facing trail of what the wizard loads: one INFO line with
        # bucket/table counts (+ duration), so "wizard is empty/slow" is
        # diagnosable from server logs.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-log-success")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                return_value=[{"id": "in.c-main", "name": "main", "stage": "in", "description": ""}],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[
                    {"id": "in.c-main.orders", "name": "orders", "bucket": {"id": "in.c-main"}},
                ],
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
            caplog.at_level("INFO", logger="app.api.admin_source_connections"),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200
        assert f"tables listing for connection {conn_id}" in caplog.text
        assert "1 buckets, 1 tables" in caplog.text

    def test_tables_endpoint_transport_error_logs_redacted_warning(self, seeded_app, caplog):
        # The 502 path must leave a server-side WARNING (the catch-all 500 it
        # replaced logged a full traceback) — with the token redacted.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-log-warning")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=requests.exceptions.ReadTimeout("read timed out; X-StorageApi-Token: fake-token"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
            caplog.at_level("WARNING", logger="app.api.admin_source_connections"),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 502
        assert f"tables listing failed for connection {conn_id}" in caplog.text
        assert "<redacted-storage-token>" in caplog.text
        assert "fake-token" not in caplog.text

    def test_tables_endpoint_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get(f"{BASE}/some-id/tables", headers=_auth(token))
        assert resp.status_code == 403


class TestSourceConnectionsMasterSecret:
    """PUT/DELETE .../secret with kind=master — Keboola master-token vault slot.

    Distinct from the plain ``kind=storage`` secret exercised by
    ``TestSourceConnectionsSecret``: this is validated at save time via a
    Storage API ``verify_token`` preflight (keboola-only, must be a master
    token), and stored under a separate vault key (``master_secret_key``).
    """

    @pytest.fixture(autouse=True)
    def _stable_vault_key(self, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        yield
        _reset_ephemeral_key_for_tests()

    def _create_keboola(self, c, token, *, name="test-master-kbc"):
        resp = c.post(
            BASE,
            json={
                "name": name,
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_master_secret_rejected_for_non_keboola(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-master-bq",
                "source_type": "bigquery",
                "config": {"project_id": "p"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        resp2 = c.put(
            f"{BASE}/{conn_id}/secret",
            json={"value": "some-master-token", "kind": "master"},
            headers=_auth(token),
        )
        assert resp2.status_code == 400
        assert resp2.json()["detail"] == "master_token_only_for_keboola"

    def test_master_secret_rejects_non_master_token(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-nonmaster")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"isMasterToken": False},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "not-a-master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 400
        assert "master" in resp.json()["detail"].lower()

    def test_master_secret_stores_and_reports(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-store")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"isMasterToken": True},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "a-real-master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 204

        detail = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert detail["has_master_secret"] is True
        assert detail["has_secret"] is False

        resp2 = c.delete(
            f"{BASE}/{conn_id}/secret",
            params={"kind": "master"},
            headers=_auth(token),
        )
        assert resp2.status_code == 204

        detail2 = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert detail2["has_master_secret"] is False
        assert detail2["has_secret"] is False

    def test_master_secret_storage_api_outage(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-outage")

        from connectors.keboola.storage_api import StorageApiError

        # The candidate token must appear in the simulated failure so this
        # assertion actually exercises the client's redaction rather than
        # trivially passing because the token was never in the message.
        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                side_effect=StorageApiError("boom: token=super-secret-master-token"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "super-secret-master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 502
        assert "super-secret-master-token" not in resp.text

    def test_master_secret_storage_api_outage_logs_redacted_warning(self, seeded_app, caplog):
        # The preflight 502 must leave a server-side WARNING with the
        # candidate token redacted — same trail as the /tables listing.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-outage-log")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                side_effect=StorageApiError("boom: token=super-secret-master-token"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            caplog.at_level("WARNING", logger="app.api.admin_source_connections"),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "super-secret-master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 502
        assert f"master-token preflight failed for connection {conn_id}" in caplog.text
        assert "<redacted-storage-token>" in caplog.text
        assert "super-secret-master-token" not in caplog.text

    def test_master_secret_rejected_token_is_a_400_not_a_gateway_error(self, seeded_app):
        """A 4xx from the Storage API means the pasted token is wrong — an
        admin-fixable mistake, not a gateway failure.

        Reported from a live instance: pasting a non-Storage token returned
        502, the operator read "Bad Gateway", and the incident was chased as
        an Agnes outage for a day. The detail still carries the upstream
        reason; only the status changes.
        """
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-bad-token")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                side_effect=StorageApiError(
                    "GET https://connection.example.com/v2/storage/tokens/verify -> HTTP 401: "
                    '{"error": "Invalid access token", "code": "storage.tokenInvalid"}',
                    status=401,
                ),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "wrong-kind-of-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 400, resp.text
        assert "storage.tokenInvalid" in resp.json()["detail"]

    def test_master_secret_upstream_5xx_is_still_a_502(self, seeded_app):
        """The gateway status survives for what it actually means."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-upstream-5xx")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                side_effect=StorageApiError("HTTP 503: upstream unavailable", status=503),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "a-real-master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 502, resp.text

    def test_master_secret_network_failure_is_still_a_502(self, seeded_app):
        """A transport error has no status at all — it must not be mistaken
        for a client error just because the classifier found no 4xx."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-netfail")

        import requests

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                side_effect=requests.ConnectionError("name resolution failed"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "a-real-master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 502, resp.text

    def test_connection_delete_clears_master_secret(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-delete-conn")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"isMasterToken": True},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "a-master-token-to-be-deleted", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 204

        from app.api.admin_source_connections import master_secret_key
        from src.repositories import connection_secrets_repo

        assert connection_secrets_repo().has(master_secret_key(conn_id)) is True

        resp2 = c.delete(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp2.status_code == 204

        assert connection_secrets_repo().has(master_secret_key(conn_id)) is False


class TestProjectIdentityBinding:
    """A connection is ONE Keboola project, and which one must be knowable.

    Reported from an instance running several Keboola projects: the connections
    looked identical (same stack host, a "master token: SET" badge on each) and
    nothing recorded which project any token actually opened. A master token
    pasted onto the wrong connection stored happily, and the semantic layer then
    synced that other project's metrics under this connection's name.
    """

    @pytest.fixture(autouse=True)
    def _stable_vault_key(self, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        yield
        _reset_ephemeral_key_for_tests()

    def _create_keboola(self, c, token, *, name):
        resp = c.post(
            BASE,
            json={
                "name": name,
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def _verify(self, *, project_id, project_name="Acme Analytics", master=True):
        return {"isMasterToken": master, "owner": {"id": project_id, "name": project_name}}

    def test_storage_token_binds_the_connection_to_its_project(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-identity-bind")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value=self._verify(project_id=1234, master=False),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "storage-token", "kind": "storage"},
                headers=_auth(token),
            )
        assert resp.status_code == 204, resp.text

        row = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert row["config"]["project_id"] == 1234
        assert row["config"]["project_name"] == "Acme Analytics"

    def test_master_token_from_another_project_is_refused(self, seeded_app):
        """The core failure: it used to store fine and badge "SET"."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-identity-mismatch")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value=self._verify(project_id=1234, master=False),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "storage-token", "kind": "storage"},
                headers=_auth(token),
            )

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value=self._verify(project_id=9999, project_name="Other Project"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "master-token-of-another-project", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert "project_mismatch" in detail
        # Both sides named: which project the token opens, and which one the
        # connection expects. Either alone leaves the admin guessing.
        assert "9999" in detail and "1234" in detail
        assert "Other Project" in detail and "Acme Analytics" in detail

        # ...and nothing was stored, so the badge cannot claim otherwise.
        row = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert row["has_master_secret"] is False

    def test_master_token_from_the_bound_project_is_accepted(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-identity-match")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value=self._verify(project_id=1234, master=False),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "storage-token", "kind": "storage"},
                headers=_auth(token),
            )

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value=self._verify(project_id=1234),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "the-right-master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 204, resp.text
        row = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert row["has_master_secret"] is True

    def test_unbound_connection_records_identity_from_the_master_token(self, seeded_app):
        """Connections that predate identity recording have nothing to
        contradict — the first verified token binds them rather than being
        rejected against a hole."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-identity-legacy")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value=self._verify(project_id=4242, project_name="Legacy Project"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 204, resp.text
        row = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert row["config"]["project_id"] == 4242

    def test_missing_vault_key_is_reported_before_any_upstream_call(self, seeded_app, monkeypatch):
        """Ordering, not just status: the storage-token preflight added here
        would otherwise ask Keboola to validate a secret this instance cannot
        store, then report a token error for what is really an unconfigured
        vault."""
        monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
        monkeypatch.setenv("LOCAL_DEV_MODE", "0")
        _reset_ephemeral_key_for_tests()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-identity-novault")

        with patch(
            "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
        ) as verify:
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "storage-token", "kind": "storage"},
                headers=_auth(token),
            )
        assert resp.status_code == 409, resp.text
        verify.assert_not_called()

    def test_test_endpoint_reports_a_disagreement_instead_of_re_binding(self, seeded_app):
        """/test must not quietly re-point a bound connection.

        The token /test resolves is not necessarily the one that established
        the binding (`_resolve_token` falls back to `token_env`), so
        overwriting on a probe would leave the stored master token failing a
        mismatch nobody caused. Devin Review on #1242.
        """
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-identity-probe")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value=self._verify(project_id=1234, master=False),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "storage-token", "kind": "storage"},
                headers=_auth(token),
            )

        probe = MagicMock()
        probe.status_code = 200
        probe.json.return_value = {"owner": {"id": 9999, "name": "Other Project"}}
        with (
            patch("httpx.AsyncClient.get", AsyncMock(return_value=probe)),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.post(f"{BASE}/{conn_id}/test", json={}, headers=_auth(token))

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert "project_mismatch" in body["error"]

        # The binding is untouched — this is the whole point.
        row = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert row["config"]["project_id"] == 1234

    def test_storage_token_still_saves_before_a_stack_url_exists(self, seeded_app):
        """The wizard creates the row before the config is complete, so
        requiring a stack_url to store the token broke half-built connections.
        Devin Review on #1242."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={"name": "test-identity-nostack", "source_type": "keboola", "config": {}},
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        conn_id = resp.json()["id"]

        with patch("app.api.admin_source_connections.KeboolaStorageClient.verify_token") as verify:
            r = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "storage-token", "kind": "storage"},
                headers=_auth(token),
            )
        assert r.status_code == 204, r.text
        # Nothing to preflight against, so nothing was asked and nothing bound.
        verify.assert_not_called()
        row = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert "project_id" not in (row["config"] or {})

    def test_moving_to_another_stack_clears_the_binding(self, seeded_app):
        """The escape hatch: a project id is only meaningful on its own stack,
        and without this a re-pointed connection would fail project_mismatch
        forever with no way to clear it. Devin Review on #1242."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-identity-restack")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value=self._verify(project_id=1234, master=False),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "storage-token", "kind": "storage"},
                headers=_auth(token),
            )
        assert c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()["config"]["project_id"] == 1234

        r = c.put(
            f"{BASE}/{conn_id}",
            json={"config": {"stack_url": "https://connection.other-stack.example.com"}},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        config = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()["config"]
        assert "project_id" not in config
        assert config["stack_url"] == "https://connection.other-stack.example.com"

    def test_owner_without_an_id_is_not_recorded_as_an_identity(self, seeded_app):
        """An identity we cannot read must not be persisted as a known one —
        otherwise the mismatch check compares against a hole and passes
        anything."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-identity-noowner")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"isMasterToken": True, "owner": {}},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 204, resp.text
        row = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert "project_id" not in (row["config"] or {})


class TestAnOrdinaryEditKeepsTheProjectBinding:
    """Devin Review on this PR: the safeguard was removable through the UI.

    `PUT /{id}` REPLACES the stored config, and the admin form posts only the
    fields it renders — `project_id`/`project_name` are recorded by the
    connection itself, not typed, so they are never in the payload. Every
    ordinary edit (a rename, a token_env change, re-saving the same form)
    therefore dropped the binding, and the next token from any project was
    accepted again. The deliberate clear on a stack move must survive.
    """

    @pytest.fixture(autouse=True)
    def _stable_vault_key(self, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        yield
        _reset_ephemeral_key_for_tests()

    def _bound(self, c, token, *, name):
        resp = c.post(
            BASE,
            json={
                "name": name,
                "source_type": "keboola",
                "config": {
                    "stack_url": "https://connection.example.com",
                    "project_id": 1234,
                    "project_name": "Acme Analytics",
                },
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def test_editing_the_name_keeps_the_binding(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._bound(c, token, name="edit-keeps-binding")

        r = c.put(
            f"{BASE}/{conn_id}",
            json={"name": "renamed", "config": {"stack_url": "https://connection.example.com"}},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        cfg = r.json()["config"]
        assert cfg.get("project_id") == 1234, "an ordinary edit disabled the wrong-token safeguard"
        assert cfg.get("project_name") == "Acme Analytics"

    def test_moving_to_another_stack_still_clears_it(self, seeded_app):
        """A project id means nothing on a different stack."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._bound(c, token, name="edit-moves-stack")

        r = c.put(
            f"{BASE}/{conn_id}",
            json={"config": {"stack_url": "https://other.example.com"}},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        cfg = r.json()["config"]
        assert "project_id" not in cfg
        assert "project_name" not in cfg

    def test_an_explicit_null_clears_it_without_moving_stack(self, seeded_app):
        """How a mis-recorded binding is reset from the UI."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = self._bound(c, token, name="edit-explicit-clear")

        r = c.put(
            f"{BASE}/{conn_id}",
            json={
                "config": {
                    "stack_url": "https://connection.example.com",
                    "project_id": None,
                    "project_name": None,
                }
            },
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["config"].get("project_id") is None


def test_the_admin_page_offers_a_way_out_of_a_project_binding():
    """Devin Review on this PR: the lock had no release.

    Making the binding survive an ordinary edit (its own fix) turned it into a
    one-way door: a connection is refused a token for any other project, and
    the page offered no control to clear the recorded identity — so
    re-pointing an existing connection at another project on the same stack
    became impossible from the UI.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
    ).read_text(encoding="utf-8")

    assert "function unbindProject(" in src
    assert 'onclick="unbindProject(' in src, "the control is defined but never rendered"
    # "Styled, not bare" — originally a page-local `.ds-unbind` rule. The card
    # rework moved the control onto the source card's own fact row, where it
    # takes the SHARED button (`.btn.btn-secondary`) like every other action on
    # that card. That is the same property, satisfied by the design system
    # instead of by a private class, so the guard asks for the shared one.
    unbind_line = next(line for line in src.splitlines() if 'onclick="unbindProject(' in line)
    assert "btn btn-secondary" in unbind_line, "the control must be styled, not bare"
    # It has to send explicit nulls: the handler carries the keys forward when
    # they are ABSENT, which is what stops an ordinary edit dropping them.
    assert "project_id: null" in src and "project_name: null" in src


def test_the_test_button_is_targeted_explicitly_not_by_position():
    """Devin Review on this PR: adding the unbind control moved the target.

    `testConn` disabled `card.querySelector("button")` — the FIRST button in
    the card — which stopped being Test the moment a control landed in the
    header above the action row. The link greyed out, Test stayed live, and
    repeated presses fired duplicate requests with no sign of progress.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
    ).read_text(encoding="utf-8")

    assert 'data-role="test"' in src, "the Test button carries no stable handle"
    assert 'card.querySelector("button")' not in src, "still selecting by position"
    # The original fix was to look the button up by that handle instead of by
    # position (`card.querySelector('button[data-role="test"]')`). Test has
    # since moved into the card's Actions menu, which is a fixed portal — there
    # is no button left on the card to grey out, so `testConn` opens the body
    # and reports "Testing…" there instead. Requiring the old lookup would be
    # requiring dead code; what must hold is that the press is acknowledged
    # somewhere, which is the failure the position bug actually caused.
    assert "setSourceOpen(id, true);" in src, "a press must open the body it reports into"
    assert '"Testing…"' in src, "a press must say it registered"


class TestOnlyKeboolaCarriesItsProjectForward:
    """Devin Review on this PR: the carry-forward was type-blind.

    On Keboola, `project_id`/`project_name` are RECORDED — written from the
    token's own owner block, never typed — so an absent key must mean
    "unchanged". On BigQuery, `project_id` is an ordinary field the admin
    types, and carrying it forward made it unclearable: emptying the field
    brought it straight back.
    """

    def test_a_bigquery_project_id_can_be_cleared(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        created = c.post(
            BASE,
            json={
                "name": "bq-clearable",
                "source_type": "bigquery",
                "config": {"project_id": "my-gcp-project", "dataset": "analytics"},
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        conn_id = created.json()["id"]

        r = c.put(f"{BASE}/{conn_id}", json={"config": {"dataset": "analytics"}}, headers=_auth(token))
        assert r.status_code == 200, r.text
        assert "project_id" not in r.json()["config"], "a typed BigQuery field could not be cleared"

    def test_a_keboola_binding_is_still_carried_forward(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        created = c.post(
            BASE,
            json={
                "name": "kbc-still-bound",
                "source_type": "keboola",
                "config": {
                    "stack_url": "https://connection.example.com",
                    "project_id": 1234,
                    "project_name": "Acme",
                },
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        conn_id = created.json()["id"]

        r = c.put(
            f"{BASE}/{conn_id}",
            json={"name": "renamed", "config": {"stack_url": "https://connection.example.com"}},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["config"].get("project_id") == 1234


def test_the_add_project_wizard_reuses_its_connection_on_retry():
    """Devin Review on this PR: every failed attempt left a stray project.

    The wizard creates the connection first, and every step after that can
    fail — a mistyped token being the common one. It returned with the row
    already saved, so a retry re-ran step 1 and minted another "Untitled
    project" per attempt, leaving the admin to clean them up.
    """
    import pathlib
    import re
    import subprocess
    import tempfile

    page = pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
    src = page.read_text(encoding="utf-8")

    assert "if (_wizardConnId) {" in src, "the wizard does not reuse the connection it already created"
    reuse = src.index("if (_wizardConnId) {")
    create = src.index("const createResp = await fetch(API_CONNECTIONS,")
    assert reuse < create, "the reuse check must come before creating another connection"
    assert "this connection is reused, not duplicated" in src, "the failure message still implies a leftover"
    # …and reuse must APPLY what the admin corrected. Without this the wizard
    # told them to fix the URL and retry, then talked to the original address
    # every time, failing identically with nothing saying the new value was
    # ignored. (Devin Review, second pass.)
    reuse_block = src[reuse : src.index("// 2. Store the token.")]
    assert "stack_url: stack" in reuse_block, "a corrected URL is never saved on retry"
    assert (
        reuse_block.index("stack_url: stack") < reuse_block.index("await fetch(API_CONNECTIONS,")
        if "await fetch(API_CONNECTIONS," in reuse_block
        else True
    )

    # The restructure moved a `return` inside a new block — parse the page's
    # script to be sure it is still valid JS, since nothing else here would.
    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", src, re.S)
    assert blocks, "no inline script found — re-point this guard"
    js = re.sub(r"\{%.*?%\}", "", "\n".join(blocks), flags=re.S)
    js = re.sub(r"\{\{.*?\}\}", '"JINJA"', js, flags=re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        path = f.name
    proc = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    pathlib.Path(path).unlink(missing_ok=True)
    if proc.returncode == 127:
        return  # node unavailable
    assert proc.returncode == 0, proc.stderr


def test_the_wizard_retry_also_applies_a_corrected_name():
    """Devin Review on this PR: only the URL was re-saved.

    A name the admin fixed on the retry was thrown away while the success
    banner went on to claim that name was used.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
    ).read_text(encoding="utf-8")
    reuse = src.index("if (_wizardConnId) {")
    block = src[reuse : src.index("// 2. Store the token.")]
    assert "name ? { name, config:" in block, "a corrected name is still discarded on retry"


class TestBookkeepingCannotFailAPassingConnectionTest:
    """Devin Review on this PR: `_record_project_identity` sat inside the
    network try/except, so a vault or DB fault surfaced to the admin as
    "connection test failed" — with a database message — for a project that
    is in fact correctly configured."""

    def test_the_identity_write_is_isolated(self):
        import inspect

        from app.api import admin_source_connections as mod

        src = inspect.getsource(mod.test_connection)
        i = src.index("_record_project_identity(connection_id, row, data)")
        preceding = src[:i]
        assert preceding.rstrip().endswith("try:"), (
            "the identity write is not wrapped in its own try — a bookkeeping "
            "failure still reports as a failed connectivity check"
        )
        after = src[i:]
        assert "passed but its project identity could not be recorded" in after


def test_the_secret_path_bookkeeping_is_isolated_too():
    """Devin Review on this PR: I fixed `/test` and left `PUT /secret`.

    The token is safely stored before `_record_project_identity` runs, so a
    vault or DB fault there turned an already-successful save into an error
    response — the admin retries a store that already worked.

    The preflight/persist/identity-recording body now lives in
    `_store_connection_secret`, shared with the Keboola "Import as managed
    connection" vault-seeding step — `set_connection_secret` itself is a
    thin wrapper around it, so the isolation guard targets the function
    that actually does the work.
    """
    import inspect

    from app.api import admin_source_connections as mod

    src = inspect.getsource(mod._store_connection_secret)
    i = src.index("_record_project_identity(connection_id, row, info)")
    assert src[:i].rstrip().endswith("try:"), "the identity write is not isolated on the secret path"
    assert "could not record its project identity" in src[i:]


def test_a_failed_sync_reports_one_project_s_reason_and_code():
    """Devin Review on this PR: two independent `next(...)` scans.

    With several projects failing, the message could come from one and the
    failure type from another — the worst pairing when only one of them is
    the project the admin is debugging.
    """
    from connectors.keboola.semantic_layer import _aggregate_sources

    sources = [
        {"connection_id": "a", "status": "error", "error": "bad token for A", "code": "invalid_token"},
        {"connection_id": "b", "status": "error", "error": "stack unreachable for B", "code": "upstream_down"},
    ]
    out = _aggregate_sources(sources)
    assert out["status"] == "error"
    assert (out["error"], out["code"]) in (
        ("bad token for A", "invalid_token"),
        ("stack unreachable for B", "upstream_down"),
    ), (out["error"], out.get("code"))


def test_the_error_formatter_is_declared_once():
    """Devin Review on #1249: a rebase left two identical declarations.

    The later copy silently replaces the earlier one — harmless while they
    agree, and a trap the moment one is edited: the edit would appear to do
    nothing.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
    ).read_text(encoding="utf-8")
    assert src.count("function detailMessage") == 1, "detailMessage is declared more than once"


def test_the_wizard_rejects_http_the_way_the_server_does():
    """Devin Review on #1249: the check and its own message disagreed.

    It read `startsWith("http")` while telling the admin the URL must start
    with `https://` — and the server rejects `http://`, so the form waved the
    input through and the server bounced it.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
    ).read_text(encoding="utf-8")
    assert 'startsWith("https://")' in src
    assert 'startsWith("http")' not in src.replace('startsWith("https://")', "")
