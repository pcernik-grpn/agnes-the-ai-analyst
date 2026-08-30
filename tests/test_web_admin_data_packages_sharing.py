"""The Packages workspace (`GET /admin/data-packages`) — sharing STATED on
each card, plus the unpackaged-tables tray.

"Who can use this package?" had no answer anywhere in the product until this
page grew one: grants were written only from a group's Access tab. The answer
stays; the EDITOR moved to the package's own page. A grant is consequential —
`Automatic` puts a package in every member's workspace on their next pull —
and an index card shows none of its consequences, while the detail page sets
it right beside the delivery read-out ("14 people get this, 11 have pulled
it") that answers for it.

What this suite pins:

  * each ROW states who can use the package, from the same `resource_grants`
    rows the group-side editor writes, and names the tier when one is
    Automatic;
  * an ungranted package says so out loud ("Not shared"), because that state
    is the one that strands analysts and was previously invisible;
  * the row carries no sharing CONTROL — no grants endpoint, no editor — and
    points at the page that does;
  * the packages render as the shared admin `.data-table`, the same object
    People, Tables and Sources use. They were cards, and the questions this
    page answers are comparative — which packages carry nothing, which are
    shared with nobody, which are Automatic for someone. A card puts each
    package in its own box, so answering any of those means reading N boxes
    and holding the answers in your head; a column answers it down the page
    in one pass;
  * the unpackaged tray applies the same distributable fold as the /admin
    gap card (blank → local; `remote` excluded);
  * the audit contract this page already had is untouched — every package
    renders regardless of grant.
"""

from __future__ import annotations

import re
import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mk_pkg(slug_prefix: str, name: str) -> str:
    from src.repositories import data_packages_repo

    return data_packages_repo().create(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:6]}",
        name=name,
        description="",
        icon=None,
        color=None,
        created_by="test",
    )


def _stock(pkg_id: str) -> str:
    """Register a table and put it in the package; returns the table id.

    A package with nothing in it renders the **No tables** alarm in the
    footer's action slot — deliberately, since an empty package delivers
    nothing however it is shared — so any test about the TIER has to give the
    package something to deliver first.
    """
    from src.repositories import data_packages_repo, table_registry_repo

    tid = f"pkgtbl-{uuid.uuid4().hex[:6]}"
    table_registry_repo().register(
        id=tid,
        name=f"pkg_table_{tid[-6:]}",
        source_type="keboola",
        bucket="in.c-pkg",
        source_table="pkg",
        query_mode="local",
    )
    data_packages_repo().add_table(pkg_id, tid, added_by="test")
    return tid


def _row_of(body: str, pkg_id: str) -> str:
    """The rendered table row for one package.

    Anchored on the row's own `data-pkg-id` hook rather than on a name match,
    so a slice can never accidentally span into a neighbour's markup.
    """
    start = body.index(f'data-pkg-id="{pkg_id}"')
    end = body.find("</tr>", start)
    assert end > start, "package row is not a closed <tr> — did the table markup change?"
    return body[start:end]


class TestSharingReadOut:
    def test_ungranted_package_says_it_is_not_shared(self, seeded_app):
        """The state that strands analysts: registered, bundled, and visible
        to nobody. It has to be readable from across a grid of forty."""
        pkg_id = _mk_pkg("share-none", "Share None Pkg")
        c = seeded_app["client"]
        body = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        row = _row_of(body, pkg_id)
        assert "Not shared" in row

    def test_granted_package_names_the_group_and_tier(self, seeded_app):
        from src.repositories import resource_grants_repo, user_groups_repo

        pkg_id = _mk_pkg("share-tier", "Share Tier Pkg")
        tid = _stock(pkg_id)
        everyone = next(g for g in user_groups_repo().list_all() if g.get("is_system") and g["name"] == "Everyone")
        grants = resource_grants_repo()
        gid = grants.create(
            group_id=everyone["id"],
            resource_type="data_package",
            resource_id=pkg_id,
            requirement="required",
        )
        try:
            c = seeded_app["client"]
            body = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
            row = _row_of(body, pkg_id)
            # A single grant is NAMED (a count of one says less than the name);
            # the tier is worded Automatic, with the API's own word in the
            # title attribute so the CLI/API vocabulary stays learnable.
            assert "Everyone" in row
            assert "Automatic" in row
            assert "required" in row
            assert "Not shared" not in row
        finally:
            from src.repositories import data_packages_repo, table_registry_repo

            grants.delete(gid)
            # Membership first: `table_registry` is the FK parent of the
            # package junction, so unregistering a table still in a package
            # raises rather than cascading.
            data_packages_repo().remove_table(pkg_id, tid)
            table_registry_repo().unregister(tid)

    def test_the_card_carries_no_sharing_control(self, seeded_app):
        """Sharing is STATED here and EDITED on the package's own page, next
        to the delivery read-out that says what a grant actually costs. The
        index must therefore ship no grants endpoint and no editor."""
        c = seeded_app["client"]
        body = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert "/api/admin/grants" not in body
        assert "adp-share-modal" not in body
        assert "Share…" not in body


class TestTheRowIsTheSharedAdminTable:
    def test_packages_render_the_shared_admin_table(self, seeded_app):
        """One table component across the admin: this is an index of every
        package on the instance, and People, Tables and Sources — the other
        three indexes — are all `.data-table`.

        They were `.fbar-card` grids, on the argument that a package the admin
        publishes and a package an analyst receives should look like one
        object. That argument was about the analyst's Library, which is itself
        a table; the cards matched a projection of it rather than the thing.
        What an index has to support is comparison down a column, which a grid
        of boxes cannot do at any size.
        """
        pkg_id = _mk_pkg("card-shape", "Card Shape Pkg")
        c = seeded_app["client"]
        body = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert 'class="data-table adp-table"' in body, "packages are not on the shared admin table"
        row = _row_of(body, pkg_id)
        assert "<td>" in row, "the row has no cells"
        # …and NOT either card it replaced.
        assert "stack-card__photo" not in body
        assert "fbar-card__foot" not in body, "a card footer is still being rendered"

    def test_the_table_gives_what_is_in_the_package_its_own_column(self, seeded_app):
        """An admin scanning packages needs the count first — it is the fact
        that says whether the package delivers anything at all.

        On a card that meant "lead the meta line with it"; in a table it gets a
        column, which is strictly better for the same reason the table is: the
        counts line up, so an empty package is visible without reading any of
        the names beside it.
        """
        pkg_id = _mk_pkg("card-meta", "Card Meta Pkg")
        c = seeded_app["client"]
        body = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert "<th>Tables</th>" in body or ">Tables</th>" in body
        row = _row_of(body, pkg_id)
        # An empty package delivers nothing however it is shared, so the count
        # cell raises it rather than printing a bare 0…
        assert "adp-chip--warn" in row
        assert "/admin/tables?assign_to=" in row, "the alarm is not a door to the fix"
        # …and the Access cell shows no tier at all, rather than one that would
        # promise delivery this package cannot make.
        assert "Automatic" not in row and "Optional" not in row


class TestUnpackagedTray:
    """The unpackaged pile, now stated as the shared `.apg-strip--warn` line
    every Data lens uses for a standing fact worth acting on.

    It used to be a page-local dashed tray listing up to 24 table NAMES, which
    is a sample rather than a list on an instance with 450 of them; the strip's
    own link lands on all of them in the Tables lens, filtered. So these guards
    read the COUNT the strip states, not the names it no longer prints.
    """

    @staticmethod
    def _tray_count(client, token) -> int:
        body = client.get("/admin/data-packages", headers=_auth(token)).text
        m = re.search(r"<strong>(\d+) tables? in no package</strong>", body)
        return int(m.group(1)) if m else 0

    def test_distributable_unpackaged_table_lands_in_the_tray(self, seeded_app):
        from src.repositories import table_registry_repo

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        before = self._tray_count(c, token)

        repo = table_registry_repo()
        tid = f"tray-{uuid.uuid4().hex[:6]}"
        repo.register(
            id=tid,
            name=f"tray_table_{tid[-6:]}",
            source_type="keboola",
            bucket="in.c-test",
            source_table="tray",
            query_mode="local",
        )
        try:
            body = c.get("/admin/data-packages", headers=_auth(token)).text
            assert "in no package" in body
            # The strip is the shared object, and it carries the way out.
            assert 'class="apg-strip apg-strip--warn"' in body
            assert "/admin/tables?unpackaged=1" in body
            assert self._tray_count(c, token) == before + 1
        finally:
            repo.unregister(tid)

    def test_remote_tables_do_not_raise_the_tray_alarm(self, seeded_app):
        """`remote` rows answer server-side without a package — counting them
        as 'nobody can pull them' would be a standing false alarm."""
        from src.repositories import table_registry_repo

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        before = self._tray_count(c, token)

        repo = table_registry_repo()
        tid = f"tray-remote-{uuid.uuid4().hex[:6]}"
        repo.register(
            id=tid,
            name=f"tray_remote_{tid[-6:]}",
            source_type="bigquery",
            bucket="ds",
            source_table="remote",
            query_mode="remote",
        )
        try:
            assert self._tray_count(c, token) == before
        finally:
            repo.unregister(tid)
