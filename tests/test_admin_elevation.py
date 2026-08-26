"""Admin elevation consent gate (app/auth/elevation.py).

While an admin's elevation is paused (cookie or instance default), the
Admin short-circuit in ``can_access`` is skipped and ``require_admin``
refuses with the distinct ``admin_elevation_paused`` detail. The cookie
can only reduce privilege; non-request contexts always see the elevated
default so scheduler/CLI automation is untouched.
"""

import logging

import pytest

from app.auth import access, elevation
from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def system_conn(seeded_app):
    conn = get_system_db()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unit level: contextvar + can_access interplay
# ---------------------------------------------------------------------------


def test_default_is_elevated_outside_requests(system_conn):
    assert elevation.elevation_paused() is False
    assert access.can_access("admin1", "table", "keboola.anything", conn=system_conn)


def test_paused_skips_god_mode_short_circuit(system_conn):
    token = elevation.set_paused_for_request(True)
    try:
        assert not access.can_access("admin1", "table", "keboola.ungranted", conn=system_conn)
    finally:
        elevation.reset_for_request(token)
    # and back to normal after reset
    assert access.can_access("admin1", "table", "keboola.ungranted", conn=system_conn)


def test_paused_admin_keeps_explicit_grants(system_conn):
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    group = UserGroupsRepository(system_conn).ensure("elevation-grantees")
    UserGroupMembersRepository(system_conn).add_member("admin1", group["id"], source="admin")
    ResourceGrantsRepository(system_conn).ensure_grant(group["id"], "table", "keboola.mine")

    token = elevation.set_paused_for_request(True)
    try:
        assert access.can_access("admin1", "table", "keboola.mine", conn=system_conn)
        assert not access.can_access("admin1", "table", "keboola.other", conn=system_conn)
    finally:
        elevation.reset_for_request(token)


def test_paused_never_logs_god_mode_bypass(system_conn, caplog):
    access._god_mode_logged.clear()
    token = elevation.set_paused_for_request(True)
    try:
        with caplog.at_level(logging.INFO, logger="app.auth.access"):
            access.can_access("admin1", "table", "keboola.paused-check", conn=system_conn)
    finally:
        elevation.reset_for_request(token)
    assert not [r for r in caplog.records if "god_mode_bypass" in r.getMessage()]


def test_resolve_from_cookie_precedence(monkeypatch):
    assert elevation.resolve_from_cookie("paused") is True
    assert elevation.resolve_from_cookie("elevated") is False
    # absence/garbage → instance default
    assert elevation.resolve_from_cookie(None) is False
    assert elevation.resolve_from_cookie("junk") is False
    monkeypatch.setattr(elevation, "default_elevation", lambda: elevation.PAUSED)
    assert elevation.resolve_from_cookie(None) is True
    assert elevation.resolve_from_cookie("elevated") is False  # explicit cookie wins
    # Bearer callers (CLI/PAT) are exempt from the instance default —
    # they have no cookie jar to re-elevate with...
    assert elevation.resolve_from_cookie(None, bearer_auth=True) is False
    assert elevation.resolve_from_cookie("junk", bearer_auth=True) is False
    # ...but an explicit paused cookie still reduces, Bearer or not
    assert elevation.resolve_from_cookie("paused", bearer_auth=True) is True


def test_default_elevation_reads_real_config(monkeypatch):
    """Regression for the #1146 review finding: default_elevation must walk
    the real nested config (get_value takes one positional per level +
    default= keyword) — a dotted-string key silently never matched and the
    knob was dead code. Drives the REAL get_value, only the config load is
    stubbed."""
    import app.instance_config as ic

    monkeypatch.setattr(ic, "load_instance_config", lambda: {"access": {"admin_default_elevation": "paused"}})
    assert elevation.default_elevation() == elevation.PAUSED
    monkeypatch.setattr(ic, "load_instance_config", lambda: {"access": {"admin_default_elevation": "elevated"}})
    assert elevation.default_elevation() == elevation.ELEVATED
    # absent / unknown values fall back to elevated
    monkeypatch.setattr(ic, "load_instance_config", lambda: {})
    assert elevation.default_elevation() == elevation.ELEVATED
    monkeypatch.setattr(ic, "load_instance_config", lambda: {"access": {"admin_default_elevation": "wat"}})
    assert elevation.default_elevation() == elevation.ELEVATED


# ---------------------------------------------------------------------------
# HTTP level: middleware + toggle endpoint + require_admin
# ---------------------------------------------------------------------------


def test_admin_api_refuses_while_paused_cookie(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]

    r = c.get("/api/admin/groups", headers=_auth(admin))
    assert r.status_code == 200

    c.cookies.set(elevation.ELEVATION_COOKIE, elevation.PAUSED)
    try:
        r = c.get("/api/admin/groups", headers=_auth(admin))
        assert r.status_code == 403
        assert r.json()["detail"] == "admin_elevation_paused"
    finally:
        c.cookies.delete(elevation.ELEVATION_COOKIE)

    r = c.get("/api/admin/groups", headers=_auth(admin))
    assert r.status_code == 200


def test_toggle_endpoint_sets_cookie_and_audits(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]

    r = c.post("/api/me/elevation", json={"paused": True}, headers=_auth(admin))
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "paused": True}
    assert c.cookies.get(elevation.ELEVATION_COOKIE) == elevation.PAUSED

    # paused admin can still re-elevate (endpoint gates on RAW membership,
    # not require_admin — otherwise they'd be locked out)
    r = c.post("/api/me/elevation", json={"paused": False}, headers=_auth(admin))
    assert r.status_code == 200
    assert c.cookies.get(elevation.ELEVATION_COOKIE) == elevation.ELEVATED
    c.cookies.delete(elevation.ELEVATION_COOKIE)


def test_toggle_endpoint_refuses_non_admin(seeded_app):
    c = seeded_app["client"]
    r = c.post("/api/me/elevation", json={"paused": True}, headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 403


def test_instance_default_paused_is_browser_only(seeded_app, monkeypatch):
    """default=paused gates cookie-session browsers, never Bearer callers.

    Bearer-authenticated automation (CLI, PATs, service tokens) has no
    cookie jar to re-elevate with — a paused instance default must not
    403 every `agnes admin …` call (#1146 review finding)."""
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    monkeypatch.setattr(elevation, "default_elevation", lambda: elevation.PAUSED)

    # Bearer caller (CLI-shaped): exempt from the instance default
    r = c.get("/api/admin/groups", headers=_auth(admin))
    assert r.status_code == 200

    # ...but an explicit paused cookie still reduces even with Bearer auth
    c.cookies.set(elevation.ELEVATION_COOKIE, elevation.PAUSED)
    try:
        r = c.get("/api/admin/groups", headers=_auth(admin))
        assert r.status_code == 403
    finally:
        c.cookies.delete(elevation.ELEVATION_COOKIE)

    # Browser-shaped caller (session cookie, no Authorization header):
    # the paused default applies...
    c.cookies.set("access_token", admin)
    try:
        r = c.get("/api/admin/groups")
        assert r.status_code == 403
        assert r.json()["detail"] == "admin_elevation_paused"

        # ...and an explicit elevated cookie wins over it
        c.cookies.set(elevation.ELEVATION_COOKIE, elevation.ELEVATED)
        r = c.get("/api/admin/groups")
        assert r.status_code == 200
    finally:
        c.cookies.delete(elevation.ELEVATION_COOKIE)
        c.cookies.delete("access_token")


def test_a_pause_only_applies_to_the_person_who_paused(monkeypatch):
    """The pause is an admin pausing THEIR OWN god-mode, so it must not answer
    authorization questions asked about somebody else.

    `can_access` is called about other users — the co-drive invite checks the
    INVITEE's access, not the caller's — and consulting the caller's pause
    there told an admin their admin colleague "lacks chat access" while the
    colleague's own permissions were untouched (Devin Review on #1146).
    """
    from app.auth.elevation import (
        elevation_paused,
        reset_caller_for_request,
        reset_for_request,
        set_caller_for_request,
        set_paused_for_request,
    )

    ptok = set_paused_for_request(True)
    ctok = set_caller_for_request("admin-a")
    try:
        assert elevation_paused("admin-a") is True, "the pauser's own checks stay paused"
        assert elevation_paused("admin-b") is False, "…but a colleague's do not"
        assert elevation_paused() is True, "no subject keeps the request-scoped meaning"
    finally:
        reset_caller_for_request(ctok)
        reset_for_request(ptok)


def test_an_unknown_caller_still_honours_the_pause():
    """Fail toward less privilege: if nothing stamped the caller, the pause
    stands rather than being silently discarded."""
    from app.auth.elevation import elevation_paused, reset_for_request, set_paused_for_request

    tok = set_paused_for_request(True)
    try:
        assert elevation_paused("anyone") is True
    finally:
        reset_for_request(tok)


def test_not_paused_is_never_paused_for_anyone():
    from app.auth.elevation import elevation_paused

    assert elevation_paused("admin-a") is False
    assert elevation_paused() is False


def test_can_access_ignores_the_pause_when_asked_about_another_admin(system_conn):
    """End-to-end counterpart: the co-drive invite calls `can_access` about the
    INVITEE. With the caller stamped, one admin's pause must not answer a
    question about a different admin (Devin Review on #1146)."""
    from src.repositories.user_group_members import UserGroupMembersRepository

    admin_gid = system_conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]
    UserGroupMembersRepository(system_conn).add_member("admin2", admin_gid, source="admin")

    ptok = elevation.set_paused_for_request(True)
    ctok = elevation.set_caller_for_request("admin1")
    try:
        assert not access.can_access("admin1", "table", "keboola.ungranted", conn=system_conn), (
            "the pauser still short-circuits"
        )
        assert access.can_access("admin2", "table", "keboola.ungranted", conn=system_conn), (
            "one admin's pause blocked a check about a different admin"
        )
    finally:
        elevation.reset_caller_for_request(ctok)
        elevation.reset_for_request(ptok)


class TestRequestIsNoninteractive:
    """The elevation middleware treats a request as non-interactive (exempt
    from the instance-wide paused default) via request_is_noninteractive.
    An X-StorageApi-Token header counts ONLY when it is the actual credential;
    a session-cookie request merely carrying it must stay interactive, or an
    admin could slip past a paused default by adding the header (Devin #1288)."""

    def test_bearer_is_noninteractive(self):
        from app.auth.elevation import request_is_noninteractive

        assert request_is_noninteractive(authorization="Bearer x", has_session_cookie=False, has_sapi_header=False)
        # Bearer wins even alongside a cookie (bearer has get_current_user precedence).
        assert request_is_noninteractive(authorization="Bearer x", has_session_cookie=True, has_sapi_header=False)

    def test_sole_sapi_header_is_noninteractive(self):
        from app.auth.elevation import request_is_noninteractive

        assert request_is_noninteractive(authorization="", has_session_cookie=False, has_sapi_header=True)

    def test_sapi_header_with_session_cookie_stays_interactive(self):
        from app.auth.elevation import request_is_noninteractive

        # The fix: a cookie-authenticated request carrying the header must NOT
        # be exempted from the paused default.
        assert not request_is_noninteractive(authorization="", has_session_cookie=True, has_sapi_header=True)

    def test_plain_session_is_interactive(self):
        from app.auth.elevation import request_is_noninteractive

        assert not request_is_noninteractive(authorization="", has_session_cookie=True, has_sapi_header=False)
        assert not request_is_noninteractive(authorization="", has_session_cookie=False, has_sapi_header=False)


# ---------------------------------------------------------------------------
# Chrome: the rendered UI must match the authority the request actually has
# ---------------------------------------------------------------------------
#
# `session.user.is_admin` was raw Admin-group membership, so a paused admin
# kept every admin affordance — the rail's Admin row above all — and each one
# led to a 403 `admin_elevation_paused`. The flag is EFFECTIVE authority now,
# with `is_admin_paused` carrying the difference so the chrome can say why
# instead of going quiet.


def test_the_admin_flag_is_effective_authority_not_raw_membership(seeded_app):
    """A paused admin renders as a non-admin, and says so."""
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    html = {"Accept": "text/html"}

    body = c.get("/library", headers={**_auth(admin), **html}).text
    assert 'href="/admin"' in body, "an elevated admin has the rail's Admin row"
    assert "Admin paused" not in body

    c.cookies.set(elevation.ELEVATION_COOKIE, elevation.PAUSED)
    try:
        body = c.get("/library", headers={**_auth(admin), **html}).text
        # Not one link into the subtree that would 403 — asserted on the
        # RENDERED page, so a chrome consumer that gates on raw membership
        # somewhere else on this page fails here too.
        assert 'href="/admin"' not in body
        # …replaced by an honest one pointing at the switch, which is NOT
        # admin-gated — so pausing is always reversible from the UI.
        assert "Admin paused" in body
        assert 'href="/me/profile#admin-mode"' in body
    finally:
        c.cookies.delete(elevation.ELEVATION_COOKIE)


def test_a_paused_admin_is_never_stranded(seeded_app):
    """The re-elevate switch renders for a paused admin on a page that does
    not require elevation — the property that makes hiding admin chrome safe
    rather than a lockout."""
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    c.cookies.set(elevation.ELEVATION_COOKIE, elevation.PAUSED)
    try:
        r = c.get("/me/profile", headers={**_auth(admin), "Accept": "text/html"})
        assert r.status_code == 200, r.text
        assert 'id="admin-mode"' in r.text
    finally:
        c.cookies.delete(elevation.ELEVATION_COOKIE)


def test_a_non_admin_is_neither_admin_nor_paused(seeded_app):
    """`is_admin_paused` must not fire for someone who was never an admin —
    otherwise every analyst gets a rail row about a mode they cannot have."""
    c = seeded_app["client"]
    body = c.get("/library", headers={**_auth(seeded_app["analyst_token"]), "Accept": "text/html"}).text
    assert "Admin paused" not in body
    assert 'href="/admin"' not in body
