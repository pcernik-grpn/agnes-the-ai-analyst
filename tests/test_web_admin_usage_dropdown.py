"""Custom design-system dropdown on the Telemetry page's "Time range" and
"Group by" controls (#1055).

`#u-window` / `#u-groupby` stay real `<select>` elements in the DOM (existing
`winSel`/`groupSel` JS wiring in admin_usage.html is untouched) with a
`ds.dropdown()` custom button+menu alongside each. Visibility between the two
is a CSS theme decision, not a template one — see
`app/web/static/css/paper-skin.css`.

The four facet selects (`#u-user`, `#u-tool`, `#u-source`, `#u-event-type`)
are populated client-side from `/api/admin/telemetry/facets` and also get a
paired `ds.dropdown()` (initially just the `{'', 'Any'}` option), kept in
sync with the fetched facet options via `ds_dropdown.js`'s exported re-init
hook (#1473).
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAdminUsageDropdown:
    def test_native_selects_still_render_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get("/admin/telemetry", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="u-window" class="ds-dropdown-native">' in text
        assert '<select id="u-groupby" class="ds-dropdown-native">' in text
        assert '<option value="10080" selected>Last 7d</option>' in text
        assert '<option value="day" selected>Day</option>' in text

    def test_facet_selects_are_converted(self, seeded_app):
        resp = seeded_app["client"].get("/admin/telemetry", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        for select_id in ("u-user", "u-tool", "u-source", "u-event-type"):
            assert f'<select id="{select_id}" class="obs-select ds-dropdown-native">' in text
            assert f'data-ds-dropdown-target="{select_id}"' in text
            assert f'id="{select_id}-dd-btn"' in text
            assert f'id="{select_id}-dd-menu"' in text

    def test_custom_dropdown_markup_present_for_window_and_groupby(self, seeded_app):
        resp = seeded_app["client"].get("/admin/telemetry", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        for base_id, target in (("u-window-dd", "u-window"), ("u-groupby-dd", "u-groupby")):
            assert f'data-ds-dropdown-target="{target}"' in text
            assert f'id="{base_id}-btn"' in text
            assert f'id="{base_id}-menu"' in text
        assert text.count('aria-haspopup="menu"') >= 2
        assert text.count('role="menuitemradio"') >= 11  # 5 window + 6 groupby options
        for value in ("1440", "10080", "43200", "129600", "525600"):
            assert f'data-value="{value}"' in text
        for value in ("day", "username", "tool_name", "source", "ref_id"):
            assert f'data-value="{value}"' in text

    def test_dropdown_js_module_is_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/admin/telemetry", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text

    def test_non_admin_gets_403(self, seeded_app):
        resp = seeded_app["client"].get("/admin/telemetry", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403
