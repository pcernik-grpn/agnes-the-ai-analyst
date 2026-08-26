"""Running a named agent from web chat (AGT-6).

The headline complaint was "agents can't be used — their tab only configures
them". The runtime was never missing: ``ChatManager.create_session(agent_id=)``
and ``build_profile`` are surface-agnostic and are exactly what
``POST /api/v1/agents/{slug}/sessions`` already used. Web chat was simply never
wired to them, so every browser session ran as the caller's default agent
whatever they had built.

Two things worth pinning beyond "it works":

* **Ownership.** Naming another user's slug must not hand the caller a persona
  assembled from grants that are not theirs. 404, not 403 — a distinct status
  would confirm that someone else's agent exists.
* **The default path is untouched.** No ``agent_slug`` still resolves to the
  caller's default, because every existing session and its attribution depend
  on that.
"""

from __future__ import annotations

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
    resp = seeded_app["client"].post("/api/agents", json={"name": name}, headers=_auth(token))
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
        """Owner-scoped lookup: the admin's agent must not resolve for the analyst."""
        theirs = _make_agent(seeded_app, seeded_app["admin_token"], "Admins Agent")
        resp = _chat_or_skip(seeded_app, surface="web", agent_slug=theirs["slug"])
        assert resp.status_code == 404, (
            "a foreign slug resolved — the caller would get a persona built on grants that are not theirs"
        )

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
            "UNRESOLVED names a handler that now carries the guard — remove it: "
            f"{sorted(self.UNRESOLVED - missing)}"
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
        ``is_default`` row in ``GET /api/agents`` — not by null-checking here."""
        resp = _chat_or_skip(seeded_app, surface="web")
        assert resp.status_code == 201, resp.text

        from src.repositories import agents_repo

        assert resp.json().get("agent_id") == agents_repo().get_or_create_default("analyst1")["id"]

    def test_listing_sessions_reports_each_agent(self, seeded_app):
        agent = _make_agent(seeded_app, seeded_app["analyst_token"], "Listed On The Wire")
        created = _chat_or_skip(seeded_app, surface="web", agent_slug=agent["slug"])
        assert created.status_code == 201, created.text

        listed = seeded_app["client"].get(
            "/api/chat/sessions", headers=_auth(seeded_app["analyst_token"])
        )
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
        # In the footer strip UNDER the input, sharing that line with the Stack
        # context line — not inside the composer pill and not floating loose on
        # the page.
        assert html.index('id="chat-agent-btn"') > html.index('id="chat-input"')
        assert html.index('id="chat-agent-btn"') > html.index('class="cloud-chat-composer-foot"')
        # Trailing the row, not leading it: the Stack line reads first and the
        # agent lands at the right edge, under the send button.
        assert html.index('class="rdb-context"') < html.index('id="chat-agent-btn"')
        css = Path("app/web/static/css/chat.css").read_text(encoding="utf-8")
        assert ".cloud-chat-agent-wrap {\n  margin-left: auto;\n}" in css, (
            "the agent element no longer trails the footer row"
        )

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
        # ...while the Stack line it shares the row with keeps hiding with the
        # dashboard, by its own rule now that it no longer rides that container.
        css = Path("app/web/static/css/chat.css").read_text(encoding="utf-8")
        assert "#chat-capabilities[hidden] ~ #chat-form .rdb-context" in css

    def test_the_picker_offers_only_agents_the_caller_owns(self):
        """``GET /api/agents`` also returns agents merely SHARED with the caller,
        but ``_resolve_agent_id`` resolves a slug against their OWN rows only —
        so offering a shared agent would 404 on click."""
        js = self._js()
        assert "filter(a => a.mine && a.slug)" in js, (
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
        # And the flag is raised where a conversation actually begins.
        assert js.count("_sessionHasTurns = true;") >= 2, (
            "the turns flag is no longer set on both submit and history hydration"
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
        assert "_sessionHasTurns = true;" in block, (
            "a first submit creates the session, so openSession resets the flag "
            "and the settled label flips back into a live picker mid-send"
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
        within the same tick as the `/api/agents` fetch."""
        js = self._js()
        idx_await = js.index("await _agentsLoaded;\n    const agent = _agentById(_currentAgentId);")
        assert idx_await > 0
