"""/library toolbar + grant-aware ownership facets.

The Library page (the renamed, widened former /artefacts) carries a toolbar
(search · ownership segments · Type/Source facets · sort · table⇄grid view) and
is not owner-scoped: it shows what you OWN plus anything shared into a group you
belong to, each row tagged with an ownership facet the toolbar slices on. These
tests lock the toolbar markup and the mine / shared_with_me / shared_by_me
classification.
"""

from __future__ import annotations
import pytest

import io


@pytest.fixture(autouse=True)
def _rail_layout(monkeypatch):
    """This file exercises the RAIL redesign's unified /library. Topnav keeps
    the legacy collections page (the /catalog pattern) — guarded by
    tests/test_ui_layout_theme.py::TestDefaultContentParity."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(seeded_app, name: str, token: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _upload(seeded_app, cid: str, filename: str, content: bytes, ctype: str, token: str):
    return seeded_app["client"].post(
        f"/api/collections/{cid}/files",
        files={"files": (filename, io.BytesIO(content), ctype)},
        headers=_auth(token),
    )


def _share_collection_with_user(collection_id: str, user_id: str, group_name: str = "library-share-grp") -> None:
    """Add ``user_id`` to a group and grant that group the collection — the
    minimal path to a "shared with me" artefact."""
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "collection", collection_id):
        grants.create(group_id=grp["id"], resource_type="collection", resource_id=collection_id, assigned_by="test")


def test_library_toolbar_controls_render(seeded_app):
    """Search, the category-based Filter menu, sort and the view toggle render,
    plus the row attributes the client-side engine reads. The four ownership
    tabs and the Type facet are gone — items group by type instead."""
    _create(seeded_app, "Toolbar Demo", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text

    assert 'id="lib-search"' in text
    # Retired: ownership tabs + the Type facet (grouping replaced both).
    assert 'id="lib-own"' not in text
    assert 'data-own="all"' not in text
    assert 'data-facet="type"' not in text
    # Filter menu is now a list of facet CATEGORIES, each with a popover.
    assert 'id="lib-filter-btn"' in text
    assert 'id="lib-filter-menu"' in text
    assert 'class="fbar-cat"' in text
    assert "fbar-cat__pop" in text
    assert 'id="lib-chips"' in text
    assert 'id="lib-sort"' in text
    # Items are grouped into collapsible per-type sections.
    assert "data-lib-sec=" in text
    assert "data-sec-toggle" in text
    assert "data-sec-count" in text
    # Table ⇄ grid view toggle (shared .fbar-view control, table default).
    assert 'data-view="table"' in text
    assert 'data-view="grid"' in text
    # The reusable engine is loaded.
    assert "js/filter_toolbar.js" in text
    # Row attributes the facets read.
    assert 'data-ownership="mine"' in text
    assert 'data-origin="uploaded"' in text
    assert "data-requirement=" in text
    assert "data-search=" in text


def test_source_facet_offers_uploaded_option(seeded_app):
    """The Source facet exposes the artefact's provenance (origin column).
    A freshly uploaded artefact is 'uploaded' and appears as a Source option."""
    _create(seeded_app, "Prov Demo", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert 'data-facet="origin"' in text
    assert 'value="uploaded"' in text


def test_shared_collection_is_shared_with_me_for_grantee(seeded_app):
    """A collection admin owns and shares into the analyst's group shows on the
    analyst's Library as shared_with_me, attributed to the owner."""
    col = _create(seeded_app, "Board Deck", seeded_app["admin_token"])
    _share_collection_with_user(col["id"], "analyst1")

    # Analyst sees it, tagged shared_with_me, owned by "Admin".
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Board Deck" in text
    assert 'data-ownership="shared_with_me"' in text
    assert "Admin" in text  # owner label
    assert "lib-vis--shared" in text

    # The owner sees the SAME collection as shared_by_me (it now carries a grant).
    owner_text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert 'data-ownership="shared_by_me"' in owner_text


def test_unshared_collection_stays_private_to_owner(seeded_app):
    """No grant → the analyst never sees the admin's private collection, and the
    admin's own row is plain 'mine' (not shared_by_me)."""
    _create(seeded_app, "Private Notes", seeded_app["admin_token"])

    analyst_text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Private Notes" not in analyst_text

    admin_text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "Private Notes" in admin_text
    assert 'data-ownership="mine"' in admin_text


def test_files_of_every_format_share_one_files_section(seeded_app):
    """Images, documents and every other format now live in ONE top-level
    "Files" section (the per-format sections were merged), while the row itself
    still names the real format — on the name's second line, now that the Type
    column is gone — so nothing is lost."""
    col = _create(seeded_app, "Diagram", seeded_app["admin_token"])
    r = _upload(
        seeded_app, col["id"], "diagram.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40, "image/png", seeded_app["admin_token"]
    )
    assert r.status_code in (200, 201), r.text

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    # One merged section, not per-format ones.
    assert 'data-lib-sec="files"' in text
    assert ">Artefacts<" in text
    for retired in ("image", "document", "collection", "spreadsheet"):
        assert f'data-lib-sec="{retired}"' not in text
    # The row still says what the file actually is — its format, on the second
    # line of the name cell (the Type chip that used to say "Image" is retired).
    assert 'lib-name-desc">PNG<' in text


# ---------------------------------------------------------------------------
# Stack: the "In stack only" toggle + the demoted availability filter
# ---------------------------------------------------------------------------


def _add_to_stack(seeded_app, collection_id: str, token: str):
    return seeded_app["client"].post(f"/api/stack/artefacts/{collection_id}", headers=_auth(token))


def test_stack_filter_is_the_in_stack_only_toggle(seeded_app):
    """The stack filter is a pressed-state BUTTON on the bar (`.fbar-toggle`,
    the design system's own pattern for a binary refinement worth seeing at
    rest) carrying the unfiltered in-Stack tally — not a scope segment (that
    gave one filter tab-rank; tried after the fold, retired) and not a row
    inside the Filter menu (the condition must not need a click to
    discover). The acquisition question stays one level deep as the Filter
    menu's "Not in stack yet" toggle: every acquirable row already sits in
    the unfiltered list wearing its own Add pill, so nothing is hidden."""
    tok = seeded_app["admin_token"]
    added = _create(seeded_app, "Stack Added", tok)
    _create(seeded_app, "Stack Not Added", tok)
    assert _add_to_stack(seeded_app, added["id"], tok).status_code == 200

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert 'id="lib-stack-toggle"' in text
    assert 'data-facet-value="in_stack"' in text
    # The count rides the button, kept truthful by refreshStackCount after
    # an in-place membership change.
    assert "fbar-toggle__n" in text
    assert "data-stack-count" in text
    # The two-state Scope segment is retired — a filter is not a tab.
    assert 'id="lib-scope"' not in text
    assert 'data-seg="in_stack"' not in text
    # Both controls slice on the row's strict Stack membership: the button
    # keeps `in_stack` rows, the availability toggle keeps `available` ones —
    # one attribute, no ownership-blended data-scope projection to drift.
    assert 'data-stack="in_stack"' in text
    assert 'data-stack="available"' in text
    assert 'data-scope="' not in text, "the retired scope projection must not return"
    # The demoted acquisition filter: one toggle checkbox in the menu, with
    # its tally (the fixture leaves one row addable).
    assert 'data-facet="availability"' in text
    assert "Not in stack yet" in text


def test_stack_deep_link_arrives_with_the_toggle_applied(seeded_app):
    """``/library?stack=in_stack`` is the landing the chat empty state's Stack
    status line uses now that /stack is off the rail (#1088): the page arrives
    with the "In stack only" filter applied. Plain ``/library`` — and any other
    value of the param — must not."""
    tok = seeded_app["admin_token"]
    added = _create(seeded_app, "Deep Link Added", tok)
    _create(seeded_app, "Deep Link Not Added", tok)
    assert _add_to_stack(seeded_app, added["id"], tok).status_code == 200

    c = seeded_app["client"]
    assert "const STACK_ONLY = true;" in c.get("/library?stack=in_stack", headers=_auth(tok)).text
    assert "const STACK_ONLY = false;" in c.get("/library", headers=_auth(tok)).text
    # Not a free-text hook into the page's JS — anything but the facet's one
    # legal value is simply off.
    assert "const STACK_ONLY = false;" in c.get("/library?stack=whatever", headers=_auth(tok)).text


# (test_stack_toggle_shows_the_matching_item_count is folded into the main
# toggle test above: the count rides the button again — `fbar-toggle__n` +
# `data-stack-count` are pinned there.)


def test_stack_toggle_keeps_locked_admin_required_items(seeded_app):
    """A locked admin-required membership IS a membership, so the row carries the
    in-Stack state "In stack only" slices on — the tier is filterable on its own
    Optional/Required category, not by hiding the row here."""
    import uuid

    from src.db import get_system_db
    from src.repositories.data_packages import DataPackagesRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    pkg_id = DataPackagesRepository(conn).create(
        name="Mandated Toggle Package", slug="req-toggle-pkg", description="d", icon=None, color=None, created_by="t"
    )
    gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) VALUES (?, ?, 'data_package', ?, 'required', CURRENT_TIMESTAMP, 't')",
        [str(uuid.uuid4()), gid, pkg_id],
    )
    conn.close()

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    start = text.rindex("<tr", 0, text.index("Mandated Toggle Package"))
    row = text[start : text.index("</tr>", start)]
    assert "lib-instack--locked" in row  # locked membership
    assert 'data-stack="in_stack"' in row  # …and the toggle keeps it


def test_stack_toggle_absent_when_it_would_change_nothing(seeded_app):
    """No dead filters: with nothing in the Stack the toggle would only ever
    empty the page, so it doesn't render at all."""
    tok = seeded_app["analyst_token"]
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    if 'data-stack="in_stack"' not in text:
        assert 'id="lib-stack-toggle"' not in text


def test_stack_state_is_visible_on_every_row(seeded_app):
    """A filter must never hide a row for an invisible reason: an in-Stack row
    carries the badge, and an available-but-addable one the Add button."""
    tok = seeded_app["admin_token"]
    added = _create(seeded_app, "Badged", tok)
    _create(seeded_app, "Addable", tok)
    _add_to_stack(seeded_app, added["id"], tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    # Both states share the `.lib-stackpill` box (so the in-place swap can't
    # shift the row) and then diverge: `.lib-instack` is the status, the Add
    # button is the action.
    assert 'class="lib-stackpill lib-instack"' in text
    assert "data-add-to-stack=" in text


def test_granted_resources_report_in_stack_not_addable(seeded_app, monkeypatch):
    """Auto-membership (opt-in): a grant on the caller's group puts a resource
    in their Stack with no action, so those rows say "In stack" and offer no
    Add. The classic default renders granted-but-unsubscribed rows as
    not-a-member (tests/test_web_library.py::
    test_library_available_grant_classic_is_not_claimed_in_stack)."""
    import re

    monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")

    from src.db import get_system_db
    from src.repositories import data_packages_repo
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("stack-facet-grp") or groups.create(
        name="stack-facet-grp", description="t", created_by="t"
    )
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="t")
    pkg = data_packages_repo().create(
        name="Auto Stacked", slug="auto-stacked", description="d", icon=None, color=None, created_by="admin"
    )
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="data_package", resource_id=pkg, assigned_by="admin"
    )

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = re.search(r'<tr[^>]*data-type="data_package"[^>]*>', text)
    assert row, "data_package row not found"
    assert 'data-stack="in_stack"' in row.group(0)


def test_every_row_carries_a_stack_state(seeded_app):
    """A stateless row would be silently dropped by "In stack only" while its
    own pill claims membership — the filter must never hide a row for an
    invisible reason. An authored skill counts as Available: reachable, but not
    in the Stack."""
    import re

    tok = seeded_app["admin_token"]
    _create(seeded_app, "State Check", tok)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    rows = re.findall(r'<tr\b[^>]*data-item-id="[^"]+"[^>]*>', text)
    assert rows, "no library rows rendered"
    stateless = [r for r in rows if 'data-stack=""' in r]
    assert not stateless, f"{len(stateless)} row(s) have no Stack state"


# ---------------------------------------------------------------------------
# Sorting lives on the column headers, not in the toolbar
# ---------------------------------------------------------------------------


def test_sortable_columns_are_name_owner_and_sharing(seeded_app):
    """A column header is where a reader asks "order by this", so sorting moved
    out of the toolbar and onto the columns. Every column that remains sorts
    except Actions, which is not data — the unsortable Type column is gone
    entirely, for the same reason it could not sort: the list is already GROUPED
    by type into these very sections."""
    import re

    _create(seeded_app, "Sort Columns", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text

    head = text.split("<thead>", 1)[1].split("</thead>", 1)[0]
    keys = re.findall(r'data-sort-key="([^"]+)"', head)
    assert keys == ["name", "owner", "sharing"], f"sortable columns drifted: {keys}"
    assert ">Type<" not in head, "the Type column should be gone, not merely unsortable"
    before, marker, _ = head.partition(">Actions<")
    assert marker, "Actions header missing"
    assert "lib-sort" not in before.rsplit("<th", 1)[-1], "Actions must not be sortable"


def test_every_sortable_column_opens_a_to_z(seeded_app):
    """All three sort text the reader can see, so all three open ascending —
    there is no date column to want newest-first."""
    import re

    _create(seeded_app, "Sort Direction", seeded_app["admin_token"])
    head = (
        seeded_app["client"]
        .get("/library", headers=_auth(seeded_app["admin_token"]))
        .text.split("<thead>", 1)[1]
        .split("</thead>", 1)[0]
    )
    pairs = re.findall(r'data-sort-key="([^"]+)" data-sort-first="([^"]+)"', head)
    assert dict(pairs) == {"name": "asc", "owner": "asc", "sharing": "asc"}


def test_sortable_header_keeps_the_plain_column_header_look(seeded_app):
    """A sortable column is a column first and a control second: same muted
    uppercase as the headers that don't sort, with a chevron as the entire
    difference. The <button> UA style resets `text-transform`, so the header
    would otherwise read "Name" in a row of "TYPE" / "OWNER"."""
    # One item, or the type sections — and with them the <thead> — don't render.
    _create(seeded_app, "Header Look", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "text-transform: inherit; letter-spacing: inherit;" in text
    assert 'Name <span class="lib-sort__dir"' in text
    # No accent recolour on the active column — the chevron carries the state.
    assert ".lib-sort.is-sorted { color: var(--ds-text-primary); }" in text


def test_sharing_sorts_on_the_label_not_the_internal_key(seeded_app):
    """`data-visibility` ("private" / "shared" / "workspace") does not sort into
    the order the column is READ in, so the header sorts on the label the cell
    actually shows."""
    import re

    _create(seeded_app, "Shared Sort", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "sharing: 'data-sharing'" in text
    labels = re.findall(r'data-sharing="([^"]*)"', text)
    assert labels, "no row carries the Sharing sort key"
    assert any(v and not v.islower() for v in labels), f"looks like keys, not labels: {set(labels)}"


def test_toolbar_sort_select_is_grid_only(seeded_app):
    """In table view the headers ARE the sort control, so the toolbar select
    would be a second live readout of one order — it ships hidden and the engine
    reveals it only in grid view, where there are no headers to click. Hidden by
    the engine rather than by Jinja because the view is a client-side, persisted
    choice the server cannot know."""
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert '<div class="fbar-select" id="lib-sortwrap" hidden>' in text
    assert "headers: '.lib-table .lib-sort', wrap: '#lib-sortwrap'" in text

    js = seeded_app["client"].get("/static/js/filter_toolbar.js").text
    # One order, two controls, both re-synced from it.
    assert "function setSort(value)" in js
    assert "function syncSortControls()" in js
    assert "if (sortWrapEl && sortBtns.length) sortWrapEl.hidden = !grid;" in js
    # The header drives the accessible sorted state, not colour alone.
    assert "th.setAttribute('aria-sort'" in js


def test_long_category_gets_its_own_search_field(seeded_app):
    """A tag vocabulary runs to dozens of values, and past ~10 rows a category
    popover is a list you scroll rather than one you read — so the engine injects
    a search field (and a "No matches" line) into any popover holding more than
    CAT_SEARCH_MIN options. Injected from the row count in JS, never authored in
    a template, so every page and every future facet inherits it; a short
    category (the Access facet's two options) stays a plain list."""
    js = seeded_app["client"].get("/static/js/filter_toolbar.js").text
    assert "var CAT_SEARCH_MIN = 10;" in js
    assert "function setupCatSearch(cat)" in js
    assert "if (opts.length <= CAT_SEARCH_MIN) return;" in js
    # It narrows the OPTIONS of one category, not the page's rows.
    assert "e.el.hidden = !hit;" in js
    # The tally is a count, not part of a name — searching "1" must not match
    # every option on the page.
    assert "'.fbar-menu__opt-text'" in js
    # Height changed under a viewport-placed popover, so it is re-placed.
    assert "placeSubmenu(cat);" in js
    # Narrowing to nothing is silent for a screen reader unless it is announced.
    assert "none.setAttribute('role', 'status');" in js
    # Escape with a query clears the field; only an empty field lets the outer
    # handler close the menu.
    assert "if (e.key === 'Escape' && input.value)" in js
    # Focus outranks the hover grace period — typing must not lose the popover.
    assert "if (cat.contains(document.activeElement)) return;" in js

    css = seeded_app["client"].get("/static/css/filter_toolbar.css").text
    # The field stays reachable while its results scroll.
    assert ".fbar-cat__search {" in css
    assert "position: sticky; top: -6px;" in css
    # `.fbar-menu__opt` sets display:flex, which beats the UA's [hidden] rule.
    assert ".fbar-menu__opt[hidden] { display: none; }" in css


def test_chip_label_keeps_a_name_that_ends_in_a_number(seeded_app):
    """A chip reads its label off the menu option so the two can never drift.

    It used to strip a trailing number from whatever it read, on the theory
    that the text ended in the option's tally — but every caller puts the tally
    in a sibling `.fbar-menu__opt-n`, so `.fbar-menu__opt-text` holds the name
    alone and the strip ate names that legitimately end in a digit: a data
    package called "Customer 360" chipped as "Customer", a tag "Q3 2026" as
    "Q3". The strip now runs ONLY on the fallback path, where the row's own
    textContent really does carry the count.
    """
    js = seeded_app["client"].get("/static/js/filter_toolbar.js").text
    assert "var span = opt.querySelector('.fbar-menu__opt-text');" in js
    assert "if (!span) txt = txt.replace(/\\s+\\d+\\s*$/, '');" in js
