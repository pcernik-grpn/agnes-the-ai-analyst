"""Web logout route — issue #1675.

The Logout menu item used to be a plain ``GET`` link to ``/login`` (no route
existed for ``auth.logout``): it neither cleared the ``access_token`` cookie
nor ended the session, it just looked like sign-out. This covers the
GET-confirms / POST-mutates replacement (F2 double-submit CSRF, matching the
``slack_bind`` / ``slack_bind_confirm`` shape — see tests/test_web_csrf.py).

Server-side revocation (issue #1676 — a replayed cookie is refused, not just
cleared client-side) is covered on the Postgres backend in
tests/db_pg/test_session_revocation.py: DuckDB has no revocation column (A3
ratchet), so a DuckDB-backed ``seeded_app`` cannot exercise that half of the
contract — only the cookie-clearing / CSRF-gating half, which is
backend-agnostic.
"""

from __future__ import annotations

import re


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "logout confirm page must embed a csrf_token"
    return m.group(1)


def _cookies(seeded_app, **extra: str) -> dict[str, str]:
    return {"access_token": seeded_app["admin_token"], **extra}


# ---------------------------------------------------------------------------
# GET /auth/logout — confirmation page, no mutation
# ---------------------------------------------------------------------------


def test_get_logout_page_renders_confirm_form_with_csrf(seeded_app):
    c = seeded_app["client"]
    r = c.get("/auth/logout", cookies=_cookies(seeded_app), follow_redirects=False)
    assert r.status_code == 200
    token = r.cookies.get("web_csrf")
    assert token, "GET /auth/logout must set the web_csrf cookie"
    assert f'name="csrf_token" value="{token}"' in r.text
    assert 'action="/auth/logout"' in r.text
    assert "admin@test.com" in r.text


def test_get_logout_page_unauthenticated_redirects_to_login(seeded_app):
    c = seeded_app["client"]
    r = c.get("/auth/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_get_logout_page_does_not_clear_the_session_cookie(seeded_app):
    """The GET must never mutate — security playbook #10. A prior bug used a
    plain GET link that never even tried; this pins the negative the fixed
    route must also keep true (rendering the confirm form is not logout)."""
    c = seeded_app["client"]
    r = c.get("/auth/logout", cookies=_cookies(seeded_app), follow_redirects=False)
    assert r.status_code == 200
    set_cookie = (
        r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else [r.headers.get("set-cookie", "")]
    )
    assert not any("access_token=" in h and ("Max-Age=0" in h or "max-age=0" in h.lower()) for h in set_cookie), (
        "GET must not clear access_token"
    )

    # The session is still live afterwards.
    r2 = c.get("/me/profile", cookies=_cookies(seeded_app), follow_redirects=False)
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# POST /auth/logout — CSRF gate
# ---------------------------------------------------------------------------


def test_post_logout_without_csrf_token_is_rejected(seeded_app):
    c = seeded_app["client"]
    r = c.post(
        "/auth/logout",
        data={},
        cookies=_cookies(seeded_app, web_csrf="some-token"),
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_post_logout_with_mismatched_csrf_token_is_rejected(seeded_app):
    c = seeded_app["client"]
    r = c.post(
        "/auth/logout",
        data={"csrf_token": "wrong-value"},
        cookies=_cookies(seeded_app, web_csrf="right-value"),
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_post_logout_csrf_failure_does_not_clear_the_cookie(seeded_app):
    """A forged cross-site POST (no matching csrf pair) must not be able to
    force-logout a signed-in victim."""
    c = seeded_app["client"]
    r = c.post(
        "/auth/logout",
        data={"csrf_token": "wrong-value"},
        cookies=_cookies(seeded_app, web_csrf="right-value"),
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "access_token" not in r.headers.get("set-cookie", "")

    # The session is still live: the same cookie still authenticates.
    r2 = c.get("/me/profile", cookies=_cookies(seeded_app), follow_redirects=False)
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# POST /auth/logout — success path
# ---------------------------------------------------------------------------


def test_post_logout_with_valid_csrf_clears_the_cookie_and_redirects(seeded_app):
    c = seeded_app["client"]
    csrf = _extract_csrf(c.get("/auth/logout", cookies=_cookies(seeded_app), follow_redirects=False).text)

    r = c.post(
        "/auth/logout",
        data={"csrf_token": csrf},
        cookies=_cookies(seeded_app, web_csrf=csrf),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    set_cookie = r.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie.lower()


def test_post_logout_without_a_session_still_succeeds(seeded_app):
    """A stale tab (already logged out elsewhere) posting the confirm form
    must not 500 — nothing to revoke, but the cookie-clear + redirect still
    complete cleanly."""
    c = seeded_app["client"]
    csrf = "no-session-token"
    r = c.post(
        "/auth/logout",
        data={"csrf_token": csrf},
        cookies={"web_csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_url_for_auth_logout_resolves_to_the_route(seeded_app):
    """The rail's Logout link (``{{ url_for('auth.logout') }}``) must resolve
    to the real route, not the old dead-end redirect-to-/login mapping."""
    from app.web.router import _url_for_shim

    assert _url_for_shim("auth.logout") == "/auth/logout"
