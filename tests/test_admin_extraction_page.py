"""`/admin/extraction` — the SharePoint extraction fleet dashboard shell
(2026-09-02).

Shell-only page: the table itself is fetched client-side from
``GET /api/admin/sharepoint/extraction/runs`` (covered by
``tests/test_admin_extraction.py`` / ``tests/db_pg/test_extraction_api_pg.py``).
This file covers the page's own contract — auth gate, page-shell markers the
JS hangs off, and the off-nav registration.

The poll/render logic itself moved into a static, cache-eligible asset
(``admin_extraction.js``, TCRD-296 synthesis, 2026-09-03 — mirrors the source
card's own ``data_sources_extraction_observability.js`` externalization a
day earlier), so the fetch URL and the reprocessing-button wiring live
there, not inlined into this page's own HTML.
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
        assert 'id="ext-jobs-strip"' in body
        assert "admin_extraction.js" in body

        # …and the referenced asset actually serves the poll/render code,
        # fetched through the SAME client, the way a browser would.
        script = client.get("/static/js/admin/admin_extraction.js")
        assert script.status_code == 200, script.text
        assert "/api/admin/sharepoint/extraction/runs" in script.text
        assert "renderJobsStrip" in script.text

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
        assert "pending" not in entry["reached_from"], entry

    def test_the_source_card_is_the_door(self):
        """The off-nav record names the source card's Run row as the door;
        the template has to actually carry it. The generic guard in
        `tests/test_web_admin_nav.py` checks every off-nav page has SOME
        literal link — this pins WHICH template, so the door cannot quietly
        migrate to a page an operator watching a crawl never opens."""
        from pathlib import Path

        # The Run row is drawn by the source card's externalized script
        # (`data_sources_extraction_observability.js`), so that is where the
        # literal door has to live — the template only loads the script.
        tpl = Path("app/web/templates/admin_data_sources.html").read_text(encoding="utf-8")
        assert "data_sources_extraction_observability.js" in tpl
        script = Path("app/web/static/js/admin/data_sources_extraction_observability.js").read_text(encoding="utf-8")
        assert 'href="/admin/extraction"' in script
        assert "All connections" in script

    def test_visiting_the_page_lights_the_data_section(self):
        from app.web.admin_nav import resolve_active_section_key

        assert resolve_active_section_key("/admin/extraction") == "data"
