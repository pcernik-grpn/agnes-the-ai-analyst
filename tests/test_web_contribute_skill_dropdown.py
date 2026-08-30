"""Custom design-system dropdown on /admin/contribute-skill's "Visible to"
group-select control (#1055).

`#grant_group` stays a real `<select>` in the DOM (the plain POST form reads
it natively — no JS wiring to preserve) with a `ds.dropdown()` custom
button+menu alongside it. Visibility between the two is a CSS theme decision,
not a template one — see `app/web/static/css/paper-skin.css`.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def contribute_skill_on(monkeypatch):
    """The page is hidden by default since the admin cleanup; these tests are
    about what it renders when exposed."""
    monkeypatch.setenv("AGNES_CONTRIBUTE_SKILL_ENABLED", "1")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestContributeSkillGrantGroupDropdown:
    def test_native_select_still_renders_for_form_submission(self, seeded_app):
        resp = seeded_app["client"].get("/admin/contribute-skill", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert '<select name="grant_group" id="grant_group"' in text
        # The seeded system groups (Admin, Everyone) both render as options.
        assert '<option value="Admin" selected>Admin</option>' in text
        assert "Everyone" in text

    def test_custom_dropdown_markup_present(self, seeded_app):
        resp = seeded_app["client"].get("/admin/contribute-skill", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'class="ds-dropdown"' in text
        assert 'data-ds-dropdown-target="grant_group"' in text
        assert 'id="grant-group-dd-btn"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'aria-controls="grant-group-dd-menu"' in text
        assert 'id="grant-group-dd-menu"' in text
        assert 'role="menu"' in text
        assert 'role="menuitemradio"' in text
        assert 'data-value="Admin"' in text
        # Default selection (matches the native select's initial `selected`).
        assert 'aria-checked="true"' in text

    def test_dropdown_js_module_is_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/admin/contribute-skill", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text
