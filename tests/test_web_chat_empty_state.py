"""Web UI route — the ``/chat`` pre-conversation Dashboard (issue #896).

The rail empty state is the Dashboard: greeting, the real composer, a
"Using N knowledge sources and M capabilities from your Stack" context
line, activity panels, and guided task starters. (Its ancestors —
the standalone ``/ask`` hero, then the ``/chat`` "Ask anything." hero with
the "Operated by Agnes" pill — are retired.) The counts are the caller's
ACTUAL Stack contents, matching the /stack page the line links to:
knowledge sources = ``StackResolver.stack()`` over data packages + memory
domains (``_stack_knowledge_source_count``); capabilities = the
``?tab=my`` plugin roster — subscribed/required curated plugins ∩ RBAC,
plus Store installs (``_stack_capability_count``). NOT everything the
caller could browse or add. These tests render ``/chat``'s rail empty
state and assert that counting + pluralization.

Rendering it needs three things: rail layout, an enabled chat backend,
and CHAT *access* (admin clears it via god-mode; a normal user needs a
``chat`` grant to pass the route's default-deny guard).
"""

from __future__ import annotations

from types import SimpleNamespace


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _grant(
    group_name: str,
    resource_type: str,
    resource_id: str,
    requirement: str = "available",
    users: list[str] | None = None,
) -> None:
    """Add a resource_grants row for the named user-group.

    Mirrors the helper in ``tests/test_web_catalog_unified.py`` — also
    ensures ``users`` are members of the group (seeded_app only puts
    admin1 in the Admin group; everyone else starts with zero memberships).
    """
    import uuid
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()
        if not gid:
            return
        group_id = gid[0]
        if users:
            members = UserGroupMembersRepository(conn)
            for u in users:
                try:
                    members.add_member(u, group_id, source="test")
                except Exception:
                    pass
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), group_id, resource_type, resource_id, requirement],
        )
    finally:
        conn.close()


def _enable_rail_chat(seeded_app, monkeypatch) -> None:
    """Make ``/chat`` render its rail empty-state hero: rail chrome + an
    enabled chat backend. Callers still need CHAT *access* — admin via
    god-mode, or a ``_grant(..., "chat", "chat", ...)`` for a normal user.

    Auto-membership rides along: the Dashboard is a redesign surface and
    these tests pin the redesign experience, where the stack mode is enabled
    together with the chrome (spec 2026-08-07-default-chrome-ux-parity) —
    the seeded ``available`` grants below only count into the context line
    under that mode.

    Also reports the instance as having REGISTERED TABLES. These tests call
    ``/chat`` with the admin token against a seeded instance that has none, and
    in that state the page carries the "no data is registered yet" admin notice
    (see ``admin_notice`` in chat.html) — correct product behaviour, but not the
    state any test in this file is about. Pinning it here keeps every assertion
    in the file talking about the ordinary landing page; the notice's own
    behaviour is covered by ``TestRailDashboard`` in
    tests/test_ui_layout_theme.py."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")
    seeded_app["client"].app.state.chat_config = SimpleNamespace(enabled=True)

    import app.services.admin_dashboard as admin_dashboard

    monkeypatch.setattr(
        admin_dashboard,
        "resolve_journey",
        lambda: {
            "setup": {
                "steps": [{"key": "tables", "done": True, "failed": False, "cta": "x", "href": "/x"}],
                "complete": False,
                "done_count": 1,
                "total": 6,
                "summary": "",
            }
        },
    )


class TestChatEmptyStatePill:
    def test_renders_the_dashboard(self, seeded_app, monkeypatch):
        """Rail ``/chat`` empty state renders the Dashboard — greeting, heading,
        lede, the guided task starters and the two doors.

        It used to assert a "Using N … from your Stack" line here too. That line
        is retired: it reported a count the reader could not act on, in the one
        row between the composer and its suggestions."""
        _enable_rail_chat(seeded_app, monkeypatch)
        c = seeded_app["client"]
        resp = c.get("/chat", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        body = resp.text
        # The page introduces itself in TEXT, not in a banner: greeting, the
        # "Ask Agnes anything" heading, then one factual sentence about what it
        # answers. The Knowledge Layer hero that used to lead here is retired —
        # it asserted a category rather than a capability, and its "Connect your
        # tools" CTA read as sending data INTO Agnes.
        assert 'class="cld-greet"' in body
        assert "Ask Agnes anything" in body
        assert 'class="cld-lede"' in body
        assert "shows you where each answer came from" in body
        # The doors at the foot carry the routes the hero carried, with the tools
        # one naming the DIRECTION rather than "Connect your tools".
        assert 'class="cld-doors"' in body
        assert "Take Agnes to your tools" in body
        # The tool NAMES stay (they are the useful half of the retired hero, and
        # the door names them in its description); the floating chip markup that
        # carried them is what went with the banner.
        assert "Claude Code" in body
        assert "Secure. Private. Always in sync." in body
        # The hero's whole `.klb-*` class family is deleted, not merely uncalled
        # (macros/_knowledge_layer.html and its ~230 lines of CSS are gone), so
        # the guard is the prefix rather than a list of the classes anyone
        # happened to think of.
        assert "klb" not in body, "the retired hero's markup is back"
        for retired in ("Agnes is your knowledge layer.", "Connect your tools"):
            assert retired not in body, f"the retired hero's copy is back: {retired}"
        assert 'id="rdb-actions"' in body
        # The suggestions carry a label again, and it names the one thing the
        # chips cannot say about themselves — that they are derived from what
        # this caller can reach.
        assert "Suggested for you" in body
        # No Stack count line, and no row under the composer to hold one.
        assert 'class="rdb-context"' not in body
        assert "cloud-chat-composer-foot" not in body
        # The retired hero copy must be gone.
        assert "Ask anything." not in body
        assert "Operated by" not in body
        assert "Suggested questions" not in body

    def test_below_the_input_is_the_suggestions_and_nothing_else(self, seeded_app, monkeypatch):
        """Below the composer carries the suggested questions and nothing else —
        no onboarding, no marketing, no documentation link.

        The rule protects the INTENT ZONE: the span a reader crosses between
        having a question and the suggestions that help them phrase it, where a
        navigate-away control is a detour. It used to also allow two readouts
        here (the Stack count line and the agent picker); the picker moved into
        the composer pill and the count line is retired, so the zone is now
        strictly empty of everything but the chips.
        """
        _enable_rail_chat(seeded_app, monkeypatch)
        resp = seeded_app["client"].get("/chat", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        body = resp.text
        below = body[body.index("</form>") :]
        below = below[: below.index('id="rdb-actions-list"')]
        for retired in ("rdb-orient", "New here?", 'href="/how-it-works"', 'class="rdb-context"'):
            assert retired not in below, f"below-input area is not suggestions-only: {retired}"
        # The picker is in the composer, not under it.
        composer = body[body.index('class="cloud-chat-composer"') : body.index("</form>")]
        assert 'id="chat-agent-btn"' in composer

    def test_the_cards_sit_by_INSTANCE_STATE_not_by_audience(self, seeded_app, monkeypatch):
        """The two cards close the page — after the composer and after the
        suggestions — for admin and member alike. The reader came to ask
        something and every card navigates away from the composer, so the ways
        out belong past the thing they came for.

        The one inversion is an instance with NOTHING REGISTERED: there is
        nothing to ground an answer in, so "Add your first data" is the page's
        real action and leads it (covered by the admin-notice tests in
        tests/test_ui_layout_theme.py, which render that state).

        This was gated on AUDIENCE for one release (`admin_setup`) so that an
        admin's lead card — a job, with setup progress on it — led the page on
        every instance. The cost was that a fully configured instance still put
        a setup card between an admin and the input. The gate is instance state
        now, the same condition the heading and lede read.

        The trust line travels with the cards either way.
        """
        _enable_rail_chat(seeded_app, monkeypatch)
        c = seeded_app["client"]

        # The seeded instance has a registered table (see _enable_rail_chat), so
        # this is the ordinary state for both readers.
        admin = c.get("/chat", headers=_auth(seeded_app["admin_token"])).text
        assert admin.index('id="chat-form"') < admin.index('class="cld-doors"'), (
            "an admin's cards belong after the suggestions on an instance with data"
        )
        assert admin.index('id="rdb-actions"') < admin.index('class="cld-doors"')
        assert admin.index('class="cld-doors"') < admin.index('class="cld-trust"')

        _grant("Everyone", "chat", "chat", users=["analyst1"])
        member = c.get("/chat", headers=_auth(seeded_app["analyst_token"])).text
        assert member.index('id="chat-form"') < member.index('class="cld-doors"'), (
            "a member's cards belong after the suggestions"
        )
        assert member.index('id="rdb-actions"') < member.index('class="cld-doors"')
        assert member.index('class="cld-doors"') < member.index('class="cld-trust"')

        # Exactly one of each, in both — the two emission points are mutually
        # exclusive, not additive.
        for who, body in (("admin", admin), ("member", member)):
            assert body.count('class="cld-doors"') == 1, who
            assert body.count('class="cld-trust"') == 1, who

    def test_orientation_routes_live_in_the_doors_row(self, seeded_app, monkeypatch):
        """The first-run path to /how-it-works, which the rail otherwise carries
        only as a quiet ``.rail-meta`` row in its foot (_app_rail.html).

        It used to be a text link under the hero's "Connect your tools" CTA.
        With that hero retired it is one of TWO labelled doors at the foot of the
        empty state — still a plain link, still not a button, and now beside the
        one other route off this page instead of competing with a banner CTA
        above the composer.

        "How {brand} works" is no longer a door at all: it is reference reading,
        not a move, so it sits in the foot line beside the trust caption where a
        third card of equal weight used to imply it was a comparable choice.
        """
        _enable_rail_chat(seeded_app, monkeypatch)
        resp = seeded_app["client"].get("/chat", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        body = resp.text
        doors = body[body.index('class="cld-doors"') :]
        doors = doors[: doors.index("</nav>")]
        # Both routes, and each says where it goes. The FIRST card is
        # audience-dependent: an admin gets "Set up Agnes" (the job only they
        # can do) where a member gets the Library door, so this test — which
        # signs in as an admin — expects the setup card, and the member variant
        # is covered in tests/test_ui_layout_theme.py.
        assert "cld-door--setup" in doors
        assert "Set up Agnes" in doors
        assert 'href="/how-it-works#connect"' in doors
        assert "Take Agnes to your tools" in doors
        # The third card is retired. Its destination survives as a foot link,
        # OUTSIDE this row — a card gave reference reading the same weight as
        # setting the instance up.
        assert "How Agnes works" not in doors
        assert 'class="cld-trust-link" href="/how-it-works"' in body
        assert "See how Agnes works" in body
        # Links, not buttons — the composer above is the page's only action.
        # (`klb-cta` was the hero CTA's class; it is deleted, and the body-wide
        # prefix guard above covers it now.)
        assert "btn btn-primary" not in doors, "a door rendered as a button"

    def test_requires_login(self, seeded_app):
        """Same auth gate as every other authenticated page — unauthenticated
        requests redirect to /login rather than rendering (TestClient
        follows redirects by default, so assert on the pre-redirect hop)."""
        c = seeded_app["client"]
        resp = c.get("/chat", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/login")

    def test_rail_empty_state_has_no_semantic_layer_detour(self, seeded_app, monkeypatch):
        """The empty state offers NO browse link to /catalog/semantics.

        It carried one (#1108) directly above the composer. Retired: the empty
        state is the moment of intent — the reader came to ask something — so a
        control whose only function is to navigate away from the composer,
        offered before they have an answer to check, is a detour. The semantic
        layer is reached from the rail's Definitions nav row, the Library's
        Definitions band, and search.

        Asserted on the button's own wrapper class rather than the bare
        `/catalog/semantics` URL: the rail chrome carries a Definitions nav row
        on every page, this one included, so the URL is legitimately in the
        markup. What must not come back is the in-page control.
        """
        _enable_rail_chat(seeded_app, monkeypatch)
        c = seeded_app["client"]
        resp = c.get("/chat", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "rdb-semantic-links" not in resp.text
        assert "Browse metrics" not in resp.text
