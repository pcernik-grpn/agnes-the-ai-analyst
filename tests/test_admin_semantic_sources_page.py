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


class TestScanScopeBesideTheSyncResult:
    """Finding A17 on #1707: "ok · 0 models" cannot separate "there is nothing
    upstream" from "the role I connect as cannot see it". The page renders
    client-side, so what is pinned here is the shell it renders FROM — the
    renderer, the field it reads, and the fact that an absent scope stays
    silent."""

    def _body(self, seeded_app) -> str:
        return seeded_app["client"].get(
            "/admin/semantic-sources", headers=_auth(seeded_app["admin_token"])
        ).text

    def test_the_row_renders_the_api_field(self, seeded_app):
        assert "scan_scope" in self._body(seeded_app)

    def test_it_is_rendered_in_the_last_sync_cell(self, seeded_app):
        """"Scanned X" belongs next to the result of the scan, not in a column
        of its own — it is context for the sync line, not a fourth status."""
        assert "${fmtLastSync(s)}${fmtScanScope(s)}" in self._body(seeded_app)

    def test_it_is_labelled_so_the_string_is_not_bare(self, seeded_app):
        renderer = self._body(seeded_app).split("function fmtScanScope")[1].split("\nfunction ")[0]
        assert "scanned ${esc(s.scan_scope)}" in renderer

    def test_an_absent_scope_renders_nothing_rather_than_an_empty_claim(self, seeded_app):
        renderer = self._body(seeded_app).split("function fmtScanScope")[1].split("\nfunction ")[0]
        assert 'typeof s.scan_scope !== "string"' in renderer
        assert 'return "";' in renderer

    def test_the_scope_is_escaped_before_it_reaches_innerHTML(self, seeded_app):
        """The string is composed from admin-supplied config (a repo URL, a
        database name) and the row is built with innerHTML."""
        renderer = self._body(seeded_app).split("function fmtScanScope")[1].split("\nfunction ")[0]
        assert "${s.scan_scope}" not in renderer

    def test_the_tooltip_holds_for_a_never_synced_row_too(self, seeded_app):
        """The cell also renders on a row that has never synced, where the
        scope is what the FIRST sync will look at — a tooltip saying "last
        sync" would contradict the "never synced" beside it."""
        renderer = self._body(seeded_app).split("function fmtScanScope")[1].split("\nfunction ")[0]
        title = renderer.split('title="')[1].split('"')[0]
        assert "last sync" not in title.lower()

    def test_it_is_informational_not_a_status_accent(self, seeded_app):
        """A scope is neither good nor bad news — it must not borrow the
        success/warn/danger vocabulary."""
        scope_css = [line for line in self._body(seeded_app).splitlines() if ".ss-scope" in line]
        assert scope_css, "the scope style must be in the page's head_extra"
        assert not any("accent" in line for line in scope_css)
