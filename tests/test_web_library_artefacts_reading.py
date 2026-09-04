"""The Library's Artifacts section as something you READ (#2141).

Four faults reported against one surface on a live instance, all the same
reading: a list is a claim, and it may not change under the reader after they
have started acting on it.

  1. the tabs are a partition, so the MARKUP already reflects them — nothing
     appears, disappears or renumbers after first paint;
  2. an expanded folder is a PEEK, and the Library's search still reaches the
     files that peek left out;
  3. "back" from a file page returns to the list the reader came from, with
     the folder they left reopened — the collection stays as a breadcrumb;
  4. a crawled file's source path is navigable rather than decorative.

Item 5 of the issue (general sluggishness) has no profile behind it and is not
asserted here; (2) is the part of it this change actually answers, by not
rendering thousands of hidden rows.
"""

from __future__ import annotations

import io
import re

import pytest


@pytest.fixture(autouse=True)
def _rail_layout(monkeypatch):
    """Same gate as tests/test_web_library_files_folders.py — this file
    exercises the RAIL redesign's unified /library."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _collection(seeded_app, name: str, token: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _upload(seeded_app, cid: str, filename: str, token: str, path: str | None = None):
    data = {"paths": path} if path else None
    return seeded_app["client"].post(
        f"/api/collections/{cid}/files",
        files={"files": (filename, io.BytesIO(b"# doc\n" + b"x" * 300), "text/markdown")},
        data=data,
        headers=_auth(token),
    )


def _folder(seeded_app, name: str, token: str, count: int, prefix: str = "f") -> dict:
    """A collection holding `count` files, named `<prefix>-000.md` upward."""
    col = _collection(seeded_app, name, token)
    for i in range(count):
        assert _upload(seeded_app, col["id"], f"{prefix}-{i:03d}.md", token).status_code in (200, 201)
    return col


def _markup(html: str) -> str:
    """The page with its <style> AND <script> blocks removed.

    Every class this file asserts on is also NAMED in the page's CSS, in the
    comments above it, and in the JS that rewrites the row — so a substring
    check against the whole response passes whether or not the markup was
    ever rendered. Three of these tests first passed against nothing that
    way, and one of them passed the CSS check and still failed on the JS.
    """
    without_style = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
    return re.sub(r"<script\b.*?</script>", "", without_style, flags=re.S)


def _rows_of(html: str, parent_id: str) -> list[str]:
    """The child <tr> blocks nested under one folder row."""
    return [m for m in re.findall(r"<tr[^>]*>", html) if f'data-parent-id="{parent_id}"' in m]


# ---------------------------------------------------------------------------
# 1. Nothing moves after first paint
# ---------------------------------------------------------------------------


def test_sections_outside_the_active_tab_render_hidden(seeded_app):
    """The out-of-tab sections used to render VISIBLE and be hidden by the
    filter engine's first apply(), so opening Library painted Plugins and then
    swapped it for Artifacts a beat later — and the reader's first click landed
    on whichever row had moved into place."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Paint Folder", tok, 2)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    secs = re.findall(r'<section class="fbar-group lib-group[^"]*" data-lib-sec="([^"]+)"([^>]*)>', text)
    assert secs, "no sections rendered"
    # Artifacts is a Knowledge section and Library opens on Knowledge, so it
    # paints; the Capabilities sections (skill/plugin/agent) must not.
    by_key = {key: attrs for key, attrs in secs}
    assert "hidden" not in by_key["files"]
    for cap in ("plugin", "skill", "agent"):
        if cap in by_key:
            assert "hidden" in by_key[cap], f"{cap} paints before it is hidden"


def test_the_active_tab_paints_hidden_the_other_way_round(seeded_app):
    """The same guarantee on the other tab — ?tab= is the explicit form."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Other Tab Folder", tok, 2)
    text = seeded_app["client"].get("/library?tab=capabilities", headers=_auth(tok)).text

    by_key = dict(re.findall(r'<section class="fbar-group lib-group[^"]*" data-lib-sec="([^"]+)"([^>]*)>', text))
    assert "hidden" in by_key["files"]
    for cap in ("plugin", "skill", "agent"):
        if cap in by_key:
            assert "hidden" not in by_key[cap]


def test_the_saved_view_is_decided_before_first_paint(seeded_app):
    """Item 1's fault survived in GRID view, by a different route.

    The saved view lives in localStorage, so the server sends the table
    visible and an empty grid beside it — and a reader whose saved view is
    grid got the whole table painted and then swapped out from under them.
    Hiding the out-of-tab sections did nothing for this one.

    The fix is the standard no-flash pattern: a BLOCKING script in <head>
    stamps the saved view on <html> and a CSS rule hides the table before it
    can paint. Three properties, each load-bearing:
      * the script is in the HEAD and not deferred, or it runs too late;
      * the grid reserves the height its cards will need, or the page grows
        under the reader when they land;
      * the attribute is RETIRED once the engine owns `hidden`, or its
        `display:none` outranks `hidden` and the table stays hidden forever
        after a switch back to it.
    """
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Prepaint Folder", tok, 4)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    head = text.split("</head>")[0]
    assert "localStorage.getItem('lib-view')" in head, "the view probe must run before the body paints"
    assert "dataset.libView" in head
    # Blocking on purpose — `defer`/`async` would run it after first paint.
    probe = re.search(r"<script([^>]*)>\s*\(function \(\) \{\s*try \{\s*if \(localStorage", head)
    assert probe, "the probe is not the plain inline script it needs to be"
    assert "defer" not in probe.group(1) and "async" not in probe.group(1)

    assert 'html[data-lib-view="grid"] .lib-tablewrap { display: none; }' in text
    assert "--lib-cards: 1;" in text or re.search(r"--lib-cards: \d+;", text), "no reserved grid height"
    assert "delete document.documentElement.dataset.libView" in text, (
        "the pre-paint attribute is never retired — a switch back to the table would stay hidden"
    )


def test_the_item_count_is_the_active_tabs_own_total(seeded_app):
    """It was the whole inventory, so a Knowledge load painted "17 items" and
    then corrected itself to "10 items" — a number that moves after first paint
    reads as a filter the reader applied. The value must equal the active tab's
    badge, which is what the page's own refreshItemCount() computes."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Count Folder", tok, 2)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    count = re.search(r'id="lib-item-count">(\d+) item', text)
    badge = re.search(r'data-seg-count="knowledge">(\d+)<', text)
    assert count and badge
    assert count.group(1) == badge.group(1)


# ---------------------------------------------------------------------------
# 2. The expansion is a peek, and search reaches past it
# ---------------------------------------------------------------------------


def test_a_big_folder_shows_a_way_through_with_no_files_fetched_up_front(seeded_app):
    """25 files used to be 25 child rows pre-rendered hidden (1154 on the
    instance that reported this, #2141) — then, in the first cut of the peek
    fix, TEN rows pre-rendered hidden plus a "Browse all" row (still an
    unconditional per-collection file fetch and a `data-search` value built
    from every filename — the round-2 incident: 19.6 MB of HTML on a
    392-collection instance whose live DOM was 1.09 MB). Now: the index
    fetches nothing about this folder's FILES at all — only its batched
    count — and the "Browse all" row (and its count) comes from that count
    alone. No child `<tr>` exists until the reader expands the row."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Peek Folder", tok, 25)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    kids = _rows_of(text, col["id"])
    # Exactly the "Browse all" row — no peek rows pre-rendered.
    assert len(kids) == 1
    assert "data-more-row" in kids[0]
    assert "Browse all 25 files" in text
    assert 'href="/library/' + col["slug"] + '#files-section"' in text
    # And no per-file identity anywhere on the index for this folder.
    assert "f-000.md" not in text
    assert "f-024.md" not in text


def test_a_small_folder_also_shows_no_files_up_front_and_no_browse_all_row(seeded_app):
    """A folder inside the peek size gets the same treatment as a big one now
    — nothing about its files is fetched at index-render time, so it has no
    child rows AND no "Browse all" row (nothing was left out of a peek that
    was never taken)."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Whole Folder", tok, 3)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    kids = _rows_of(text, col["id"])
    assert kids == []
    # Matched against MARKUP, never the whole page: the row's own CSS block
    # names it in a comment, so `"Browse all" not in text` passes vacuously.
    assert "lib-more__link" not in _markup(text)


def test_a_folders_peek_is_fetched_lazily_from_its_own_route(seeded_app):
    """The rows a folder's twisty reveals — however many files it holds —
    live at `GET /library/{slug}/peek`, never the index response. Same
    contract as `matching-files`: real Library child rows, through the same
    macro, capped at `_LIBRARY_FOLDER_PEEK`."""
    from app.web.router import _LIBRARY_FOLDER_PEEK

    tok = seeded_app["admin_token"]
    big = _folder(seeded_app, "Peek Route Big", tok, 25)
    small = _folder(seeded_app, "Peek Route Small", tok, 3)

    r = seeded_app["client"].get(f"/library/{big['slug']}/peek", headers=_auth(tok))
    assert r.status_code == 200
    kids = _rows_of(r.text, big["id"])
    assert len(kids) == _LIBRARY_FOLDER_PEEK
    assert all("data-more-row" not in k for k in kids)
    assert 'data-filename="f-000.md"' in r.text
    assert "f-024.md" not in r.text  # past the peek — that is what matching-files is for

    r_small = seeded_app["client"].get(f"/library/{small['slug']}/peek", headers=_auth(tok))
    assert r_small.status_code == 200
    assert len(_rows_of(r_small.text, small["id"])) == 3


def test_the_folder_row_carries_only_its_own_name_and_description_to_search_by(seeded_app):
    """The index card is the count/name/description alone (round 2 of the
    incident fix) — a folder is no longer searchable by a filename it holds
    without opening it. `matching-files`/`peek` are the ways to reach a file
    by name now, not the index's own search box."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Search Text Folder", tok, 25)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    row = next(m for m in re.findall(r"<tr[^>]*>", text) if f'data-item-id="{col["id"]}"' in m)
    search = re.search(r'data-search="([^"]*)"', row).group(1)
    assert "search text folder" in search  # the collection's own name
    assert "f-000.md" not in search
    assert "f-024.md" not in search


def test_matching_files_route_returns_the_rows_the_peek_left_out(seeded_app):
    """The fix for the review finding on the cap: a search that names a file
    the peek did not render must still SHOW that file, not offer a link to go
    looking for it. The rows come from the server through the page's own
    macro."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Match Folder", tok, 25)

    r = seeded_app["client"].get(f"/library/{col['slug']}/matching-files?q=f-024", headers=_auth(tok))
    assert r.status_code == 200
    kids = _rows_of(r.text, col["id"])
    assert len(kids) == 1
    assert 'data-filename="f-024.md"' in r.text
    # A real Library child row: the fragment shares the page's macro, so it
    # carries the row contract — its own sharing control included.
    assert 'data-share-type="corpus_file"' in r.text
    assert 'data-visibility="private"' in r.text
    assert "lib-row--child" in r.text


def test_matching_files_answers_a_search_and_nothing_else(seeded_app):
    """A blank q returns nothing rather than the whole collection: this route
    answers a search, and the unfiltered list is what the peek already is."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Blank Query Folder", tok, 25)

    for q in ("", "   "):
        r = seeded_app["client"].get(f"/library/{col['slug']}/matching-files?q={q}", headers=_auth(tok))
        assert r.status_code == 200
        assert r.text.strip() == ""

    r = seeded_app["client"].get(f"/library/{col['slug']}/matching-files?q=nothing-matches-this", headers=_auth(tok))
    assert r.status_code == 200
    assert r.text.strip() == ""


def test_matching_files_is_capped_too(seeded_app):
    """It is a fragment for a list, not a dump: a query matching everything
    returns at most `_LIBRARY_MATCH_LIMIT` rows, and the page's "Browse all"
    row is what continues past them."""
    from app.web.router import _LIBRARY_MATCH_LIMIT

    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Capped Match Folder", tok, 25)

    r = seeded_app["client"].get(f"/library/{col['slug']}/matching-files?q=f-", headers=_auth(tok))
    assert r.status_code == 200
    assert len(_rows_of(r.text, col["id"])) == min(25, _LIBRARY_MATCH_LIMIT)


def test_matching_files_404s_for_unknown_and_for_no_access(seeded_app):
    """Same contract as /library/{slug}: 404 for both, so the URL space cannot
    be probed for collection existence."""
    admin = seeded_app["admin_token"]
    other = seeded_app["analyst_token"]
    col = _folder(seeded_app, "Private Match Folder", admin, 12)

    assert (
        seeded_app["client"].get("/library/no-such-collection/matching-files?q=x", headers=_auth(admin)).status_code
        == 404
    )
    r = seeded_app["client"].get(f"/library/{col['slug']}/matching-files?q=f-", headers=_auth(other))
    assert r.status_code == 404, "a caller with no grant enumerated a collection's files"


def test_a_per_file_grant_does_not_open_the_collections_file_list(seeded_app):
    """The gate is deliberately NARROWER than the file page's.

    `library_file_detail` falls back to a per-file grant, so a caller handed
    one file inside a folder can open that file. This route lists the folder's
    files, so it must not accept that same grant — otherwise sharing one file
    would leak the names of every file beside it. Hence
    `can_access_collection`, not the per-file rule.

    `ensure_everyone_membership` is called explicitly for the reason given in
    the empty-menu test above: without it the Everyone grant resolves to
    nothing, the file page 404s too, and this test would pass while proving
    the opposite of what it claims.
    """
    from app.auth.group_sync import ensure_everyone_membership
    from app.resource_types import ResourceType

    admin = seeded_app["admin_token"]
    other = seeded_app["analyst_token"]
    col = _folder(seeded_app, "One File Shared Folder", admin, 12)
    fid = (
        seeded_app["client"]
        .get(f"/api/collections/{col['id']}/files", headers=_auth(admin))
        .json()["files"][0]["file_id"]
    )

    assert ensure_everyone_membership("analyst1", added_by="test")
    groups = seeded_app["client"].get("/api/sharing/groups", headers=_auth(admin)).json()
    everyone = next(g for g in groups if g["is_everyone"])
    r = seeded_app["client"].put(
        f"/api/sharing/{ResourceType.CORPUS_FILE.value}/{fid}",
        json={"group_ids": [everyone["id"]]},
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text

    # The premise: that ONE file is now openable by the other caller…
    assert seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}", headers=_auth(other)).status_code == 200, (
        "the per-file grant did not reach the caller — the assertion below would prove nothing"
    )
    # …and its siblings are still not enumerable.
    assert (
        seeded_app["client"].get(f"/library/{col['slug']}/matching-files?q=f-", headers=_auth(other)).status_code == 404
    )


def test_the_page_carries_the_client_half_of_the_match_lookup(seeded_app):
    """The wiring the two halves meet on — asserted because the route and the
    page are useless apart, and a rename on either side is silent otherwise."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Wiring Folder", tok, 25)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    assert "/matching-files?q=" in text
    assert "data-match-row" in text  # how injected rows are found again
    assert "syncMatches(q)" in text  # …and where they are asked for
    # Declared above the toolbar: the engine's first apply() reaches
    # syncChildren() -> syncMatches(), and a `const` further down the file
    # would still be in its temporal dead zone (the bug this file's sibling
    # records for `openFolders`).
    assert text.index("const matched = new Map()") < text.index("window.FilterToolbar.init")


# ---------------------------------------------------------------------------
# 3. Back goes where the reader came from
# ---------------------------------------------------------------------------


def test_a_file_page_backs_out_to_the_library_not_to_its_collection(seeded_app):
    """The back affordance was the COLLECTION, so backing out of a file opened
    from the Library's own expansion landed on a page the reader had never
    seen. Back is the Library; the collection stays as a breadcrumb."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Back Folder", tok, 3)
    fid = (
        seeded_app["client"]
        .get(f"/api/collections/{col['id']}/files", headers=_auth(tok))
        .json()["files"][0]["file_id"]
    )

    # `?from=library` is the route this test is about — the Library's own rows
    # carry it (see `test_the_library_rows_declare_where_they_came_from`), and
    # it is what puts the arrow on the Library rather than the collection.
    text = seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}?from=library", headers=_auth(tok)).text

    crumbs = re.search(r'<nav class="detail-back detail-crumbs".*?</nav>', text, re.S)
    assert crumbs, "the file page still renders a single back link"
    crumbs = crumbs.group(0)
    # The FIRST crumb carries the arrow and is the back action.
    back = re.search(r'<a class="detail-crumbs__here" href="([^"]+)"', crumbs)
    assert back and back.group(1).startswith("/library?section=files")
    assert ">Library</a>" in crumbs
    # …and the collection is a plain crumb after it, not the back action.
    assert f'<a class="detail-crumbs__up" href="/library/{col["slug"]}">' in crumbs


def test_the_back_arrow_follows_the_route_the_reader_took(seeded_app):
    """Both routes to a file page are real — the Library lists it inline under
    its folder, and the collection page lists it too — so one fixed back target
    is wrong for one of them. `?from=library` (carried by the Library's own
    rows) moves the arrow; anything else leaves it on the collection.

    Asserted on the ARROW, not on the trail: both crumbs render either way, so
    a test that only checked the links would pass with the arrow on the wrong
    one — which is the bug this closes.
    """
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Arrow Folder", tok, 3)
    fid = (
        seeded_app["client"]
        .get(f"/api/collections/{col['id']}/files", headers=_auth(tok))
        .json()["files"][0]["file_id"]
    )

    def _arrow(url: str) -> str:
        text = seeded_app["client"].get(url, headers=_auth(tok)).text
        nav = re.search(r'<nav class="detail-back detail-crumbs".*?</nav>', text, re.S)
        assert nav, "no crumb trail"
        here = re.search(r'<a class="detail-crumbs__here" href="([^"]+)"', nav.group(0))
        assert here, "no crumb carries the back arrow"
        # Both crumbs must be present whichever one holds the arrow.
        assert nav.group(0).count("<a ") == 2
        return here.group(1)

    # Arrived from the Library list -> back to the Library, folder reopened.
    from_lib = _arrow(f"/library/{col['slug']}/f/{fid}?from=library")
    assert from_lib.startswith("/library?section=files")
    assert f"open={col['id']}" in from_lib

    # Arrived any other way (the collection page, the preview modal, a pasted
    # URL) -> back to the collection, this file's structural parent.
    assert _arrow(f"/library/{col['slug']}/f/{fid}") == f"/library/{col['slug']}"


def test_the_library_rows_declare_where_they_came_from(seeded_app):
    """The other half of the pair above: the Library's file rows must carry
    `?from=library`, or the arrow silently falls back to the collection for
    the very path #2141 reported. Child rows are fetched lazily from
    `/library/{slug}/peek` (round 2 of the incident fix), never pre-rendered
    on the index — this checks the fragment they actually land from."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "From Folder", tok, 3)
    text = seeded_app["client"].get(f"/library/{col['slug']}/peek", headers=_auth(tok)).text

    kids = _rows_of(text, col["id"])
    assert kids, "no child rows rendered"
    assert all("?from=library" in k for k in kids if "data-more-row" not in k)
    # The fragment's rows are Library rows too — same builder, same marker.
    r = seeded_app["client"].get(f"/library/{col['slug']}/matching-files?q=f-", headers=_auth(tok))
    assert "?from=library" in r.text


def test_grid_view_does_not_fetch_rows_it_cannot_show(seeded_app):
    """Grid cards are projected from the engine's TOP-LEVEL rows, so a file
    inside a folder has never had one. Fetching matches there bought a request
    nothing could display; and because the engine's `setView` calls
    `applyView()` alone — never `apply()` — switching back to the table has to
    re-run the sync itself, or the match stays missing until the next
    keystroke. Both halves are asserted, since either alone is a bug."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Grid Folder", tok, 25)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    assert '.fbar-view__btn.is-active[data-view="grid"]' in text, "no grid guard in syncMatches"
    assert "setTimeout(syncChildren, 0)" in text, "the view toggle does not re-sync"


def test_backing_out_reopens_the_folder_the_reader_left(seeded_app):
    """A file inside a collection is only ON the Library page as a child of an
    expanded folder, so a back link to a collapsed list drops the reader
    somewhere that does not contain what they came from."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Reopen Folder", tok, 3)
    fid = (
        seeded_app["client"]
        .get(f"/api/collections/{col['id']}/files", headers=_auth(tok))
        .json()["files"][0]["file_id"]
    )

    text = seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}", headers=_auth(tok)).text
    assert f"open={col['id']}" in text

    lib = seeded_app["client"].get(f"/library?section=files&open={col['id']}", headers=_auth(tok)).text
    assert f'const OPEN_FOLDER = "{col["id"]}"' in lib


def test_the_open_param_is_validated_against_the_dom_not_trusted(seeded_app):
    """It reaches the page as a JS string and is only ever compared against ids
    already in the DOM — the same posture as `?new=`. A hostile value must land
    as an inert string, never as markup or a script break."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Injection Folder", tok, 3)

    text = seeded_app["client"].get("/library?open=</script><script>alert(1)</script>", headers=_auth(tok)).text
    assert "<script>alert(1)</script>" not in text
    assert "const OPEN_FOLDER = " in text


def test_the_rail_names_the_collection_once(seeded_app):
    """The rail stated the file's collection TWICE in one column — as a
    "Collection" facts row and, one block below, as the "In this collection"
    related entity, both linking to the same page. The related block stays: it
    names the parent as an entity (glyph, kind label), which is what that slot
    is for, where the row restated it as a fact about the file.

    Counted over LINKS to the collection inside the rail, not over the name,
    which legitimately appears in the crumb trail and in the page's prose.
    """
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Rail Folder", tok, 3)
    fid = (
        seeded_app["client"]
        .get(f"/api/collections/{col['id']}/files", headers=_auth(tok))
        .json()["files"][0]["file_id"]
    )

    text = _markup(seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}", headers=_auth(tok)).text)
    rail = re.search(r'<aside class="detail-aside">.*?</aside>', text, re.S)
    # Never skipped on a missing match: the rail IS the default layout, and a
    # skip here would retire the guard the moment the container is renamed.
    assert rail, "no rail found on the file detail page"
    body = rail.group(0)
    links = re.findall(rf'href="/library/{re.escape(col["slug"])}"', body)
    assert len(links) == 1, f"the rail links to the collection {len(links)} times"
    # …and the one that stayed is the related-entity block (which labels the
    # link with its KIND), not the facts row (which labels it with a key).
    assert "In this collection" in body
    assert "detail-side__row" not in body.split("In this collection")[1]


def test_the_crumb_trail_is_opt_in_and_other_detail_pages_are_unchanged(seeded_app):
    """`hero(crumbs=…)` is additive: a page that passes nothing keeps the lone
    back link it has always rendered. The collection page is the neighbour most
    likely to be broken by a change to the shared scaffold."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Neighbour Folder", tok, 3)

    text = seeded_app["client"].get(f"/library/{col['slug']}", headers=_auth(tok)).text
    assert 'class="detail-back"' in text
    assert "detail-crumbs" not in re.sub(r"<style>.*?</style>", "", text, flags=re.S)


def test_the_overflow_menu_drops_the_third_path_to_the_collection(seeded_app):
    """ "Open collection" behind a chevron was a third route to a page the crumb
    trail now links in one click, next to the title. The page's own comment
    already retired Sharing from this menu on that argument; this applies it."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Menu Folder", tok, 3)
    fid = (
        seeded_app["client"]
        .get(f"/api/collections/{col['id']}/files", headers=_auth(tok))
        .json()["files"][0]["file_id"]
    )

    text = _markup(seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}", headers=_auth(tok)).text)
    assert "Open collection" not in text
    # The collection is still reachable — from the crumb and from the rail.
    assert f'href="/library/{col["slug"]}"' in text
    # Delete, the one action that belongs behind a chevron, stays.
    assert "data-delete-file" in text


def test_no_empty_overflow_menu_when_there_is_nothing_in_it(seeded_app):
    """`detail.menu([])` renders a chevron over an empty panel, and removing
    "Open collection" made the empty case reachable: a reader who may not
    delete the file now has no menu items at all. The control must be absent,
    not inert."""
    admin = seeded_app["admin_token"]
    other = seeded_app["analyst_token"]
    col = _folder(seeded_app, "No Menu Folder", admin, 3)
    fid = (
        seeded_app["client"]
        .get(f"/api/collections/{col['id']}/files", headers=_auth(admin))
        .json()["files"][0]["file_id"]
    )

    # Share the COLLECTION so the other caller can open the page at all, but
    # they still do not own it — so `can_manage` is false and the menu empties.
    #
    # `ensure_everyone_membership` is called explicitly because `seeded_app`
    # builds its role users directly and so bypasses the two paths that
    # materialize the row in production (`app.auth.provisioning` at sign-in,
    # `POST /api/users`). Without it an Everyone grant resolves to nothing for
    # this caller and the assertions below would pass against a 404 page.
    from app.auth.group_sync import ensure_everyone_membership

    assert ensure_everyone_membership("analyst1", added_by="test")
    groups = seeded_app["client"].get("/api/sharing/groups", headers=_auth(admin)).json()
    everyone = next(g for g in groups if g["is_everyone"])
    r = seeded_app["client"].put(
        f"/api/sharing/collection/{col['id']}",
        json={"group_ids": [everyone["id"]]},
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text

    resp = seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}", headers=_auth(other))
    assert resp.status_code == 200, "the collection share did not reach the other caller"
    text = _markup(resp.text)
    assert "data-delete-file" not in text, "a non-owner was offered Delete"
    assert '<details class="detail-menu">' not in text, "an empty overflow menu rendered"


# ---------------------------------------------------------------------------
# Both page bodies
# ---------------------------------------------------------------------------
#
# `detail.redesign` is literally `instance_theme == 'paper'`, and the file
# detail page has two entirely separate bodies behind that one gate: the
# two-column rail layout (paper, the default) and the single-column
# Details/Sharing cards (any explicitly configured theme — a supported
# opt-out, and what an instance that pins `instance.theme` actually renders).
# A fix that lands on only one of them half-ships, so the two that CROSS the
# gate are asserted on both.


@pytest.mark.parametrize("theme", ["paper", "blue"])
def test_the_crumb_trail_renders_on_both_page_bodies(seeded_app, monkeypatch, theme):
    """Item 3's fix must not be paper-only. It isn't by construction — the back
    link lives in the shared hero ABOVE the redesign gate — and this is what
    keeps it there."""
    monkeypatch.setenv("AGNES_INSTANCE_THEME", theme)
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, f"Both Bodies {theme}", tok, 3)
    fid = (
        seeded_app["client"]
        .get(f"/api/collections/{col['id']}/files", headers=_auth(tok))
        .json()["files"][0]["file_id"]
    )

    text = seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}?from=library", headers=_auth(tok)).text
    assert f'data-theme="{theme}"' in text, "the theme under test did not take effect"
    # The rail exists on paper only — which is the point of parametrizing.
    assert ("detail-aside" in text) == (theme == "paper")
    crumbs = re.search(r'<nav class="detail-back detail-crumbs".*?</nav>', text, re.S)
    assert crumbs, f"no crumb trail on the {theme} body"
    back = re.search(r'<a class="detail-crumbs__here" href="([^"]+)"', crumbs.group(0))
    assert back and back.group(1).startswith("/library?section=files")
    assert f"open={col['id']}" in back.group(1)


@pytest.mark.parametrize("theme", ["paper", "blue"])
def test_the_source_path_is_navigable_on_both_page_bodies(seeded_app, monkeypatch, theme):
    """Item 4's fix likewise — the collection page's file rows are outside the
    gate, and this holds them there."""
    monkeypatch.setenv("AGNES_INSTANCE_THEME", theme)
    tok = seeded_app["admin_token"]
    col = _collection(seeded_app, f"Path Both {theme}", tok)
    assert _upload(seeded_app, col["id"], "deep.md", tok, path="Docs/2026/deep.md").status_code in (200, 201)

    text = seeded_app["client"].get(f"/library/{col['slug']}", headers=_auth(tok)).text
    assert f'data-theme="{theme}"' in text
    labels = [lbl for _h, lbl in re.findall(r'<a class="file__pathseg"\s+href="([^"]+)"[^>]*>([^<]+)</a>', text)]
    assert labels == ["Docs", "2026"], f"path not navigable on the {theme} body: {labels}"


# ---------------------------------------------------------------------------
# 4. A crawled file's path is navigable
# ---------------------------------------------------------------------------


def test_a_source_path_renders_as_links_that_narrow_the_file_list(seeded_app):
    """The path was plain text, so the one piece of structure a crawled
    collection has could be read and not used: a reader looking at a file could
    see the folder it came from and had no way to see its siblings."""
    tok = seeded_app["admin_token"]
    col = _collection(seeded_app, "Crawled Folder", tok)
    assert _upload(seeded_app, col["id"], "deep.md", tok, path="Shared Documents/Reports/2026/deep.md").status_code in (
        200,
        201,
    )

    text = seeded_app["client"].get(f"/library/{col['slug']}", headers=_auth(tok)).text

    segs = re.findall(r'<a class="file__pathseg"\s+href="([^"]+)"[^>]*>([^<]+)</a>', text)
    assert [label for _href, label in segs] == ["Shared Documents", "Reports", "2026"], segs
    # Each link narrows to its own CUMULATIVE prefix — "everything under this
    # folder" — through the page's own ?q=, which matches path as well as name.
    assert "q=Shared+Documents&" in segs[0][0] or "q=Shared+Documents#" in segs[0][0]
    assert "Shared+Documents%2FReports%2F2026" in segs[2][0]
    assert segs[2][0].endswith("#files-section")


def test_the_filename_is_not_offered_as_a_folder_to_browse(seeded_app):
    """The trailing segment is the FILE when the path carries it, and a link
    that narrows the list to the row you are looking at is noise."""
    tok = seeded_app["admin_token"]
    col = _collection(seeded_app, "Trailing Folder", tok)
    assert _upload(seeded_app, col["id"], "leaf.md", tok, path="Docs/leaf.md").status_code in (200, 201)

    text = seeded_app["client"].get(f"/library/{col['slug']}", headers=_auth(tok)).text
    labels = [label for _h, label in re.findall(r'<a class="file__pathseg"\s+href="([^"]+)"[^>]*>([^<]+)</a>', text)]
    assert labels == ["Docs"]


def test_a_manual_upload_has_no_path_and_renders_none(seeded_app):
    """No path, no path line — the block is for crawled/source-managed files."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Manual Folder", tok, 2)

    text = seeded_app["client"].get(f"/library/{col['slug']}", headers=_auth(tok)).text
    assert '<a class="file__pathseg"' not in _markup(text)


def test_the_path_narrowing_actually_narrows(seeded_app):
    """End to end, because the link is only useful if the page it opens agrees:
    ?q=<prefix> matches on PATH, so a folder prefix selects that folder's
    files and excludes the rest."""
    tok = seeded_app["admin_token"]
    col = _collection(seeded_app, "Narrowing Folder", tok)
    for path in ("A/2026/one.md", "A/2026/two.md", "A/2025/old.md", "B/other.md"):
        assert _upload(seeded_app, col["id"], path.rsplit("/", 1)[-1], tok, path=path).status_code in (200, 201)

    text = seeded_app["client"].get(f"/library/{col['slug']}?q=A/2026", headers=_auth(tok)).text
    assert "2 of 4 files" in text
    assert "one.md" in text and "two.md" in text
    assert "old.md" not in text and "other.md" not in text
