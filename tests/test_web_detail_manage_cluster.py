"""The governance cluster on a resource detail page (`detail.manage`).

ONE way for a detail page to offer the actions that manage the thing it shows,
replacing four idioms that had grown across these pages: an overflow-menu item
on the data package, a bare `edit_icon` on the same page's legacy path, an
inline "Edit · admin-only" text link per row on the memory domain, and a
status-conditional block on the library file.

What this suite pins — the two rules the shape exists to encode:

  * **Object-scoped only.** The cluster manages the thing you are standing on.
    The one door that leaves for instance-scoped work is `Manage in Admin`,
    and it is the last thing in the block.
  * **It never outranks the reader.** A caller who can manage this arrived to
    READ it, so the hero's primary action stays the reading verb and the
    cluster is quiet secondary chrome in the rail.

And the boundary that makes it safe: a reader who cannot manage the resource
gets none of it — not the block, and not the editor component behind it.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES = Path("app/web/templates")

MACROS = TEMPLATES / "macros" / "_detail.html"
PACKAGE_DETAIL = TEMPLATES / "catalog_package_detail.html"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _a_package_slug(seeded_app) -> str:
    """A package the ANALYST can open, created through the admin API.

    Granted to ``Everyone`` deliberately. Without a grant the analyst gets 403
    from `/catalog/p/{slug}` and the "an analyst sees none of this" test would
    pass by never rendering the page at all — proving the route's RBAC gate,
    which is not what it is for. The grant makes the analyst a legitimate
    READER of the package, which is the only state in which "does the
    management block leak to them" is a real question.
    """
    from src.repositories import user_group_members_repo, user_groups_repo

    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])
    slug = "manage-cluster-subject"
    created = c.post(
        "/api/admin/data-packages",
        headers=auth,
        json={"name": "Manage cluster subject", "slug": slug},
    )
    if created.status_code == 201:
        pkg_id = created.json()["id"]
        everyone = user_groups_repo().get_by_name("Everyone")
        assert everyone, "the Everyone system group must exist"
        # The fixture mints an analyst TOKEN but never enrols the identity in a
        # group (it seeds only admin1, into Admin), so `analyst1` belongs to
        # nothing and every grant-gated page 403s for them. Enrol them the same
        # way conftest enrols the admin — directly through the repo.
        members = user_group_members_repo()
        if not members.has_membership("analyst1", everyone["id"]):
            members.add_member("analyst1", everyone["id"], source="test")
        grant = c.post(
            "/api/admin/grants",
            headers=auth,
            json={
                "group_id": everyone["id"],
                "resource_type": "data_package",
                "resource_id": pkg_id,
                "requirement": "available",
            },
        )
        assert grant.status_code in (200, 201, 409), grant.text
    return slug


class TestTheMacroIsTheOneIdiom:
    def test_the_macro_exists_and_carries_the_admin_door(self) -> None:
        src = MACROS.read_text()
        assert "{% macro manage(" in src
        assert "detail-manage__door" in src, "the cluster needs exactly one door out to Admin"
        assert "detail-manage__key" in src, "the cluster must name whose authority it is"

    def test_the_package_page_no_longer_carries_the_retired_idioms(self) -> None:
        """The admin errand was an item in the READER's overflow menu pointing
        at the Tables lens. Both the menu item and the legacy `edit_icon` on
        the redesign path are gone; the rail block replaces them."""
        src = PACKAGE_DETAIL.read_text()
        assert "'label': 'Edit package metadata'" not in src
        assert "detail.manage(" in src, "the cluster is how this page offers management now"


class TestOnlyACallerWhoCanManageSeesIt:
    def test_an_admin_gets_the_cluster_and_the_editor(self, seeded_app) -> None:
        c = seeded_app["client"]
        slug = _a_package_slug(seeded_app)
        html = c.get(f"/catalog/p/{slug}", headers=_auth(seeded_app["admin_token"])).text
        assert "data-manage" in html
        assert 'id="pkg-edit-btn"' in html
        assert "js/components/package_drawer.js" in html
        assert "css/package_drawer.css" in html

    def test_an_analyst_gets_neither(self, seeded_app) -> None:
        """Not just the block: the editor component and its sheet must not be
        served either. A reader who cannot manage the package should never
        download the form for doing so."""
        c = seeded_app["client"]
        slug = _a_package_slug(seeded_app)
        r = c.get(f"/catalog/p/{slug}", headers=_auth(seeded_app["analyst_token"]))
        # Must be 200: the analyst is a granted reader of this package (see
        # `_a_package_slug`). A 403 here would mean the test is asserting
        # against an error page rather than the rendered detail page.
        assert r.status_code == 200, f"the analyst must be able to READ it: {r.status_code}"
        html = r.text
        assert "data-manage" not in html
        assert 'id="pkg-edit-btn"' not in html
        assert "js/components/package_drawer.js" not in html
        assert "css/package_drawer.css" not in html


class TestTheAdminErrandDoesNotLeaveForTheTablesLens:
    def test_no_page_sends_a_reader_to_edit_package_on_the_tables_lens(self, seeded_app) -> None:
        """`/admin/tables?edit_package=` is still a live entry point on the
        Tables lens itself — this pins that no OTHER page uses it as its answer
        to "edit this package", which is what made the errand a teleport."""
        c = seeded_app["client"]
        slug = _a_package_slug(seeded_app)
        auth = _auth(seeded_app["admin_token"])
        for url in (f"/catalog/p/{slug}",):
            html = c.get(url, headers=auth).text
            assert "/admin/tables?edit_package=" not in html, url
