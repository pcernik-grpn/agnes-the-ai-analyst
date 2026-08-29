"""Library Files section: collections as folders, per-file pages, drag-to-move.

Every file format (images, documents, anything else) shares ONE top-level
"Files" section, with multi-file collections nested inside it as folders:

  - a single-file artefact IS its file — a loose row, draggable into a folder;
  - a multi-file collection is a folder row: a drop target that expands to its
    files, each of which has its own detail page AND its own sharing;
  - non-file kinds (skills, data packages, memory, …) stay separate top-level
    sections and are never drop targets.

Drag-and-drop is `POST /api/collections/{src}/files/{fid}/move`.
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


def _collection(seeded_app, name: str, token: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _upload(
    seeded_app,
    cid: str,
    filename: str,
    token: str,
    content: bytes = b"# doc\n" + b"x" * 300,
    ctype: str = "text/markdown",
):
    return seeded_app["client"].post(
        f"/api/collections/{cid}/files",
        files={"files": (filename, io.BytesIO(content), ctype)},
        headers=_auth(token),
    )


def _files(seeded_app, cid: str, token: str) -> list:
    r = seeded_app["client"].get(f"/api/collections/{cid}/files", headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()["files"]


def _folder(seeded_app, name: str, token: str, names=("a.md", "b.md")) -> dict:
    col = _collection(seeded_app, name, token)
    for n in names:
        assert _upload(seeded_app, col["id"], n, token).status_code in (200, 201)
    return col


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_one_files_section_holds_loose_files_and_folders(seeded_app):
    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Solo Pic", tok)
    _upload(seeded_app, solo["id"], "pic.png", tok, b"\x89PNG\r\n\x1a\n" + b"0" * 40, "image/png")
    _folder(seeded_app, "Board Pack", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert 'data-lib-sec="files"' in text
    # The per-format and standalone Collections sections are retired.
    for retired in ("image", "document", "collection", "spreadsheet", "pdf"):
        assert f'data-lib-sec="{retired}"' not in text
    # Both the loose file and the folder are in it.
    assert "Solo Pic" in text
    assert "Board Pack" in text


def test_groups_are_bands_of_one_united_list(seeded_app):
    """Every type is a group wearing the same `.fbar-group` header component My
    Stack's Required / Added-by-you split uses, and the groups sit inside ONE
    framed list (`.lib-list`) rather than as separately-boxed sections."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "United List Deck", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert text.count('<div class="lib-list">') == 1
    assert re.search(r'<section class="fbar-group lib-group[^"]*" data-lib-sec="', text)
    assert 'class="fbar-groupband lib-band"' in text
    assert 'class="fbar-grouptoggle"' in text
    # …and the retired per-section heading chrome is gone.
    assert "fbar-sec__head" not in text
    assert 'class="fbar-sec"' not in text


def test_each_group_has_its_own_column_header_on_one_shared_grid(seeded_app):
    """Each group carries its OWN <thead> — that is what lets the header be
    sticky per group — while every group repeats the SAME <colgroup>, so the
    columns of every header line up with the columns of every group's rows."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Own Header Deck", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    groups = re.findall(r'<section class="fbar-group lib-group[^"]*" data-lib-sec="([^"]+)"', text)
    assert groups
    # One table, one colgroup and one thead per group — no page-level header that
    # would pin a second row above the group's own.
    assert text.count('<table class="data-table lib-table">') == len(groups)
    assert text.count("<thead>") == len(groups)
    assert text.count('<col class="lib-col-owner">') == len(groups)
    # No Type column: the list is grouped by type into these very sections.
    assert "lib-col-type" not in text
    assert '<th scope="col">Type</th>' not in text
    # Every colgroup is the same, so the grids cannot drift apart.
    colgroups = re.findall(r"<colgroup>(.*?)</colgroup>", text, re.S)
    assert len(set(cg.split() and " ".join(cg.split()) for cg in colgroups)) == 1
    # Fixed layout is what makes the shared colgroup binding rather than advisory.
    assert "table-layout: fixed" in text


def test_group_headers_are_sticky_and_replace_one_another(seeded_app):
    """The band and its column header pin per group. Both offsets read the same
    `--lib-bandh` token so the column header lands exactly under its band, and
    the sticky boxes are the group's own section and table — a sticky table CELL
    is not constrained by its row group, so one table per group is what keeps the
    headers replacing each other instead of stacking up."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Sticky Deck", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert ".lib-band { position: sticky; top: 0;" in text
    assert "position: sticky; top: var(--lib-bandh);" in text
    assert "--lib-bandh: 40px;" in text
    assert "height: var(--lib-bandh);" in text
    # Sticky dies inside a scroll container: the primitive's `overflow: hidden`
    # on the table is overridden, the one-frame wrapper clips instead of hiding,
    # and horizontal scroll is scoped to the narrow breakpoint.
    assert ".lib-table { overflow: visible; }" in text
    assert "overflow: clip" in text
    assert "@media (max-width: 980px)" in text
    # A folded group must not leave a column header pinned behind it.
    assert ".lib-group.is-collapsed .lib-tablewrap" in text


def test_grid_view_drops_the_frame_and_the_filled_band(seeded_app):
    """The frame and the FILL are TABLE chrome. In grid view the cards carry
    their own border and background, so the list frame is dropped, the group
    header goes transparent and unpadded, and it stands off its cards by the
    grid's own gap. Which view is on is read off the projected grid being
    visible, so the styling needs no JS of its own. What does NOT come off is
    the pinning — see the sticky test below."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Grid Chrome Deck", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    grid_on = ":has(> .fbar-grid:not([hidden]))"
    # The frame goes.
    assert ".lib-list:has(> .lib-group > .fbar-grid:not([hidden]))" in text
    # The band goes transparent and unbordered — and off the cards by the grid's
    # own gap, which is a token both that padding and the veil's ramp read.
    assert f".lib-group{grid_on} > .lib-band" in text
    assert "background: none; border-bottom: 0; box-shadow: none;" in text
    assert "--lib-band-gap: 14px;" in text
    assert "padding-top: var(--lib-band-gap);" in text
    # No hover fill either — that would put the filled bar back.
    assert f".lib-group{grid_on} > .lib-band .fbar-grouptoggle" in text
    assert "padding-left: 0; padding-right: 0; background: none;" in text
    # Folded → header only, no gap kept for cards that aren't shown.
    assert f".lib-group.is-collapsed{grid_on} > .lib-band" in text
    # …while table view keeps its frame.
    assert ".lib-list { border: 1px solid var(--ds-border);" in text


def test_grid_view_keeps_the_sticky_header_and_veils_it(seeded_app):
    """Grid view pins its group headers exactly as the table does — same box
    (the group's own section), same offset (the viewport top), so switching view
    doesn't move the header. Two things make it work over cards rather than rows:

    · BOTH gaps — band-to-cards and group-to-group — are padding on the GRID,
      and while the grid is folded away the band carries a gap of its own so a
      run of folded groups doesn't close up to bare headings 40px apart. It
      takes the narrower `--lib-band-gap` for that: the wider group gap is sized
      to clear a bordered card's bottom edge, and a folded group has no card.
      Everywhere it is PADDING, never a margin and never on the group: a sticky
      box is confined to its parent's CONTENT box, so group padding does not
      extend the pin and a bottom margin shortens it; either way the outgoing
      header unpins early and leaves a strip of page with no header on it.
    · a frosted veil instead of a fill — a wash of the surface the page actually
      paints (`--ds-surface`, what the index shell puts behind the list, NOT
      `--ds-bg`, which would read as a grey bar over a white page) plus a blur,
      ramped out at both ends, on a pseudo-element so `backdrop-filter` does not
      become the containing block for the cards' fixed row menus.
    """
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Grid Sticky Deck", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    css = text.split("</style>")[0]
    grid_on = ":has(> .fbar-grid:not([hidden]))"
    # ONE sticky rule, both views — grid never unpins the band.
    assert ".lib-band { position: sticky; top: 0;" in text
    assert "position: static" not in css
    # Both gaps sit inside the grid, so the group's content box runs right up to
    # the next group's band and the handoff is exact.
    assert f".lib-group{grid_on} > .fbar-grid" in text
    assert "--lib-group-gap: 22px;" in text
    assert "padding-bottom: var(--lib-group-gap);" in text
    assert f".lib-group{grid_on}:last-child > .fbar-grid {{ padding-bottom: 0; }}" in text
    # Folded, the band carries the narrower gap in the grid's place — 54px of
    # pitch rather than a bare 40 — and the last group still trails nothing.
    assert f".lib-group.is-collapsed{grid_on} > .lib-band" in text
    assert "padding-bottom: var(--lib-band-gap);" in text
    assert f".lib-group.is-collapsed{grid_on}:last-child > .lib-band" in text
    # Neither of the two things that would shorten the pin may come back.
    assert f".lib-group{grid_on} + .lib-group" not in text
    assert "margin-bottom: var(--lib-band-gap)" not in css
    assert "margin-bottom: var(--lib-group-gap)" not in css
    # The veil: pseudo-element, behind the type, blurred, ramped at both ends,
    # and washed in the colour the page itself paints.
    assert f".lib-group{grid_on} > .lib-band::before" in text
    assert "backdrop-filter: blur(8px);" in text
    assert "background: color-mix(in srgb, var(--ds-surface) 92%, transparent);" in text
    assert "--lib-veil-up: 10px;" in text
    assert "--lib-veil-down: var(--lib-band-gap);" in text
    assert "mask-image: linear-gradient(to bottom, transparent 0, black var(--lib-veil-up)," in text
    # Reduced transparency gets the readability without the glass.
    assert "@media (prefers-reduced-transparency: reduce)" in text


def test_group_header_carries_label_count_and_hint(seeded_app):
    """The header is the whole component: collapse caret, kind glyph, label, a
    live count, and one line saying what the group holds (the slot My Stack
    fills with "Optional resources you added.")."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Hinted Deck", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    head = re.search(r'<button type="button" class="fbar-grouptoggle".*?</button>', text, re.S).group(0)
    assert 'data-sec-toggle="files"' in head  # keyed, so both hosts fold together
    # Rendered folded — the page's default. The script re-opens whatever the
    # caller has stored; rendering open would flash the unfolded list first.
    assert 'aria-expanded="false"' in head
    assert "fbar-group__caret" in head
    assert "lib-sec-icon" in head  # the per-type glyph
    assert ">Artefacts<" in head
    assert "data-sec-count" in head
    assert "Files you upload, outputs your agent generates, and your hosted data apps." in head


def test_groups_render_folded_in_a_fixed_order(seeded_app):
    """Every group arrives FOLDED, and they arrive in one canonical order.

    Folded is the whole point of the ordering: the first screen is the list of
    sections itself, so their sequence is what a caller reads on arrival rather
    than an incidental detail below the fold. The order runs outward from what
    an agent is built on — governed data, then the capabilities acting on it,
    then the caller's own files, with curated Memory last.

    Asserted as a SUBSEQUENCE so the test doesn't depend on which kinds the
    fixture happens to seed: whatever renders must appear in this relative
    order, and a new kind added to the middle doesn't break it.
    """
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Ordered Deck", tok)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    canonical = ["data_package", "plugin", "skill", "agent", "recipe", "files", "memory_domain"]
    rendered = re.findall(r'<section class="fbar-group lib-group[^"]*" data-lib-sec="([^"]+)"', text)
    assert rendered, "no groups rendered"
    assert rendered == [k for k in canonical if k in rendered]

    # …and every one of them is folded, class and ARIA agreeing.
    sections = re.findall(r'<section class="(fbar-group lib-group[^"]*)" data-lib-sec=', text)
    assert all("is-collapsed" in cls for cls in sections)
    assert text.count('class="fbar-grouptoggle" data-sec-toggle') == len(rendered)
    assert 'aria-expanded="true"' not in text.split('<div class="lib-list">', 1)[1]


def test_folder_row_is_a_drop_target_and_expands(seeded_app):
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Expandable", tok)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert 'data-folder="1"' in text
    assert 'data-drop-target="1"' in text
    assert "data-folder-toggle=" in text
    # Its files are rendered as child rows, hidden until expanded.
    assert "data-parent-id=" in text
    assert "lib-row--child" in text


def test_loose_file_is_draggable_and_folder_is_not(seeded_app):
    """Files drag INTO folders; folders don't nest, so they never drag."""
    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Draggable One", tok)
    _upload(seeded_app, solo["id"], "one.md", tok)
    _folder(seeded_app, "Target Folder", tok)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert 'draggable="true"' in text
    assert 'draggable="false"' in text


def test_section_count_reflects_the_hierarchy(seeded_app):
    """The Files badge counts TOP-LEVEL entries — a folder counts once, not
    once per file inside it."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Count Solo", tok)
    _upload(seeded_app, solo["id"], "s.md", tok)
    _folder(seeded_app, "Count Folder", tok, names=("x.md", "y.md", "z.md"))

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    m = re.search(r'data-lib-sec="files".*?data-sec-count>(\d+)<', text, re.S)
    assert m, "files section badge not found"
    # 2 top-level entries (one loose file + one folder), not 4 files.
    assert int(m.group(1)) == 2


def test_collections_are_listed_before_loose_files(seeded_app):
    """Inside Files the collections form their own block at the top — the
    containers first, the loose files after them."""
    import re

    tok = seeded_app["admin_token"]
    # Created loose-file-first, so recency order alone would put it on top.
    solo = _collection(seeded_app, "Order Solo", tok)
    _upload(seeded_app, solo["id"], "solo.md", tok)
    _folder(seeded_app, "Order Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    # Scoped to the group's own <tbody> — every group shares one table now, so
    # reading to `</table>` would sweep in the groups below it.
    body = re.search(r'data-lib-sec="files".*?</tbody>', text, re.S)
    assert body, "files section not found"
    rows = re.findall(r'<tr class="lib-row([^"]*)"', body.group(0))
    tops = [r for r in rows if "lib-row--child" not in r]
    # Every folder row precedes every loose-file row.
    kinds = ["folder" if "lib-row--folder" in r else "file" for r in tops]
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "folder" else 1), kinds
    assert "folder" in kinds and "file" in kinds


def test_collection_rows_wear_a_folder_glyph_and_files_do_not(seeded_app):
    """A collection is visually a folder in the Files table; a loose file keeps
    the document glyph, so the two never read as the same kind of thing."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Glyph Solo", tok)
    _upload(seeded_app, solo["id"], "g.md", tok)
    _folder(seeded_app, "Glyph Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    folder_row = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S)
    file_row = re.search(r'<tr class="lib-row lib-row--file".*?</tr>', text, re.S)
    assert folder_row and file_row
    assert "lib-icon--folder" in folder_row.group(0)
    assert "M4 7.5A1.5 1.5 0 0 1 5.5 6" in folder_row.group(0)  # folder glyph
    assert "lib-icon--folder" not in file_row.group(0)
    assert "M7 4h7l4 4v12H7z" in file_row.group(0)  # single-document glyph


def test_every_row_is_the_link_and_carries_no_open_button(seeded_app):
    """The whole row opens the item — there is no per-row Open button."""
    import re

    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Clickable Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    row = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S)
    assert row
    assert f'data-href="/library/{col["slug"]}"' in row.group(0)
    assert ">Open" not in row.group(0)


def test_every_section_shares_one_fixed_column_grid(seeded_app):
    """Each section owns its own <table>, so without a shared colgroup their
    columns drift apart and Type/Sharing/Actions land at a different x per
    section. Fixed layout + one colgroup is what keeps the page on one grid —
    and what makes a nested file row ride its folder's columns exactly."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Grid Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert "table-layout: fixed" in text
    tables = re.findall(r"<table class=\"data-table lib-table\">(.*?)</table>", text, re.S)
    assert tables, "no library tables rendered"
    groups = [re.search(r"<colgroup>.*?</colgroup>", t, re.S) for t in tables]
    assert all(groups), "a section rendered without the shared column grid"
    # Byte-identical colgroup in every section = identical widths everywhere.
    assert len({re.sub(r"\s+", " ", g.group(0)) for g in groups}) == 1


def test_nested_files_use_the_plain_row_with_a_connector(seeded_app):
    """Nesting is carried by indentation + a connector rail, not a tinted band:
    a child row is the same white row as any other, and the last child is marked
    so its rail can stop at the elbow."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Connector Folder", tok, names=("c1.md", "c2.md"))

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    kids = re.findall(r'<tr class="lib-row lib-row--file lib-row--child[^"]*"', text)
    assert len(kids) >= 2
    assert any("lib-row--lastchild" in k for k in kids), "last child not marked"
    # Exactly one last-child per folder.
    assert sum(1 for k in kids if "lib-row--lastchild" in k) == 1


def test_actions_hold_only_the_reserved_stack_slot(seeded_app):
    """One fixed slot keeps the Stack control at the same x on every row of every
    section. Sharing is NOT here any more — the Sharing column's badge is the
    control, so a separate share icon would be a second door to one room."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Slots Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    row = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S).group(0)
    assert 'class="lib-slot lib-slot--stack"' in row
    assert "lib-slot--share" not in row
    assert "lib-iconbtn" not in text


def test_no_row_carries_a_type_badge(seeded_app):
    """The Type column is gone — the list is grouped by type into the sections the
    rows are drawn in, so a per-row chip restated the band above it. A file's
    format, the one thing the chip said that the grouping does not, moved to the
    name's second line (see the format tests below)."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Badge Solo", tok)
    _upload(seeded_app, solo["id"], "b.md", tok)
    _folder(seeded_app, "Badge Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    folder = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S).group(0)
    file_row = re.search(r'<tr class="lib-row lib-row--file".*?</tr>', text, re.S).group(0)
    assert "lib-typechip" not in text
    # Four cells per row, not five — the chip's cell is gone, not just emptied.
    assert len(re.findall(r"<td[ >]", folder)) == 4
    assert len(re.findall(r"<td[ >]", file_row)) == 4
    # The type's WORDS still ride the row, as an ATTRIBUTE and only that: the grid
    # card names the kind on its metadata line and is projected from the row, so it
    # needs them even though no cell renders them.
    assert 'data-type-label="Collection"' in folder
    assert ">Collection<" not in folder
    assert 'data-type-label="Document"' in file_row
    assert ">Document<" not in file_row


def test_file_rows_show_their_format_where_the_description_was(seeded_app):
    """A file's second line is its FORMAT ("MD", "PNG"), not the boilerplate
    sentence every file shared. A collection keeps its file count there, and a
    nested child file — which never had a description at all — gets a format line
    too."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Format Solo", tok)
    _upload(seeded_app, solo["id"], "notes.md", tok)
    folder = _collection(seeded_app, "Format Folder", tok)
    _upload(seeded_app, folder["id"], "one.png", tok, content=b"\x89PNG\r\n\x1a\n" + b"p" * 300, ctype="image/png")
    _upload(seeded_app, folder["id"], "two.csv", tok, content=b"a,b\n1,2\n" + b"3,4\n" * 60, ctype="text/csv")

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    def _desc(row: str) -> str:
        m = re.search(r'<span class="lib-name-desc">(.*?)</span>', row, re.S)
        return m.group(1).strip() if m else ""

    loose = re.search(r'<tr class="lib-row lib-row--file"(?:(?!</tr>).)*?Format Solo.*?</tr>', text, re.S)
    assert loose, "the single-file artefact must render as a loose file row"
    assert _desc(loose.group(0)) == "MD"
    # The retired boilerplate is gone from the row entirely.
    assert "A private file — searchable by your agents." not in loose.group(0)

    folder_row = re.search(r'<tr class="lib-row lib-row--folder"(?:(?!</tr>).)*?Format Folder.*?</tr>', text, re.S)
    assert folder_row, "a 2-file artefact must render as a folder row"
    assert _desc(folder_row.group(0)) == "2 files"

    # Children: titled by filename, second line = their own format. No closing
    # quote in the pattern — the last child carries `lib-row--lastchild` too.
    kids = re.findall(r'<tr class="lib-row lib-row--file lib-row--child.*?</tr>', text, re.S)
    kid_formats = {_desc(k) for k in kids}
    assert {"PNG", "CSV"} <= kid_formats, kid_formats
    # The format also rides the row, for the in-place file⇄collection transitions.
    assert 'data-file-format="MD"' in loose.group(0)


def test_sharing_badge_is_the_control_when_you_own_it(seeded_app):
    """On an item the caller owns the badge IS the sharing control: a button with
    a chevron that opens the dialog. It must not double as row navigation."""
    import re

    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Editable Sharing", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    row = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S).group(0)
    badge = re.search(r"<button[^>]*lib-vis[^>]*>", row)
    assert badge, "the owned row's sharing badge is not a button"
    assert "lib-vis--editable" in badge.group(0)
    assert f'data-share="{col["id"]}"' in badge.group(0)
    assert f"change who can see {col['name']}" in badge.group(0)
    assert "lib-vis__caret" in row  # the "editable" cue


def test_sharing_badge_is_read_only_when_shared_with_you(seeded_app):
    """An item shared WITH the caller keeps a plain badge whose tooltip says the
    owner controls access — a control that cannot act is worse than none."""
    import re

    from src.db import get_system_db
    from src.repositories import data_packages_repo
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("ro-badge-grp") or groups.create(name="ro-badge-grp", description="t", created_by="t")
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="t")
    pkg = data_packages_repo().create(
        name="RO Badge Data", slug="ro-badge-data", description="d", icon=None, color=None, created_by="admin"
    )
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="data_package", resource_id=pkg, assigned_by="admin"
    )

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = re.search(r'<tr[^>]*data-type="data_package".*?</tr>', text, re.S).group(0)
    assert "only the owner can change" in row
    assert "lib-vis--editable" not in row
    assert "lib-vis__caret" not in row
    # No share affordance at all on something you can't re-share.
    assert "data-share=" not in row
    # Its own COLOUR, not just its own tooltip: it must not wear the primary tint,
    # which now means exactly "you can change this". Shape alone (a missing
    # chevron) is too quiet a signal for "this one does nothing when you press it".
    # It needs no colour rule of its own — the base `.lib-vis` IS that grey.
    assert "lib-vis--readonly" in row


def test_sharing_badge_colour_means_changeable_not_visibility():
    """The chip's hue answers one question — "can I change this?" — and nothing
    else. Blue only on the badge that opens the dialog that changes access; grey on
    everything else, including the store-entity badge that opens a dialog which
    merely explains the model. Keyed off editability, never off the visibility
    VALUE: value and editability don't correlate (your own `Private` upload is
    changeable, someone else's `Everyone` item is not), so colouring by value made
    two rows with the same colour offer opposite affordances."""
    from pathlib import Path

    library = Path("app/web/templates/library.html").read_text(encoding="utf-8")

    # Blue belongs to editability, and to nothing else.
    assert ".lib-vis--editable { color: var(--ds-primary)" in library
    for value in ("--shared", "--workspace", "--private"):
        assert f".lib-vis{value} {{" not in library, f"the {value} VALUE must not carry a colour"
    # …and `--fixed` takes it back for the interactive-but-unchangeable case. It
    # must be declared AFTER --editable, which it accompanies, or it loses on equal
    # specificity.
    assert ".lib-vis--fixed {" in library
    assert library.index(".lib-vis--editable {") < library.index(".lib-vis--fixed {")
    # Grey is the neutral family, not the old violet read-only trio.
    assert "--ds-readonly" not in library
    fixed_rule = library.split(".lib-vis--fixed {", 1)[1].split("}", 1)[0]
    assert "--ds-text-muted" in fixed_rule and "--ds-primary" not in fixed_rule

    fbar = Path("app/web/static/css/filter_toolbar.css").read_text(encoding="utf-8")
    assert "--ds-readonly" not in fbar
    fixed_card = fbar.split(".fbar-card__access--fixed {", 1)[1].split("}", 1)[0]
    assert "--ds-primary" not in fixed_card


def test_card_sharing_control_states_changeability_by_box_not_hue():
    """On the grid CARD the same distinction is carried by FORM, not by ink.

    The card cannot use the table's hue rule: `.fbar-card:hover` already turns the
    title primary and `:focus-within` draws a primary ring, so primary-at-rest on
    the sharing control competed with the card's own ambient hover — and beside a
    filled, semibold `Add to stack` pill, blue 400-weight text with no container
    read as coloured metadata rather than as a control.

    So the contract here is: `--editable` gets a BORDERED BOX at rest (never a
    fill — that would double the footer's blue against the primary action), the
    blue arrives on hover as a background CHANGE (a different channel from the
    card's elevation hover), and `--fixed` stays bare text so box-vs-no-box is what
    separates "you can change this" from "you cannot"."""
    from pathlib import Path

    fbar = Path("app/web/static/css/filter_toolbar.css").read_text(encoding="utf-8")
    editable = fbar.split(".fbar-card__access--editable {", 1)[1].split("}", 1)[0]

    # A box at rest — and NOT the primary fill/ink the old version leaned on.
    assert "border-color: var(--ds-border)" in editable
    assert "border-radius" in editable
    assert "--ds-primary" not in editable, "the card chip must not be primary at rest"
    # Target size: 26px matches .lib-stackpill and clears WCAG 2.2 SC 2.5.8, which
    # the previous 20px control failed (the adjacent 26px pill denies it the
    # spacing exemption).
    assert "min-height: 26px" in editable

    # Hover is a FILL, not a hue nudge: #0284c7 -> #0369a1 on 12px text was the
    # entire previous response, and it fired while the whole card was lifting.
    hover = fbar.split("button.fbar-card__access--editable:hover,", 1)[1].split("}", 1)[0]
    assert "background: var(--ds-primary-light)" in hover
    assert "color: var(--ds-primary)" in hover

    # Bare text where nothing can change — no box, so the distinction survives a
    # 40-card scan without relying on colour.
    fixed_card = fbar.split(".fbar-card__access--fixed {", 1)[1].split("}", 1)[0]
    assert "border-color" not in fixed_card and "min-height: 26px" not in fixed_card

    # The chevron the card used to drop: present in the table badge, and now
    # emitted for the card's editable control too (a second, non-colour cue —
    # WCAG 1.4.1).
    library = Path("app/web/templates/library.html").read_text(encoding="utf-8")
    card_fn = library.split("access.className = 'fbar-card__access'", 1)[1]
    assert "VIS_CARET_SVG" in card_fn.split("foot.appendChild(access)", 1)[0]


def test_sharing_vocabulary_has_one_source():
    """One set of words for who-can-see-this, owned by the server.

    The card used to keep its own map ("Only you" / "Specific groups" /
    "Everyone") against the table's ("Private" / "Shared" / "Workspace"), so most
    rows called their own state something different depending on the view — and
    because that map only knew the three scope keys, a skill pending review
    (correctly "In review" on its row) printed "Only you" on its card, which was
    not a wording difference but a false statement.

    The card now prints `data-sharing` — whatever the server rendered — so review
    states arrive without being enumerated client-side and the views cannot drift.
    """
    from pathlib import Path

    from app.services.artefact_access import VISIBILITY_LABELS

    assert VISIBILITY_LABELS == {
        "private": "Private",
        "shared": "Specific groups",
        # "Everyone", never "Workspace": `workspace` is the internal key, and the
        # product calls that scope the organization everywhere it addresses a user.
        "workspace": "Everyone",
    }

    library = Path("app/web/templates/library.html").read_text(encoding="utf-8")
    # The competing card-side maps are gone for good.
    assert "ACCESS_LABEL" not in library
    assert "ACCESS_TITLE" not in library
    # The card reads the server's word off the row…
    assert "row.dataset.sharing" in library
    # …and a saved share change refreshes that attribute, or the card (and the
    # Sharing column's sort, which reads the same attribute) would go stale.
    upd = library.split("function updateRowVisibility(", 1)[1].split("\n  }", 1)[0]
    assert "row.dataset.sharing = label" in upd


def test_details_column_is_retired_and_a_collection_shows_its_contents(seeded_app):
    """The Details column is gone. A COLLECTION now says what's inside it where a
    description would go ("2 files") — more useful than boilerplate prose, and the
    only place the count still shows in the table. A file says its FORMAT there
    (the boilerplate sentence it used to print is gone with the Type column); both
    carry the meta text on the row for the grid cards to render."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Details Solo", tok)
    _upload(seeded_app, solo["id"], "d.md", tok)
    _folder(seeded_app, "Details Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert "<th>Details</th>" not in text
    assert "lib-col-details" not in text
    # Four columns: Name · Owner · Sharing · Actions — in each group's own header
    # (they are identical; check the first). Type went the way of Details.
    head = re.search(r"<thead>.*?</thead>", text, re.S).group(0)
    # `<th[ >]` so the count doesn't also match the enclosing <thead>.
    assert len(re.findall(r"<th[ >]", head)) == 4

    folder = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S).group(0)
    assert re.search(r'lib-name-desc">\s*2 files\s*<', folder), "collection does not show its contents"
    assert 'data-meta="2 files"' in folder
    loose = re.search(r'<tr class="lib-row lib-row--file".*?</tr>', text, re.S).group(0)
    assert re.search(r'lib-name-desc">\s*MD\s*<', loose)  # a file shows its format
    # …not the boilerplate it replaced. Scoped to the rendered line: the sentence
    # legitimately survives (lowercased) in `data-search`, so searching the words
    # of a file's description still finds it.
    assert not re.search(r'lib-name-desc">[^<]*searchable by your agents', loose)
    assert 'data-meta="d.md' in loose  # …and its meta rides the row


def test_added_column_is_retired(seeded_app):
    """The Added column is gone from the table — the timestamp still rides the row
    as `data-added` (the sort reads it) and shows on the grid cards' metadata
    line, but it no longer spends a column of its own."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "No Added Column", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert "<th>Added</th>" not in text
    assert "lib-col-added" not in text
    # The data the sort needs is still there.
    assert "data-added=" in text
    assert 'value="added_desc"' in text


def test_rows_carry_what_an_in_place_move_needs(seeded_app):
    """A drag or keyboard move reconciles the table in the browser instead of
    reloading, so each row has to carry the facts that rebuild it: the target's
    slug (the moved file's per-file URL), the file's own name (its title as a
    child), and the collection's file count (to bump)."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Move Hooks Solo", tok)
    _upload(seeded_app, solo["id"], "hook.md", tok)
    col = _folder(seeded_app, "Move Hooks Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    folder = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S).group(0)
    assert f'data-slug="{col["slug"]}"' in folder
    assert 'data-files="2"' in folder
    loose = re.search(r'<tr class="lib-row lib-row--file".*?</tr>', text, re.S).group(0)
    assert 'data-filename="hook.md"' in loose
    assert "data-file-id=" in loose


def test_a_movable_file_has_a_keyboard_move_control(seeded_app):
    """Dragging is pointer-only, so every draggable file also carries a real
    BUTTON whose menu lists the collections it can move into. Non-file kinds get
    neither — they can't go into a collection at all."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Keyboard Move", tok)
    _upload(seeded_app, solo["id"], "kb.md", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    loose = re.search(r'<tr class="lib-row lib-row--file".*?</tr>', text, re.S).group(0)
    grip = re.search(r"<button[^>]*data-move-file[^>]*>", loose)
    assert grip, "no keyboard move control on a movable file"
    assert 'aria-haspopup="true"' in grip.group(0)
    assert "Move Keyboard Move into a collection" in grip.group(0)
    # A collection is a target, never a thing you move into one.
    folder_rows = re.findall(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S)
    for fr in folder_rows:
        assert "data-move-file" not in fr


def test_sections_carry_their_detail_page_accent(seeded_app):
    """A type's section is coloured with the SAME --ds-kind-* token its detail
    page hero resolves, so the colour is the type's identity end to end."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Accent Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert "--lib-kind: var(--ds-kind-library)" in text
    assert "--lib-kind-soft: var(--ds-kind-library-soft)" in text


def test_non_file_kinds_are_separate_sections_and_never_drop_targets(seeded_app):
    """Skills / data packages / memory stay top-level and can't take files."""
    import re

    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories import data_packages_repo

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("files-sec-grp") or groups.create(name="files-sec-grp", description="t", created_by="t")
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="t")
    pkg = data_packages_repo().create(
        name="Sec Data", slug="sec-data", description="d", icon=None, color=None, created_by="admin"
    )
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="data_package", resource_id=pkg, assigned_by="admin"
    )

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert 'data-lib-sec="data_package"' in text
    # The data-package row is not a folder and not a drop target.
    row = re.search(r'<tr[^>]*data-type="data_package"[^>]*>', text)
    assert row, "data_package row not found"
    assert "data-drop-target" not in row.group(0)
    assert 'data-folder="1"' not in row.group(0)


# ---------------------------------------------------------------------------
# Per-file detail page + per-file sharing
# ---------------------------------------------------------------------------


def test_file_inside_a_folder_has_its_own_detail_page(seeded_app):
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Detail Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    r = seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}", headers=_auth(tok))
    assert r.status_code == 200
    # It names the file, links back to its folder, and shows its OWN sharing.
    assert f"/library/{col['slug']}" in r.text
    # The visibility chip is the SHARED `detail.visibility_chip(…)` component
    # now (`.detail-vis`), not a page-local `.lfd-vis` — sharing is a concept
    # every resource has, so every detail page draws it the same way.
    assert "detail-vis--private" in r.text
    # …and the chip is the CONTROL: it opens the shared sharing dialog for this
    # file's own resource, which is what "share an individual file" means here.
    assert 'data-share-type="corpus_file"' in r.text
    assert f'data-share-id="{fid}"' in r.text
    assert "js/components/share_dialog.js" in r.text


def test_file_detail_uses_the_shared_detail_scaffold(seeded_app):
    """The file page is a detail page like any other (data package, plugin,
    table): the shared hero + section cards from macros/_detail.html — and it
    presents the file only. No in-page ask/search box, and no upload drop-zone
    (uploading targets a COLLECTION, so it lives on the collection page)."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Scaffold Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    text = seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}", headers=_auth(tok)).text
    # Shared scaffold, not a bespoke header.
    assert "detail-hero" in text
    assert "detail-section" in text
    assert "detail-hero--compact" in text  # library item pages use the compact header
    # Presents the file. Under the editorial (paper) layout — the default since
    # Wave 0, 2026-08 — the FACTS move to the rail as a lookup and the main
    # column carries the two things worth a sentence; the blue layout keeps
    # them as the ">Details<" / ">Sharing<" cards this used to assert.
    assert ">How sharing works here<" in text
    assert ">Indexing<" in text
    # Neither an ask box nor an upload zone.
    assert "Ask this file" not in text
    assert "Add files" not in text
    assert 'id="lib-drop"' not in text


def test_file_detail_leads_with_a_preview_action(seeded_app):
    """The first thing you want from a file's page is to see the file, so
    Preview is the primary hero action and opens the shared modal."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Preview Action Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    text = seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}", headers=_auth(tok)).text
    assert 'id="lfd-preview-btn"' in text
    assert "js/file_preview.js" in text
    assert "css/file_preview.css" in text
    assert "openFilePreview" in text


def test_collection_rows_preview_in_place_and_stay_links(seeded_app):
    """A row click previews the file without leaving the collection — but the
    row is still a real link to the file's own page, so Cmd/middle-click, the
    context menu and a no-JS load all still work."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Row Preview Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    text = seeded_app["client"].get(f"/library/{col['slug']}", headers=_auth(tok)).text
    assert f'href="/library/{col["slug"]}/f/{fid}"' in text
    assert f'data-preview-file="{fid}"' in text
    assert f'data-preview-collection="{col["id"]}"' in text
    assert "js/file_preview.js" in text


def test_file_detail_404s_for_wrong_collection_and_unknown_file(seeded_app):
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Guard Folder", tok)
    other = _folder(seeded_app, "Other Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    c = seeded_app["client"]
    assert c.get(f"/library/{other['slug']}/f/{fid}", headers=_auth(tok)).status_code == 404
    assert c.get(f"/library/{col['slug']}/f/cf_nope", headers=_auth(tok)).status_code == 404
    # Someone with no access to the parent gets 404, not 403.
    assert c.get(f"/library/{col['slug']}/f/{fid}", headers=_auth(seeded_app["analyst_token"])).status_code == 404


def test_a_single_file_can_be_shared_without_its_folder(seeded_app):
    """The point of per-file sharing: one file goes out, the folder stays put."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Mixed Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    c = seeded_app["client"]
    gs = c.get("/api/sharing/groups", headers=_auth(tok)).json()
    everyone = next(g for g in gs if g["is_everyone"])
    r = c.put(f"/api/sharing/corpus_file/{fid}", json={"group_ids": [everyone["id"]]}, headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "workspace"
    # The folder itself is untouched.
    assert c.get(f"/api/sharing/collection/{col['id']}", headers=_auth(tok)).json()["visibility"] == "private"
    # And the Library shows the file's own state.
    text = c.get("/library", headers=_auth(tok)).text
    assert 'data-share-type="corpus_file"' in text


def test_detail_pages_make_the_sharing_badge_the_control(seeded_app, monkeypatch):
    """One sharing control per page, and it is the badge — the same thing the
    Library row's badge is. The "Manage sharing" button that sat beside it in
    the rail, and the header item that duplicated it, are gone: both pointed at
    `/library?share=…`, a URL no route handles, so the badge is now the only
    control AND the only place the state is stated."""
    monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Badge Control Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]
    c = seeded_app["client"]

    for url, rtype, rid in (
        (f"/library/{col['slug']}", "collection", col["id"]),
        (f"/library/{col['slug']}/f/{fid}", "corpus_file", fid),
    ):
        text = c.get(url, headers=_auth(tok)).text
        assert "detail-vis--editable" in text, url
        assert "data-share-open" in text, url
        assert f'data-share-type="{rtype}"' in text, url
        assert f'data-share-id="{rid}"' in text, url
        # The dialog the badge opens is the shared component, not page markup.
        assert "js/components/share_dialog.js" in text, url
        assert 'id="shareModal"' not in text, url
        # No redundant second control, and no dead sharing URL anywhere.
        assert "Manage sharing" not in text, url
        assert "?share=" not in text, url


def test_sharing_badge_is_a_read_out_when_the_caller_cannot_reshare(seeded_app, monkeypatch):
    """A badge that opens a dialog whose save would 404 is worse than a plain
    read-out, so the control is only rendered for a caller who owns the item.
    An admin-owned collection granted to the analyst's group is readable by
    them — and re-shareable by nobody but its owner."""
    import re

    monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Granted Folder", tok)

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("readers-badge") or groups.create(
        name="readers-badge", description="t", created_by="admin1"
    )
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="admin1")
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="collection", resource_id=col["id"], assigned_by="admin1"
    )

    text = seeded_app["client"].get(f"/library/{col['slug']}", headers=_auth(seeded_app["analyst_token"])).text
    # A plain read-out (`<span>`), not the control (`<button>`) — matched on the
    # markup, since the scaffold's stylesheet always carries the editable rule.
    chip = re.search(r'<(span|button)[^>]*class="detail-vis[^"]*"', text)
    assert chip, "the reader still learns who can see this"
    assert chip.group(1) == "span"
    assert "detail-vis--editable" not in chip.group(0)
    assert "data-share-open" not in text
    # Nothing to open, so the dialog is not shipped either.
    assert "js/components/share_dialog.js" not in text


def test_per_file_sharing_is_owner_scoped(seeded_app):
    """A non-owner can neither read nor change a file's sharing (404)."""
    col = _folder(seeded_app, "Owned Folder", seeded_app["admin_token"])
    fid = _files(seeded_app, col["id"], seeded_app["admin_token"])[0]["file_id"]
    other = _auth(seeded_app["analyst_token"])
    c = seeded_app["client"]
    assert c.get(f"/api/sharing/corpus_file/{fid}", headers=other).status_code == 404
    assert c.put(f"/api/sharing/corpus_file/{fid}", json={"group_ids": []}, headers=other).status_code == 404


# ---------------------------------------------------------------------------
# Drag-and-drop move
# ---------------------------------------------------------------------------


def test_move_puts_a_loose_file_into_a_folder_and_tidies_the_husk(seeded_app):
    """Dragging a single-file artefact into a folder must not strand an empty
    collection in the listing — a loose file IS its collection."""
    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Lonely File", tok)
    _upload(seeded_app, solo["id"], "lonely.md", tok)
    folder = _folder(seeded_app, "Home Folder", tok)
    fid = _files(seeded_app, solo["id"], tok)[0]["file_id"]

    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{solo['id']}/files/{fid}/move",
        json={"target_collection_id": folder["id"]},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text
    assert r.json()["source_emptied"] is True
    assert len(_files(seeded_app, folder["id"], tok)) == 3
    # The emptied source is gone from the Library.
    assert "Lonely File" not in c.get("/library", headers=_auth(tok)).text


def test_move_between_folders_keeps_both(seeded_app):
    tok = seeded_app["admin_token"]
    src = _folder(seeded_app, "Src Folder", tok, names=("p.md", "q.md"))
    dst = _folder(seeded_app, "Dst Folder", tok, names=("r.md", "s.md"))
    fid = _files(seeded_app, src["id"], tok)[0]["file_id"]

    r = seeded_app["client"].post(
        f"/api/collections/{src['id']}/files/{fid}/move",
        json={"target_collection_id": dst["id"]},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text
    assert r.json()["source_emptied"] is False
    assert len(_files(seeded_app, src["id"], tok)) == 1
    assert len(_files(seeded_app, dst["id"], tok)) == 3


def test_move_rejects_same_collection_and_unknown_target(seeded_app):
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Reject Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]
    c = seeded_app["client"]

    r = c.post(
        f"/api/collections/{col['id']}/files/{fid}/move", json={"target_collection_id": col["id"]}, headers=_auth(tok)
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "same_collection"

    r = c.post(
        f"/api/collections/{col['id']}/files/{fid}/move", json={"target_collection_id": "col_nope"}, headers=_auth(tok)
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "target_not_found"


def test_move_requires_access_to_the_target(seeded_app):
    """Both ends are gated — a caller can't push a file into a collection they
    can't reach (and gets 404, never a hint that it exists)."""
    admin, analyst = seeded_app["admin_token"], seeded_app["analyst_token"]
    mine = _folder(seeded_app, "Analyst Folder", analyst)
    theirs = _collection(seeded_app, "Admin Only", admin)
    fid = _files(seeded_app, mine["id"], analyst)[0]["file_id"]

    r = seeded_app["client"].post(
        f"/api/collections/{mine['id']}/files/{fid}/move",
        json={"target_collection_id": theirs["id"]},
        headers=_auth(analyst),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "target_not_found"


# ---------------------------------------------------------------------------
# Drop one file on another: propose a new collection
# ---------------------------------------------------------------------------


def test_loose_file_is_a_pair_target_and_names_the_gesture(seeded_app):
    """Dropping a file on a COLLECTION moves it in; dropping it on another FILE
    proposes a collection made of the two. Two gestures over the same drag, so
    each target has to say which one it is — a different hook and a different
    label, never the ring alone."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Pairable One", tok)
    _upload(seeded_app, solo["id"], "one.md", tok)
    _folder(seeded_app, "Absorbing Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    loose = re.search(r'<tr class="lib-row lib-row--file".*?</tr>', text, re.S).group(0)
    assert 'data-pair-target="1"' in loose
    assert "New collection" in loose
    # …and it is NOT a move target: a file cannot absorb a file.
    assert "data-drop-target" not in loose

    folder = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S).group(0)
    assert 'data-drop-target="1"' in folder
    assert "Drop here" in folder
    assert "data-pair-target" not in folder


def test_a_file_inside_a_collection_is_not_a_pair_target(seeded_app):
    """A child already lives in a collection, so the gesture that would put a
    file beside it is the move its FOLDER row already offers."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Children Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    for child in re.findall(r"<tr[^>]*lib-row--child.*?</tr>", text, re.S):
        assert "data-pair-target" not in child


def test_a_file_shared_with_you_is_not_a_pair_target(seeded_app):
    """Pairing MOVES both files, which consumes the single-file collection each
    one sits in. Doing that to a file someone shared with you would delete their
    collection out from under them, so their row offers no pair."""
    import re

    from app.resource_types import ResourceType
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    admin, analyst = seeded_app["admin_token"], seeded_app["analyst_token"]
    theirs = _collection(seeded_app, "Their Loose File", admin)
    _upload(seeded_app, theirs["id"], "theirs.md", admin)

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("pair-share-grp") or groups.create(name="pair-share-grp", description="t", created_by="t")
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="t")
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"],
        resource_type=ResourceType.COLLECTION.value,
        resource_id=theirs["id"],
        assigned_by="admin",
    )

    text = seeded_app["client"].get("/library", headers=_auth(analyst)).text
    row = re.search(r'<tr[^>]*data-item-id="' + theirs["id"] + r'".*?</tr>', text, re.S).group(0)
    assert 'data-ownership="shared_with_me"' in row
    assert "data-pair-target" not in row
    # It can still be moved into a collection of the analyst's own — that is the
    # existing gesture and this changes nothing about it.
    assert 'draggable="true"' in row


def test_the_page_carries_the_new_collection_dialog(seeded_app):
    """The drop opens a proposal rather than committing: creating the collection
    moves both files out of where they are now, which is more than a drag should
    do on its own. The same dialog is the keyboard route, from the drag grip's
    menu — so it lists its files rather than naming two in a sentence."""
    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Dialog File", tok)
    _upload(seeded_app, solo["id"], "d.md", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert 'id="pairModal"' in text
    assert 'id="pairFiles"' in text
    assert ">Create collection<" in text
    assert "New collection…" in text  # the grip menu's item
    # It says what will happen to the files it is about to move.
    assert "nothing is copied and nothing is deleted" in text


def test_grid_cards_mirror_the_pair_hooks_and_name_files_off_the_row(seeded_app):
    """Grid cards are projected from the rows by `buildCard`, so anything the
    gesture reads has to travel with them — and anything it reads that ONLY a row
    carries has to be resolved through the row, or the gesture works in the table
    and fails silently one view switch later.

    Both halves are asserted here because both were real: the pair hook has to be
    mirrored onto the card, and ownership + filename have to be read back off the
    row (a card has no `data-filename`, so naming a file off the card gave the
    dialog one filename and one display title)."""
    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Grid Pair File", tok)
    _upload(seeded_app, solo["id"], "grid.md", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    # The card projection carries the hook and the hint.
    assert "if (row.dataset.pairTarget) card.dataset.pairTarget = '1';" in text
    assert "'New collection'" in text
    # …and the per-item facts are resolved through the row, in both views.
    assert "function rowForFile(" in text
    assert "dragOwned = isOwnedByCaller(rowForFile(dragFileId) || src);" in text
    assert "pairLabelOf(rowForFile(targetFileId) || target)" in text


def test_pairing_two_loose_files_leaves_one_collection_and_no_husks(seeded_app):
    """The server contract behind the gesture: create, then one move per file.
    There is no pairing endpoint and none is needed — `move` already tidies away
    each single-file collection it empties, which is exactly what has to happen
    to the two files' current homes."""
    tok = seeded_app["admin_token"]
    a = _collection(seeded_app, "Pair Left", tok)
    _upload(seeded_app, a["id"], "left.md", tok)
    b = _collection(seeded_app, "Pair Right", tok)
    _upload(seeded_app, b["id"], "right.md", tok)
    fa = _files(seeded_app, a["id"], tok)[0]["file_id"]
    fb = _files(seeded_app, b["id"], tok)[0]["file_id"]

    c = seeded_app["client"]
    new = _collection(seeded_app, "Paired Up", tok)
    for src, fid in ((a["id"], fa), (b["id"], fb)):
        r = c.post(
            f"/api/collections/{src}/files/{fid}/move",
            json={"target_collection_id": new["id"]},
            headers=_auth(tok),
        )
        assert r.status_code == 200, r.text
        assert r.json()["source_emptied"] is True

    assert len(_files(seeded_app, new["id"], tok)) == 2
    text = c.get("/library", headers=_auth(tok)).text
    assert "Paired Up" in text
    # Both husks are gone — the two files are now children of the new collection.
    assert "Pair Left" not in text
    assert "Pair Right" not in text
