"""`/admin/access` stops offering `table`, and keeps the rows already written.

Observed, not reasoned. The picker listed every registered table among 239
grantable "knowledge" items, and a production group's own list showed page
after page of `table` rows inherited from `Everyone`, each rendered with the
label *"reached through a package"*. An admin ticked those believing they
granted data.

(An earlier version of this docstring said "~86 table grants". That number
was inferred from a "86 via Everyone" badge, which counts inherited grants
of EVERY type — the tables are a subset of it, and nobody has counted them.
`scripts/audit_table_grants.py` reports the real figure.)

They granted nothing. `src/rbac.py::can_access_table` resolves the caller's
data packages, returns False outright if they have none, then intersects
those with the packages containing the table — a `resource_grants` row on the
table itself is read nowhere in that path. So the picker was writing rows
nothing reads, in a slot that implied it was granting access.

The effort's ticket 11 originally answered "tables stay, and say what they
do"; the screenshots reopened and reversed it. See
`docs/superpowers/maps/access-who-can-see-what/issues/11-*`.

The second test is the half that is easy to get wrong: only the OFFER goes.
Hiding existing rows too would leave the ones already written invisible AND
unremovable, which is worse than the crowding.
"""

from __future__ import annotations


def test_table_is_not_offered_in_the_access_picker():
    from app.resource_types import ResourceType, enabled_resource_types

    offered = {s.key for s in enabled_resource_types() if s.offered_on_access}
    assert ResourceType.TABLE not in offered, (
        "the picker is offering `table` again — ticking one grants no analyst "
        "access, so the row it writes is never read"
    )
    # Everything else still is. A blanket filter that quietly dropped a second
    # type would pass the assertion above and break the page.
    assert ResourceType.DATA_PACKAGE in offered, (
        "data packages ARE how an analyst reaches a table; they must stay offered"
    )
    assert len(offered) == len(enabled_resource_types()) - 1, (
        "exactly one type should be withheld from the picker"
    )


def test_table_stays_a_registered_enforceable_type():
    """Withheld from the picker is not removed from the model.

    A `table` grant is still enforced (it is the union in
    `src/agent_scope_intersection.py`), still revocable, and still a valid
    `ResourceType`. Only the offer is gone.
    """
    from app.resource_types import RESOURCE_TYPES, ResourceType, is_resource_type_enabled

    assert ResourceType.TABLE in RESOURCE_TYPES
    assert is_resource_type_enabled(ResourceType.TABLE), (
        "`table` must stay ENABLED — disabling it would refuse writes at the API "
        "and strand the grants that already exist"
    )
    spec = RESOURCE_TYPES[ResourceType.TABLE]
    assert spec.offered_on_access is False
    assert callable(spec.list_blocks), (
        "the projection delegate stays, so any surface that wants to list tables "
        "on purpose still can"
    )


def test_the_overview_projection_omits_tables_but_keeps_table_grants(monkeypatch):
    """End-to-end on the shape the page consumes.

    `resources` drives the picker and must not contain tables. `grants` is
    every row in the table and must still contain an existing table grant —
    that is what keeps the existing ones cleanable.
    """
    import app.api.access as access_mod

    fake_grants = [
        {
            "id": "g-table",
            "group_id": "grp-1",
            "resource_type": "table",
            "resource_id": "in.c-crm.opportunities",
            "requirement": "available",
            "assigned_at": None,
            "assigned_by": "admin@x",
            "source": None,
            "scope": None,
            "group_name": "Delivery",
        }
    ]

    class _Grants:
        def list_all(self, *a, **k):
            return list(fake_grants)

        def count_for_group(self, group_id):
            return len(fake_grants)

    class _Groups:
        def list_all(self):
            return [{"id": "grp-1", "name": "Delivery", "is_system": False, "created_by": "admin"}]

        def get_by_name(self, name):
            return None

    class _Members:
        def count_members(self, gid):
            return 0

        def list_members_for_group(self, gid):
            return []

    monkeypatch.setattr(access_mod, "resource_grants_repo", lambda: _Grants())
    monkeypatch.setattr(access_mod, "user_groups_repo", lambda: _Groups())
    monkeypatch.setattr(access_mod, "user_group_members_repo", lambda: _Members())

    import dataclasses

    from app.resource_types import ResourceType, enabled_resource_types

    # Neutralise every projection: this test is about which TYPES appear, not
    # about what any of them can read from an empty database. The spec is a
    # frozen dataclass, so swap in copies rather than assigning to a field.
    quiet = [dataclasses.replace(s, list_blocks=lambda: []) for s in enabled_resource_types()]
    monkeypatch.setattr(access_mod, "enabled_resource_types", lambda: quiet, raising=False)
    monkeypatch.setattr(
        "app.resource_types.enabled_resource_types", lambda: quiet, raising=False
    )

    import asyncio

    payload = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        access_mod.access_overview(user={"id": "admin1", "email": "admin@x"})
    )

    type_keys = {r["type_key"] for r in payload["resources"]}
    assert ResourceType.TABLE.value not in type_keys, (
        "the picker payload still offers tables"
    )
    assert ResourceType.DATA_PACKAGE.value in type_keys

    grant_types = {g["resource_type"] for g in payload["grants"]}
    assert "table" in grant_types, (
        "an existing table grant vanished from the payload — the rows already "
        "written would be invisible and unremovable"
    )


# ---------------------------------------------------------------------------
# Ticket 10 — the page's two sections, decided on the actionability axis
# ---------------------------------------------------------------------------


def test_grant_sections_split_on_whether_the_admin_can_act():
    """`Change here` / `Set elsewhere`, keyed on `revocable`.

    The question this answered assumed the split was "mine versus the
    machine's" — one writer against nine. Measured against
    `src/grant_sources.py`, nine writers are not the admin but only TWO
    produce rows a revoke cannot remove: the nightly marketplace sync
    re-asserts, and the SharePoint wizard rewrites its scope's collection
    grants. The other seven revoke cleanly and stay revoked.

    So grouping by authorship would file seven revocable kinds under "not
    yours" — including the Library shares an admin most often opens the page
    to check. Both section names say where the ACTION lives, which is the
    axis that was chosen.
    """
    from src.grant_sources import (
        GRANT_SOURCES,
        SECTION_CHANGE_HERE,
        SECTION_SET_ELSEWHERE,
        section_for,
    )

    elsewhere = {k for k in GRANT_SOURCES if section_for(k) == SECTION_SET_ELSEWHERE}
    assert elsewhere == {"marketplace_sync", "sharepoint_wizard"}, (
        f"exactly the two re-asserting writers belong in the inert section, got {elsewhere}"
    )

    # The seven an admin came here to act on.
    for key in (
        "library_share",
        "share_request",
        "collection_create",
        "skill_contribution",
        "mcp_source_default",
        "chat_seed",
        "system_plugin_migration",
    ):
        assert section_for(key) == SECTION_CHANGE_HERE, (
            f"{key} revokes cleanly, so it belongs where the admin can act on it"
        )


def test_an_unrecorded_source_is_actionable_not_quarantined():
    """The default matters more than the mapping.

    A row with no source is every grant predating the provenance column and
    every grant on a DuckDB instance, where the column does not exist at all.
    A stale key is a writer removed in a later release. All are revocable and
    nothing re-asserts them, so the actionable section is the right home —
    filing them under "set elsewhere" would hide working controls behind a
    heading that says they do not work.
    """
    from src.grant_sources import ACCESS_PAGE, SECTION_CHANGE_HERE, section_for

    assert section_for(None) == SECTION_CHANGE_HERE
    assert section_for("") == SECTION_CHANGE_HERE
    assert section_for("a_writer_deleted_in_2027") == SECTION_CHANGE_HERE
    assert section_for(ACCESS_PAGE) == SECTION_CHANGE_HERE


def test_the_overview_payload_carries_a_section_per_grant(monkeypatch):
    """Sent, not re-derived client-side — so the rule has one home."""
    import asyncio

    import app.api.access as access_mod

    rows = [
        {
            "id": "g-own", "group_id": "grp-1", "resource_type": "chat",
            "resource_id": "chat", "requirement": "available", "assigned_at": None,
            "assigned_by": "admin@x", "source": None, "scope": None, "group_name": "D",
        },
        {
            "id": "g-sync", "group_id": "grp-1", "resource_type": "marketplace_plugin",
            "resource_id": "acme/p", "requirement": "available", "assigned_at": None,
            "assigned_by": "system", "source": "marketplace_sync", "scope": None,
            "group_name": "D",
        },
    ]

    class _Grants:
        def list_all(self, *a, **k):
            return list(rows)

        def count_for_group(self, gid):
            return len(rows)

    class _Groups:
        def list_all(self):
            return [{"id": "grp-1", "name": "D", "is_system": False, "created_by": "a"}]

        def get_by_name(self, name):
            return None

    class _Members:
        def count_members(self, gid):
            return 0

        def list_members_for_group(self, gid):
            return []

    monkeypatch.setattr(access_mod, "resource_grants_repo", lambda: _Grants())
    monkeypatch.setattr(access_mod, "user_groups_repo", lambda: _Groups())
    monkeypatch.setattr(access_mod, "user_group_members_repo", lambda: _Members())

    import dataclasses

    from app.resource_types import enabled_resource_types

    quiet = [dataclasses.replace(s, list_blocks=lambda: []) for s in enabled_resource_types()]
    monkeypatch.setattr(access_mod, "enabled_resource_types", lambda: quiet, raising=False)
    monkeypatch.setattr("app.resource_types.enabled_resource_types", lambda: quiet, raising=False)

    payload = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        access_mod.access_overview(user={"id": "admin1", "email": "admin@x"})
    )
    by_id = {g["id"]: g for g in payload["grants"]}
    assert by_id["g-own"]["section"] == "change_here"
    assert by_id["g-sync"]["section"] == "set_elsewhere", (
        "a marketplace-sync row is rewritten on its next run; revoking it here "
        "does not stick, so it must not sit among rows that do"
    )
