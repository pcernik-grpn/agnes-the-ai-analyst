"""Custom design-system dropdown on the MCP sources list's create modal (#1055).

Each of the create-modal's three `<select>`s — transport, secret scope, auth
method — stays a real `<select>` in the DOM (existing JS wiring:
`syncTransportFields` / `confirm-create-btn` untouched) with a `ds.dropdown()`
custom button+menu alongside it. Visibility between the two is a CSS theme
decision, not a template one — see `app/web/static/css/paper-skin.css`.
"""

from __future__ import annotations


def _auth(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['admin_token']}"}


class TestMcpSourcesListingHasNoCreateForm:
    """The create modal these dropdowns dressed is gone.

    It was the second door to registering a source, and the unguarded one: it
    posted whatever was typed, with no connection check and no tools, while
    the builder that refuses an unreachable server was linked from nowhere on
    this page. The paired `ds.dropdown()` markup went with the form.
    """

    def test_no_create_selects_remain(self, seeded_app):
        resp = seeded_app["client"].get("/admin/mcp-sources", headers=_auth(seeded_app))
        assert resp.status_code == 200
        for dead in ('id="new-transport"', 'id="new-scope"', 'id="new-auth-method"', "syncDropdown"):
            assert dead not in resp.text, f"the create modal is back: {dead}"

    def test_the_add_button_opens_the_builder(self, seeded_app):
        resp = seeded_app["client"].get("/admin/mcp-sources", headers=_auth(seeded_app))
        assert "/admin/mcp-sources/new" in resp.text
