"""Web UI routes for Collections — /library and /library/{slug}."""

from __future__ import annotations
import pytest

import io

# Glyph path fragments (mirror macros/_catalog_card.html kind_glyph).
_DOC_GLYPH = "M7 4h7l4 4v12H7z"  # single document — a one-file artefact
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

    The Definitions footer only renders when at least one side is populated, so
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


def test_library_shows_definitions_as_a_footer_not_a_row(seeded_app):
    """The semantic layer closes the page; it is NOT part of the inventory.

    It shipped briefly as a "Definitions" band holding two rows, and that was
    wrong: metrics and glossary terms are the one thing here nobody owns,
    shares, installs, drops or edits, so as rows they had to blank all four of
    the table's columns at once (Owner / Sharing / Stack / Actions). Four
    special-cased columns is the list saying the object is not one of its rows.
    A data package looks similar but differs where it counts — access to it
    varies per caller, which is what makes it "what I have"; everyone has the
    whole glossary.
    """
    _seed_definitions(metrics=2, terms=3)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'id="lib-defs"' in r.text
    assert "2 metrics" in r.text
    assert "3 glossary terms" in r.text
    assert 'href="/catalog/semantics#metrics"' in r.text
    assert 'href="/catalog/semantics#glossary"' in r.text
    # Not inventory: no band, no row, no kind.
    assert 'data-lib-sec="definitions"' not in r.text
    assert 'data-kind="definitions"' not in r.text
    # And not the retired header link either.
    assert "lib-browse-semantics" not in r.text


def test_library_definitions_footer_carries_its_contents_for_search(seeded_app):
    """The footer is searchable BY TERM, not just by the word "definitions".

    Someone types "MRR" or "active account" — the term they half-remember —
    the list comes back empty, and the footer is the one thing on the page
    that knows the word. Without the index it stays silent and the reader
    concludes Agnes has never heard of it. This is also the only one-step term
    lookup the rail chrome has, since it renders no global search box.
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
    index = r.text.split('data-defs-search="', 1)[1].split('"', 1)[0]
    # Reachable by display name, short name, synonym and glossary term.
    assert "monthly" in index and "recurring" in index
    assert "mrr" in index
    assert "arr" in index
    assert "active" in index
    # Definition BODIES stay out — the index ships on every page load, and
    # matching on prose would surface the block on incidental words.
    assert "normalized" not in index
    assert "paid" not in index


def test_library_definitions_footer_counts_are_singular_for_one(seeded_app):
    """ "1 metric", not "1 metrics" — the count is read as a sentence."""
    _seed_definitions(metrics=1, terms=1)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "1 metric " in r.text or "1 metric&" in r.text or "1 metric<" in r.text
    assert "1 metrics" not in r.text
    assert "1 glossary terms" not in r.text


def test_library_hides_definitions_footer_when_semantic_layer_is_empty(seeded_app):
    """No metrics AND no glossary -> no footer.

    A block advertising "0 metrics · 0 glossary terms" describes the instance's
    setup, not its content, and reads as a broken feature rather than an
    unconfigured one. One populated side is enough to render it.
    """
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'id="lib-defs"' not in r.text
    assert "/catalog/semantics" not in r.text


def test_library_shows_definitions_footer_when_only_one_side_is_populated(seeded_app):
    """Metrics but no glossary still renders it — "0 glossary terms" is a true
    and useful statement about a semantic layer that exists."""
    _seed_definitions(metrics=1, terms=0)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'id="lib-defs"' in r.text
    assert "0 glossary terms" in r.text


def test_library_definitions_footer_is_labeled_in_plain_words(seeded_app):
    """ "Definitions", never "Semantic layer" — that name belongs to
    /admin/semantic-layer, where the reader operates the sync rather than
    looking a term up. Scoped to the footer because an admin's nav dropdown
    carries the admin link on every page, and that one is correct."""
    _seed_definitions()
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    block = r.text.split('id="lib-defs"', 1)[1].split("</aside>", 1)[0]
    assert "Definitions" in block
    assert "Semantic layer" not in block


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
    """One file in an artefact reads AS the file — single-document glyph,
    filename + size in the meta, "File" framing, never "a collection with 1
    file" — but the title is the artefact's NAME (what the caller typed), not
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
    """The page leads with what is IN the artefact; adding to it comes after
    (Files section above the Add-files drop zone). On a one-file artefact the
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
    """A second file promotes the artefact to a Collection: the list shows
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
    """Regression: two single-file artefacts holding the *same* filename but
    given different names must render under their own names on the Artefacts
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
#   Own artefact     → "In stack" ⇄ "Add to stack", a real toggle. A personal
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
#: Both tiers are locked; only the wording differs.
LOCKED_TOOLTIP = "Required by your admin and cannot be removed from your stack."
GRANTED_TOOLTIP = "Granted to your group — only an admin can remove it from your stack."


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
    assert "In stack" in row
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
    assert "In stack" in row
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
    assert "In stack" in row


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
    """The contrast case: an artefact's membership IS the caller's, so its
    pill stays an actionable button rather than a status."""
    c = seeded_app["client"]
    col = _create(seeded_app, "Toggleable Artefact")

    row = _row_for(c.get("/library", headers=_auth(seeded_app["admin_token"])).text, "Toggleable Artefact")
    assert "data-add-to-stack" in row  # not yet added
    assert "lib-instack--locked" not in row

    r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code in (200, 201), r.text

    row = _row_for(c.get("/library", headers=_auth(seeded_app["admin_token"])).text, "Toggleable Artefact")
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
