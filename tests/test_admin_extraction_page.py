"""`/admin/extraction` — the SharePoint extraction fleet dashboard shell
(2026-09-02).

Shell-only page: the table itself is fetched client-side from
``GET /api/admin/sharepoint/extraction/runs`` (covered by
``tests/test_admin_extraction.py`` / ``tests/db_pg/test_extraction_api_pg.py``).
This file covers the page's own contract — auth gate, page-shell markers the
JS hangs off, and the off-nav registration.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestExtractionFleetPageAuth:
    def test_admin_can_load_page(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        resp = client.get("/admin/extraction", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert 'id="ext-tbody"' in body
        assert 'id="ext-scope-active"' in body
        assert 'id="ext-scope-all"' in body
        assert "/api/admin/sharepoint/extraction/runs" in body

    def test_non_admin_gets_403(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["analyst_token"]
        resp = client.get("/admin/extraction", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauthenticated_redirects(self, seeded_app):
        resp = seeded_app["client"].get("/admin/extraction", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)


class TestExtractionFleetPageNav:
    def test_page_is_registered_off_nav_with_a_reason(self):
        from app.web.admin_nav import ADMIN_NAV_OFFNAV

        entry = next((e for e in ADMIN_NAV_OFFNAV if e["href"] == "/admin/extraction"), None)
        assert entry is not None, "/admin/extraction must be in ADMIN_NAV_OFFNAV with a reached_from"
        assert entry["reached_from"]

    def test_visiting_the_page_lights_the_data_section(self):
        from app.web.admin_nav import resolve_active_section_key

        assert resolve_active_section_key("/admin/extraction") == "data"
