"""Admin API tests for the external SSO login config (design 2026-08-28).

``/api/admin/sso/*`` — config CRUD, write-only client secret, the
last-login-door guard, the server-side test-config probe, and the paginated
identities list. PG-backed via ``build_seeded_client`` (the repos are
PG-only, A3 ratchet); the DuckDB half is the typed-501 contract asserted at
the bottom and by the parity sweeps' ``_PG_ONLY_ROUTE_EXEMPTIONS``.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from tests.db_pg._parity_sweep_util import build_seeded_client

TENANT_GUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def sso_env(tmp_path, monkeypatch, pg_engine):
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    from app.auth.jwt import create_access_token

    analyst_token = create_access_token("analyst1", "analyst@test.com")
    return client, admin_token, analyst_token


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _valid_body(**overrides):
    body = {
        "tenant_id": TENANT_GUID,
        "client_id": "app-client-id",
        "display_name": "Fabrikam",
        "allowed_email_domains": ["fabrikam.com"],
        "enabled": False,
    }
    body.update(overrides)
    return body


def _put_config(client, token, **overrides):
    return client.put("/api/admin/sso/config", json=_valid_body(**overrides), headers=_h(token))


def _audit_actions(pg_engine) -> list[str]:
    with pg_engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT action FROM audit_log ORDER BY timestamp")).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# gate + config CRUD
# ---------------------------------------------------------------------------


def test_admin_gate(sso_env):
    client, admin_token, analyst_token = sso_env
    assert client.get("/api/admin/sso/config").status_code == 401
    assert client.get("/api/admin/sso/config", headers=_h(analyst_token)).status_code == 403
    assert client.get("/api/admin/sso/config", headers=_h(admin_token)).status_code == 200


def test_get_config_unconfigured(sso_env):
    client, admin_token, _ = sso_env
    data = client.get("/api/admin/sso/config", headers=_h(admin_token)).json()
    assert data["configured"] is False
    assert data["enabled"] is False
    assert data["vault_key_configured"] is True


def test_put_config_rejects_reserved_and_malformed_tenants(sso_env):
    client, admin_token, _ = sso_env
    for bad in ("common", "organizations", "consumers", "not a tenant!", ""):
        resp = _put_config(client, admin_token, tenant_id=bad)
        assert resp.status_code == 422, bad


def test_put_config_rejects_empty_display_name_and_domains(sso_env):
    client, admin_token, _ = sso_env
    assert _put_config(client, admin_token, display_name="   ").status_code == 422
    assert _put_config(client, admin_token, allowed_email_domains=[]).status_code == 422
    assert _put_config(client, admin_token, allowed_email_domains=["  ", ""]).status_code == 422


def test_put_config_round_trip_and_audit(sso_env, pg_engine):
    client, admin_token, _ = sso_env
    resp = _put_config(client, admin_token, allowed_email_domains=["Fabrikam.COM", "partners.fabrikam.com"])
    assert resp.status_code == 200

    data = client.get("/api/admin/sso/config", headers=_h(admin_token)).json()
    assert data["configured"] is True
    assert data["enabled"] is False
    assert data["provider_type"] == "entra_oidc"
    assert data["tenant_id"] == TENANT_GUID
    assert data["client_id"] == "app-client-id"
    assert data["display_name"] == "Fabrikam"
    assert data["allowed_email_domains"] == ["fabrikam.com", "partners.fabrikam.com"]
    assert data["has_client_secret"] is False
    assert data["updated_by"] == "admin1"
    assert data["updated_at"] is not None
    assert "sso.config.set" in _audit_actions(pg_engine)


def test_put_config_accepts_verified_domain_tenant(sso_env):
    client, admin_token, _ = sso_env
    assert _put_config(client, admin_token, tenant_id="fabrikam.onmicrosoft.com").status_code == 200


def test_enable_requires_stored_decryptable_secret(sso_env, pg_engine):
    client, admin_token, _ = sso_env
    resp = _put_config(client, admin_token, enabled=True)
    assert resp.status_code == 422
    assert "secret" in resp.json()["detail"]

    assert _put_config(client, admin_token).status_code == 200
    r = client.put("/api/admin/sso/client-secret", json={"value": "s3cret"}, headers=_h(admin_token))
    assert r.status_code == 204
    resp = _put_config(client, admin_token, enabled=True)
    assert resp.status_code == 200
    assert client.get("/api/admin/sso/config", headers=_h(admin_token)).json()["enabled"] is True
    actions = _audit_actions(pg_engine)
    assert "sso.config.enable" in actions
    assert "sso.secret.set" in actions


# ---------------------------------------------------------------------------
# client secret (write-only)
# ---------------------------------------------------------------------------


def test_set_secret_without_config_is_404(sso_env):
    client, admin_token, _ = sso_env
    r = client.put("/api/admin/sso/client-secret", json={"value": "s3cret"}, headers=_h(admin_token))
    assert r.status_code == 404


def test_set_secret_without_vault_key_is_409(sso_env, monkeypatch):
    client, admin_token, _ = sso_env
    _put_config(client, admin_token)
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    r = client.put("/api/admin/sso/client-secret", json={"value": "s3cret"}, headers=_h(admin_token))
    assert r.status_code == 409
    assert "vault_key_not_configured" in r.json()["detail"]


def test_secret_value_never_leaves_the_api(sso_env, pg_engine):
    client, admin_token, _ = sso_env
    _put_config(client, admin_token)
    client.put("/api/admin/sso/client-secret", json={"value": "s3cret-value"}, headers=_h(admin_token))

    data = client.get("/api/admin/sso/config", headers=_h(admin_token)).json()
    assert data["has_client_secret"] is True
    assert "s3cret-value" not in str(data)
    # Audit rows carry no values either.
    with pg_engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT params FROM audit_log")).fetchall()
    assert "s3cret-value" not in str(rows)


def test_clear_secret(sso_env, pg_engine):
    client, admin_token, _ = sso_env
    _put_config(client, admin_token)
    client.put("/api/admin/sso/client-secret", json={"value": "s3cret"}, headers=_h(admin_token))
    r = client.delete("/api/admin/sso/client-secret", headers=_h(admin_token))
    assert r.status_code == 204
    assert client.get("/api/admin/sso/config", headers=_h(admin_token)).json()["has_client_secret"] is False
    assert "sso.secret.delete" in _audit_actions(pg_engine)


# ---------------------------------------------------------------------------
# delete config + identity retention
# ---------------------------------------------------------------------------


def _seed_identity(user_id: str, email: str, subject: str = "oid-1") -> None:
    from src.repositories import user_external_identities_repo, users_repo

    users_repo().create(id=user_id, email=email, name="U")
    user_external_identities_repo().link(
        user_id=user_id,
        provider_type="entra_oidc",
        tenant_id=TENANT_GUID,
        subject=subject,
        email_at_link=email,
    )


def test_delete_config_keeps_identity_rows(sso_env, pg_engine):
    client, admin_token, _ = sso_env
    _put_config(client, admin_token)
    _seed_identity("u-keep", "keep@fabrikam.com")

    r = client.delete("/api/admin/sso/config", headers=_h(admin_token))
    assert r.status_code == 204
    assert client.get("/api/admin/sso/config", headers=_h(admin_token)).json()["configured"] is False

    from src.repositories import user_external_identities_repo

    assert user_external_identities_repo().get_by_user_id("u-keep") is not None
    assert "sso.config.delete" in _audit_actions(pg_engine)


def test_delete_config_when_unconfigured_is_404(sso_env):
    client, admin_token, _ = sso_env
    assert client.delete("/api/admin/sso/config", headers=_h(admin_token)).status_code == 404


# ---------------------------------------------------------------------------
# last-login-door guard
# ---------------------------------------------------------------------------


def _enable_sso(client, admin_token):
    assert _put_config(client, admin_token).status_code == 200
    assert (
        client.put("/api/admin/sso/client-secret", json={"value": "s3cret"}, headers=_h(admin_token)).status_code == 204
    )
    assert _put_config(client, admin_token, enabled=True).status_code == 200


def test_last_login_door_guard_refuses_all_three_transitions(sso_env, monkeypatch):
    client, admin_token, _ = sso_env
    monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "sso")
    _enable_sso(client, admin_token)

    # No other door: allowlist names only sso, no user holds a password.
    r = _put_config(client, admin_token, enabled=False)
    assert r.status_code == 422
    assert "last_login_door" in r.json()["detail"]
    r = client.delete("/api/admin/sso/client-secret", headers=_h(admin_token))
    assert r.status_code == 422
    assert "last_login_door" in r.json()["detail"]
    r = client.delete("/api/admin/sso/config", headers=_h(admin_token))
    assert r.status_code == 422
    assert "last_login_door" in r.json()["detail"]

    # SSO stayed fully usable.
    data = client.get("/api/admin/sso/config", headers=_h(admin_token)).json()
    assert data["enabled"] is True
    assert data["has_client_secret"] is True


def test_last_login_door_guard_allows_when_password_door_usable(sso_env, monkeypatch, pg_engine):
    client, admin_token, _ = sso_env
    monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "sso,password")
    _enable_sso(client, admin_token)

    r = _put_config(client, admin_token, enabled=False)
    assert r.status_code == 422  # password allowed but nobody holds one

    from src.repositories import users_repo

    users_repo().create(id="holder", email="holder@test.com", name="H", password_hash="x" * 60)
    r = _put_config(client, admin_token, enabled=False)
    assert r.status_code == 200
    assert client.get("/api/admin/sso/config", headers=_h(admin_token)).json()["enabled"] is False


def test_guard_inactive_when_sso_is_not_a_usable_door(sso_env, monkeypatch):
    """Deleting a DISABLED config removes no login door — never refused."""
    client, admin_token, _ = sso_env
    monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "sso")
    _put_config(client, admin_token)  # enabled=False
    assert client.delete("/api/admin/sso/config", headers=_h(admin_token)).status_code == 204


# ---------------------------------------------------------------------------
# test-config probe
# ---------------------------------------------------------------------------


def test_test_config_reports_missing_config_and_secret(sso_env):
    client, admin_token, _ = sso_env
    assert client.post("/api/admin/sso/test-config", headers=_h(admin_token)).status_code == 404

    _put_config(client, admin_token)
    r = client.post("/api/admin/sso/test-config", headers=_h(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "secret" in body["error"]


def test_test_config_fetches_discovery(sso_env, monkeypatch):
    client, admin_token, _ = sso_env
    _put_config(client, admin_token)
    client.put("/api/admin/sso/client-secret", json={"value": "s3cret"}, headers=_h(admin_token))

    from app.api import admin_sso

    seen: dict = {}

    def fake_fetch(url: str) -> dict:
        seen["url"] = url
        return {
            "issuer": f"https://login.microsoftonline.com/{TENANT_GUID}/v2.0",
            "authorization_endpoint": "https://login.microsoftonline.com/x/oauth2/v2.0/authorize",
            "token_endpoint": "https://login.microsoftonline.com/x/oauth2/v2.0/token",
        }

    monkeypatch.setattr(admin_sso, "_fetch_discovery_document", fake_fetch)
    r = client.post("/api/admin/sso/test-config", headers=_h(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["issuer"].endswith("/v2.0")
    assert body["authorization_endpoint"]
    assert body["token_endpoint"]
    # Fixed host, tenant quoted into a single path segment.
    assert seen["url"] == (f"https://login.microsoftonline.com/{TENANT_GUID}/v2.0/.well-known/openid-configuration")


# ---------------------------------------------------------------------------
# identities list + unlink
# ---------------------------------------------------------------------------


def test_identities_pagination(sso_env):
    client, admin_token, _ = sso_env
    for i in range(3):
        _seed_identity(f"u{i}", f"user{i}@fabrikam.com", subject=f"oid-{i}")

    r = client.get("/api/admin/sso/identities", headers=_h(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["identities"]) == 3
    row = body["identities"][0]
    assert {"user_id", "email", "subject", "tenant_id", "linked_at", "last_login_at"} <= set(row)

    body = client.get("/api/admin/sso/identities?limit=2&offset=2", headers=_h(admin_token)).json()
    assert body["total"] == 3
    assert len(body["identities"]) == 1

    # Limit is clamped to the 200 max; offset past the end is an empty page.
    body = client.get("/api/admin/sso/identities?limit=9999", headers=_h(admin_token)).json()
    assert body["limit"] == 200
    body = client.get("/api/admin/sso/identities?offset=50", headers=_h(admin_token)).json()
    assert body["identities"] == []


def test_unlink_identity(sso_env, pg_engine):
    client, admin_token, _ = sso_env
    _seed_identity("u-x", "x@fabrikam.com")

    r = client.delete("/api/admin/sso/identities/u-x", headers=_h(admin_token))
    assert r.status_code == 204
    from src.repositories import user_external_identities_repo, users_repo

    assert user_external_identities_repo().get_by_user_id("u-x") is None
    # The user row itself is untouched.
    assert users_repo().get_by_id("u-x") is not None
    assert "sso.identity.unlinked" in _audit_actions(pg_engine)

    assert client.delete("/api/admin/sso/identities/u-x", headers=_h(admin_token)).status_code == 404


# ---------------------------------------------------------------------------
# DuckDB backend fails clean (typed 501)
# ---------------------------------------------------------------------------


def test_duckdb_backend_answers_typed_501(tmp_path, monkeypatch, pg_engine):
    client, admin_token = build_seeded_client("duckdb", tmp_path, monkeypatch, pg_engine)
    r = client.get("/api/admin/sso/config", headers=_h(admin_token))
    assert r.status_code == 501
    assert r.json()["error"] == "requires_postgres_backend"


# ---------------------------------------------------------------------------
# GET /api/me/external-identity + profile line (PG side)
# ---------------------------------------------------------------------------


def test_me_external_identity_not_linked(sso_env):
    client, admin_token, _ = sso_env
    r = client.get("/api/me/external-identity", headers=_h(admin_token))
    assert r.status_code == 200
    assert r.json() == {"linked": False}


def test_me_external_identity_linked_shape(sso_env):
    client, _, analyst_token = sso_env
    from src.repositories import user_external_identities_repo

    user_external_identities_repo().link(
        user_id="analyst1",
        provider_type="entra_oidc",
        tenant_id=TENANT_GUID,
        subject="oid-analyst",
        email_at_link="analyst@test.com",
    )
    r = client.get("/api/me/external-identity", headers=_h(analyst_token))
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is True
    assert body["provider_type"] == "entra_oidc"
    assert body["tenant_id"] == TENANT_GUID
    assert body["subject"] == "oid-analyst"
    assert body["linked_at"] is not None
    assert body["last_login_at"] is None


def test_profile_page_shows_linked_identity(sso_env):
    client, _, analyst_token = sso_env
    from src.repositories import user_external_identities_repo

    client.cookies.set("access_token", analyst_token)
    page = client.get("/me/profile").text
    assert "Linked identity" in page

    user_external_identities_repo().link(
        user_id="analyst1",
        provider_type="entra_oidc",
        tenant_id=TENANT_GUID,
        subject="oid-analyst",
        email_at_link="analyst@test.com",
    )
    page = client.get("/me/profile").text
    assert "oid-analyst" in page
