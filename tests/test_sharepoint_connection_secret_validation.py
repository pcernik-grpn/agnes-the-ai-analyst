"""SharePoint credential UX hardening.

Two failure modes this file pins, both hit by real admins:

1. ``PUT /api/admin/source-connections/{id}/secret`` on a SharePoint
   connection used to accept ANY string and fail only at the first Graph
   call — an opaque provider auth error hours later. The PUT now validates
   the combined PEM (a parseable CERTIFICATE block + a parseable PRIVATE
   KEY block) and answers a typed 400 naming exactly which half is missing.

2. The create/update ``token_env`` / ``cert_private_key_env`` allowlist
   guard answered every wrong input with the same expert-facing message.
   The two common mistakes — pasting a cloud secret-manager secret NAME
   where an env-var name belongs, and pasting the PEM CONTENT into the
   name field — now get targeted messages that point at the vault upload
   path, and the message names the actual field (``cert_private_key_env``
   was previously reported as ``token_env``).
"""

import datetime

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.secrets_vault import _reset_ephemeral_key_for_tests

BASE = "/api/admin/source-connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _self_signed_pem() -> str:
    """Throwaway self-signed certificate + private key, concatenated — the
    combined-PEM shape ``SharePointSettings.private_key`` documents (same
    helper as ``tests/test_sharepoint_graph_client.py``)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agnes-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return cert_pem + key_pem


COMBINED_PEM = _self_signed_pem()
CERT_ONLY_PEM = COMBINED_PEM.split("-----BEGIN PRIVATE KEY-----", 1)[0]
KEY_ONLY_PEM = "-----BEGIN PRIVATE KEY-----" + COMBINED_PEM.split("-----BEGIN PRIVATE KEY-----", 1)[1]


@pytest.fixture()
def sp_client(seeded_app, monkeypatch):
    """Admin client + a fresh SharePoint connection id, with a stable vault
    key so the 204 path can actually store."""
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.post(
        BASE,
        json={
            "name": f"sp-secret-validation-{id(monkeypatch)}",
            "source_type": "sharepoint",
            "config": {
                "tenant_id": "11111111-1111-1111-1111-111111111111",
                "client_id": "app-client-id",
            },
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    yield c, token, resp.json()["id"]
    _reset_ephemeral_key_for_tests()


class TestSharePointSecretPutValidatesPem:
    def test_combined_pem_is_accepted_and_stored(self, sp_client):
        c, token, conn_id = sp_client
        resp = c.put(f"{BASE}/{conn_id}/secret", json={"value": COMBINED_PEM}, headers=_auth(token))
        assert resp.status_code == 204, resp.text
        row = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert row["has_secret"] is True

    def test_key_only_pem_is_rejected_naming_the_missing_certificate(self, sp_client):
        c, token, conn_id = sp_client
        resp = c.put(f"{BASE}/{conn_id}/secret", json={"value": KEY_ONLY_PEM}, headers=_auth(token))
        assert resp.status_code == 400, resp.text
        assert "sharepoint_pem_invalid" in resp.text
        assert "CERTIFICATE" in resp.text
        # The fix is spelled out: one PEM, certificate then key.
        assert "one PEM" in resp.text

    def test_cert_only_pem_is_rejected_naming_the_missing_key(self, sp_client):
        c, token, conn_id = sp_client
        resp = c.put(f"{BASE}/{conn_id}/secret", json={"value": CERT_ONLY_PEM}, headers=_auth(token))
        assert resp.status_code == 400, resp.text
        assert "sharepoint_pem_invalid" in resp.text
        assert "PRIVATE KEY" in resp.text

    def test_non_pem_garbage_is_rejected(self, sp_client):
        c, token, conn_id = sp_client
        resp = c.put(f"{BASE}/{conn_id}/secret", json={"value": "not a pem at all"}, headers=_auth(token))
        assert resp.status_code == 400, resp.text
        assert "sharepoint_pem_invalid" in resp.text

    def test_rejected_pem_is_not_stored(self, sp_client):
        c, token, conn_id = sp_client
        c.put(f"{BASE}/{conn_id}/secret", json={"value": "not a pem at all"}, headers=_auth(token))
        row = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert row["has_secret"] is False

    def test_other_source_types_still_accept_opaque_tokens(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "sf-opaque-token-ok",
                "source_type": "snowflake",
                "config": {"account": "acme", "user": "svc", "database": "DB", "warehouse": "WH"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        conn_id = resp.json()["id"]
        resp = c.put(f"{BASE}/{conn_id}/secret", json={"value": "plain-password"}, headers=_auth(token))
        assert resp.status_code == 204, resp.text
        _reset_ephemeral_key_for_tests()


class TestGuardMessagesNameTheMistake:
    def test_secret_manager_style_name_gets_the_vault_pointer(self, seeded_app):
        """A lowercase-with-dashes value is a cloud secret-store NAME, not an
        env var of the server process — the message says so and points at
        the vault upload instead of the env-var machinery."""
        c = seeded_app["client"]
        resp = c.post(
            BASE,
            json={
                "name": "sp-sm-name",
                "source_type": "sharepoint",
                "config": {
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "client_id": "app-client-id",
                    "cert_private_key_env": "example-sharepoint-connector-cert",
                },
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "secret manager" in resp.text
        assert "--from-file" in resp.text

    def test_config_field_error_names_the_actual_field(self, seeded_app):
        """`cert_private_key_env` mistakes were previously reported as
        `token_env` — a field the SharePoint wizard doesn't even show."""
        c = seeded_app["client"]
        resp = c.post(
            BASE,
            json={
                "name": "sp-field-name",
                "source_type": "sharepoint",
                "config": {
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "client_id": "app-client-id",
                    "cert_private_key_env": "example-sharepoint-connector-cert",
                },
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "cert_private_key_env" in resp.text
        assert "token_env 'example-sharepoint-connector-cert'" not in resp.text

    def test_pem_content_pasted_into_the_name_field_is_called_out(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            BASE,
            json={
                "name": "sp-pem-paste",
                "source_type": "sharepoint",
                "config": {
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "client_id": "app-client-id",
                    "cert_private_key_env": "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----",
                },
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        # Names the mistake (content vs name) without echoing the material back.
        assert "PEM content" in resp.text
        assert "MIIB" not in resp.text

    def test_plain_disallowed_env_name_keeps_the_generic_message(self, seeded_app):
        """An UPPER_CASE name that simply isn't allowlisted keeps the original
        remedies (allowlist env vars or the vault PUT)."""
        c = seeded_app["client"]
        resp = c.post(
            BASE,
            json={
                "name": "kb-generic-msg",
                "source_type": "keboola",
                "token_env": "SOME_RANDOM_NAME",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "AGNES_CONFIG_SECRET_ENVS" in resp.text
        assert "token_env" in resp.text


class TestClientSecretConnections:
    """`config.auth_method = "client_secret"` connections store an opaque
    secret in the same vault slot — the PEM validation must not fire on
    them, and certificate material pasted into one is a named mistake."""

    @pytest.fixture()
    def secret_conn(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": f"sp-clientsecret-{id(monkeypatch)}",
                "source_type": "sharepoint",
                "config": {
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "client_id": "app-client-id",
                    "auth_method": "client_secret",
                },
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        yield c, token, resp.json()["id"]
        _reset_ephemeral_key_for_tests()

    def test_opaque_secret_is_accepted(self, secret_conn):
        c, token, conn_id = secret_conn
        resp = c.put(f"{BASE}/{conn_id}/secret", json={"value": "plain-app-secret"}, headers=_auth(token))
        assert resp.status_code == 204, resp.text

    def test_pem_material_in_a_client_secret_connection_is_named(self, secret_conn):
        c, token, conn_id = secret_conn
        resp = c.put(f"{BASE}/{conn_id}/secret", json={"value": COMBINED_PEM}, headers=_auth(token))
        assert resp.status_code == 400, resp.text
        assert "client_secret" in resp.text

    def test_unknown_auth_method_is_rejected_at_create(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            BASE,
            json={
                "name": "sp-bad-auth-method",
                "source_type": "sharepoint",
                "config": {
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "client_id": "app-client-id",
                    "auth_method": "managed_identity",
                },
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "auth_method" in resp.text

    def test_client_secret_env_name_is_allowlist_gated(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            BASE,
            json={
                "name": "sp-secret-env-evil",
                "source_type": "sharepoint",
                "config": {
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "client_id": "app-client-id",
                    "auth_method": "client_secret",
                    "client_secret_env": "JWT_SECRET_KEY",
                },
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "client_secret_env" in resp.text
