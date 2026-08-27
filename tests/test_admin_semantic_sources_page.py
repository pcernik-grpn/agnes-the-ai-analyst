"""Tests for the /admin/semantic-sources page.

Shell-only route (same pattern as /admin/mcp-sources, router.py:8151-8158):
every dynamic bit is client-fetched from the existing
/api/admin/semantic-sources* REST API (app/api/semantic_models.py), so this
page needs no new server-side query logic — just auth + template rendering.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestSemanticSourcesPageAuth:
    def test_semantic_sources_page_requires_admin(self, seeded_app):
        c = seeded_app["client"]

        anon_resp = c.get("/admin/semantic-sources", follow_redirects=False)
        assert anon_resp.status_code in (302, 303, 307)

        token = seeded_app["analyst_token"]
        non_admin_resp = c.get("/admin/semantic-sources", headers=_auth(token))
        assert non_admin_resp.status_code == 403

    def test_admin_can_load_page(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/semantic-sources", headers=_auth(token))
        assert resp.status_code == 200
        assert "Semantic sources" in resp.text

    def test_page_targets_the_existing_semantic_sources_api(self, seeded_app):
        """The page must be a shell over the existing REST API, not a new
        server-side surface — no new endpoint should be needed."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/semantic-sources", headers=_auth(token))
        assert "/api/admin/semantic-sources" in resp.text
