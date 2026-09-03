"""Web UI routes for Collections — /library and /library/{slug}."""

from __future__ import annotations

from pathlib import Path
import pytest

import io

# Glyph path fragments (mirror macros/_catalog_card.html kind_glyph).
_DOC_GLYPH = "M7 4h7l4 4v12H7z"  # single document — a one-file artifact
_LIB_GLYPH = "M9 7h6l4 4v9"  # two overlapping sheets — a collection (detail hero)
# In the Library's Files TABLE a collection wears a folder glyph instead: there
# it sits beside loose files and takes drops, so it reads as the container it is.
_FOLDER_GLYPH = "M4 7.5A1.5 1.5 0 0 1 5.5 6"


@pytest.fixture(autouse=True)
def _rail_layout(monkeypatch):
    """This file exercises the RAIL redesign's unified /library. Topnav keeps
    the legacy collections page (the /catalog pattern) — guarded by
    tests/test_ui_layout_theme.py::TestDefaultContentParity."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(seeded_app, name: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 201, r.text
    return r.json()


def _upload(seeded_app, cid: str, filename: str, content: bytes, ctype: str):
    return seeded_app["client"].post(
        f"/api/collections/{cid}/files",
        files={"files": (filename, io.BytesIO(content), ctype)},
        headers=_auth(seeded_app["admin_token"]),
    )


def test_library_page_renders_with_collections(seeded_app):
    c = seeded_app["client"]
    _create(seeded_app, "LibraryUI Demo")
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    # /library is now the unified Library surface (the renamed /artefacts), not
    # the old "Your collections" list page it replaced.
    assert "Search library" in r.text
    assert "LibraryUI Demo" in r.text


def test_library_has_no_agent_affordance(seeded_app):
    """Agents are NOT a Library kind — they have their own home at /agents, so
    the Library header offers no "Build an agent" entry and lists no agent rows.
    (Supersedes an earlier deep-link-into-the-builder assertion.)

    An agent TEMPLATE is a Library kind (store ``type='agent'``, renamed in
    AGT-4) and its menu entry reads "Build an agent template" — which shares a
    prefix with the thing this guards against, so the match is on the closing
    tag rather than the prefix.
    """
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert ">Build an agent<" not in r.text
    assert ">Build an agent template<" in r.text
    assert 'data-kind="agent"' not in r.text


def _seed_definitions(metrics: int = 1, terms: int = 1) -> None:
    """Put a metric and/or a glossary term into the semantic layer.

    The Definitions strip only renders when at least one side is populated, so
    a test that wants it has to say so — which is the point of the guard.
    """
    from src.repositories import glossary_repo, metric_repo

    for i in range(metrics):
        metric_repo().create(
            id=f"finance/m{i}",
            name=f"m{i}",
            display_name=f"Metric {i}",
            category="finance",
            sql="SELECT 1",
            description="A canonical definition.",
        )
    for i in range(terms):
        glossary_repo().create(id=f"g{i}", term=f"Term {i}", definition="What it means here.")


def test_library_shows_definitions_as_a_strip_above_the_inventory(seeded_app):
    """The whole semantic layer is a DESTINATION, not inventory.

    Two objects, and both fail the table for the same reason at different
    depths. A metric or a glossary term is the one thing on this page nobody
    owns, shares, installs, drops or edits, so as a row it would blank all
    four of the table's columns at once. A stored MODEL answers those columns
    honestly — which is why it was a row for a while — but it is still not a
    PEER of the rows beside it: a Data Package is data you can reach, a model
    is a statement about that data and is worthless without one. The toolbar
    settles it. Filter, sort and "Agents use it" are inventory affordances,
    and a row none of the three can act on is the table saying the object is
    not one of its rows.

    So the layer leaves the table entirely and becomes one strip above it,
    with `/semantic-layer` holding models, metrics and glossary as three tabs
    at their own top level. Third placement and, unlike the footer aside and
    the section band before it, the first that does not have to special-case
    the object to keep it.
    """
    _seed_definitions(metrics=2, terms=3)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'id="lib-defs"' in r.text
    # The counts are stated IN PLACE, as prose rather than as links: a reader
    # who never clicks still learns the vocabulary exists and how much of it
    # is defined, which is the only reason this is on the page at all.
    assert "2 metrics" in r.text
    assert "3 glossary terms" in r.text
    # ONE call to action. Two competing links beside a sentence is the band
    # this replaced, and the strip stops being an entrance the moment it
    # becomes a second inventory.
    assert r.text.count('class="lib-defs__cta"') == 1
    # Names what it opens — "the semantic layer" is the concept, the models
    # are the thing on the other side of the link.
    assert "Browse definitions" in r.text
    assert "/catalog/semantics" not in r.text
    # The layer is off the table: no Semantic models section, no rows, no
    # facet — the models are listed by /semantic-layer's own Models tab.
    assert 'data-lib-sec="semantic_model"' not in r.text
    assert 'data-lib-sec="definitions"' not in r.text
    assert 'data-kind="definitions"' not in r.text
    # And not the retired header link either.
    assert "lib-browse-semantics" not in r.text


def test_library_definitions_link_lands_on_a_tab_the_caller_can_use(seeded_app):
    """The one link's TARGET carries what the two links used to say.

    `/semantic-layer` opens on Models, which is the right front door only for
    a caller who can read a document. A caller with visible metrics and no
    readable model would land on an empty tab and conclude the definitions
    they were just told about are not there — so they are sent straight to
    the projection that IS theirs. One link, one label, different landing.
    """
    _seed_definitions(metrics=2, terms=3)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    # No semantic model seeded here, so the metrics tab is the honest landing.
    assert 'href="/semantic-layer?tab=all_metrics"' in r.text


def test_library_search_does_not_answer_from_the_definitions(seeded_app):
    """The strip is a SIGN, not a search result — one job per surface.

    It used to answer the page's search box: it shipped an index of every
    metric name, synonym and glossary term in a `data-defs-search` attribute
    and said "'mrr' is one of your organization's definitions" when a query
    matched. That was built for the old band placement, where the definitions
    sat halfway down the page and a reader who searched and found nothing had
    no way to learn they existed.

    Two things retired it. The strip now stands permanently above the empty
    state with its counts and its link, so it answers that reader by being on
    screen rather than by matching their query — the hint was paying twice for
    one job. And it could not be positioned honestly: a block that responds to
    the search box belongs in the results region, and a block in the results
    region that ignores Filter and sort reads as broken.

    So the Library's search searches the Library, and `/semantic-layer`
    searches its own content (a filter box on the metrics tab, a search box on
    the glossary tab). This guards the split, and the payload win that came
    with it — the index shipped in an attribute on every single page load.
    """
    from src.repositories import glossary_repo, metric_repo

    metric_repo().create(
        id="finance/mrr",
        name="mrr",
        display_name="Monthly Recurring Revenue",
        category="finance",
        sql="SELECT 1",
        description="Normalized monthly subscription revenue.",
        synonyms=["ARR"],
    )
    glossary_repo().create(id="g_active", term="Active account", definition="At least one paid seat.")

    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'id="lib-defs"' in r.text, "the strip itself stays — only its search coupling went"
    assert "data-defs-search" not in r.text
    assert 'id="lib-defs-hit"' not in r.text
    # Nothing about a metric's vocabulary rides the page any more.
    assert "Monthly Recurring Revenue" not in r.text

    src = (Path("app/web/templates/library.html")).read_text(encoding="utf-8")
    assert "syncDefinitionsHit" not in src, (
        "the definitions hint is back in the onApply chain — the strip is a sign, "
        "not a search result; /semantic-layer searches its own content"
    )


def test_the_semantic_layer_page_searches_its_own_content():
    """The other half of the split above: dropping the Library's hint is only
    safe because the destination is self-sufficient. If these boxes go, the
    definitions become searchable nowhere and the hint has to come back."""
    src = (Path("app/web/templates/semantic_layer_list.html")).read_text(encoding="utf-8")
    # Stated as the CLAIM, not as one generation's ids: the Library drops its
    # own search hint only because the destination searches its own content, so
    # what has to hold is that a search box exists there at all. Pinning
    # `id="sl-search"` would make this fail on a branch that has the Library
    # half of the redesign without the Definitions half, which is exactly how
    # these two ship — stacked, and reviewable one at a time.
    assert 'type="search"' in src, "the destination must search its own content"


def test_library_definitions_counts_are_singular_for_one(seeded_app):
    """ "1 metric", not "1 metrics" — the counts ARE the sentence now.

    They stopped being two link labels and became prose inside the strip's
    description, so they have to read as prose: asserted on the counts span
    itself rather than by scanning the whole page for a substring.
    """
    _seed_definitions(metrics=1, terms=1)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    counts = r.text.split('class="lib-defs__counts"', 1)[1].split("</span>", 1)[0]
    assert "1 metric," in counts
    # No full stop: the counts are a line of page metadata now, not prose.
    assert "1 glossary term" in counts
    assert "1 metrics" not in counts
    assert "1 glossary terms" not in counts


def test_library_hides_definitions_when_semantic_layer_is_empty(seeded_app):
    """No metrics AND no glossary -> no block.

    A block advertising "0 metrics · 0 glossary terms" describes the instance's
    setup, not its content, and reads as a broken feature rather than an
    unconfigured one. One populated side is enough to render it.
    """
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'id="lib-defs"' not in r.text
    assert "/catalog/semantics" not in r.text


def test_library_shows_definitions_when_only_one_side_is_populated(seeded_app):
    """Metrics but no glossary still renders it — "0 glossary terms" is a true
    and useful statement about a semantic layer that exists."""
    _seed_definitions(metrics=1, terms=0)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'id="lib-defs"' in r.text
    assert "0 glossary terms" in r.text


def test_library_definitions_are_labeled_in_plain_words(seeded_app):
    """ "Definitions", never "Semantic layer" — that name belongs to
    /admin/semantic-layer, where the reader operates the sync rather than
    looking a term up. Scoped to the block because an admin's nav dropdown
    carries the admin link on every page, and that one is correct."""
    _seed_definitions()
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    block = r.text.split('id="lib-defs"', 1)[1].split("</div>", 1)[0]
    assert "Definitions" in block
    # "Semantic layer" is allowed on the CTA (it names the destination page),
    # but never as the block's own label — that name belongs to
    # /admin/semantic-layer, where the reader operates the sync.
    assert "Semantic layer" not in block


def test_library_definitions_are_not_at_the_tail_of_an_unbounded_list(seeded_app):
    """#1898 item 1 / #1707 N3 — the block is reachable on ARRIVAL, at any length.

    It used to close the page, under the item list, on the (correct) reasoning
    that the organization's vocabulary is not one of the list's rows. The cost
    was discoverability: past a handful of items it was below the fold on every
    visit, so the surface positioned as the curated single source of truth was
    the one thing nobody could find.

    Two fixes since, and this asserts the property both were after rather
    than either mechanism: first a Semantic models section at a fixed slot in
    `_SECTION_ORDER`, now a strip above the toolbar with the whole layer off
    the table. Either way the definitions precede the unbounded artifact
    sections rather than trailing them. Position is asserted rather than mere
    presence, because presence is exactly what the old bug had.
    """
    _seed_definitions()
    for i in range(6):
        _create(seeded_app, f"Filler {i}")
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    body = r.text
    defs_at = body.index('id="lib-defs"')
    # The sections that grow without bound are the ones it must not sit behind:
    # a reader who has to scroll past every file, app and memory domain to find
    # the vocabulary is back to the footer this replaced.
    for later in ("files", "data_app", "memory_domain"):
        marker = f'data-lib-sec="{later}"'
        if marker in body:
            assert defs_at < body.index(marker), (
                f"Definitions must precede the unbounded {later} section, "
                "or it is a footer again under a different name"
            )
    # Above every BAND, including the first one — which is the property that
    # matters and the one the footer version failed. It now follows the count
    # and the controls rather than preceding them: it opens the list instead of
    # standing over the page, because it is one of the things the reader came
    # for, not a sign about them.
    assert defs_at < body.index('<div class="lib-list">')


def test_library_filter_is_disabled_when_there_is_nothing_to_filter(seeded_app):
    """#1898 item 4 — an empty popover is worse than no popover.

    Every facet is conditional (a category with fewer than two values does not
    render), so on a fresh instance the menu came out holding nothing but its own
    Clear and Done buttons. "No dead filters" has to cover the Filter button
    itself: with no facets it is visibly unavailable, says why, and the menu is
    not built at all rather than built empty.
    """
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    btn = r.text.split('id="lib-filter-btn"', 1)[1].split(">", 1)[0]
    assert "disabled" in btn
    assert 'aria-disabled="true"' in btn
    assert "title=" in btn, "an unavailable control has to say why"
    # No popover, so there is no empty one to open.
    assert 'id="lib-filter-menu"' not in r.text
    assert "data-fbar-done" not in r.text


def test_library_filter_is_live_once_a_facet_exists(seeded_app):
    """The mirror image, so the disable can never become permanent: two owners is
    two values of one category, which is a real filter, so the control opens a
    menu with that category in it."""
    cid = _create(seeded_app, "Two formats")["id"]
    assert _upload(seeded_app, cid, "a.csv", b"x,y\n1,2\n", "text/csv").status_code in (200, 201)
    assert _upload(seeded_app, cid, "b.md", b"# note\n", "text/markdown").status_code in (200, 201)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    btn = r.text.split('id="lib-filter-btn"', 1)[1].split(">", 1)[0]
    assert "disabled" not in btn
    assert 'aria-haspopup="true"' in btn
    assert 'id="lib-filter-menu"' in r.text
    assert "data-fbar-done" in r.text
    # …and the menu is not the empty shell the disabled case exists to prevent.
    menu = r.text.split('id="lib-filter-menu"', 1)[1].split("</div>", 1)[0]
    assert "fbar-cat" in r.text.split('id="lib-filter-menu"', 1)[1][:4000] or "fbar-menu__opt" in menu


def test_library_detail_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    col = _create(seeded_app, "DetailUI Demo")
    r = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "DetailUI Demo" in r.text
    assert "Add files" in r.text
    # A detail page presents its entity — no in-page ask/search box (the hero's
    # Ask Agnes action carries asking into chat instead).
    assert "Ask this collection" not in r.text
    assert 'id="lib-q"' not in r.text


def test_library_detail_404_for_missing(seeded_app):
    r = seeded_app["client"].get("/library/does-not-exist", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404


def test_library_detail_404_for_non_member(seeded_app):
    c = seeded_app["client"]
    col = _create(seeded_app, "Private UI")
    # analyst1 has no grant — returns 404 (not 403) so existence isn't leaked.
    r = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 404


def test_library_lists_only_accessible(seeded_app):
    c = seeded_app["client"]
    _create(seeded_app, "Hidden From Analyst")
    r = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 200
    # analyst1 has no grants → no rows for it, but the page still renders
    assert "Hidden From Analyst" not in r.text


def test_single_file_artefact_presents_as_file(seeded_app, monkeypatch):
    """One file in an artifact reads AS the file — single-document glyph,
    filename + size in the meta, "File" framing, never "a collection with 1
    file" — but the title is the artifact's NAME (what the caller typed), not
    the filename, so distinct names stay distinct (they previously all
    rendered as the same filename)."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    col = _create(seeded_app, "Solo Upload")
    _upload(seeded_app, col["id"], "report.pdf", b"%PDF-1.4 x", "application/pdf")

    lst = c.get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "Solo Upload" in lst  # the typed name is the title
    assert "report.pdf" in lst  # filename is surfaced in the meta
    assert _DOC_GLYPH in lst  # single-document glyph
    assert "1 file" not in lst  # never the "1 file" container framing

    det = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["admin_token"])).text
    assert "Solo Upload" in det  # the typed name is the hero title
    assert "report.pdf" in det  # filename surfaced in the hero meta
    assert _DOC_GLYPH in det  # single-document hero glyph
    assert "Ask this file" not in det  # detail pages carry no in-page ask box


def test_upload_box_follows_the_file_list_and_explains_promotion(seeded_app):
    """The page leads with what is IN the artifact; adding to it comes after
    (Files section above the Add-files drop zone). On a one-file artifact the
    drop zone also says what uploading does — it turns the file into a
    collection — so the change of shape never surprises. A real collection
    needs no such warning."""
    c = seeded_app["client"]
    col = _create(seeded_app, "Order Demo")
    _upload(seeded_app, col["id"], "solo.pdf", b"%PDF-1.4 x", "application/pdf")

    det = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["admin_token"])).text
    assert det.index("Files</h2>") < det.index("Add files</h2>")
    assert "turns this file into a" in det

    _upload(seeded_app, col["id"], "second.txt", b"more", "text/plain")
    det = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["admin_token"])).text
    assert "turns this file into a" not in det


def test_multi_file_artefact_presents_as_collection(seeded_app, monkeypatch):
    """A second file promotes the artifact to a Collection: the list shows
    ``N files`` + a folder glyph (it's a container among loose files there), and
    the detail page reads as a collection again — under the two-sheet hero."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    col = _create(seeded_app, "Grouped Upload")
    _upload(seeded_app, col["id"], "a.txt", b"aaa", "text/plain")
    _upload(seeded_app, col["id"], "b.txt", b"bbb", "text/plain")

    lst = c.get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "2 files" in lst
    assert _FOLDER_GLYPH in lst  # folder glyph — the row is a drop target

    det = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["admin_token"])).text
    assert "collection" in det  # collection framing in the hero copy
    assert _LIB_GLYPH in det  # two-sheet hero glyph is unchanged


def test_single_file_artefacts_with_same_filename_keep_distinct_names(seeded_app, monkeypatch):
    """Regression: two single-file artifacts holding the *same* filename but
    given different names must render under their own names on the Artifacts
    list — the title is the name, not the filename, so they don't collapse
    into two identical rows."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    a = _create(seeded_app, "First Report")
    b = _create(seeded_app, "Second Report")
    _upload(seeded_app, a["id"], "logo.png", b"\x89PNG\r\n\x1a\n x", "image/png")
    _upload(seeded_app, b["id"], "logo.png", b"\x89PNG\r\n\x1a\n x", "image/png")

    lst = c.get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "First Report" in lst
    assert "Second Report" in lst


# ── Stack pill: what a row says about how it got into the Stack ────────────
#
# Membership is ONE state — "In stack" — and the rows differ only in whether
# the caller may change it:
#
#   Required grant   → "In stack" + LOCK. Not the caller's to remove; the
#                      unsubscribe API answers 400 cannot_remove_required.
#                      The word "Required" is NOT the label: the tier is an
#                      attribute of the membership, filterable through the
#                      separate Optional/Required facet.
#   Available grant  → "In stack", plain checkmark. Auto-membership
#                      (StackResolver's browse() sets in_stack
#                      unconditionally) — no "add" to offer, the grant did it.
#   Own artifact     → "In stack" ⇄ "Add to stack", a real toggle. A personal
#                      upload has no admin grant tier, so the subscription row
#                      IS the membership.


def _grant_package(conn, *, slug: str, name: str, user_id: str, requirement: str) -> str:
    """Seed a data package granted to the caller's group at ``requirement``."""
    import uuid

    from src.repositories.data_packages import DataPackagesRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    pkg_id = DataPackagesRepository(conn).create(
        name=name,
        slug=slug,
        description="d",
        icon=None,
        color=None,
        created_by="test",
    )
    gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]
    UserGroupMembersRepository(conn).add_member(user_id, gid, source="test")
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) VALUES (?, ?, 'data_package', ?, ?, CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), gid, pkg_id, requirement],
    )
    return pkg_id


def _row_for(body: str, title: str) -> str:
    """The single <tr> carrying ``title`` — so a pill assertion can't be
    satisfied by some other row elsewhere on the page."""
    start = body.rindex("<tr", 0, body.index(title))
    return body[start : body.index("</tr>", start)]


#: The locked-membership tooltips, verbatim. A test that paraphrases them would
#: let the shipped copy drift from the spec, so the exact sentences are asserted.
#: Both tiers are locked, and the ROW now says so identically — one pill,
#: "Agents can query this" — because the caller can do exactly the same thing
#: with either tier: query it, and not remove it. The old pair of pills promised
#: two different things about removal ("cannot be removed" vs "only an admin can
#: remove it") for one state, which is what made a single state read as two. The
#: tier survives here, in the tooltip, where it explains WHY rather than
#: pretending to be a different capability.
LOCKED_TOOLTIP = "Required by your admin — your agents get this automatically, and you cannot remove it."
GRANTED_TOOLTIP = (
    "Granted to your group by your admin — your agents can already use it, and only an admin can change that."
)


def test_library_required_grant_is_locked_in_stack(seeded_app):
    """A required grant reads the SAME "In stack" as any other member — it is
    one — and is marked by a lock plus the locked tooltip. The tier is an
    attribute of the membership, not a different state, so the word "Required"
    is NOT the pill's label (the separate Optional/Required facet filters it)."""
    from src.db import get_system_db

    conn = get_system_db()
    _grant_package(conn, slug="req-pkg", name="Mandated Package", user_id="analyst1", requirement="required")
    conn.close()

    body = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row_for(body, "Mandated Package")
    # The pill states the OUTCOME, not the tier and not the mechanism. A granted
    # package is queryable the moment it is granted (auto-membership), so "In
    # stack" described a membership the caller could neither create nor drop —
    # and naming the tier instead just moved the problem, since the two tiers
    # are one capability. What the reader needs is what their agents can do.
    assert "Agents can query this" in row
    assert "lib-instack--locked" in row  # locked → lock glyph + info tint
    assert LOCKED_TOOLTIP in row
    # Not a button, and not addable — nothing to click either way.
    assert "data-remove-from-stack" not in row
    assert "data-add-to-stack" not in row
    # It still FILTERS as in-stack: a locked membership IS a membership, so the
    # "In stack only" toggle keeps it (the tier is filterable on its own
    # Optional/Required category).
    assert 'data-stack="in_stack"' in row


def test_library_available_grant_reads_in_stack_and_offers_no_toggle(seeded_app, monkeypatch):
    """AUTO-membership (opt-in): an available grant is already in the stack,
    so the row reports that plainly — no "Add to stack" for something already
    in it, and no remove (the grant is an admin's to change). The classic
    default renders the honest not-a-member state (sibling below).

    It is LOCKED too, for the same reason the required one is: the grant IS the
    membership, so there is nothing on the row to drop. The lock is keyed on
    droppability, NOT on the tier — keyed on the tier, this row wore the
    success-tinted check that a *removable* pill shows at rest, and the only
    way to find out it wasn't one was to hover it and watch nothing happen.
    The tier lives in the tooltip and in the Optional/Required facet."""
    from src.db import get_system_db

    monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")
    conn = get_system_db()
    _grant_package(conn, slug="avail-pkg", name="Offered Package", user_id="analyst1", requirement="available")
    conn.close()

    body = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row_for(body, "Offered Package")
    # Same pill as the required tier — deliberately. See LOCKED_TOOLTIP above.
    assert "Agents can query this" in row
    assert "lib-instack--fixed" in row
    assert "lib-instack--locked" in row  # not the removable pill's rest state
    assert "data-add-to-stack" not in row
    assert "data-remove-from-stack" not in row
    # …and it says who CAN remove it, rather than only what it is.
    assert GRANTED_TOOLTIP in row
    # The tier is still distinguishable — just not by the affordance.
    assert LOCKED_TOOLTIP not in row


def test_library_available_grant_classic_is_not_claimed_in_stack(seeded_app, monkeypatch):
    """CLASSIC: a granted-but-unsubscribed ``available`` package is NOT a
    stack member — membership is required ∪ subscribed, and it also drives
    query authorization — so the row must not claim "In stack", must not
    land in the "In stack only" filter bucket, and points the caller at the
    Catalog to add it (Devin Review on #1199). A subscribed one renders as a
    member again.

    Classic is no longer the presetless default (Wave 0, 2026-08, coupled the
    sole remaining `redesign` experience to auto-membership) — forced here
    via the still-fully-supported explicit per-knob override, which wins over
    any preset."""
    from src.db import get_system_db

    monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "0")
    conn = get_system_db()
    pkg_id = _grant_package(
        conn, slug="classic-avail-pkg", name="Classic Offered Package", user_id="analyst1", requirement="available"
    )
    conn.close()

    body = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row_for(body, "Classic Offered Package")
    assert 'data-stack="available"' in row, "unsubscribed available must filter as addable, not in-stack"
    assert 'data-stack-badge="' not in row or "In stack" not in row.split("data-add-to-stack")[0], (
        "a non-member must not wear the member pill"
    )
    # A real Add control wired to the generic subscribe endpoint (JSON body
    # in data-stack-body), carrying the remove direction too — the post-add
    # state is the REMOVABLE member (a self-subscription is the caller's to
    # drop; the old locked-after contract claimed an admin mandate the
    # caller had just created).
    assert 'data-add-to-stack="' in row
    assert 'data-stack-endpoint="/api/stack/subscribe"' in row
    assert "data-stack-body=" in row and "data_package" in row
    assert 'data-stack-remove-endpoint="/api/stack/subscription/data_package/' in row

    # Subscribing joins the stack — the row becomes a member.
    conn = get_system_db()
    conn.execute(
        "INSERT INTO user_stack_subscriptions(user_id, resource_type, resource_id, subscribed_at) "
        "VALUES ('analyst1', 'data_package', ?, CURRENT_TIMESTAMP)",
        [pkg_id],
    )
    conn.close()
    body = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row_for(body, "Classic Offered Package")
    assert 'data-stack="in_stack"' in row
    # Classic (opt-in) mode: the caller subscribed, and under classic that is
    # what makes the package queryable — membership drives
    # get_accessible_tables — so the row states the OUTCOME in the same
    # vocabulary every other row uses, and offers the undo. It used to read
    # "Local copy", which described the side effect (`agnes pull` keeps a copy)
    # rather than the thing the caller changed, in a word the page no longer
    # speaks anywhere else.
    assert "Agents can query this" in row
    assert 'data-remove-from-stack="' in row


def test_library_lists_granted_curated_plugins(seeded_app):
    """Regression: a granted curated marketplace plugin must appear in the
    Library's Plugins section.

    The grant's ``resource_id`` is ``"<marketplace_id>/<plugin_name>"`` — the
    key the API is gated on. The Library used to rebuild that path through a
    ``{registry_id: row["slug"]}`` map, but ``marketplace_registry`` has no
    ``slug`` column (its PRIMARY KEY *is* the slug), so the path came out
    ``"None/<plugin>"``, matched no grant, and dropped EVERY curated plugin —
    silently, since a non-matching path raises nothing. `/catalog` still
    showed them (it reads plugin *subscriptions*, not grants), so the two
    surfaces disagreed about what the caller had.
    """
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url) VALUES (?, 'Curated Co', 'https://example.com/r.git')",
        ["curated-lib-test"],
    )
    conn.execute(
        "INSERT INTO marketplace_plugins (marketplace_id, name, description, is_system) "
        "VALUES (?, ?, 'A granted curated plugin', FALSE)",
        ["curated-lib-test", "granted-plugin"],
    )
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("plugin-grant-grp") or groups.create(
        name="plugin-grant-grp", description="t", created_by="t"
    )
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="t")
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"],
        resource_type="marketplace_plugin",
        # Exactly the key the API gates on — NOT a registry slug lookup.
        resource_id="curated-lib-test/granted-plugin",
        assigned_by="admin",
    )
    conn.close()

    body = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "granted-plugin" in body, "granted curated plugin missing from the Library"
    row = _row_for(body, "granted-plugin")
    # Granted at the `available` tier and never subscribed, so the grant is
    # ELIGIBILITY, not membership: the row offers the toggle. (This assertion
    # used to read "In stack" + no toggle — the auto-membership model, which is
    # right for data packages and wrong for plugins. See the dedicated
    # subscription-tracking test below for why.)
    assert "data-add-to-stack" in row
    assert 'data-stack="available"' in row


def _seed_granted_plugin(
    conn,
    *,
    marketplace_id: str,
    name: str,
    user_id: str,
    requirement: str | None = None,
    is_system: bool = False,
) -> None:
    """Seed a curated plugin granted to ``user_id``'s group at ``requirement``."""
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    if not conn.execute("SELECT 1 FROM marketplace_registry WHERE id = ?", [marketplace_id]).fetchone():
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url) VALUES (?, 'Curated Co', 'https://example.com/r.git')",
            [marketplace_id],
        )
    conn.execute(
        "INSERT INTO marketplace_plugins (marketplace_id, name, description, is_system) VALUES (?, ?, 'p', ?)",
        [marketplace_id, name, is_system],
    )
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(f"grp-{name}") or groups.create(name=f"grp-{name}", description="t", created_by="t")
    UserGroupMembersRepository(conn).add_member(user_id, grp["id"], source="admin", added_by="t")
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"],
        resource_type="marketplace_plugin",
        resource_id=f"{marketplace_id}/{name}",
        assigned_by="admin",
        requirement=requirement,
    )


def test_library_plugin_stack_state_tracks_subscription_not_the_grant(seeded_app):
    """A plugin's Library Stack state must follow the SUBSCRIPTION, not the grant.

    Plugins are the one granted kind whose membership is not automatic: Model B
    (v28+) has ``resolve_user_marketplace`` serve ``subscriptions ∪
    required-tier grants``, so an ``available``-tier grant the caller never
    subscribed to is genuinely NOT in their served set — its skills and commands
    are not loaded in their Claude Code.

    The Library used to reuse the auto-membership model that data packages and
    memory domains legitimately have, rendering every eligible plugin as a
    LOCKED "In stack". That was wrong twice over: it contradicted /marketplace
    and the agent's own ``marketplace_search`` (both of which read the
    subscription union), and because the row rendered locked it removed the only
    affordance that could have corrected the state.
    """
    from src.db import get_system_db

    c = seeded_app["client"]
    hdr = _auth(seeded_app["analyst_token"])
    conn = get_system_db()
    _seed_granted_plugin(conn, marketplace_id="sub-mp", name="optional-plugin", user_id="analyst1")
    conn.close()

    endpoint = "/api/marketplace/curated/sub-mp/optional-plugin/install"

    # Eligible, not subscribed → addable, and the row names the endpoint the
    # kind-agnostic toggle POSTs to.
    row = _row_for(c.get("/library", headers=hdr).text, "optional-plugin")
    assert 'data-stack="available"' in row
    assert "data-add-to-stack" in row
    assert endpoint in row
    assert "lib-instack--locked" not in row

    # Subscribed → in stack, and REMOVABLE: the subscription is the caller's own.
    assert c.post(endpoint, headers=hdr).status_code == 200
    row = _row_for(c.get("/library", headers=hdr).text, "optional-plugin")
    assert 'data-stack="in_stack"' in row
    assert "data-remove-from-stack" in row
    assert "lib-instack--locked" not in row
    assert GRANTED_TOOLTIP not in row  # an admin is not who removes this one

    # …and unsubscribing returns it to addable rather than stranding the pill.
    assert c.delete(endpoint, headers=hdr).status_code == 204
    row = _row_for(c.get("/library", headers=hdr).text, "optional-plugin")
    assert "data-add-to-stack" in row


def test_library_required_plugin_grant_is_locked_in_stack(seeded_app):
    """A required-tier plugin grant IS membership (the resolver unions it in) and
    the caller cannot drop it — ``curated_uninstall`` answers 409
    ``cannot_uninstall_required_plugin``. So it locks, exactly like a required
    data package, and the lock promises what the API enforces."""
    from src.db import get_system_db

    c = seeded_app["client"]
    hdr = _auth(seeded_app["analyst_token"])
    conn = get_system_db()
    _seed_granted_plugin(
        conn, marketplace_id="req-mp", name="mandated-plugin", user_id="analyst1", requirement="required"
    )
    conn.close()

    row = _row_for(c.get("/library", headers=hdr).text, "mandated-plugin")
    assert 'data-stack="in_stack"' in row
    assert "lib-instack--locked" in row
    assert LOCKED_TOOLTIP in row
    assert "data-add-to-stack" not in row
    assert "data-remove-from-stack" not in row
    # The affordance is not a lie: the API refuses the drop it doesn't offer.
    assert c.delete("/api/marketplace/curated/req-mp/mandated-plugin/install", headers=hdr).status_code == 409


def test_library_plugin_stack_state_agrees_with_marketplace_items(seeded_app):
    """Cross-surface guard: for the same caller and the same plugin, the
    Library's Stack state and ``GET /api/marketplace/items``' ``installed`` flag
    must agree — they are two renderings of one fact (what
    ``resolve_user_marketplace`` serves), and the bug this pins was precisely
    them disagreeing. Both now derive from ``_curated_stack_sets``.
    """
    from src.db import get_system_db

    c = seeded_app["client"]
    hdr = _auth(seeded_app["analyst_token"])
    conn = get_system_db()
    _seed_granted_plugin(conn, marketplace_id="agree-mp", name="agree-plugin", user_id="analyst1")
    conn.close()

    def _both() -> tuple[bool, bool]:
        items = c.get("/api/marketplace/items", params={"tab": "curated"}, headers=hdr).json()["items"]
        api = next(i["installed"] for i in items if i["id"] == "curated-agree-mp/agree-plugin")
        library = 'data-stack="in_stack"' in _row_for(c.get("/library", headers=hdr).text, "agree-plugin")
        return api, library

    api, library = _both()
    assert api is False and library is False, "eligible-but-unsubscribed must read the same on both surfaces"

    assert c.post("/api/marketplace/curated/agree-mp/agree-plugin/install", headers=hdr).status_code == 200
    api, library = _both()
    assert api is True and library is True, "subscribed must read the same on both surfaces"


def test_library_own_artefact_keeps_a_real_stack_toggle(seeded_app):
    """The contrast case: an artifact's membership IS the caller's, so its
    pill stays an actionable button rather than a status."""
    c = seeded_app["client"]
    col = _create(seeded_app, "Toggleable Artifact")

    row = _row_for(c.get("/library", headers=_auth(seeded_app["admin_token"])).text, "Toggleable Artifact")
    assert "data-add-to-stack" in row  # not yet added
    assert "lib-instack--locked" not in row

    r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code in (200, 201), r.text

    row = _row_for(c.get("/library", headers=_auth(seeded_app["admin_token"])).text, "Toggleable Artifact")
    assert "data-remove-from-stack" in row  # …and removable once added
    assert "lib-instack--fixed" not in row
    assert "lib-instack--locked" not in row


# ── Hosted data apps ────────────────────────────────────────────────────────
#
# The Library is "everything you have", and an app built from chat is as much
# yours as a collection you uploaded. Until it was listed here a `rail`
# instance could not reach one at all: the "Apps" nav entry shipped in the
# topnav template only, so the feature ran, served, and had nothing anywhere
# in the UI pointing at it.

_APP_GLYPH = "M3.5 9h17"  # browser window with a title bar — a thing you open


def _seed_app(seeded_app, slug: str, name: str, monkeypatch):
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "true")
    from src.repositories import data_apps_repo

    data_apps_repo().create(slug=slug, name=name, owner_user_id="admin1")


def test_library_lists_a_hosted_data_app(seeded_app, monkeypatch):
    _seed_app(seeded_app, "libapp", "Library App Demo", monkeypatch)
    r = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "Library App Demo" in r.text, "an owned data app must appear in the Library"
    assert "/apps/detail/libapp" in r.text, "the row must link to the app's detail page"


def test_library_data_app_row_carries_the_app_glyph(seeded_app, monkeypatch):
    _seed_app(seeded_app, "glyphapp", "Glyph App", monkeypatch)
    r = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
    assert _APP_GLYPH in r.text, "data-app rows should not borrow the doc/plugin glyph"


def test_library_hides_data_apps_when_the_feature_is_off(seeded_app, monkeypatch):
    """A row linking to a 404 is worse than no row."""
    _seed_app(seeded_app, "offapp", "Disabled App", monkeypatch)
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "false")
    r = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
    assert "Disabled App" not in r.text


def test_library_excludes_drafts(seeded_app, monkeypatch):
    """A draft is an iteration branch of an app, not an app — the same
    exclusion `/apps` makes."""
    _seed_app(seeded_app, "parentapp", "Parent App", monkeypatch)
    from src.repositories import data_apps_repo

    repo = data_apps_repo()
    parent = repo.get_by_slug("parentapp")
    repo.create_draft(
        parent_app_id=parent["id"],
        slug="parentapp--wip",
        branch="wip",
        owner_user_id="admin1",
    )
    r = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
    assert "Parent App" in r.text
    assert "parentapp--wip" not in r.text


def test_library_does_not_give_an_admin_every_app_in_the_instance(seeded_app, monkeypatch):
    """The Library is "what you have", NOT an audit view.

    `library_page`'s own docstring says so: "Deliberately NOT admin god-mode
    — an admin still sees their own Library, not every item in the instance
    (the audit view is /admin/access)". Reusing `data_apps._can_view` here
    broke that, because it short-circuits True for any admin — so an admin's
    Library listed every hosted app including other people's private ones,
    labelled "Shared with you" (Devin Review on this PR).
    """
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "true")
    from src.repositories import data_apps_repo, users_repo

    users_repo().create(id="someone_else", email="someone.else@test.com", name="Someone Else")
    data_apps_repo().create(slug="notyours", name="Not Your App", owner_user_id="someone_else")

    r = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "Not Your App" not in r.text, "an admin must not see another user's app in their own Library"


def test_library_data_app_row_shows_the_admins_description_override(seeded_app, monkeypatch):
    """Effective description, not the raw column.

    A linked app carries both `description` (rewritten by the MCP lister on
    every sync) and `description_override` (an admin's edit); every other
    surface shows the override when set. Reading `a["description"]` here
    showed the stale synced wording right next to a detail page saying
    something else (Devin Review on this PR).
    """
    _seed_app(seeded_app, "overapp", "Override App", monkeypatch)
    from src.repositories import data_apps_repo

    repo = data_apps_repo()
    repo.update(repo.get_by_slug("overapp")["id"], description="Synced upstream text")
    repo.set_description_override("overapp", "The wording the admin chose")

    r = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "The wording the admin chose" in r.text
    assert "Synced upstream text" not in r.text


def test_library_own_data_app_row_carries_the_share_control(seeded_app, monkeypatch):
    """Data apps are a grantable resource type (`resource_grants` on the
    slug is what `_can_view` honours), so an owner's row must offer the same
    Share control every other owner-held kind does — rendering it share-less
    left /admin/access as the only sharing surface for the one kind a user
    builds from chat (Devin Review on this PR).

    The share id must be the SLUG: the dialog PUTs to
    `/api/sharing/{share_type}/{item_id}`, and a grant keyed on the row id
    would be read by nothing.
    """
    _seed_app(seeded_app, "shareapp", "Shareable App", monkeypatch)
    r = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200

    row_at = r.text.index('data-item-id="shareapp"')
    row = r.text[row_at : r.text.index("</tr>", row_at)]
    assert 'data-share-type="data_app"' in row, "an owned app row must be shareable"
    assert 'data-share="shareapp"' in row, "the Sharing badge must be the editable control, keyed on the slug"


@pytest.mark.parametrize("layout", ["topnav", "rail"])
def test_data_app_detail_renders_the_recorded_failure_reason(seeded_app, monkeypatch, layout):
    """A failed deploy stores the runner's own message in `state_detail`.

    It was recorded and returned by the API from the start, but no template
    rendered it — so the page showed a bare `error` badge while the Logs pane
    (the obvious next click) 502s for the very same reason. Parametrized over
    both layouts because the detail pages are a frozen pair: the default
    instance renders `_legacy`, so a fix on the redesigned copy alone would
    silently not exist for most installs.
    """
    _seed_app(seeded_app, f"errapp{layout}", "Errored App", monkeypatch)
    monkeypatch.setenv("AGNES_UI_LAYOUT", layout)

    from src.repositories import data_apps_repo

    repo = data_apps_repo()
    row = repo.get_by_slug(f"errapp{layout}")
    repo.set_state(row["id"], "error", "image_not_found")

    r = seeded_app["client"].get(f"/apps/detail/errapp{layout}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "image_not_found" in r.text, (
        f"[{layout}] the recorded failure reason must be on the page — without it a failed "
        "deploy leaves the operator with a bare 'error' and a 502 in the logs pane"
    )


def test_data_app_detail_shows_no_error_row_when_healthy(seeded_app, monkeypatch):
    """The row is for saying something, not for an empty slot on every app."""
    _seed_app(seeded_app, "okapp", "Fine App", monkeypatch)
    r = seeded_app["client"].get("/apps/detail/okapp", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'id="dda-state-detail"' not in r.text


# ---------------------------------------------------------------------------
# The type map is NOT on this page. It counts what extraction pulled OUT of
# documents — a setup question, asked by whoever configured the crawl — and it
# now lives on /admin/ontology, beside the controls that fix a bad number. Its
# new contract needs a graph, so it needs Postgres: tests/db_pg/test_facts_ui.py.
# ---------------------------------------------------------------------------


def test_library_renders_without_a_type_map_when_the_graph_is_empty(seeded_app):
    """The facts feature is off by default, so this is the ordinary case: the
    Library must look exactly as it did before this wiring existed."""
    r = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.text
    assert 'id="lib-typemap"' not in r.text


def test_the_definitions_strip_is_not_tab_driven(seeded_app):
    """It stands above the split, at the rank of the page's own lede, so it is
    visible in BOTH buckets — a page-level sign, never a member of one half.

    It used to be Knowledge-only, on the reasoning that definitions describe
    data and the other half holds skills, plugins and agents. What retired
    that is the toolbar moving above the tabs: with one search over the whole
    Library and the tabs reading its results, there is no half left for a
    page-level sign to live inside.
    """
    _seed_definitions()
    src = (Path("app/web/templates/library.html")).read_text(encoding="utf-8")
    assert "lib-defs-wrap" not in src, "the tab-gated wrapper is gone — the strip is page-level now"

    c = seeded_app["client"]
    for tab in ("knowledge", "capabilities"):
        r = c.get(f"/library?tab={tab}", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert 'id="lib-defs"' in r.text, f"the strip must render on {tab} too"


def test_the_tabs_sit_under_the_toolbar_as_result_buckets():
    """The tabs are not a scope picked before searching — they are where one
    search's results fell.

    They headed the page for a long time, above the toolbar, on the ordinary
    rule that the wider scope contains the narrower. The ENGINE never worked
    that way: `refreshTabCounts` calls `libRowsMatching(null, true)` — the
    `ignoreTab` path — so search and every facet have always been applied
    across both halves, and each badge has always been that half's share of
    one global result set. Above the search box the tabs described a set the
    reader had not narrowed yet; under it they read correctly, and a match in
    the half you are not looking at is a number instead of being invisible.

    No engine change came with the move, which is exactly why this needs a
    guard: putting the tabs back above the toolbar would silently restore a
    layout the code contradicts.
    """
    src = (Path("app/web/templates/library.html")).read_text(encoding="utf-8")
    # Order in the source is the order on the page: the Definitions card closes
    # the page head, then toolbar, tabs, count, list. It sits ABOVE the controls
    # because a semantic model is not optional — `src/claude_md.py` writes every
    # readable model into the workspace document at session start, and there is
    # no `semantic_model` type in the Stack, so no row-level "Add to my agents"
    # exists for one. Every row in the list below has an opt-in state, which is
    # the defining property of that region; an always-on block among them reads
    # as a member of a set it is not in.
    defs_at = src.index("{{ definitions_strip(library_definitions) }}")
    bar_at = src.index('<div class="fbar fbar--ranked"')
    tabs_at = src.index('<div class="tab-strip lib-tabs" id="lib-tabs"')
    count_at = src.index('id="lib-item-count"')
    assert defs_at < bar_at < tabs_at < count_at
    # The tabs left the page head with the move; a rule still scoping them
    # there is dead and would drop their spacing without failing anything.
    assert ".lib-head .lib-tabs" not in src
    # The cross-tab count path is what makes the order correct — keep it.
    assert "libRowsMatching(null, true)" in src


def test_the_type_map_helper_fails_soft_on_every_axis():
    """Facts off, DuckDB backend, or an empty graph all return [] — the
    Library must not 500 because a decoration is unavailable."""
    from app.web.router import _library_type_map

    assert _library_type_map({"id": "nobody", "email": "nobody@test.com"}) == []


# ---------------------------------------------------------------------------
# Why the caller has a granted row
# ---------------------------------------------------------------------------


def _grant_package_to_named_group(conn, *, slug, name, user_id, group_name, requirement="required"):
    """Seed a package granted through a NAMED group the user belongs to.

    ``_grant_package`` above grants through ``Everyone``, which is the one
    group whose name makes the reason clause uninformative — everybody is in
    it. These tests need the interesting case.
    """
    import uuid

    from src.repositories.data_packages import DataPackagesRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    pkg_id = DataPackagesRepository(conn).create(
        name=name, slug=slug, description="d", icon=None, color=None, created_by="test"
    )
    gid = UserGroupsRepository(conn).ensure(group_name, description="")["id"]
    UserGroupMembersRepository(conn).add_member(user_id, gid, source="test")
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) VALUES (?, ?, 'data_package', ?, ?, CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), gid, pkg_id, requirement],
    )
    return pkg_id


def test_granted_row_names_the_group_that_brought_it(seeded_app):
    """*Why do I have this?* — the one question about a granted row that the
    product could not answer anywhere.

    The row already said WHAT it is and that an admin put it there. Through
    WHICH group was nowhere: not on the row, not on the detail page, not in
    /me/profile. It is also the only part of the answer a member can act on,
    because the group is what they ask their admin to change.

    The clause is APPENDED to the existing sentence rather than replacing it —
    the two answer different questions ("can I remove this" and "why do I have
    it"), and the existing one is asserted verbatim elsewhere.
    """
    from src.db import get_system_db

    conn = get_system_db()
    _grant_package_to_named_group(
        conn, slug="fin-pkg", name="Finance Core", user_id="analyst1", group_name="Finance team"
    )
    conn.close()

    body = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row_for(body, "Finance Core")
    assert LOCKED_TOOLTIP in row
    assert "You have it because you are in Finance team." in row


def test_two_granting_groups_are_both_named(seeded_app):
    """Reaching a package through two groups is not an edge case — it is what
    happens the moment an admin grants to a team AND to Everyone. Naming only
    one would answer the question wrongly rather than partially: the member
    would ask to be removed from a group that is not the reason.
    """
    import uuid

    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    pkg_id = _grant_package_to_named_group(
        conn, slug="dual-pkg", name="Dual Granted", user_id="analyst1", group_name="Analysts"
    )
    gid = UserGroupsRepository(conn).ensure("Ops", description="")["id"]
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) VALUES (?, ?, 'data_package', ?, 'required', CURRENT_TIMESTAMP, 't')",
        [str(uuid.uuid4()), gid, pkg_id],
    )
    conn.close()

    body = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row_for(body, "Dual Granted")
    assert "You have it because you are in Analysts and Ops." in row


def test_reason_clause_never_takes_the_row_down(seeded_app, monkeypatch):
    """The clause is an addition to a tooltip that reads correctly without it,
    so a failure to resolve the reason must degrade to the old sentence — not
    to a 500 on the member's main page. Guarded because the lookup reaches two
    repositories that the rest of the row does not need.
    """
    from src.db import get_system_db

    conn = get_system_db()
    _grant_package_to_named_group(
        conn, slug="boom-pkg", name="Still Renders", user_id="analyst1", group_name="Finance team"
    )
    conn.close()

    from app.services.stack_resolver import StackResolver

    def _explode(self, user_id, resource_type):
        raise RuntimeError("groups repo down")

    monkeypatch.setattr(StackResolver, "granting_groups", _explode)
    resp = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    row = _row_for(resp.text, "Still Renders")
    assert LOCKED_TOOLTIP in row
    assert "You have it because" not in row


def test_the_definitions_glyph_is_in_both_sources():
    """`kind_glyph` in macros/_catalog_card.html is the ORIGINAL and
    static/js/kind_glyph.js is its transcription — the macro's own note says
    adding a kind means adding it to both and nowhere else. The JS copy had
    already fallen six kinds behind once, which is the failure that file
    exists to prevent, so a new kind is pinned in both."""
    from pathlib import Path

    path = "M12 7.2C10.4 5.7 7.9 5.1 4 5.6v12.6c3.9-.5 6.4.1 8 1.6 1.6-1.5 4.1-2.1 8-1.6V5.6c-3.9-.5-6.4.1-8 1.6Z"
    macro = Path("app/web/templates/macros/_catalog_card.html").read_text(encoding="utf-8")
    js = Path("app/web/static/js/kind_glyph.js").read_text(encoding="utf-8")
    assert "kind == 'definitions'" in macro
    assert "definitions:" in js
    assert path in macro and path in js, "the same drawing, not two drawings of the same idea"


def test_the_agent_template_band_link_survives_the_definitions_move():
    """#2015's "Start from a template" link is band-level, and this branch
    rewrote the band it sits in.

    The regression it guards against is silent: the router keeps passing
    `band_link`, so nothing errors — the macro that renders it was simply
    dropped along with the Definitions band it used to share classes with, and
    the link disappears. Main has no test for that link at all, which is why it
    could go unnoticed; a parallel session caught it, not me.

    Asserted as the CHAIN rather than through a rendered page: the rows come
    from `user_store_installs_repo`, so a live render needs a store install
    seeded, and each link below is a place the chain has actually broken —
    populated, called, rendered, styled.
    """
    from pathlib import Path

    router = Path("app/web/router.py").read_text(encoding="utf-8")
    src = Path("app/web/templates/library.html").read_text(encoding="utf-8")

    # 1. the router still populates it for the agent section
    assert '"band_link": (' in router
    assert '{"href": "/agents?from_template=1", "label": "Start from a template"}' in router

    # 2. the macro exists, and renders the link as a real anchor
    macro = src.split("{% macro band_link(link) %}", 1)[1].split("{% endmacro %}", 1)[0]
    assert 'href="{{ link.href }}"' in macro
    assert "{{ link.label }}" in macro

    # 3. …and is CALLED. This is the link that was missing.
    assert "{{ band_link(sec.band_link) }}" in src

    # 4. …and is styled. Its own class names now: on main the macro borrowed
    #    `.lib-defs__link(s)` from the Definitions band, which this branch turns
    #    into a page-level row, so those names would describe a block nowhere
    #    near a section band.
    assert ".lib-band__link {" in src
    assert "lib-defs__link" not in src, "the retired Definitions-band classes are gone"


def test_definitions_link_avoids_a_tab_with_nothing_in_it(seeded_app):
    """Metrics is the default landing, not the unconditional one.

    An instance whose semantic layer holds glossary terms and no metrics was
    told "0 metrics, 3 glossary terms" and then sent to the metrics tab — the
    one empty list on the page (Devin Review on #2069). The link follows the
    counts it just stated.
    """
    _seed_definitions(metrics=0, terms=3)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'href="/semantic-layer?tab=all_glossary"' in r.text
    assert 'href="/semantic-layer?tab=all_metrics"' not in r.text


def test_one_unreadable_count_does_not_erase_the_definitions_row(seeded_app, monkeypatch):
    """The three counts had ONE exception boundary between them, so a single
    failing read removed the whole row and told a reader with a populated
    semantic layer that they had none (Devin Review on #2069). Each read now
    stands alone: the failed one degrades, the others still state their
    number."""
    _seed_definitions(metrics=2, terms=3)

    import app.web.router as router

    def _boom(*a, **k):
        raise RuntimeError("metric table unavailable")

    monkeypatch.setattr(router, "metric_repo", _boom)

    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "glossary term" in r.text, (
        "a failed metric read must not take the glossary count down with it"
    )
    assert "3 glossary term" in r.text


def test_a_saturated_glossary_count_is_not_stated_as_an_exact_total():
    """`_glossary_terms_count` reads at most `_GLOSSARY_COUNT_LIMIT` rows, so
    at or past that limit the number it returns IS the limit. Rendering it
    bare states a cap as a total (Devin Review on #2069)."""
    from app.web.router import _GLOSSARY_COUNT_LIMIT, _glossary_count_label

    assert _glossary_count_label(0) == "0"
    assert _glossary_count_label(_GLOSSARY_COUNT_LIMIT - 1) == str(_GLOSSARY_COUNT_LIMIT - 1)
    assert _glossary_count_label(_GLOSSARY_COUNT_LIMIT) == f"{_GLOSSARY_COUNT_LIMIT}+"
