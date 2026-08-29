"""Admin menu: the "Moderation & Trust" hub (/admin/store) surfaces in the
Admin menu for admins and never for non-admins.

The hub is HIDDEN by default now (`features.store_moderation_enabled` —
tests/test_retired_admin_surfaces.py owns that default and the redirect), so
both tests here turn the flag ON. That is not a workaround: with the flag off
the non-admin test would pass because the row renders for NOBODY, which is the
wrong reason and would keep passing if the admin-only rule were deleted. The
question this module asks — is the row admin-only — only exists on an instance
where the row can render at all.

Still deliberately UNGATED on `store.verification_enabled` (spec 2026-08-07,
accepted deviations — settled in Devin Review round 5 on #1200): the hub
hosts the flea submission-review count and marketplace-curation jump-offs
even with verification off, so hiding the row would unlink live content.
The page itself hides its verification section when the switch is off
(`admin_moderation_hub.html` renders off `store_verification_enabled`). The
two switches are independent and this module pins that: it enables only the
hub flag, never the verification one.

The link used to live in the topnav's Admin mega-menu, and this suite
rendered /dashboard to find it there. Wave 0 (2026-08) retired that chrome and
/dashboard with it; the admin inventory is `app/web/admin_nav.py`, rendered as
the admin sidebar on every /admin/* page, so that is where the row is asserted
now. `/admin/store` reaching the inventory at all was an open TODO on the old
hand-written rail menu — this is the guard that it stayed closed.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_moderation_hub_link_in_admin_menu_for_admin(seeded_app, monkeypatch):
    """Present for admins regardless of the verification switch (left off here
    — the two flags are independent, see the module docstring)."""
    monkeypatch.setenv("AGNES_STORE_MODERATION_ENABLED", "1")
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin", headers=_auth(token))
    assert resp.status_code == 200
    # Exact href (trailing quote) so it doesn't match /admin/store/submissions.
    assert 'href="/admin/store"' in resp.text


def test_moderation_hub_link_absent_for_non_admin(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_STORE_MODERATION_ENABLED", "1")
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    # A non-admin cannot reach /admin at all, and no ordinary page carries an
    # admin row — the sidebar renders only for admins on admin pages.
    assert c.get("/admin", headers=_auth(token)).status_code in (302, 303, 403)
    resp = c.get("/library", headers=_auth(token))
    assert resp.status_code == 200
    assert 'href="/admin/store"' not in resp.text
