"""Tests for src.mcp_source_grants — TCRD-236 backward-compat RBAC seeding.

MCP sources predate a grantable resource type of their own; shipping
ResourceType.MCP_SOURCE as a source-level gate would silently drop every
existing MCP connection the moment it boots if nothing grandfathered the
already-registered rows. These tests cover the two seed points that keep
it a no-op until an admin deliberately narrows a specific source: the
idempotent boot-time sweep, and the registration-time seed for a freshly
created row.
"""

from __future__ import annotations

import pytest

from src.db import get_system_db
from src.mcp_source_grants import (
    ensure_default_mcp_source_grant,
    seed_default_mcp_source_grants,
)
from src.repositories.mcp_sources import MCPSourceRepository


@pytest.fixture
def system_conn(seeded_app):
    """seeded_app seeds the system groups (Admin/Everyone) this module
    needs; we just need a raw connection to write mcp_sources rows."""
    conn = get_system_db()
    try:
        yield conn
    finally:
        conn.close()


def _create_source(conn, source_id: str, **kw) -> None:
    kw.setdefault("transport", "stdio")
    kw.setdefault("command", "/bin/true")
    kw.setdefault("name", source_id)
    MCPSourceRepository(conn).upsert(id=source_id, **kw)


class TestSeedDefaultMcpSourceGrants:
    def test_grandfathers_ungranted_existing_source_onto_everyone(self, system_conn):
        _create_source(system_conn, "src_a")

        seed_default_mcp_source_grants()

        from src.repositories import resource_grants_repo, user_groups_repo

        everyone = user_groups_repo().get_by_name("Everyone")
        grants = resource_grants_repo().list_all(resource_type="mcp_source")
        assert any(g["resource_id"] == "src_a" and g["group_id"] == everyone["id"] for g in grants)

    def test_does_not_touch_a_source_an_admin_already_narrowed(self, system_conn):
        """A source that already holds SOME grant (even to a group other than
        Everyone) must be left alone — that is the admin's deliberate
        narrowing, and the seed must never re-open it."""
        _create_source(system_conn, "src_b")
        from src.repositories import resource_grants_repo, user_groups_repo

        ops = user_groups_repo().create("Ops", "narrow grant target")
        resource_grants_repo().ensure_grant(group_id=ops["id"], resource_type="mcp_source", resource_id="src_b")

        seed_default_mcp_source_grants()

        everyone = user_groups_repo().get_by_name("Everyone")
        grants = [g for g in resource_grants_repo().list_all(resource_type="mcp_source") if g["resource_id"] == "src_b"]
        assert len(grants) == 1
        assert grants[0]["group_id"] == ops["id"]
        assert not any(g["group_id"] == everyone["id"] for g in grants)

    def test_idempotent(self, system_conn):
        _create_source(system_conn, "src_c")

        seed_default_mcp_source_grants()
        seed_default_mcp_source_grants()

        from src.repositories import resource_grants_repo

        grants = [g for g in resource_grants_repo().list_all(resource_type="mcp_source") if g["resource_id"] == "src_c"]
        assert len(grants) == 1

    def test_no_op_on_empty_registry(self, system_conn):
        # Must not raise when there are no mcp sources at all.
        seed_default_mcp_source_grants()

    def test_seeds_every_ungranted_source_in_one_sweep(self, system_conn):
        _create_source(system_conn, "src_d1")
        _create_source(system_conn, "src_d2")

        seed_default_mcp_source_grants()

        from src.repositories import resource_grants_repo, user_groups_repo

        everyone = user_groups_repo().get_by_name("Everyone")
        granted_ids = {
            g["resource_id"]
            for g in resource_grants_repo().list_all(resource_type="mcp_source")
            if g["group_id"] == everyone["id"]
        }
        assert {"src_d1", "src_d2"} <= granted_ids


class TestEnsureDefaultMcpSourceGrant:
    def test_grants_everyone_for_a_fresh_source(self, system_conn):
        _create_source(system_conn, "src_fresh")

        ensure_default_mcp_source_grant("src_fresh")

        from src.repositories import resource_grants_repo, user_groups_repo

        everyone = user_groups_repo().get_by_name("Everyone")
        grants = resource_grants_repo().list_all(resource_type="mcp_source")
        assert any(g["resource_id"] == "src_fresh" and g["group_id"] == everyone["id"] for g in grants)

    def test_idempotent_call(self, system_conn):
        _create_source(system_conn, "src_fresh2")

        ensure_default_mcp_source_grant("src_fresh2")
        ensure_default_mcp_source_grant("src_fresh2")

        from src.repositories import resource_grants_repo

        grants = [
            g for g in resource_grants_repo().list_all(resource_type="mcp_source") if g["resource_id"] == "src_fresh2"
        ]
        assert len(grants) == 1


class TestCreateMcpSourceSeedsEveryoneGrant:
    """REST-level: POST /api/admin/mcp-sources seeds the same default a
    freshly-registered source needs post-TCRD-236 — "works like today"
    until an admin narrows it on /admin/access."""

    def test_registering_a_source_grants_everyone(self, seeded_app):
        pytest.importorskip("mcp", reason="mcp SDK not installed")
        client = seeded_app["client"]
        r = client.post(
            "/api/admin/mcp-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
            json={"name": "reg_probe", "transport": "stdio", "command": "/bin/true"},
        )
        assert r.status_code == 201, r.text
        source_id = r.json()["id"]

        from src.repositories import resource_grants_repo, user_groups_repo

        everyone = user_groups_repo().get_by_name("Everyone")
        grants = resource_grants_repo().list_all(resource_type="mcp_source")
        assert any(g["resource_id"] == source_id and g["group_id"] == everyone["id"] for g in grants)
