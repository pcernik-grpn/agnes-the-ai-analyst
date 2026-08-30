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
import pytest
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


class TestCorpusMap:
    def test_corpus_map_keys_are_producer_resolver_shaped(self, seeded_app):
        """Keys must be what the producer's corpus_for() resolver matches
        against crawler rows: the site display name, with the document-
        library segment DROPPED for folder scopes — never the raw scope id
        (which matches no row) and never display_path verbatim."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="map-conn")

        # Folder scope (item id): breadcrumb carries the library segment.
        r1 = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "01SO3DIHVJLOMDRMYCA5B37XU577X4KL57",
                "display_path": "Site One/Documents/Project Kemp",
            },
            headers=_auth(token),
        )
        # Site scope (composite id): bare site name is the key.
        r2 = c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "host.sharepoint.com,f0259dd4,aaac162f",
                "display_path": "Site Two",
            },
            headers=_auth(token),
        )

        mapping = c.get(f"{BASE}/{conn_id}/corpus-map", headers=_auth(token))
        assert mapping.status_code == 200
        assert mapping.json() == {
            "Site One/Project Kemp": r1.json()["collection_id"],
            "Site Two": r2.json()["collection_id"],
        }

    def test_corpus_map_ambiguous_scopes_are_409(self, seeded_app):
        """A site scope plus a drive scope of the same site collapse to the
        same key with different collections — a typed 409, never a
        best-guess map."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="map-ambiguous-conn")

        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "host.sharepoint.com,f0259dd4,aaac162f",
                "display_path": "Site One",
            },
            headers=_auth(token),
        )
        c.post(
            f"{BASE}/{conn_id}/scopes",
            json={
                "source_scope_id": "b!1J0l8L5qG0WbfGdg0c3LXy8WrKo60DdB",
                "display_path": "Site One / Documents",
            },
            headers=_auth(token),
        )

        mapping = c.get(f"{BASE}/{conn_id}/corpus-map", headers=_auth(token))
        assert mapping.status_code == 409, mapping.text
        assert mapping.json()["detail"]["error"] == "corpus_map_ambiguous"

    def test_corpus_map_empty_for_connection_with_no_scopes(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = _create_connection(c, token, name="empty-map-conn")
        mapping = c.get(f"{BASE}/{conn_id}/corpus-map", headers=_auth(token))
        assert mapping.status_code == 200
        assert mapping.json() == {}


# ---------------------------------------------------------------------------
# Extraction enqueue wiring (TCRD-226) — the admin trigger + the scheduled
# sweep. Neither test class launches a real producer subprocess; they cover
# the endpoints' OWN responsibilities: 404-before-work, the feature-usable
# gate, duplicate-run dedup, and the exact payload shape enqueued for
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
    "extraction": {
        "enabled": True,
        "producer": {"command": "python -m fake_producer"},
        "timeout_s": 60,
    }
}


class TestExtractionTrigger:
    """``POST /connections/{connection_id}/extract`` — admin-triggered
    one-off run of the existing ``corpus-extraction`` job kind."""

    EXTRACT = "{base}/{cid}/extract"

    @pytest.fixture(autouse=True)
    def _clear_extraction_env_var(self, monkeypatch):
        # AGNES_EXTRACTION_ENABLED wins over the mocked get_value config —
        # clear it so each test's fake config is what actually decides.
        monkeypatch.delenv("AGNES_EXTRACTION_ENABLED", raising=False)

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(
            self.EXTRACT.format(base=BASE, cid="nope"), headers=_auth(seeded_app["analyst_token"])
        )
        assert r.status_code == 403

    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].post(self.EXTRACT.format(base=BASE, cid="nope"))
        assert r.status_code == 401

    def test_404_for_unknown_connection_before_any_work(self, seeded_app, monkeypatch):
        """404 fires even with extraction fully disabled — connection
        existence is checked BEFORE the feature-usable gate."""
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        r = seeded_app["client"].post(
            self.EXTRACT.format(base=BASE, cid="does-not-exist"), headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 404

    def test_refuses_when_extraction_disabled(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-disabled")
        r = c.post(self.EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "extraction_disabled"

    def test_refuses_when_no_producer_configured(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value({"extraction": {"enabled": True}}))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="ex-no-producer")
        r = c.post(self.EXTRACT.format(base=BASE, cid=conn_id), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "extraction_producer_not_configured"

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


class TestExtractionRunDue:
    """``POST /extraction/run-due`` — the scheduler-driven sweep. Not
    connection-scoped in its path; walks every sharepoint connection."""

    RUN_DUE = "/api/admin/sharepoint/extraction/run-due"

    @pytest.fixture(autouse=True)
    def _clear_extraction_env_var(self, monkeypatch):
        monkeypatch.delenv("AGNES_EXTRACTION_ENABLED", raising=False)

    def test_requires_admin(self, seeded_app):
        r = seeded_app["client"].post(self.RUN_DUE, headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_noop_when_extraction_disabled(self, seeded_app, monkeypatch):
        config = {
            "extraction": {
                **_ENABLED_EXTRACTION_CONFIG["extraction"],
                "enabled": False,
                "schedule": "every 15m",
            }
        }
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(config))
        c = seeded_app["client"]
        _create_connection(c, seeded_app["admin_token"], name="due-off")
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
        config = {"extraction": {**_ENABLED_EXTRACTION_CONFIG["extraction"], "schedule": "every 15m"}}
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
        config = {"extraction": {**_ENABLED_EXTRACTION_CONFIG["extraction"], "schedule": "every 15m"}}
        monkeypatch.setattr("app.instance_config.get_value", _config_get_value(config))
        c = seeded_app["client"]
        conn_id = _create_connection(c, seeded_app["admin_token"], name="due-not-yet")
        first = c.post(self.RUN_DUE, headers=_auth(seeded_app["admin_token"]))
        assert first.json()["dispatched"] == [conn_id]

        second = c.post(self.RUN_DUE, headers=_auth(seeded_app["admin_token"]))
        assert second.status_code == 200, second.text
        assert second.json()["dispatched"] == []

    def test_ignores_non_sharepoint_connections(self, seeded_app, monkeypatch):
        config = {"extraction": {**_ENABLED_EXTRACTION_CONFIG["extraction"], "schedule": "every 15m"}}
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
