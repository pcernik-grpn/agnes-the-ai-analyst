"""The builder that authored a skill can reopen it.

Editing used to live somewhere else and could not touch the one thing the
builder produced. `/marketplace/flea/{id}/edit` edits the listing — title,
tagline, category, cover photo — and takes a replacement `.zip`; the document
itself was unreachable from every surface. So an author who wanted to change
one sentence of their own skill had to rebuild a bundle by hand, in a form
that shares no vocabulary, layout or assistant with the page that wrote it.

These guards pin the two halves of the fix: the API can hand a document back
and take a new one, and the page opens in an edit mode that cannot quietly
turn into a second publish.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "app" / "web" / "templates" / "skills.html"
DETAIL = ROOT / "app" / "web" / "templates" / "macros" / "_detail.html"


@pytest.fixture(scope="module")
def page() -> str:
    return SKILLS.read_text(encoding="utf-8")


def test_the_edit_endpoints_are_the_publish_pipeline(page):
    """Both halves delegate rather than reimplement: the read returns what is
    on disk, and the write goes through `update_entity`, so versioning,
    guardrails, review and the block-while-pending rule apply by construction
    instead of by being copied."""
    src = (ROOT / "app" / "api" / "store.py").read_text(encoding="utf-8")
    assert '@router.get("/entities/{entity_id}/markdown"' in src
    assert '@router.put("/entities/{entity_id}/from-markdown"' in src
    body = re.search(r"async def update_entity_from_markdown\(.*?\n\n\n", src, re.S)
    assert body, "update_entity_from_markdown moved — re-point this guard"
    assert "await update_entity(" in body.group(0), (
        "the edit path writes files itself instead of delegating, so guardrails and "
        "versioning are now two implementations that can disagree"
    )


def test_reading_a_document_back_is_owner_only(page):
    src = (ROOT / "app" / "api" / "store.py").read_text(encoding="utf-8")
    body = re.search(r"async def get_entity_markdown\(.*?\n\n\n", src, re.S).group(0)
    assert 'is_user_admin(user["id"], conn)' in body and "entity_not_found" in body, (
        "the editing read is not owner-gated, or it leaks the row's existence to a stranger"
    )


def test_an_edit_is_never_parked_as_a_draft(page):
    """`drafts[type]` is the author's unfinished NEW work. Writing an opened
    published item into that slot would overwrite it and then offer to
    "resume" something that is already saved."""
    persist = re.search(r"function persist\(\) \{(.*?)\n  \}", page, re.S)
    assert persist, "persist() moved — re-point this guard"
    assert "if (editing) return" in persist.group(1), "an edit overwrites the author's draft slot"


def test_edit_mode_saves_through_the_edit_endpoint(page):
    """The failure this prevents is the worst one available here: an edit that
    silently POSTs and mints a second copy under a new name."""
    save = re.search(r"function doSave\(\) \{(.*?)\n  \}", page, re.S)
    assert save, "doSave moved — re-point this guard"
    body = save.group(1)
    assert "if (editing) {" in body
    assert "/from-markdown'" in body and "putJson(" in body
    assert body.index("if (editing) {") < body.index("postJson('/api/store/entities/from-markdown'"), (
        "the create path is reached before the edit branch, so editing publishes a duplicate"
    )


def test_edit_mode_does_not_offer_a_type_switch(page):
    """Type is locked server-side (`type_locked`), so a menu offering three of
    them offers something that cannot happen."""
    menu = re.search(r"function titleMenuHtml\(c\) \{(.*?)\n  \}", page, re.S)
    assert menu and "if (editing)" in menu.group(1), "the type switch is still live while editing"


def test_a_failure_before_a_type_exists_is_still_shown(page):
    """The alerts host used to render only in the built page, so a bad
    `?edit=` id set its reason and then drew a bare type picker — which reads
    as "your click did nothing"."""
    render = re.search(r"function render\(\) \{(.*?)\n    var c = cfg\(\)", page, re.S)
    assert render, "render() moved — re-point this guard"
    empty = re.search(r"if \(!type\) \{(.*?)\n    \}", render.group(1), re.S)
    assert empty and "alertsHtml()" in empty.group(1), (
        "a load failure on the type-picker view is silent again"
    )


def test_the_detail_page_sends_a_document_to_the_builder():
    src = DETAIL.read_text(encoding="utf-8")
    assert "'/skills?edit=' ~ entity.id" in src, "Edit no longer opens the builder for authored items"
    assert "entity.type in ('skill', 'agent')" in src, (
        "the builder is being offered for bundles too, which have no single document behind them"
    )
    assert "'Edit listing'" in src, (
        "the listing fields (photo, tagline, video) lost their entry point"
    )


# ── The other three builders ────────────────────────────────────────────────
#
# Same rule, different surfaces: whatever a builder creates, that builder
# edits. /agents already worked this way — the page IS the editor, every field
# change writes back — which is why it appears nowhere below.

MCP_JS = ROOT / "app" / "web" / "static" / "js" / "components" / "mcp_builder.js"
PKG_JS = ROOT / "app" / "web" / "static" / "js" / "components" / "package_drawer.js"
ROUTER = ROOT / "app" / "web" / "router.py"


def test_both_edit_pages_refuse_an_id_that_is_not_a_row():
    """An edit page pointed at nothing renders an empty builder — and an empty
    builder CREATES on save. Both routes check before rendering."""
    src = ROUTER.read_text(encoding="utf-8")
    for fn, missing in (
        ("admin_mcp_builder_edit", "mcp_source_not_found"),
        ("admin_package_builder_edit", "data_package_not_found"),
    ):
        body = re.search(rf"async def {fn}\(.*?\n\n\n", src, re.S)
        assert body, f"{fn} moved — re-point this guard"
        assert missing in body.group(0), f"{fn} renders a builder for an id that does not exist"


def test_the_mcp_edit_saves_a_difference_not_a_repost():
    """The create path posts everything. Applied to an existing source that
    would duplicate its tools and could never remove one, so the tool toggle
    and the group picker would be decoration."""
    src = MCP_JS.read_text(encoding="utf-8")
    body = re.search(r"function saveEdit\(\) \{(.*?)\n  \}", src, re.S)
    assert body, "saveEdit moved — re-point this guard"
    b = body.group(1)
    assert "method: 'DELETE'" in b and "TOOLS_API" in b, "a tool turned off is never deleted"
    assert "/grants/'" in b, "a group unpicked is never revoked"
    save = re.search(r"function save\(\) \{(.*?)\n  \}", src, re.S).group(1)
    assert "if (editing) return saveEdit();" in save, "editing falls through to the create path"


def test_neither_builder_greets_an_edit_with_its_create_script():
    """"Tell me what you are connecting" and a set of fresh-start chips, on
    something that exists, reads as though nothing had been done — and one
    chip click lands on top of real work."""
    mcp = MCP_JS.read_text(encoding="utf-8")
    assert "This source is registered." in mcp, "the MCP builder still opens an edit with the create script"
    pkg = PKG_JS.read_text(encoding="utf-8")
    assert "OPENING_EDIT" in pkg, "the package builder still opens an edit with the create script"
    chips = re.search(r"chips: \(convBusy \|\| llmUnavailable\) \? \[\](.*?),\n", pkg, re.S)
    assert chips and "'edit'" in chips.group(1), "starter chips are offered on an edit"


def test_the_package_edit_is_a_page_like_its_create():
    """Authoring was a workspace and revising was an overlay on whatever page
    you were on — one component at two sizes, which made the edit read as the
    lesser thing. The drawer keeps the case it was built for."""
    detail = (ROOT / "app" / "web" / "templates" / "admin_package_detail.html").read_text(encoding="utf-8")
    assert "/edit`" in detail and "AgnesPackageDrawer.open({" not in detail.split("apd-edit-details")[1][:400], (
        "Edit details still opens the overlay instead of the builder page"
    )
    tables = (ROOT / "app" / "web" / "templates" / "admin_tables.html").read_text(encoding="utf-8")
    assert "AgnesPackageDrawer.open({" in tables, (
        "the in-place drawer was removed from the one surface that needs it — "
        "assigning a table to a package that does not exist yet"
    )


def test_an_edit_does_not_announce_a_restored_draft(page):
    """`openEdit` routes through `chooseType` to reset the panes, then
    overwrites every field from the saved item — so the create path's
    "Restored your saved skill draft." toast fired on a page showing something
    else entirely."""
    body = re.search(r"function chooseType\(k\) \{(.*?)\n  \}", page, re.S)
    assert body, "chooseType moved — re-point this guard"
    tail = body.group(1)
    assert "if (editing) {" in tail and tail.index("if (editing) {") < tail.index("Restored your saved"), (
        "an edit announces a restored draft again"
    )
