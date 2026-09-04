"""Who owns a SharePoint collection's grant, and whether a revoke can hold.

Reported from a dev instance: SharePoint-derived collections on
/admin/access offered **Revoke** and a link away to the collection's Library
page, and an admin could not manage them from the page whose job that is.

Reading the two writers showed the classification was inverted.

**The wizard** (`app/api/admin_sharepoint.py::confirm_scope`) rewrites a
scope's collection grants ONLY when an admin re-submits that scope's form
with a group list. There is no scheduled pass behind it, so an admin's
revoke stands until someone deliberately reopens the wizard — ordinary
last-writer-wins. It was nonetheless marked `revocable=False`.

**The ACL sync** (`connectors/sharepoint/acl_sync.py`) is the one that runs
on a schedule and recomputes grants from SharePoint's own permissions, so a
row removed on the page really does come back. It recorded no source at all,
which filed its rows under "change here" — the Revoke that undid itself.

So the fix is not "make SharePoint grants revocable": it is to tell the two
writers apart, which is what `resource_grants.source` exists for.
"""

from __future__ import annotations

from src.grant_sources import describe, section_for


class TestTheWizardsGrantsCanBeManagedHere:
    def test_a_wizard_grant_is_revocable(self):
        d = describe("sharepoint_wizard")
        assert d is not None
        assert d["revocable"] is True

    def test_it_lands_in_the_actionable_section(self):
        assert section_for("sharepoint_wizard") == "change_here"

    def test_and_says_what_would_overwrite_it(self):
        """Revocable is not the same as unowned. The row still has to warn
        that re-submitting the scope in the wizard replaces what you set."""
        d = describe("sharepoint_wizard")
        assert "wizard" in d["reason"].lower()
        assert "overwrite" in d["reason"].lower()


class TestTheAclSyncsGrantsCannotBe:
    def test_a_mirrored_grant_is_not_revocable(self):
        d = describe("sharepoint_acl_sync")
        assert d is not None
        assert d["revocable"] is False

    def test_it_lands_in_set_elsewhere(self):
        assert section_for("sharepoint_acl_sync") == "set_elsewhere"

    def test_it_points_at_the_surface_that_owns_it(self):
        d = describe("sharepoint_acl_sync")
        assert d["surface"] == "Data sources"
        assert d["href"] == "/admin/data-sources"

    def test_and_names_the_real_way_to_change_it(self):
        """Stopping the mirroring is `access_mode` on the scope, not a
        revoke on one row (spec §2.3) — so the sentence has to send the
        admin at the scope rather than at this row."""
        reason = describe("sharepoint_acl_sync")["reason"].lower()
        assert "mirrored" in reason
        assert "data sources" in reason


class TestTheSyncStampsAndAdoptsItsOwnRows:
    """A classification the writer never records cannot be acted on."""

    def test_the_sync_has_a_source_of_its_own(self):
        from connectors.sharepoint.acl_sync import ACL_SYNC_GRANT_SOURCE

        assert ACL_SYNC_GRANT_SOURCE == "sharepoint_acl_sync"
        assert describe(ACL_SYNC_GRANT_SOURCE) is not None

    def test_the_source_is_distinct_from_the_sentinel(self):
        """The sentinel identifies rows to the sync itself; the source tells
        the Access page what it may offer. Collapsing them would make one
        rename break the other."""
        from connectors.sharepoint.acl_sync import ACL_SYNC_GRANT_SOURCE, ACL_SYNC_SENTINEL

        assert ACL_SYNC_SENTINEL != ACL_SYNC_GRANT_SOURCE

    def test_new_grants_are_written_with_the_source(self):
        from pathlib import Path

        src = Path("connectors/sharepoint/acl_sync.py").read_text(encoding="utf-8")
        block = src[src.index("for group_id in target_set - current_group_ids:") :][:900]
        assert "source=ACL_SYNC_GRANT_SOURCE" in block

    def test_existing_grants_are_adopted_rather_than_left_wrong(self):
        """Without this the fix reaches only grants created from here on,
        and every mirrored collection already on an instance keeps drawing a
        Revoke the next sync undoes."""
        from pathlib import Path

        src = Path("connectors/sharepoint/acl_sync.py").read_text(encoding="utf-8")
        assert "adopt_source(g[\"id\"], ACL_SYNC_GRANT_SOURCE)" in src
        assert 'if not g.get("source")' in src
