"""Every grantable resource type belongs to one of three families.

The families are the Library's own two — Knowledge (what you read, query or
open) and Capabilities (what changes what your agent can do) — plus Surfaces,
for the two types that are neither: a chat and a Slack channel are where a
group *meets* Agnes, not something it holds.

`/admin/access` groups by these, so the admin taxonomy and the Library
taxonomy are the same taxonomy. Design:
`docs/superpowers/specs/2026-08-28-access-page-definition.md`.
"""

from __future__ import annotations

from app.resource_types import (
    RESOURCE_FAMILIES,
    RESOURCE_TYPES,
    ResourceFamily,
    ResourceType,
)

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# The decided mapping. A new resource type must be added here deliberately —
# the assertion below is exhaustive in both directions.
EXPECTED: dict[ResourceType, ResourceFamily] = {
    ResourceType.TABLE: ResourceFamily.KNOWLEDGE,
    ResourceType.DATA_PACKAGE: ResourceFamily.KNOWLEDGE,
    ResourceType.SEMANTIC_MODEL: ResourceFamily.KNOWLEDGE,
    ResourceType.MEMORY_DOMAIN: ResourceFamily.KNOWLEDGE,
    ResourceType.MEMORY_ITEM: ResourceFamily.KNOWLEDGE,
    ResourceType.RECIPE: ResourceFamily.KNOWLEDGE,
    ResourceType.COLLECTION: ResourceFamily.KNOWLEDGE,
    ResourceType.KNOWLEDGE_DIGEST: ResourceFamily.KNOWLEDGE,
    ResourceType.DATA_APP: ResourceFamily.KNOWLEDGE,
    ResourceType.CORPUS_FILE: ResourceFamily.KNOWLEDGE,
    ResourceType.MARKETPLACE_PLUGIN: ResourceFamily.CAPABILITY,
    ResourceType.STORE_ENTITY: ResourceFamily.CAPABILITY,
    ResourceType.AGENT: ResourceFamily.CAPABILITY,
    ResourceType.CHAT: ResourceFamily.SURFACE,
    ResourceType.SLACK_CHANNEL: ResourceFamily.SURFACE,
}


class TestEveryTypeHasAFamily:
    def test_registry_covers_every_enum_member(self):
        assert set(RESOURCE_TYPES) == set(ResourceType)

    def test_mapping_is_exhaustive_both_ways(self):
        """A new ResourceType fails here until someone decides its family."""
        assert set(EXPECTED) == set(ResourceType)

    def test_each_spec_carries_the_decided_family(self):
        actual = {key: spec.family for key, spec in RESOURCE_TYPES.items()}
        assert actual == EXPECTED


class TestFamilyRegistry:
    def test_three_families_in_render_order(self):
        """Knowledge first — it is what an admin grants most of."""
        assert list(RESOURCE_FAMILIES) == [
            ResourceFamily.KNOWLEDGE,
            ResourceFamily.CAPABILITY,
            ResourceFamily.SURFACE,
        ]

    def test_every_family_has_a_label_and_a_blurb(self):
        for family, spec in RESOURCE_FAMILIES.items():
            assert spec.key is family
            assert spec.display_name.strip()
            assert spec.blurb.strip()

    def test_capability_blurb_states_the_line(self):
        """The Library's own test for the second tab: does it change what the
        agent can do? Nothing else belongs in Capabilities."""
        blurb = RESOURCE_FAMILIES[ResourceFamily.CAPABILITY].blurb.lower()
        assert "agent" in blurb

    def test_no_family_is_empty(self):
        used = {spec.family for spec in RESOURCE_TYPES.values()}
        assert used == set(RESOURCE_FAMILIES)


class TestAccessOverviewCarriesFamilies:
    """The template groups by family, so the snapshot has to say it.

    Without this the page would need a second source of truth for the
    mapping — a hard-coded list in Jinja or JS that drifts the first time
    someone adds a resource type.
    """

    def test_every_resource_entry_names_its_family(self, seeded_app):
        r = seeded_app["client"].get(
            "/api/admin/access-overview", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200, r.text

        entries = r.json()["resources"]
        assert entries, "no resource types projected"
        for entry in entries:
            assert entry["family"] in {f.value for f in ResourceFamily}
            assert entry["family_display"].strip()

    def test_families_arrive_in_render_order(self, seeded_app):
        r = seeded_app["client"].get(
            "/api/admin/access-overview", headers=_auth(seeded_app["admin_token"])
        )

        seen: list[str] = []
        for entry in r.json()["resources"]:
            if entry["family"] not in seen:
                seen.append(entry["family"])
        expected = [f.value for f in RESOURCE_FAMILIES]
        assert seen == [f for f in expected if f in seen]

    def test_snapshot_names_the_families_it_renders(self, seeded_app):
        """A section with no granted rows still needs its header and blurb,
        so an empty family reads as empty rather than missing."""
        r = seeded_app["client"].get(
            "/api/admin/access-overview", headers=_auth(seeded_app["admin_token"])
        )

        families = r.json()["families"]
        assert [f["key"] for f in families] == [f.value for f in RESOURCE_FAMILIES]
        for f in families:
            assert f["display_name"].strip() and f["blurb"].strip()


class TestGrantsCarryTheirWriter:
    """A row should be able to say where it came from.

    Both writers record `assigned_by` — the admin API an email, the Library's
    owner-sharing path a user id — but the snapshot the page reads dropped
    the column, so every grant looked equally like an admin's doing.
    """

    def test_overview_grants_expose_assigned_by(self, seeded_app):
        c = seeded_app["client"]
        hdr = _auth(seeded_app["admin_token"])
        made = c.post(
            "/api/admin/grants",
            headers=hdr,
            json={
                "group_id": c.get("/api/admin/access-overview", headers=hdr).json()["groups"][0]["id"],
                "resource_type": "chat",
                "resource_id": "chat",
                "requirement": "available",
            },
        )
        assert made.status_code in (200, 201), made.text

        grants = c.get("/api/admin/access-overview", headers=hdr).json()["grants"]
        assert grants, "no grants in the snapshot"
        assert all("assigned_by" in g for g in grants)
        mine = [g for g in grants if g["resource_type"] == "chat"]
        assert mine and mine[0]["assigned_by"], "the writer was not recorded"


class TestPackagesSayWhichTablesTheyCarry:
    """`/admin/tables` asks `/admin/access` "who can see this table".

    A per-table grant no longer surfaces a table in an analyst's manifest —
    the package grant does — so the honest answer names the packages that
    carry it, and the groups those are granted to. The page can only give
    that answer if the projection says what a package contains.
    """

    def test_package_items_carry_their_table_ids(self, seeded_app):
        from app.resource_types import _data_package_blocks
        from src.repositories import data_packages_repo, table_registry_repo

        table_registry_repo().register(
            id="tbl_orders", name="orders", source_type="local", bucket="in.c-crm",
            source_table="orders",
        )
        repo = data_packages_repo()
        pkg_id = repo.create(
            name="Revenue Core", description="revenue", created_by="admin@test.com",
            slug="revenue-core", icon=None, color=None,
        )
        repo.add_table(pkg_id, "tbl_orders", added_by="admin@test.com")

        blocks = _data_package_blocks()
        items = [i for b in blocks for i in b["items"] if i["resource_id"] == pkg_id]
        assert items, "the package is not in the projection"
        assert items[0]["contains"] == ["tbl_orders"]

    def test_a_package_with_no_tables_says_so_with_an_empty_list(self, seeded_app):
        """Not a missing key — the page distinguishes "carries nothing" from
        "we did not look"."""
        from app.resource_types import _data_package_blocks
        from src.repositories import data_packages_repo

        pkg_id = data_packages_repo().create(
            name="Empty", description="", created_by="admin@test.com",
            slug="empty", icon=None, color=None,
        )
        blocks = _data_package_blocks()
        items = [i for b in blocks for i in b["items"] if i["resource_id"] == pkg_id]
        assert items and items[0]["contains"] == []
