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
        # Search, sort keys and the segment set — the whole client-side contract.
        assert 'data-search="revenue deep dive' in row
        assert 'data-name="revenue deep dive"' in row
        assert "data-updated=" in row
        assert 'data-buckets="all"' in row
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

    def test_archived_rows_are_only_in_the_archived_bucket(self, web_client, admin_cookie, monkeypatch):
        """`all` is a token every live row carries, not a wildcard — that is what
        keeps an archived conversation out of All, Pinned and Shared without a
        special case in the filter engine (see `segments.multi`)."""
        _enable_chat(web_client, monkeypatch)
        live = _seed(web_client, title="Live one")
        gone = _seed(web_client, title="Old one", archived=True)
        html = web_client.get("/chats", cookies=admin_cookie).text
        assert 'data-buckets="all"' in _row(html, live)
        assert 'data-buckets="archived"' in _row(html, gone)
        # ...and the archived one is still LISTED. Before this page there was no
        # surface that showed an archived conversation at all, which is what made
        # the old soft-delete a one-way door.
        assert "Old one" in html
        # The segment badge counts the buckets, not the rows.
        assert '<span class="fbar-seg__n" data-seg-count="archived">1</span>' in html
        assert '<span class="fbar-seg__n" data-seg-count="all">1</span>' in html

    def test_pinned_row_is_marked_and_leads_the_list(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        _seed(web_client, title="Just chatting")
        pin = _seed(web_client, title="The pinned one", pinned=True)
        html = web_client.get("/chats", cookies=admin_cookie).text
        row = _row(html, pin)
        assert 'data-pinned="1"' in row
        assert "pinned" in row.split('data-buckets="')[1].split('"')[0]
        # Pinned first, the same order the rail uses, so the page opens on the
        # ordering the caller already knows.
        assert html.index("The pinned one") < html.index("Just chatting")

    def test_the_four_views_live_inside_the_filter_menu(self, web_client, admin_cookie, monkeypatch):
        """All · Pinned · Shared · Archived are the FIRST thing in the Filter
        popover, not a segmented control beside the Filter button — two controls
        doing one job, of which the wider spent its width on counts a caller reads
        once. They stay single-select (the engine's `segments`, one `data-own`
        each): a chat is shown in one view at a time, and unlike a checkbox facet
        these have no "nothing selected" state that would let archived
        conversations back into the default view."""
        _enable_chat(web_client, monkeypatch)
        _seed(web_client, title="Something")
        html = web_client.get("/chats", cookies=admin_cookie).text
        menu = html.split('id="ch-filter-menu"', 1)[1].split("</div>\n    </div>", 1)[0]
        for label in ("All", "Pinned", "Shared", "Archived"):
            assert f'<span class="ch-viewsel__txt">{label}</span>' in menu, f"{label} must be in the Filter menu"
        for own in ("all", "pinned", "shared", "archived"):
            assert f'data-own="{own}"' in menu
        assert 'id="ch-seg"' in menu, "the engine's segment container moves into the menu with them"
        # The views come FIRST, above the optional refinements.
        assert menu.index('id="ch-seg"') < menu.index("fbar-menu__foot")
        # ...and there is no segmented control left on the bar itself.
        bar = html.split('class="fbar ch-fbar"', 1)[1].split('id="ch-filter-menu"', 1)[0]
        assert "fbar-seg__btn" not in bar
        # The active view is named on the Filter button, which is the one thing the
        # tabs said at rest.
        assert 'id="ch-filter-view"' in html
        # Search, sort, view toggle.
        assert 'id="ch-search"' in html
        assert 'id="ch-sort"' in html
        assert 'data-view="grid"' in html and 'data-view="table"' in html
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
        """One toolbar, not a lookalike: the page wraps the SHARED component
        (`.fbar-dock` → `.fbar-dock__card` → `.fbar`, filter_toolbar.css) that
        /library renders, and places its two page-level controls the way /library
        does — search at the LEFT end of the controls row, the primary action at
        the RIGHT end, with the narrowing controls between them. The header is
        the page's name and nothing else; a header scrolls away, and the two
        controls a caller reaches for most must not live in the one place that
        leaves the screen."""
        _enable_chat(web_client, monkeypatch)
        _seed(web_client, title="Something")
        html = web_client.get("/chats", cookies=admin_cookie).text
        assert 'class="fbar-dock"' in html
        assert 'class="fbar-dock__card"' in html
        bar = html.split('class="fbar ch-fbar"', 1)[1].split("</div>\n  </div></div>", 1)[0]
        assert 'id="ch-search"' in bar, "search rides the toolbar, not the header"
        assert 'class="cc-btn cc-btn--primary ch-new"' in bar, "the primary action closes the row"
        assert bar.index('id="ch-search"') < bar.index('id="ch-seg"') < bar.index("ch-new")
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


# ── The rail is a working set now ─────────────────────────────────────────


class TestRailWorkingSet:
    def test_rail_caps_recents_under_a_chats_destination(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        _enable_chat(web_client, monkeypatch)
        rail = web_client.get("/library", cookies=admin_cookie).text
        rail = rail.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
        # The section says it is a slice, or it repeats the destination row above.
        assert '<span class="rail-chatsec-txt">Recent</span>' in rail
        assert '<span class="rail-chatsec-txt">Pinned</span>' in rail
        # One way out, to the page — a DESTINATION ROW, and it NAVIGATES; it is
        # not the "Show more" in-place expander the rail used to carry.
        assert 'id="nav-chats"' in rail
        assert 'href="/chats"' in rail
        assert "Show less" not in rail and "rail-history-more" not in rail
        # It sits ABOVE the lists, in the nav zone with New chat — not at the foot
        # of the scroll box. That is the whole fix: the scroll box is text-only, so
        # the collapsed rail hides it, and the collapsed rail is the default on
        # /admin — a "View all chats" link in there left /chats with no reachable
        # entry point at all from an admin page.
        assert 'id="rail-view-all-chats"' not in rail
        assert "View all chats" not in rail
        chats_at = rail.index('id="nav-chats"')
        assert chats_at < rail.index('class="rail-history"'), "Chats leads the lists, it does not close them"
        assert rail.index('id="new-chat"') < chats_at, "verb then noun: New chat, then Chats"
        # And it lives in the nav zone, so it survives the collapse the lists don't.
        zone = rail[rail.index('class="rail-nav rail-nav-top"') : rail.index('class="rail-history"')]
        assert 'id="nav-chats"' in zone

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
