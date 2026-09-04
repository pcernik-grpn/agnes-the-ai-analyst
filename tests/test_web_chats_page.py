"""The /chats page — the conversation inventory the rail now links out to.

Three contracts are guarded here:

  1. **The page.** Chat-gated exactly like /chat, server-rendered, and every row
     carries the `data-*` the shared client-side toolbar filters and sorts over —
     including the pipe-separated `data-buckets` set that makes All / Pinned /
     Shared / Archived work without archived rows leaking into the other three.
  2. **The two new endpoints.** Archive is a named, reversible state
     (`PUT /sessions/{id}/archived`) and Delete actually deletes
     (`DELETE /sessions/{id}/permanent`) — the distinction the page's row menu
     and bulk bar promise. Both ownership-gated 404, never 403.
  3. **The rail is a working set.** Pinned + a capped Recent feed + one link out,
     with pins never capped.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.chat.types import Surface

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
RAIL_JS = STATIC / "js" / "rail_history.js"
CHAT_JS = STATIC / "js" / "chat.js"
PAGE_JS = STATIC / "js" / "chats_page.js"
MENU_JS = STATIC / "js" / "components" / "chat_row_menu.js"
TOOLBAR_JS = STATIC / "js" / "filter_toolbar.js"


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    # shared_app does not run the startup hook that normally builds these (the
    # TestClient is used without its lifespan, like every other web-page test
    # here), so wire the two pieces the page + its endpoints read: a real repo
    # over the test system DB, and a manager whose only method they touch is
    # `kill` (archive and delete both stop the sandbox first).
    from src.db import get_system_db

    from app.chat.persistence import ChatRepository

    app.state.chat_repo = ChatRepository(get_system_db())

    async def _kill(chat_id, reason=None):
        return None

    app.state.chat_manager = SimpleNamespace(kill=_kill)
    yield TestClient(app)
    close_system_db()


@pytest.fixture
def admin_cookie(web_client):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    password = "AdminPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1",
        email="admin@test.com",
        name="Admin",
        password_hash=PasswordHasher().hash(password),
    )
    grant_admin(conn, "admin1")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "admin@test.com", "password": password})
    assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
    return {"access_token": resp.json()["access_token"]}


def _enable_chat(web_client, monkeypatch):
    """can_chat = chat enabled AND an explicit CHAT grant (admin god-mode does
    not short-circuit has_explicit_grant, so it is patched)."""
    import app.auth.access as access

    monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
    web_client.app.state.chat_config = SimpleNamespace(enabled=True)


def _seed(web_client, *, title, pinned=False, archived=False, messages=0, email="admin@test.com"):
    repo = web_client.app.state.chat_repo
    s = repo.create_session(user_email=email, surface=Surface.WEB, title=title)
    for i in range(messages):
        repo.append_message(session_id=s.id, role="user" if i % 2 == 0 else "assistant", content="hi")
    if pinned:
        repo.set_pinned(s.id, True)
    if archived:
        repo.archive_session(s.id)
    return s.id


def _row(html: str, session_id: str) -> str:
    """The one row's opening tag for a session — enough to assert its data-*.

    A <div>, not a <tr>: the list is deliberately not a table (see the note in
    chats.html), and the filter engine works over any element set."""
    m = re.search(r"<div[^>]*data-item-id=\"" + re.escape(session_id) + r"\"[^>]*>", html)
    assert m, f"no row rendered for {session_id}"
    return m.group(0)


# ── The page ──────────────────────────────────────────────────────────────


class TestChatsPage:
    def test_page_is_chat_gated_like_the_chat_page(self, web_client, admin_cookie, monkeypatch):
        """No grant (or chat disabled) → bounced home, not 403: same contract as
        /chat, whose rail link is hidden for these callers too. This guards the
        direct URL hit."""
        web_client.app.state.chat_config = SimpleNamespace(enabled=False)
        resp = web_client.get("/chats", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 307, "same redirect the chat page issues"
        assert resp.headers["location"] == "/"

        # The route resolves the gate through the module at call time, so
        # patching the attribute is enough (no dependency override needed).
        import app.auth.access as access

        monkeypatch.setattr(access, "can_access", lambda *a, **k: False)
        web_client.app.state.chat_config = SimpleNamespace(enabled=True)
        resp = web_client.get("/chats", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 307

    def test_empty_state_names_the_thing_to_do(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        html = web_client.get("/chats", cookies=admin_cookie).text
        assert "No conversations yet" in html
        # A dead end is the one thing an empty state may not be.
        assert 'href="/chat"' in html
        # No list, no select-all, no bulk bar over nothing.
        assert 'id="ch-list"' not in html
        assert 'id="ch-select-all"' not in html

    def test_rows_carry_the_data_the_toolbar_filters_on(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        sid = _seed(web_client, title="Revenue deep dive", messages=4)
        html = web_client.get("/chats", cookies=admin_cookie).text
        row = _row(html, sid)
        # Search, sort keys and the lifecycle state — the whole client-side
        # contract.
        assert 'data-search="revenue deep dive' in row
        assert 'data-name="revenue deep dive"' in row
        assert "data-updated=" in row
        assert 'data-status="all|active"' in row
        assert 'data-owned="1"' in row
        # The row IS the link to the conversation.
        assert f'data-href="/chat?session={sid}"' in row
        # The agent is on the row — it is the one fact about a conversation that
        # its title cannot give you.
        assert "Default agent" in html
        # The message count is NOT: it says nothing about which conversation this
        # is, and a column of small numbers is the kind of furniture that made
        # the list read as a table.
        assert "data-messages" not in row
        assert ">Messages<" not in html

    def test_the_row_carries_its_state_and_its_attributes_separately(self, web_client, admin_cookie, monkeypatch):
        """Two dimensions, two attributes on the row. `data-status` is the
        LIFECYCLE STATE the Show radios filter on — `active` on every live row
        (the facet's resting value, which is what keeps the archive out of the
        default list without a special case in the engine) and `all` on every
        row, so the option of that name can mean what it says.

        `data-pinned` / `data-shared` are ATTRIBUTES and stay out of it: they
        are their own toggle facets, and archiving a conversation does not unpin
        it, so a pinned archived row is real and "Pinned only" has to reach it.
        Putting them in the state set is what made "All + Shared" a combination
        with no meaning."""
        _enable_chat(web_client, monkeypatch)
        live = _seed(web_client, title="Live one")
        pinned_live = _seed(web_client, title="Pinned live one", pinned=True)
        gone = _seed(web_client, title="Old one", archived=True)
        html = web_client.get("/chats", cookies=admin_cookie).text
        assert 'data-status="all|active"' in _row(html, live)
        assert 'data-status="all|active"' in _row(html, pinned_live)
        assert 'data-pinned="1"' in _row(html, pinned_live)
        assert 'data-status="all|archived"' in _row(html, gone)
        assert "pinned" not in _row(html, gone) and "shared" not in _row(html, gone)
        # ...and the archived one is still LISTED. Before this page there was no
        # surface that showed an archived conversation at all, which is what made
        # the old soft-delete a one-way door.
        assert "Old one" in html
        # The option tallies: `all` is the whole list because that is what the
        # option shows, and the attribute tally spans the archive because that
        # is the scope its toggle has.
        assert 'data-opt-count="all">3</span>' in html
        assert 'data-opt-count="active">2</span>' in html
        assert 'data-opt-count="archived">1</span>' in html
        assert 'data-opt-count="pinned">1</span>' in html

    def test_archiving_unpins_and_an_archived_row_never_reads_as_pinned(
        self, web_client, admin_cookie, monkeypatch
    ):
        """The invariant, end to end. A pin means "keep this at the top of my
        list" and archiving means "this is not in my list", so the two cannot
        both hold: archiving clears the pin.

        The read side normalises as well, which is what makes a row written
        BEFORE this invariant conform without a data migration (the DuckDB
        schema ladder is frozen, so there is no step to carry one)."""
        _enable_chat(web_client, monkeypatch)
        sid = _seed(web_client, title="Pinned then archived", pinned=True)
        repo = web_client.app.state.chat_repo

        html = web_client.get("/chats", cookies=admin_cookie).text
        assert 'data-pinned="1"' in _row(html, sid)

        r = web_client.put(
            f"/api/chat/sessions/{sid}/archived", json={"archived": True}, cookies=admin_cookie
        )
        assert r.status_code == 200, r.text
        assert repo.get_session(sid).pinned_at is None, "archiving must clear the pin"

        html = web_client.get("/chats", cookies=admin_cookie).text
        row = _row(html, sid)
        assert 'data-status="all|archived"' in row
        assert "data-pinned" not in row
        # Nothing is pinned any more, so the option is not rendered at all — the
        # "an option that cannot change the list is a dead end" rule.
        assert 'data-facet="pinned"' not in html

        # A row still pinned in the database — written before the invariant —
        # is normalised on read rather than migrated. `set_pinned` is used
        # directly because the endpoint now refuses exactly this.
        repo.set_pinned(sid, True)
        assert repo.get_session(sid).pinned_at is not None, "the DB row really is pinned"
        html = web_client.get("/chats", cookies=admin_cookie).text
        assert "data-pinned" not in _row(html, sid), "legacy pinned+archived reads as unpinned"
        assert 'data-facet="pinned"' not in html

    def test_pinning_an_archived_conversation_is_refused(self, web_client, admin_cookie, monkeypatch):
        """409, not a silent success: the API is the other way the contradictory
        state could be created. Unpinning stays allowed, so a row pinned before
        the invariant can still be tidied up."""
        _enable_chat(web_client, monkeypatch)
        sid = _seed(web_client, title="Put away", archived=True)
        r = web_client.put(f"/api/chat/sessions/{sid}/pin", json={"pinned": True}, cookies=admin_cookie)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "session_archived"

        r = web_client.put(f"/api/chat/sessions/{sid}/pin", json={"pinned": False}, cookies=admin_cookie)
        assert r.status_code == 200, "unpinning an archived row must stay allowed"

        # A live conversation is unaffected.
        live = _seed(web_client, title="Still going")
        r = web_client.put(f"/api/chat/sessions/{live}/pin", json={"pinned": True}, cookies=admin_cookie)
        assert r.status_code == 200, r.text

    def test_an_option_that_cannot_change_the_list_is_not_rendered(self, web_client, admin_cookie, monkeypatch):
        """The rule the Agent and Source categories already follow. "Shared 0"
        sat in the menu as a row whose only possible effect was to empty the
        page — not a filter, a dead end.

        The WRAPPER goes with the last of its options, and it has to be the
        template that decides: a CSS guard cannot, because `:empty` is false for
        an element containing whitespace and Jinja leaves plenty — so
        `.ch-onlys:not(:empty)` matched a wrapper with zero option rows and
        painted its divider and 10px of margin over nothing."""
        _enable_chat(web_client, monkeypatch)
        _seed(web_client, title="Just mine")
        html = web_client.get("/chats", cookies=admin_cookie).text
        assert 'data-facet="pinned"' not in html, "nothing is pinned, so no Pinned only row"
        assert 'data-facet="shared"' not in html, "nothing is shared, so no Shared only row"
        assert 'class="ch-onlys"' not in html, "and no empty wrapper left behind"
        # The state radios always render: one of them is always the answer.
        for key in ("active", "archived", "all"):
            assert f'data-facet="status" value="{key}"' in html

        # One qualifying option brings the wrapper back — with a role and an
        # accessible name, so its checkboxes are announced as part of "Show"
        # the way the radios above them are.
        _seed(web_client, title="Kept handy", pinned=True)
        html = web_client.get("/chats", cookies=admin_cookie).text
        assert 'class="ch-onlys" role="group" aria-labelledby="ch-status-label"' in html
        assert 'data-facet="pinned"' in html
        assert 'data-facet="shared"' not in html, "still nothing shared"

    def test_the_no_results_panel_does_not_point_at_views_that_no_longer_exist(self):
        """It read "Try a different search term, or another view" — there are no
        views to send anyone to since the four became two filter groups. It now
        names the two things that can actually be narrowing the list, and its
        button says what it clears (`data-fbar-reset` clears the search box too,
        which the Filter menu's own Clear deliberately does not)."""
        html = (TEMPLATES / "chats.html").read_text(encoding="utf-8")
        panel = html.split('id="ch-noresults"', 1)[1].split("</div>", 1)[0]
        assert "another view" not in panel
        assert "Try a shorter search term, or take a filter off above." in panel
        assert ">Clear search and filters<" in panel

    def test_pinned_row_is_marked_and_leads_the_list(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        _seed(web_client, title="Just chatting")
        pin = _seed(web_client, title="The pinned one", pinned=True)
        html = web_client.get("/chats", cookies=admin_cookie).text
        row = _row(html, pin)
        assert 'data-pinned="1"' in row
        assert 'data-status="all|active"' in row, "pinned is an attribute, not a state"
        # Pinned first, the same order the rail uses, so the page opens on the
        # ordering the caller already knows.
        assert html.index("The pinned one") < html.index("Just chatting")

    def test_the_show_block_is_two_groups_in_the_filter_menu(
        self, web_client, admin_cookie, monkeypatch
    ):
        """The FIRST thing in the Filter popover, and TWO groups rather than one
        list of four — because the four were never four of a kind.

        `Archived` is a lifecycle STATE (live or archived, never both, hidden
        until asked for); `Pinned` and `Shared` are ATTRIBUTES a conversation
        carries on either side of it. Crammed into one control they produced
        combinations that answer nothing — as segments (exactly one on) "my
        pinned ones in the archive" was unaskable; as one OR-group `All` was a
        superset of `Shared`, so ticking both said nothing extra. Now: radios for
        the state, checkboxes for the attributes, ANDed. The engine is executed
        over these values in tests/test_web_chats_filter_clear.py."""
        _enable_chat(web_client, monkeypatch)
        _seed(web_client, title="Something", pinned=True)
        html = web_client.get("/chats", cookies=admin_cookie).text
        menu = html.split('id="ch-filter-menu"', 1)[1].split("</div>\n    </div>", 1)[0]
        for key, label in (("active", "Active"), ("archived", "Archived"), ("all", "All")):
            assert f'type="radio" name="ch-status" data-facet="status" value="{key}"' in menu
            assert f'<span class="fbar-menu__opt-text">{label}</span>' in menu
            assert f'data-opt-count="{key}"' in menu
        # The resting option is checked, so the group is never in a "no choice"
        # state the engine would have to invent a meaning for.
        assert 'value="active"\n                   checked' in menu or 'value="active" checked' in menu.replace(
            "\n                   ", " "
        )
        # The attributes are CHECKBOXES, in their own group, under the state.
        assert 'type="checkbox" data-facet="pinned" value="1"' in menu
        assert '<span class="fbar-menu__opt-text">Pinned only</span>' in menu
        assert menu.index('class="ch-viewsel"') < menu.index('class="ch-onlys"')
        # Both groups come FIRST, above the optional refinements.
        assert menu.index('class="ch-onlys"') < menu.index("fbar-menu__foot")
        # No segmented control anywhere on the page any more — not on the bar,
        # not in the menu. Anchored on the bar's aria-label rather than its class
        # list: the class list carries opt-in modifiers (`fbar--ranked`) that
        # this test has no view on.
        bar = html.split('aria-label="Search, filter and sort chats"', 1)[1].split('id="ch-filter-menu"', 1)[0]
        assert "fbar-seg__btn" not in bar
        assert "fbar-seg__btn" not in html and 'data-own="' not in html
        # The bespoke "· Archived" label on the Filter button is gone with them:
        # the chips beside the count say what is applied, and unlike the label
        # they can be clicked off.
        assert 'id="ch-filter-view"' not in html
        assert 'id="ch-chips"' in html
        # Search and sort — and NO view toggle: this page has one projection, so
        # `.fbar-view` is absent from the bar and `view` from the engine config.
        assert 'id="ch-search"' in html
        assert 'id="ch-sort"' in html
        assert "fbar-view" not in html
        assert 'data-view="grid"' not in html and 'data-view="table"' not in html
        assert 'id="ch-grid"' not in html
        # Sorting is the toolbar's <select> — there are no column headers to
        # click, because the list is not a table.
        for order in ("updated_desc", "name_asc", "agent_asc"):
            assert f'value="{order}"' in html
        assert "data-sort-key" not in html
        assert "<table" not in html.split('id="ch-list"', 1)[1], "the list must not be a table"

    def test_multi_select_and_bulk_actions_are_rendered(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        _seed(web_client, title="One")
        html = web_client.get("/chats", cookies=admin_cookie).text
        assert 'id="ch-select-all"' in html
        assert 'class="ch-check"' in html
        for action in ("pin", "unpin", "archive", "restore", "delete"):
            assert f'data-bulk="{action}"' in html
        assert 'id="ch-bulk"' in html and "hidden" in html.split('id="ch-bulk"')[1][:40]

    def test_toolbar_is_the_shared_component_with_librarys_placement(self, web_client, admin_cookie, monkeypatch):
        """One toolbar, not a lookalike: the page renders the SHARED component
        /library renders — the same `.fbar`, opted into the same `.fbar--ranked`
        rank treatment (filter_toolbar.css) — in a browsing block at the head of
        its list, and places its two page-level controls the way /library does:
        search at the LEFT end of the controls row, the primary action at the
        RIGHT end, with the narrowing controls between them. The header is the
        page's name and nothing else.

        This asserted the floating `.fbar-dock` until TCRD-280. The dock was
        bought for a real property — controls reachable at row 200 as much as at
        row 1 — but it floats over the FOOT of the page, so it covered the last
        rows on every screen and put the controls below the list they narrow.
        /library left it in #1751 and /chats was the only caller still on it.
        The assertion is INVERTED rather than deleted: the dock must not come
        back to this page by accident, and the two pages must not drift apart
        again."""
        _enable_chat(web_client, monkeypatch)
        _seed(web_client, title="Something")
        html = web_client.get("/chats", cookies=admin_cookie).text
        assert "fbar-dock" not in html, "the toolbar heads the list; it does not float over its foot"
        assert 'class="ch-browse"' in html
        assert "fbar--ranked" in html, "the shared rank treatment, not a page-local copy of it"
        # Bounded by the state line, which is the next landmark after the row.
        bar = html.split('aria-label="Search, filter and sort chats"', 1)[1].split('class="ch-listhead"', 1)[0]
        assert 'id="ch-search"' in bar, "search rides the toolbar, not the header"
        assert 'class="cc-btn cc-btn--primary ch-new"' in bar, "the primary action closes the row"
        assert bar.index('id="ch-search"') < bar.index('id="ch-filter-btn"') < bar.index("ch-new")
        # The state line is BELOW the controls, against the list it describes —
        # the dock had the chips ABOVE the bar, which was right only while the
        # bar itself sat under the list.
        assert html.index('id="ch-count"') > html.index('id="ch-search"')
        assert html.index('id="ch-chips"') > html.index('id="ch-count"')
        head = html.split('class="ch-head"', 1)[1].split("</div>", 2)[0]
        assert "ch-search" not in head and "ch-new" not in head

    def test_page_rides_the_design_system_shell_and_shared_toolbar(self):
        """No bespoke chrome and no second filter engine: the page extends the
        index shell and loads the SHARED toolbar + row menu, which is what keeps
        it and /library reading as one product."""
        text = (TEMPLATES / "chats.html").read_text(encoding="utf-8")
        assert '{% extends "base_index.html" %}' in text
        assert "css/filter_toolbar.css" in text
        assert "js/filter_toolbar.js" in text
        assert "js/components/chat_row_menu.js" in text
        assert "js/chats_page.js" in text
        # Page CSS is a sheet, not inline in the body (design-system contract).
        assert "css/chats.css" in text
        assert (STATIC / "css" / "chats.css").exists()


# ── Archive / restore / delete ────────────────────────────────────────────


class TestArchiveRestoreDelete:
    def test_archive_is_reversible_and_delete_is_not(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        sid = _seed(web_client, title="Tidy me", messages=2)
        repo = web_client.app.state.chat_repo

        r = web_client.put(f"/api/chat/sessions/{sid}/archived", json={"archived": True}, cookies=admin_cookie)
        assert r.status_code == 200 and r.json()["archived"] is True
        assert repo.get_session(sid).archived is True
        # Archived means "put away", not "gone" — it must still be readable, or
        # the Archived view has nothing to show and Restore nothing to restore.
        assert repo.get_session(sid) is not None

        r = web_client.put(f"/api/chat/sessions/{sid}/archived", json={"archived": False}, cookies=admin_cookie)
        assert r.status_code == 200 and r.json()["archived"] is False
        assert repo.get_session(sid).archived is False

        r = web_client.delete(f"/api/chat/sessions/{sid}/permanent", cookies=admin_cookie)
        assert r.status_code == 204
        assert repo.get_session(sid) is None, "Delete must mean the row is gone"

    def test_plain_delete_still_only_archives(self, web_client, admin_cookie, monkeypatch):
        """The long-standing `DELETE /sessions/{id}` is unchanged — the chat page
        and the rail row menus both use it, and this page's Archive/Delete split
        was added beside it, not on top of it."""
        _enable_chat(web_client, monkeypatch)
        sid = _seed(web_client, title="Soft")
        assert web_client.delete(f"/api/chat/sessions/{sid}", cookies=admin_cookie).status_code == 204
        row = web_client.app.state.chat_repo.get_session(sid)
        assert row is not None and row.archived is True

    def test_both_endpoints_are_ownership_gated_with_404(self, web_client, admin_cookie, monkeypatch):
        """404, never 403: a 403 would confirm that somebody else's session id
        exists, which is a probe."""
        _enable_chat(web_client, monkeypatch)
        other = _seed(web_client, title="Not yours", email="someone@else.com")
        assert (
            web_client.put(
                f"/api/chat/sessions/{other}/archived", json={"archived": True}, cookies=admin_cookie
            ).status_code
            == 404
        )
        assert web_client.delete(f"/api/chat/sessions/{other}/permanent", cookies=admin_cookie).status_code == 404
        assert web_client.app.state.chat_repo.get_session(other) is not None
        # And an id that never existed reads the same way.
        assert web_client.delete("/api/chat/sessions/chat_nope/permanent", cookies=admin_cookie).status_code == 404

    def test_hard_delete_takes_the_messages_with_it(self, web_client, admin_cookie, monkeypatch):
        """DuckDB has no ON DELETE CASCADE, so the children have to be deleted
        explicitly — a session with messages is exactly the case that would
        otherwise fail on the FK."""
        _enable_chat(web_client, monkeypatch)
        sid = _seed(web_client, title="Chatty", messages=6)
        repo = web_client.app.state.chat_repo
        assert len(repo.list_messages(sid)) == 6
        assert web_client.delete(f"/api/chat/sessions/{sid}/permanent", cookies=admin_cookie).status_code == 204
        assert repo.get_session(sid) is None
        assert repo.list_messages(sid) == []

    def test_repo_restore_and_hard_delete_are_idempotent_and_honest(self, web_client, monkeypatch):
        repo = web_client.app.state.chat_repo
        sid = _seed(web_client, title="Round trip")
        repo.restore_session(sid)  # restoring a live session is a no-op
        assert repo.get_session(sid).archived is False
        repo.archive_session(sid)
        repo.restore_session(sid)
        assert repo.get_session(sid).archived is False
        assert repo.hard_delete_session(sid) is True
        # Returns whether there was anything to delete, so a caller can tell a
        # successful delete from a missing row.
        assert repo.hard_delete_session(sid) is False


# ── The page's own client-side contract ───────────────────────────────────


class TestChatsPageScript:
    def test_actions_use_the_existing_per_session_endpoints(self):
        js = PAGE_JS.read_text(encoding="utf-8")
        for path in ("/pin", "/title", "/archived", "/permanent"):
            assert path in js, f"chats_page.js must call {path}"
        assert "promptModal" in js, "rename must use the app-wide modal"
        assert "confirmModal" in js and "danger: true" in js, "delete must be confirmed, destructively"
        # Archive is reversible, so it is deliberately NOT confirmed — only the
        # delete path may reach confirmDelete.
        assert js.count("confirmDelete(") >= 2

    def test_rows_update_in_place_rather_than_reloading(self):
        """A reload would throw away the search term, segment and sort the caller
        set up to find these rows — on a tidy-up surface that is most of the work
        they had done."""
        js = PAGE_JS.read_text(encoding="utf-8")
        assert "location.reload" not in js
        assert "toolbar.refresh()" in js
        # The bucket set is DERIVED from the row's state flags, so the client and
        # the server cannot disagree about which view a row belongs to.
        assert "function syncBuckets" in js

    def test_selection_cannot_survive_a_filter_invisibly(self):
        """A bulk action must never reach a row the caller can no longer see."""
        js = PAGE_JS.read_text(encoding="utf-8")
        assert "syncSelection();" in js.split("onApply:", 1)[1], "the engine's apply hook must re-sync it"
        assert "r.hidden" in js

    def test_shared_with_me_rows_offer_no_owner_actions(self):
        """Pin / rename / archive / delete are owner-only server-side (404), so a
        co-drive conversation someone else owns must show none of them — and no
        checkbox, or a bulk action would silently skip it."""
        text = (TEMPLATES / "chats.html").read_text(encoding="utf-8")
        assert "{% if c.owned %}" in text
        assert "Shared with you" in text
        js = PAGE_JS.read_text(encoding="utf-8")
        assert 'dataset.owned === "1"' in js

    def test_every_mutation_refreshes_the_rail(self):
        """The rail sits BESIDE this page and lists the same conversations, so
        anything that moves here has to move there. It did not: an archived
        conversation went on sitting in the rail's Pinned shelf, and a rename or
        a delete was just as stale, until the next full page load — because
        `rail_history.js`'s `load()` was module-private and nothing on the page
        could reach it.

        The refresh rides `afterMutation`, the single funnel every action on this
        page already goes through (archive, restore, pin, unpin, rename, delete,
        and every bulk equivalent), rather than being bolted onto each one."""
        page = PAGE_JS.read_text(encoding="utf-8")
        fn = page[page.index("function afterMutation()") : page.index("// ---- Feedback for one row's action")]
        assert "window.railChatHistory.reload()" in fn
        # Guarded, because /chats renders for callers whose rail has no history
        # section at all — a missing hook must not break the mutation itself.
        assert "if (window.railChatHistory && window.railChatHistory.reload)" in fn

        rail = RAIL_JS.read_text(encoding="utf-8")
        assert "window.railChatHistory = Object.assign(" in rail, "merged, so railChatSections survives"
        assert "{ reload: load }" in rail

    def test_archiving_offers_an_undo_and_restoring_does_not(self):
        """Archive is the one row action whose result LEAVES THE VIEW while
        being reversible, so it is the one that earns a toast: without it the row
        simply vanished, and undoing a click meant opening Filter, choosing
        Archived, finding the row and hitting Restore.

        Restore gets none — it happens while the caller is deliberately looking
        AT the archive, having gone there to do exactly this, so the row leaving
        is the confirmation and an Undo would only offer to re-archive it.

        It rides the shared `showUndoToast` every admin delete already uses, so
        the app keeps ONE undo affordance."""
        page = PAGE_JS.read_text(encoding="utf-8")
        assert "function announceUndo" in page
        assert "window.showUndoToast" in page
        # Absent helper → no toast, never a broken action.
        assert 'if (typeof window.showUndoToast !== "function") return;' in page

        archive = page[page.index("onArchive: function ()") : page.index("onRestore: function ()")]
        assert "announceUndo(" in archive
        restore = page[page.index("onRestore: function ()") : page.index("onDelete: function ()")]
        assert "announceUndo(" not in restore

        # The bulk equivalent announces ONCE for the batch, and only on a clean
        # run — an Undo over a half-failed batch would promise to put back rows
        # that never moved.
        assert "function runBulk(label, targets, action, onAllDone)" in page
        assert "if (onAllDone) onAllDone();" in page
        bulk = page[page.index('} else if (kind === "archive")') : page.index('} else if (kind === "restore")')]
        assert "announceUndo(" in bulk
        assert 'Promise.all(going.map(' in bulk, "undo puts back exactly the rows this action moved"

    def test_the_shared_undo_toast_accepts_a_callback(self):
        """/chats un-archives with `PUT .../archived {archived:false}`, which the
        helper's POST-to-a-URL form cannot express. The two forms report failure
        differently and must not be conflated: a URL is judged on `response.ok`,
        a callback on whether its promise settles. Reading `.ok` off a callback's
        result would break the common case, since the app's own `api()` throws on
        non-2xx and resolves with parsed JSON that has no `ok` at all."""
        html = (TEMPLATES / "_app_scripts.html").read_text(encoding="utf-8")
        toast = html[html.index("window.showUndoToast = function") :]
        assert "if (typeof restoreUrl === 'function') {" in toast
        assert "await restoreUrl();   // rejects on failure; nothing to inspect" in toast
        # The URL form keeps its own check.
        assert "if (!r.ok) {" in toast

    def test_bulk_failure_does_not_lose_the_rest(self):
        """One failure among ten must not abort the other nine — allSettled
        semantics, not a Promise.all that rejects on the first error."""
        js = PAGE_JS.read_text(encoding="utf-8")
        assert "function runBulk" in js
        assert ".catch(function () {" in js

    def test_row_menu_reads_its_row_at_open_time(self):
        """The page updates rows in place, so a `session` snapshot taken when the
        menu was built would offer "Pin" on a row it had just pinned."""
        menu = MENU_JS.read_text(encoding="utf-8")
        assert "function buildActions" in menu
        assert "open(btn, buildActions())" in menu
        # Archive and Restore are one wiring, chosen from the row's own state.
        assert "s.archived && opts.onRestore" in menu
        assert "!s.archived && opts.onArchive" in menu

    def test_no_pin_control_anywhere_on_an_archived_row(self):
        """Pinned and archived are contradictory states, so neither control
        offers either direction on an archived row.

        A pin means "keep this at the top of my list"; archiving means "this is
        not in my list". The state is therefore prevented at the source —
        `archive_session` clears `pinned_at`, the pin endpoint refuses a pin on
        an archived row, and `_chats_rows` never presents one as pinned — which
        is why there is no Unpin branch to keep here either: an archived row
        never carries `data-pinned` for a control to act on."""
        menu = MENU_JS.read_text(encoding="utf-8")
        assert "if (!s.archived) {" in menu
        assert '{ id: "pin", label: s.pinned ? "Unpin" : "Pin"' in menu
        page = PAGE_JS.read_text(encoding="utf-8")
        # The bulk bar's availability check AND its action, both gated.
        assert page.count('r.dataset.pinned !== "1" && r.dataset.archived !== "1"') == 2
        # Archiving in place drops the pin too, or the row keeps a glyph and a
        # `data-pinned` that a reload would not reproduce.
        assert 'if (archived && row.dataset.pinned === "1") setRowPinned(row, false);' in page

    def test_menu_offers_archive_only_where_a_caller_supplies_it(self):
        """The rail and the chat page pass no archive handler and must keep the
        three-action menu: neither lists archived rows, so archiving there would
        put a conversation somewhere the caller cannot see it again."""
        for path in (RAIL_JS, CHAT_JS):
            js = path.read_text(encoding="utf-8")
            assert "onArchive" not in js, f"{path.name} must not offer Archive"

    def test_segments_multi_is_documented_in_the_shared_engine(self):
        """The non-exclusive segment mode is a shared-engine feature, not a
        page-local fork of the filter code."""
        js = TOOLBAR_JS.read_text(encoding="utf-8")
        assert "segMulti" in js
        assert "multi: true" in js
        # `all` stops being a wildcard in that mode — the whole reason archived
        # rows can be excluded from the default view.
        assert "segValue === 'all' && !segMulti" in js


# ── Nothing in the list may be unreachable ────────────────────────────────
#
# The default view excludes archived conversations, so the count read "7 of 27
# chats" with no control anywhere near it and a search for a hidden chat's own
# title returned nothing: twenty conversations that were put away looked
# exactly like twenty that were lost (#1974).


class TestNothingIsUnreachable:
    def test_search_looks_past_the_show_filter_not_just_inside_it(self):
        """A search is a request for one named thing. A filter that hides it makes
        the thing unfindable, not merely unlisted — so on /chats the search
        outranks the Show filter. Per-FACET and opt-in (`spansSearch`), which is
        why it does not leak to Agent or Source: those the reader set on purpose.

        This rode `segments.searchSpansSegments` until the four views became a
        facet; the option went with them, since /library's tabs — the one
        remaining `segments` caller — are a real scope and mean it. The
        behaviour is executed, not just grepped, in
        tests/test_web_chats_filter_clear.py."""
        js = TOOLBAR_JS.read_text(encoding="utf-8")
        assert "searchSpansSegments" not in js and "segSpansOnSearch" not in js
        facet = js[js.index("function facetMatch(row)") : js.index("function has(allowed, value)")]
        assert "if (f.spansSearch && searchActive()) continue;" in facet
        # It has to come BEFORE the facet's own test, or it never runs.
        assert facet.index("f.spansSearch") < facet.index("f.whenEmpty")

        page = PAGE_JS.read_text(encoding="utf-8")
        assert "spansSearch: true" in page, "/chats' state facet is what opts in"
        assert page.index('key: "status"') < page.index("spansSearch: true")
        # NOT on the attribute toggles or the categories — those the reader set.
        assert page.index("spansSearch: true") < page.index('key: "pinned"')

    def test_the_count_has_a_control_behind_it(self):
        """"7 of 27" is a filter's readout; without something to click it reads
        as a fault. The way into the archived view sits beside the number that
        raises the question, not only inside the Filter menu."""
        html = (TEMPLATES / "chats.html").read_text(encoding="utf-8")
        assert 'id="ch-show-archived"' in html
        assert html.index('id="ch-count"') < html.index('id="ch-show-archived"'), (
            "the control belongs next to the count it explains"
        )
        page = PAGE_JS.read_text(encoding="utf-8")
        fn = page[page.index("function syncHiddenNote()") : page.index("// ---- Show-option counts")]
        # Goes through the engine, so this and choosing Archived in the menu are
        # the same act — including growing the chip that takes it back off.
        assert 'toolbar.setFacet("status", "archived", true)' in page
        assert '"Show " + archived + " archived"' in fn
        # Shown ONLY while nothing is applied: that is the state with no control
        # on screen saying the archive exists, and the only one in which this
        # control's own number is the number you would actually get.
        assert 'statusValue() === "active" && !anyFacetApplied() && !searching && archived > 0' in fn
        # …and recomputed after an action, or archiving the last chat leaves a
        # control offering an archive with nothing in it.
        counts = page[page.index("function updateSegmentCounts()") :]
        assert "syncHiddenNote();" in counts[: counts.index("\n  }")]

    def test_the_engine_exposes_the_facet_setter_it_needs(self):
        js = TOOLBAR_JS.read_text(encoding="utf-8")
        api = js[js.index("    return {\n      apply: apply") :]
        assert "setFacet: setFacet," in api[: api.index("};")]
        # `setSegment` stays exposed for /library's tabs.
        assert "setSegment: setSegment," in api[: api.index("};")]


# ── The rail is a working set now ─────────────────────────────────────────


class TestRailWorkingSet:
    def test_rail_caps_recents_under_a_chats_destination(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        _enable_chat(web_client, monkeypatch)
        rail = web_client.get("/library", cookies=admin_cookie).text
        rail = rail.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
        # One unlabelled list — what says the feed is a slice is the row that
        # closes it, not a header over it.
        assert "rail-chatsec-txt" not in rail
        assert ">Recent<" not in rail and ">Pinned<" not in rail
        # One way out, to the page — and it NAVIGATES; it is not the "Show more"
        # in-place expander the rail used to carry.
        assert "Show less" not in rail and "rail-history-more" not in rail
        # Expanded, the way out CLOSES the lists: a quiet link at the foot of the
        # scroll box, after both sections.
        assert '<a class="rail-history-all" href="/chats">View all chats</a>' in rail
        link_at = rail.index('class="rail-history-all"')
        assert rail.index('id="rail-pinned"') < link_at
        assert rail.index('id="rail-chats"') < link_at, "the link closes the lists"
        assert link_at < rail.index('class="rail-nav rail-nav-bottom"'), "…and stays inside the chat zone"
        # Collapsed, the same destination is the Chats row in the nav zone — the
        # scroll box is text-only, so the 56px strip cannot show it, and a way to
        # a page cannot live only in the part of the rail that collapse hides.
        assert 'id="nav-chats"' in rail
        assert 'href="/chats"' in rail
        chats_at = rail.index('id="nav-chats"')
        assert rail.index('id="new-chat"') < chats_at, "verb then noun: New chat, then Chats"
        zone = rail[rail.index('class="rail-nav rail-nav-top"') : rail.index('class="rail-history"')]
        assert 'id="nav-chats"' in zone, "the stand-in must survive the collapse the lists don't"
        # The two are complements, not a pair on screen at once: the row folds
        # away wherever the lists render.
        assert "rail-i--collapsed-only" in zone

    def test_chats_row_is_active_on_the_page_it_leads_to(self, web_client, admin_cookie, monkeypatch):
        """`.on` in the rail means "you are looking at this". /chats is a
        destination like Library or Agents, so it takes the tint there — and only
        there. On /chat the pre-conversation state belongs to New chat above it,
        and an open conversation is marked in the list below; a Chats row lit on
        either would put a second you-are-here marker in the column.

        (The link this row replaced deliberately took NO active state, because a
        quiet footer link tinted like a nav row read as a fourth destination. Now
        that it IS a destination, the tint is the honest signal.)"""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        _enable_chat(web_client, monkeypatch)
        _seed(web_client, title="Something")

        def _classes(path: str) -> list[str]:
            rail = web_client.get(path, cookies=admin_cookie).text
            rail = rail.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
            row = rail[rail.index('id="nav-chats"') - 200 : rail.index('id="nav-chats"')]
            return row[row.rindex('class="') + 7 :].split('"')[0].split()

        assert "on" in _classes("/chats")
        assert "on" not in _classes("/chat")
        assert "on" not in _classes("/library")

    def test_both_renderers_cap_the_recent_feed_but_never_the_pins(self):
        """One rail, one contract: five recent rows on /library and unbounded on
        /chat would be two contradictory lists. Pins are uncapped in both — the
        shelf is hand-curated, and hiding a pin breaks the promise pinning makes.
        """
        for path in (RAIL_JS, CHAT_JS):
            js = path.read_text(encoding="utf-8")
            assert "RAIL_RECENT_LIMIT" in js, f"{path.name} must cap the rail's recent feed"
            assert re.search(r"RAIL_RECENT_LIMIT\s*=\s*5", js), f"{path.name}: same limit in both renderers"
            assert "slice(0, RAIL_RECENT_LIMIT)" in js
        # The cap is applied to the UNPINNED feed and to nothing else — asserted
        # on the exact loops rather than on a slice of the file, so a later edit
        # that moved the cap onto the pinned list would fail here.
        rail_js = RAIL_JS.read_text(encoding="utf-8")
        assert "for (const s of dated.slice(0, RAIL_RECENT_LIMIT))" in rail_js
        assert "for (const s of pinned) pinnedList.appendChild" in rail_js
        chat_js = CHAT_JS.read_text(encoding="utf-8")
        assert "list.filter(s => !s.pinned).slice(0, RAIL_RECENT_LIMIT)" in chat_js
        assert "for (const s of list.filter(s => s.pinned)) pinnedUl.appendChild" in chat_js

    def test_rail_renders_no_date_group_headers(self):
        """At five rows a date header labels a boundary the list is too short to
        have — "Older" sat inside a section already labelled "Recent". Topnav
        keeps its five buckets, so the grouping helper stays, topnav-only."""
        rail_js = RAIL_JS.read_text(encoding="utf-8")
        assert "cloud-chat-list-group-header" not in rail_js
        assert "groupByDate" not in rail_js
        chat_js = CHAT_JS.read_text(encoding="utf-8")
        assert "Earlier this week" in chat_js, "topnav keeps its date buckets"
        assert "boundaryLabel" not in chat_js, "the rail's two-bucket variant is retired"
        css = (STATIC / "css" / "rail.css").read_text(encoding="utf-8")
        assert 'html[data-ui-layout="rail"] .rail-history .cloud-chat-list-group-header {' not in css

    # `test_topnav_reaches_the_page_too` was here: the topnav's conversations
    # column carried its own "View all chats" link, because the rail's link was
    # the rail's alone. Wave 0 (2026-08) retired that chrome, and the rail
    # answers reachability with a Chats DESTINATION ROW rather than a link
    # inside the conversation region — deliberately, since a way out cannot
    # live in the one part of the rail that collapse hides. That row is pinned
    # by tests/test_ui_layout_theme.py::TestRailChatsDestination.
