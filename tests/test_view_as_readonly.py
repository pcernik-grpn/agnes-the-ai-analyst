"""Read-only "view this page as another user" mode for admins.

The mode swaps the EFFECTIVE PRINCIPAL of a browser session for one target
user, for reads only. Six properties are the whole point of the feature, and
each one has its own block below:

1. admin-only entry (``require_admin``),
2. strictly read-only while active (every non-GET/HEAD refused centrally,
   with a typed error — never a 500, never a route allow-list),
3. never an escalation (authority is the target's explicit grants, with the
   Admin god-mode short-circuit suppressed — so viewing as another ADMIN
   confers nothing),
4. audited on entry and exit, naming viewer and target,
5. impossible to be in by accident (banner on every page, signed
   self-expiring ticket, bound to the viewer's own session),
6. not CSRF-triggerable (POST + the repo's ``web_csrf`` double-submit token,
   the ``slack_bind_confirm`` pattern — security playbook §10).
"""

from __future__ import annotations

import re

import pytest

from app.auth.view_as import VIEW_AS_COOKIE

ANALYST = "analyst1"
ANALYST_EMAIL = "analyst@test.com"
ADMIN = "admin1"
ADMIN_EMAIL = "admin@test.com"
ADMIN2 = "admin2"
ADMIN2_EMAIL = "admin2@test.com"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def va(monkeypatch, seeded_app):
    """Seeded app + a second admin, a group for the analyst, and one grant
    only the analyst's group holds.

    The grant is what makes the "authority is the TARGET's" assertions real:
    a non-admin caller reaches it through an explicit grant, and the admin
    reaches it only through god-mode — so the two answers differ.
    """
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")

    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories import (
        resource_grants_repo,
        user_group_members_repo,
        user_groups_repo,
        users_repo,
    )

    users_repo().create(id=ADMIN2, email=ADMIN2_EMAIL, name="Second Admin")
    admin_gid = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)["id"]
    user_group_members_repo().add_member(ADMIN2, admin_gid, source="test")

    analysts = user_groups_repo().create(name="Analysts", description="test group")
    analysts_gid = analysts["id"] if isinstance(analysts, dict) else analysts
    user_group_members_repo().add_member(ANALYST, analysts_gid, source="test")
    resource_grants_repo().create(
        group_id=analysts_gid,
        resource_type="collection",
        resource_id="col_analyst_only",
        assigned_by=ADMIN,
    )

    client = seeded_app["client"]
    client.cookies.set("access_token", seeded_app["admin_token"])

    get_system_db().close()
    return {
        **seeded_app,
        "client": client,
        "analysts_gid": analysts_gid,
        "admin_gid": admin_gid,
    }


def _mint_csrf(client) -> str:
    """Load the Access page (which mints + sets the ``web_csrf`` cookie) and
    return the token, exactly as a browser would have it."""
    r = client.get("/admin/access")
    assert r.status_code == 200, r.status_code
    token = client.cookies.get("web_csrf")
    assert token, "GET /admin/access must set the web_csrf double-submit cookie"
    return token


def _enter(
    client,
    target_id: str = ANALYST,
    *,
    next_path: str = "/me/profile",
    return_to: str | None = None,
    csrf: str | None = None,
):
    if csrf is None:
        csrf = _mint_csrf(client)
    data = {"user_id": target_id, "csrf_token": csrf, "next": next_path}
    if return_to is not None:
        data["return_to"] = return_to
    return client.post("/admin/view-as", data=data, follow_redirects=False)


def _exit(client, csrf: str | None = None):
    """Exit posts the CSRF token and NOTHING else.

    No destination field on purpose — the route has no auth dependency, so a
    form-supplied redirect target would be its one attacker-influenced value;
    where to land comes from the signed ticket instead.
    """
    if csrf is None:
        csrf = client.cookies.get("web_csrf")
    return client.post(
        "/admin/view-as/exit",
        data={"csrf_token": csrf or ""},
        follow_redirects=False,
    )


def _audit_rows(action: str) -> list[dict]:
    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action=action, limit=50)
    return rows


# ---------------------------------------------------------------------------
# 1. Admin-only entry
# ---------------------------------------------------------------------------


def test_non_admin_cannot_enter_view_as(va):
    """A non-admin gets 403 and no ticket, even with a valid CSRF token."""
    client = va["client"]
    csrf = _mint_csrf(client)  # minted while still the admin's browser
    client.cookies.set("access_token", va["analyst_token"])

    r = _enter(client, target_id="viewer1", csrf=csrf)

    assert r.status_code == 403
    assert VIEW_AS_COOKIE not in r.cookies
    assert not client.cookies.get(VIEW_AS_COOKIE)


def test_unauthenticated_caller_cannot_enter_view_as(va):
    client = va["client"]
    csrf = _mint_csrf(client)
    client.cookies.delete("access_token")

    r = _enter(client, csrf=csrf)

    assert r.status_code in (401, 403)
    assert not client.cookies.get(VIEW_AS_COOKIE)


def test_admin_cannot_view_as_themselves(va):
    r = _enter(va["client"], target_id=ADMIN)
    assert r.status_code == 400
    assert not va["client"].cookies.get(VIEW_AS_COOKIE)


def test_entering_on_an_unknown_user_is_404(va):
    r = _enter(va["client"], target_id="nope-not-a-user")
    assert r.status_code == 404
    assert not va["client"].cookies.get(VIEW_AS_COOKIE)


# ---------------------------------------------------------------------------
# 6. Not CSRF-triggerable
# ---------------------------------------------------------------------------


def test_entering_is_not_reachable_by_get(va):
    """State change on GET is the hole slack_bind was bitten by (playbook
    §10). There must be no GET door at all."""
    r = va["client"].get(f"/admin/view-as?user_id={ANALYST}", follow_redirects=False)
    assert r.status_code in (404, 405)
    assert not va["client"].cookies.get(VIEW_AS_COOKIE)


def test_entering_without_a_csrf_token_is_refused(va):
    client = va["client"]
    _mint_csrf(client)
    r = client.post(
        "/admin/view-as",
        data={"user_id": ANALYST},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_check_failed"
    assert not client.cookies.get(VIEW_AS_COOKIE)


def test_entering_with_a_forged_csrf_token_is_refused(va):
    client = va["client"]
    _mint_csrf(client)
    r = _enter(client, csrf="forged-token-value")
    assert r.status_code == 403
    assert not client.cookies.get(VIEW_AS_COOKIE)


def test_exiting_without_a_csrf_token_is_refused_and_stays_in_view_as(va):
    client = va["client"]
    assert _enter(client).status_code == 303

    r = client.post("/admin/view-as/exit", data={}, follow_redirects=False)

    assert r.status_code == 403
    assert client.cookies.get(VIEW_AS_COOKIE), "a refused exit must not clear the ticket"


# ---------------------------------------------------------------------------
# 3. Authorization uses the TARGET's authority — and only ever narrows
# ---------------------------------------------------------------------------


def test_page_renders_as_the_target(va):
    """The whole ask: open a PAGE as that person.

    The profile page renders the caller's identity and their group
    memberships, so "Analysts" (a group only the target is in) present and
    "Admin" (the group only the viewer is in) absent is the page actually
    resolving against the target's row, not the viewer's.
    """
    client = va["client"]
    assert _enter(client).status_code == 303

    r = client.get("/me/profile")

    assert r.status_code == 200
    assert ANALYST_EMAIL in r.text
    assert "Analysts" in r.text
    assert ">Admin<" not in r.text


def test_effective_access_is_the_targets_not_the_admins(va):
    """Same endpoint, two answers: the admin's own (admin, no explicit
    grants) and the target's (not admin, one explicit grant)."""
    client = va["client"]

    own = client.get("/api/me/effective-access").json()
    assert own["is_admin"] is True
    assert not [i for i in own["items"] if i["resource_id"] == "col_analyst_only"]

    assert _enter(client).status_code == 303
    as_target = client.get("/api/me/effective-access").json()

    assert as_target["is_admin"] is False
    assert [i for i in as_target["items"] if i["resource_id"] == "col_analyst_only"]


def test_admin_routes_are_refused_while_viewing_as_a_non_admin(va):
    client = va["client"]
    assert client.get("/api/admin/groups").status_code == 200

    assert _enter(client).status_code == 303

    assert client.get("/api/admin/groups").status_code == 403


def test_viewing_as_another_admin_confers_no_admin_authority(va):
    """A target in the Admin group must not hand the mode god-mode back.

    View-as only ever NARROWS: the effective authority is the target's
    EXPLICIT grants, with the Admin short-circuit suppressed.
    """
    client = va["client"]
    assert _enter(client, target_id=ADMIN2).status_code == 303

    assert client.get("/api/admin/groups").status_code == 403
    body = client.get("/api/me/effective-access").json()
    assert body["is_admin"] is False


def test_admin_page_is_refused_while_in_view_as(va):
    client = va["client"]
    assert client.get("/admin/access").status_code == 200

    assert _enter(client).status_code == 303

    assert client.get("/admin/access", follow_redirects=False).status_code == 403


def test_view_as_does_not_widen_a_target_beyond_their_own_grants(va):
    """Belt and braces on "narrow only": a resource NOBODY granted the
    target stays unreachable, even though the viewer is an admin."""
    client = va["client"]
    assert _enter(client).status_code == 303

    body = client.get("/api/me/effective-access").json()
    assert not [i for i in body["items"] if i["resource_id"] == "col_nobody_granted"]


# ---------------------------------------------------------------------------
# 2. Strictly read-only
# ---------------------------------------------------------------------------


def test_a_self_service_mutation_is_refused_with_a_typed_error(va):
    """The exact impersonation risk: acting AS the target. ``PATCH
    /api/me/display-name`` is something the target may genuinely do, so a
    pass here is the read-only guard, not an RBAC gate."""
    client = va["client"]
    assert _enter(client).status_code == 303

    r = client.patch("/api/me/display-name", json={"name": "renamed by the viewer"})

    assert r.status_code == 403
    assert r.json()["error"] == "view_as_read_only"

    from src.repositories import users_repo

    assert users_repo().get_by_id(ANALYST)["name"] == "Analyst"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_mutating_method_is_refused_by_construction(va, method):
    """Not a route allow-list: an unrouted path is refused too, so a route
    added tomorrow is covered without touching this middleware."""
    client = va["client"]
    assert _enter(client).status_code == 303

    r = client.request(method, "/api/a-route-that-does-not-exist-yet")

    assert r.status_code == 403, (method, r.status_code)
    assert r.json()["error"] == "view_as_read_only"


def test_the_read_only_refusal_is_not_a_500(va):
    """A crash would also 'refuse' the request. It must be a clean, typed
    refusal a client can branch on."""
    client = va["client"]
    assert _enter(client).status_code == 303

    r = client.post("/api/sync/trigger")

    assert r.status_code == 403
    body = r.json()
    assert body["error"] == "view_as_read_only"
    assert body["detail"]


def test_a_state_changing_get_behind_deny_principal_is_refused(va):
    """The method guard cannot see a GET that mutates, and this codebase has
    more than one. The MCP OAuth connect authorize/callback pair is a browser
    navigation that creates a flow row and parks an upstream credential —
    under the TARGET's identity, while a banner says the session is
    read-only. ``deny_principal`` (the existing human-only gate) refuses a
    view-as session for exactly that reason.

    This docstring used to say "and this codebase has one". It did not: a
    review found ``GET /api/sync/manifest`` writing to the target as well
    (covered now at its own call site, see
    ``test_a_state_changing_get_does_not_write_to_the_target``). Stating a
    completeness claim as fact is how the next reader stops looking, which is
    the same mistake ``app/middleware/csrf_origin.py`` made with "there are no
    state-changing GET routes". Treat this as a list of the ones found so far,
    never as proof there are no others: a mutating GET added tomorrow is
    invisible to the method guard and must be covered where it is written.
    """
    client = va["client"]
    assert _enter(client).status_code == 303

    r = client.get(
        "/api/mcp/sources/some-source/oauth/authorize",
        follow_redirects=False,
    )

    assert r.status_code == 403
    assert r.json()["detail"] == "not available in view-as"


def test_deny_principal_lets_an_ordinary_session_through(va):
    """Positive control for the guard above: outside view-as the same call is
    not refused by it (it fails later, on the source lookup, which is the
    route's own business)."""
    from app.chat.session_principal_guard import deny_principal

    deny_principal({"id": ADMIN, "email": ADMIN_EMAIL})  # does not raise


def test_reads_still_work_while_in_view_as(va):
    client = va["client"]
    assert _enter(client).status_code == 303

    assert client.get("/api/me/effective-access").status_code == 200
    assert client.get("/me/profile").status_code == 200


def test_websocket_upgrades_are_refused_while_in_view_as(va):
    """A socket can mutate (chat sends messages) and the read-only guard
    cannot inspect frames — so the handshake itself is refused.

    Asserts the SPECIFIC rejection, not a bare ``Exception``: the guard runs
    for websocket scopes, where a ``starlette.requests.Request`` would raise
    an AssertionError — and a test that accepted any exception would have
    called that crash a pass (it did, until this assertion was tightened).
    """
    from starlette.websockets import WebSocketDisconnect

    client = va["client"]
    assert _enter(client).status_code == 303

    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/api/notifications/ws"):
        pass
    assert exc.value.code == 1008  # policy violation, closed before accept


def test_websocket_upgrades_work_when_not_in_view_as(va):
    """Positive control for the refusal above — without it, a socket that
    never connects in this fixture at all would make that test vacuous."""
    client = va["client"]
    with client.websocket_connect("/api/notifications/ws"):
        pass


# ---------------------------------------------------------------------------
# 5. Impossible to be in by accident
# ---------------------------------------------------------------------------


def test_every_page_carries_the_banner_naming_the_target(va):
    client = va["client"]
    assert _enter(client).status_code == 303

    for path in ("/me/profile", "/catalog", "/"):
        r = client.get(path)
        if r.status_code != 200:
            continue
        assert "view-as-banner" in r.text, path
        assert ANALYST_EMAIL in r.text, path
        assert 'action="/admin/view-as/exit"' in r.text, path


def test_the_banner_is_absent_when_not_in_view_as(va):
    r = va["client"].get("/me/profile")
    assert r.status_code == 200
    assert "view-as-banner" not in r.text


def test_the_ticket_cookie_is_httponly_samesite_strict_and_session_scoped(va):
    r = _enter(va["client"])
    assert r.status_code == 303
    header = [h for h in r.headers.get_list("set-cookie") if h.startswith(f"{VIEW_AS_COOKIE}=")]
    assert header, "entering must set the ticket cookie"
    raw = header[0]
    assert "HttpOnly" in raw
    assert "SameSite=strict" in raw or "SameSite=Strict" in raw
    # No Max-Age/Expires: the ticket dies with the browser session.
    assert "Max-Age" not in raw and "expires" not in raw.lower()


def test_a_ticket_minted_for_one_admin_does_nothing_for_another_user(va):
    """State must not leak across users: the ticket is bound to the viewer's
    own session, re-checked live on every request."""
    client = va["client"]
    assert _enter(client).status_code == 303
    stolen = client.cookies.get(VIEW_AS_COOKIE)
    assert stolen

    client.cookies.set("access_token", va["analyst_token"])
    body = client.get("/api/me/effective-access").json()

    # The analyst is themselves, not "the analyst as seen through admin1's
    # ticket", and certainly not the ticket's viewer.
    assert body["is_admin"] is False
    r = client.get("/me/profile")
    assert "view-as-banner" not in r.text


def test_a_ticket_is_inert_once_the_viewer_is_no_longer_an_admin(va):
    """Live authority, not a pre-computed grant: demote the viewer and the
    identity swap stops applying on the very next request.

    The read-only freeze and the banner deliberately DO survive until they
    exit. Dropping them on the same request would hand a just-demoted session
    write access back silently; keeping them leaves the person looking at the
    one control that gets them out.
    """
    client = va["client"]
    assert _enter(client).status_code == 303
    assert client.get("/me/profile").text.count(ANALYST_EMAIL) > 0

    from src.repositories import user_group_members_repo

    user_group_members_repo().remove_member(ADMIN, va["admin_gid"])

    r = client.get("/me/profile")
    assert "Analysts" not in r.text, "the demoted viewer is back to being themselves"
    assert "view-as-banner" in r.text, "still frozen, so still told why and how to leave"
    assert client.patch("/api/me/display-name", json={"name": "x"}).status_code == 403

    assert _exit(client).status_code == 303
    assert client.patch("/api/me/display-name", json={"name": "Admin"}).status_code == 200


def test_a_tampered_ticket_is_ignored_entirely(va):
    client = va["client"]
    assert _enter(client).status_code == 303
    good = client.cookies.get(VIEW_AS_COOKIE)
    # delete-then-set: httpx keys jar entries on (domain, path, name), so a
    # bare set() would ADD a second cookie and the ORIGINAL would still be
    # sent (and win the header parse) — the test would pass vacuously.
    client.cookies.delete(VIEW_AS_COOKIE)
    client.cookies.set(VIEW_AS_COOKIE, good[:-4] + "AAAA")

    body = client.get("/api/me/effective-access").json()
    assert body["is_admin"] is True, "a forged ticket must not swap the principal"

    # ...and it must not freeze the admin's own session read-only either.
    r = client.patch("/api/me/display-name", json={"name": "Admin"})
    assert r.status_code == 200


def test_an_expired_ticket_is_ignored(va, monkeypatch):
    client = va["client"]
    assert _enter(client).status_code == 303

    from app.auth import view_as

    monkeypatch.setattr(view_as, "MAX_AGE_SECONDS", -1)

    assert client.get("/api/me/effective-access").json()["is_admin"] is True


def test_state_does_not_leak_between_requests(va):
    """The mode is request-scoped: the next request on the same worker,
    without the cookie, is not in view-as."""
    client = va["client"]
    assert _enter(client).status_code == 303
    assert client.get("/api/me/effective-access").json()["is_admin"] is False

    client.cookies.delete(VIEW_AS_COOKIE)

    assert client.get("/api/me/effective-access").json()["is_admin"] is True
    assert client.patch("/api/me/display-name", json={"name": "Admin"}).status_code == 200


def test_a_bearer_authenticated_call_is_never_in_view_as(va):
    """The mode is a browser-session concept. A PAT/CLI caller sending the
    cookie alongside a bearer token authenticates as the bearer's owner and
    must not be silently narrowed (nor silently frozen read-only)."""
    client = va["client"]
    assert _enter(client).status_code == 303

    headers = {"Authorization": f"Bearer {va['admin_token']}"}
    body = client.get("/api/me/effective-access", headers=headers).json()
    assert body["is_admin"] is True

    r = client.patch("/api/me/display-name", json={"name": "Admin"}, headers=headers)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Exit
# ---------------------------------------------------------------------------


def test_exiting_restores_the_admins_own_identity(va):
    client = va["client"]
    assert _enter(client).status_code == 303
    assert client.get("/api/me/effective-access").json()["is_admin"] is False

    r = _exit(client)

    assert r.status_code == 303
    assert not client.cookies.get(VIEW_AS_COOKIE)
    assert client.get("/api/me/effective-access").json()["is_admin"] is True
    assert client.get("/api/admin/groups").status_code == 200
    assert "view-as-banner" not in client.get("/me/profile").text


def test_exit_returns_to_the_page_the_mode_was_entered_from(va):
    """The errand, finished — not abandoned wherever the browse stopped.

    The admin left the Access page to answer a question about one person. The
    banner's exit form used to post the CURRENT page back as `next`, so leaving
    from `/library` landed them on `/library` as themselves, with the person
    they were investigating forgotten and the lens closed. The origin rides the
    signed ticket now, so it survives however far the browse wandered.
    """
    client = va["client"]
    origin = f"/admin/access?lens=simulate&user={ANALYST}"
    assert _enter(client, next_path="/library", return_to=origin).status_code == 303

    # Wander: several pages deep, nowhere near where the mode was entered.
    for path in ("/library", "/me/profile", "/catalog"):
        client.get(path)

    r = _exit(client)

    assert r.status_code == 303
    assert r.headers["location"] == origin


def test_exit_falls_back_to_the_person_deep_link_when_no_origin_was_recorded(va):
    """A ticket minted before `return_to` existed is still a valid ticket.

    `_REQUIRED_KEYS` deliberately does not list it, so a deploy does not evict
    every admin mid-session. The fallback is derived from the ticket rather
    than being a bare `/admin/access`, because the picker would otherwise come
    back empty and the admin would have to find their person again.
    """
    client = va["client"]
    assert _enter(client, return_to=None).status_code == 303

    r = _exit(client)

    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/admin/access?")
    assert "lens=simulate" in location
    assert f"user={ANALYST}" in location


def test_exit_takes_no_destination_from_the_caller(va):
    """The route mounts no auth dependency, so it accepts no redirect target.

    A `next` field here would be the one attacker-influenced value on an
    endpoint that has no auth gate to lean on. Posting one must change nothing:
    the ticket's own origin still wins.
    """
    client = va["client"]
    origin = f"/admin/access?lens=simulate&user={ANALYST}"
    assert _enter(client, return_to=origin).status_code == 303
    csrf = client.cookies.get("web_csrf")

    r = client.post(
        "/admin/view-as/exit",
        data={"csrf_token": csrf, "next": "https://evil.example.com/"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == origin


def test_the_banner_exit_form_carries_no_destination(va):
    """Pins the shape, not just today's behavior.

    The bug was reintroducible by one hidden input: a banner that posts its own
    page back is a banner that can only ever return the admin to where they
    already are. If a future edit adds a destination field, this fails.
    """
    client = va["client"]
    assert _enter(client, next_path="/library").status_code == 303

    r = client.get("/library")
    assert r.status_code == 200
    banner = r.text.split('action="/admin/view-as/exit"', 1)[1].split("</form>", 1)[0]
    assert 'name="csrf_token"' in banner
    assert 'name="next"' not in banner
    assert 'name="return_to"' not in banner


class TestTheModeCanActuallyEngage:
    """A ticket that can never engage is worse than no ticket.

    The binding rule — a ticket only counts for the viewer it was minted for
    — was checked against the `access_token` cookie in three places, one of
    them an inline copy inside the read-only middleware. Under any mode that
    authenticates WITHOUT that cookie the check does not fail the ticket, it
    fails to evaluate: the mode silently never engages. Since the banner is
    the mode's only exit control, "never engages" renders as a button that
    sets a cookie, redirects to the Library, and leaves the admin on an
    ordinary page with no banner and no way back.

    Two halves, and both matter: the binding is now ONE definition that knows
    about every credential (`session_matches_viewer`), and entry refuses
    before it mints anything when no binding is possible at all.
    """

    def test_the_middleware_uses_the_one_shared_definition(self) -> None:
        """`session_matches_viewer`'s own docstring names this middleware as a
        consumer that bound the ticket at its own layer, and warns that a rule
        kept in three places will be dropped in a fourth. It was still
        inlining the check here — so the warning was about itself."""
        from pathlib import Path

        src = Path("app/middleware/view_as_readonly.py").read_text(encoding="utf-8")
        assert "session_matches_viewer(" in src
        # The retired inline copy: its own verify_token call and its own
        # bare requirement of the cookie.
        assert 'payload.get("sub")' not in src
        assert 'session_token = request.cookies.get("access_token")' not in src

    def test_binding_is_possible_needs_a_credential_to_bind_to(self, monkeypatch) -> None:
        from app.auth import view_as as va_mod

        monkeypatch.setattr(va_mod, "local_dev_viewer_id", lambda: None)
        assert va_mod.binding_is_possible("a-session-token") is True
        assert va_mod.binding_is_possible(None) is False
        assert va_mod.binding_is_possible("") is False

    def test_a_configured_identity_can_be_bound_to(self, monkeypatch) -> None:
        """The generalization, not a relaxation: dev mode authenticates from
        configuration, so the binding is checked against THAT identity rather
        than skipped."""
        from app.auth import view_as as va_mod

        monkeypatch.setattr(va_mod, "local_dev_viewer_id", lambda: "dev-user-1")
        ticket = va_mod.ViewAsTicket("dev-user-1", "dev@x", "target-1", "t@x")
        other = va_mod.ViewAsTicket("someone-else", "s@x", "target-1", "t@x")

        assert va_mod.binding_is_possible(None) is True
        # Still a binding — a ticket minted for anyone else is inert.
        assert va_mod.session_matches_viewer(None, ticket) is True
        assert va_mod.session_matches_viewer(None, other) is False
        assert va_mod.session_matches_viewer(None, None) is False

    def test_an_unreadable_session_cookie_does_not_defeat_the_fallback(self, monkeypatch) -> None:
        """The miss that shipped in the first cut of this fix.

        Browser cookies ignore the PORT, so every local instance on 127.0.0.1
        shares one jar: a developer who has opened any other Agnes on that
        host carries an `access_token` this instance cannot verify, signed by
        a different deployment's secret. A fallback that fired only on a
        MISSING cookie took the token path, failed to read it, and silently
        declined to engage — the same no-banner-no-exit outcome the fallback
        exists to prevent, reached by a different route.
        """
        from app.auth import view_as as va_mod

        monkeypatch.setattr(va_mod, "local_dev_viewer_id", lambda: "dev-user-1")
        ticket = va_mod.ViewAsTicket("dev-user-1", "dev@x", "target-1", "t@x")

        assert va_mod.session_matches_viewer("not-a-real-token", ticket) is True
        # Still a binding: a foreign ticket stays inert whatever is in the jar.
        other = va_mod.ViewAsTicket("someone-else", "s@x", "target-1", "t@x")
        assert va_mod.session_matches_viewer("not-a-real-token", other) is False

    def test_an_unreadable_session_cookie_still_fails_off_dev_mode(self, monkeypatch) -> None:
        """Where real sessions are issued there is nothing to fall back ON, so
        an unverifiable token must keep failing."""
        from app.auth import view_as as va_mod

        monkeypatch.setattr(va_mod, "local_dev_viewer_id", lambda: None)
        ticket = va_mod.ViewAsTicket("viewer-1", "v@x", "target-1", "t@x")
        assert va_mod.session_matches_viewer("not-a-real-token", ticket) is False

    def test_no_configured_identity_means_no_ticket_without_a_session(self, monkeypatch) -> None:
        """Fail-closed stays the default everywhere else."""
        from app.auth import view_as as va_mod

        monkeypatch.setattr(va_mod, "local_dev_viewer_id", lambda: None)
        ticket = va_mod.ViewAsTicket("viewer-1", "v@x", "target-1", "t@x")
        assert va_mod.session_matches_viewer(None, ticket) is False

    def test_entry_refuses_rather_than_minting_a_ticket_that_cannot_engage(self, va, monkeypatch) -> None:
        """The fix for the button that lied: refuse BEFORE setting a cookie."""
        import app.auth.view_as as va_mod

        client = va["client"]
        csrf = _mint_csrf(client)
        # No bindable credential of any kind: no configured identity, and the
        # route reads the session cookie through the same accessor.
        monkeypatch.setattr(va_mod, "local_dev_viewer_id", lambda: None)
        monkeypatch.setattr(va_mod, "binding_is_possible", lambda token: False)

        r = client.post(
            "/admin/view-as",
            data={"user_id": ANALYST, "csrf_token": csrf, "next": "/library"},
            follow_redirects=False,
        )

        assert r.status_code == 403
        assert r.json()["detail"] == "view_as_requires_interactive_session"
        assert not client.cookies.get(VIEW_AS_COOKIE)


class TestTheOfferIsWithheldWhereItCannotSucceed:
    """The caller's own row offers no view-as.

    `view_as_self` is a real 400 at the entry route — there is no view of
    yourself to open and the mode would have nothing to narrow — but the lens
    rendered the button for every person including the caller, so picking
    yourself and clicking it produced a full-page 400 carrying a machine
    token and no route back to the lens. Same rule the rest of the product
    follows: never offer what cannot succeed.
    """

    def test_the_page_tells_the_lens_who_is_asking(self, va) -> None:
        """Withholding the offer needs the caller's id client-side; without
        it the guard cannot be written at all."""
        r = va["client"].get("/admin/access", headers={"Accept": "text/html"})
        assert r.status_code == 200
        assert "const VIEWER_USER_ID = " in r.text
        assert ADMIN in r.text

    def test_the_lens_withholds_the_button_on_the_callers_own_row(self) -> None:
        from pathlib import Path

        src = Path("app/web/templates/admin_access.html").read_text(encoding="utf-8")
        # The guard, and the reason that takes the button's place.
        assert "uid === VIEWER_USER_ID" in src
        assert 'el("ax-sim-self")' in src
        assert "pick someone else to open a page as them" in src

    def test_a_stale_post_of_self_still_gets_a_sentence(self, va) -> None:
        """Defence for the case the guard cannot cover — a cached page, or a
        hand-made POST. The refusal stands; only its legibility changes."""
        client = va["client"]
        csrf = _mint_csrf(client)

        r = client.post(
            "/admin/view-as",
            data={"user_id": ADMIN, "csrf_token": csrf, "next": "/library"},
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )

        assert r.status_code == 400
        assert "There is no view of yourself to open" in r.text
        assert "/admin/access?lens=simulate" in r.text
        assert not client.cookies.get(VIEW_AS_COOKIE)


class TestARefusalDuringViewAsSaysWhy:
    """An admin page refused during a view-as must not blame the admin.

    The mode suppresses admin authority by making `elevation_paused` answer
    True for the narrowed subject, so every admin surface refused during a
    view-as arrives at the error page carrying `admin_elevation_paused` — and
    the page told the admin they had paused admin mode and offered
    "Re-enable admin mode". That action is a dead end twice: the toggle lives
    on a page that renders as the TARGET while the mode is on, and flipping it
    is a POST the read-only guard refuses.
    """

    def test_the_error_page_names_the_mode_and_not_a_paused_toggle(self, va) -> None:
        client = va["client"]
        assert _enter(client).status_code == 303

        # `Accept: text/html` on purpose: the copy under test lives in
        # error.html, and an API-shaped request gets the JSON detail instead.
        # Which of `require_admin`'s two guards fires first is not the point —
        # the page branches on a live ticket, not on the detail string, so it
        # explains the mode whichever refusal brought the reader here.
        r = client.get(
            "/admin/access",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert r.status_code == 403
        body = r.text
        assert "viewing as" in body.lower()
        assert ANALYST_EMAIL in body
        # The wrong explanation and its dead-end action are both gone.
        assert "You paused admin mode" not in body
        assert "/me/profile#admin-mode" not in body
        # The way out is the banner's own control, which this page carries.
        assert 'action="/admin/view-as/exit"' in body

    def test_the_paused_toggle_copy_survives_when_that_is_the_real_reason(self, va) -> None:
        """A positive control: without it, the branch above could be gutting
        the elevation case rather than sitting in front of it."""
        from pathlib import Path

        src = Path("app/web/templates/error.html").read_text(encoding="utf-8")
        assert "You paused admin mode for this browser" in src
        assert "/me/profile#admin-mode" in src


def test_a_hostile_origin_at_entry_is_refused_not_signed(va):
    """`return_to` arrives from a form field, so it is sanitized on the way IN.

    A rejected value must degrade to "not recorded" rather than ride along
    inside a signed blob, where the next reader would trust it for having a
    valid signature.
    """
    client = va["client"]
    for hostile in ("https://evil.example.com/", "//evil.example.com/", "/\\evil.example.com"):
        assert _enter(client, return_to=hostile).status_code == 303
        r = _exit(client)
        assert r.status_code == 303
        location = r.headers["location"]
        assert location.startswith("/admin/access?"), (hostile, location)
        assert "evil.example.com" not in location, (hostile, location)


def test_enter_redirects_only_to_an_internal_path(va):
    client = va["client"]
    r = _enter(client, next_path="//evil.example.com/")
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/") and not location.startswith("//")


# ---------------------------------------------------------------------------
# 4. Audited
# ---------------------------------------------------------------------------


def test_entering_writes_an_audit_row_naming_viewer_and_target(va):
    assert _enter(va["client"]).status_code == 303

    rows = _audit_rows("view_as.start")

    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == ADMIN, "the row is attributed to the VIEWER"
    assert ANALYST in (row["resource"] or "")


def test_exiting_writes_an_audit_row_naming_viewer_and_target(va):
    client = va["client"]
    assert _enter(client).status_code == 303
    assert _exit(client).status_code == 303

    rows = _audit_rows("view_as.end")

    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == ADMIN
    assert ANALYST in (row["resource"] or "")


def test_reads_made_while_in_view_as_are_attributed_to_the_viewer(va):
    """An audit row must never blame the target for what the viewer read.

    ``GET /api/sync/manifest`` is a self-auditing sensitive read (it is in
    ``READ_SELF_AUDITING``) whose handler writes ``user_id=user["id"]`` — the
    TARGET's id while the mode is on. The correction lives in the one place
    every row passes through, so the handler needs no view-as awareness.
    """
    client = va["client"]
    assert _enter(client).status_code == 303

    client.get("/api/sync/manifest")
    client.get("/api/me/effective-access")

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(user_id=ANALYST, limit=50)
    assert not rows, "no audit row may be attributed to the target while they are only being viewed"

    viewer_rows, _ = audit_repo().query(user_id=ADMIN, action="manifest.fetch", limit=50)
    assert len(viewer_rows) == 1, "the read landed on the VIEWER's trail"
    params = viewer_rows[0]["params"]
    if isinstance(params, str):
        import json

        params = json.loads(params)
    assert params.get("viewed_as") == ANALYST, "a row written through view-as says so"


def test_the_audit_actions_are_cataloged():
    from src.audit_events import is_cataloged

    assert is_cataloged("view_as.start")
    assert is_cataloged("view_as.end")


def test_the_routes_declare_their_audit_posture():
    from src.audit_posture import POSTURE

    assert POSTURE["POST /admin/view-as"] == "view_as.start"
    assert POSTURE["POST /admin/view-as/exit"] == "view_as.end"


# ---------------------------------------------------------------------------
# The ticket primitive itself
# ---------------------------------------------------------------------------


def test_the_narrowing_guards_are_subject_scoped(va):
    """``is_user_admin`` / ``elevation_paused`` answer False/True for the
    VIEWED identity only.

    If the narrowing were global instead, two things would break silently:
    an admin page asking "is Maria an admin?" would answer no about someone
    who is, and ``_maybe_view_as``'s own live re-check of the VIEWER would
    fail — dropping the mode the moment it was entered.
    """
    from app.auth.access import is_user_admin
    from app.auth.elevation import elevation_paused
    from app.auth.view_as import ViewAsTicket, reset_for_request, set_active_for_request

    token = set_active_for_request(
        ViewAsTicket(
            viewer_user_id=ADMIN,
            viewer_email=ADMIN_EMAIL,
            target_user_id=ADMIN2,
            target_email=ADMIN2_EMAIL,
        )
    )
    try:
        assert is_user_admin(ADMIN2) is False  # the narrowed subject
        assert elevation_paused(ADMIN2) is True
        assert is_user_admin(ADMIN) is True  # the viewer, unchanged
        assert elevation_paused(ADMIN) is False
    finally:
        reset_for_request(token)

    assert is_user_admin(ADMIN2) is True  # and nothing outlives the request


def test_ticket_roundtrip_and_tamper_detection(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
    from app.auth.view_as import sign_ticket, verify_ticket

    raw = sign_ticket(
        viewer_user_id=ADMIN,
        viewer_email=ADMIN_EMAIL,
        target_user_id=ANALYST,
        target_email=ANALYST_EMAIL,
    )
    ticket = verify_ticket(raw)
    assert ticket is not None
    assert ticket.viewer_user_id == ADMIN
    assert ticket.target_user_id == ANALYST

    assert verify_ticket(raw[:-4] + "AAAA") is None
    assert verify_ticket("") is None
    assert verify_ticket("not-a-ticket") is None


def test_a_ticket_is_not_a_session_credential(monkeypatch):
    """The ticket must never be usable as an ``access_token``: it is an
    opaque signed blob, not a JWT identity."""
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
    from app.auth.jwt import verify_token
    from app.auth.view_as import sign_ticket

    raw = sign_ticket(
        viewer_user_id=ADMIN,
        viewer_email=ADMIN_EMAIL,
        target_user_id=ANALYST,
        target_email=ANALYST_EMAIL,
    )
    assert verify_token(raw) is None


def test_banner_partial_uses_design_tokens_only():
    from pathlib import Path

    partial = Path("app/web/templates/_view_as_banner.html").read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", partial), "raw hex in the banner partial"
    assert "<style" not in partial, "banner CSS belongs in a stylesheet, not the body"


def test_every_page_layout_includes_the_banner():
    """Structural half of "a banner on every page".

    The runtime test above proves it for the pages it visits; this one closes
    the gap a future THIRD layout would open. Every template that owns a
    ``<body>`` either includes the banner partial or is on the pre-auth
    allow-list below — and a pre-auth layout is genuinely out of scope: the
    guard only engages for a request whose own session cookie names the
    ticket's viewer, so a signed-out page can never be in view-as.

    ``base.html`` is on the INCLUDED side deliberately. It is legacy and no
    new page may extend it, but three catalog detail pages still do, and a
    page that cannot show the banner must not be reachable in view-as. The
    include adds no new dependency ON base.html: the partial is shared with
    ``base_ds.html``, which is where it belongs.
    """
    from pathlib import Path

    pre_auth_layouts = {
        # Sign-in shell — no session exists, so no ticket can ever engage.
        "base_login.html",
    }

    jinja_comment = re.compile(r"\{#.*?#\}", re.DOTALL)
    body_tag = re.compile(r"<body[\s>]")
    missing = []
    for path in sorted(Path("app/web/templates").rglob("*.html")):
        text = jinja_comment.sub("", path.read_text(encoding="utf-8"))
        # A root LAYOUT owns the document: an <html> element, a real <body>
        # tag, and no parent to inherit one from. (A partial that merely
        # *mentions* end-of-<body> in a comment is not a layout.)
        if "<html" not in text or not body_tag.search(text) or "{% extends" in text:
            continue
        if path.name in pre_auth_layouts:
            continue
        if "_view_as_banner.html" not in text:
            missing.append(str(path))

    assert not missing, (
        "a page layout that cannot show the view-as banner must not be reachable in view-as:\n"
        + "\n".join(f"  {p}" for p in missing)
    )


def test_a_state_changing_get_does_not_write_to_the_target(va):
    """A GET that mutates must not mutate the TARGET because someone looked.

    The read-only middleware refuses by METHOD, so a mutating GET is invisible
    to it, and the swapped identity is a plain dict (the target's live user
    row) so it sails past `isinstance(user, PRINCIPAL_TYPES)` too — the two
    nets that catch everything else both miss this shape.

    `GET /api/sync/manifest` is the live instance of it: it stamps
    `users.last_pull_at` and emits a `sync.pull_started` usage event, both
    keyed on the caller's id. Under view-as that is the target's, so merely
    opening the page would bump their real "last pulled" — and an admin who
    entered view-as to investigate a stale pull would destroy the evidence
    they came to read.

    The audit row is deliberately NOT suppressed (see
    `test_reads_made_while_in_view_as_are_attributed_to_the_viewer`): it is
    re-attributed to the viewer, and losing it would make view-as a way to
    read a sensitive surface leaving no trace.
    """
    from src.repositories import users_repo

    client = va["client"]

    before = users_repo().get_by_id(ANALYST) or {}
    stamp_before = before.get("last_pull_at")

    assert _enter(client).status_code == 303
    assert client.get("/api/sync/manifest").status_code == 200

    after = users_repo().get_by_id(ANALYST) or {}
    assert after.get("last_pull_at") == stamp_before, (
        "view-as must not stamp the target's last_pull_at — an admin looking at someone is not that person pulling"
    )


def test_the_same_get_still_writes_when_nobody_is_being_viewed(va):
    """The positive control for the guard above.

    Without it the previous test would also pass if the stamp had simply been
    deleted, or if the manifest route had stopped working at all.
    """
    from src.repositories import users_repo

    client = va["client"]

    before = users_repo().get_by_id(ADMIN) or {}
    stamp_before = before.get("last_pull_at")

    assert client.get("/api/sync/manifest").status_code == 200

    after = users_repo().get_by_id(ADMIN) or {}
    assert after.get("last_pull_at") != stamp_before, (
        "outside view-as the stamp must still land — the guard is scoped to the mode, not a removal of the feature"
    )


def test_a_leaked_ticket_alone_cannot_forge_an_exit_audit_row(va):
    """The exit route must bind the ticket to the caller's own session.

    `verify_ticket` proves signature and expiry only, which makes the ticket a
    BEARER string. The exit handler mounts no auth dependency (deliberately —
    `get_current_user` resolves to the target while the mode is on, so
    `require_admin` would 403 the person trying to leave), and its CSRF check
    is a double-submit: it proves same-origin-browser, never identity, and an
    attacker supplies both halves themselves.

    So without a binding check, anyone holding a ticket value that escaped out
    of band — a proxy log, a HAR attached to a bug report, a shared machine —
    could POST it here and have Agnes write a `view_as.end` row attributed to
    the real admin for something they never did. `active_ticket()` is never
    stamped for such a request (the middleware's own binding check refuses to
    engage), so `apply_view_as_attribution` does not correct it either: the
    forged `user_id` is written verbatim.

    Bounded impact — no privilege gained, no data read — but audit forgery is
    exactly the primitive an audit trail exists to deny.
    """
    from src.repositories import audit_repo

    client = va["client"]
    assert _enter(client).status_code == 303
    stolen = client.cookies.get("agnes_view_as")
    assert stolen, "entering must set the ticket cookie"

    rows_before, _ = audit_repo().query(user_id=ADMIN, action="view_as.end", limit=50)

    # A different browser: the stolen ticket, and a self-supplied double-submit
    # CSRF pair, but NO session cookie of the admin's.
    from fastapi.testclient import TestClient

    attacker = TestClient(client.app)
    attacker.cookies.set("agnes_view_as", stolen)
    attacker.cookies.set("web_csrf", "attacker-chosen-value")
    attacker.post(
        "/admin/view-as/exit",
        data={"csrf_token": "attacker-chosen-value", "next": "/me/profile"},
        follow_redirects=False,
    )

    rows_after, _ = audit_repo().query(user_id=ADMIN, action="view_as.end", limit=50)
    assert len(rows_after) == len(rows_before), (
        "a ticket without a matching session must not write a view_as.end row "
        "attributed to the admin who never sent the request"
    )


def test_the_real_admin_can_still_exit_and_it_is_audited(va):
    """Positive control for the binding check above.

    Without this, the previous test would also pass if exit auditing had been
    removed outright, or if exit had been broken for everyone.
    """
    from src.repositories import audit_repo

    client = va["client"]
    assert _enter(client).status_code == 303
    assert _exit(client).status_code == 303

    rows, _ = audit_repo().query(user_id=ADMIN, action="view_as.end", limit=50)
    assert len(rows) == 1, "the genuine admin's exit is still recorded on their trail"


def test_the_configured_identity_fallback_is_off_outside_dev_mode(monkeypatch):
    """The one line the whole fallback's safety rests on, tested unmocked.

    Every other test of this fallback monkeypatches `local_dev_viewer_id`, so
    they pin what its CONSUMERS do with an answer — not that the function
    itself refuses to answer off dev mode. That gap is worth closing here
    rather than trusting the guard by reading it, because the function it
    delegates to does NOT check the mode: `_get_local_dev_user` says "when
    LOCAL_DEV_MODE is on, else None" in its docstring but its body just looks
    the configured address up and returns whatever it finds. So
    `is_local_dev_mode()` inside `local_dev_viewer_id` is the ONLY thing
    standing between a deployment that happens to hold an account at
    `get_local_dev_email()` and a `session_matches_viewer` that accepts a
    ticket carrying no verifiable session at all.

    Removing that check leaves every existing view-as test green — which is
    the definition of an untested security boundary.
    """
    from app.auth import view_as as va_mod

    # Dev mode off (the deployed case) — the answer is None even when the
    # lookup underneath would happily return a user.
    monkeypatch.setattr(va_mod, "local_dev_viewer_id", va_mod.local_dev_viewer_id)
    monkeypatch.setattr("app.auth.dependencies.is_local_dev_mode", lambda: False)
    monkeypatch.setattr("app.auth.dependencies._get_local_dev_user", lambda conn=None: {"id": "dev-user-1"})
    assert va_mod.local_dev_viewer_id() is None, (
        "local_dev_viewer_id answered off dev mode — session_matches_viewer would "
        "then accept a ticket with no verifiable session behind it"
    )

    # ...and the consequence, through the real function rather than a stub:
    # a ticket naming that account is inert without a session token.
    ticket = va_mod.ViewAsTicket("dev-user-1", "dev@x", "target-1", "t@x")
    assert va_mod.session_matches_viewer(None, ticket) is False
    assert va_mod.binding_is_possible(None) is False

    # Positive control: with the mode on, the same wiring does answer, so the
    # assertions above are about the gate and not about a broken lookup.
    monkeypatch.setattr("app.auth.dependencies.is_local_dev_mode", lambda: True)
    assert va_mod.local_dev_viewer_id() == "dev-user-1"
    assert va_mod.session_matches_viewer(None, ticket) is True
