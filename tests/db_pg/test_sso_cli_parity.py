"""API ↔ CLI parity for `agnes admin sso …` (design 2026-08-28).

The SSO config repos are PG-only (A3 ratchet), so these parity cases live
here (with the ``pg_engine`` fixture) rather than in
``tests/test_cli_api_parity.py``, whose harness snapshots the DuckDB
app-state DB. Same discipline: fire the HTTP path and the CLI path against
the same backend and assert they leave identical state.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from cli.main import app as cli_app
from tests.db_pg._parity_sweep_util import build_seeded_client
from tests.test_cli_api_parity import _patch_cli_to_testclient

TENANT_GUID = "11111111-2222-3333-4444-555555555555"
_SSO_CLI_MODULES = ["cli.commands.admin_sso"]

runner = CliRunner()


@pytest.fixture
def sso_cli(tmp_path, monkeypatch, pg_engine):
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _patch_cli_to_testclient(monkeypatch, _SSO_CLI_MODULES, client, admin_token)
    return client, admin_token


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _config_state(pg_engine):
    with pg_engine.connect() as conn:
        row = (
            conn.execute(
                sa.text(
                    "SELECT provider_type, tenant_id, client_id, display_name, "
                    "allowed_email_domains, enabled, (client_secret_enc IS NOT NULL) AS has_secret "
                    "FROM sso_config"
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _reset(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM sso_config"))
        conn.execute(sa.text("DELETE FROM user_external_identities"))
        conn.execute(sa.text("DELETE FROM users WHERE id LIKE 'u-sso-%'"))


def test_set_and_set_secret_parity(sso_cli, pg_engine):
    client, admin_token = sso_cli

    # API path
    r = client.put(
        "/api/admin/sso/config",
        json={
            "tenant_id": TENANT_GUID,
            "client_id": "app-client",
            "display_name": "Fabrikam",
            "allowed_email_domains": ["fabrikam.com", "partners.fabrikam.com"],
            "enabled": False,
        },
        headers=_h(admin_token),
    )
    assert r.status_code == 200
    r = client.put("/api/admin/sso/client-secret", json={"value": "s3cret"}, headers=_h(admin_token))
    assert r.status_code == 204
    api_state = _config_state(pg_engine)

    _reset(pg_engine)

    # CLI path — same inputs; the secret comes from the hidden prompt.
    result = runner.invoke(
        cli_app,
        [
            "admin",
            "sso",
            "set",
            "--tenant-id",
            TENANT_GUID,
            "--client-id",
            "app-client",
            "--display-name",
            "Fabrikam",
            "--domains",
            "fabrikam.com,partners.fabrikam.com",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_app, ["admin", "sso", "set-secret"], input="s3cret\n")
    assert result.exit_code == 0, result.output
    cli_state = _config_state(pg_engine)

    # Fernet ciphertexts differ per encryption — compare presence, not bytes.
    assert api_state == cli_state


def test_enable_disable_parity(sso_cli, pg_engine):
    client, admin_token = sso_cli

    def _seed():
        _reset(pg_engine)
        client.put(
            "/api/admin/sso/config",
            json={
                "tenant_id": TENANT_GUID,
                "client_id": "app-client",
                "display_name": "Fabrikam",
                "allowed_email_domains": ["fabrikam.com"],
                "enabled": False,
            },
            headers=_h(admin_token),
        )
        client.put("/api/admin/sso/client-secret", json={"value": "s3cret"}, headers=_h(admin_token))

    _seed()
    r = client.put(
        "/api/admin/sso/config",
        json={
            "tenant_id": TENANT_GUID,
            "client_id": "app-client",
            "display_name": "Fabrikam",
            "allowed_email_domains": ["fabrikam.com"],
            "enabled": True,
        },
        headers=_h(admin_token),
    )
    assert r.status_code == 200
    api_state = _config_state(pg_engine)

    _seed()
    result = runner.invoke(cli_app, ["admin", "sso", "set", "--enable"])
    assert result.exit_code == 0, result.output
    cli_state = _config_state(pg_engine)
    assert api_state == cli_state
    assert cli_state["enabled"] is True


def test_delete_parity_requires_yes_and_keeps_identities(sso_cli, pg_engine):
    client, admin_token = sso_cli

    def _seed():
        _reset(pg_engine)
        client.put(
            "/api/admin/sso/config",
            json={
                "tenant_id": TENANT_GUID,
                "client_id": "app-client",
                "display_name": "Fabrikam",
                "allowed_email_domains": ["fabrikam.com"],
                "enabled": False,
            },
            headers=_h(admin_token),
        )
        from src.repositories import user_external_identities_repo, users_repo

        users_repo().create(id="u-sso-1", email="one@fabrikam.com", name="One")
        user_external_identities_repo().link(
            user_id="u-sso-1",
            provider_type="entra_oidc",
            tenant_id=TENANT_GUID,
            subject="oid-1",
            email_at_link="one@fabrikam.com",
        )

    _seed()
    assert client.delete("/api/admin/sso/config", headers=_h(admin_token)).status_code == 204
    assert _config_state(pg_engine) is None
    from src.repositories import user_external_identities_repo

    assert user_external_identities_repo().get_by_user_id("u-sso-1") is not None

    _seed()
    # Without --yes the CLI must not delete.
    result = runner.invoke(cli_app, ["admin", "sso", "delete"], input="n\n")
    assert _config_state(pg_engine) is not None
    result = runner.invoke(cli_app, ["admin", "sso", "delete", "--yes"])
    assert result.exit_code == 0, result.output
    assert _config_state(pg_engine) is None
    assert user_external_identities_repo().get_by_user_id("u-sso-1") is not None


def test_clear_secret_and_unlink_parity(sso_cli, pg_engine):
    client, admin_token = sso_cli

    def _seed():
        _reset(pg_engine)
        client.put(
            "/api/admin/sso/config",
            json={
                "tenant_id": TENANT_GUID,
                "client_id": "app-client",
                "display_name": "Fabrikam",
                "allowed_email_domains": ["fabrikam.com"],
                "enabled": False,
            },
            headers=_h(admin_token),
        )
        client.put("/api/admin/sso/client-secret", json={"value": "s3cret"}, headers=_h(admin_token))
        from src.repositories import user_external_identities_repo, users_repo

        users_repo().create(id="u-sso-2", email="two@fabrikam.com", name="Two")
        user_external_identities_repo().link(
            user_id="u-sso-2",
            provider_type="entra_oidc",
            tenant_id=TENANT_GUID,
            subject="oid-2",
            email_at_link="two@fabrikam.com",
        )

    from src.repositories import user_external_identities_repo

    _seed()
    client.delete("/api/admin/sso/client-secret", headers=_h(admin_token))
    client.delete("/api/admin/sso/identities/u-sso-2", headers=_h(admin_token))
    api_state = _config_state(pg_engine)
    assert api_state["has_secret"] is False
    assert user_external_identities_repo().get_by_user_id("u-sso-2") is None

    _seed()
    result = runner.invoke(cli_app, ["admin", "sso", "clear-secret", "--yes"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_app, ["admin", "sso", "unlink", "u-sso-2", "--yes"])
    assert result.exit_code == 0, result.output
    assert _config_state(pg_engine) == api_state
    assert user_external_identities_repo().get_by_user_id("u-sso-2") is None


def test_status_and_identities_read_commands(sso_cli, pg_engine):
    client, admin_token = sso_cli
    client.put(
        "/api/admin/sso/config",
        json={
            "tenant_id": TENANT_GUID,
            "client_id": "app-client",
            "display_name": "Fabrikam",
            "allowed_email_domains": ["fabrikam.com"],
            "enabled": False,
        },
        headers=_h(admin_token),
    )

    result = runner.invoke(cli_app, ["admin", "sso", "status"])
    assert result.exit_code == 0, result.output
    assert "Fabrikam" in result.output
    assert "disabled" in result.output.lower() or "enabled: false" in result.output.lower()

    result = runner.invoke(cli_app, ["admin", "sso", "status", "--json"])
    assert result.exit_code == 0, result.output
    import json as _json

    body = _json.loads(result.output)
    assert body["configured"] is True
    assert body["tenant_id"] == TENANT_GUID

    result = runner.invoke(cli_app, ["admin", "sso", "identities", "--json"])
    assert result.exit_code == 0, result.output
    body = _json.loads(result.output)
    assert body["total"] == 0
    assert body["identities"] == []
