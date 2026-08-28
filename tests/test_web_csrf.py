"""F2 double-submit CSRF coverage for state-changing HTML form POSTs.

The web session cookie is accepted as an auth fallback, so state-changing
form POSTs cannot rely on ``SameSite=Lax`` alone. Each must require the
double-submit pair: a ``web_csrf`` cookie matching the ``csrf_token`` hidden
form field (or ``X-CSRF-Token`` header for JS calls). The Slack bind pair has
its own short-lived variant (``slack_bind_csrf``); the refetch-groups header
coverage lives in tests/test_me_debug.py.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def contribute_skill_on(monkeypatch):
    """Several cases here use `/admin/contribute-skill` as their example of a
    state-changing HTML form POST, and that page is hidden by default since the
    admin cleanup retired it (both POSTs redirect home ahead of the CSRF check,
    by design — a stale external button must not publish past a hidden page).

    The subject of these tests is the double-submit pair, not the page, so they
    expose the page rather than move to a different form. `tests/
    test_retired_admin_surfaces.py` owns the hidden behavior.
    """
    monkeypatch.setenv("AGNES_CONTRIBUTE_SKILL_ENABLED", "1")


# Stateless double-submit: any matching cookie/field pair passes, so tests
# supply the pair directly instead of scraping it from a prior GET.
_CSRF = "test-csrf-token-0123456789abcdef"


def _admin_cookies(seeded_app, **extra: str) -> dict[str, str]:
    return {"access_token": seeded_app["admin_token"], **extra}


# ---------------------------------------------------------------------------
# Token issuance — the pages that host the forms set the cookie + embed the token
# ---------------------------------------------------------------------------


def test_contribute_page_issues_cookie_and_embeds_matching_token(seeded_app):
    c = seeded_app["client"]
    r = c.get("/admin/contribute-skill", cookies=_admin_cookies(seeded_app))
    assert r.status_code == 200
    token = r.cookies.get("web_csrf")
    assert token, "GET /admin/contribute-skill must set the web_csrf cookie"
    assert f'name="csrf_token" value="{token}"' in r.text


def test_contribute_page_reuses_existing_cookie_token(seeded_app):
    """An already-issued token is reused, so several open tabs keep working."""
    c = seeded_app["client"]
    r = c.get("/admin/contribute-skill", cookies=_admin_cookies(seeded_app, web_csrf=_CSRF))
    assert r.status_code == 200
    assert f'name="csrf_token" value="{_CSRF}"' in r.text


def test_profile_page_issues_cookie(seeded_app):
    c = seeded_app["client"]
    r = c.get("/me/profile", cookies=_admin_cookies(seeded_app))
    assert r.status_code == 200
    assert r.cookies.get("web_csrf"), "GET /me/profile must set the web_csrf cookie"


# ---------------------------------------------------------------------------
# POST /admin/contribute-skill
# ---------------------------------------------------------------------------


def test_contribute_post_without_token_is_rejected(seeded_app, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        "src.skill_contribution.contribute_skill",
        lambda *a, **k: calls.append(1),
    )
    c = seeded_app["client"]

    # Missing form field.
    r = c.post(
        "/admin/contribute-skill",
        data={"skill_md": "# skill"},
        cookies=_admin_cookies(seeded_app, web_csrf=_CSRF),
    )
    assert r.status_code == 400
    # Field present but not matching the cookie.
    r = c.post(
        "/admin/contribute-skill",
        data={"skill_md": "# skill", "csrf_token": "some-other-value"},
        cookies=_admin_cookies(seeded_app, web_csrf=_CSRF),
    )
    assert r.status_code == 400
    # No web_csrf cookie at all (clear the client jar first — the 400
    # responses above re-issue the cookie, which the TestClient stores).
    c.cookies.clear()
    r = c.post(
        "/admin/contribute-skill",
        data={"skill_md": "# skill", "csrf_token": _CSRF},
        cookies=_admin_cookies(seeded_app),
    )
    assert r.status_code == 400

    assert not calls, "contribute_skill must not run on a CSRF failure"


def test_contribute_post_with_matching_pair_is_accepted(seeded_app, monkeypatch):
    def fake_contribute(skill_md, registered_by=None, grant_group=None):
        return {
            "skill_name": "Test Skill",
            "plugin_name": "test-skill",
            "detail_url": "/marketplace/contributed/test-skill",
            "granted_group": grant_group,
        }

    monkeypatch.setattr("src.skill_contribution.contribute_skill", fake_contribute)
    c = seeded_app["client"]
    r = c.post(
        "/admin/contribute-skill",
        data={"skill_md": "# skill", "csrf_token": _CSRF},
        cookies=_admin_cookies(seeded_app, web_csrf=_CSRF),
    )
    assert r.status_code == 200
    assert "Published." in r.text


# ---------------------------------------------------------------------------
# POST /admin/contribute-skill/{name}/delete
# ---------------------------------------------------------------------------


def test_delete_post_without_token_is_rejected(seeded_app):
    c = seeded_app["client"]
    r = c.post(
        "/admin/contribute-skill/some-plugin/delete",
        cookies=_admin_cookies(seeded_app, web_csrf=_CSRF),
    )
    assert r.status_code == 400
    assert "Security check failed" in r.text


def test_non_ascii_form_token_is_rejected_cleanly(seeded_app, monkeypatch):
    """``secrets.compare_digest`` raises TypeError on non-ASCII *str* input, so
    the check must compare UTF-8 bytes — a crafted token must yield the normal
    400 rejection, not an unhandled 500."""
    calls: list[int] = []
    monkeypatch.setattr(
        "src.skill_contribution.contribute_skill",
        lambda *a, **k: calls.append(1),
    )
    c = seeded_app["client"]
    r = c.post(
        "/admin/contribute-skill",
        data={"skill_md": "# skill", "csrf_token": "é-not-ascii"},
        cookies=_admin_cookies(seeded_app, web_csrf=_CSRF),
    )
    assert r.status_code == 400
    assert not calls


def test_slack_bind_non_ascii_token_is_rejected_cleanly(seeded_app):
    """The Slack-bind double-submit check has the same shape; a non-ASCII
    token must hit the 400 csrf branch, not a TypeError → 500."""
    c = seeded_app["client"]
    r = c.post(
        "/slack/bind",
        data={"code": "ABC123", "csrf_token": "é-not-ascii"},
        cookies={
            "access_token": seeded_app["admin_token"],
            "slack_bind_csrf": "some-cookie-token",
        },
    )
    assert r.status_code == 400


def test_delete_post_with_matching_pair_reaches_handler(seeded_app):
    """With a valid pair the request passes the CSRF gate and the handler's
    own not-found branch answers (proves the gate, no fixture plugin needed)."""
    c = seeded_app["client"]
    r = c.post(
        "/admin/contribute-skill/nonexistent-plugin/delete",
        data={"csrf_token": _CSRF},
        cookies=_admin_cookies(seeded_app, web_csrf=_CSRF),
    )
    assert r.status_code == 200
    assert "not found" in r.text


def test_rejected_post_does_not_rotate_an_existing_token(seeded_app):
    """A rejected submission must not re-issue the cookie the caller already has.

    The cookie is SameSite=Strict, so a cross-site POST arrives without it.
    Setting it unconditionally on the rejection path would let any site
    rotate a signed-in admin's token and break the tabs they already have
    open — a nuisance the CSRF check exists to prevent, not to create.
    """
    c = seeded_app["client"]
    c.cookies.clear()
    r = c.post(
        "/admin/contribute-skill",
        data={"skill_md": "# skill", "csrf_token": "wrong"},
        cookies=_admin_cookies(seeded_app, web_csrf=_CSRF),
    )
    assert r.status_code == 400
    assert "web_csrf" not in r.headers.get("set-cookie", ""), (
        "a caller that already holds a token must keep it across a rejection"
    )


def test_rejected_post_without_a_cookie_still_issues_one(seeded_app):
    """The re-rendered form needs a usable token, so a caller with no cookie
    must still get one — that is the case the non-rotation guard allows."""
    c = seeded_app["client"]
    c.cookies.clear()
    r = c.post(
        "/admin/contribute-skill",
        data={"skill_md": "# skill", "csrf_token": "wrong"},
        cookies=_admin_cookies(seeded_app),
    )
    assert r.status_code == 400
    assert "web_csrf" in r.headers.get("set-cookie", "")
