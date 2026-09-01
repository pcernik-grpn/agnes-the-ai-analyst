"""The package detail page (`GET /admin/data-packages/{id}`).

Until this page existed the package — the unit an analyst actually receives —
was the one object in the Data section with no home. Its composition was
edited inside the package-grouped layout on /admin/tables, its sharing on
/admin/data-packages, and the card's own drilldown left the admin area
entirely for the analyst-facing /catalog/p/<slug>.

What this suite pins:

  * the page exists, is admin-gated, and 404s on an unknown id;
  * it states the package's whole life in reading order — what is IN it, who
    can USE it, who actually HAS it;
  * the delivery read-out distinguishes a grant (a permission) from a pull
    (the data actually landing), which is the fact the product could not
    state anywhere before;
  * `query_mode` is worded in plain language while KEEPING the system word,
    so the CLI vocabulary stays learnable;
  * the Packages lens drills HERE, not into /catalog;
  * no tab strip — a lens switch offered one level below the lenses is a way
    to leave the package you opened without noticing;
  * the way back is the page's FIRST line, above the heading;
  * the two states that strand analysts (no tables, shared with nobody) are
    bands ABOVE the work, each carrying the control that fixes it;
  * the page WRITES NOTHING. It carried two editors of its own — an
    add-tables drawer and a share drawer writing grants on every click — plus
    the create/edit form opened as a third chrome for the same fields, which
    is how one object ended up with four ways in and the page that answers
    "who does this reach" also being the page that changed it. Every verb here
    is now a link to /admin/data-packages/{id}/edit, the one write surface;
    what stays is the read-out a builder cannot hold.
"""

from __future__ import annotations

import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mk_pkg(name: str = "Revenue Core") -> str:
    from src.repositories import data_packages_repo

    return data_packages_repo().create(
        slug=f"pkg-{uuid.uuid4().hex[:8]}",
        name=name,
        description="The canonical revenue tables.",
        icon=None,
        color=None,
        created_by="test",
    )


class TestPageExists:
    def test_renders_for_an_admin(self, seeded_app):
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        r = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "Revenue Core" in r.text

    def test_unknown_package_is_a_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.get(
            "/admin/data-packages/pkg_does_not_exist",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 404

    def test_requires_admin(self, seeded_app):
        """Same gate as every other /admin page — an unauthenticated caller
        must not read the instance's distribution map."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        r = c.get(f"/admin/data-packages/{pkg_id}", follow_redirects=False)
        assert r.status_code in (302, 303, 307, 401, 403)


class TestTheWholeLifeInReadingOrder:
    def test_carries_the_three_panels(self, seeded_app):
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        # what is in it → who can use it → who actually has it
        assert "Tables" in html
        assert "Sharing" in html
        assert 'id="apd-delivery"' in html
        assert "At a glance" in html

    def test_composition_and_sharing_are_someone_else_s_job_now(self, seeded_app):
        """This page reads. It used to write both — composition through the
        data-packages junction and sharing through the same
        `/api/admin/grants` rows a group's Access tab writes — from two
        drawers of its own. Both moved to the builder, which is the only
        surface that writes a package, so the page carries no write endpoint
        at all."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "/api/admin/grants" not in html
        assert f"/admin/data-packages/{pkg_id}/edit" in html
        # The grants are still editable from the other end of the same
        # relationship, which this page names rather than reimplements.
        assert "/admin/access" in html

    def test_no_tab_strip(self, seeded_app):
        """A detail page is one level BELOW the lenses. Offering the lens
        strip here invites a lateral move that silently abandons the package
        you opened; `.apg-back` is the way out and it names where it goes."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "tab-flow__item" not in html
        assert 'class="apg-back"' in html

    def test_the_way_back_is_above_the_heading(self, seeded_app):
        """It used to render inside `{% block page %}`, which sits BELOW the
        hero — so the link out of the page appeared under the title it is
        meant to precede. `{% block page_prehead %}` is the slot for it."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert html.index('class="apg-back"') < html.index('class="page-header__title"')


class TestTheAlarmsComeBeforeTheWork:
    """A package with no tables and a package nobody can see are the two
    states that strand analysts. Both were previously legible only by counting
    rows in a panel; each is now a band above the columns carrying the control
    that fixes it."""

    def test_an_empty_package_says_so_above_the_columns(self, seeded_app):
        c = seeded_app["client"]
        pkg_id = _mk_pkg("Empty One")
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "This package carries no tables" in html
        assert html.index("carries no tables") < html.index('class="apd-cols"')

    def test_an_unshared_package_says_so_above_the_columns(self, seeded_app):
        c = seeded_app["client"]
        pkg_id = _mk_pkg("Unshared One")
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        band = html[html.index('id="apd-unshared-note"') : html.index('class="apd-cols"')]
        assert "hidden" not in band.split(">")[0], "the band must render open when nothing is shared"
        assert "Shared with nobody" in band

    def test_the_unshared_band_is_hidden_once_a_grant_exists(self, seeded_app):
        from src.repositories import resource_grants_repo, user_groups_repo

        c = seeded_app["client"]
        pkg_id = _mk_pkg("Shared One")
        everyone = next(g for g in user_groups_repo().list_all() if g["name"] == "Everyone")
        gid = resource_grants_repo().create(
            group_id=everyone["id"],
            resource_type="data_package",
            resource_id=pkg_id,
            requirement="available",
            assigned_by="test",
        )
        try:
            html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
            band_open = html[html.index('id="apd-unshared-note"') :].split(">")[0]
            assert "hidden" in band_open
        finally:
            resource_grants_repo().delete(gid)


class TestEveryVerbLeaves:
    """The page had three editors and now has none.

    Two drawers of its own (add-tables, share) plus the create/edit form
    opened as a third chrome for fields that are also a full builder page and
    were also a drawer on the admin grid. One object, four ways in, each
    looking like a different product and each free to drift. What is left here
    is what a builder cannot hold — per-table mode and freshness, the reach
    arithmetic, the per-group people counts — and every control is a link to
    the one surface that writes.
    """

    def _html(self, seeded_app) -> str:
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        return c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text

    def test_no_drawer_survives_on_this_page(self, seeded_app):
        html = self._html(seeded_app)
        for gone in ('id="apd-add-drawer"', 'id="apd-share-drawer"', "ds-drawer__panel", "css/drawer.css"):
            assert gone not in html, f"{gone} is still here"
        # Nor the component that was the third copy of the same form.
        assert "js/components/package_drawer.js" not in html
        assert "AgnesPackageDrawer" not in html

    def test_the_pickers_and_their_toolbar_left_with_the_drawers(self, seeded_app):
        """The add-tables picker's search, facet menu and sort moved INTO the
        builder's own picker (see tests/test_web_package_one_write_surface.py),
        so nothing of it should be served here — including the ~500 candidate
        rows the route used to enumerate for it."""
        html = self._html(seeded_app)
        for gone in ('id="apd-add-search"', 'id="apd-add-filter-menu"', 'id="apd-add-sort"',
                     'id="apd-add-save"', "js/filter_toolbar.js", "data-pick-table="):
            assert gone not in html, f"{gone} is still here"

    def test_the_controls_that_remain_are_links_to_the_builder(self, seeded_app):
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        edit = f"/admin/data-packages/{pkg_id}/edit"
        # Add tables, edit access, edit details — one destination.
        assert html.count(edit) >= 3, "each verb on the page should lead to the builder"
        # And the alarm bands above the columns still carry their fix.
        assert "+ Add tables" in html and "Edit details" in html

    def test_nothing_on_the_page_removes_anything(self, seeded_app):
        """Removal is staged in the builder and applied on Save, so there is
        no immediate destructive write left here to confirm — which is why the
        three confirm dialogs this page used to carry are gone rather than
        merely unreferenced."""
        from src.repositories import data_packages_repo, table_registry_repo

        repo = table_registry_repo()
        tid = f"sel-{uuid.uuid4().hex[:6]}"
        repo.register(
            id=tid,
            name=f"sel_table_{tid[-6:]}",
            source_type="keboola",
            bucket="in.c-sel",
            source_table="sel",
            query_mode="local",
        )
        pkg_id = _mk_pkg()
        try:
            c = seeded_app["client"]
            data_packages_repo().add_table(pkg_id, tid, added_by="test")
            html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
            # The member is still LISTED — this page's job is saying what the
            # package carries.
            assert tid in html or f"sel_table_{tid[-6:]}" in html
            # …but not with a control that removes it, in bulk or per row.
            for gone in ('id="apd-selall"', 'id="apd-selremove"',
                         f'data-pick-row="{tid}"', "data-remove=", "data-unshare="):
                assert gone not in html, f"{gone} is still here"
            # Not `confirmModal` itself — that helper is served on every page
            # by _app_scripts.html. What must be gone are the two dialogs THIS
            # page raised, named by the button each one offered.
            assert "Remove table" not in html
            assert "Stop sharing" not in html
        finally:
            # Membership first: `table_registry` is the FK parent of the
            # package junction, so unregistering a table still in a package
            # raises rather than cascading.
            data_packages_repo().remove_table(pkg_id, tid)
            repo.unregister(tid)


class TestDeliveryReadOut:
    def test_says_nothing_is_delivered_when_shared_with_nobody(self, seeded_app):
        """A package nobody can see is the state that strands analysts, and
        it was previously invisible on every surface."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg("Orphan")
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "share it with a group first" in html

    def test_separates_a_grant_from_a_pull(self, seeded_app):
        """Shared != delivered. A grant is a permission; the data lands only
        when `agnes pull` runs, and `users.last_pull_at` is what turns
        "shared with 14" into "11 actually have it"."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "on their machine" in html
        assert "not yet delivered" in html

    def test_counts_only_automatic_grants_as_reached(self, seeded_app):
        """An OPTIONAL grant only makes the package offerable — counting it
        as reached would overstate delivery, which is the exact confusion
        this panel exists to end."""
        from src.repositories import resource_grants_repo, user_groups_repo

        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        everyone = next(g for g in user_groups_repo().list_all() if g["name"] == "Everyone")
        resource_grants_repo().create(
            group_id=everyone["id"],
            resource_type="data_package",
            resource_id=pkg_id,
            requirement="available",
            assigned_by="test",
        )
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "every grant here is optional" in html


class TestPlainLanguageModes:
    def test_leads_with_meaning_and_keeps_the_system_word(self, seeded_app):
        """`query_mode` is vocabulary the admin meets again in `agnes
        catalog` and in the API, so it must survive — but "local" alone
        never said what it does. Both, in that order."""
        from app.web.router import _mode_words

        assert _mode_words("local") == {"label": "Synced copy", "word": "local"}
        assert _mode_words("remote") == {"label": "Live query", "word": "remote"}
        assert _mode_words("materialized") == {"label": "Saved query", "word": "materialized"}
        # A blank mode reads as local — the schema default, and what every
        # other consumer already assumes.
        assert _mode_words("")["word"] == "local"
        assert _mode_words(None)["word"] == "local"

    def test_an_unknown_mode_reads_as_itself(self, seeded_app):
        """A mode nobody has worded yet must never be guessed into the wrong
        one — it passes through verbatim until someone names it."""
        from app.web.router import _mode_words

        assert _mode_words("brand_new") == {"label": "brand_new", "word": "brand_new"}


class TestPackagesLensDrillsHere:
    def test_card_drilldown_is_the_admin_page_not_the_catalog(self, seeded_app):
        """The card is the ADMIN's index of packages; its drilldown used to
        leave the admin area for a read-only page written for a different
        reader."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert f"/admin/data-packages/{pkg_id}" in html
