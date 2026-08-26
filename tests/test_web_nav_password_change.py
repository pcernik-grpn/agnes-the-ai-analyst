"""B6: /auth/password/change must be reachable from the account menu, not
just by typing the URL — the same placement contract as /me/connections
(tests/test_web_nav_me_connections.py). Guards against the entry point
shipping URL-only, the exact regression that file exists to prevent for its
own page.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_change_password_link_in_account_menu(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert 'href="/auth/password/change"' in body
    assert ">Change password<" in body
    assert "app-user-menu-item" in body
