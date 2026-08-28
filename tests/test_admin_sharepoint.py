"""SharePoint connect wizard admin API (spec 2026-08-27 §13.2).

Covers: admin gating on every route, the typed "certificate unresolved"
error (surface absence rather than fail, per spec) vs. a real Graph browse
with the Graph transport mocked, scope->collection creation idempotency
(re-confirming a scope reuses the same collection), the no-group ("indexed
but invisible") warning, and the producer-handoff corpus-map endpoint.
"""

from __future__ import annotations

import datetime

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

BASE = "/api/admin/sharepoint/connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _self_signed_pem() -> str:
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


def _create_connection(client, token, *, name="corp-sharepoint", tenant_id="tenant-1", client_id="client-1"):
    resp = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {"tenant_id": tenant_id, "client_id": client_id},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestAuthGating:
    def test_tree_requires_auth(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/nope/tree")
        assert r.status_code == 401

    def test_tree_requires_admin(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/nope/tree", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_scopes_get_requires_admin(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/nope/scopes", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_scopes_post_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(
            f"{BASE}/nope/scopes",
            json={"source_scope_id": "x", "display_path": "x"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403

    def test_corpus_map_requires_admin(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/nope/corpus-map", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403


class TestConnectionNotFound:
    def test_tree_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/does-not-exist/tree", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404
        assert r.json()["detail"] == "connection_not_found"

    def test_tree_404_for_non_sharepoint_connection(self, seeded_app, monkeypatch):
        c = seeded_app["client"]
        created = c.post(
            "/api/admin/source-connections",
            json={"name": "not-sp", "source_type": "bigquery", "config": {"project": "p"}},
            headers=_auth(seeded_app["admin_token"]),
        )
        conn_id = created.json()["id"]
        r = c.get(f"{BASE}/{conn_id}/tree", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404


class TestTreeCertResolution:
    def test_missing_certificate_is_a_typed_409_not_a_500(self, seeded_app, monkeypatch):
        """Surface absence rather than fail the crawl (spec §13.2)."""
        monkeypatch.delenv("SHAREPOINT_CERT_PRIVATE_KEY", raising=False)
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="no-cert-conn")
        r = c.get(f"{BASE}/{conn_id}/tree", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "sharepoint_cert_unresolved"

    def test_browses_sites_with_a_resolved_certificate(self, seeded_app, monkeypatch):
        from connectors.sharepoint import graph_client as gc

        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok-abc"})
            assert request.url.path == "/v1.0/sites"
            return httpx.Response(200, json={"value": [{"id": "s1", "displayName": "Corp", "webUrl": "https://x/s1"}]})

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        )

        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="cert-conn")
        r = c.get(f"{BASE}/{conn_id}/tree", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["level"] == "sites"
        assert body["items"] == [{"id": "s1", "name": "Corp", "web_url": "https://x/s1"}]

    def test_drives_level_when_site_id_given(self, seeded_app, monkeypatch):
        from connectors.sharepoint import graph_client as gc

        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok-abc"})
            assert request.url.path == "/v1.0/sites/s1/drives"
            return httpx.Response(
                200, json={"value": [{"id": "d1", "name": "Documents", "driveType": "documentLibrary"}]}
            )

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        )

        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="cert-conn-drives")
        r = c.get(f"{BASE}/{conn_id}/tree", params={"site_id": "s1"}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["level"] == "drives"

    def test_graph_upstream_failure_is_a_typed_502(self, seeded_app, monkeypatch):
        from connectors.sharepoint import graph_client as gc

        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_client"})

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        )

        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="cert-conn-fail")
        r = c.get(f"{BASE}/{conn_id}/tree", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error"] == "sharepoint_graph_error"


class TestScopeConfirmationIdempotency:
    def test_confirming_same_scope_twice_reuses_the_collection(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="idem-conn")

        first = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:abc123", "display_path": "Corp Site / Documents", "anonymize": False},
            headers=_auth(token),
        )
        assert first.status_code == 201, first.text
        collection_id_1 = first.json()["collection_id"]
        assert collection_id_1

        second = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:abc123",
                "display_path": "Corp Site / Documents (renamed)",
                "anonymize": True,
            },
            headers=_auth(token),
        )
        assert second.status_code == 201, second.text
        assert second.json()["collection_id"] == collection_id_1
        assert second.json()["display_path"] == "Corp Site / Documents (renamed)"
        assert second.json()["anonymize"] is True

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token))
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 1  # no second row, no second collection

    def test_two_different_scopes_get_two_collections(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="two-scopes-conn")

        r1 = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:one", "display_path": "Site A / Lib"},
            headers=_auth(token),
        )
        r2 = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:two", "display_path": "Site B / Lib"},
            headers=_auth(token),
        )
        assert r1.json()["collection_id"] != r2.json()["collection_id"]

    def test_slug_collision_across_scopes_still_creates_distinct_collections(self, seeded_app):
        """Two different source folders whose display paths normalize to the
        same slug (e.g. two "Contracts" folders under different sites) must
        not collide — each scope gets its own collection."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="collision-conn")

        r1 = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:site-a-contracts", "display_path": "Contracts"},
            headers=_auth(token),
        )
        r2 = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:site-b-contracts", "display_path": "Contracts"},
            headers=_auth(token),
        )
        assert r1.status_code == 201, r1.text
        assert r2.status_code == 201, r2.text
        assert r1.json()["collection_id"] != r2.json()["collection_id"]


class TestScopeRemoval:
    def test_removing_a_scope_drops_the_row_not_the_collection(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="remove-conn")

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:gone", "display_path": "To be excluded"},
            headers=_auth(token),
        )
        collection_id = confirmed.json()["collection_id"]

        deleted = c.delete(f"{BASE}/{conn_id}/scopes", params={"source_scope_id": "drive:gone"}, headers=_auth(token))
        assert deleted.status_code == 204

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token))
        assert listed.json()["items"] == []

        # The collection itself is untouched by unselecting the scope.
        coll = c.get(f"/api/collections/{collection_id}", headers=_auth(token))
        assert coll.status_code == 200

    def test_removing_unknown_scope_is_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="remove-404-conn")
        r = c.delete(f"{BASE}/{conn_id}/scopes", params={"source_scope_id": "nope"}, headers=_auth(token))
        assert r.status_code == 404


class TestNoGroupWarning:
    def test_unit_no_group_warning(self):
        from app.api.admin_sharepoint import no_group_warning

        assert no_group_warning([]) is True
        assert no_group_warning(["g1"]) is False

    def test_scope_with_no_group_grant_warns(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="warn-conn")
        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:ungranted", "display_path": "Ungranted"},
            headers=_auth(token),
        )
        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token))
        row = listed.json()["items"][0]
        assert row["no_group_warning"] is True
        assert row["group_ids"] == []

    def test_scope_with_a_group_grant_does_not_warn(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="granted-conn")

        group_resp = c.post(
            "/api/admin/groups",
            json={"name": "sp-warn-group"},
            headers=_auth(token),
        )
        assert group_resp.status_code in (200, 201), group_resp.text
        group_id = group_resp.json()["id"]

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:granted", "display_path": "Granted", "group_ids": [group_id]},
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text
        assert confirmed.json()["no_group_warning"] is False
        assert confirmed.json()["group_ids"] == [group_id]

    def test_unknown_group_id_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bad-group-conn")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:x", "display_path": "X", "group_ids": ["does-not-exist"]},
            headers=_auth(token),
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_group_id"


class TestCorpusMap:
    def test_corpus_map_is_the_flat_scope_to_collection_mapping(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="map-conn")

        r1 = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:a", "display_path": "A"},
            headers=_auth(token),
        )
        r2 = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:b", "display_path": "B"},
            headers=_auth(token),
        )

        mapping = c.get(f"{BASE}/{conn_id}/corpus-map", headers=_auth(token))
        assert mapping.status_code == 200
        assert mapping.json() == {
            "drive:a": r1.json()["collection_id"],
            "drive:b": r2.json()["collection_id"],
        }

    def test_corpus_map_empty_for_connection_with_no_scopes(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="empty-map-conn")
        mapping = c.get(f"{BASE}/{conn_id}/corpus-map", headers=_auth(token))
        assert mapping.status_code == 200
        assert mapping.json() == {}
