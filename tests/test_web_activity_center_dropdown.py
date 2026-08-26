"""Custom design-system dropdown on the Activity Center filter bar (#1055).

Three of the six `<select>` controls on `/admin/activity` carry a fixed,
server-rendered option list (`#obs-window`, `#f-result`, `#f-resource`) and
get a paired `ds.dropdown()` custom button+menu, following the same pattern
as My Stack's `#stk-sort` — the real `<select>` stays in the DOM (existing
JS wiring untouched) with the custom UI alongside it; visibility between the
two is a CSS theme decision, not a template one (see `paper-skin.css`).

The other three (`#f-user`, `#f-action`, `#f-source`) populate their
`<option>`s entirely at runtime from `/api/admin/observability/facets` and
also get a paired `ds.dropdown()` (initially just the `{'', 'Any'}` option),
kept in sync with the fetched facet options by `syncObsFacetDropdown()` using
`ds_dropdown.js`'s exported re-init hook (#1473).
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestActivityCenterDropdowns:
    def test_native_selects_still_render_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get("/admin/activity", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="obs-window" aria-label="Time range"' in text
        assert '<select id="f-result" class="obs-select ds-dropdown-native">' in text
        assert (
            '<select id="f-resource" class="obs-select ds-dropdown-native" aria-label="Filter by resource type">'
            in text
        )
        # Default selections match the pre-existing native-only markup.
        assert '<option value="1440" selected>Last 24h</option>' in text

    def test_custom_dropdown_markup_present_for_time_range(self, seeded_app):
        resp = seeded_app["client"].get("/admin/activity", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'data-ds-dropdown-target="obs-window"' in text
        assert 'id="obs-window-dd-btn"' in text
        assert 'aria-controls="obs-window-dd-menu"' in text
        assert 'id="obs-window-dd-menu"' in text
        for value in ("60", "360", "1440", "10080", "43200"):
            assert f'data-value="{value}"' in text
        # 24h is the default-selected item.
        assert 'aria-checked="true"' in text

    def test_custom_dropdown_markup_present_for_result_filter(self, seeded_app):
        resp = seeded_app["client"].get("/admin/activity", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'data-ds-dropdown-target="f-result"' in text
        assert 'id="f-result-dd-btn"' in text
        assert 'id="f-result-dd-menu"' in text
        for value in ("", "success", "error", "denied", "none", "other"):
            assert f'data-value="{value}"' in text

    def test_custom_dropdown_markup_present_for_resource_filter(self, seeded_app):
        resp = seeded_app["client"].get("/admin/activity", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'data-ds-dropdown-target="f-resource"' in text
        assert 'id="f-resource-dd-btn"' in text
        assert 'id="f-resource-dd-menu"' in text
        for value in (
            "table:",
            "knowledge_item:",
            "marketplace:",
            "store_submission:",
            "store_entity:",
            "store_upload:",
            "user:",
            "token:",
            "job:",
            "memory_domain_suggestion:",
        ):
            assert f'data-value="{value}"' in text

    def test_dynamic_facet_selects_are_converted(self, seeded_app):
        """#f-user / #f-action / #f-source keep their runtime-populated native
        <select> (existing fillSelect() JS wiring untouched) and are paired
        with a ds.dropdown() kept in sync by syncObsFacetDropdown()."""
        resp = seeded_app["client"].get("/admin/activity", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        for select_id in ("f-user", "f-action", "f-source"):
            assert (
                f'<select id="{select_id}" class="obs-select ds-dropdown-native"><option value="">Any</option></select>'
                in text
            )
            assert f'data-ds-dropdown-target="{select_id}"' in text
            assert f'id="{select_id}-dd-btn"' in text
            assert f'id="{select_id}-dd-menu"' in text

    def test_dropdown_js_module_is_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/admin/activity", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text

    def test_non_admin_still_gets_denied(self, seeded_app):
        resp = seeded_app["client"].get("/admin/activity", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code in (302, 303, 307, 308, 403)
