"""The Access surface (`GET /admin/access`) — the third leg of
People → Data → Access.

This URL has had three lives and the tests have to pin the current contract
without losing the reason for the middle one: it was a standalone grant
matrix, was retired into the group detail page's Access tab (grants key on
`group_id`, so the group's own page is where a single group's grants belong),
and returns as the cross-group WORKSPACE plus **Simulate** — two jobs the
per-group tab structurally cannot do.

What matters, and so what is pinned here:

  * it is not a fork — the page reads `/api/admin/access-overview` and writes
    the same `/api/admin/grants` rows as the group tab and the package-side
    Share editor, so the three surfaces cannot disagree;
  * both entry points survive (the group tab is still linked);
  * the legacy `/admin/grants` URL still resolves, carrying `?group=`;
  * the admin gate holds on the page AND on the redirect — a 308 naming an
    internal URL would leak where a surface lives to a non-admin.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAccessPage:
    def test_admin_sees_both_tabs(self, seeded_app):
        """TWO tabs. There were three while "Groups" was a separate list page
        over the same rows the workspace's left column carries — a section
        whose first two tabs were both "here are the groups"."""
        c = seeded_app["client"]
        resp = c.get("/admin/access", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Groups" in resp.text
        assert "Simulate a person" in resp.text
        assert "Who can use what" not in resp.text

    def test_non_admin_is_refused(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/access", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code in (401, 403)

    def test_it_reads_and_writes_the_canonical_grant_apis(self, seeded_app):
        """One storage, three entry points. A page that grew its own endpoint
        (or its own table) is how the group tab and this view would start
        disagreeing about who can use what."""
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert "/api/admin/access-overview" in body
        assert "/api/admin/grants" in body

    def test_the_group_side_editor_folded_into_this_page(self, seeded_app):
        """The per-group detail page carried Members and Access as two tabs.
        They are this page's two panes, so the page redirects here rather
        than standing beside it as a second editor over the same rows."""
        c = seeded_app["client"]
        auth = _auth(seeded_app["admin_token"])
        gid = c.get("/api/admin/groups", headers=auth).json()[0]["id"]
        r = c.get(f"/admin/groups/{gid}", headers=auth, follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == f"/admin/access?group={gid}"

    def test_an_unknown_group_still_404s(self, seeded_app):
        """Redirecting an unknown id would silently open on a different
        group — worse than saying it is gone."""
        c = seeded_app["client"]
        r = c.get("/admin/groups/nope-not-a-group", headers=_auth(seeded_app["admin_token"]), follow_redirects=False)
        assert r.status_code == 404

    def test_tiers_are_worded_as_what_they_do(self, seeded_app):
        """`available`/`required` is the API's vocabulary; an admin reads
        Optional/Automatic. Both must be present — the plain-language label
        for the reader, the system word for the control's title so the two
        vocabularies stay connected."""
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert "Optional" in body and "Automatic" in body
        assert '"available"' in body and '"required"' in body

    def test_simulate_uses_the_effective_access_endpoint(self, seeded_app):
        """The reason chain is derived from the explicit grant graph the API
        already exposes, not recomputed in the page — recomputing it is how a
        debugging view starts disagreeing with enforcement."""
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert "effective-access" in body
        assert "memberships" in body

    def test_admin_god_mode_is_stated_not_hidden(self, seeded_app):
        """Admins reach everything regardless of grants (`is_user_admin`
        short-circuits every check). A page about access that does not say so
        invites an admin to conclude their grants are what let them in."""
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert "Admins can always reach everything" in body


class TestLegacyGrantsUrl:
    def test_it_redirects_to_the_access_page(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/admin/grants",
            headers=_auth(seeded_app["admin_token"]),
            follow_redirects=False,
        )
        assert resp.status_code == 308
        assert resp.headers["location"] == "/admin/access"

    def test_it_carries_the_group_deep_link_through(self, seeded_app):
        """The retired matrix accepted `?group=<id>`; the Access page reads it
        to preselect that group, so an old bookmark still lands somewhere
        useful instead of on an arbitrary first group."""
        c = seeded_app["client"]
        resp = c.get(
            "/admin/grants?group=grp-123",
            headers=_auth(seeded_app["admin_token"]),
            follow_redirects=False,
        )
        assert resp.status_code == 308
        assert resp.headers["location"] == "/admin/access?group=grp-123"

    def test_the_redirect_keeps_the_admin_gate(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/admin/grants",
            headers=_auth(seeded_app["analyst_token"]),
            follow_redirects=False,
        )
        assert resp.status_code in (401, 403)


class TestAccessIsInTheNav:
    def test_the_access_section_carries_the_page(self):
        """Access is the third intent DESTINATION, and it has TWO tabs: the
        groups workspace (who is in each group, and what each one can use —
        one object, one editor) and the question you ask of the result.

        There were three while Groups was a separate list page over the same
        rows the workspace's left column already carries. That table's
        columns are on the selector rows and in the pane header now, so the
        tab it had would be a second name for the page you are already on."""
        from app.web.admin_nav import ADMIN_NAV_SECTIONS, resolve_active_section_key, resolve_section_tabs

        access = next((s for s in ADMIN_NAV_SECTIONS if s["key"] == "access"), None)
        assert access is not None, "the Access section is missing from the nav inventory"
        assert access["href"] == "/admin/access"
        assert [t["label"] for t in access["tabs"]] == [
            "Groups",
            "Simulate a person",
        ]
        # The row lands where its first tab does — see the guard in
        # test_web_admin_nav.py::test_a_destination_row_lands_on_its_own_first_tab.
        assert access["tabs"][0]["href"] == access["href"]

        # Every path sits in the section, including the two redirects — a 308
        # is followed by the browser, but anything resolving a section from a
        # path must still light Access rather than nothing.
        assert resolve_active_section_key("/admin/access") == "access"
        assert resolve_active_section_key("/admin/groups") == "access"
        assert resolve_active_section_key("/admin/grants") == "access"

        # The strip lights exactly one tab per page.
        for path, query, lit in (
            ("/admin/access", "", "Groups"),
            ("/admin/access", "lens=simulate", "Simulate a person"),
        ):
            active = [t["label"] for t in resolve_section_tabs(path, query) if t["active"]]
            assert active == [lit], (path, query, active)

    def test_the_page_renders_the_nav_row_as_active(self, seeded_app):
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        nav = body.split('<aside class="admin-nav"', 1)[1].split("</aside>", 1)[0]
        # The sidebar's active row is the Access DESTINATION, and only it — no
        # item row anywhere else in the column is lit.
        assert nav.count("is-active") == 1
        assert 'class="admin-nav__link admin-nav__link--dest is-active"' in nav
        assert 'href="/admin/access"' in nav
        # The page renders the SECTION's strip — not the local button strip the
        # two lenses used to be. `data-tab="…"` was that strip's pane-switch
        # hook, on the buttons AND on the one handler that clicked them; both
        # are gone, so Simulate is reached by URL and by nothing else.
        assert 'class="admin-tabs"' in body
        assert 'href="/admin/access?lens=simulate"' in body
        assert 'data-tab="' not in body

    def test_the_simulate_lens_opens_server_side(self, seeded_app):
        """A deep link paints the right pane on the first byte. Rendering the
        editor and letting JS swap panes would flash the wrong lens on every
        visit to a bookmarked Simulate URL."""
        c = seeded_app["client"]
        auth = _auth(seeded_app["admin_token"])

        bare = c.get("/admin/access", headers=auth).text
        assert '<div class="ax-pane is-on" data-axpane="edit">' in bare
        assert '<div class="ax-pane" data-axpane="sim">' in bare

        sim = c.get("/admin/access?lens=simulate", headers=auth).text
        assert '<div class="ax-pane" data-axpane="edit">' in sim
        assert '<div class="ax-pane is-on" data-axpane="sim">' in sim


class TestMembersInContext:
    """A group is two things — who is in it, and what it can use. Editing the
    second while the first is a page away is where an admin loses the thread
    ("Finance can use Revenue… is Maria even in Finance?"), so the membership
    slice sits above the grants with the one write action that question leads
    to.

    It is a SLICE, not a second People section: the full roster,
    deactivation and credentials stay on People, and this pane links there.
    """

    def _body(self, seeded_app) -> str:
        c = seeded_app["client"]
        return c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text

    def test_the_members_pane_exists_above_the_grants(self, seeded_app):
        body = self._body(seeded_app)
        assert 'id="ax-members"' in body
        # ...and before the resource sections in document order, because the
        # question arrives in that order.
        assert body.index('id="ax-members"') < body.index('id="ax-resources"')

    def test_it_uses_the_canonical_membership_endpoints(self, seeded_app):
        """Same rows the group detail page's Members tab writes — a second
        membership store is how the two surfaces would start disagreeing about
        who is in a group."""
        body = self._body(seeded_app)
        assert "/members" in body
        assert '"POST"' in body and '"DELETE"' in body

    def test_adding_a_stranger_routes_to_People_instead_of_failing_blankly(self, seeded_app):
        """The common miss is a person with no account yet. That is a People
        job, so BOTH dead ends name it: the search that finds nobody, and the
        add that comes back 404 anyway (a race, or an account deactivated
        between the two calls)."""
        body = self._body(seeded_app)
        assert "invite them on People first" in body
        assert "Invite them on People first" in body

    def test_everyone_is_explained_not_enumerated(self, seeded_app):
        """`Everyone` has automatic membership — every account is in it by
        construction, so there is no audience to shape and nothing to add.
        The pane says what the group MEANS instead of listing the user table
        back at the reader."""
        body = self._body(seeded_app)
        assert "Every account" in body
        assert "every account on this instance" in body.lower()
        assert "nothing to add or remove" in body

    def test_google_managed_membership_is_read_only(self, seeded_app):
        """Workspace owns membership for synced groups and the API refuses
        writes on them, so the pane must not offer an add field that cannot
        work — it names where to do it instead."""
        body = self._body(seeded_app)
        assert "managed by Google Workspace" in body
        assert "admin.google.com" in body

    def test_the_pane_routes_to_the_owning_surfaces(self, seeded_app):
        body = self._body(seeded_app)
        assert "All people" in body


class TestTheGroupItself:
    """What the retired list and detail pages owned, and this pane had to
    absorb before either could go: the group's identity, its lifecycle, its
    full roster, and a way to find one item among everything grantable."""

    def _body(self, seeded_app) -> str:
        c = seeded_app["client"]
        return c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text

    def test_identity_lives_in_the_pane_header(self, seeded_app):
        """Name, upstream address, origin pill, description, created date —
        the detail page's header, which is most of why it existed."""
        body = self._body(seeded_app)
        for el_id in ("ax-what-title", "ax-what-idsub", "ax-what-origin",
                      "ax-what-meta", "ax-what-managed"):
            assert f'id="{el_id}"' in body, f"lost {el_id}"

    def test_rename_and_delete_are_here(self, seeded_app):
        """Without them the workspace could edit a group's people and its
        grants but not the group — which is why a second page had to exist.

        They live on the ROW, not in the pane header. A control in the header
        can only ever act on the selected group, so renaming any other one
        meant selecting it first — a navigation performed to reach an edit
        that has nothing to do with what the pane is showing."""
        body = self._body(seeded_app)
        assert "data-grename=" in body
        assert "data-gdelete=" in body
        assert "data-gmenu=" in body
        # ...and NOT in the pane header, where they used to be.
        assert 'id="ax-rename"' not in body
        assert 'id="ax-gmenu-btn"' not in body

    def test_the_row_menu_is_a_sibling_of_the_row_button(self, seeded_app):
        """A <button> inside a <button> is invalid, and browsers resolve it by
        breaking one of the two. The kebab is positioned over the row's right
        edge as a sibling instead, with the wrapper as its menu's anchor."""
        body = self._body(seeded_app)
        assert "ax-grow" in body
        assert "ax-gkebab" in body

    def test_only_editable_groups_get_a_row_menu(self, seeded_app):
        """`Admin` and `Everyone` are renamed and deleted where they are
        owned — in the seed, or in Workspace — so offering the menu on them
        would be offering two actions the API refuses."""
        body = self._body(seeded_app)
        assert "isEditable(g) ?" in body

    def test_the_row_menu_reveals_on_hover_without_trapping_clicks(self, seeded_app):
        """Same three-part reveal as the package page's remove control, and
        for the same reasons — dropping any one of them breaks a real input."""
        body = self._body(seeded_app)
        block = body.split(".ax-gkebab {", 1)[1].split(".ax-menu {", 1)[0]
        assert "@media (hover: hover)" in block
        assert "pointer-events: none" in block
        assert "focus-within" in block
        assert "opacity: 0" in block
        # An open menu keeps its trigger visible, or the menu floats with
        # nothing to have come from the moment the pointer moves away.
        assert 'aria-expanded="true"' in block

    def test_the_full_roster_is_the_people_section(self, seeded_app):
        """Search answers "is Maria in Finance?"; only a roster answers "who
        IS in Finance, and how did each of them get there?".

        It is the section BODY, not a disclosure inside it. The count used to
        be drawn three times over — a sentence, a row of avatars, and a
        "Show all N" label — none of which was the list itself; it is now the
        section head's summary, once."""
        body = self._body(seeded_app)
        assert 'id="ax-sec-people"' in body
        assert 'id="ax-people-sum"' in body
        assert "How they got here" in body
        # The avatar row and its disclosure are gone, not merely collapsed.
        assert "ax-faces" not in body
        assert "Show all " not in body
        # The per-member origin the detail page's Origin column carried — and
        # the rule deciding whether Remove is offered at all.
        for label in ("added by admin", "synced from Google", "system-managed"):
            assert label in body, f"roster lost the {label!r} origin"

    def test_adding_is_one_act_not_a_tree_to_browse(self, seeded_app):
        """Replaces `test_the_grant_tree_is_nested_by_block`.

        The nested bucket tree (and the Advanced tree it later became) was
        the page's way to grant: browse every grantable resource, tick rows
        in place. The open group now shows only what the group HAS, and
        adding is one deliberate act — a + Add row opening a picker of
        everything it does not have, chosen and applied together.

        Capability deliberately dropped with the tree: the tri-state bucket
        checkbox that granted a whole bucket in one click. The picker takes
        many selections at once, but there is no select-all-in-bucket. If
        that is missed, it belongs in the picker, not in a second tree.
        """
        body = self._body(seeded_app)
        assert "data-add-grant" in body          # the one way in, per group
        assert "openPicker" in body              # …opens the picker
        assert "data-share-bundle" in body       # and the same act by bundle
        assert "openBundlePicker" in body
        # The tree and its bulk control are gone, not hidden.
        assert "data-bucket=" not in body
        assert 'class="ax-blk"' not in body

    def test_finding_one_row_among_hundreds_still_works(self, seeded_app):
        """Replaces `test_the_grant_tree_can_be_filtered`.

        "Give Finance the revenue package" still has to mean finding one row
        among hundreds — the capability the per-group filter existed for.
        Two controls carry it now, and neither is a second search box beside
        the first: the header search narrows the group list *and* what an
        open group shows, and the picker has its own search over everything
        not yet granted.
        """
        body = self._body(seeded_app)
        assert 'id="ax-group-find"' in body       # the one page-level search
        assert "Search everything grantable" in body   # the picker's own
        # The per-group input is gone: two search boxes on one surface, one
        # of them hidden, was the ambiguity this collapse removed.
        assert 'id="ax-rfind"' not in body

    def test_the_resource_deep_link_still_lands(self, seeded_app):
        """/admin/tables' per-row Manage-access sent `#table:<id>` to the
        group list. The workspace speaks `?resource=` and rewrites the old
        hash, so an existing bookmark lands filtered rather than ignored."""
        body = self._body(seeded_app)
        assert 'id="ax-pick"' in body
        assert '"resource"' in body
        assert "#table:" in body

    def test_the_group_rows_carry_the_retired_columns(self, seeded_app):
        """Origin, member count and grant count were the list table's
        scannable columns. They ride the selector rows now."""
        body = self._body(seeded_app)
        assert "ax-orig--" in body
        assert "grant_count" in body and "member_count" in body
