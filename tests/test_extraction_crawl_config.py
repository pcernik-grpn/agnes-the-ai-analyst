"""``PATCH /api/admin/sharepoint/connections/{id}/extraction/crawl-config`` —
per-connection age filter override for ``extraction.crawl.min_modified``: a
190k-document backfill run can crawl only what changed on/after a cutoff
date instead of re-walking the whole corpus.

The resolver itself (``connectors.sharepoint.crawler.resolve_min_modified``
— an ISO date on the connection row, or unfiltered when absent/invalid) is
unit-tested in ``tests/test_sharepoint_crawler.py``; the crawler gate itself
(boundary rule, missing-timestamp handling, deleted-item exemption) lives
there too. This module covers the HTTP surface: RBAC, the write, the
resolved-value response shape, validation, and the audit row — the exact
sibling of ``tests/test_extraction_facts_config.py``.
"""

from __future__ import annotations

BASE = "/api/admin/sharepoint/connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_connection(client, token, *, name="corp-sharepoint-crawl-config"):
    resp = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {"tenant_id": "tenant-1", "client_id": "client-1"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestAuthGating:
    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].patch(f"{BASE}/nope/extraction/crawl-config", json={"min_modified": "2023-12-31"})
        assert r.status_code == 401

    def test_requires_admin(self, seeded_app):
        token = seeded_app["analyst_token"]
        r = seeded_app["client"].patch(
            f"{BASE}/nope/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )
        assert r.status_code == 403


class TestUnknownConnection:
    def test_unknown_connection_is_404(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        r = client.patch(
            f"{BASE}/does-not-exist/extraction/crawl-config",
            json={"min_modified": "2023-12-31"},
            headers=_auth(token),
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "connection_not_found"

    def test_non_sharepoint_connection_is_404(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        created = client.post(
            "/api/admin/source-connections",
            json={
                "name": "kbc-crawl-config",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        r = client.patch(
            f"{BASE}/{created.json()['id']}/extraction/crawl-config",
            json={"min_modified": "2023-12-31"},
            headers=_auth(token),
        )
        assert r.status_code == 404


class TestPatch:
    def test_setting_the_override_returns_it_as_the_resolved_source(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token)

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["connection_id"] == conn_id
        assert body["min_modified"] == {"value": "2023-12-31", "source": "connection"}

    def test_the_override_is_actually_written_to_the_connection_row(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-write")

        client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert row["config"]["extraction"]["crawl"]["min_modified"] == "2023-12-31"
        # A shallow PATCH of `extraction` must not clobber sibling connect-
        # wizard config already on the row.
        assert row["config"]["tenant_id"] == "tenant-1"

    def test_a_null_min_modified_clears_a_previously_set_override(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-clear")
        client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        r = client.patch(f"{BASE}/{conn_id}/extraction/crawl-config", json={}, headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["min_modified"] == {"value": None, "source": "none"}

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert "crawl" not in (row["config"].get("extraction") or {})

    def test_an_invalid_min_modified_is_refused_with_400(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-invalid")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "not-a-date"}, headers=_auth(token)
        )

        assert r.status_code == 400
        assert r.json()["detail"] == "invalid_min_modified"

    def test_works_regardless_of_sharepoint_enabled(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-disabled")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        assert r.status_code == 200


class TestAudit:
    def test_the_patch_is_audited_with_the_value_and_its_resolution(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-audit")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )
        assert r.status_code == 200

        import json

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="extraction.min_modified_set", limit=50)
        matches = [row for row in rows if conn_id in (row.get("resource") or "")]
        assert matches, "the handler's own log_safe row is missing"
        assert matches[0]["user_id"] == "admin1"
        raw_params = matches[0]["params"]
        params = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
        assert params["min_modified"] == "2023-12-31"
        assert params["resolved"] == "2023-12-31"
        assert params["source"] == "connection"


class TestConfigDrawerResolvedValue:
    """`GET .../extraction/config` (the drawer's own read) carries the
    resolved ``min_modified`` alongside the instance-level rows, so the
    Crawl filter panel can pre-fill its date input with the CURRENT
    override rather than opening blank."""

    def test_unset_by_default(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-drawer")
        r = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["min_modified"] == {"value": None, "source": "none"}

    def test_reflects_a_set_override(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-drawer-set")
        client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )
        r = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token))
        assert r.json()["min_modified"] == {"value": "2023-12-31", "source": "connection"}


class TestAdminUiWiring:
    """The `/admin/data-sources` page must actually ship the Crawl filter
    control, not just the API underneath it (the API alone is unusable by
    an admin without shell/API access)."""

    def test_the_page_ships_the_crawl_filter_panel(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        resp = client.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "crawlFilterSave" in body
        assert "crawlFilterClear" in body
        assert "extraction/crawl-config" in body
        # Cookie-session `/api/**` protection: the app-wide CsrfOriginMiddleware
        # origin check, not a form-embedded csrf token — same as the
        # sibling Stop button's own fetch call.
        assert 'credentials: "include"' in body
