"""``connectors.sharepoint.graph_client`` — the connect wizard's live
folder-tree browser (spec 2026-08-27 §13.2).

No live network: the ``_http_client()`` seam is monkeypatched to an
``httpx.AsyncClient`` wired to ``httpx.MockTransport``, same idiom as
``tests/test_teams_sigverify.py``. A throwaway self-signed certificate +
RSA key (generated in-process via ``cryptography``) stands in for a real
Entra app-registration certificate — enough to exercise the JWT-assertion
parsing/signing path without any real credential.
"""

from __future__ import annotations

import asyncio
import datetime

import httpx
import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from connectors.sharepoint import graph_client as gc


def _self_signed_pem() -> str:
    """A throwaway self-signed certificate + its private key, concatenated —
    exactly the combined-PEM shape ``SharePointSettings.private_key`` is
    documented to hold."""
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


PEM = _self_signed_pem()


class TestBuildClientAssertion:
    def test_signs_a_jwt_with_x5t_header(self):
        token = gc.build_client_assertion("tenant-1", "client-1", PEM)
        header = pyjwt.get_unverified_header(token)
        assert "x5t" in header and header["x5t"]
        claims = pyjwt.decode(token, options={"verify_signature": False})
        assert claims["iss"] == "client-1"
        assert claims["sub"] == "client-1"
        assert claims["aud"] == "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"

    def test_missing_certificate_block_raises_typed_error(self):
        key_only = "-----BEGIN PRIVATE KEY-----\nc2VjcmV0\n-----END PRIVATE KEY-----"
        with pytest.raises(gc.SharePointGraphError, match="CERTIFICATE"):
            gc.build_client_assertion("t", "c", key_only)

    def test_missing_private_key_block_raises_typed_error(self):
        cert_only = PEM.split("-----BEGIN PRIVATE KEY-----")[0]
        with pytest.raises(gc.SharePointGraphError, match="PRIVATE KEY"):
            gc.build_client_assertion("t", "c", cert_only)

    def test_unparseable_pem_raises_typed_error_not_a_bare_exception(self):
        garbage = "-----BEGIN CERTIFICATE-----\nnot-real\n-----END CERTIFICATE-----\n" + PEM.split(
            "-----BEGIN PRIVATE KEY-----"
        )[1].join(["-----BEGIN PRIVATE KEY-----", ""])
        with pytest.raises(gc.SharePointGraphError):
            gc.build_client_assertion("t", "c", garbage)


def _install_transport(monkeypatch, handler):
    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)

    monkeypatch.setattr(gc, "_http_client", _client)


class TestGetAppToken:
    def test_returns_access_token_on_200(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/oauth2/v2.0/token")
            return httpx.Response(200, json={"access_token": "graph-token-abc", "expires_in": 3600})

        _install_transport(monkeypatch, handler)
        token = asyncio.run(gc.get_app_token("tenant-1", "client-1", PEM))
        assert token == "graph-token-abc"

    def test_non_200_raises_typed_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_client"})

        _install_transport(monkeypatch, handler)
        with pytest.raises(gc.SharePointGraphError, match="401"):
            asyncio.run(gc.get_app_token("tenant-1", "client-1", PEM))

    def test_missing_access_token_in_body_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token_type": "Bearer"})

        _install_transport(monkeypatch, handler)
        with pytest.raises(gc.SharePointGraphError, match="access_token"):
            asyncio.run(gc.get_app_token("tenant-1", "client-1", PEM))


class TestBrowseLevels:
    def test_list_sites(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/sites"
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(
                200,
                json={"value": [{"id": "site1", "displayName": "Corp Site", "webUrl": "https://x/site1"}]},
            )

        _install_transport(monkeypatch, handler)
        sites = asyncio.run(gc.list_sites("tok"))
        assert sites == [{"id": "site1", "name": "Corp Site", "web_url": "https://x/site1"}]

    def test_list_drives(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/sites/site1/drives"
            return httpx.Response(
                200, json={"value": [{"id": "drv1", "name": "Documents", "driveType": "documentLibrary"}]}
            )

        _install_transport(monkeypatch, handler)
        drives = asyncio.run(gc.list_drives("tok", "site1"))
        assert drives == [{"id": "drv1", "name": "Documents", "drive_type": "documentLibrary"}]

    def test_list_root_children(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/drives/drv1/root/children"
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "f1", "name": "Contracts", "folder": {"childCount": 5}},
                        {"id": "f2", "name": "notes.txt", "file": {}},
                    ]
                },
            )

        _install_transport(monkeypatch, handler)
        items = asyncio.run(gc.list_root_children("tok", "drv1"))
        assert items == [
            {"id": "f1", "name": "Contracts", "is_folder": True, "child_count": 5},
            {"id": "f2", "name": "notes.txt", "is_folder": False, "child_count": None},
        ]

    def test_non_200_raises_typed_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "Forbidden"}})

        _install_transport(monkeypatch, handler)
        with pytest.raises(gc.SharePointGraphError, match="403"):
            asyncio.run(gc.list_sites("tok"))
