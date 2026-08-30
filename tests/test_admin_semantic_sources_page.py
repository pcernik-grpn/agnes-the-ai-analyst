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


class TestOwnedModelCountColumn:
    """#1707: a source that syncs ok and imports nothing must not look like a
    healthy one. The page renders client-side, so what is pinned here is the
    shell it renders FROM — the column, the field it reads, and the two
    visually distinct markers.
    """

    def _body(self, seeded_app) -> str:
        return seeded_app["client"].get(
            "/admin/semantic-sources", headers=_auth(seeded_app["admin_token"])
        ).text

    def test_the_table_has_a_models_column(self, seeded_app):
        assert "<th>Models</th>" in self._body(seeded_app)

    def test_the_row_renders_the_api_field(self, seeded_app):
        body = self._body(seeded_app)
        assert "owned_model_count" in body

    def test_owning_nothing_and_owning_some_render_different_markers(self, seeded_app):
        """Distinguishable at a glance, which "ok" alone is not."""
        body = self._body(seeded_app)
        assert "ss-models-empty" in body
        assert "ss-models-neutral" in body

    def test_neither_marker_is_an_error_state(self, seeded_app):
        """Importing nothing is worth attention, not an error — no danger
        accent on either marker (design-system status vocabulary)."""
        body = self._body(seeded_app)
        marker_css = [line for line in body.splitlines() if ".ss-models" in line]
        assert marker_css, "the marker styles must be in the page's head_extra"
        assert not any("danger" in line for line in marker_css)

    def test_the_attention_marker_fires_only_for_a_source_that_actually_synced(self, seeded_app):
        """Same rule as the CLI and the docs: amber means "synced and still
        owns nothing". A never-synced (or skipped) source owning nothing has
        not failed to import — it has not run — so it stays neutral."""
        body = self._body(seeded_app)
        renderer = body.split("function fmtOwnedModels")[1].split("\nfunction ")[0]
        amber_branch = renderer.split("ss-models-empty")[0]
        assert 'n === 0 && s.last_sync_status === "ok"' in amber_branch

    def test_an_unknown_count_is_not_rendered_as_zero(self, seeded_app):
        """`owned_model_count: null` means "cannot say" — the page must show
        the unknown marker, never a confident "0 models"."""
        body = self._body(seeded_app)
        renderer = body.split("function fmtOwnedModels")[1].split("\nfunction ")[0]
        assert 'typeof n !== "number"' in renderer
        assert "ss-models-unknown" in renderer
