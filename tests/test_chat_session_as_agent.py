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


class TestTheComposerCanChooseAnAgent:
    """The picker itself — static-source guards, same rationale as the
    deep-link class above: the behaviour lives in browser JS that the Python
    suite cannot execute, but the invariants that make it correct are all
    visible in the source.
    """

    def _js(self) -> str:
        from pathlib import Path

        return Path("app/web/static/js/chat.js").read_text(encoding="utf-8")

    def test_the_composer_renders_the_picker(self):
        """Asserted against the template rather than a rendered ``/chat``: the
        page needs a live ``chat_config`` on app state, which the seeded test
        app does not build, so rendering it here would test the fixture."""
        from pathlib import Path

        html = Path("app/web/templates/chat.html").read_text(encoding="utf-8")
        assert 'id="chat-agent-btn"' in html, "no way to choose an agent from the composer"
        assert 'id="chat-agent-menu"' in html
        # INSIDE the composer and AFTER the textarea, in the trailing cluster
        # with Send. Choosing an agent is part of composing (the agent is bound
        # at session creation, so this sets a property of the message about to be
        # sent), which is why it is in the pill and not in a strip beneath it.
        #
        # After the textarea, specifically: ahead of the "+" a longer agent name
        # moved where the placeholder began — a measured 58px jump between
        # "Agnes" and "Finance Proposals", reflowing any draft already typed.
        # Trailing, a wider pill takes its width off the end of the field and the
        # text origin does not move at all.
        composer_at = html.index('class="cloud-chat-composer"')
        assert composer_at < html.index('id="chat-input"') < html.index('id="chat-agent-btn"')
        assert html.index('id="chat-plus-btn"') < html.index('id="chat-agent-btn"')
        assert html.index('id="chat-agent-btn"') < html.index('class="cloud-chat-form-actions"'), (
            "the pill pairs with Send, ahead of it"
        )
        css = Path("app/web/static/css/chat.css").read_text(encoding="utf-8")
        assert ".cloud-chat-agent-wrap {\n  margin-left: auto;\n}" not in css, (
            "the agent element must not be pushed to the far end of a row"
        )
        btn_rule = css.split(".cloud-chat-agent-btn {", 1)[1].split("}", 1)[0]
        # One height across the trailing pair, so the pill reads as Send's
        # partner rather than a short thing floating beside a tall circle.
        assert "height: 44px" in btn_rule
        # A cap remains as a backstop for a long single word (which has no
        # initials to take — see `_agentPillLabel`).
        assert "max-width" in btn_rule

    def test_the_pill_abbreviates_a_long_name_but_never_hides_it(self):
        """The pill shows initials past `AGENT_LABEL_MAX` so its width is stable
        across agents. That trades legibility for stability — two names sharing
        initials look alike in the pill — so the full name must remain reachable
        without opening anything: the title attribute carries it, the menu spells
        it out, and the in-conversation label is never abbreviated."""
        js = self._js()
        assert "function _agentPillLabel" in js
        # The menu and the label use the FULL name (`_agentLabel`); only the
        # button text goes through the pill form.
        assert "btnLabel.textContent = pill" in js
        assert "_agentPillLabel(name)" in js
        # Hovering an abbreviated pill must say what it stands for.
        assert "choose which agent to chat with" in js
        # A single long word has no initials to take and falls back to the name
        # (CSS ellipsis), rather than rendering one lonely letter.
        assert "words.length < 2" in js

    def test_the_menu_offers_the_way_to_make_another_agent(self):
        """The picker is where a caller discovers their agents are not enough, so
        the next move belongs in reach rather than back through the rail.

        `?new=1` is the SAME create path the Agents page's own card uses — a
        second door to one flow, not a second flow."""
        js = self._js()
        from pathlib import Path

        assert '"/agents?new=1"' in js
        assert "Create new agent" in js
        css = Path("app/web/static/css/chat.css").read_text(encoding="utf-8")
        # Ruled off from the agents above it: they switch this conversation,
        # this one leaves the page.
        create = css.split(".cloud-chat-agent-menu-create {", 1)[1].split("}", 1)[0]
        assert "border-top" in create
        # It must read as a CONTROL, not a label that happens to carry a caret —
        # which agent answers a question changes the answer. What carries that at
        # rest is ink and weight, not a fill: a standing tint on the one row under
        # the composer competed with the input for the eye. The fill is the hover
        # signal instead, so this asserts the pair rather than either alone.
        #
        # Comments stripped first: these rules explain what they replaced, and the
        # prose mentions the very declarations being asserted about — a raw
        # substring check matched the explanation instead of the CSS.
        import re as _re

        def _decls(selector: str) -> str:
            body = css.split(selector, 1)[1].split("}", 1)[0]
            return _re.sub(r"/\*.*?\*/", "", body, flags=_re.S)

        btn = _decls(".cloud-chat-agent-btn {")
        assert "var(--ds-primary)" in btn, "the resting state must carry accent ink"
        assert "background: transparent" in btn, "no standing fill under the composer"
        hover = _decls(".cloud-chat-agent-btn:hover:not(:disabled) {")
        assert "background: color-mix" in hover, "hover is where the fill happens"
        # One device, not two: no outline drawn around the tinted pill.
        assert "border-color" not in hover

    def test_a_live_conversation_shows_a_label_not_a_control(self):
        """Mid-conversation the agent is fixed, so the button is swapped for a
        plain label rather than disabled in place. A disabled button still
        announces itself as a button to assistive tech and still invites the
        click it must refuse."""
        from pathlib import Path

        html = Path("app/web/templates/chat.html").read_text(encoding="utf-8")
        assert 'id="chat-agent-label"' in html, "no in-conversation agent label"
        js = self._js()
        assert "btn.hidden = _sessionHasTurns;" in js
        assert "staticLabel.hidden = !_sessionHasTurns;" in js
        # And the label is never styled as a control.
        css = Path("app/web/static/css/chat.css").read_text(encoding="utf-8")
        label_block = css[css.index(".cloud-chat-agent-label {") :]
        label_block = label_block[: label_block.index("}")]
        for control_ish in ("cursor: pointer", "border:", "background:"):
            assert control_ish not in label_block, f"the agent label looks clickable: {control_ish}"

    def test_the_picker_survives_the_start_of_a_conversation(self):
        """It lives in the FORM, not in #chat-empty-extras. Extras hide the
        moment #chat-capabilities does, so a picker parked there would vanish
        exactly when a reader most wants to know who they are talking to."""
        from pathlib import Path

        html = Path("app/web/templates/chat.html").read_text(encoding="utf-8")
        assert html.index('id="chat-agent-btn"') < html.index('id="chat-empty-extras"')
        assert html.index('id="chat-agent-btn"') > html.index('id="chat-form"')
        # It is INSIDE the composer pill now, which is inside the form — so it
        # survives for the same reason, and there is no longer a row under the
        # input for it to share (the Stack line that occupied it is retired).
        assert html.index('id="chat-agent-btn"') < html.index("</form>")
        css = Path("app/web/static/css/chat.css").read_text(encoding="utf-8")
        assert ".cloud-chat-composer-foot {" not in css, "the footer row is retired"
        assert ".rdb-context {" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    def test_the_picker_offers_only_agents_the_caller_owns(self):
        """``GET /api/v1/agents`` also returns agents merely SHARED with the caller,
        but ``_resolve_agent_id`` resolves a slug against their OWN rows only —
        so offering a shared agent would 404 on click."""
        js = self._js()
        block = js[js.index("async function _refreshAgents()") :]
        block = block[: block.index("\n}")]
        assert "a.mine" in block and "a.slug" in block, (
            "the picker no longer filters to owned agents; a shared agent would 404 on click"
        )

    def test_the_picker_disables_on_turns_not_on_session_existence(self):
        """An agent is fixed at session creation, so the control must not claim
        to re-target a live conversation. But a session row exists the moment
        "+ New chat" is clicked, so keying the disabled state on that would
        dead-end the picker permanently — the rule is "has this conversation
        started"."""
        js = self._js()
        assert "btn.hidden = _sessionHasTurns;" in js
        # And the flag is raised where a conversation actually begins. It is
        # raised through `_markConversationStarted()` rather than assigned at
        # each site, so the thread header can never disagree with the picker
        # about whether a conversation has started.
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
            "and the settled label flips back into a live picker mid-send"
        )

    def test_switching_agent_on_an_empty_chat_does_not_stage_a_conversation(self):
        """Picking an agent goes through `newChat()` — it needs a session to
        run the new agent as — and `openSession` used to title EVERY session it
        opened, "Untitled chat" when there was nothing better. That put
        `.has-thread` on the shell, so choosing an agent from the dashboard
        redrew the page as a conversation that did not exist: thread header,
        Copy transcript, composer at the foot, dashboard still underneath.

        The header follows the same rule as the picker now — has this
        conversation started, not does a session row exist — so an untitled
        session gets no chrome until history says it has turns.
        """
        js = self._js()
        opened = js[js.index("async function openSession(") :]
        opened = opened[: opened.index("_syncAgentPicker();")]
        assert '"Untitled chat"' not in opened, (
            "openSession titles every session again, so switching agent on the "
            "empty dashboard re-enters the conversation layout"
        )
        assert "setThreadTitle(meta && meta.title ? meta.title : null);" in opened, (
            "a session with no title of its own must open without thread chrome"
        )
        # …and the chrome is raised from the one place that decides a
        # conversation has started, so the two cannot drift apart again.
        helper = js[js.index("function _markConversationStarted()") :]
        helper = helper[: helper.index("\n}")]
        assert "setThreadTitle(" in helper, "the header no longer follows the turns flag"

    def test_the_picker_offers_ready_agents_only(self):
        """A draft is unfinished by its author's own say-so, so it is not
        something to start a conversation with; /agents is where drafts belong.

        The DEFAULT agent is the one thing this filter must not catch. It is
        seeded lazily by `get_or_create_default` and carries `status: "draft"`
        because nobody ever marked it ready — it is never built in the builder
        at all. Filtering it out would strand anyone who switched to a named
        agent with no way back to their own default, which is the exact dead
        end the picker's on-open refresh exists to avoid.
        """
        js = self._js()
        block = js[js.index("async function _refreshAgents()") :]
        block = block[: block.index("\n}")]
        assert 'a.status === "ready"' in block, "the picker offers drafts again"
        assert "a.is_default" in block, (
            "the default agent is filtered out with the drafts — switching away "
            "from it would be one-way"
        )

    def test_the_settled_state_names_the_way_out(self):
        """A label that just states a name teaches nothing about why it can no
        longer be changed; its title says how to get what you wanted."""
        js = self._js()
        assert "start a new chat to switch agent" in js

    def test_choosing_an_agent_starts_a_new_session_as_that_agent(self):
        js = self._js()
        assert "newChat(a.slug)" in js, "the picker no longer spawns a session as the chosen agent"

    def test_an_empty_agent_session_opens_with_the_authored_greeting(self):
        """``agents.greeting`` (v110) was authored in the builder and previewed
        there, but no real chat window ever rendered it. Client-side by design:
        generating a hello through the model would force a sandbox spawn and
        burn a turn before the user has typed anything."""
        js = self._js()
        assert 'renderMessage({ role: "assistant", content: agent.greeting })' in js

    def test_the_greeting_waits_for_the_agent_list_instead_of_racing_it(self):
        """The `/chat?agent=` deep link and a picker click both open a session
        within the same tick as the `/api/v1/agents` fetch."""
        js = self._js()
        idx_await = js.index("await _agentsLoaded;\n    const agent = _agentById(_currentAgentId);")
        assert idx_await > 0
