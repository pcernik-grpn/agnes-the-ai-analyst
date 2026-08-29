"""Tests for ``GET /api/memory/bundle?domain=<slug>`` (Task 7.5).

The per-domain variant returns ``text/markdown`` and is RBAC-gated on
``MEMORY_DOMAIN`` grants (admins bypass via ``can_access``). It's what
``agnes pull`` writes to ``~/.claude/memory/<slug>/bundle.md``.
"""

from __future__ import annotations

import uuid

from src.db import get_system_db
from src.repositories.knowledge import KnowledgeRepository
from src.repositories.memory_domains import MemoryDomainsRepository
from src.repositories.resource_grants import ResourceGrantsRepository
from src.repositories.user_group_members import UserGroupMembersRepository


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_domain(slug: str, name: str = "T") -> str:
    conn = get_system_db()
    repo = MemoryDomainsRepository(conn)
    try:
        existing = repo.get_by_slug(slug)
        if existing:
            return existing["id"]
        domain_id = repo.create(
            name=name,
            slug=slug,
            description="Test domain",
            icon=None,
            color=None,
            created_by="test",
        )
    finally:
        conn.close()
    return domain_id


def _create_item_in_domain(
    domain_id: str,
    title: str,
    content: str,
    *,
    status: str = "approved",
    is_required: bool = False,
) -> str:
    conn = get_system_db()
    try:
        item_id = "ki_" + uuid.uuid4().hex[:8]
        KnowledgeRepository(conn).create(
            id=item_id,
            title=title,
            content=content,
            category="engineering",
            status=status,
            is_required=is_required,
        )
        MemoryDomainsRepository(conn).add_item(domain_id, item_id, added_by="test")
    finally:
        conn.close()
    return item_id


def _grant_user_group_access_to_domain(domain_id: str, group_name: str = "Everyone", *, user_id: str = "analyst1"):
    conn = get_system_db()
    try:
        gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()[0]
        ResourceGrantsRepository(conn).create(
            group_id=gid,
            resource_type="memory_domain",
            resource_id=domain_id,
            assigned_by="test",
        )
        # The seeded_app fixture creates users AFTER the schema migration's
        # Everyone backfill, so they aren't members of any group yet. Add
        # them explicitly so the grant resolves through the user.
        UserGroupMembersRepository(conn).add_member(
            user_id,
            gid,
            source="test",
        )
    finally:
        conn.close()


def _upvote(item_id: str, user_id: str) -> None:
    conn = get_system_db()
    try:
        KnowledgeRepository(conn).vote(item_id, user_id, 1)
    finally:
        conn.close()


class TestPerDomainBundle:
    def test_admin_can_fetch_any_domain(self, seeded_app):
        c = seeded_app["client"]
        dom_id = _create_domain("phase7-admin", "Admin Domain")
        _create_item_in_domain(dom_id, "T1", "Body 1", is_required=True)
        t2_id = _create_item_in_domain(dom_id, "T2", "Body 2")
        # #1573: default distribution_mode (hybrid) gates non-required
        # approved items behind a personal upvote — vote as the caller so
        # this test still exercises both the "Required" and "Approved"
        # sections rendering, not just required-only.
        _upvote(t2_id, "admin1")

        resp = c.get(
            "/api/memory/bundle?domain=phase7-admin",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/markdown")
        body = resp.text
        assert "Admin Domain" in body
        assert "T1" in body
        assert "T2" in body
        assert "Required" in body
        assert "Approved" in body

    def test_user_with_grant_can_fetch(self, seeded_app):
        c = seeded_app["client"]
        dom_id = _create_domain("phase7-granted", "Granted Domain")
        item_id = _create_item_in_domain(dom_id, "Granted Item", "Body")
        _grant_user_group_access_to_domain(dom_id, "Everyone")
        # #1573: hybrid mode's optional channel is personal opt-in via vote.
        _upvote(item_id, "analyst1")

        resp = c.get(
            "/api/memory/bundle?domain=phase7-granted",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        assert "Granted Item" in resp.text

    def test_user_without_grant_gets_403(self, seeded_app):
        c = seeded_app["client"]
        dom_id = _create_domain("phase7-private", "Private Domain")
        _create_item_in_domain(dom_id, "Private", "Body")

        # Make sure analyst is not in any admin group AND no resource_grants
        # row exists on this domain → can_access returns False.
        # Remove analyst from any non-Everyone groups (none should exist
        # initially, but be defensive).
        resp = c.get(
            "/api/memory/bundle?domain=phase7-private",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_unknown_slug_404(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/memory/bundle?domain=does-not-exist",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404

    def test_empty_domain_renders_placeholder(self, seeded_app):
        c = seeded_app["client"]
        _create_domain("phase7-empty", "Empty Domain")
        resp = c.get(
            "/api/memory/bundle?domain=phase7-empty",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert "_No items" in resp.text

    def test_legacy_no_domain_param_still_returns_json(self, seeded_app):
        """Backward-compat: bare /api/memory/bundle still returns JSON."""
        c = seeded_app["client"]
        resp = c.get(
            "/api/memory/bundle",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        # No domain → original JSON shape.
        data = resp.json()
        assert "mandatory" in data
        assert "approved" in data
        assert "token_budget" in data
