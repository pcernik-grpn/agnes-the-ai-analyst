"""Running a named agent from web chat (AGT-6).

The headline complaint was "agents can't be used — their tab only configures
them". The runtime was never missing: ``ChatManager.create_session(agent_id=)``
and ``build_profile`` are surface-agnostic and are exactly what
``POST /api/v1/agents/{slug}/sessions`` already used. Web chat was simply never
wired to them, so every browser session ran as the caller's default agent
whatever they had built.

Three things worth pinning beyond "it works":

* **Ownership.** Naming another user's slug must not hand the caller a persona
  assembled from grants that are not theirs. 404, not 403 — a distinct status
  would confirm that someone else's agent exists.
* **The default path is untouched.** No ``agent_slug`` still resolves to the
  caller's default, because every existing session and its attribution depend
  on that.
* **Sharing (C2.3).** An agent shared to a group the caller belongs to
  (``ResourceType.AGENT``) now DOES resolve for web chat — addressed by the
  agent's id, since a slug is only meaningful in its owner's own namespace.
  See ``docs/superpowers/plans/2026-08-26-one-agent-model.md`` §C2.3.
"""

from __future__ import annotations

import re

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _chat_granted():
    """Chat is an RBAC resource (``require_resource_access(CHAT, "chat")``).

    Granted to Everyone here rather than stubbed, so these run through the same
    gate a browser does — the point of the feature is the real request path.
    """
    import uuid

    from src.db import get_system_db

    conn = get_system_db()
    grp = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()
    if grp:
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'chat', 'chat', 'available', CURRENT_TIMESTAMP, 'test') "
            "ON CONFLICT DO NOTHING",
            [str(uuid.uuid4()), grp[0]],
        )
    conn.close()


def _make_agent(seeded_app, token: str, name: str) -> dict:
    resp = seeded_app["client"].post("/api/v1/agents", json={"name": name}, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _chat_or_skip(seeded_app, **body):
    """POST a session, skipping the test when chat is disabled in this env."""
    resp = seeded_app["client"].post("/api/chat/sessions", json=body, headers=_auth(seeded_app["analyst_token"]))
    if resp.status_code in (403, 503):
        pytest.skip(f"chat unavailable in this environment ({resp.status_code})")
    return resp


class TestResolverOwnership:
    """``_resolve_agent_id`` direct — the security-relevant half.

    Tested at the function rather than only through the endpoint because chat
    is RBAC-gated and not enabled for every environment, and a skipped test
    proves nothing about who may run whose agent.

    C2.3 (shared-agent runtime) widened this from "owned only" to "owned or
    shared via a ResourceType.AGENT grant" — see
    ``test_a_shared_agents_id_resolves_for_a_grantee`` below for the new
    half; the un-shared cases below are unchanged.
    """

    def test_my_own_slug_resolves_to_my_agent(self, seeded_app):
        from app.api.chat import _resolve_agent_id

        agent = _make_agent(seeded_app, seeded_app["analyst_token"], "Mine To Run")
        got = _resolve_agent_id(agent["slug"], {"id": "analyst1", "email": "analyst@example.com"})
        assert got == agent["id"]

    def test_another_users_slug_does_not_resolve(self, seeded_app):
        from fastapi import HTTPException

        from app.api.chat import _resolve_agent_id

        theirs = _make_agent(seeded_app, seeded_app["admin_token"], "Not Yours")
        with pytest.raises(HTTPException) as exc:
            _resolve_agent_id(theirs["slug"], {"id": "analyst1", "email": "a@example.com"})
        assert exc.value.status_code == 404, "a foreign slug resolved, or leaked its existence with a 403"

    def test_a_shared_agents_id_resolves_for_a_grantee(self, seeded_app):
        """A grantee cannot borrow the owner's SLUG (that namespace is the
        owner's alone — see ``test_another_users_slug_does_not_resolve``
        above), but resolves fine by the agent's id once shared to a group
        they belong to."""
        from app.api.chat import _resolve_agent_id
        from src.db import get_system_db
        from src.repositories.resource_grants import ResourceGrantsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.user_groups import UserGroupsRepository

        theirs = _make_agent(seeded_app, seeded_app["admin_token"], "Shared To Me")

        conn = get_system_db()
        grp = UserGroupsRepository(conn).create(name="chat-shared-agent-grp", created_by="admin1")
        UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="admin1")
        ResourceGrantsRepository(conn).create(grp["id"], "agent", theirs["id"], assigned_by="admin1")
        conn.close()

        got = _resolve_agent_id(theirs["id"], {"id": "analyst1", "email": "analyst@test.com"})
        assert got == theirs["id"]

    def test_an_unknown_slug_does_not_resolve(self):
        from fastapi import HTTPException

        from app.api.chat import _resolve_agent_id

        with pytest.raises(HTTPException) as exc:
            _resolve_agent_id("no-such-agent", {"id": "analyst1", "email": "a@example.com"})
        assert exc.value.status_code == 404

    def test_no_slug_falls_back_to_the_default_agent(self):
        from app.api.chat import _resolve_agent_id
        from src.repositories import agents_repo

        got = _resolve_agent_id(None, {"id": "analyst1", "email": "a@example.com"})
        assert got == agents_repo().get_or_create_default("analyst1")["id"]


class TestSpawnAsNamedAgent:
    def test_a_session_can_run_as_one_of_my_agents(self, seeded_app):
        agent = _make_agent(seeded_app, seeded_app["analyst_token"], "Release Writer")
        resp = _chat_or_skip(seeded_app, surface="web", agent_slug=agent["slug"])
        assert resp.status_code == 201, resp.text

        from src.repositories import chat_sessions_repo

        row = chat_sessions_repo().get(resp.json()["id"])
        assert row is not None
        assert row.get("agent_id") == agent["id"], (
            "the session was attributed to the default agent, so the persona the caller built is not the one running"
        )

    def test_no_slug_still_uses_the_default_agent(self, seeded_app):
        """The pre-existing path, unchanged — every old session depends on it."""
        resp = _chat_or_skip(seeded_app, surface="web")
        assert resp.status_code == 201, resp.text

        from src.repositories import agents_repo, chat_sessions_repo

        row = chat_sessions_repo().get(resp.json()["id"])
        default_id = agents_repo().get_or_create_default("analyst1")["id"]
        assert row.get("agent_id") == default_id


class TestOwnership:
    def test_an_unknown_slug_is_refused(self, seeded_app):
        resp = _chat_or_skip(seeded_app, surface="web", agent_slug="no-such-agent")
        assert resp.status_code == 404

    def test_another_users_agent_is_refused(self, seeded_app):
        """Owner-scoped lookup: the admin's agent must not resolve for the
        analyst by SLUG — a slug is only meaningful in its owner's own
        namespace (see ``test_a_shared_agent_is_accepted_by_id`` below for
        the id-shaped, actually-shared case)."""
        theirs = _make_agent(seeded_app, seeded_app["admin_token"], "Admins Agent")
        resp = _chat_or_skip(seeded_app, surface="web", agent_slug=theirs["slug"])
        assert resp.status_code == 404, (
            "a foreign slug resolved — the caller would get a persona built on grants that are not theirs"
        )

    def test_a_shared_agent_is_accepted_by_id(self, seeded_app):
        """C2.3: once shared (a ``ResourceType.AGENT`` grant via a group the
        caller belongs to), a non-owner CAN open a web-chat session against
        the agent — addressed by its id, since its slug lives in the
        owner's namespace."""
        from src.db import get_system_db
        from src.repositories.resource_grants import ResourceGrantsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.user_groups import UserGroupsRepository

        theirs = _make_agent(seeded_app, seeded_app["admin_token"], "Shared Via Chat")

        conn = get_system_db()
        grp = UserGroupsRepository(conn).create(name="chat-shared-agent-e2e-grp", created_by="admin1")
        UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="admin1")
        ResourceGrantsRepository(conn).create(grp["id"], "agent", theirs["id"], assigned_by="admin1")
        conn.close()

        resp = _chat_or_skip(seeded_app, surface="web", agent_slug=theirs["id"])
        assert resp.status_code == 201, resp.text

        from src.repositories import chat_sessions_repo

        row = chat_sessions_repo().get(resp.json()["id"])
        assert row.get("agent_id") == theirs["id"]

    def test_a_restricted_principal_is_refused_not_crashed(self):
        """An agent session may not open a chat session — 403, not 500.

        `require_resource_access` hands back a frozen dataclass for a restricted
        principal, so the handler's `user["id"]` raises TypeError and the caller
        used to get a 500. The hazard predates `agent_slug`; that field is what
        gives it teeth, since naming a slug is how one agent would pick up
        another agent's persona. Asserted at the guard rather than end-to-end:
        the route is what has to refuse, whichever principal type arrives.
        """
        from fastapi import HTTPException

        from app.api.chat import _reject_restricted_principal
        from app.auth.session_principal import PRINCIPAL_TYPES

        assert PRINCIPAL_TYPES, "no restricted principal types to guard against"
        for cls in PRINCIPAL_TYPES:
            probe = object.__new__(cls)  # no ctor args — only isinstance matters
            with pytest.raises(HTTPException) as exc:
                _reject_restricted_principal(probe, "start a conversation")
            assert exc.value.status_code == 403

        # And a normal dict caller passes straight through.
        _reject_restricted_principal({"id": "u1", "email": "a@b.c"}, "start a conversation")


class TestTheWiringIsPresent:
    """The page half — a card that cannot be clicked is still unusable."""

    @pytest.fixture(autouse=True)
    def _rail(self, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")

    def test_the_agents_page_offers_a_chat_action(self, seeded_app):
        resp = seeded_app["client"].get("/agents", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "/chat?agent=" in resp.text, "no way to reach an agent from its card"

    def test_the_work_in_progress_caveat_is_gone(self, seeded_app):
        """It apologised for exactly the thing that now works."""
        resp = seeded_app["client"].get("/agents", headers=_auth(seeded_app["analyst_token"]))
        assert "Work in progress" not in resp.text
        assert "Actually running them" not in resp.text


class TestAgentSlugDeepLinkDoesNotRaceASessionDeepLink:
    """Static-source guard, same shape as test_chat_surface_badge.py.

    `_maybeOpenInitialSession` nulls `_initialSessionId` synchronously but
    defers the actual `openSession` call into a `requestAnimationFrame`
    callback — so `currentChatId` is still unset immediately after calling
    it, even when a session deep-link (`data-initial-session`) is about to
    open. The `?agent=` spawn must check whether that deep-link EXISTED, not
    only `currentChatId`, or the two race and open two sessions.
    """

    def _js(self) -> str:
        from pathlib import Path

        return Path("app/web/static/js/chat.js").read_text(encoding="utf-8")

    def test_agent_slug_spawn_is_gated_on_a_pending_session_deep_link(self):
        js = self._js()
        assert "_hadInitialSession" in js
        marker = "if (_agentSlug && !currentChatId && !_hadInitialSession)"
        assert marker in js, "the agent-slug spawn no longer checks the captured pre-open flag"

    def test_the_flag_is_captured_before_the_deep_link_is_consumed(self):
        js = self._js()
        capture = "const _hadInitialSession = !!_initialSessionId;"
        idx_capture = js.index(capture)
        idx_open_call = js.index("_maybeOpenInitialSession();", idx_capture)
        # Must read _initialSessionId before the call that nulls it — reading
        # it after would always see null and defeat the guard.
        assert idx_capture < idx_open_call


class TestEveryChatRouteRefusesARestrictedPrincipal:
    """`require_resource_access` hands back a FROZEN DATACLASS for a restricted
    principal, so any handler that subscripts `user` returns 500 where 403 is
    the answer. Seven routes on this router carry `_reject_restricted_principal`
    for that reason; `GET /sessions` did not, which surfaced while adding the
    `agent_id` projection to it.

    Asserted structurally over the whole router rather than one route at a time:
    the failure mode is a route that FORGETS the guard, and a per-route test can
    only ever pin the routes someone remembered to write one for.
    """

    #: Routes that subscript `user` and do NOT yet carry the guard. These are
    #: NOT blessed — each needs a co-presence decision that this change is the
    #: wrong place to make, because for these three a restricted principal may
    #: have a legitimate meaning and adding the guard blind would break
    #: co-drive rather than harden it:
    #:
    #:   reissue_ticket  — a co-session participant plausibly needs a WS ticket
    #:                     to attach to the conversation it is party to.
    #:   list_messages   — likewise for reading the transcript it is party to.
    #:   archive_session — a mutation, so probably SHOULD be guarded, but it is
    #:                     the owner's shelf and the answer depends on the same
    #:                     co-presence model as the two above.
    #:
    #: The set is a RATCHET BASELINE, not an allow-list to grow: it exists so a
    #: NEW route cannot quietly join them. Shrink it, never extend it.
    UNRESOLVED = frozenset({"reissue_ticket", "list_messages", "archive_session"})

    def test_no_new_handler_subscripts_user_without_the_guard(self):
        import inspect

        from app.api import chat as chat_api

        src = inspect.getsource(chat_api)
        # Split on handler definitions so each body can be inspected alone.
        bodies = src.split("\n@router.")[1:]
        missing = set()
        for body in bodies:
            if "async def " not in body:
                continue
            name = body.split("async def ", 1)[-1].split("(", 1)[0]
            if 'user["' in body and "_reject_restricted_principal(user" not in body:
                missing.add(name)
        new_gaps = missing - self.UNRESOLVED
        assert not new_gaps, (
            "these chat handlers subscript `user` without refusing a restricted "
            f"principal first, so they 500 instead of 403: {sorted(new_gaps)}"
        )
        # And the baseline must shrink, not rot: a name that no longer has the
        # gap has to leave the set, or the set stops describing anything.
        assert self.UNRESOLVED <= missing, (
            f"UNRESOLVED names a handler that now carries the guard — remove it: {sorted(self.UNRESOLVED - missing)}"
        )


class TestTheSessionSaysWhichAgentItRunsAs:
    """``agent_id`` on the wire.

    The column has existed since v101 and both backends already round-trip it
    into ``ChatSession``; it was simply never projected into a response. That
    omission is what made the composer's agent picker impossible: a client that
    reopens a conversation had no way to learn who it was with, so the control
    could neither label it nor disable itself honestly.
    """

    def test_creating_a_session_reports_its_agent(self, seeded_app):
        agent = _make_agent(seeded_app, seeded_app["analyst_token"], "Named On The Wire")
        resp = _chat_or_skip(seeded_app, surface="web", agent_slug=agent["slug"])
        assert resp.status_code == 201, resp.text
        assert resp.json().get("agent_id") == agent["id"]

    def test_the_default_path_reports_the_default_agent(self, seeded_app):
        """Never null: an unnamed web session is attributed to the default
        agent, so a client tells "named" from "default" by comparing against the
        ``is_default`` row in ``GET /api/v1/agents`` — not by null-checking here."""
        resp = _chat_or_skip(seeded_app, surface="web")
        assert resp.status_code == 201, resp.text

        from src.repositories import agents_repo

        assert resp.json().get("agent_id") == agents_repo().get_or_create_default("analyst1")["id"]

    def test_listing_sessions_reports_each_agent(self, seeded_app):
        agent = _make_agent(seeded_app, seeded_app["analyst_token"], "Listed On The Wire")
        created = _chat_or_skip(seeded_app, surface="web", agent_slug=agent["slug"])
        assert created.status_code == 201, created.text

        listed = seeded_app["client"].get("/api/chat/sessions", headers=_auth(seeded_app["analyst_token"]))
        assert listed.status_code == 200, listed.text
        row = next((s for s in listed.json() if s["id"] == created.json()["id"]), None)
        assert row is not None, "the just-created session is missing from the list"
        assert row.get("agent_id") == agent["id"], (
            "the sidebar cannot tell which agent a conversation is with, so reopening it "
            "cannot restore the picker's label"
        )


class TestTheChatWindowSaysWhoYouAreTalkingTo:
    """Static-source guards, same rationale as the deep-link class above: the
    behaviour lives in browser JS the Python suite cannot execute, but the
    invariants that make it correct are all visible in the source.

    The design these pin REPLACED a picker inside the composer — a "Default"
    pill beside Send. Two things were wrong with it and neither was the label.
    Choosing an agent calls ``newChat(slug)``: it POSTs a NEW session and
    abandons the one on screen, so a control living inside the input promised
    to adjust the message being composed and did the opposite. And the binding
    is permanent (scope, memory notebook, pinned model and token budget are
    fixed at session creation), so there was never a version of it that could
    re-point a conversation. The two jobs it conflated are separated now:
    CHOOSING is one small selector below the composer, SAYING WHO is the
    hero and the thread header.
    """

    def _js(self) -> str:
        from pathlib import Path

        return Path("app/web/static/js/chat.js").read_text(encoding="utf-8")

    def _html(self) -> str:
        from pathlib import Path

        return Path("app/web/templates/chat.html").read_text(encoding="utf-8")

    def _css(self) -> str:
        from pathlib import Path

        return Path("app/web/static/css/chat.css").read_text(encoding="utf-8")

    def test_the_composer_carries_no_agent_control(self):
        """Nothing inside the pill may offer an agent choice.

        Asserted against the template rather than a rendered ``/chat``: the page
        needs a live ``chat_config`` on app state, which the seeded test app does
        not build, so rendering it here would test the fixture.
        """
        html = self._html()
        css = self._css()
        for gone in ('id="chat-agent-btn"', 'id="chat-agent-menu"', 'id="chat-agent-label"'):
            assert gone not in html, f"the composer agent picker is back: {gone}"
        for gone_css in (".cloud-chat-agent-btn", ".cloud-chat-agent-menu", ".cloud-chat-agent-wrap"):
            assert gone_css not in css, f"dead picker styling left behind: {gone_css}"
        js = self._js()
        for gone_js in ("_syncAgentPicker", "_renderAgentMenu", "_agentPillLabel", "initAgentPicker"):
            assert gone_js not in js, f"dead picker code left behind: {gone_js}"
        # The retired composer footer row stays retired.
        assert ".cloud-chat-composer-foot {" not in css, "the footer row is back"
        assert ".rdb-context {" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    def test_the_empty_state_offers_one_small_agent_selector(self):
        """Choosing, put where choosing belongs — in a shape whose footprint does
        not depend on how many agents exist.

        Two earlier shapes are pinned here by their absence. A pill INSIDE the
        composer beside Send: its position promised it adjusted the message
        being composed, when clicking it abandoned that session for a new one.
        Then a ROW OF CHIPS, one per agent: right for three and wrong for
        fifteen — it grew with the list, wrapped over the composer it was meant
        to sit under, and needed a cap plus a "+11 more" link, which is a list
        apologising for being a list.
        """
        html = self._html()
        assert 'id="chat-agent-select"' in html, "no way to choose an agent from the chat page"
        assert 'id="chat-agent-select-menu"' in html
        assert html.index("</form>") < html.index('id="chat-agent-select"'), (
            "the selector is back inside the composer"
        )
        assert html.index('id="chat-agent-select"') < html.index('id="chat-empty-extras"')
        # It belongs to the empty state: an agent is bound at session creation,
        # so once there are turns there is nothing left to choose.
        css = self._css()
        assert "#chat-capabilities[hidden] ~ #chat-agent-select" in css, (
            "the selector survives into a live conversation, where it cannot work"
        )
        # And no trace of the chip row it replaced.
        assert "cloud-chat-agent-starter" not in css
        assert "cloud-chat-agent-starter" not in html

    def test_the_panel_does_not_grow_with_the_agent_list(self):
        """Fifteen agents must cost the page nothing: the list scrolls inside a
        fixed panel, and the filter appears only once the list is long enough to
        need one — a search box over five names is furniture."""
        css = self._css()
        block = css.split(".cloud-chat-agent-select-list {", 1)[1].split("}", 1)[0]
        assert "max-height" in block and "overflow-y: auto" in block, (
            "the panel grows with the number of agents"
        )
        js = self._js()
        assert "AGENT_FILTER_THRESHOLD" in js, "the filter is unconditional"
        assert "filter.hidden = rowCount <= AGENT_FILTER_THRESHOLD" in js
        # Matched on the role too, so an agent named for its subject rather than
        # its job is still findable by what it does.
        assert 'a.role || ""' in js

    def test_the_panel_is_shaped_like_the_rest_of_the_app(self):
        """Two rendering bugs, both worth a guard.

        `--ds-radius-lg` / `--ds-radius-md` are NOT in the token set, and a
        `var()` to an undefined property with NO fallback voids the whole
        declaration — which is how this panel shipped with square corners inside
        a rounded design. The same trap is documented on
        `.cloud-chat-system-note`; the radii come from `--radius-*`
        (style-custom.css).

        And the shared focus ring is 2px at +2px OFFSET, which is right for a
        control with room around it and wrong for one inset 5px from a panel
        edge: it painted across the padding and over the panel's own border,
        reading as a second blue outline around the whole dropdown.
        """
        css = self._css()
        panel = css.split(".cloud-chat-agent-select-menu {", 1)[1].split("}", 1)[0]
        assert "border-radius: var(--radius-lg)" in panel, "square corners on the panel"
        # Comments stripped first: the rule explains the very token it must not
        # USE, so a raw substring check matched the explanation.
        assert "--ds-radius" not in re.sub(r"/\*.*?\*/", "", panel, flags=re.S), (
            "a var() to a token that does not exist voids the declaration it is in"
        )
        block = css.split(".cloud-chat-agent-select-filter:focus,", 1)[1].split("}", 1)[0]
        assert "outline: none" in block and "inset" in block, (
            "the filter's focus ring paints outside the field, onto the panel edge"
        )

    def test_the_panel_flips_when_it_would_run_past_the_fold(self):
        """The control sits low in the empty state — under the composer, with
        the two doors beneath — so on a laptop viewport the panel ran past the
        bottom and its last rows and "Manage agents" could not be reached.
        Measured at open time rather than guessed at with a media query: what
        matters is this button's distance to the fold, which varies with the
        greeting, the suggestions and whether the instance has any data."""
        js = self._js()
        assert "window.innerHeight - btn.getBoundingClientRect().bottom" in js
        assert 'menu.classList.add("is-up")' in js
        assert ".cloud-chat-agent-select-menu.is-up" in self._css()

    def test_the_selector_is_a_switch_not_a_one_way_door(self):
        """The default agent leads the list, under the instance's brand.

        It was left out for one iteration on the reasoning that it IS the plain
        chat and needs no entry. The cost was that starting a chat with an agent
        stranded you in it: the only way back was "+ New chat", which says
        nothing about agents and reads as "throw this away".

        The entry must not depend on the default agent's ROW existing — it is
        seeded lazily, on the owner's first session as the default, so a caller
        whose sessions have all been with named agents has none and would lose
        the way back in exactly the state that needs it. A slugless entry falls
        through to `newChat()` with no agent, the same request "+ New chat"
        makes.
        """
        js = self._js()
        block = js[js.index("function _agentSelectRows()") :]
        block = block[: block.index("\n}\n")]
        assert "_defaultAgent() || {is_default: true" in block, (
            "no way back to the default agent when its row has not been seeded yet"
        )
        assert "newChat(a.slug || undefined)" in js, (
            "the slugless default entry must post the plain create, not agent_slug=null"
        )
        html = self._html()
        assert "data-brand=" in html[html.index('id="chat-agent-select"') :][:250], (
            "the selector cannot name the default agent without the brand; the "
            'seeded row is called "Default", which names the mechanism'
        )
        sync = js[js.index("function _syncAgentSelect()") :]
        sync = sync[: sync.index("\n}\n")]
        assert "wrap.hidden = _agentSelectRows().length < 2;" in sync, (
            "one entry is not a choice — a control offering only the agent the "
            "composer above already talks to cannot change anything"
        )

    def test_the_list_leads_with_the_agents_the_caller_actually_uses(self):
        """With fifteen agents the panel scrolls, so what sits at the top is the
        whole question. Ranked by the caller's own most recent conversation with
        each, read off the sidebar cache that already carries `agent_id` and
        `last_message_at` — "the ones you were just talking to" beats both
        alphabetical and creation order."""
        js = self._js()
        block = js[js.index("function _rankedNamedAgents()") :]
        block = block[: block.index("\n}\n")]
        assert "_sessionsCache" in block and "last_message_at" in block, (
            "the ordering no longer reads the caller's own conversations"
        )

    def test_the_control_says_it_is_about_agents(self):
        """A name and a caret under the composer read as a model or mode switch
        — which is exactly how the control this replaced was read. The rail's
        own Agents glyph is what says otherwise."""
        html = self._html()
        assert "cloud-chat-agent-select-ico" in html, "the selector carries no agent glyph"
        assert "Manage agents" in html, "no route to the page that lists them all"

    def test_an_agents_chat_withholds_the_instance_suggestions(self):
        """The suggested questions are computed from what this DEPLOYMENT holds
        and offered to everyone. Under a heading reading "You're chatting with
        Delivery Health" they read as that agent's own suggestions, which is a
        claim nothing behind them supports — and an agent has no authored
        suggestions of its own yet. None beats four wrong ones."""
        js = self._js()
        assert 'main.classList.toggle("has-agent-intro", !!agent)' in js
        css = self._css()
        assert ".cloud-chat-main.has-agent-intro > #chat-suggested" in css

    def test_the_default_agent_is_never_badged(self):
        """The plain chat is the unmarked case. Badging every conversation with
        the brand name is how a badge stops being read — which is exactly what
        happened to the muted label under the send button that this replaced."""
        js = self._js()
        block = js[js.index("function _namedAgentForSession()") :]
        block = block[: block.index("\n}")]
        assert "a.is_default" in block and "return null" in block, (
            "the default agent now announces itself like a named one"
        )

    def test_the_hero_says_who_instead_of_the_generic_intro(self):
        """Arriving through an agent's own front door used to look identical to
        arriving at the general chat. The agent's name takes the heading slot —
        same size, same place, more specific claim — and the generic intro gives
        way rather than stacking, because a page cannot say both."""
        html = self._html()
        assert 'id="chat-agent-intro"' in html
        assert 'id="chat-agent-intro-name"' in html
        assert 'id="chat-agent-intro-role"' in html
        # "Delivery Health agent", not "Delivery Health". The name alone left
        # the reader to infer what KIND of thing they had landed on — an agent,
        # a workspace, a mode — from an eyebrow two lines up. Static markup, so
        # the name element stays the one thing written from the API.
        assert '<span class="cld-agent-intro-kind">agent</span>' in html
        kind = self._css().split(".cld-agent-intro-kind {", 1)[1].split("}", 1)[0]
        assert "margin-left" in kind, (
            "an ordinary space between the two spans collapses into the "
            "heading's indentation and glues the words together"
        )
        js = self._js()
        assert 'aside.classList.toggle("has-agent-intro", !!agent)' in js
        css = self._css()
        # Child combinator: the intro's OWN heading and lede are nested deeper
        # and must survive the rule that hides the generic pair.
        assert ".cloud-chat-capabilities.has-agent-intro > .rdb-ask-heading" in css
        assert ".cloud-chat-capabilities.has-agent-intro > .cld-lede" in css
        assert ".cloud-chat-capabilities.has-agent-intro > .cld-greet" in css

    def test_the_agent_follows_the_conversation_into_the_thread_header(self):
        """Who this runs as is a fact about the thread, so it sits with the
        thread chrome. Its predecessor was a muted string trailing the composer,
        which is where it went unread."""
        html = self._html()
        assert 'id="chat-thread-agent"' in html
        assert html.index('id="chat-thread-header"') < html.index('id="chat-thread-agent"')
        assert html.index('id="chat-thread-agent"') < html.index('id="chat-messages"')
        js = self._js()
        assert "chip.hidden = !agent;" in js, "the chip shows for the default agent too"

    def test_the_settled_state_names_the_way_out(self):
        """A name alone teaches nothing about why it cannot be changed; the
        title says how to get what you wanted."""
        js = self._js()
        assert "start a new chat to talk to someone else" in js

    def test_the_selector_offers_only_agents_the_caller_owns(self):
        """``GET /api/v1/agents`` also returns agents merely SHARED with the
        caller, but ``_resolve_agent_id`` resolves a slug against their OWN rows
        only — so offering a shared agent would 404 on click."""
        js = self._js()
        block = js[js.index("async function _refreshAgents()") :]
        block = block[: block.index("\n}")]
        assert "a.mine" in block and "a.slug" in block, (
            "a shared agent is offered again; it would 404 on click"
        )

    def test_the_selector_offers_ready_agents_only(self):
        """A draft is unfinished by its author's own say-so, so it is not
        something to start a conversation with; /agents is where drafts belong.

        The DEFAULT agent is the one thing this filter must not catch. It is
        seeded lazily and can carry ``status: "draft"`` in the window before
        `get_or_create_default` promotes it — and dropping it from the cache
        would make `_namedAgentForSession` mistake a plain chat for a named one
        and badge it.
        """
        js = self._js()
        block = js[js.index("async function _refreshAgents()") :]
        block = block[: block.index("\n}")]
        assert 'a.status === "ready"' in block, "drafts are offered again"
        assert "a.is_default" in block, (
            "the default agent is filtered out with the drafts, so a plain chat "
            "would be badged as a named agent"
        )

    def test_the_identity_keys_on_turns_not_on_session_existence(self):
        """A session row exists the moment "+ New chat" is clicked, so keying
        the thread chrome on that would stage a conversation that has not
        started. The rule is "has this conversation started"."""
        js = self._js()
        helper = js[js.index("function _markConversationStarted()") :]
        helper = helper[: helper.index("\n}")]
        assert "_sessionHasTurns = true;" in helper, "the helper stopped raising the flag"
        assert js.count("_markConversationStarted();") >= 2, (
            "the turns flag is no longer raised on both submit and history hydration"
        )

    def test_the_settled_agent_survives_the_session_open_a_submit_triggers(self):
        """Two openSession paths reset the flag, and both bit once.

        `submitUserMessage` -> `ensureWsReady` re-enters openSession for the
        CURRENT session when the socket is closed (handled by the
        `_switchingSession` check), and for a brand-new chat it CREATES the
        session first — so openSession sees an id it has never opened and the
        switch check does not save it. The flag is re-asserted after the await,
        alongside the `hideCapabilities()` that exists for the same reason.
        """
        js = self._js()
        assert "const _switchingSession = currentChatId !== chatId;" in js
        assert "if (_switchingSession) _sessionHasTurns = false;" in js
        block = js[js.index("    await ensureWsReady();") :]
        block = block[: block.index("} catch (err) {")]
        assert "_markConversationStarted();" in block, (
            "a first submit creates the session, so openSession resets the flag "
            "and the thread chrome flips back to the empty-state hero mid-send"
        )

    def test_starting_an_agent_chat_does_not_stage_a_conversation(self):
        """Picking an agent goes through `newChat()` — it needs a session to run
        the agent as — and `openSession` used to title EVERY session it opened,
        "Untitled chat" when there was nothing better. That put `.has-thread` on
        the shell, so starting an agent chat from the dashboard redrew the page
        as a conversation that did not exist: thread header, Copy transcript,
        composer at the foot, dashboard still underneath.
        """
        js = self._js()
        opened = js[js.index("async function openSession(") :]
        opened = opened[: opened.index("_syncAgentIdentity();")]
        assert '"Untitled chat"' not in opened, (
            "openSession titles every session again, so picking an agent re-enters "
            "the conversation layout on an empty dashboard"
        )
        assert "setThreadTitle(meta && meta.title ? meta.title : null);" in opened, (
            "a session with no title of its own must open without thread chrome"
        )
        helper = js[js.index("function _markConversationStarted()") :]
        helper = helper[: helper.index("\n}")]
        assert "setThreadTitle(" in helper, "the header no longer follows the turns flag"

    def test_choosing_an_agent_starts_a_new_session_as_that_agent(self):
        js = self._js()
        assert "newChat(a.slug || undefined)" in js, (
            "picking an agent no longer spawns a session as that agent"
        )

    def test_an_empty_agent_session_opens_with_the_authored_greeting(self):
        """``agents.greeting`` (v110) was authored in the builder and previewed
        there, but no real chat window ever rendered it. Client-side by design:
        generating a hello through the model would force a sandbox spawn and
        burn a turn before the user has typed anything."""
        js = self._js()
        assert 'renderMessage({ role: "assistant", content: agent.greeting })' in js

    def test_the_greeting_waits_for_the_agent_list_instead_of_racing_it(self):
        """The `/chat?agent=` deep link and a starter chip both open a session
        within the same tick as the `/api/v1/agents` fetch."""
        js = self._js()
        idx_await = js.index("await _agentsLoaded;\n    const agent = _agentById(_currentAgentId);")
        assert idx_await > 0
