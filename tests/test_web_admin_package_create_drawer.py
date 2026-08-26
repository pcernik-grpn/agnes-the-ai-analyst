"""Creating a Data Package: one shared right-side drawer, opened in place.

Three things had gone wrong with the create form, and all three are the same
mistake — the form was page furniture on /admin/tables rather than a component:

  * it was a centred modal carrying the tallest form in the admin surface
    (name, slug, description, lifecycle, category, icon, colour, cover image
    AND a group-access matrix), so on a laptop its footer sat over its own
    last field. Setup flows in this product come from the right;
  * the Packages lens — where "+ New package" belongs — had no copy of it, so
    its button LINKED to `/admin/tables?new_package=1`: asking for a new
    package on the Data packages tab switched you to the Tables tab;
  * the native `<select>`, colour input and file input inside it were
    undressed browser chrome next to fields that were not.

What this suite pins:

  * the Packages lens opens the drawer in place — a button, not a link out;
  * /admin/tables no longer carries the create MARKUP, only the component and
    the two entry points that name it (`?new_package=1` from the legacy
    catalog CTA, and the chip-input's `chip-create`);
  * both consumers link the shared drawer chrome AND the component, since a
    consumer that links one without the other renders an unstyled form or no
    form at all;
  * the controls the shared field rules do not cover are dressed in the shared
    sheet (`.fbar-select`, `.ds-drawer__color`, `.ds-drawer__file`), not left
    native and not re-invented per page.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES = Path("app/web/templates")
STATIC = Path("app/web/static")

COMPONENT = STATIC / "js" / "components" / "package_drawer.js"
DRAWER_CSS = STATIC / "css" / "drawer.css"
COMPONENT_CSS = STATIC / "css" / "package_drawer.css"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestTheComponentExists:
    def test_the_flow_is_a_component_not_a_page_script(self) -> None:
        assert COMPONENT.exists(), "the create-package flow must live in js/components/package_drawer.js"
        assert COMPONENT_CSS.exists(), "the component's own two objects belong in css/package_drawer.css"
        src = COMPONENT.read_text(encoding="utf-8")
        # The public surface every consumer calls.
        assert "window.AgnesPackageDrawer = { open: open, close: close };" in src
        # The shared drawer chrome, not a card of its own vocabulary.
        assert "root.className = 'ds-drawer';" in src
        assert "ds-drawer__panel" in src and "ds-drawer__foot" in src
        assert "modal-overlay" not in src

    def test_the_create_response_carries_only_an_id_so_the_name_rides_along(self) -> None:
        """`POST /api/admin/data-packages` answers `{id}` and nothing else.

        A callback reading `pkg.name` off that body gets `undefined` — which is
        what the chip and the toast used to show ('Data Package "undefined"
        created'). The component must hand the caller what it SENT plus the id.
        """
        src = COMPONENT.read_text(encoding="utf-8")
        assert "var pkg = { id: created.id, name: name, slug: slug };" in src

    def test_the_undressed_controls_are_dressed_in_the_shared_sheet(self) -> None:
        css = DRAWER_CSS.read_text(encoding="utf-8")
        for selector in (".ds-drawer__color", ".ds-drawer__file", ".ds-drawer__disclose", ".ds-drawer__row"):
            assert selector in css, f"{selector} must live in the shared drawer sheet"
        # `::file-selector-button` is the ONLY part of a file input that can be
        # styled; without it the control is a native grey button beside fields
        # that are not.
        assert "::file-selector-button" in css
        src = COMPONENT.read_text(encoding="utf-8")
        # The select borrows the product's own select chrome rather than a
        # drawer-local copy of it.
        assert "fbar-select" in src
        # …and the access tier borrows the product's segmented control.
        assert "fbar-seg" in src


class TestThePackagesLensOpensItInPlace:
    def test_new_package_is_a_button_not_a_link_to_another_lens(self, seeded_app) -> None:
        c = seeded_app["client"]
        html = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert "data-adp-new-package>+ New package</button>" in " ".join(html.split())
        # The bug this replaced: creating a package moved you to the Tables lens.
        assert "/admin/tables?new_package=1" not in html

    def test_the_empty_state_opens_the_same_drawer(self, seeded_app) -> None:
        """An instance with no packages is exactly where "+ New package" matters
        most, and its CTA was the same cross-lens link."""
        src = (TEMPLATES / "admin_data_packages.html").read_text(encoding="utf-8")
        empty_block = src[src.index("No Data Packages yet") : src.index("Memory Domains")]
        assert "data-adp-new-package" in empty_block
        assert "new_package=1" not in empty_block

    def test_the_lens_links_the_chrome_and_the_component(self, seeded_app) -> None:
        c = seeded_app["client"]
        html = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert "css/drawer.css" in html
        assert "css/package_drawer.css" in html
        assert "js/components/package_drawer.js" in html

    def test_suggest_by_bucket_still_crosses_deliberately(self, seeded_app) -> None:
        """Not every cross-lens link was a slip: that flow reads the table
        registry and assigns rows, so the table list IS its subject."""
        c = seeded_app["client"]
        html = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert "/admin/tables?group_by_bucket=1" in html


class TestTheTablesLensKeepsTheEntryPointsAndDropsTheMarkup:
    def test_the_modal_markup_is_gone(self, seeded_app) -> None:
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
        assert 'id="createDataPackageModal"' not in html
        # Every field id of the old form went with it — a leftover would be a
        # second, invisible copy of the same inputs.
        for field in ("cdp-name", "cdp-slug", "cdp-status", "cdp-color", "cdp-rbac-rows"):
            assert f'id="{field}"' not in html, field

    def test_the_component_and_its_two_entry_points_stay(self, seeded_app) -> None:
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
        assert "js/components/package_drawer.js" in html
        assert "css/package_drawer.css" in html
        # `?new_package=1` — the legacy catalog CTA lands here with it.
        assert "new_package" in html
        # The chip-input's "+ Create new" tail row, which must keep the host so
        # the new package comes back as a chip on the table being edited.
        assert "chip-create" in html
        assert "chipHost: host" in html


class TestThePackagePageEditsItsOwnPackage:
    """The package's own page could not edit the package.

    `/admin/data-packages/{id}` is the surface whose whole docstring is "ONE
    package, end to end", yet renaming it, re-describing it or retiring it
    meant following an "Edit details…" LINK to `/admin/tables?edit_package=`
    — the Tables lens, a page about something else — where a legacy centred
    modal did the work. The drawer that already owns *creating* a package now
    owns editing one too, so the verb happens on the object you are standing
    on, exactly as create already does.

    The legacy modal on /admin/tables is deliberately NOT retired here: it is
    still the only surface carrying composition + the RBAC matrix, and
    `?edit_package=` still routes to it. This pins the new door, not the old
    one's removal.
    """

    def test_the_component_carries_an_edit_mode(self) -> None:
        src = COMPONENT.read_text()
        assert "'edit'" in src, "the drawer must branch on an edit mode"
        assert "PUT" in src, "editing writes through PUT /api/admin/data-packages/{id}"

    def test_the_slug_is_not_editable_when_editing(self) -> None:
        """The slug is permanent — it is in URLs and in grants, and `PUT`
        carries no slug field, so an editable box would silently discard what
        the admin typed. The legacy modal disabled it for the same reason."""
        src = COMPONENT.read_text()
        assert "slug.disabled" in src

    def test_the_package_page_opens_the_drawer_instead_of_linking_out(self, seeded_app) -> None:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        pkg_id = seeded_app_package_id(seeded_app)
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(token)).text
        assert "/admin/tables?edit_package=" not in html, (
            "the package page must edit in place, not send the admin to the Tables lens"
        )
        assert "js/components/package_drawer.js" in html
        assert "css/package_drawer.css" in html
        assert "css/drawer.css" in html


def seeded_app_package_id(seeded_app) -> str:
    """A package id for the id-keyed detail route.

    Created through the public admin API rather than seeded in the fixture, so
    the test owns its own subject and cannot start passing (or failing) because
    some other suite changed what the fixture happens to contain.
    """
    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])
    rows = c.get("/api/admin/data-packages", headers=auth).json()
    if rows:
        return rows[0]["id"]
    created = c.post(
        "/api/admin/data-packages",
        headers=auth,
        json={"name": "Edit drawer subject", "slug": "edit-drawer-subject"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


class TestTheEditDrawerAlsoOwnsComposition:
    """Editing a package from the Library must include what is IN it.

    The first cut of edit mode carried metadata only, on the reasoning that
    composition belonged behind the "Manage in Admin" door. That split the one
    question an admin actually has open — "is this package right?" — across two
    surfaces, so the drawer now owns membership too. BOTH modes do: create was
    excluded on the reasoning that a package with no id yet has nothing to
    attach a table to, but the group picks in the same form are collected
    before the id exists and applied once the POST answers. So the id was never
    the obstacle — and a create that could not choose tables produced an empty
    package and sent the admin to a second surface to fill it, which is the
    split this drawer exists to close.

    Membership is DIFFED and applied on Save rather than written per tick,
    because the drawer offers Cancel and a click that had already hit the API
    would make that button a lie.
    """

    def test_composition_is_in_the_drawer(self) -> None:
        src = COMPONENT.read_text()
        assert "pdw-tables" in src, "the drawer needs a table list"
        assert "/tables" in src, "membership writes through the package tables endpoint"
        assert "'DELETE'" in src, "and removes through it too"

    def test_the_table_list_is_offered_in_both_modes(self) -> None:
        """The picker used to be hidden on a create (`tablesField.hidden =
        !editing`). It is not any more: the field carries no `hidden` attribute
        and nothing re-hides it per mode, so choosing tables is part of making
        a package rather than a second trip to another surface."""
        src = COMPONENT.read_text()
        assert "tablesField.hidden" not in src, (
            "the create/edit split on the table picker is what this change removed"
        )
        assert '<div class="ds-drawer__field" id="pdw-tables-field">' in src, (
            "the field must not ship hidden either"
        )

    def test_create_hydrates_the_registry_before_the_package_exists(self) -> None:
        """A create has no package to read members from, so `hydrateTables` is
        called with a null id and resolves an empty member set — the registry
        and the project-name lookup are the same two fetches either way."""
        src = COMPONENT.read_text()
        assert "hydrateTables(null)" in src
        assert "Promise.resolve({ tables: [] })" in src

    def test_create_applies_membership_after_the_id_exists(self) -> None:
        """Ticks are collected before the POST and written after it answers —
        the same collect-then-apply order the group picks already used. The
        snapshot matters: `st` belongs to the open drawer and the writes land
        after it closes."""
        src = COMPONENT.read_text()
        assert "Array.from(st.tablesSelected)" in src, "the ticked set must be snapshotted"
        assert "tableIds.map(" in src, "and applied once there is an id"

    def test_grant_and_table_failures_are_counted_separately(self) -> None:
        """One merged number would be a worse message than either: a failed
        grant and a failed table send the admin to different places."""
        src = COMPONENT.read_text()
        assert "done(pkg, failures, tableFailures)" in src
        caller = (TEMPLATES / "admin_tables.html").read_text(encoding="utf-8")
        assert "table(s) not added" in caller, "the only caller that shows the count must name it"

    def test_membership_is_applied_on_save_not_on_tick(self) -> None:
        src = COMPONENT.read_text()
        assert "tablesOriginal" in src and "tablesSelected" in src, (
            "a diff needs both the loaded set and the edited one"
        )

    def test_a_failed_table_change_does_not_report_a_failed_save(self) -> None:
        """The metadata PUT has already succeeded by the time membership is
        applied, so a failure there must name what did not happen rather than
        claim the save failed."""
        src = COMPONENT.read_text()
        assert "The other details were saved." in src


class TestTheTableListFollowsTheSourceStructure:
    """~500 tables across a handful of projects and a hundred-odd buckets.

    A package is almost never "these three arbitrary tables" — it is "this
    bucket" or "everything from that project", because that is how the source
    systems are organised. A flat list made the admin tick that structure back
    in by hand, one row at a time.

    Two levels, PROJECT › BUCKET, and both carry a tri-state box. Neither is an
    Agnes concept: a project is a source connection and a bucket is its own
    grouping inside it, which is exactly why the list says so in as many words
    (the package is the Agnes container).
    """

    def test_the_list_groups_by_project_then_bucket(self) -> None:
        src = COMPONENT.read_text()
        assert "pdw-grp--project" in src and "pdw-grp--bucket" in src
        assert 'data-group="project"' in src and 'data-group="bucket"' in src

    def test_a_group_box_is_tri_state(self) -> None:
        """all / some / none. `indeterminate` is a property with no HTML
        attribute, so it cannot ride the markup and must be set after paint —
        a group that reads 'all' with one child unticked is a lie."""
        src = COMPONENT.read_text()
        assert "function tallyState" in src
        assert "'some'" in src and "b.indeterminate = true;" in src

    def test_ticking_a_partly_selected_group_selects_the_rest(self) -> None:
        """`indeterminate` reads as unchecked to `.checked`, so a click on a
        partly-selected group must mean "select the rest", never "clear it"."""
        src = COMPONENT.read_text()
        assert "var want = box.checked;" in src

    def test_a_group_tick_honours_the_search(self) -> None:
        """`tablesUnder` reads the FILTERED groups, so ticking a group while a
        search is active takes what the reader can see under it, not the whole
        registry."""
        src = COMPONENT.read_text()
        assert "function tablesUnder" in src and "tableGroups().forEach" in src

    def test_project_headings_are_names_not_enum_values(self) -> None:
        """A raw `bigquery` beside a real project's name reads as leaked data.
        Tables with no source CONNECTION group under their source's display
        name instead."""
        src = COMPONENT.read_text()
        assert "SOURCE_LABELS" in src
        assert "'/api/admin/source-connections'" in src, "project names come from the connections"
