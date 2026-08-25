"""Custom design-system dropdown on the Analyst sessions page's "Time range"
control (#1055).

`#sx-window` stays a real `<select>` element in the DOM (existing `winSel`
JS wiring in admin_sessions.html is untouched) with a `ds.dropdown()` custom
button+menu alongside it. Visibility between the two is a CSS theme decision,
not a template one — see `app/web/static/css/paper-skin.css`.

The two facet selects (`#sx-user`, `#sx-model`) are populated client-side
from `/api/admin/sessions/facets` and also get a paired `ds.dropdown()`
(initially just the `{'', 'Any'}` option), kept in sync with the fetched
facet options via `ds_dropdown.js`'s exported re-init hook (#1473).
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAdminSessionsDropdown:
    def test_native_select_still_renders_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get("/admin/sessions", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="sx-window" class="ds-dropdown-native">' in text
        assert '<option value="10080" selected>Last 7d</option>' in text

    def test_facet_selects_are_converted(self, seeded_app):
        resp = seeded_app["client"].get("/admin/sessions", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        for select_id in ("sx-user", "sx-model"):
            assert f'<select id="{select_id}" class="obs-select ds-dropdown-native">' in text
            assert f'data-ds-dropdown-target="{select_id}"' in text
            assert f'id="{select_id}-dd-btn"' in text
            assert f'id="{select_id}-dd-menu"' in text

    def test_custom_dropdown_markup_present(self, seeded_app):
        resp = seeded_app["client"].get("/admin/sessions", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'data-ds-dropdown-target="sx-window"' in text
        assert 'id="sx-window-dd-btn"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'aria-controls="sx-window-dd-menu"' in text
        assert 'id="sx-window-dd-menu"' in text
        assert 'role="menu"' in text
        assert 'role="menuitemradio"' in text
        for value in ("1440", "10080", "43200", "129600", "525600"):
            assert f'data-value="{value}"' in text
        assert 'aria-checked="true"' in text

    def test_dropdown_js_module_is_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/admin/sessions", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text

    def test_non_admin_gets_403(self, seeded_app):
        resp = seeded_app["client"].get("/admin/sessions", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403
