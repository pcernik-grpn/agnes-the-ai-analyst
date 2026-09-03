"""SharePoint connect wizard admin API (spec 2026-08-27 §13.2).

Covers: admin gating on every route, the typed "certificate unresolved"
error (surface absence rather than fail, per spec) vs. a real Graph browse
with the Graph transport mocked, scope->collection creation idempotency
(re-confirming a scope reuses the same collection), the no-group ("indexed
but invisible") warning.

2026-09-01: the whole router is gated by the single ``sharepoint`` switch
(``app/api/admin_sharepoint.py``'s module-level ``_require_sharepoint_enabled``
dependency, 409 ``feature_disabled`` when off) — see ``TestSharePointGate``
below for that gate itself. The module-level ``_sharepoint_enabled_by_default``
fixture turns the switch ON for every OTHER test in this file via
``AGNES_SHAREPOINT_ENABLED`` so the rest of the suite exercises the routes'
own behavior, not the gate; a class that needs to control the switch itself
(``TestAclSyncTrigger``, ``TestSubtreeSweepTrigger``, ``TestExtractionTrigger``,
``TestExtractionRunDue``) clears the env var in its own autouse fixture and
drives the flag through the mocked ``get_value`` config instead.
"""

from __future__ import annotations

import datetime
import json
import re
import sys

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

BASE = "/api/admin/sharepoint/connections"


@pytest.fixture(autouse=True)
def _sharepoint_enabled_by_default(monkeypatch):
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _audit_params(row: dict) -> dict:
    """``audit_repo().query()`` returns ``params`` as the raw stored JSON
    string, not a parsed dict — decode it here so tests can assert on the
    structured fields (same helper as ``tests/test_agent_memory_write_api.py``)."""

    v = row.get("params")
    return json.loads(v) if isinstance(v, str) else (v or {})


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

    def test_changes_requires_auth(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/nope/changes")
        assert r.status_code == 401

    def test_changes_requires_admin(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/nope/changes", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403


class TestConnectionNotFound:
    def test_tree_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/does-not-exist/tree", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_changes_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/does-not-exist/changes", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404
        assert r.json()["detail"] == "connection_not_found"
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


class TestSiteByUrl:
    """``?site_url=`` — the `Sites.Selected` escape hatch: that permission
    forbids ALL site discovery (Graph 403s ``/sites?search=*`` by design), so
    the wizard must be able to reach a granted site addressed directly by the
    URL an admin pastes, and the discovery 403 itself must say so instead of
    reading as an outage."""

    def _mock_graph(self, monkeypatch, handler):
        from connectors.sharepoint import graph_client as gc

        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)

        def full_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok-abc"})
            return handler(request)

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(full_handler), timeout=10)
        )

    def test_resolves_a_granted_site_directly_by_url(self, seeded_app, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/sites/contoso.sharepoint.com:/sites/ProjectHub"
            return httpx.Response(
                200, json={"id": "s-by-url", "displayName": "Project Hub", "webUrl": "https://contoso/x"}
            )

        self._mock_graph(monkeypatch, handler)
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="by-url-conn")
        r = c.get(
            f"{BASE}/{conn_id}/tree",
            params={"site_url": "https://contoso.sharepoint.com/sites/ProjectHub"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["level"] == "sites"
        assert body["items"] == [{"id": "s-by-url", "name": "Project Hub", "web_url": "https://contoso/x"}]

    def test_a_deep_page_url_is_trimmed_to_its_site(self, seeded_app, monkeypatch):
        """Admins paste whatever their browser shows — a document-library page
        deep inside the site must still resolve the SITE, not 404 on the page
        path."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/sites/contoso.sharepoint.com:/sites/ProjectHub"
            return httpx.Response(200, json={"id": "s-by-url", "displayName": "Project Hub", "webUrl": "https://c/x"})

        self._mock_graph(monkeypatch, handler)
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="by-url-deep")
        r = c.get(
            f"{BASE}/{conn_id}/tree",
            params={
                "site_url": "https://contoso.sharepoint.com/sites/ProjectHub/Shared%20Documents/Forms/AllItems.aspx"
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["items"][0]["id"] == "s-by-url"

    def test_site_url_is_exclusive_with_tree_coordinates(self, seeded_app, monkeypatch):
        self._mock_graph(monkeypatch, lambda request: httpx.Response(500))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="by-url-excl")
        r = c.get(
            f"{BASE}/{conn_id}/tree",
            params={"site_url": "https://contoso.sharepoint.com/sites/X", "site_id": "s1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "site_url_exclusive"

    @pytest.mark.parametrize(
        "bad",
        [
            "http://contoso.sharepoint.com/sites/X",  # non-https scheme
            "https://user@contoso.sharepoint.com/sites/X",  # userinfo smuggling
            "https://contoso.sharepoint.com:8443/sites/X",  # explicit port
            "https://contoso.sharepoint.com/sites/%2e%2e/other",  # dot-dot segment
            "https://bad host/sites/X",  # malformed hostname
            "https:///sites/X",  # no hostname at all
            "https://contoso.sharepoint.com/sites",  # managed path with no site name
        ],
    )
    def test_malformed_site_url_is_a_typed_422(self, seeded_app, monkeypatch, bad):
        self._mock_graph(monkeypatch, lambda request: httpx.Response(500))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="by-url-bad")
        r = c.get(f"{BASE}/{conn_id}/tree", params={"site_url": bad}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "invalid_site_url"

    def test_discovery_403_names_sites_selected_and_the_url_fallback(self, seeded_app, monkeypatch):
        """Found live on a Sites.Selected tenant (2026-08-31): the listing
        call 403s BY DESIGN, and the old generic wrap ("SharePoint did not
        answer") read as an outage while the certificate was fine."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "accessDenied"}})

        self._mock_graph(monkeypatch, handler)
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="disc-403")
        r = c.get(f"{BASE}/{conn_id}/tree", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 502, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "sharepoint_discovery_forbidden"
        assert "Sites.Selected" in detail["message"]
        assert "URL" in detail["message"]

    def test_by_url_403_is_a_typed_not_granted_error(self, seeded_app, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "accessDenied"}})

        self._mock_graph(monkeypatch, handler)
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="by-url-403")
        r = c.get(
            f"{BASE}/{conn_id}/tree",
            params={"site_url": "https://contoso.sharepoint.com/sites/NotGranted"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error"] == "sharepoint_site_not_granted"

    def test_drive_level_403_keeps_the_generic_graph_error(self, seeded_app, monkeypatch):
        """The Sites.Selected classification applies only where it is TRUE —
        a 403 while browsing inside a known site is not a discovery refusal
        and must not be dressed up as one."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "accessDenied"}})

        self._mock_graph(monkeypatch, handler)
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="drives-403")
        r = c.get(f"{BASE}/{conn_id}/tree", params={"site_id": "s1"}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error"] == "sharepoint_graph_error"


class TestManualSites:
    """Persistence for a site added by URL (2026-09-01 bug report): the
    ``Sites.Selected`` escape hatch (``?site_url=`` on ``.../tree``) only
    ever RESOLVED a site — the result lived in the wizard's own in-memory
    ``spManualSites`` and vanished the moment the wizard was reopened,
    forcing the admin to re-paste the same URL every time. These two routes
    store the resolved site on the connection's own ``config.manual_sites``
    so it survives a reopen, same idempotent-on-id contract as a confirmed
    scope's collection."""

    def _mock_graph(self, monkeypatch, handler):
        from connectors.sharepoint import graph_client as gc

        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)

        def full_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok-abc"})
            return handler(request)

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(full_handler), timeout=10)
        )

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(
            f"{BASE}/nope/manual-sites",
            json={"site_url": "https://contoso.sharepoint.com/sites/X"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403

    def test_delete_requires_admin(self, seeded_app):
        r = seeded_app["client"].delete(
            f"{BASE}/nope/manual-sites", params={"site_id": "x"}, headers=_auth(seeded_app["analyst_token"])
        )
        assert r.status_code == 403

    def test_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].post(
            f"{BASE}/does-not-exist/manual-sites",
            json={"site_url": "https://contoso.sharepoint.com/sites/X"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 404

    def test_adding_a_site_by_url_persists_it_on_the_connection(self, seeded_app, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/sites/contoso.sharepoint.com:/sites/ProjectHub"
            return httpx.Response(
                200, json={"id": "s-by-url", "displayName": "Project Hub", "webUrl": "https://contoso/x"}
            )

        self._mock_graph(monkeypatch, handler)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="manual-site-persist")

        r = c.post(
            f"{BASE}/{conn_id}/manual-sites",
            json={"site_url": "https://contoso.sharepoint.com/sites/ProjectHub"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json() == {"id": "s-by-url", "name": "Project Hub", "web_url": "https://contoso/x"}

        # Persisted on the connection's own config, readable via the generic
        # listing the wizard already fetches on every page load.
        listed = c.get("/api/admin/source-connections", headers=_auth(token))
        row = next(row for row in listed.json() if row["id"] == conn_id)
        assert row["config"]["manual_sites"] == [
            {"id": "s-by-url", "name": "Project Hub", "web_url": "https://contoso/x"}
        ]

    def test_adding_the_same_site_twice_is_idempotent(self, seeded_app, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "s-dup", "displayName": "Dup Site", "webUrl": "https://contoso/dup"})

        self._mock_graph(monkeypatch, handler)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="manual-site-idempotent")

        for _ in range(2):
            r = c.post(
                f"{BASE}/{conn_id}/manual-sites",
                json={"site_url": "https://contoso.sharepoint.com/sites/Dup"},
                headers=_auth(token),
            )
            assert r.status_code == 201, r.text

        listed = c.get("/api/admin/source-connections", headers=_auth(token))
        row = next(row for row in listed.json() if row["id"] == conn_id)
        assert row["config"]["manual_sites"] == [{"id": "s-dup", "name": "Dup Site", "web_url": "https://contoso/dup"}]

    def test_malformed_site_url_is_a_typed_422(self, seeded_app, monkeypatch):
        self._mock_graph(monkeypatch, lambda request: httpx.Response(500))
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="manual-site-bad-url")
        r = c.post(
            f"{BASE}/{conn_id}/manual-sites",
            json={"site_url": "http://contoso.sharepoint.com/sites/X"},
            headers=_auth(token),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "invalid_site_url"

    def test_not_granted_site_is_a_typed_error(self, seeded_app, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "accessDenied"}})

        self._mock_graph(monkeypatch, handler)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="manual-site-not-granted")
        r = c.post(
            f"{BASE}/{conn_id}/manual-sites",
            json={"site_url": "https://contoso.sharepoint.com/sites/NotGranted"},
            headers=_auth(token),
        )
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error"] == "sharepoint_site_not_granted"

    def test_removing_a_manual_site(self, seeded_app, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"id": "s-remove", "displayName": "Remove Me", "webUrl": "https://contoso/rm"}
            )

        self._mock_graph(monkeypatch, handler)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="manual-site-remove")
        c.post(
            f"{BASE}/{conn_id}/manual-sites",
            json={"site_url": "https://contoso.sharepoint.com/sites/Remove"},
            headers=_auth(token),
        )

        r = c.delete(f"{BASE}/{conn_id}/manual-sites", params={"site_id": "s-remove"}, headers=_auth(token))
        assert r.status_code == 204, r.text

        listed = c.get("/api/admin/source-connections", headers=_auth(token))
        row = next(row for row in listed.json() if row["id"] == conn_id)
        assert row["config"]["manual_sites"] == []

    def test_removing_unknown_manual_site_is_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="manual-site-remove-404")
        r = c.delete(f"{BASE}/{conn_id}/manual-sites", params={"site_id": "nope"}, headers=_auth(token))
        assert r.status_code == 404

    def test_delete_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].delete(
            f"{BASE}/does-not-exist/manual-sites", params={"site_id": "x"}, headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 404

    def test_add_and_remove_are_audited(self, seeded_app, monkeypatch):
        from src.repositories import audit_repo

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"id": "s-audit", "displayName": "Audit Site", "webUrl": "https://contoso/audit"}
            )

        self._mock_graph(monkeypatch, handler)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="manual-site-audit")

        c.post(
            f"{BASE}/{conn_id}/manual-sites",
            json={"site_url": "https://contoso.sharepoint.com/sites/Audit"},
            headers=_auth(token),
        )
        c.delete(f"{BASE}/{conn_id}/manual-sites", params={"site_id": "s-audit"}, headers=_auth(token))

        rows, _ = audit_repo().query(action="sharepoint_connection.manual_site_add", limit=10)
        assert any(conn_id in (row.get("resource") or "") for row in rows)
        rows, _ = audit_repo().query(action="sharepoint_connection.manual_site_remove", limit=10)
        assert any(conn_id in (row.get("resource") or "") for row in rows)


class TestSubfolderBrowsing:
    """TCRD-240: `?item_id=` lets the wizard browse below the drive root at
    any depth — the pre-existing contract stopped at "sites -> drives ->
    root children"."""

    def _connect(self, seeded_app, monkeypatch, name="subfolder-conn"):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        return c, _create_connection(c, seeded_app["admin_token"], name=name)

    def test_item_id_browses_that_folders_children(self, seeded_app, monkeypatch):
        from connectors.sharepoint import graph_client as gc

        c, conn_id = self._connect(seeded_app, monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok-abc"})
            assert request.url.path == "/v1.0/drives/d1/items/f1/children"
            return httpx.Response(
                200, json={"value": [{"id": "f1x", "name": "Subfolder", "folder": {"childCount": 0}}]}
            )

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        )
        r = c.get(
            f"{BASE}/{conn_id}/tree",
            params={"drive_id": "d1", "item_id": "f1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["level"] == "items"
        assert body["item_id"] == "f1"
        assert body["items"] == [{"id": "f1x", "name": "Subfolder", "is_folder": True, "child_count": 0}]

    def test_item_id_without_drive_id_is_422(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        r = c.get(f"{BASE}/{conn_id}/tree", params={"item_id": "f1"}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "item_id_requires_drive_id"

    def test_malformed_item_id_is_422_not_a_500(self, seeded_app, monkeypatch):
        """Structural validation before it ever reaches a Graph URL path
        segment — a `/` in item_id must never build a different request."""
        c, conn_id = self._connect(seeded_app, monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree",
            params={"drive_id": "d1", "item_id": "../root"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "invalid_item_id"


class TestUniquePermissionsAdvisory:
    """`GET .../tree?with_permissions=1` — ADVISORY-ONLY unique-permissions
    signal (spec §13.1/§13.2, Decision #2: Agnes never derives or enforces
    anything from SharePoint ACLs). Off by default, batched, never blocks or
    fails ordinary browsing even when the probe itself fails."""

    def _connect(self, seeded_app, monkeypatch, name="perm-conn"):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        return c, _create_connection(c, seeded_app["admin_token"], name=name)

    def _install(self, monkeypatch, *, batch_handler=None):
        from connectors.sharepoint import graph_client as gc

        calls = {"batch": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok-abc"})
            if path == "/v1.0/drives/d1/items/f1/children":
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {"id": "sub1", "name": "Unique folder", "folder": {"childCount": 0}},
                            {"id": "sub2", "name": "Ordinary folder", "folder": {"childCount": 0}},
                            {"id": "doc1", "name": "report.pdf", "file": {}},
                        ]
                    },
                )
            if path == "/v1.0/$batch":
                calls["batch"] += 1
                if batch_handler:
                    return batch_handler(request)
                payload = request.content
                import json as _json

                requests = _json.loads(payload)["requests"]
                responses = []
                for req in requests:
                    item_id = req["url"].split("/items/")[1].split("?")[0]
                    flag = {"sub1": True, "sub2": False}.get(item_id)
                    responses.append(
                        {"id": req["id"], "status": 200, "body": {"listItem": {"hasUniqueRoleAssignments": flag}}}
                    )
                return httpx.Response(200, json={"responses": responses})
            raise AssertionError(f"unexpected path: {path}")

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        )
        return calls

    def test_off_by_default_never_calls_batch(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        calls = self._install(monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree",
            params={"drive_id": "d1", "item_id": "f1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert calls["batch"] == 0
        for item in r.json()["items"]:
            assert "unique_permissions" not in item

    def test_with_permissions_flags_folders_true_false(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        calls = self._install(monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree",
            params={"drive_id": "d1", "item_id": "f1", "with_permissions": "1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert calls["batch"] == 1
        by_id = {item["id"]: item for item in r.json()["items"]}
        assert by_id["sub1"]["unique_permissions"] is True
        assert by_id["sub2"]["unique_permissions"] is False

    def test_files_are_never_probed_or_flagged_true(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        self._install(monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree",
            params={"drive_id": "d1", "item_id": "f1", "with_permissions": "1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        by_id = {item["id"]: item for item in r.json()["items"]}
        # Files are never scoped by the wizard, so they are never probed and
        # never carry the key at all — not even as `null`.
        assert "unique_permissions" not in by_id["doc1"]

    def test_probe_failure_degrades_to_unknown_never_fails_the_browse(self, seeded_app, monkeypatch):
        def failing_batch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream unavailable")

        c, conn_id = self._connect(seeded_app, monkeypatch)
        calls = self._install(monkeypatch, batch_handler=failing_batch)
        r = c.get(
            f"{BASE}/{conn_id}/tree",
            params={"drive_id": "d1", "item_id": "f1", "with_permissions": "1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert calls["batch"] == 1
        by_id = {item["id"]: item for item in r.json()["items"]}
        assert by_id["sub1"]["unique_permissions"] is None
        assert by_id["sub2"]["unique_permissions"] is None

    def test_sites_level_ignores_with_permissions_no_batch_call(self, seeded_app, monkeypatch):
        from connectors.sharepoint import graph_client as gc

        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok-abc"})
            if request.url.path == "/v1.0/$batch":
                raise AssertionError("sites level has no folder items to probe")
            assert request.url.path == "/v1.0/sites"
            return httpx.Response(200, json={"value": [{"id": "s1", "displayName": "Corp", "webUrl": "https://x/s1"}]})

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        )
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="perm-sites-conn")
        r = c.get(f"{BASE}/{conn_id}/tree", params={"with_permissions": "1"}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text


class TestTreeSearch:
    """TCRD-240: `GET .../tree/search` — bounded BFS folder search, never
    Graph's own `/search` (module docstring in `graph_client`)."""

    def _connect(self, seeded_app, monkeypatch, name="search-conn"):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        return c, _create_connection(c, seeded_app["admin_token"], name=name)

    def _install_tree(self, monkeypatch):
        from connectors.sharepoint import graph_client as gc

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok-abc"})
            if path == "/v1.0/drives/d1/root/children":
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {"id": "c1", "name": "Contracts", "folder": {"childCount": 1}},
                            {"id": "i1", "name": "Invoices", "folder": {"childCount": 0}},
                        ]
                    },
                )
            if path == "/v1.0/drives/d1/items/c1/children":
                return httpx.Response(
                    200, json={"value": [{"id": "c1x", "name": "Contracts 2026", "folder": {"childCount": 0}}]}
                )
            # Leaf folders (Invoices, and Contracts 2026 itself — a folder
            # with childCount 0 is still walked one level to confirm it is
            # empty) — every folder the BFS reaches needs a route, even a
            # childless one.
            if path in ("/v1.0/drives/d1/items/i1/children", "/v1.0/drives/d1/items/c1x/children"):
                return httpx.Response(200, json={"value": []})
            raise AssertionError(f"unexpected path: {path}")

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        )

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].get(
            f"{BASE}/nope/tree/search", params={"q": "ab"}, headers=_auth(seeded_app["analyst_token"])
        )
        assert r.status_code == 403

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/nope/tree/search", params={"q": "ab"})
        assert r.status_code == 401

    def test_unknown_connection_is_404(self, seeded_app):
        r = seeded_app["client"].get(
            f"{BASE}/does-not-exist/tree/search", params={"q": "ab"}, headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 404

    def test_query_shorter_than_two_chars_is_422(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        r = c.get(f"{BASE}/{conn_id}/tree/search", params={"q": "a"}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 422

    def test_finds_matches_within_the_given_drive_and_reports_no_truncation(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        self._install_tree(monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "contract", "mode": "contains", "drive_id": "d1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["truncated"] is False
        paths = sorted(m["display_path"] for m in body["matches"])
        assert paths == ["Contracts", "Contracts / Contracts 2026"]
        assert all(m["drive_id"] == "d1" for m in body["matches"])

    def test_default_mode_is_prefix(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        self._install_tree(monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "Con", "drive_id": "d1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        paths = sorted(m["display_path"] for m in r.json()["matches"])
        assert paths == ["Contracts", "Contracts / Contracts 2026"]

    def test_glob_mode(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        self._install_tree(monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "Contracts*", "mode": "glob", "drive_id": "d1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        paths = sorted(m["display_path"] for m in r.json()["matches"])
        assert paths == ["Contracts", "Contracts / Contracts 2026"]

    def test_invalid_mode_is_422(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "ab", "mode": "fuzzy", "drive_id": "d1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422

    def test_malformed_glob_is_422(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "Contracts[2026", "mode": "glob", "drive_id": "d1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "invalid_search_pattern"

    def test_item_id_without_drive_id_is_422(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "ab", "item_id": "c1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "item_id_requires_drive_id"

    def test_malformed_drive_id_is_422_not_a_500(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "ab", "drive_id": "not/a/valid/id"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "invalid_drive_id"

    def test_max_depth_and_max_visited_are_clamped_not_rejected(self, seeded_app, monkeypatch):
        """Asking for more than the server allows still returns a bounded
        200 — never a 422 for an out-of-range cap."""
        c, conn_id = self._connect(seeded_app, monkeypatch)
        self._install_tree(monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "contract", "mode": "contains", "drive_id": "d1", "max_depth": 999, "max_visited": 999999},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text

    def test_out_of_range_caps_clamp_to_the_raised_real_library_scale_caps(self, seeded_app, monkeypatch):
        """443k files / 97,899 folders in one real library (task context) is
        what motivated raising the ceiling from depth 10 / visited 2000 to
        depth 12 / visited 20000 — assert the endpoint actually clamps to
        the NEW numbers, not the old ones."""
        import app.api.admin_sharepoint as admin_sp

        c, conn_id = self._connect(seeded_app, monkeypatch)
        self._install_tree(monkeypatch)
        captured = {}
        real_search_folders = admin_sp.search_folders

        async def spy(*args, **kwargs):
            captured["max_depth"] = kwargs["max_depth"]
            captured["max_visited"] = kwargs["max_visited"]
            return await real_search_folders(*args, **kwargs)

        monkeypatch.setattr(admin_sp, "search_folders", spy)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "contract", "mode": "contains", "drive_id": "d1", "max_depth": 999, "max_visited": 999999},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert captured == {"max_depth": 12, "max_visited": 20000}

    def test_default_max_visited_is_raised_to_2000(self, seeded_app, monkeypatch):
        import app.api.admin_sharepoint as admin_sp

        c, conn_id = self._connect(seeded_app, monkeypatch)
        self._install_tree(monkeypatch)
        captured = {}
        real_search_folders = admin_sp.search_folders

        async def spy(*args, **kwargs):
            captured["max_visited"] = kwargs["max_visited"]
            return await real_search_folders(*args, **kwargs)

        monkeypatch.setattr(admin_sp, "search_folders", spy)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "contract", "mode": "contains", "drive_id": "d1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert captured["max_visited"] == 2000

    def test_truncated_response_carries_visited_count_and_a_hint(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        self._install_tree(monkeypatch)
        # `max_visited=1` clamps to 1 (the floor) — the walk stops after the
        # very first "list children" call, guaranteed to leave the fixture's
        # tree short of fully covered, i.e. `truncated: true`.
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "contract", "mode": "contains", "drive_id": "d1", "max_visited": 1},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["truncated"] is True
        assert isinstance(body["visited"], int)
        assert body["hint"]
        assert "scope" in body["hint"].lower() or "narrow" in body["hint"].lower()

    def test_non_truncated_response_hint_is_null(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        self._install_tree(monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "contract", "mode": "contains", "drive_id": "d1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["truncated"] is False
        assert body["hint"] is None

    def test_missing_certificate_is_a_typed_409(self, seeded_app, monkeypatch):
        monkeypatch.delenv("SHAREPOINT_CERT_PRIVATE_KEY", raising=False)
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="search-no-cert")
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "ab", "drive_id": "d1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "sharepoint_cert_unresolved"

    def _install_tree_with_one_forbidden_site(self, monkeypatch):
        """`GET .../tree/search` with no ``drive_id`` ("search everywhere")
        over two sites — one readable, one 403 on its drive listing."""
        from connectors.sharepoint import graph_client as gc

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok-abc"})
            if path == "/v1.0/sites":
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {"id": "s1", "displayName": "Open Site", "webUrl": "https://x/s1"},
                            {"id": "s2", "displayName": "Blocked Site", "webUrl": "https://x/s2"},
                        ]
                    },
                )
            if path == "/v1.0/sites/s1/drives":
                return httpx.Response(
                    200, json={"value": [{"id": "d1", "name": "Documents", "driveType": "documentLibrary"}]}
                )
            if path == "/v1.0/sites/s2/drives":
                return httpx.Response(403, json={"error": {"code": "accessDenied"}})
            if path == "/v1.0/drives/d1/root/children":
                return httpx.Response(
                    200, json={"value": [{"id": "c1", "name": "Contracts", "folder": {"childCount": 0}}]}
                )
            if path == "/v1.0/drives/d1/items/c1/children":
                return httpx.Response(200, json={"value": []})
            raise AssertionError(f"unexpected path: {path}")

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        )

    def test_a_forbidden_site_is_skipped_and_the_other_sites_matches_still_come_back(self, seeded_app, monkeypatch):
        c, conn_id = self._connect(seeded_app, monkeypatch)
        self._install_tree_with_one_forbidden_site(monkeypatch)
        r = c.get(
            f"{BASE}/{conn_id}/tree/search",
            params={"q": "contract", "mode": "contains"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert [m["display_path"] for m in body["matches"]] == ["Open Site / Documents / Contracts"]
        assert len(body["skipped"]) == 1
        assert body["skipped"][0]["site_id"] == "s2"
        assert body["skipped"][0]["reason"] == "forbidden"
        # A permission gap is not a cap-truncated walk — the two stay distinct.
        assert body["truncated"] is False


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


class TestAnonymizationDeclaredField:
    """The wizard's honest badge state (spec §9.2/§13.2): `anonymize` is the
    admin's checkbox (a wish); `anonymization_declared` is whether the LAST
    persisted ingest run actually declared this collection anonymized. The
    two must never collapse into one boolean — a badge reading "anonymized"
    from the checkbox alone is exactly the bug this field exists to fix."""

    def test_requested_but_not_yet_declared_by_default(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="anon-conn")

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:anon", "display_path": "Contracts", "anonymize": True},
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text
        assert confirmed.json()["anonymize"] is True
        assert confirmed.json()["anonymization_declared"] is False

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token))
        assert listed.json()["items"][0]["anonymization_declared"] is False

    def test_declared_once_the_latest_run_reports_the_collection(self, seeded_app, monkeypatch):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="anon-conn-2")

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:anon2", "display_path": "Contracts", "anonymize": True},
            headers=_auth(token),
        )
        collection_id = confirmed.json()["collection_id"]

        class _FakeRunsRepo:
            def list_recent(self, limit=1):
                return [{"anonymization": {"declared": True, "scopes": {collection_id: {"docs_anonymized": 3}}}}]

        monkeypatch.setattr("src.repositories.facts_ingest_runs_repo", lambda: _FakeRunsRepo())

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token))
        row = listed.json()["items"][0]
        assert row["anonymize"] is True
        assert row["anonymization_declared"] is True

    def test_non_anonymize_scope_never_reads_declared_true(self, seeded_app, monkeypatch):
        """A collection appearing in a run's declared set does not flip
        `anonymization_declared` for a scope that was never marked
        `anonymize` — the field means "requested AND declared", never
        "declared alone"."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="anon-conn-3")

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:plain", "display_path": "Public", "anonymize": False},
            headers=_auth(token),
        )
        collection_id = confirmed.json()["collection_id"]

        class _FakeRunsRepo:
            def list_recent(self, limit=1):
                return [{"anonymization": {"declared": True, "scopes": {collection_id: {"docs_anonymized": 3}}}}]

        monkeypatch.setattr("src.repositories.facts_ingest_runs_repo", lambda: _FakeRunsRepo())

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token))
        row = listed.json()["items"][0]
        assert row["anonymize"] is False
        assert row["anonymization_declared"] is False

    def test_run_report_lookup_failure_degrades_to_not_declared(self, seeded_app, monkeypatch):
        """`facts_ingest_runs_repo()` raising (PG-only repo on a
        DuckDB-backed instance, per the A3 ratchet) must never 500 the
        wizard's scope listing — it degrades to "nothing declared yet"."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="anon-conn-4")

        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:anon4", "display_path": "Contracts", "anonymize": True},
            headers=_auth(token),
        )

        def _boom():
            raise RuntimeError("requires_postgres_backend")

        monkeypatch.setattr("src.repositories.facts_ingest_runs_repo", _boom)

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token))
        assert listed.status_code == 200
        assert listed.json()["items"][0]["anonymization_declared"] is False


def _add_corpus_file(collection_id: str, filename: str = "doc.md") -> str:
    """Plant one ingested file row so a collection reads as non-empty."""
    from src.repositories import corpus_files_repo

    return corpus_files_repo().add(
        corpus_id=collection_id,
        filename=filename,
        sha256="0" * 64,
        file_type="text/markdown",
        size_bytes=1,
        storage_path=None,
    )


class TestScopeRemoval:
    def test_removing_a_scope_keeps_a_collection_that_has_files(self, seeded_app):
        """Deleting a collection that holds indexed data stays a separate,
        deliberate operation — untick only drops the wizard's bookkeeping row
        and TELLS the admin the collection stayed (``collection_kept``)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="remove-conn")

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:gone", "display_path": "To be excluded"},
            headers=_auth(token),
        )
        collection_id = confirmed.json()["collection_id"]
        _add_corpus_file(collection_id)

        deleted = c.delete(f"{BASE}/{conn_id}/scopes", params={"source_scope_id": "drive:gone"}, headers=_auth(token))
        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
        assert body["collection_kept"] is True
        assert body["collection"]["id"] == collection_id
        assert body["collection"]["slug"]

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token))
        assert listed.json()["items"] == []

        # The collection itself is untouched by unselecting the scope.
        coll = c.get(f"/api/collections/{collection_id}", headers=_auth(token))
        assert coll.status_code == 200

    def test_removing_a_scope_with_an_empty_collection_deletes_it(self, seeded_app):
        """An EMPTY scope collection holds no data, so keeping it on untick
        only breeds orphans (observed live 2026-08-31: a 0-file collection
        with no scope pointing at it, next to its re-tick twin)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="remove-empty-conn")

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:empty-gone", "display_path": "Never crawled"},
            headers=_auth(token),
        )
        collection_id = confirmed.json()["collection_id"]

        deleted = c.delete(
            f"{BASE}/{conn_id}/scopes", params={"source_scope_id": "drive:empty-gone"}, headers=_auth(token)
        )
        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
        assert body["collection_kept"] is False
        assert body["collection"] is None

        coll = c.get(f"/api/collections/{collection_id}", headers=_auth(token))
        assert coll.status_code == 404

    def test_removing_unknown_scope_is_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="remove-404-conn")
        r = c.delete(f"{BASE}/{conn_id}/scopes", params={"source_scope_id": "nope"}, headers=_auth(token))
        assert r.status_code == 404

    def test_removing_a_scope_deletes_its_sentinel_owned_grants(self, seeded_app):
        """2026-08-31 plan, Task 8: the sync's own mirrored grants must not
        dangle once the scope row that anchors them is gone — an
        admin-assigned grant on the same collection survives untouched."""
        from app.resource_types import ResourceType
        from connectors.sharepoint.acl_sync import ACL_SYNC_SENTINEL
        from src.repositories import resource_grants_repo, user_groups_repo

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="remove-grant-hygiene")
        admin_group_id = c.post("/api/admin/groups", json={"name": "sp-remove-admin"}, headers=_auth(token)).json()[
            "id"
        ]

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:remove-hygiene",
                "display_path": "Hygiene",
                "group_ids": [admin_group_id],
            },
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text
        collection_id = confirmed.json()["collection_id"]

        sentinel_group = user_groups_repo().ensure(name="entra:remove-hygiene-oid", created_by=ACL_SYNC_SENTINEL)
        resource_grants_repo().ensure_grant(
            sentinel_group["id"], ResourceType.COLLECTION.value, collection_id, assigned_by=ACL_SYNC_SENTINEL
        )

        r = c.delete(
            f"{BASE}/{conn_id}/scopes", params={"source_scope_id": "drive:remove-hygiene"}, headers=_auth(token)
        )
        assert r.status_code == 200

        remaining = [
            g
            for g in resource_grants_repo().list_all(resource_type=ResourceType.COLLECTION.value)
            if g["resource_id"] == collection_id
        ]
        assert len(remaining) == 1
        assert remaining[0]["group_id"] == admin_group_id

    def test_removing_a_scope_that_shares_a_collection_keeps_it_even_when_empty(self, seeded_app, monkeypatch):
        """Two scopes sharing ONE collection (bulk-add's `collection` option)
        — unticking one must not soft-delete (or tombstone as solely-owned)
        the collection while the OTHER scope still routes to it, even
        though the collection holds zero files."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(
            monkeypatch,
            {"Folder A": _folder_item("item-a", "Folder A"), "Folder B": _folder_item("item-b", "Folder B")},
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="remove-shared-conn")

        bulk = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A", "Folder B"], "drive_id": "drv1", "collection": {"name": "Shared Site"}},
            headers=_auth(token),
        )
        assert bulk.status_code == 200, bulk.text
        created = bulk.json()["created"]
        shared_collection_id = created[0]["collection_id"]
        assert created[1]["collection_id"] == shared_collection_id

        r = c.delete(f"{BASE}/{conn_id}/scopes", params={"source_scope_id": "item-a"}, headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["collection_kept"] is True
        assert body["collection"]["id"] == shared_collection_id

        # The collection is still live and the OTHER scope still resolves it.
        coll = c.get(f"/api/collections/{shared_collection_id}", headers=_auth(token))
        assert coll.status_code == 200

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"]
        assert [i["source_scope_id"] for i in listed] == ["item-b"]
        assert listed[0]["collection_id"] == shared_collection_id

    def test_removing_a_scope_that_shares_a_collection_with_another_connection_keeps_it(self, seeded_app, monkeypatch):
        """The shared collection can be referenced from a DIFFERENT
        connection too (a second bulk-add call reusing ``collection_id``, or
        post-consolidation) — untick must scan every SharePoint connection,
        not just this one, and only clean up once the LAST reference is
        gone."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn1 = _create_connection(c, token, name="remove-shared-conn-1")
        conn2 = _create_connection(c, token, name="remove-shared-conn-2")

        _install_item_resolver(monkeypatch, {"Folder A": _folder_item("item-a", "Folder A")})
        bulk1 = c.post(
            f"{BASE}/{conn1}/scopes/bulk",
            json={"paths": ["Folder A"], "drive_id": "drv1", "collection": {"name": "Cross-conn Site"}},
            headers=_auth(token),
        )
        assert bulk1.status_code == 200, bulk1.text
        shared_collection_id = bulk1.json()["created"][0]["collection_id"]

        _install_item_resolver(monkeypatch, {"Folder B": _folder_item("item-b", "Folder B")})
        bulk2 = c.post(
            f"{BASE}/{conn2}/scopes/bulk",
            json={"paths": ["Folder B"], "drive_id": "drv1", "collection_id": shared_collection_id},
            headers=_auth(token),
        )
        assert bulk2.status_code == 200, bulk2.text
        assert bulk2.json()["created"][0]["collection_id"] == shared_collection_id

        r = c.delete(f"{BASE}/{conn1}/scopes", params={"source_scope_id": "item-a"}, headers=_auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["collection_kept"] is True

        coll = c.get(f"/api/collections/{shared_collection_id}", headers=_auth(token))
        assert coll.status_code == 200

        # Now remove the LAST reference (conn2's scope) — nothing shares it
        # any more, so the empty collection is finally cleaned up.
        r2 = c.delete(f"{BASE}/{conn2}/scopes", params={"source_scope_id": "item-b"}, headers=_auth(token))
        assert r2.status_code == 200, r2.text
        assert r2.json()["collection_kept"] is False

        coll2 = c.get(f"/api/collections/{shared_collection_id}", headers=_auth(token))
        assert coll2.status_code == 404

    def test_removing_a_scope_that_shares_a_collection_keeps_the_sentinel_grant(self, seeded_app, monkeypatch):
        """Untick of ONE scope sharing a collection must not purge the
        OTHER, still-live scope's sharepoint-acl-sync sentinel grant — the
        sync will keep reconciling that collection on its own schedule."""
        from app.resource_types import ResourceType
        from connectors.sharepoint.acl_sync import ACL_SYNC_SENTINEL
        from src.repositories import resource_grants_repo, user_groups_repo

        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(
            monkeypatch,
            {"Folder A": _folder_item("item-a", "Folder A"), "Folder B": _folder_item("item-b", "Folder B")},
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="remove-shared-sentinel")

        bulk = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={
                "paths": ["Folder A", "Folder B"],
                "drive_id": "drv1",
                "collection": {"name": "Shared Sentinel Site"},
            },
            headers=_auth(token),
        )
        assert bulk.status_code == 200, bulk.text
        shared_collection_id = bulk.json()["created"][0]["collection_id"]

        sentinel_group = user_groups_repo().ensure(
            name="entra:remove-shared-sentinel-oid", created_by=ACL_SYNC_SENTINEL
        )
        resource_grants_repo().ensure_grant(
            sentinel_group["id"], ResourceType.COLLECTION.value, shared_collection_id, assigned_by=ACL_SYNC_SENTINEL
        )

        r = c.delete(f"{BASE}/{conn_id}/scopes", params={"source_scope_id": "item-a"}, headers=_auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["collection_kept"] is True

        remaining = [
            g
            for g in resource_grants_repo().list_all(resource_type=ResourceType.COLLECTION.value)
            if g["resource_id"] == shared_collection_id
        ]
        assert len(remaining) == 1
        assert remaining[0]["group_id"] == sentinel_group["id"]


class TestUntickRetickLifecycle:
    """Tick → untick → re-tick must never breed a duplicate collection.

    Observed live 2026-08-31: unticking and re-ticking the SAME folder left
    an orphaned 0-file collection next to a live slug-suffixed twin, because
    ``confirm_scope``'s idempotency was keyed on the scope row that
    ``remove_scope`` had just deleted."""

    def _confirm(self, c, token, conn_id, scope_id, path="Site / Folder"):
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": scope_id, "display_path": path},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        return r.json()

    def _untick(self, c, token, conn_id, scope_id):
        r = c.delete(f"{BASE}/{conn_id}/scopes", params={"source_scope_id": scope_id}, headers=_auth(token))
        assert r.status_code == 200, r.text
        return r.json()

    def _live_collections_named(self, name_fragment: str) -> list:
        from src.repositories import file_corpora_repo

        return [r for r in file_corpora_repo().list_all() if name_fragment in (r.get("name") or "")]

    def test_empty_scope_cycle_restores_the_same_collection(self, seeded_app):
        """Untick auto-deletes the empty collection; re-tick brings back the
        SAME one — same id, same slug, exactly one live collection."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="cycle-empty-conn")

        first = self._confirm(c, token, conn_id, "drive:cycle-empty")
        self._untick(c, token, conn_id, "drive:cycle-empty")
        again = self._confirm(c, token, conn_id, "drive:cycle-empty")

        assert again["collection_id"] == first["collection_id"]
        assert again["collection"]["slug"] == first["collection"]["slug"]
        assert len(self._live_collections_named("cycle-empty-conn")) == 1

    def test_empty_scope_cycle_survives_repeated_unticks(self, seeded_app):
        """Three full cycles: the deterministic slug-suffix fallback in
        ``_create_scope_collection`` absorbs only ONE collision, so anything
        short of true re-adoption 500s by the third tick."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="cycle-thrice-conn")

        first = self._confirm(c, token, conn_id, "drive:cycle-thrice")
        for _ in range(3):
            self._untick(c, token, conn_id, "drive:cycle-thrice")
            again = self._confirm(c, token, conn_id, "drive:cycle-thrice")
            assert again["collection_id"] == first["collection_id"]
        assert len(self._live_collections_named("cycle-thrice-conn")) == 1

    def test_kept_collection_is_readopted_on_retick(self, seeded_app):
        """A collection kept on untick (it has files) is re-adopted on
        re-tick of the same folder — files intact, no suffixed twin."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="cycle-kept-conn")

        first = self._confirm(c, token, conn_id, "drive:cycle-kept")
        _add_corpus_file(first["collection_id"])
        unticked = self._untick(c, token, conn_id, "drive:cycle-kept")
        assert unticked["collection_kept"] is True

        again = self._confirm(c, token, conn_id, "drive:cycle-kept")
        assert again["collection_id"] == first["collection_id"]
        assert len(self._live_collections_named("cycle-kept-conn")) == 1

        files = c.get(f"/api/collections/{first['collection_id']}/files", headers=_auth(token))
        assert files.status_code == 200
        assert len(files.json()["files"]) == 1

    def test_library_delete_between_untick_and_retick_is_respected(self, seeded_app):
        """A DELIBERATE Library delete of the kept collection is never
        resurrected by a later re-tick — that mints a fresh collection."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="cycle-libdel-conn")

        first = self._confirm(c, token, conn_id, "drive:cycle-libdel")
        _add_corpus_file(first["collection_id"])
        self._untick(c, token, conn_id, "drive:cycle-libdel")

        deleted = c.delete(f"/api/collections/{first['collection_id']}", headers=_auth(token))
        assert deleted.status_code == 204, deleted.text

        again = self._confirm(c, token, conn_id, "drive:cycle-libdel")
        assert again["collection_id"] != first["collection_id"]
        assert c.get(f"/api/collections/{first['collection_id']}", headers=_auth(token)).status_code == 404
        assert len(self._live_collections_named("cycle-libdel-conn")) == 1


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

    def test_unchecking_a_group_in_the_share_step_revokes_its_access(self, seeded_app):
        """The wizard's step-3 checkboxes are pre-checked from the grants that
        exist and the row re-renders its "indexed but invisible — no group
        yet" warning the moment the last one is unticked, so the UI states an
        outcome. It only ever POSTed the *checked* ids into a handler that only
        ever added, so unticking left the group's access in place while the
        screen said it was gone. ``group_ids`` is a SET for this collection."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="revoke-conn")

        keep = c.post("/api/admin/groups", json={"name": "sp-keep"}, headers=_auth(token)).json()["id"]
        drop = c.post("/api/admin/groups", json={"name": "sp-drop"}, headers=_auth(token)).json()["id"]

        first = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:rev", "display_path": "Rev", "group_ids": [keep, drop]},
            headers=_auth(token),
        )
        assert first.status_code == 201, first.text
        assert sorted(first.json()["group_ids"]) == sorted([keep, drop])

        # Re-confirm with `drop` unticked — the shape the finish button posts.
        again = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:rev", "display_path": "Rev", "group_ids": [keep]},
            headers=_auth(token),
        )
        assert again.status_code == 201, again.text
        assert again.json()["group_ids"] == [keep], "unchecking must revoke, not just stop re-adding"

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"][0]
        assert listed["group_ids"] == [keep]

    def test_unticking_the_last_group_is_honoured_not_ignored(self, seeded_app):
        """An empty list is a real answer here — it is exactly the state the
        row's own ⚠ warning describes — so it must revoke rather than be read
        as "nothing to say about sharing"."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="revoke-all-conn")
        gid = c.post("/api/admin/groups", json={"name": "sp-last"}, headers=_auth(token)).json()["id"]

        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:none", "display_path": "None", "group_ids": [gid]},
            headers=_auth(token),
        )
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:none", "display_path": "None", "group_ids": []},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["group_ids"] == []
        assert r.json()["no_group_warning"] is True

    def test_omitting_group_ids_leaves_existing_grants_alone(self, seeded_app):
        """The other half of the contract, and the reason revoking keys on the
        field being PRESENT rather than on the list being empty: step 2
        confirms a scope without saying anything about sharing, and a rename
        or an anonymize toggle must never strip access as a side effect."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="omit-conn")
        gid = c.post("/api/admin/groups", json={"name": "sp-omit"}, headers=_auth(token)).json()["id"]

        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:omit", "display_path": "Omit", "group_ids": [gid]},
            headers=_auth(token),
        )
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:omit", "display_path": "Renamed"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["group_ids"] == [gid], "an omitted field must not revoke"
        assert r.json()["display_path"] == "Renamed"

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


# ---------------------------------------------------------------------------
# SharePoint ACL mirroring (2026-08-30 plan, Task 5) — access_mode, drive_id,
# mirrored (sentinel-owned) grants staying read-only through the wizard's
# own share-step checkboxes, and the admin "sync now" trigger.
# ---------------------------------------------------------------------------


class TestAccessModeAndDriveId:
    def test_access_mode_defaults_to_manual_and_drive_id_defaults_to_none(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="am-default")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:am1", "display_path": "Manual"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["access_mode"] == "manual"
        assert r.json()["drive_id"] is None

    def test_mirrored_without_drive_id_is_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="am-missing-drive")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:am2", "display_path": "Mirrored", "access_mode": "mirrored"},
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "missing_drive_id"

    def test_mirrored_with_drive_id_round_trips_through_list_and_confirm(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="am-mirrored")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:am3",
                "display_path": "Mirrored",
                "access_mode": "mirrored",
                "drive_id": "drive-abc123",
            },
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["access_mode"] == "mirrored"
        assert body["drive_id"] == "drive-abc123"

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"][0]
        assert listed["access_mode"] == "mirrored"
        assert listed["drive_id"] == "drive-abc123"

    def test_malformed_drive_id_is_422_not_a_500(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="am-bad-drive")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:am4",
                "display_path": "Mirrored",
                "access_mode": "mirrored",
                "drive_id": "not/a-valid-id",
            },
            headers=_auth(token),
        )
        assert r.status_code == 422, r.text

    def test_manual_scope_may_omit_drive_id(self, seeded_app):
        """The obligation's other half: a manual scope never needs one."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="am-manual-no-drive")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:am5", "display_path": "Manual", "access_mode": "manual"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["drive_id"] is None


class TestMirroredGrantsSurviveTheShareStep:
    """A sentinel-owned (``assigned_by='system:sharepoint-acl-sync'``) grant
    is the ``sharepoint-acl-sync`` job's own bookkeeping — the wizard's
    share-step checkboxes (``group_ids``) must never delete it, even when
    every OTHER group is unticked. "Stop mirroring" is ``access_mode``, not
    this checkbox (spec §2.3/§2.5)."""

    def _confirm_with_sentinel_grant(self, c, token, conn_id, *, source_scope_id, admin_group_id):
        from app.resource_types import ResourceType
        from connectors.sharepoint.acl_sync import ACL_SYNC_SENTINEL
        from src.repositories import resource_grants_repo, user_groups_repo

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": source_scope_id, "display_path": "Sentinel", "group_ids": [admin_group_id]},
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text
        collection_id = confirmed.json()["collection_id"]

        # A sentinel-owned group+grant, written the way `run_acl_sync` would.
        sentinel_group = user_groups_repo().ensure(name=f"entra:{source_scope_id}", created_by=ACL_SYNC_SENTINEL)
        resource_grants_repo().ensure_grant(
            sentinel_group["id"], ResourceType.COLLECTION.value, collection_id, assigned_by=ACL_SYNC_SENTINEL
        )
        return collection_id, sentinel_group["id"]

    def test_unticking_every_group_deletes_the_admin_grant_not_the_sentinel_one(self, seeded_app):
        from app.resource_types import ResourceType
        from connectors.sharepoint.acl_sync import ACL_SYNC_SENTINEL
        from src.repositories import resource_grants_repo

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="sentinel-guard")
        admin_group_id = c.post("/api/admin/groups", json={"name": "sp-admin-grant"}, headers=_auth(token)).json()["id"]

        collection_id, sentinel_group_id = self._confirm_with_sentinel_grant(
            c, token, conn_id, source_scope_id="drive:sentinel", admin_group_id=admin_group_id
        )

        # Untick everything through the share step.
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:sentinel", "display_path": "Sentinel", "group_ids": []},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        # The admin grant is gone; the sentinel one is the only survivor —
        # NOT an empty list, since the row is still (mirror-)granted.
        assert r.json()["group_ids"] == [sentinel_group_id]

        remaining = [
            g
            for g in resource_grants_repo().list_all(resource_type=ResourceType.COLLECTION.value)
            if g["resource_id"] == collection_id
        ]
        assert len(remaining) == 1
        assert remaining[0]["group_id"] == sentinel_group_id
        assert remaining[0]["assigned_by"] == ACL_SYNC_SENTINEL

    def test_switching_mirrored_to_manual_deletes_the_sentinel_grant(self, seeded_app):
        from app.resource_types import ResourceType
        from connectors.sharepoint.acl_sync import ACL_SYNC_SENTINEL
        from src.repositories import resource_grants_repo, user_groups_repo

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="mode-switch")

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:switch",
                "display_path": "Switching",
                "access_mode": "mirrored",
                "drive_id": "drive-switch",
            },
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text
        collection_id = confirmed.json()["collection_id"]

        sentinel_group = user_groups_repo().ensure(name="entra:switch-oid", created_by=ACL_SYNC_SENTINEL)
        resource_grants_repo().ensure_grant(
            sentinel_group["id"], ResourceType.COLLECTION.value, collection_id, assigned_by=ACL_SYNC_SENTINEL
        )

        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:switch", "display_path": "Switching", "access_mode": "manual"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["access_mode"] == "manual"

        remaining = [
            g
            for g in resource_grants_repo().list_all(resource_type=ResourceType.COLLECTION.value)
            if g["resource_id"] == collection_id
        ]
        assert remaining == []


class TestAclSyncTrigger:
    """``POST /connections/{connection_id}/acl-sync`` — admin "sync now"
    trigger for the ``sharepoint-acl-sync`` job (spec §5.1)."""

    ACL_SYNC = "{base}/{cid}/acl-sync"

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(
            self.ACL_SYNC.format(base=BASE, cid="nope"), headers=_auth(seeded_app["analyst_token"])
        )
        assert r.status_code == 403

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].post(self.ACL_SYNC.format(base=BASE, cid="nope"))
        assert r.status_code == 401

    def test_409_when_flag_disabled_even_for_an_unknown_connection(self, seeded_app, monkeypatch):
        """The router-level gate refuses the WHOLE surface before any
        per-route work — including the connection lookup — so an unknown
        connection id gets the same 409 as a real one. Clears the
        module-level `_sharepoint_enabled_by_default` env override so the
        mocked `get_value` config is what actually decides."""
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        r = seeded_app["client"].post(
            self.ACL_SYNC.format(base=BASE, cid="does-not-exist"), headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "feature_disabled"

    def test_409_when_flag_disabled(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="acl-flag-off")
        r = c.post(self.ACL_SYNC.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "feature_disabled"

    def test_happy_path_enqueues_the_exact_payload_shape(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({"sharepoint": {"enabled": True}}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="acl-happy")
        r = c.post(self.ACL_SYNC.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["job_id"]

        from src.repositories import jobs_repo

        job = jobs_repo().get(body["job_id"])
        assert job["kind"] == "sharepoint-acl-sync"
        assert job["payload_json"] == {"connection_id": conn_id}

    def test_duplicate_run_is_409(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({"sharepoint": {"enabled": True}}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="acl-dup")
        first = c.post(self.ACL_SYNC.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert first.status_code == 202, first.text
        second = c.post(self.ACL_SYNC.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "acl_sync_already_running"
        assert second.json()["detail"]["job_id"] == first.json()["job_id"]


class TestSubtreeSweepTrigger:
    """``POST /connections/{connection_id}/subtree-sweep`` — admin
    "re-check subtrees now" trigger for the ``sharepoint-subtree-sweep`` job
    (2026-08-31 plan, Task 8). Mirrors ``TestAclSyncTrigger`` exactly — same
    idempotency-key/dedup/flag-gate mechanics, same job-kind family."""

    SWEEP = "{base}/{cid}/subtree-sweep"

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(
            self.SWEEP.format(base=BASE, cid="nope"), headers=_auth(seeded_app["analyst_token"])
        )
        assert r.status_code == 403

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].post(self.SWEEP.format(base=BASE, cid="nope"))
        assert r.status_code == 401

    def test_409_when_flag_disabled_even_for_an_unknown_connection(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        r = seeded_app["client"].post(
            self.SWEEP.format(base=BASE, cid="does-not-exist"), headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "feature_disabled"

    def test_409_when_flag_disabled(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="sweep-flag-off")
        r = c.post(self.SWEEP.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "feature_disabled"

    def test_happy_path_enqueues_the_exact_payload_shape(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({"sharepoint": {"enabled": True}}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="sweep-happy")
        r = c.post(self.SWEEP.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["job_id"]

        from src.repositories import jobs_repo

        job = jobs_repo().get(body["job_id"])
        assert job["kind"] == "sharepoint-subtree-sweep"
        assert job["payload_json"] == {"connection_id": conn_id}

    def test_duplicate_run_is_409(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({"sharepoint": {"enabled": True}}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="sweep-dup")
        first = c.post(self.SWEEP.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert first.status_code == 202, first.text
        second = c.post(self.SWEEP.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "sweep_already_running"
        assert second.json()["detail"]["job_id"] == first.json()["job_id"]

    def test_idempotency_key_is_distinct_from_acl_sync(self, seeded_app, monkeypatch):
        """A sweep trigger and an acl-sync trigger for the SAME connection
        must never dedup against each other — they are different job kinds
        with different idempotency-key prefixes."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({"sharepoint": {"enabled": True}}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="sweep-vs-acl-sync")
        sweep = c.post(self.SWEEP.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert sweep.status_code == 202, sweep.text
        acl_sync = c.post(f"{BASE}/{conn_id}/acl-sync", headers=_auth(seeded_app["admin_token"]))
        assert acl_sync.status_code == 202, acl_sync.text
        assert sweep.json()["job_id"] != acl_sync.json()["job_id"]


_ENABLED_FACTS_CONFIG = {
    "sharepoint": {"enabled": True},
    "extraction": {"facts": {"enabled": True}},
    "facts": {"enabled": True},
}


class TestFactsExtractionTrigger:
    """``POST /connections/{connection_id}/facts-extract`` — admin/ops
    trigger for the ``sharepoint-facts-extraction`` job: build the fact
    graph over a connection's already-indexed corpus, without a crawl.
    Mirrors ``TestAclSyncTrigger``/``TestSubtreeSweepTrigger``'s mechanics,
    plus its OWN readiness gate (the two facts-specific switches, checked
    BEFORE enqueue like ``TestExtractionTrigger``'s own readiness check)."""

    FACTS_EXTRACT = "{base}/{cid}/facts-extract"

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(
            self.FACTS_EXTRACT.format(base=BASE, cid="nope"), headers=_auth(seeded_app["analyst_token"])
        )
        assert r.status_code == 403

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].post(self.FACTS_EXTRACT.format(base=BASE, cid="nope"))
        assert r.status_code == 401

    def test_409_when_sharepoint_disabled_even_for_an_unknown_connection(self, seeded_app, monkeypatch):
        """The router-level gate refuses the WHOLE surface before any
        per-route work — including the connection lookup."""
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        r = seeded_app["client"].post(
            self.FACTS_EXTRACT.format(base=BASE, cid="does-not-exist"), headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "feature_disabled"

    def test_404_for_unknown_connection_before_the_facts_readiness_gate(self, seeded_app, monkeypatch):
        """404 fires even with the two facts switches OFF — connection
        existence is checked BEFORE facts readiness, same ordering as the
        crawl trigger's own extraction-readiness check."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({"sharepoint": {"enabled": True}}))
        r = seeded_app["client"].post(
            self.FACTS_EXTRACT.format(base=BASE, cid="does-not-exist"), headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 404

    def test_409_when_the_cost_switch_is_off(self, seeded_app, monkeypatch):
        monkeypatch.setattr(
            "app.instance_config.get_value",
            _config_get_value({"sharepoint": {"enabled": True}, "facts": {"enabled": True}}),
        )
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="facts-cost-off")
        r = c.post(self.FACTS_EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "facts_extraction_disabled"
        assert "extraction.facts.enabled" in r.json()["detail"]["message"]

    def test_409_when_the_facts_surface_is_off(self, seeded_app, monkeypatch):
        monkeypatch.setattr(
            "app.instance_config.get_value",
            _config_get_value({"sharepoint": {"enabled": True}, "extraction": {"facts": {"enabled": True}}}),
        )
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="facts-surface-off")
        r = c.post(self.FACTS_EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "facts_extraction_disabled"
        assert "facts.enabled" in r.json()["detail"]["message"]

    def test_happy_path_enqueues_the_exact_payload_shape(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_FACTS_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="facts-happy")
        r = c.post(self.FACTS_EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["job_id"]

        from src.repositories import jobs_repo

        job = jobs_repo().get(body["job_id"])
        assert job["kind"] == "sharepoint-facts-extraction"
        assert job["payload_json"] == {"connection_id": conn_id}

    def test_options_ride_in_the_payload_only_when_set(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_FACTS_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="facts-options")
        r = c.post(
            self.FACTS_EXTRACT.format(base=BASE, cid=conn_id),
            headers=_auth(seeded_app["admin_token"]),
            json={"doc_ids": ["d1", "d2"], "timeout_s": 120},
        )
        assert r.status_code == 202, r.text

        from src.repositories import jobs_repo

        job = jobs_repo().get(r.json()["job_id"])
        assert job["payload_json"] == {"connection_id": conn_id, "doc_ids": ["d1", "d2"], "timeout_s": 120}

    def test_duplicate_run_is_409(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_FACTS_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="facts-dup")
        first = c.post(self.FACTS_EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert first.status_code == 202, first.text
        second = c.post(self.FACTS_EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "facts_extraction_already_running"
        assert second.json()["detail"]["job_id"] == first.json()["job_id"]

    def test_idempotency_key_is_distinct_from_the_crawl_trigger(self, seeded_app, monkeypatch):
        """A facts-extract trigger and an in-flight ``corpus-extraction`` job
        for the SAME connection must never dedup against each other —
        different job kinds, different idempotency-key prefixes. The crawl
        side is enqueued directly (not via ``POST …/extract``, which also
        gates on the ``extraction`` optional dependency actually being
        installed — a separate concern this test has no business on)."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_FACTS_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="facts-vs-crawl")

        from src.repositories import jobs_repo

        crawl_job = jobs_repo().enqueue(
            "corpus-extraction", {"connection_id": conn_id}, idempotency_key=f"corpus-extraction:{conn_id}"
        )

        facts = c.post(self.FACTS_EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert facts.status_code == 202, facts.text
        assert facts.json()["job_id"] != crawl_job["id"]


class TestCertificateMetadata:
    """`GET /connections/{id}/certificate` — read-only certificate metadata
    for the source card / an admin's own comparison against the identity
    provider, derived at request time from the connection's already-stored
    PEM. See `connectors.sharepoint.graph_client.certificate_metadata` for
    the derivation itself; this class covers the endpoint's plumbing:
    auth gating, connection resolution, and the typed-absence paths."""

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/nope/certificate", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/nope/certificate")
        assert r.status_code == 401

    def test_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/does-not-exist/certificate", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_returns_metadata_for_a_configured_certificate(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="cert-meta-conn")
        r = c.get(f"{BASE}/{conn_id}/certificate", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reason"] is None
        cert = body["certificate"]
        assert cert["subject"] == "CN=agnes-test"
        assert cert["issuer"] == "CN=agnes-test"
        assert cert["thumbprint_x5t"]
        assert len(cert["thumbprint_sha1_hex"]) == 40
        assert cert["status"] in ("ok", "expiring_soon", "expired")
        assert isinstance(cert["expires_in_days"], int)

    def test_response_never_contains_private_key_material(self, seeded_app, monkeypatch):
        """HARD CONSTRAINT: metadata only, never the private key — even
        though the stored PEM is a combined cert+key bundle."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="cert-meta-nokey-conn")
        r = c.get(f"{BASE}/{conn_id}/certificate", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        assert "PRIVATE KEY" not in r.text
        assert "BEGIN CERTIFICATE" not in r.text

    def test_no_certificate_configured_is_a_clean_200_not_a_500(self, seeded_app, monkeypatch):
        monkeypatch.delenv("SHAREPOINT_CERT_PRIVATE_KEY", raising=False)
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="cert-meta-missing-conn")
        r = c.get(f"{BASE}/{conn_id}/certificate", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["certificate"] is None
        assert body["reason"]

    def test_unparseable_certificate_is_a_clean_200_not_a_500(self, seeded_app, monkeypatch):
        monkeypatch.setenv(
            "SHAREPOINT_CERT_PRIVATE_KEY", "-----BEGIN CERTIFICATE-----\nbm90LXJlYWw=\n-----END CERTIFICATE-----\n"
        )
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="cert-meta-garbage-conn")
        r = c.get(f"{BASE}/{conn_id}/certificate", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["certificate"] is None
        assert body["reason"].startswith("certificate_unparseable")


class TestWebhookSecretRotation:
    """`POST /connections/{id}/webhook` — (re)generates the Graph
    change-notification receiver's shared secret, returning it alongside the
    receiver URL. Both feed `connectors/sharepoint/subscriptions.py`: the
    secret becomes each drive subscription's `clientState`, the URL its
    `notificationUrl` (see tests/test_sharepoint_subscriptions.py)."""

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(f"{BASE}/nope/webhook", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].post(f"{BASE}/nope/webhook")
        assert r.status_code == 401

    def test_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].post(f"{BASE}/does-not-exist/webhook", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_generates_a_secret_and_the_receiver_url(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="webhook-conn")
        r = c.post(f"{BASE}/{conn_id}/webhook", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["webhook_url"].endswith(f"/api/webhooks/sharepoint/{conn_id}")
        assert isinstance(body["secret"], str) and len(body["secret"]) >= 32

    def test_persists_the_secret_on_the_connection(self, seeded_app):
        from src.repositories import source_connections_repo

        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="webhook-persist-conn")
        r = c.post(f"{BASE}/{conn_id}/webhook", headers=_auth(token))
        secret = r.json()["secret"]
        row = source_connections_repo().get(conn_id)
        assert row["config"]["webhook_secret"] == secret

    def test_rotating_again_mints_a_different_secret(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="webhook-rotate-conn")
        first = c.post(f"{BASE}/{conn_id}/webhook", headers=_auth(token)).json()["secret"]
        second = c.post(f"{BASE}/{conn_id}/webhook", headers=_auth(token)).json()["secret"]
        assert first != second


class TestChangesFeedFailsCleanOnDuckDB:
    """The observed-changes feed (`GET .../changes`) is PG-only —
    `corpus_file_events` has no DuckDB counterpart (A3 ratchet). The happy
    path (real events, since/until filtering, pagination, all four change
    kinds off a realistic upload/update/rename/delete fixture) lives in
    tests/db_pg/test_sharepoint_changes_pg.py; this suite (the DuckDB-backed
    default here) only proves the typed 501 — never a raw 500 — regardless
    of whether the connection has any confirmed scopes yet."""

    @pytest.fixture(autouse=True)
    def _pin_duckdb_backend(self, duckdb_backend_pinned):
        """Resolve DuckDB regardless of a `tests/db_pg/` test having run
        earlier in this worker process (issue #1658)."""

    def test_changes_501_on_duckdb_backend_no_scopes(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="changes-noscope-conn")
        r = c.get(f"{BASE}/{conn_id}/changes", headers=_auth(token))
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"

    def test_changes_501_on_duckdb_backend_with_a_confirmed_scope(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="changes-scoped-conn")
        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:a", "display_path": "A"},
            headers=_auth(token),
        )
        r = c.get(f"{BASE}/{conn_id}/changes", headers=_auth(token))
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"


class TestConsolidateCollectionsFailsCleanOnDuckDB:
    """Collection consolidation (`POST .../collections/consolidate`) is
    PG-only by construction — it touches `corpus_file_sources` / `claims` /
    `fact_alias_sources`, themselves PG-only (A3 ratchet). The happy path
    (preview counts, the real merge, grants union, soft-delete) lives in
    tests/db_pg/test_sharepoint_collection_consolidate_route_pg.py; this
    suite (the DuckDB-backed default here) only proves the typed 501 —
    never a raw 500 — for both the dry-run and the real-merge shape."""

    @pytest.fixture(autouse=True)
    def _pin_duckdb_backend(self, duckdb_backend_pinned):
        """Resolve DuckDB regardless of a `tests/db_pg/` test having run
        earlier in this worker process (issue #1658)."""

    def _connection_with_two_scopes(self, c, token, name):
        conn_id = _create_connection(c, token, name=name)
        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:a", "display_path": "A"},
            headers=_auth(token),
        )
        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:b", "display_path": "B"},
            headers=_auth(token),
        )
        return conn_id

    def test_dry_run_501_on_duckdb_backend(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._connection_with_two_scopes(c, token, "consolidate-duckdb-dry")
        r = c.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target": {"name": "Merged"}},
            headers=_auth(token),
        )
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"

    def test_real_merge_501_on_duckdb_backend(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._connection_with_two_scopes(c, token, "consolidate-duckdb-real")
        r = c.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target": {"name": "Merged"}, "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"

    def test_include_split_siblings_still_501s_on_duckdb_backend(self, seeded_app):
        """`include_split_siblings` widens which collections are folded but
        never changes WHICH repo does the folding — still typed 501, never
        a raw 500, on a DuckDB-backed instance."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._connection_with_two_scopes(c, token, "consolidate-duckdb-siblings")
        r = c.post(
            f"{BASE}/{conn_id}/collections/consolidate",
            json={"target": {"name": "Merged"}, "include_split_siblings": True},
            headers=_auth(token),
        )
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"


class TestSplitMergeFailsCleanOnDuckDB:
    """Split-merge (``POST .../splits/merge``) is PG-only by construction —
    it touches ``sharepoint_connection_state`` (crawl/facts bookkeeping) and
    ``extraction_runs``, both PG-only (A3 ratchet), on top of the same
    PG-only collection consolidation ``TestConsolidateCollectionsFailsCleanOnDuckDB``
    above already covers. The happy path lives in
    tests/db_pg/test_sharepoint_connection_split_merge_route_pg.py; this
    suite (the DuckDB-backed default here) only proves the typed 501 —
    never a raw 500 — for both the dry-run and the real-merge shape."""

    @pytest.fixture(autouse=True)
    def _pin_duckdb_backend(self, duckdb_backend_pinned):
        """Resolve DuckDB regardless of a ``tests/db_pg/`` test having run
        earlier in this worker process (issue #1658)."""

    def test_dry_run_501_on_duckdb_backend(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        target = _create_connection(c, token, name="split-merge-duckdb-target")
        sib = _create_connection(c, token, name="split-merge-duckdb-sib")
        r = c.post(
            f"{BASE}/{target}/splits/merge",
            json={"sibling_ids": [sib], "target": {"name": "Merged"}},
            headers=_auth(token),
        )
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"

    def test_real_merge_501_on_duckdb_backend(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        target = _create_connection(c, token, name="split-merge-duckdb-real-target")
        sib = _create_connection(c, token, name="split-merge-duckdb-real-sib")
        r = c.post(
            f"{BASE}/{target}/splits/merge",
            json={"sibling_ids": [sib], "target": {"name": "Merged"}, "dry_run": False},
            headers=_auth(token),
        )
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"


# ---------------------------------------------------------------------------
# Extraction enqueue wiring (TCRD-226) — the admin trigger + the scheduled
# sweep. Neither test class runs a crawl; they cover the endpoints' OWN
# responsibilities: 404-before-work, the feature-usable gate, duplicate-run
# dedup, and the exact payload shape enqueued for
# app/worker/kinds.py::_run_corpus_extraction to pick up.
# ---------------------------------------------------------------------------


def _config_get_value(config: dict):
    """A drop-in ``app.instance_config.get_value`` fake driven by a plain
    nested dict — same idiom as ``tests/test_worker_kinds.py``'s helper of
    the same name (duplicated, not imported, so the two test modules never
    couple on a shared fixture)."""

    def _get(*keys, default=None):
        current = config
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current

    return _get


_ENABLED_EXTRACTION_CONFIG = {
    "sharepoint": {"enabled": True},
    "extraction": {
        "producer": {"command": "python -m fake_producer"},
        "timeout_s": 60,
    },
}


class TestExtractionTrigger:
    """``POST /connections/{connection_id}/extract`` — admin-triggered
    one-off run of the existing ``corpus-extraction`` job kind."""

    EXTRACT = "{base}/{cid}/extract"

    @pytest.fixture(autouse=True)
    def _clear_extraction_env_var(self, monkeypatch):
        # AGNES_EXTRACTION_PRODUCER_COMMAND / AGNES_EXTRACTION_PRODUCER_MODULE
        # win over the mocked get_value config — clear them so each test's
        # fake config is what actually decides, except the one test below
        # that sets one on purpose. AGNES_SHAREPOINT_ENABLED is left alone
        # (module-level `_sharepoint_enabled_by_default` keeps it ON) so
        # test_requires_admin/test_requires_auth still reach the per-route
        # auth dependency; the two tests below that need it OFF clear it
        # themselves.
        monkeypatch.delenv("AGNES_EXTRACTION_PRODUCER_COMMAND", raising=False)
        monkeypatch.delenv("AGNES_EXTRACTION_PRODUCER_MODULE", raising=False)

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(
            self.EXTRACT.format(base=BASE, cid="nope"), headers=_auth(seeded_app["analyst_token"])
        )
        assert r.status_code == 403

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].post(self.EXTRACT.format(base=BASE, cid="nope"))
        assert r.status_code == 401

    def test_404_for_unknown_connection_before_extraction_readiness(self, seeded_app, monkeypatch):
        """404 fires even with NO producer configured — connection
        existence is checked BEFORE the extraction-readiness gate. Requires
        the sharepoint switch itself ON (the router-level gate runs first
        and unconditionally, so an unknown connection with the WHOLE
        connector off gets 409, not 404 — see the sibling test below)."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({"sharepoint": {"enabled": True}}))
        r = seeded_app["client"].post(
            self.EXTRACT.format(base=BASE, cid="does-not-exist"), headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 404

    def test_409_when_sharepoint_disabled_even_for_an_unknown_connection(self, seeded_app, monkeypatch):
        """The router-level gate refuses the WHOLE surface before any
        per-route work — including the connection lookup."""
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        r = seeded_app["client"].post(
            self.EXTRACT.format(base=BASE, cid="does-not-exist"), headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "feature_disabled"

    def test_refuses_when_sharepoint_disabled(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-disabled")
        r = c.post(self.EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "feature_disabled"

    def test_refuses_when_the_extraction_extra_is_not_installed(self, seeded_app, monkeypatch):
        """The pipeline runs IN-PROCESS now, so a server without the
        converter backends would crawl and then fail on every single file.
        Refused up front instead, naming the exact install command."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        # `None` in sys.modules makes the import raise ImportError — what an
        # uninstalled extra actually looks like.
        monkeypatch.setitem(sys.modules, "pypdfium2", None)
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-no-extra")
        r = c.post(self.EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "extraction_dependencies_missing"
        assert "agnes[extraction]" in detail["message"]

    def test_the_switch_alone_satisfies_readiness(self, seeded_app, monkeypatch):
        """Readiness needs exactly two things — ``sharepoint.enabled`` and
        the installed ``extraction`` extra. No producer config exists
        anymore, so nothing else may be demanded before a run can start."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({"sharepoint": {"enabled": True}}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-switch-only")
        r = c.post(self.EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 202, r.text

    def test_happy_path_enqueues_the_exact_payload_shape(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-happy")
        r = c.post(self.EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["job_id"]

        from src.repositories import jobs_repo

        job = jobs_repo().get(body["job_id"])
        assert job["kind"] == "corpus-extraction"
        # The exact payload shape app/worker/kinds.py::_run_corpus_extraction
        # documents: connection_id required, nothing invented.
        assert job["payload_json"] == {"connection_id": conn_id}

    def test_run_options_ride_in_the_payload(self, seeded_app, monkeypatch):
        """The Run-now options (per-run concurrency / time limit) land in
        the job payload under the exact keys the handler documents — and
        ONLY the keys the admin set, so an absent key keeps meaning "the
        configured value"."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-options")
        r = c.post(
            self.EXTRACT.format(base=BASE, cid=conn_id),
            json={"concurrency": 8, "timeout_s": 1200},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 202, r.text

        from src.repositories import jobs_repo

        job = jobs_repo().get(r.json()["job_id"])
        assert job["payload_json"] == {"connection_id": conn_id, "concurrency": 8, "timeout_s": 1200}

    def test_a_partial_options_body_only_adds_what_was_set(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-options-partial")
        r = c.post(
            self.EXTRACT.format(base=BASE, cid=conn_id),
            json={"concurrency": 2},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 202, r.text

        from src.repositories import jobs_repo

        job = jobs_repo().get(r.json()["job_id"])
        assert job["payload_json"] == {"connection_id": conn_id, "concurrency": 2}

    def test_resync_option_rides_in_the_payload(self, seeded_app, monkeypatch):
        """The supported recovery path for a connection whose delta cursor
        ran past documents it never ingested: no key when unset (same
        "absent means configured/default" contract as the other options),
        `resync: true` when the admin asks for one."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-options-resync")
        r = c.post(
            self.EXTRACT.format(base=BASE, cid=conn_id),
            json={"resync": True},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 202, r.text

        from src.repositories import jobs_repo

        job = jobs_repo().get(r.json()["job_id"])
        assert job["payload_json"] == {"connection_id": conn_id, "resync": True}

    def test_force_reprocess_option_rides_in_the_payload(self, seeded_app, monkeypatch):
        """The stronger 're-process everything' control (unlike `resync`,
        also ignores cTags — see `connectors.sharepoint.crawler._process_
        item`): no key when unset (same "absent means off" contract as
        every other option here), `force_reprocess: true` when the admin
        ticks the box — and nothing else rides along uninvited."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-options-force")
        r = c.post(
            self.EXTRACT.format(base=BASE, cid=conn_id),
            json={"force_reprocess": True},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 202, r.text

        from src.repositories import jobs_repo

        job = jobs_repo().get(r.json()["job_id"])
        assert job["payload_json"] == {"connection_id": conn_id, "force_reprocess": True}

    def test_retry_failed_option_rides_in_the_payload(self, seeded_app, monkeypatch):
        """The targeted alternative to `resync` (see
        `connectors.sharepoint.crawler._retry_failed_items`'s
        `include_given_up`): no key when unset, `retry_failed: true` when
        the admin ticks the box — and nothing else rides along uninvited."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-options-retry-failed")
        r = c.post(
            self.EXTRACT.format(base=BASE, cid=conn_id),
            json={"retry_failed": True},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 202, r.text

        from src.repositories import jobs_repo

        job = jobs_repo().get(r.json()["job_id"])
        assert job["payload_json"] == {"connection_id": conn_id, "retry_failed": True}

    def test_retry_failed_response_carries_queued_count_from_the_backlog(self, seeded_app, monkeypatch):
        """The toast the button shows after clicking must say the same
        count the button already promised — read from the SAME persisted
        backlog `GET .../extraction/status` counts."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-retry-failed-count")

        from connectors.sharepoint.crawler import save_state

        save_state(
            conn_id,
            {
                "delta_links": {},
                "ctags": {},
                "failed_items": {
                    "graph:item1": {
                        "state_key": "b!drive1",
                        "item": {"id": "item1", "name": "a.pdf"},
                        "path": "Reports/a.pdf",
                    },
                    "graph:item2": {
                        "state_key": "b!drive1",
                        "item": {"id": "item2", "name": "b.pdf"},
                        "path": "Reports/b.pdf",
                    },
                },
                "empty_items": {},
            },
        )

        r = c.post(
            self.EXTRACT.format(base=BASE, cid=conn_id),
            json={"retry_failed": True},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 202, r.text
        assert r.json()["queued_count"] == 2

    def test_a_plain_trigger_never_carries_a_queued_count(self, seeded_app, monkeypatch):
        """`queued_count` is meaningless without `retry_failed` — an ordinary
        trigger's response shape must stay exactly what it always was."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-no-queued-count")
        r = c.post(self.EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 202, r.text
        assert "queued_count" not in r.json()

    def test_out_of_range_run_options_are_refused_not_reclamped(self, seeded_app, monkeypatch):
        """The crawler would clamp these silently; the endpoint refuses them
        instead, where the admin can see why the run isn't what they asked
        for. Bounds mirror the crawler's own ([1,16] payload concurrency,
        [0,86400] timeout)."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-options-range")
        for body in ({"concurrency": 0}, {"concurrency": 17}, {"timeout_s": -1}, {"timeout_s": 86401}):
            r = c.post(
                self.EXTRACT.format(base=BASE, cid=conn_id),
                json=body,
                headers=_auth(seeded_app["admin_token"]),
            )
            assert r.status_code == 422, (body, r.text)

    def test_duplicate_run_is_409(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-dup")
        first = c.post(self.EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert first.status_code == 202, first.text
        second = c.post(self.EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "extraction_already_running"
        assert second.json()["detail"]["job_id"] == first.json()["job_id"]

    def test_records_dispatch_on_the_connection_for_the_due_check(self, seeded_app, monkeypatch):
        """The manual trigger also stamps config.extraction.last_run_at /
        last_job_id — the scheduled sweep's due-check reads it, and a
        manual run right before the schedule fires must reset that clock."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-stamp")
        r = c.post(self.EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 202, r.text

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        extraction_state = (row.get("config") or {}).get("extraction") or {}
        assert extraction_state.get("last_job_id") == r.json()["job_id"]
        assert extraction_state.get("last_run_at")


class TestRetryEmptyExtraction:
    """``POST /connections/{connection_id}/extraction/retry-empty`` —
    re-queues a connection's ``convert_empty`` backlog. Same job/readiness
    machinery as ``TestExtractionTrigger`` above; this class covers what is
    DIFFERENT about it (the payload's ``retry_empty`` flag, and
    ``queued_count`` reflecting the persisted backlog)."""

    RETRY_EMPTY = "{base}/{cid}/extraction/retry-empty"

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(
            self.RETRY_EMPTY.format(base=BASE, cid="nope"), headers=_auth(seeded_app["analyst_token"])
        )
        assert r.status_code == 403

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].post(self.RETRY_EMPTY.format(base=BASE, cid="nope"))
        assert r.status_code == 401

    def test_404_for_unknown_connection(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        r = seeded_app["client"].post(
            self.RETRY_EMPTY.format(base=BASE, cid="does-not-exist"), headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 404

    def test_409_when_sharepoint_disabled(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="retry-empty-off")
        # The router-level gate refuses first, so this never even reaches
        # the connection lookup for an existing connection either.
        r = c.post(self.RETRY_EMPTY.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "feature_disabled"

    def test_an_empty_backlog_still_succeeds_with_a_zero_count(self, seeded_app, monkeypatch):
        """No `convert_empty` items ever recorded is a normal, successful
        answer, not an error — the run still completes (its ordinary
        incremental walk is harmless), it just has nothing to replay."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="retry-empty-none")
        r = c.post(self.RETRY_EMPTY.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 202, r.text
        assert r.json()["queued_count"] == 0

    def test_queued_count_reflects_the_persisted_empty_items_backlog(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="retry-empty-count")

        from connectors.sharepoint.crawler import save_state

        save_state(
            conn_id,
            {
                "delta_links": {},
                "ctags": {},
                "failed_items": {},
                "empty_items": {
                    "graph:item1": {
                        "state_key": "b!drive1",
                        "item": {"id": "item1", "name": "a.pdf"},
                        "path": "Reports/a.pdf",
                        "first_seen_at": "2026-09-01T00:00:00+00:00",
                    },
                    "graph:item2": {
                        "state_key": "b!drive1",
                        "item": {"id": "item2", "name": "b.pdf"},
                        "path": "Reports/b.pdf",
                        "first_seen_at": "2026-09-01T00:00:00+00:00",
                    },
                },
            },
        )

        r = c.post(self.RETRY_EMPTY.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 202, r.text
        assert r.json()["queued_count"] == 2

    def test_the_job_payload_carries_retry_empty_true(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="retry-empty-payload")
        r = c.post(self.RETRY_EMPTY.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 202, r.text

        from src.repositories import jobs_repo

        job = jobs_repo().get(r.json()["job_id"])
        assert job["kind"] == "corpus-extraction"
        assert job["payload_json"] == {"connection_id": conn_id, "retry_empty": True}

    def test_duplicate_run_is_409_and_shares_the_ordinary_trigger_dedup_key(self, seeded_app, monkeypatch):
        """A retry-empty run and a plain trigger for the SAME connection
        must never overlap either — both mutate the same crawl state file."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="retry-empty-dup")
        first = c.post("{base}/{cid}/extract".format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert first.status_code == 202, first.text

        second = c.post(self.RETRY_EMPTY.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "extraction_already_running"


class TestExtractionRunDue:
    """``POST /extraction/run-due`` — the scheduler-driven sweep. Not
    connection-scoped in its path; walks every sharepoint connection."""

    RUN_DUE = "/api/admin/sharepoint/extraction/run-due"

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(self.RUN_DUE, headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_409_when_sharepoint_disabled(self, seeded_app, monkeypatch):
        """The router-level gate refuses the WHOLE surface (409
        feature_disabled) before this route's own body — including its
        usually-graceful "not usable yet" no-op — ever runs. The scheduler
        (which polls this endpoint unconditionally once a schedule is
        configured) tolerates a non-2xx status by logging and moving on
        (`services/scheduler/__main__.py`'s dispatch loop), so this is a
        louder signal than the old no-op, not a functional break."""
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        config = {"extraction": {**_ENABLED_EXTRACTION_CONFIG["extraction"], "schedule": "every 15m"}}
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(config))
        c = seeded_app["client"]
        _create_connection(c, seeded_app["admin_token"], name="due-off")
        r = c.post(self.RUN_DUE, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "feature_disabled"

    def test_noop_when_the_extraction_extra_is_missing(self, seeded_app, monkeypatch):
        """Sharepoint itself ON (router-level gate passes), but the
        `extraction` optional dependency extra is not installed — the
        route's OWN readiness no-op still applies, a clean 200 rather than
        an error, since this endpoint fires unconditionally on its own
        cadence once a schedule is configured. (The producer-config leg of
        this check died with the external mode — deps are the only
        remaining readiness requirement past the switch.)"""
        config = {"sharepoint": {"enabled": True}, "extraction": {"schedule": "every 15m"}}
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(config))
        monkeypatch.setitem(sys.modules, "pypdfium2", None)
        c = seeded_app["client"]
        _create_connection(c, seeded_app["admin_token"], name="due-no-extra")
        r = c.post(self.RUN_DUE, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 0
        assert r.json()["dispatched"] == []

    def test_noop_when_no_schedule_configured(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(_ENABLED_EXTRACTION_CONFIG))
        c = seeded_app["client"]
        _create_connection(c, seeded_app["admin_token"], name="due-no-schedule")
        r = c.post(self.RUN_DUE, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 0

    def test_dispatches_a_connection_never_run_before(self, seeded_app, monkeypatch):
        config = {
            "sharepoint": {"enabled": True},
            "extraction": {**_ENABLED_EXTRACTION_CONFIG["extraction"], "schedule": "every 15m"},
        }
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(config))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="due-never-run")
        r = c.post(self.RUN_DUE, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["dispatched"] == [conn_id]

        from src.repositories import jobs_repo

        jobs = jobs_repo().list(kind="corpus-extraction")
        assert any(j["payload_json"] == {"connection_id": conn_id} for j in jobs)

    def test_skips_a_connection_not_due_yet(self, seeded_app, monkeypatch):
        config = {
            "sharepoint": {"enabled": True},
            "extraction": {**_ENABLED_EXTRACTION_CONFIG["extraction"], "schedule": "every 15m"},
        }
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(config))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="due-not-yet")
        first = c.post(self.RUN_DUE, headers=_auth(seeded_app["admin_token"]))
        assert first.json()["dispatched"] == [conn_id]

        second = c.post(self.RUN_DUE, headers=_auth(seeded_app["admin_token"]))
        assert second.status_code == 200, second.text
        assert second.json()["dispatched"] == []

    def test_ignores_non_sharepoint_connections(self, seeded_app, monkeypatch):
        config = {
            "sharepoint": {"enabled": True},
            "extraction": {**_ENABLED_EXTRACTION_CONFIG["extraction"], "schedule": "every 15m"},
        }
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(config))
        c = seeded_app["client"]
        c.post(
            "/api/admin/source-connections",
            json={"name": "bq-conn", "source_type": "bigquery", "config": {"project": "p"}},
            headers=_auth(seeded_app["admin_token"]),
        )
        r = c.post(self.RUN_DUE, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["dispatched"] == []


class TestExcludedSubtreeAdvisory:
    """``_scope_out``'s advisory surface for the ``sharepoint-subtree-sweep``
    job's findings (2026-08-30 plan, Task 7) — ``excluded_subtree_count`` and
    ``excluded_subtrees`` (id, path, rel_path, kind — never the raw
    ``detected_at``); ``excluded_file_count`` is the sweep-v2 (2026-08-31
    plan, Task 3/8) ``kind == "file"`` slice of the same list."""

    def test_scope_out_reports_zero_when_no_sweep_has_run(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="sweep-advisory-empty")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:sweep1", "display_path": "Sweep"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["excluded_subtree_count"] == 0
        assert r.json()["excluded_file_count"] == 0
        assert r.json()["excluded_subtrees"] == []
        assert r.json()["include_excluded_subtrees"] is False

    def test_scope_out_surfaces_a_legacy_pre_sweep_v2_entry_as_a_folder(self, seeded_app):
        """Simulates the sweep job's own PRE-sweep-v2 write (no ``kind``/
        ``rel_path`` on the entry) and asserts the wizard's read path
        (``_scope_out``) reads it as ``kind="folder"``/``rel_path=None`` —
        "treat-missing-as-legacy" — never the raw ``detected_at`` timestamp."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="sweep-advisory-populated")
        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:sweep2", "display_path": "Sweep"},
            headers=_auth(token),
        )

        from src.repositories import source_connections_repo

        repo = source_connections_repo()
        row = repo.get(conn_id)
        scopes = row["config"]["scopes"]
        scopes[0]["excluded_subtrees"] = [
            {"item_id": "item-A", "path": "Sweep/A", "detected_at": "2026-08-30T00:00:00+00:00"},
        ]
        repo.update(conn_id, config={**row["config"], "scopes": scopes})

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"][0]
        assert listed["excluded_subtree_count"] == 1
        assert listed["excluded_file_count"] == 0
        assert listed["excluded_subtrees"] == [
            {"item_id": "item-A", "path": "Sweep/A", "rel_path": None, "kind": "folder"}
        ]

    def test_scope_out_reports_excluded_file_count(self, seeded_app):
        """Sweep v2 (2026-08-31 plan, Task 3) probes files as well as
        folders — ``excluded_file_count`` (Task 8) is the ``kind == "file"``
        slice, and ``rel_path``/``kind`` ride through the full projection."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="sweep-file-count")
        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:sweep3", "display_path": "Sweep"},
            headers=_auth(token),
        )

        from src.repositories import source_connections_repo

        repo = source_connections_repo()
        row = repo.get(conn_id)
        scopes = row["config"]["scopes"]
        scopes[0]["excluded_subtrees"] = [
            {
                "item_id": "item-B",
                "path": "Sweep/B",
                "rel_path": "B",
                "kind": "folder",
                "detected_at": "2026-08-31T00:00:00+00:00",
            },
            {
                "item_id": "item-C",
                "path": "Sweep/C.docx",
                "rel_path": "C.docx",
                "kind": "file",
                "detected_at": "2026-08-31T00:00:00+00:00",
            },
        ]
        repo.update(conn_id, config={**row["config"], "scopes": scopes})

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"][0]
        assert listed["excluded_subtree_count"] == 2
        assert listed["excluded_file_count"] == 1
        assert listed["excluded_subtrees"] == [
            {"item_id": "item-B", "path": "Sweep/B", "rel_path": "B", "kind": "folder"},
            {"item_id": "item-C", "path": "Sweep/C.docx", "rel_path": "C.docx", "kind": "file"},
        ]


class TestZoneVisibility:
    """``GET /connections/{connection_id}/scopes``'s ``"zones"`` key
    (2026-08-31 plan, Task 3/8) — a read-only projection of ``config
    ["acl_zones"]`` (``connectors/sharepoint/acl_sync.py::zone_rows``),
    ACTIVE and DISSOLVED zones alike."""

    def test_no_zones_returns_empty_list(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="zone-visibility-empty")
        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token))
        assert listed.status_code == 200, listed.text
        assert listed.json()["zones"] == []

    def test_scopes_response_includes_zone_rows(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="zone-visibility")
        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:zone-parent", "display_path": "Zoned"},
            headers=_auth(token),
        )

        from src.repositories import source_connections_repo

        repo = source_connections_repo()
        row = repo.get(conn_id)
        zones = [
            {
                "zone_item_id": "zone-1",
                "parent_scope_id": "drive:zone-parent",
                "drive_id": "d1",
                "name": "Legal",
                "display_path": "Zoned/Legal",
                "rel_path": "Legal",
                "collection_id": "col_zone_1",
                "detected_at": "2026-08-31T00:00:00+00:00",
                "status": "active",
            },
            {
                "zone_item_id": "zone-2",
                "parent_scope_id": "drive:zone-parent",
                "drive_id": "d1",
                "name": "Old",
                "display_path": "Zoned/Old",
                "rel_path": "Old",
                "collection_id": "col_zone_2",
                "detected_at": "2026-08-30T00:00:00+00:00",
                "status": "dissolved",
            },
        ]
        repo.update(conn_id, config={**row["config"], "acl_zones": zones})

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token))
        assert listed.status_code == 200, listed.text
        assert listed.json()["zones"] == [
            {
                "zone_item_id": "zone-1",
                "display_path": "Zoned/Legal",
                "collection_id": "col_zone_1",
                "status": "active",
                "detected_at": "2026-08-31T00:00:00+00:00",
            },
            {
                "zone_item_id": "zone-2",
                "display_path": "Zoned/Old",
                "collection_id": "col_zone_2",
                "status": "dissolved",
                "detected_at": "2026-08-30T00:00:00+00:00",
            },
        ]


class TestSubtreeOverride:
    """``ConfirmScopeBody.include_excluded_subtrees`` — the ``should_not``-only
    per-subtree "include anyway" escape hatch (2026-08-30 plan, Task 7, spec
    §3(b)/§1.2)."""

    @pytest.fixture(autouse=True)
    def _clear_guarantee_mode_env(self, monkeypatch):
        monkeypatch.delenv("AGNES_ACL_GUARANTEE_MODE", raising=False)

    def test_must_not_refuses_the_override(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACL_GUARANTEE_MODE", "must_not")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="override-must-not")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:override1",
                "display_path": "Override",
                "include_excluded_subtrees": True,
            },
            headers=_auth(token),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "must_not_forbids_subtree_override"

    def test_should_not_accepts_and_audits_the_override(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACL_GUARANTEE_MODE", "should_not")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="override-should-not")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:override2",
                "display_path": "Override",
                "include_excluded_subtrees": True,
            },
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["include_excluded_subtrees"] is True

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="sharepoint_acl.subtree_override", limit=50)
        assert len(rows) == 1
        assert _audit_params(rows[0])["source_scope_id"] == "drive:override2"

    def test_reconfirming_an_active_override_does_not_re_audit(self, seeded_app, monkeypatch):
        """Only the FALSE -> TRUE transition is audited — a re-confirm that
        resends an already-active override must not spam the audit log."""
        monkeypatch.setenv("AGNES_ACL_GUARANTEE_MODE", "should_not")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="override-idempotent")
        body = {
            "source_scope_id": "drive:override3",
            "display_path": "Override",
            "include_excluded_subtrees": True,
        }
        c.post(f"{BASE}/{conn_id}/scopes", json=body, headers=_auth(token))
        r = c.post(f"{BASE}/{conn_id}/scopes", json=body, headers=_auth(token))
        assert r.status_code == 201, r.text

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="sharepoint_acl.subtree_override", limit=50)
        assert len(rows) == 1

    def test_default_false_never_triggers_the_guard(self, seeded_app, monkeypatch):
        """A plain confirm (no override requested) must succeed under
        must_not too — the guard only fires on an explicit True."""
        monkeypatch.setenv("AGNES_ACL_GUARANTEE_MODE", "must_not")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="override-default-false")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:override4", "display_path": "Plain"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["include_excluded_subtrees"] is False


# ---------------------------------------------------------------------------
# SharePoint ACL mirroring (2026-08-30 plan, Task 8) — per-scope
# audience-class mapping (wizard data): ConfirmScopeBody.audience_classes,
# _scope_out's audience_classes/tiered, and src.audience_classes' runtime
# read path.
# ---------------------------------------------------------------------------


class TestAudienceClasses:
    """``ConfirmScopeBody.audience_classes`` / ``_scope_out``'s
    ``audience_classes``+``tiered`` fields (spec §4.1-4.3)."""

    def test_round_trip_preserves_order_and_sets_tiered(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="aud-conn")
        full = c.post("/api/admin/groups", json={"name": "aud-full"}, headers=_auth(token)).json()["id"]
        redacted = c.post("/api/admin/groups", json={"name": "aud-redacted"}, headers=_auth(token)).json()["id"]

        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:aud1",
                "display_path": "Aud",
                "audience_classes": [
                    {"name": "full", "group_ids": [full]},
                    {"name": "redacted", "group_ids": [redacted]},
                ],
            },
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["audience_classes"] == [
            {"name": "full", "group_ids": [full]},
            {"name": "redacted", "group_ids": [redacted]},
        ]
        assert r.json()["tiered"] is True

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"][0]
        assert listed["audience_classes"] == r.json()["audience_classes"]
        assert listed["tiered"] is True

    def test_defaults_to_empty_and_not_tiered(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="aud-default")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:aud2", "display_path": "Aud"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["audience_classes"] == []
        assert r.json()["tiered"] is False

    def test_omitting_leaves_existing_mapping_untouched(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="aud-omit")
        gid = c.post("/api/admin/groups", json={"name": "aud-omit-group"}, headers=_auth(token)).json()["id"]
        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:aud3",
                "display_path": "Aud",
                "audience_classes": [{"name": "full", "group_ids": [gid]}],
            },
            headers=_auth(token),
        )
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:aud3", "display_path": "Renamed"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["audience_classes"] == [{"name": "full", "group_ids": [gid]}]
        assert r.json()["tiered"] is True
        assert r.json()["display_path"] == "Renamed"

    def test_empty_list_clears_the_mapping(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="aud-clear")
        gid = c.post("/api/admin/groups", json={"name": "aud-clear-group"}, headers=_auth(token)).json()["id"]
        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:aud4",
                "display_path": "Aud",
                "audience_classes": [{"name": "full", "group_ids": [gid]}],
            },
            headers=_auth(token),
        )
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:aud4", "display_path": "Aud", "audience_classes": []},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["audience_classes"] == []
        assert r.json()["tiered"] is False

    def test_unknown_group_id_in_audience_class_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="aud-bad-group")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:aud5",
                "display_path": "Aud",
                "audience_classes": [{"name": "full", "group_ids": ["does-not-exist"]}],
            },
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "invalid_group_id"

    def test_duplicate_class_names_rejected(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="aud-dup")
        gid = c.post("/api/admin/groups", json={"name": "aud-dup-group"}, headers=_auth(token)).json()["id"]
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:aud6",
                "display_path": "Aud",
                "audience_classes": [
                    {"name": "full", "group_ids": [gid]},
                    {"name": "full", "group_ids": []},
                ],
            },
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "duplicate_audience_class"

    def test_bad_class_name_pattern_is_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="aud-bad-name")
        r = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:aud7",
                "display_path": "Aud",
                "audience_classes": [{"name": "Full Detail!", "group_ids": []}],
            },
            headers=_auth(token),
        )
        assert r.status_code == 422, r.text


class TestAudienceClassMap:
    """``src.audience_classes`` — Slice 4a's runtime read path (Task 8),
    consumed by Tasks 9-11. Exercised here (not ``test_audience_classes.py``,
    which Task 9 owns) because it reads back the exact wizard-persisted shape
    this file's other tests write through the API."""

    def test_reflects_stored_scopes_in_privilege_order(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="aud-map-conn")
        full = c.post("/api/admin/groups", json={"name": "aud-map-full"}, headers=_auth(token)).json()["id"]
        redacted = c.post("/api/admin/groups", json={"name": "aud-map-redacted"}, headers=_auth(token)).json()["id"]
        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "drive:aud-map1",
                "display_path": "Map",
                "audience_classes": [
                    {"name": "full", "group_ids": [full]},
                    {"name": "redacted", "group_ids": [redacted]},
                ],
            },
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text
        collection_id = confirmed.json()["collection_id"]

        from src.audience_classes import audience_class_map, tiered_collection_ids

        mapping = audience_class_map()
        assert mapping[collection_id] == [
            ("full", frozenset({full})),
            ("redacted", frozenset({redacted})),
        ]
        assert collection_id in tiered_collection_ids()

    def test_non_tiered_scope_is_absent(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="aud-map-plain")
        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "drive:aud-map2", "display_path": "Plain"},
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text
        collection_id = confirmed.json()["collection_id"]

        from src.audience_classes import audience_class_map, tiered_collection_ids

        assert collection_id not in audience_class_map()
        assert collection_id not in tiered_collection_ids()


def _install_item_resolver(monkeypatch, items: dict, *, drive_id: str = "drv1"):
    """Mock the Graph token exchange plus ``/drives/{drive_id}/root:/{path}``
    item-by-path lookups for :func:`connectors.sharepoint.graph_client.
    get_item_by_path`. ``items`` maps a folder path (as the admin would type
    it) to either an item dict (200) or an int HTTP status (403/404/500...);
    a path absent from ``items`` answers 404."""
    from connectors.sharepoint import graph_client as gc

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "tok-bulk"})
        prefix = f"/v1.0/drives/{drive_id}/root:/"
        assert request.url.path.startswith(prefix), request.url.path
        path = request.url.path[len(prefix) :]
        entry = items.get(path)
        if entry is None:
            return httpx.Response(404, json={"error": {"code": "itemNotFound"}})
        if isinstance(entry, int):
            return httpx.Response(entry, json={"error": {"code": "x"}})
        return httpx.Response(200, json=entry)

    monkeypatch.setattr(
        gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
    )


def _folder_item(item_id: str, name: str) -> dict:
    return {"id": item_id, "name": name, "folder": {"childCount": 0}}


class TestBulkScopeAdd:
    """``POST …/scopes/bulk`` — resolve many admin-typed folder paths to
    Graph items and confirm one scope each, in a single call (the fast path
    for splitting a large SharePoint site across several connections)."""

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(
            f"{BASE}/nope/scopes/bulk",
            json={"paths": ["A"], "drive_id": "drv1"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403

    def test_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].post(
            f"{BASE}/does-not-exist/scopes/bulk",
            json={"paths": ["A"], "drive_id": "drv1"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 404

    def test_creates_a_scope_per_resolved_path(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(
            monkeypatch,
            {"Folder A": _folder_item("item-a", "Folder A"), "Folder B/Sub": _folder_item("item-b", "Sub")},
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-create")

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A", "Folder B/Sub"], "drive_id": "drv1"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["skipped"] == []
        assert body["failed"] == []
        assert len(body["created"]) == 2
        for entry in body["created"]:
            assert entry["access_mode"] == "manual"
            assert entry["drive_id"] == "drv1"
            assert entry["include_excluded_subtrees"] is False

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"]
        assert {i["source_scope_id"] for i in listed} == {"item-a", "item-b"}
        # each path minted its own collection
        assert len({i["collection_id"] for i in listed}) == 2

    def test_skips_a_path_already_present(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(monkeypatch, {"Folder A": _folder_item("item-a", "Folder A")})
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-skip")

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "item-a", "display_path": "Folder A", "drive_id": "drv1"},
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A"], "drive_id": "drv1"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] == []
        assert body["skipped"] == [{"path": "Folder A", "source_scope_id": "item-a", "reason": "already_present"}]
        assert body["failed"] == []

    def test_unknown_path_is_reported_failed_not_found(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(monkeypatch, {"Folder A": _folder_item("item-a", "Folder A")})
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-notfound")

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A", "Ghost Folder"], "drive_id": "drv1"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["created"]) == 1
        assert body["failed"] == [{"path": "Ghost Folder", "reason": "not_found"}]

    def test_forbidden_path_is_reported_failed_forbidden(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(monkeypatch, {"Locked Folder": 403})
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-forbidden")

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Locked Folder"], "drive_id": "drv1"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["failed"] == [{"path": "Locked Folder", "reason": "forbidden"}]

    def test_upstream_outage_aborts_remaining_paths_but_keeps_already_created(self, seeded_app, monkeypatch):
        """A non-403/404 Graph failure (network fault, 5xx, ...) is not a
        per-path fact — it means the whole call is broken and aborts the
        rest of the batch with a typed 502, but whatever was already
        resolved and created before that point is still persisted."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(
            monkeypatch,
            {"Folder A": _folder_item("item-a", "Folder A"), "Folder B": 500},
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-outage")

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A", "Folder B", "Folder C"], "drive_id": "drv1"},
            headers=_auth(token),
        )
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error"] == "sharepoint_graph_error"

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"]
        assert [i["source_scope_id"] for i in listed] == ["item-a"]

    def test_drive_id_required_without_an_existing_scope(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-no-drive")

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A"]},
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "drive_id_required"

    def test_drive_id_inferred_from_an_existing_scope(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(monkeypatch, {"Folder A": _folder_item("item-a", "Folder A")}, drive_id="drive-known")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-infer-drive")

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "prior", "display_path": "Prior", "drive_id": "drive-known"},
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A"]},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["created"]) == 1

    def test_malformed_drive_id_is_422(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-bad-drive")

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A"], "drive_id": "not/a-valid-id"},
            headers=_auth(token),
        )
        assert r.status_code == 422, r.text

    def test_empty_paths_is_422(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-empty-paths")

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["   "], "drive_id": "drv1"},
            headers=_auth(token),
        )
        assert r.status_code == 422, r.text

    def test_writes_an_audit_row(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(monkeypatch, {"Folder A": _folder_item("item-a", "Folder A")})
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-audit")

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A"], "drive_id": "drv1"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="sharepoint_connection.scope_bulk_add", limit=10)
        assert len(rows) == 1
        params = _audit_params(rows[0])
        assert params == {"requested": 1, "created": 1, "skipped": 0, "failed": 0}

    def test_readopts_a_tombstoned_collection_instead_of_minting_a_duplicate(self, seeded_app, monkeypatch):
        """Tick -> untick -> bulk re-add of the same folder must re-adopt the
        SAME collection (never fork a slug-suffixed duplicate) and clear the
        tombstone — the same contract a singular re-confirm gets
        (TestUntickRetickLifecycle)."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(monkeypatch, {"Folder A": _folder_item("item-a", "Folder A")})
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-readopt")

        first = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "item-a", "display_path": "Folder A", "drive_id": "drv1"},
            headers=_auth(token),
        )
        assert first.status_code == 201, first.text
        collection_id = first.json()["collection_id"]

        untick = c.delete(f"{BASE}/{conn_id}/scopes", params={"source_scope_id": "item-a"}, headers=_auth(token))
        assert untick.status_code == 200, untick.text

        detail = c.get(f"/api/admin/source-connections/{conn_id}", headers=_auth(token)).json()
        assert "item-a" in (detail["config"].get("retired_scope_collections") or {})

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A"], "drive_id": "drv1"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["created"]) == 1
        assert r.json()["created"][0]["collection_id"] == collection_id

        detail = c.get(f"/api/admin/source-connections/{conn_id}", headers=_auth(token)).json()
        assert "item-a" not in (detail["config"].get("retired_scope_collections") or {})

    def test_collection_name_mints_one_shared_collection_for_every_created_path(self, seeded_app, monkeypatch):
        """``collection: {"name": ...}`` mints ONE new collection and routes
        every scope THIS call creates into it — the split-a-big-site fix:
        without it, every path forks its own collection (see
        ``test_creates_a_scope_per_resolved_path`` above)."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(
            monkeypatch,
            {"Folder A": _folder_item("item-a", "Folder A"), "Folder B/Sub": _folder_item("item-b", "Sub")},
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-shared-name")

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={
                "paths": ["Folder A", "Folder B/Sub"],
                "drive_id": "drv1",
                "collection": {"name": "One Big Site"},
            },
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["created"]) == 2
        collection_ids = {e["collection_id"] for e in body["created"]}
        assert len(collection_ids) == 1

        coll = c.get(f"/api/collections/{next(iter(collection_ids))}", headers=_auth(token))
        assert coll.status_code == 200
        assert coll.json()["name"] == "One Big Site"

    def test_collection_id_routes_to_an_existing_collection(self, seeded_app, monkeypatch):
        """``collection_id`` reuses an existing, live collection instead of
        minting one — the option a SECOND bulk-add call (a different
        connection in the split, or later paths on the same one) uses to
        keep growing the SAME site collection."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-shared-id")

        from src.repositories import file_corpora_repo

        existing_id = file_corpora_repo().create(
            name="Pre-existing Site", slug="pre-existing-site-bulk", description=None, created_by="admin"
        )

        _install_item_resolver(monkeypatch, {"Folder A": _folder_item("item-a", "Folder A")})
        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A"], "drive_id": "drv1", "collection_id": existing_id},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["created"][0]["collection_id"] == existing_id

    def test_collection_id_unknown_is_404(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-shared-404")

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A"], "drive_id": "drv1", "collection_id": "col_doesnotexist"},
            headers=_auth(token),
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "collection_not_found"

    def test_collection_id_and_collection_are_mutually_exclusive(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-shared-both")

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={
                "paths": ["Folder A"],
                "drive_id": "drv1",
                "collection_id": "col_x",
                "collection": {"name": "Y"},
            },
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "both_collection_id_and_collection"

    def test_a_path_already_present_keeps_its_own_collection_not_the_shared_target(self, seeded_app, monkeypatch):
        """A ``skipped`` path (already a scope on this connection) must keep
        whatever collection it already owns — the shared target only ever
        applies to scopes THIS call newly creates."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        _install_item_resolver(monkeypatch, {"Folder A": _folder_item("item-a", "Folder A")})
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="bulk-shared-skip-keeps-own")

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "item-a", "display_path": "Folder A", "drive_id": "drv1"},
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text
        own_collection_id = confirmed.json()["collection_id"]

        r = c.post(
            f"{BASE}/{conn_id}/scopes/bulk",
            json={"paths": ["Folder A"], "drive_id": "drv1", "collection": {"name": "Shared, not for item-a"}},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["created"] == []
        assert r.json()["skipped"] == [{"path": "Folder A", "source_scope_id": "item-a", "reason": "already_present"}]

        listed = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"]
        assert listed[0]["collection_id"] == own_collection_id


class TestConnectionClone:
    """``POST …/clone`` — a sibling SharePoint connection wired to the same
    credential material, no scopes, feeding the same split-a-large-site
    workflow as bulk scope-add above."""

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(
            f"{BASE}/nope/clone",
            json={"name": "x"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403

    def test_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].post(
            f"{BASE}/does-not-exist/clone",
            json={"name": "x"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 404

    def test_clone_copies_identity_and_starts_with_zero_scopes(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="clone-source", tenant_id="tenant-x", client_id="client-y")

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "item-a", "display_path": "Folder A"},
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text

        r = c.post(f"{BASE}/{conn_id}/clone", json={"name": "clone-target"}, headers=_auth(token))
        assert r.status_code == 201, r.text
        new_id = r.json()["id"]
        assert new_id != conn_id

        detail = c.get(f"/api/admin/source-connections/{new_id}", headers=_auth(token)).json()
        assert detail["source_type"] == "sharepoint"
        assert detail["config"]["tenant_id"] == "tenant-x"
        assert detail["config"]["client_id"] == "client-y"
        assert "scopes" not in detail["config"]

        listed = c.get(f"{BASE}/{new_id}/scopes", headers=_auth(token)).json()["items"]
        assert listed == []

    def test_clone_keeps_manual_sites_for_a_sites_selected_connection(self, seeded_app, monkeypatch):
        """Under Sites.Selected, `/sites` enumeration 403s and the ONLY way to
        reach a granted site is a bookmarked `manual_sites` entry
        (get_site_by_path) — a clone that lost this could not resolve the
        site it exists to split, at all. `extraction` (dispatch bookkeeping)
        stays excluded: a fresh clone has never run."""
        from connectors.sharepoint import graph_client as gc

        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok-manual"})
            assert request.url.path == "/v1.0/sites/contoso.sharepoint.com:/sites/ProjectHub"
            return httpx.Response(
                200, json={"id": "s-manual", "displayName": "Project Hub", "webUrl": "https://contoso/x"}
            )

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="clone-manual-sites-source")

        add_site = c.post(
            f"{BASE}/{conn_id}/manual-sites",
            json={"site_url": "https://contoso.sharepoint.com/sites/ProjectHub"},
            headers=_auth(token),
        )
        assert add_site.status_code == 201, add_site.text

        confirmed = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={"source_scope_id": "item-manual", "display_path": "Folder A"},
            headers=_auth(token),
        )
        assert confirmed.status_code == 201, confirmed.text

        r = c.post(f"{BASE}/{conn_id}/clone", json={"name": "clone-manual-sites-target"}, headers=_auth(token))
        assert r.status_code == 201, r.text
        new_id = r.json()["id"]

        detail = c.get(f"/api/admin/source-connections/{new_id}", headers=_auth(token)).json()
        assert detail["config"]["manual_sites"] == [
            {"id": "s-manual", "name": "Project Hub", "web_url": "https://contoso/x"}
        ]
        assert "scopes" not in detail["config"]
        assert "extraction" not in detail["config"]

    def test_clone_copies_a_vault_secret_so_the_clone_resolves_without_reupload(self, seeded_app, monkeypatch):
        """The clone's whole point is a working sibling with zero scopes —
        when the source's certificate lives in its OWN vault slot (rather
        than a deployment env var), a clone with no row of its own could
        never resolve settings and every Graph call 409ed
        ``sharepoint_cert_unresolved``. The fix copies the encrypted row
        verbatim (never decrypts) so the clone is immediately ready."""
        from cryptography.fernet import Fernet

        from app.secrets_vault import _reset_ephemeral_key_for_tests

        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        try:
            c = seeded_app["client"]
            token = seeded_app["admin_token"]
            conn_id = _create_connection(c, token, name="clone-vault-source")
            secret_resp = c.put(
                f"/api/admin/source-connections/{conn_id}/secret",
                json={"value": PEM},
                headers=_auth(token),
            )
            assert secret_resp.status_code == 204, secret_resp.text

            r = c.post(f"{BASE}/{conn_id}/clone", json={"name": "clone-vault-target"}, headers=_auth(token))
            assert r.status_code == 201, r.text
            new_id = r.json()["id"]
            assert r.json()["secret_copied"] is True

            from src.repositories import connection_secrets_repo

            secrets = connection_secrets_repo()
            assert secrets.has(conn_id) is True
            assert secrets.has(new_id) is True
            # Same plaintext, and — since a clone is created fresh, never
            # decrypted/re-encrypted along the way — the exact same ciphertext.
            assert secrets.get(new_id) == secrets.get(conn_id) == PEM

            from connectors.sharepoint.settings import resolve_sharepoint_settings

            cloned_row = c.get(f"/api/admin/source-connections/{new_id}", headers=_auth(token)).json()
            settings = resolve_sharepoint_settings({"id": new_id, "config": cloned_row["config"]})
            assert settings.credential_source == "vault"
            assert settings.private_key == PEM
        finally:
            _reset_ephemeral_key_for_tests()

    def test_clone_reports_no_secret_copied_when_source_uses_env_var(self, seeded_app):
        """A source whose certificate resolves from a deployment env var
        (``config.cert_private_key_env`` / no vault row at all) has nothing
        to copy — ``secret_copied: False`` is not an error, it just means
        every clone already resolves that same env var on its own."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="clone-no-vault-source")

        r = c.post(f"{BASE}/{conn_id}/clone", json={"name": "clone-no-vault-target"}, headers=_auth(token))
        assert r.status_code == 201, r.text
        new_id = r.json()["id"]
        assert r.json()["secret_copied"] is False

        from src.repositories import connection_secrets_repo

        assert connection_secrets_repo().has(new_id) is False

    def test_name_conflict_is_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="clone-dup-source")
        other = _create_connection(c, token, name="clone-dup-existing")

        r = c.post(f"{BASE}/{conn_id}/clone", json={"name": "clone-dup-existing"}, headers=_auth(token))
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "connection_name_exists"
        assert other  # keep the fixture referenced

    def test_writes_an_audit_row(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="clone-audit-source")

        r = c.post(f"{BASE}/{conn_id}/clone", json={"name": "clone-audit-target"}, headers=_auth(token))
        assert r.status_code == 201, r.text
        new_id = r.json()["id"]

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="sharepoint_connection.clone", limit=10)
        assert len(rows) == 1
        params = _audit_params(rows[0])
        assert params == {"source_connection_id": conn_id, "name": "clone-audit-target", "secret_copied": False}
        assert rows[0]["resource"] == f"source_connection:{new_id}"


def _split_folder(item_id: str, name: str, web_url: str | None = None) -> dict:
    return {
        "id": item_id,
        "name": name,
        "folder": {"childCount": 0},
        "webUrl": web_url or f"https://example.sharepoint.com/sites/s/Docs/{name}",
    }


def _split_file(item_id: str, name: str, web_url: str | None = None) -> dict:
    return {
        "id": item_id,
        "name": name,
        "file": {},
        "webUrl": web_url or f"https://example.sharepoint.com/sites/s/Docs/{name}",
    }


def _install_split_mock(monkeypatch, *, drive_id: str = "drv1", root_children: list, counts: dict | None = None):
    """Mock the Graph token exchange, drive-root children listing (with
    ``webUrl``) and the Search-based document count for the site-split
    planner (:func:`connectors.sharepoint.graph_client.
    list_root_children_with_url` / :func:`search_document_count`).
    ``counts`` maps a folder's ``webUrl`` to the total
    :func:`search_document_count` should answer for it; an omitted url
    answers 0 — the same "unreadable count still balances as 0" contract
    the endpoint itself documents."""
    from connectors.sharepoint import graph_client as gc

    counts = counts or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "tok-split"})
        if request.url.path == f"/v1.0/drives/{drive_id}/root/children":
            return httpx.Response(200, json={"value": root_children})
        if request.url.path == "/v1.0/search/query":
            body = json.loads(request.content)
            query = body["requests"][0]["query"]["queryString"]
            m = re.search(r'path:"([^"]+)"', query)
            web_url = m.group(1) if m else None
            return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": counts.get(web_url, 0)}]}]})
        raise AssertionError(f"unexpected sharepoint split mock path {request.url.path}")

    monkeypatch.setattr(
        gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
    )


def _confirm_scope_with_drive(client, token, conn_id, *, source_scope_id="seed", display_path="Seed", drive_id="drv1"):
    r = client.post(
        f"{BASE}/{conn_id}/scopes",
        json={"source_scope_id": source_scope_id, "display_path": display_path, "drive_id": drive_id},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text


class TestSplitPlan:
    """``GET …/split-plan`` — read-only preview of splitting a connection's
    site into N sibling connections."""

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/nope/split-plan?n=2", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/nope/split-plan?n=2")
        assert r.status_code == 401

    def test_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].get(f"{BASE}/does-not-exist/split-plan?n=2", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_drive_id_required_without_an_existing_scope(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-plan-no-drive")

        r = c.get(f"{BASE}/{conn_id}/split-plan?n=2", headers=_auth(token))
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "drive_id_required"

    def test_invalid_min_modified_is_400(self, seeded_app, monkeypatch):
        # Same status/error shape as `PATCH …/extraction/crawl-config`'s own
        # validation of this identical config key — see
        # `_validate_min_modified`'s docstring.
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-plan-bad-date")
        _confirm_scope_with_drive(c, token, conn_id)

        r = c.get(f"{BASE}/{conn_id}/split-plan?n=2&min_modified=not-a-date", headers=_auth(token))
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "invalid_min_modified"

    def test_n_out_of_range_is_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get(f"{BASE}/nope/split-plan?n=0", headers=_auth(token))
        assert r.status_code == 422, r.text
        r = c.get(f"{BASE}/nope/split-plan?n=51", headers=_auth(token))
        assert r.status_code == 422, r.text

    def test_packs_folders_and_reports_loose_files(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-plan-happy")
        _confirm_scope_with_drive(c, token, conn_id)

        root_children = [
            _split_folder("f-big", "Big"),
            _split_folder("f-small", "Small"),
            _split_file("file-1", "readme.txt"),
        ]
        counts = {
            "https://example.sharepoint.com/sites/s/Docs/Big": 100,
            "https://example.sharepoint.com/sites/s/Docs/Small": 10,
        }
        _install_split_mock(monkeypatch, root_children=root_children, counts=counts)

        r = c.get(f"{BASE}/{conn_id}/split-plan?n=2", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["drive_id"] == "drv1"
        assert body["loose_root_files"] == ["readme.txt"]
        assert sorted(body["folders"], key=lambda f: f["name"]) == [
            {"name": "Big", "documents": 100},
            {"name": "Small", "documents": 10},
        ]
        assert body["total_documents"] == 110
        assert len(body["groups"]) == 2
        # The bigger folder and the smaller one must not share a group —
        # greedy-by-largest-first puts each in its own bucket here.
        group_docs = sorted(g["documents"] for g in body["groups"])
        assert group_docs == [10, 100]
        assert body["groups"][0]["name"] == "split-plan-happy — part 1/2"
        assert body["groups"][1]["name"] == "split-plan-happy — part 2/2"
        # Public folder shape never leaks the Graph item id.
        for group in body["groups"]:
            for folder in group["folders"]:
                assert set(folder.keys()) == {"name", "documents"}

    def test_a_folder_whose_count_fails_is_still_assigned(self, seeded_app, monkeypatch):
        """search_document_count() never raises — a 500 from Graph Search
        degrades to documents=0, and the folder is still packed into a
        group, never dropped from the plan."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-plan-count-fails")
        _confirm_scope_with_drive(c, token, conn_id)

        from connectors.sharepoint import graph_client as gc

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/v2.0/token"):
                return httpx.Response(200, json={"access_token": "tok"})
            if request.url.path == "/v1.0/drives/drv1/root/children":
                return httpx.Response(200, json={"value": [_split_folder("f1", "Flaky")]})
            if request.url.path == "/v1.0/search/query":
                return httpx.Response(500, text="boom")
            raise AssertionError(request.url.path)

        monkeypatch.setattr(
            gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        )

        r = c.get(f"{BASE}/{conn_id}/split-plan?n=1", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["folders"] == [{"name": "Flaky", "documents": 0}]
        assert len(body["groups"][0]["folders"]) == 1

    def test_collection_defaults_to_the_source_single_existing_scope(self, seeded_app, monkeypatch):
        """The default shared-collection resolution reuses the source's OWN
        collection when it has exactly one confirmed scope carrying a
        `collection_id` — the common "one root scope, not yet split" shape
        `_confirm_scope_with_drive` produces."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-plan-collection-default")
        _confirm_scope_with_drive(c, token, conn_id)
        scopes = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"]
        existing_collection_id = scopes[0]["collection_id"]

        _install_split_mock(monkeypatch, root_children=[_split_folder("f1", "A")], counts={})

        r = c.get(f"{BASE}/{conn_id}/split-plan?n=1", headers=_auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["collection"]["id"] == existing_collection_id

    def test_collection_defaults_to_a_new_name_when_no_single_scope(self, seeded_app, monkeypatch):
        """No confirmed scope at all (nothing to reuse) falls back to
        minting one collection named after the source connection — not yet
        minted during a read-only preview, so `id`/`slug` stay `None`."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-plan-collection-noscope")

        _install_split_mock(monkeypatch, root_children=[_split_folder("f1", "A")], counts={})

        r = c.get(f"{BASE}/{conn_id}/split-plan?n=1&drive_id=drv1", headers=_auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["collection"] == {"id": None, "name": "split-plan-collection-noscope", "slug": None}

    def test_per_folder_collections_reports_null_collection(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-plan-per-folder")
        _confirm_scope_with_drive(c, token, conn_id)
        _install_split_mock(monkeypatch, root_children=[_split_folder("f1", "A")], counts={})

        r = c.get(f"{BASE}/{conn_id}/split-plan?n=1&per_folder_collections=true", headers=_auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["collection"] is None

    def test_explicit_target_collection_id_is_previewed(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-plan-explicit-target")
        _confirm_scope_with_drive(c, token, conn_id)
        scopes = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"]
        target_id = scopes[0]["collection_id"]
        _install_split_mock(monkeypatch, root_children=[_split_folder("f1", "A")], counts={})

        r = c.get(
            f"{BASE}/{conn_id}/split-plan?n=1&target_collection_id={target_id}",
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["collection"]["id"] == target_id

    def test_unknown_target_collection_id_is_404(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-plan-404-target")
        _confirm_scope_with_drive(c, token, conn_id)

        r = c.get(
            f"{BASE}/{conn_id}/split-plan?n=1&target_collection_id=does-not-exist",
            headers=_auth(token),
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "collection_not_found"

    def test_both_target_fields_is_400(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-plan-both-target")
        _confirm_scope_with_drive(c, token, conn_id)

        r = c.get(
            f"{BASE}/{conn_id}/split-plan?n=1&target_collection_id=x&target_name=y",
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "both_target_collection_id_and_target"

    def test_per_folder_collections_and_target_is_400(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-plan-per-folder-and-target")
        _confirm_scope_with_drive(c, token, conn_id)

        r = c.get(
            f"{BASE}/{conn_id}/split-plan?n=1&per_folder_collections=true&target_name=y",
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "per_folder_collections_and_target"


class TestSplitApply:
    """``POST …/splits`` — create N sibling connections from a split plan."""

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(f"{BASE}/nope/splits", json={"n": 2}, headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_404_for_unknown_connection(self, seeded_app):
        r = seeded_app["client"].post(
            f"{BASE}/does-not-exist/splits", json={"n": 2}, headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 404

    def test_invalid_retry_mode_is_422(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-bad-retry")
        _confirm_scope_with_drive(c, token, conn_id)

        r = c.post(
            f"{BASE}/{conn_id}/splits",
            json={"n": 1, "retry_mode": "not-a-mode"},
            headers=_auth(token),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "invalid_retry_mode"

    def test_creates_n_clones_with_scopes_and_config(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-happy", tenant_id="tenant-z", client_id="client-z")
        _confirm_scope_with_drive(c, token, conn_id, source_scope_id="seed", display_path="Seed")

        root_children = [
            _split_folder("f-big", "Big"),
            _split_folder("f-small", "Small"),
            _split_file("file-1", "readme.txt"),
        ]
        counts = {
            "https://example.sharepoint.com/sites/s/Docs/Big": 100,
            "https://example.sharepoint.com/sites/s/Docs/Small": 10,
        }
        _install_split_mock(monkeypatch, root_children=root_children, counts=counts)

        r = c.post(
            f"{BASE}/{conn_id}/splits",
            json={"n": 2, "min_modified": "2023-12-31", "transport": "batch", "retry_mode": "off"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        created = body["connections"]
        assert len(created) == 2
        names = {c_["name"] for c_ in created}
        assert names == {"split-apply-happy — part 1/2", "split-apply-happy — part 2/2"}

        total_folders = sum(len(c_["folders"]) for c_ in created)
        assert total_folders == 2  # Big + Small, split across the two clones

        for entry in created:
            detail = c.get(f"/api/admin/source-connections/{entry['id']}", headers=_auth(token)).json()
            assert detail["config"]["tenant_id"] == "tenant-z"
            assert detail["config"]["client_id"] == "client-z"
            assert detail["config"]["extraction"]["crawl"]["min_modified"] == "2023-12-31"
            assert detail["config"]["extraction"]["facts"] == {"transport": "batch", "retry_mode": "off"}
            # Never the source's own seed scope — each clone gets ONLY its
            # own group's folders.
            scope_paths = {s["display_path"] for s in detail["config"]["scopes"]}
            assert "Seed" not in scope_paths
            assert scope_paths <= {"Big", "Small"}
            for scope in detail["config"]["scopes"]:
                assert scope["drive_id"] == "drv1"
                assert scope["access_mode"] == "manual"

    def test_refuses_when_a_repeat_split_would_collide(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-repeat")
        _confirm_scope_with_drive(c, token, conn_id)

        root_children = [_split_folder("f1", "OnlyFolder")]
        _install_split_mock(monkeypatch, root_children=root_children, counts={})

        first = c.post(f"{BASE}/{conn_id}/splits", json={"n": 1}, headers=_auth(token))
        assert first.status_code == 201, first.text

        second = c.post(f"{BASE}/{conn_id}/splits", json={"n": 1}, headers=_auth(token))
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "split_exists"

    def test_start_enqueues_a_crawl_per_clone(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-start")
        _confirm_scope_with_drive(c, token, conn_id)

        root_children = [_split_folder("f1", "A"), _split_folder("f2", "B")]
        _install_split_mock(monkeypatch, root_children=root_children, counts={})

        r = c.post(f"{BASE}/{conn_id}/splits", json={"n": 2, "start": True}, headers=_auth(token))
        assert r.status_code == 201, r.text
        created = r.json()["connections"]

        from src.repositories import jobs_repo

        jobs = jobs_repo().list(kind="corpus-extraction", limit=50)
        for entry in created:
            matching = [j for j in jobs if (j.get("payload_json") or {}).get("connection_id") == entry["id"]]
            assert len(matching) == 1, f"expected a corpus-extraction job for {entry['id']}"

    def test_writes_an_audit_row(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-audit")
        _confirm_scope_with_drive(c, token, conn_id)

        root_children = [_split_folder("f1", "A")]
        _install_split_mock(monkeypatch, root_children=root_children, counts={})

        r = c.post(f"{BASE}/{conn_id}/splits", json={"n": 1}, headers=_auth(token))
        assert r.status_code == 201, r.text
        created_ids = [entry["id"] for entry in r.json()["connections"]]

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="sharepoint_connection.split_apply", limit=10)
        assert len(rows) == 1
        params = _audit_params(rows[0])
        assert params["n"] == 1
        assert params["created_ids"] == created_ids
        assert rows[0]["resource"] == f"source_connection:{conn_id}"

    def test_default_shares_one_collection_across_every_part_reusing_the_source_single_scope(
        self, seeded_app, monkeypatch
    ):
        """A site of 400 folders must not become 400 collections nobody has
        a grant to — the DEFAULT routes every part's scopes to ONE shared
        collection. When the source has exactly one confirmed scope
        carrying a `collection_id` (the common "one root scope" shape),
        that IS the shared collection — reused, not re-minted."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-shared-default")
        _confirm_scope_with_drive(c, token, conn_id)
        scopes = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"]
        existing_collection_id = scopes[0]["collection_id"]

        root_children = [_split_folder("f-big", "Big"), _split_folder("f-small", "Small")]
        counts = {
            "https://example.sharepoint.com/sites/s/Docs/Big": 100,
            "https://example.sharepoint.com/sites/s/Docs/Small": 10,
        }
        _install_split_mock(monkeypatch, root_children=root_children, counts=counts)

        r = c.post(f"{BASE}/{conn_id}/splits", json={"n": 2}, headers=_auth(token))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["collection"]["id"] == existing_collection_id

        all_collection_ids = set()
        for entry in body["connections"]:
            detail = c.get(f"/api/admin/source-connections/{entry['id']}", headers=_auth(token)).json()
            for scope in detail["config"]["scopes"]:
                all_collection_ids.add(scope["collection_id"])
        assert all_collection_ids == {existing_collection_id}

    def test_default_mints_one_new_collection_when_source_has_no_single_scope(self, seeded_app, monkeypatch):
        """`apply_split` always infers its drive from an EXISTING scope
        (`_compute_split_plan(..., drive_id=None)`), so the "no single
        scope" case that falls through to minting a NEW collection is not
        "zero scopes" (that 400s on `drive_id_required` before reaching
        collection resolution at all) but "more than one" — two confirmed
        scopes here, each with its OWN `collection_id`, so neither is
        reused; a fresh, source-named collection is minted for the split
        instead."""
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-mint-default")
        _confirm_scope_with_drive(c, token, conn_id, source_scope_id="seed-1", display_path="Seed1")
        _confirm_scope_with_drive(c, token, conn_id, source_scope_id="seed-2", display_path="Seed2")

        root_children = [_split_folder("f-big", "Big"), _split_folder("f-small", "Small")]
        _install_split_mock(monkeypatch, root_children=root_children, counts={})

        r = c.post(f"{BASE}/{conn_id}/splits", json={"n": 2}, headers=_auth(token))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["collection"] == {
            "id": body["collection"]["id"],
            "name": "split-apply-mint-default",
            "slug": body["collection"]["slug"],
        }
        minted_id = body["collection"]["id"]
        assert minted_id is not None

        scopes = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"]
        existing_ids = {s["collection_id"] for s in scopes}
        assert minted_id not in existing_ids  # a genuinely NEW collection, not one of the source's own two

        for entry in body["connections"]:
            detail = c.get(f"/api/admin/source-connections/{entry['id']}", headers=_auth(token)).json()
            assert all(scope["collection_id"] == minted_id for scope in detail["config"]["scopes"])

    def test_explicit_target_collection_id_routes_every_part(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-explicit-target-id")
        _confirm_scope_with_drive(c, token, conn_id)
        scopes = c.get(f"{BASE}/{conn_id}/scopes", headers=_auth(token)).json()["items"]
        target_id = scopes[0]["collection_id"]

        root_children = [_split_folder("f1", "A"), _split_folder("f2", "B")]
        _install_split_mock(monkeypatch, root_children=root_children, counts={})

        r = c.post(
            f"{BASE}/{conn_id}/splits",
            json={"n": 2, "target_collection_id": target_id},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["collection"]["id"] == target_id
        for entry in body["connections"]:
            detail = c.get(f"/api/admin/source-connections/{entry['id']}", headers=_auth(token)).json()
            assert all(scope["collection_id"] == target_id for scope in detail["config"]["scopes"])

    def test_explicit_target_name_mints_exactly_one_collection_for_the_whole_split(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-explicit-target-name")
        _confirm_scope_with_drive(c, token, conn_id)

        root_children = [_split_folder("f1", "A"), _split_folder("f2", "B")]
        _install_split_mock(monkeypatch, root_children=root_children, counts={})

        r = c.post(
            f"{BASE}/{conn_id}/splits",
            json={"n": 2, "target": {"name": "Whole Site"}},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["collection"]["name"] == "Whole Site"
        minted_id = body["collection"]["id"]
        assert minted_id is not None

        for entry in body["connections"]:
            detail = c.get(f"/api/admin/source-connections/{entry['id']}", headers=_auth(token)).json()
            assert all(scope["collection_id"] == minted_id for scope in detail["config"]["scopes"])

    def test_per_folder_collections_restores_the_old_one_per_folder_behavior(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-per-folder")
        _confirm_scope_with_drive(c, token, conn_id)

        root_children = [_split_folder("f1", "A"), _split_folder("f2", "B")]
        _install_split_mock(monkeypatch, root_children=root_children, counts={})

        r = c.post(
            f"{BASE}/{conn_id}/splits",
            json={"n": 2, "per_folder_collections": True},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["collection"] is None

        all_collection_ids = set()
        for entry in body["connections"]:
            detail = c.get(f"/api/admin/source-connections/{entry['id']}", headers=_auth(token)).json()
            for scope in detail["config"]["scopes"]:
                all_collection_ids.add(scope["collection_id"])
        # Two folders (A, B), each split into its OWN part (n=2) — two
        # distinct, freshly minted collections, never shared.
        assert len(all_collection_ids) == 2

    def test_both_target_fields_is_400(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-both-target")
        _confirm_scope_with_drive(c, token, conn_id)

        r = c.post(
            f"{BASE}/{conn_id}/splits",
            json={"n": 1, "target_collection_id": "x", "target": {"name": "y"}},
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "both_target_collection_id_and_target"

    def test_per_folder_collections_and_target_is_400(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-per-folder-and-target")
        _confirm_scope_with_drive(c, token, conn_id)

        r = c.post(
            f"{BASE}/{conn_id}/splits",
            json={"n": 1, "per_folder_collections": True, "target": {"name": "y"}},
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "per_folder_collections_and_target"

    def test_unknown_target_collection_id_is_404(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-404-target")
        _confirm_scope_with_drive(c, token, conn_id)

        r = c.post(
            f"{BASE}/{conn_id}/splits",
            json={"n": 1, "target_collection_id": "does-not-exist"},
            headers=_auth(token),
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "collection_not_found"

    def test_records_split_lineage_on_every_part(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="split-apply-lineage")
        _confirm_scope_with_drive(c, token, conn_id)

        root_children = [_split_folder("f1", "A"), _split_folder("f2", "B")]
        _install_split_mock(monkeypatch, root_children=root_children, counts={})

        r = c.post(f"{BASE}/{conn_id}/splits", json={"n": 2}, headers=_auth(token))
        assert r.status_code == 201, r.text
        created = r.json()["connections"]
        assert len(created) == 2

        for part, entry in enumerate(created, start=1):
            detail = c.get(f"/api/admin/source-connections/{entry['id']}", headers=_auth(token)).json()
            split = detail["config"]["split"]
            assert split["parent_connection_id"] == conn_id
            assert split["part"] == part
            assert split["n"] == 2
            assert split["created_at"]


class TestFactsExtractionRefusalNamesTheSwitch:
    """The ``409 facts_extraction_disabled`` body names WHICH of the two
    switches is off in a machine-readable ``switch`` key (additive to the
    ``error``/``message`` pair) — the source card's "Extract facts now"
    button renders that key as its disabled reason, so the UI and the API
    can never disagree about what an admin has to flip."""

    FACTS_EXTRACT = "{base}/{cid}/facts-extract"

    def test_cost_switch_off_names_extraction_facts_enabled(self, seeded_app, monkeypatch):
        monkeypatch.setattr(
            "app.instance_config.get_value",
            _config_get_value({"sharepoint": {"enabled": True}, "facts": {"enabled": True}}),
        )
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="facts-switch-cost")
        r = c.post(self.FACTS_EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "facts_extraction_disabled"
        assert detail["switch"] == "extraction.facts.enabled"

    def test_surface_off_names_facts_enabled(self, seeded_app, monkeypatch):
        monkeypatch.setattr(
            "app.instance_config.get_value",
            _config_get_value({"sharepoint": {"enabled": True}, "extraction": {"facts": {"enabled": True}}}),
        )
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="facts-switch-surface")
        r = c.post(self.FACTS_EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "facts_extraction_disabled"
        assert detail["switch"] == "facts.enabled"


class TestDispatchBookkeepingKeepsSiblings:
    """`_record_extraction_dispatch` must merge `last_run_at`/`last_job_id`
    into `config.extraction`, never replace the sub-object — the per-
    connection overrides (`facts.*`, `crawl.min_modified`) live there too."""

    def test_trigger_keeps_facts_and_crawl_overrides(self):
        from app.api.admin_sharepoint import _record_extraction_dispatch

        written = {}

        class _Repo:
            def update(self, cid, config=None):
                written["config"] = config

        import app.api.admin_sharepoint as mod

        orig = mod.source_connections_repo
        mod.source_connections_repo = lambda: _Repo()
        try:
            row = {
                "id": "c1",
                "config": {
                    "tenant_id": "t",
                    "extraction": {
                        "facts": {"retry_mode": "off", "transport": "batch"},
                        "crawl": {"min_modified": "2023-12-31"},
                        "stop_requested_at": None,
                    },
                },
            }
            _record_extraction_dispatch(row, "job-1")
        finally:
            mod.source_connections_repo = orig

        ext = written["config"]["extraction"]
        assert ext["last_job_id"] == "job-1"
        assert ext["last_run_at"]
        assert ext["facts"] == {"retry_mode": "off", "transport": "batch"}
        assert ext["crawl"] == {"min_modified": "2023-12-31"}
        assert written["config"]["tenant_id"] == "t"


class TestFactsGraphCountsDoesNotBlockTheEventLoop:
    """Production incident, 2026-09-03: on a live instance with ~390
    collections and a busy Postgres, a SINGLE `GET .../facts-graph-counts`
    whose visibility CTE ran 250-316s made the whole app stop answering
    ANY request — including `/healthz`, which does zero I/O — for as long
    as that one query ran. `pg_cancel_backend`ing the one active statement
    fixed it immediately.

    `facts_graph_counts` is a plain `def` (not `async def`) specifically so
    FastAPI dispatches it to the anyio thread pool instead of the event
    loop (Tier-1 convention, `tests/test_event_loop_offload_guard.py`) —
    and so are its dependencies (`require_admin`, the router-level
    `_require_sharepoint_enabled`). This test proves that dispatch actually
    holds under a slow repo call, rather than just asserting the function
    is not a coroutine: a concurrent `/healthz` must answer in well under a
    second regardless of how long the OTHER request's DB call takes. If
    this test ever fails, the regression is a NEW blocking call reached
    from the dependency chain on the event loop thread, not in the
    endpoint's own body — the guard above only proves the entry points are
    synchronous, not that everything they transitively call stays off the
    loop.
    """

    def test_a_slow_repo_call_does_not_delay_a_concurrent_healthz(self, seeded_app, monkeypatch):
        import threading
        import time

        from src.repositories import source_connections_repo

        conn_id = "sp-evloop-perf"
        source_connections_repo().create(
            id=conn_id,
            name="Event Loop Perf Test",
            source_type="sharepoint",
            config={
                "tenant_id": "t1",
                "client_id": "c1",
                "scopes": [{"source_scope_id": "s1", "display_path": "A", "collection_id": "col_a"}],
            },
        )

        class _SlowFactsRepo:
            def approximate_counts_for_collections(self, corpus_ids):
                time.sleep(2)
                return {cid: {"facts": 0, "edges": 0} for cid in corpus_ids}

        # Patched where `facts_graph_counts` resolves it from — a fresh
        # `from src.repositories import facts_repo` on every call, so
        # patching the factory function itself is enough.
        monkeypatch.setattr("src.repositories.facts_repo", lambda: _SlowFactsRepo())

        client = seeded_app["client"]
        token = seeded_app["admin_token"]

        results: dict[str, tuple[int, float]] = {}
        start_barrier = threading.Barrier(2, timeout=5)

        def _slow_request():
            start_barrier.wait()
            t0 = time.monotonic()
            r = client.get(f"{BASE}/{conn_id}/facts-graph-counts", headers=_auth(token))
            results["slow"] = (r.status_code, time.monotonic() - t0)

        def _healthz_request():
            start_barrier.wait()
            time.sleep(0.2)  # let the slow request's DB call actually start first
            t0 = time.monotonic()
            r = client.get("/healthz")
            results["healthz"] = (r.status_code, time.monotonic() - t0)

        t_slow = threading.Thread(target=_slow_request)
        t_health = threading.Thread(target=_healthz_request)
        t_slow.start()
        t_health.start()
        t_slow.join(timeout=10)
        t_health.join(timeout=10)

        assert "slow" in results, "the slow request never completed"
        assert "healthz" in results, "the healthz request never completed"
        assert results["slow"][0] == 200, results["slow"]
        assert results["healthz"][0] == 200, results["healthz"]
        # The whole point: healthz must not queue up behind the slow
        # request's DB call. A generous ceiling (well under the slow
        # request's own 2s sleep) — if `facts_graph_counts` (or a
        # dependency) were blocking the event loop, healthz would take
        # close to 2s too, not ~0s.
        assert results["healthz"][1] < 1.0, (
            f"GET /healthz took {results['healthz'][1]:.2f}s while a slow "
            f"facts-graph-counts request was in flight — something in that "
            f"request's dependency chain is running on the event loop "
            f"instead of the thread pool"
        )

    def test_eight_concurrent_slow_requests_still_leave_healthz_responsive(self, seeded_app, monkeypatch):
        """The production trigger was not really "a single request" — the
        card fires one `facts-graph-counts` fetch PER SharePoint connection
        on page load (`_fetchSharepointGraphCounts` in
        app/web/static/js/admin/data_sources_page.js), so a live page with 8
        connections fires 8 concurrent slow requests at once. Each is
        individually well-dispatched (see the test above); this proves 8 of
        them AT ONCE still leave the thread pool (200 tokens,
        AGNES_THREADPOOL_SIZE) with headroom for an unrelated `/healthz`."""
        import threading
        import time

        from src.repositories import source_connections_repo

        conn_ids = []
        for i in range(8):
            cid = f"sp-evloop-perf-{i}"
            source_connections_repo().create(
                id=cid,
                name=f"Event Loop Perf Test {i}",
                source_type="sharepoint",
                config={
                    "tenant_id": "t1",
                    "client_id": "c1",
                    "scopes": [{"source_scope_id": "s1", "display_path": "A", "collection_id": f"col_{i}"}],
                },
            )
            conn_ids.append(cid)

        class _SlowFactsRepo:
            def approximate_counts_for_collections(self, corpus_ids):
                time.sleep(2)
                return {cid: {"facts": 0, "edges": 0} for cid in corpus_ids}

        monkeypatch.setattr("src.repositories.facts_repo", lambda: _SlowFactsRepo())

        client = seeded_app["client"]
        token = seeded_app["admin_token"]
        results: dict[str, tuple[int, float]] = {}
        start_barrier = threading.Barrier(9, timeout=5)

        def _slow_request(cid):
            start_barrier.wait()
            r = client.get(f"{BASE}/{cid}/facts-graph-counts", headers=_auth(token))
            results[cid] = (r.status_code, 0.0)

        def _healthz_request():
            start_barrier.wait()
            time.sleep(0.3)  # let the 8 slow requests' DB calls actually start first
            t0 = time.monotonic()
            r = client.get("/healthz")
            results["healthz"] = (r.status_code, time.monotonic() - t0)

        threads = [threading.Thread(target=_slow_request, args=(cid,)) for cid in conn_ids]
        threads.append(threading.Thread(target=_healthz_request))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert "healthz" in results, "the healthz request never completed"
        assert results["healthz"][0] == 200, results["healthz"]
        assert results["healthz"][1] < 1.0, (
            f"GET /healthz took {results['healthz'][1]:.2f}s with 8 concurrent slow "
            f"facts-graph-counts requests in flight"
        )
        for cid in conn_ids:
            assert results.get(cid, (None,))[0] == 200, f"{cid}: {results.get(cid)}"
