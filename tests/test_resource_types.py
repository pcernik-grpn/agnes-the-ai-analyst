"""Tests for app.resource_types — registry + list_blocks delegates.

Focus on the TABLE resource type. The marketplace projection has integration
coverage elsewhere (test_marketplace_*.py); here we exercise the table
projection and the wiring into /api/admin/access-overview.
"""

from __future__ import annotations

import pytest

from app.resource_types import (
    RESOURCE_TYPES,
    ResourceType,
    _table_blocks,
)
from src.db import get_system_db
from src.repositories.table_registry import TableRegistryRepository


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def system_conn(seeded_app):
    """Open a system DB connection for the active test DATA_DIR.

    seeded_app sets up DATA_DIR via the e2e_env fixture and seeds users; we
    just need the system DB here. Closed by the fixture teardown.
    """
    conn = get_system_db()
    try:
        yield conn
    finally:
        conn.close()


class TestTableBlocks:
    def test_groups_by_bucket(self, system_conn):
        repo = TableRegistryRepository(system_conn)
        repo.register(id="t_finance_a", name="finance_a", bucket="in.c-finance", source_type="dummy")
        repo.register(id="t_finance_b", name="finance_b", bucket="in.c-finance", source_type="dummy")
        repo.register(id="t_marketing_a", name="marketing_a", bucket="in.c-marketing", source_type="dummy")

        blocks = _table_blocks()
        by_name = {b["name"]: b for b in blocks}

        assert "in.c-finance" in by_name
        assert "in.c-marketing" in by_name
        assert len(by_name["in.c-finance"]["items"]) == 2
        assert len(by_name["in.c-marketing"]["items"]) == 1

        finance_ids = {it["resource_id"] for it in by_name["in.c-finance"]["items"]}
        assert finance_ids == {"t_finance_a", "t_finance_b"}

    def test_item_shape_matches_ui_contract(self, system_conn):
        repo = TableRegistryRepository(system_conn)
        repo.register(
            id="shape_test",
            name="shape_test",
            bucket="b1",
            source_type="keboola",
            query_mode="remote",
            description="hello",
        )

        blocks = _table_blocks()
        item = blocks[0]["items"][0]

        # Fields the grant matrix's renderAccess() reads (it moved from the
        # retired admin_access.html to admin_group_detail.html's Access tab):
        assert item["resource_id"] == "shape_test"
        assert item["name"] == "shape_test"
        assert item["category"] == "remote"  # query_mode → badge
        assert item["source_type"] == "keboola"  # → badge
        assert item["description"] == "hello"

    def test_handles_null_or_empty_bucket(self, system_conn):
        repo = TableRegistryRepository(system_conn)
        repo.register(id="orphan", name="orphan", source_type="dummy")
        # bucket left as None

        blocks = _table_blocks()
        names = {b["name"] for b in blocks}
        assert "(no bucket)" in names
        orphan_block = next(b for b in blocks if b["name"] == "(no bucket)")
        assert orphan_block["items"][0]["resource_id"] == "orphan"

    def test_empty_registry_returns_empty_list(self, system_conn):
        # Fresh DB, no tables registered yet.
        assert _table_blocks() == []


class TestResourceTypeRegistration:
    def test_table_is_in_registry(self):
        assert ResourceType.TABLE in RESOURCE_TYPES
        spec = RESOURCE_TYPES[ResourceType.TABLE]
        assert spec.key is ResourceType.TABLE
        assert spec.display_name == "Tables"
        assert callable(spec.list_blocks)

    def test_enum_value_persisted_form(self):
        # Stored verbatim in resource_grants.resource_type — guard against
        # accidental rename.
        assert ResourceType.TABLE.value == "table"


class TestMemoryItemResourceType:
    """v49: ``memory_item`` exists for the per-group per-item Required override.
    Global default Required tier still rides on ``knowledge_items.is_required``.
    """

    def test_memory_item_in_enum(self):
        assert ResourceType.MEMORY_ITEM.value == "memory_item"

    def test_memory_item_in_registry(self):
        assert ResourceType.MEMORY_ITEM in RESOURCE_TYPES
        spec = RESOURCE_TYPES[ResourceType.MEMORY_ITEM]
        assert spec.key is ResourceType.MEMORY_ITEM
        assert callable(spec.list_blocks)

    def test_memory_item_blocks_empty_when_no_items(self, system_conn):
        from app.resource_types import _memory_item_blocks

        assert _memory_item_blocks() == []


class TestMemoryDomainResourceType:
    """v49: domain projection now reads from ``memory_domains`` table, not the
    hardcoded VALID_DOMAINS list. resource_id is the ``memory_domains.id``.
    """

    def test_memory_domain_blocks_empty_when_no_domains(self, system_conn):
        # The v49 migration seeds canonical domains, but a fresh manual seed
        # may exclude them — verify the projection scales from 0 upward.
        from app.resource_types import _memory_domain_blocks

        system_conn.execute("DELETE FROM memory_domains")
        assert _memory_domain_blocks() == []

    def test_memory_domain_blocks_returns_id_not_slug(self, system_conn):
        from app.resource_types import _memory_domain_blocks

        system_conn.execute("DELETE FROM memory_domains")
        system_conn.execute(
            "INSERT INTO memory_domains(id, slug, name, icon, color) "
            "VALUES ('md_test', 'test', 'Test domain', '🔬', '#abc')"
        )
        blocks = _memory_domain_blocks()
        assert len(blocks) == 1
        items = blocks[0]["items"]
        assert items[0]["resource_id"] == "md_test"
        assert items[0]["slug"] == "test"
        assert items[0]["name"] == "Test domain"


class TestDataPackageResourceType:
    """v49: ``data_package`` is the unit of Add-to-Stack on /catalog."""

    def test_data_package_in_enum(self):
        assert ResourceType.DATA_PACKAGE.value == "data_package"

    def test_data_package_in_registry(self):
        assert ResourceType.DATA_PACKAGE in RESOURCE_TYPES
        spec = RESOURCE_TYPES[ResourceType.DATA_PACKAGE]
        assert spec.key is ResourceType.DATA_PACKAGE
        assert callable(spec.list_blocks)

    def test_data_package_blocks_empty_when_no_packages(self, system_conn):
        from app.resource_types import _data_package_blocks

        assert _data_package_blocks() == []

    def test_data_package_blocks_includes_packages(self, system_conn):
        from app.resource_types import _data_package_blocks

        system_conn.execute(
            "INSERT INTO data_packages(id, slug, name, description, icon, color) "
            "VALUES ('pkg_sales', 'sales', 'Sales bundle', 'Sales tables', '📦', '#abc')"
        )
        blocks = _data_package_blocks()
        assert len(blocks) == 1
        block = blocks[0]
        assert block["items"][0]["resource_id"] == "pkg_sales"
        assert block["items"][0]["name"] == "Sales bundle"


class TestDataAppResourceType:
    """v96: ``data_app`` grants access to a hosted user web app (resource_id
    = slug, per Task 2 of the Data Apps feature)."""

    def test_data_app_in_enum(self):
        assert ResourceType.DATA_APP.value == "data_app"

    def test_data_app_in_registry(self):
        assert ResourceType.DATA_APP in RESOURCE_TYPES
        spec = RESOURCE_TYPES[ResourceType.DATA_APP]
        assert spec.key is ResourceType.DATA_APP
        assert spec.id_format == "<slug>"
        assert callable(spec.list_blocks)

    def test_data_app_blocks_empty_when_no_apps(self, system_conn):
        from app.resource_types import _data_app_blocks

        assert _data_app_blocks() == []

    def test_data_app_blocks_hide_soft_deleted_linked_and_use_override(self, system_conn):
        """The grant picker must not offer `linked_hidden` rows (they 404 on
        every read surface) and shows the admin-pinned description override
        where present (Devin Review on #1116)."""
        from app.resource_types import _data_app_blocks

        system_conn.execute(
            "INSERT INTO data_apps(id, slug, name, description, description_override, owner_user_id, repo_mode, state) "
            "VALUES ('app_l1', 'kbc-live', 'Live', 'synced', 'pinned by admin', 'system', 'linked', 'linked')"
        )
        system_conn.execute(
            "INSERT INTO data_apps(id, slug, name, description, owner_user_id, repo_mode, state) "
            "VALUES ('app_l2', 'kbc-gone', 'Gone', 'synced', 'system', 'linked', 'linked_hidden')"
        )
        blocks = _data_app_blocks()
        items = {i["resource_id"]: i for i in blocks[0]["items"]}
        assert "kbc-gone" not in items
        assert items["kbc-live"]["description"] == "pinned by admin"

    def test_data_app_blocks_includes_apps(self, system_conn):
        from app.resource_types import _data_app_blocks

        system_conn.execute(
            "INSERT INTO data_apps(id, slug, name, description, owner_user_id) "
            "VALUES ('app_test1', 'my-app', 'My App', 'A test app', 'u_owner')"
        )
        blocks = _data_app_blocks()
        assert len(blocks) == 1
        block = blocks[0]
        assert block["items"][0]["resource_id"] == "my-app"
        assert block["items"][0]["name"] == "My App"
        assert block["items"][0]["description"] == "A test app"
        assert block["items"][0]["slug"] == "my-app"

    def test_data_app_blocks_excludes_drafts(self, system_conn):
        """Drafts are working copies layered on a parent app, not
        independently grantable resources -- they must not clutter the
        admin grant picker (mirrors the `include_drafts=False` filter on
        every other data-apps listing surface)."""
        from app.resource_types import _data_app_blocks

        system_conn.execute(
            "INSERT INTO data_apps(id, slug, name, description, owner_user_id) "
            "VALUES ('app_test1', 'my-app', 'My App', 'A test app', 'u_owner')"
        )
        system_conn.execute(
            "INSERT INTO data_apps(id, slug, name, description, owner_user_id,"
            " parent_app_id, is_draft, draft_branch) "
            "VALUES ('app_test2', 'my-app--init', 'My App (draft)', '', 'u_owner',"
            " 'app_test1', TRUE, 'init')"
        )
        blocks = _data_app_blocks()
        assert len(blocks) == 1
        slugs = [item["resource_id"] for item in blocks[0]["items"]]
        assert slugs == ["my-app"]


class TestAccessOverviewWithholdsTables:
    """`table` is a registered type that the Access page does not OFFER.

    This class asserted the opposite until the effort's ticket 11 was
    reopened and reversed on production evidence: the grant dialog offered
    every table as knowledge, and a group's list ran page after page of
    table rows inherited from Everyone, each labelled "reached through a
    package" — the explanation had become the noise.

    The reversal is narrow, and the two halves are worth keeping apart. The
    TYPE is untouched: `ResourceType.TABLE` is still registered, table
    grants still exist and are still read (`src/agent_scope_intersection.py`
    unions raw grants with package tables when scoping an agent), and the
    projection still works. Only `offered_on_access` is False, which is what
    keeps it out of this endpoint's `resources` and therefore out of the
    picker.

    A table's audience is decided by the data package that carries it, so
    a direct table grant grants an analyst nothing on its own
    (`src/rbac.py::can_access_table` intersects the caller's packages with
    the packages containing the table and never reads a table grant).

    The positive case — that the page offers the other fifteen types and
    that the endpoint still projects tables for callers that ask for them
    another way — lives in `tests/test_access_page_does_not_offer_tables.py`.
    """

    def test_tables_are_not_offered(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/access-overview",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        type_keys = {r["type_key"] for r in resp.json()["resources"]}
        assert "table" not in type_keys
        assert "marketplace_plugin" in type_keys  # regression — still there

    def test_a_registered_table_does_not_appear(self, seeded_app):
        """Registering one must not put it back on the page.

        The withholding is a property of the TYPE, not of whether any table
        happens to exist — so the check is worth making with a row present
        rather than on an empty registry, where it would pass either way.
        """
        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="overview_test",
                name="overview_test",
                bucket="in.c-overview",
                source_type="dummy",
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/access-overview",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert not [r for r in resp.json()["resources"] if r["type_key"] == "table"]

    def test_the_type_itself_is_still_registered(self):
        """The reversal took the table off the PAGE, not out of the model."""
        from app.resource_types import RESOURCE_TYPES, ResourceType

        spec = RESOURCE_TYPES[ResourceType.TABLE]
        assert spec.offered_on_access is False
        assert spec.list_blocks is not None   # still projectable


class TestSlackChannelBlocks:
    def test_enum_member_and_spec_registered(self):
        from app.resource_types import RESOURCE_TYPES, ResourceType

        assert ResourceType.SLACK_CHANNEL.value == "slack_channel"
        spec = RESOURCE_TYPES[ResourceType.SLACK_CHANNEL]
        assert spec.display_name == "Slack channels"
        assert spec.id_format == "<channel_id>"

    def test_in_enabled_resource_types(self):
        from app.resource_types import enabled_resource_types, ResourceType

        keys = {s.key for s in enabled_resource_types()}
        assert ResourceType.SLACK_CHANNEL in keys

    def test_projects_seeded_grant(self, system_conn):
        from app.resource_types import _slack_channel_blocks

        gid = system_conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]
        system_conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
            "VALUES ('rg_sc1', ?, 'slack_channel', 'C123')",
            [gid],
        )
        blocks = _slack_channel_blocks()
        items = [it for b in blocks for it in b["items"]]
        assert any(it["resource_id"] == "C123" for it in items)

    def test_empty_when_no_grants(self, system_conn):
        from app.resource_types import _slack_channel_blocks

        system_conn.execute("DELETE FROM resource_grants WHERE resource_type = 'slack_channel'")
        assert _slack_channel_blocks() == []

    def test_admin_group_grant_not_listed(self, system_conn):
        """A slack_channel grant to a non-Everyone group (Admin) must NOT
        appear in the projection — mirrors enforcement, which only honors
        the Everyone group (see binding.is_channel_allowlisted)."""
        from app.resource_types import _slack_channel_blocks

        admin_gid = system_conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()[0]
        system_conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
            "VALUES ('rg_sc_adm', ?, 'slack_channel', 'C_ADM')",
            [admin_gid],
        )
        blocks = _slack_channel_blocks()
        items = [it for b in blocks for it in b["items"]]
        assert not any(it["resource_id"] == "C_ADM" for it in items)


class TestTableGrantsAlwaysOn:
    """v19+ — the env-gate AGNES_ENABLE_TABLE_GRANTS was removed; TABLE is
    listed unconditionally and grants succeed without a feature flag."""

    def test_resource_types_endpoint_includes_table(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/resource-types",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        keys = {r["key"] for r in resp.json()}
        assert "table" in keys
        assert "marketplace_plugin" in keys

    def test_create_table_grant_accepted(self, seeded_app):
        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="ff_table",
                name="ff_table",
                bucket="in.c-ff",
                source_type="dummy",
            )
        finally:
            conn.close()
        c = seeded_app["client"]
        admin = _auth(seeded_app["admin_token"])
        gresp = c.post(
            "/api/admin/groups",
            headers=admin,
            json={"name": "table-grant-on"},
        )
        assert gresp.status_code == 201
        gid = gresp.json()["id"]
        resp = c.post(
            "/api/admin/grants",
            headers=admin,
            json={
                "group_id": gid,
                "resource_type": "table",
                "resource_id": "ff_table",
            },
        )
        assert resp.status_code == 201


class TestCollectionResourceType:
    """v77: COLLECTION resource type for bring-your-files Collections."""

    def test_collection_enum_value(self):
        assert ResourceType.COLLECTION.value == "collection"

    def test_collection_in_registry(self):
        assert ResourceType.COLLECTION in RESOURCE_TYPES
        spec = RESOURCE_TYPES[ResourceType.COLLECTION]
        assert spec.key is ResourceType.COLLECTION
        assert spec.display_name == "Collections"
        assert callable(spec.list_blocks)

    def test_collection_in_enabled_resource_types(self):
        from app.resource_types import enabled_resource_types

        keys = {s.key for s in enabled_resource_types()}
        assert ResourceType.COLLECTION in keys

    def test_collection_blocks_empty_when_no_corpora(self, system_conn):
        from app.resource_types import _collection_blocks

        assert _collection_blocks() == []

    def test_collection_blocks_projects_live_corpora(self, system_conn):
        from app.resource_types import _collection_blocks

        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, description, created_by) "
            "VALUES ('col_abc', 'my-files', 'My Files', 'A collection', 'u1')"
        )
        blocks = _collection_blocks()
        assert len(blocks) == 1
        block = blocks[0]
        assert block["id"] == "collections"
        assert block["name"] == "Collections"
        items = block["items"]
        assert len(items) == 1
        assert items[0]["resource_id"] == "col_abc"
        assert items[0]["name"] == "My Files"
        assert items[0]["slug"] == "my-files"

    def test_collection_blocks_grant_count_zero_when_ungranted(self, system_conn):
        """spec §13.2 "/admin/access" — a collection with no group grant at
        all carries ``grant_count == 0``, the field the "⚠ nobody" badge is
        driven from."""
        from app.resource_types import _collection_blocks

        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_none', 'none', 'None', 'u1')"
        )
        items = _collection_blocks()[0]["items"]
        assert items[0]["grant_count"] == 0

    def test_collection_blocks_grant_count_reflects_every_group(self, system_conn):
        """`grant_count` counts ALL groups holding a grant, independent of
        which group an admin has selected in the /admin/access pane."""
        from app.resource_types import _collection_blocks

        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_shared', 'shared', 'Shared', 'u1')"
        )
        system_conn.execute("INSERT INTO user_groups (id, name) VALUES ('g1', 'Group One')")
        system_conn.execute("INSERT INTO user_groups (id, name) VALUES ('g2', 'Group Two')")
        from src.repositories import resource_grants_repo

        grants_repo = resource_grants_repo()
        grants_repo.create("g1", "collection", "col_shared", "admin1")
        grants_repo.create("g2", "collection", "col_shared", "admin1")

        items = _collection_blocks()[0]["items"]
        assert items[0]["grant_count"] == 2

    def test_collection_blocks_excludes_soft_deleted(self, system_conn):
        from app.resource_types import _collection_blocks

        system_conn.execute(
            "INSERT INTO file_corpora "
            "(id, slug, name, created_by, deleted_at) "
            "VALUES ('col_del', 'deleted', 'Deleted', 'u1', current_timestamp)"
        )
        blocks = _collection_blocks()
        if blocks:
            ids = [it["resource_id"] for b in blocks for it in b["items"]]
            assert "col_del" not in ids


class TestCollectionInventoryProjection:
    """The admin /access page is where "what files exist on this instance, and
    whose are they" is answered. An admin sees every collection by god-mode,
    including the private one-file artifacts a chat file-drop creates, so the
    projection has to name the owner and the file count — and must not omit
    files."""

    def _seed_user(self, conn, user_id: str, email: str) -> None:
        conn.execute(
            "INSERT INTO users (id, email, name) VALUES (?, ?, ?)",
            [user_id, email, email.split("@")[0]],
        )

    def test_collection_item_names_its_owner(self, system_conn):
        from app.resource_types import _collection_blocks

        self._seed_user(system_conn, "u_owner", "jan@example.com")
        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) "
            "VALUES ('col_owned', 'owned', 'Owned Files', 'u_owner')"
        )
        items = {i["resource_id"]: i for i in _collection_blocks()[0]["items"]}
        assert items["col_owned"]["owner_email"] == "jan@example.com"

    def test_owner_is_none_when_the_account_is_gone(self, system_conn):
        """An orphaned collection still lists — an inventory that drops rows it
        cannot fully describe is worse than one with a blank column."""
        from app.resource_types import _collection_blocks

        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) "
            "VALUES ('col_orphan', 'orphan', 'Orphan', 'u_deleted')"
        )
        items = {i["resource_id"]: i for i in _collection_blocks()[0]["items"]}
        assert "col_orphan" in items
        assert items["col_orphan"]["owner_email"] is None

    def test_collection_item_counts_its_files(self, system_conn):
        from app.resource_types import _collection_blocks
        from src.repositories import corpus_files_repo

        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_two', 'two', 'Two', 'u1')"
        )
        repo = corpus_files_repo()
        for n in ("a.pdf", "b.pdf"):
            repo.add(
                corpus_id="col_two",
                filename=n,
                sha256="0" * 64,
                file_type="pdf",
                size_bytes=1024,
                storage_path=f"/tmp/{n}",
            )
        items = {i["resource_id"]: i for i in _collection_blocks()[0]["items"]}
        assert items["col_two"]["file_count"] == 2

    def test_a_single_file_collection_is_listed_in_the_file_inventory(self, system_conn):
        """The regression this projection change exists for: a chat file-drop
        creates a ONE-file collection, and skipping those hid most of a real
        instance's files from the page that inventories them."""
        from app.resource_types import _corpus_file_blocks
        from src.repositories import corpus_files_repo

        self._seed_user(system_conn, "u_drop", "eva@example.com")
        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) "
            "VALUES ('col_drop', 'test-download', 'test_download.md', 'u_drop')"
        )
        corpus_files_repo().add(
            corpus_id="col_drop",
            filename="test_download.md",
            sha256="1" * 64,
            file_type="md",
            size_bytes=2048,
            storage_path="/tmp/test_download.md",
        )
        blocks = {b["id"]: b for b in _corpus_file_blocks()}
        assert "col_drop" in blocks, "a one-file collection's file must be visible to an admin"
        item = blocks["col_drop"]["items"][0]
        assert item["name"] == "test_download.md"
        assert item["owner_email"] == "eva@example.com"
        # The block header names the owner too, so the file list reads as
        # "somebody's upload" rather than as a folder on the instance.
        assert "eva@example.com" in blocks["col_drop"]["name"]

    def test_file_inventory_states_shape_never_content(self, system_conn):
        from app.resource_types import _corpus_file_blocks
        from src.repositories import corpus_files_repo

        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_meta', 'meta', 'Meta', 'u1')"
        )
        corpus_files_repo().add(
            corpus_id="col_meta",
            filename="report.pdf",
            sha256="2" * 64,
            file_type="pdf",
            size_bytes=2_202_010,
            storage_path="/tmp/report.pdf",
        )
        item = {b["id"]: b for b in _corpus_file_blocks()}["col_meta"]["items"][0]
        assert item["description"] == "pdf · 2.1 MB"
        # Nothing in the projection can carry document text.
        assert "text" not in item and "content" not in item

    def test_empty_collection_contributes_no_file_block(self, system_conn):
        from app.resource_types import _corpus_file_blocks

        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_empty', 'empty', 'Empty', 'u1')"
        )
        assert "col_empty" not in {b["id"] for b in _corpus_file_blocks()}

    def test_a_large_collection_is_bounded_and_reports_truncation(self, system_conn):
        """The regression this bound exists for: an instance with ~216k
        files across ~390 collections made the unbounded projection a
        37 MB payload that froze the admin's browser tab rendering it."""
        from app.resource_types import _CORPUS_FILE_PREVIEW_LIMIT, _corpus_file_blocks
        from src.repositories import corpus_files_repo

        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_big', 'big', 'Big', 'u1')"
        )
        repo = corpus_files_repo()
        total = _CORPUS_FILE_PREVIEW_LIMIT + 5
        for i in range(total):
            repo.add(
                corpus_id="col_big",
                filename=f"f{i}.pdf",
                sha256=str(i) * 8,
                file_type="pdf",
                size_bytes=10,
                storage_path=f"/tmp/f{i}.pdf",
            )
        block = {b["id"]: b for b in _corpus_file_blocks()}["col_big"]
        assert len(block["items"]) == _CORPUS_FILE_PREVIEW_LIMIT
        assert block["items_total"] == total
        assert block["items_truncated"] is True

    def test_a_collection_within_the_cap_is_not_marked_truncated(self, system_conn):
        from app.resource_types import _corpus_file_blocks
        from src.repositories import corpus_files_repo

        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_small', 'small', 'Small', 'u1')"
        )
        corpus_files_repo().add(
            corpus_id="col_small",
            filename="one.pdf",
            sha256="a" * 8,
            file_type="pdf",
            size_bytes=10,
            storage_path="/tmp/one.pdf",
        )
        block = {b["id"]: b for b in _corpus_file_blocks()}["col_small"]
        assert len(block["items"]) == 1
        assert block["items_total"] == 1
        assert block["items_truncated"] is False

    def test_an_already_granted_file_stays_visible_past_the_preview_cap(self, system_conn):
        """A group holding a per-file grant must stay resolvable on the
        /admin/access 'By group' tab even when the file falls outside the
        bounded preview — otherwise a real grant becomes invisible and
        unrevokable through the admin UI."""
        from app.resource_types import _CORPUS_FILE_PREVIEW_LIMIT, _corpus_file_blocks
        from src.db import SYSTEM_EVERYONE_GROUP
        from src.repositories import corpus_files_repo, resource_grants_repo

        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_grant', 'grant', 'Grant', 'u1')"
        )
        repo = corpus_files_repo()
        total = _CORPUS_FILE_PREVIEW_LIMIT + 5
        ids = [
            repo.add(
                corpus_id="col_grant",
                filename=f"g{i}.pdf",
                sha256=str(i) * 8,
                file_type="pdf",
                size_bytes=10,
                storage_path=f"/tmp/g{i}.pdf",
            )
            for i in range(total)
        ]
        preview_ids = {f["id"] for f in repo.list_for_corpus("col_grant", limit=_CORPUS_FILE_PREVIEW_LIMIT)}
        outside_id = next(fid for fid in ids if fid not in preview_ids)

        group_id = system_conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_EVERYONE_GROUP]).fetchone()[
            0
        ]
        resource_grants_repo().create(group_id=group_id, resource_type="corpus_file", resource_id=outside_id)

        block = {b["id"]: b for b in _corpus_file_blocks()}["col_grant"]
        item_ids = {i["resource_id"] for i in block["items"]}
        assert outside_id in item_ids
        assert len(block["items"]) == _CORPUS_FILE_PREVIEW_LIMIT + 1
        assert block["items_total"] == total
        assert block["items_truncated"] is True


class TestCorpusFileSearch:
    """The picker's bounded, on-demand counterpart to the (now capped)
    ``_corpus_file_blocks`` projection above."""

    def test_admin_can_search_by_filename(self, seeded_app, system_conn):
        from src.repositories import corpus_files_repo

        system_conn.execute("INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_s', 's', 'S', 'u1')")
        corpus_files_repo().add(
            corpus_id="col_s",
            filename="quarterly-report.pdf",
            sha256="a" * 8,
            file_type="pdf",
            size_bytes=10,
            storage_path="/tmp/qr.pdf",
        )
        corpus_files_repo().add(
            corpus_id="col_s",
            filename="unrelated.csv",
            sha256="b" * 8,
            file_type="csv",
            size_bytes=10,
            storage_path="/tmp/u.csv",
        )
        r = seeded_app["client"].get(
            "/api/admin/access/resources/corpus_file/search?q=report",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        )
        assert r.status_code == 200
        names = {i["name"] for i in r.json()}
        assert names == {"quarterly-report.pdf"}

    def test_non_admin_is_refused(self, seeded_app):
        r = seeded_app["client"].get(
            "/api/admin/access/resources/corpus_file/search?q=report",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        )
        assert r.status_code == 403

    def test_limit_is_respected(self, seeded_app, system_conn):
        from src.repositories import corpus_files_repo

        system_conn.execute(
            "INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_lim', 'lim', 'Lim', 'u1')"
        )
        repo = corpus_files_repo()
        for i in range(5):
            repo.add(
                corpus_id="col_lim",
                filename=f"doc-{i}.pdf",
                sha256=str(i) * 8,
                file_type="pdf",
                size_bytes=10,
                storage_path=f"/tmp/doc-{i}.pdf",
            )
        r = seeded_app["client"].get(
            "/api/admin/access/resources/corpus_file/search?q=doc&limit=2",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        )
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_a_query_under_two_chars_is_rejected(self, seeded_app):
        r = seeded_app["client"].get(
            "/api/admin/access/resources/corpus_file/search?q=a",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        )
        assert r.status_code == 422

    def test_an_unknown_resource_type_is_rejected(self, seeded_app):
        r = seeded_app["client"].get(
            "/api/admin/access/resources/not_a_real_type/search?q=xx",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        )
        assert r.status_code == 400


class TestMcpSourceResourceType:
    """TCRD-236: ``mcp_source`` makes a registered MCP server a grantable
    resource, ANDed with the existing per-tool ``tool_grants`` gate."""

    def test_mcp_source_in_enum(self):
        assert ResourceType.MCP_SOURCE.value == "mcp_source"

    def test_mcp_source_in_registry(self):
        assert ResourceType.MCP_SOURCE in RESOURCE_TYPES
        spec = RESOURCE_TYPES[ResourceType.MCP_SOURCE]
        assert spec.key is ResourceType.MCP_SOURCE
        assert spec.id_format == "<mcp_source_id>"
        assert callable(spec.list_blocks)

    def test_mcp_source_blocks_empty_when_no_sources(self, system_conn):
        from app.resource_types import _mcp_source_blocks

        assert _mcp_source_blocks() == []

    def test_mcp_source_blocks_project_registered_sources(self, system_conn):
        from app.resource_types import _mcp_source_blocks
        from src.repositories.mcp_sources import MCPSourceRepository

        MCPSourceRepository(system_conn).upsert(
            id="src_a",
            name="upstream-a",
            transport="stdio",
            command="/bin/true",
        )
        blocks = _mcp_source_blocks()
        assert len(blocks) == 1
        items = {i["resource_id"]: i for i in blocks[0]["items"]}
        assert "src_a" in items
        assert items["src_a"]["name"] == "upstream-a"

    def test_mcp_source_blocks_include_disabled_sources(self, system_conn):
        """A disabled source must still be manageable on /admin/access — an
        admin narrowing access to it before re-enabling is a legitimate
        action, unlike the marketplace-plugin projection which hides
        admin-disabled rows."""
        from app.resource_types import _mcp_source_blocks
        from src.repositories.mcp_sources import MCPSourceRepository

        MCPSourceRepository(system_conn).upsert(
            id="src_off",
            name="upstream-off",
            transport="stdio",
            command="/bin/true",
            enabled=False,
        )
        items = {i["resource_id"]: i for i in _mcp_source_blocks()[0]["items"]}
        assert "src_off" in items
        assert items["src_off"]["enabled"] is False
