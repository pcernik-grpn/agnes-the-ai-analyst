"""`/admin/access` stops offering `table`, and keeps the rows already written.

Measured, not reasoned. The picker listed every registered table among 239
grantable "knowledge" items, and one production group carried ~86 `table`
grants, each rendered with the label *"reached through a package"*. An admin
ticked those believing they granted data.

They granted nothing. `src/rbac.py::can_access_table` resolves the caller's
data packages, returns False outright if they have none, then intersects
those with the packages containing the table — a `resource_grants` row on the
table itself is read nowhere in that path. So the picker was writing rows
nothing reads, in a slot that implied it was granting access.

The effort's ticket 11 originally answered "tables stay, and say what they
do"; the screenshots reopened and reversed it. See
`docs/superpowers/maps/access-who-can-see-what/issues/11-*`.

The second test is the half that is easy to get wrong: only the OFFER goes.
Hiding existing rows too would leave the ~86 already written invisible AND
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
    that is what keeps the ~86 cleanable.
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
        "an existing table grant vanished from the payload — the ~86 rows already "
        "written would be invisible and unremovable"
    )
