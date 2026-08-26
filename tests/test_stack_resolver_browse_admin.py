"""Regression test for ``StackResolver.browse_admin``.

The /catalog (and /memory) page's admin god-mode path used to construct
``ResourceEntry`` instances inline from the repository list, which
silently dropped the v51/v56 enrichment fields (status was kept but
owner_name, owner_team, tags, and derived badges were not). The visible
symptom was: admin opens /catalog, the package they just gave an owner
and tags to renders as a bare card without owner chip, tag pills, or
NEW/curated badges — same package viewed as a non-admin (via
``browse``) rendered them correctly.

``browse_admin`` routes through the same ``_fetch_entries`` enrichment
pass that ``browse`` uses, so admin Browse + non-admin Browse stay
visually consistent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from app.resource_types import ResourceType
from app.services.stack_resolver import StackResolver
from src.db import _ensure_schema


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    _ensure_schema(c)
    # Admin user — created member of the Admin group so badge derivation
    # picks them up as a 'curated' source.
    c.execute(
        "INSERT INTO users(id, email, name) "
        "VALUES ('u_admin', 'admin@example.com', 'Admin User')"
    )
    admin_gid = c.execute(
        "SELECT id FROM user_groups WHERE name = 'Admin'"
    ).fetchone()[0]
    c.execute(
        "INSERT INTO user_group_members(user_id, group_id, source) "
        "VALUES ('u_admin', ?, 'admin')",
        [admin_gid],
    )
    return c


def _seed_pkg(
    c,
    *,
    pkg_id: str,
    name: str,
    owner_name: str | None = None,
    owner_team: str | None = None,
    tags: list[str] | None = None,
    created_by: str | None = None,
    created_at: datetime | None = None,
):
    import json as _json
    c.execute(
        "INSERT INTO data_packages"
        "(id, slug, name, description, icon, color, "
        " status, category, owner_name, owner_team, tags, "
        " created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            pkg_id, pkg_id.lower(), name, "desc", "📦", "#abc",
            "prod", None,
            owner_name, owner_team,
            _json.dumps(tags) if tags is not None else None,
            created_by, created_at,
        ],
    )


class TestBrowseAdminDataPackage:
    def test_returns_all_packages_regardless_of_grants(self, conn):
        """Admin god-mode bypasses resource_grants entirely."""
        _seed_pkg(conn, pkg_id="pkg_a", name="A")
        _seed_pkg(conn, pkg_id="pkg_b", name="B")
        # No grants at all → non-admin browse would return [] but
        # browse_admin sees both packages.
        resolver = StackResolver(conn)
        entries = resolver.browse_admin("u_admin", ResourceType.DATA_PACKAGE)
        assert {e.id for e in entries} == {"pkg_a", "pkg_b"}

    def test_carries_owner_name_owner_team_tags(self, conn):
        """The dropped-on-the-floor fields that motivated this method."""
        _seed_pkg(
            conn,
            pkg_id="pkg_with_owner",
            name="Sales bundle",
            owner_name="Jane Doe",
            owner_team="Sales Ops",
            tags=["Finance", "Revenue", "Margin"],
        )
        resolver = StackResolver(conn)
        entries = resolver.browse_admin("u_admin", ResourceType.DATA_PACKAGE)
        e = next(x for x in entries if x.id == "pkg_with_owner")
        assert e.owner_name == "Jane Doe"
        assert e.owner_team == "Sales Ops"
        assert e.tags == ["Finance", "Revenue", "Margin"]

    def test_derives_new_badge_for_fresh_packages(self, conn):
        """``new`` badge fires when created_at < 30 days ago."""
        fresh = datetime.now(timezone.utc) - timedelta(days=3)
        _seed_pkg(
            conn,
            pkg_id="pkg_fresh",
            name="Fresh",
            created_at=fresh,
        )
        resolver = StackResolver(conn)
        entries = resolver.browse_admin("u_admin", ResourceType.DATA_PACKAGE)
        e = next(x for x in entries if x.id == "pkg_fresh")
        assert "new" in e.badges

    def test_omits_new_badge_for_old_packages(self, conn):
        """Symmetric — packages older than 30 days do NOT get 'new'."""
        old = datetime.now(timezone.utc) - timedelta(days=90)
        _seed_pkg(
            conn,
            pkg_id="pkg_old",
            name="Old",
            created_at=old,
        )
        resolver = StackResolver(conn)
        entries = resolver.browse_admin("u_admin", ResourceType.DATA_PACKAGE)
        e = next(x for x in entries if x.id == "pkg_old")
        assert "new" not in e.badges

    def test_admin_creator_no_longer_derives_a_trust_badge(self, conn):
        """v113: the resolver stopped deriving `curated` from Admin membership.

        It read the creator's membership at render time, so an admin leaving
        the group silently un-curated everything they had created and the card
        then disagreed with its own detail page. The claim is stored now and
        passed through as ``publisher_kind``; ``badges`` carries only what is
        genuinely derived (``new``, from the clock).
        """
        _seed_pkg(
            conn,
            pkg_id="pkg_curated",
            name="Curated",
            created_by="u_admin",  # u_admin is seeded into Admin group
        )
        resolver = StackResolver(conn)
        entries = resolver.browse_admin("u_admin", ResourceType.DATA_PACKAGE)
        e = next(x for x in entries if x.id == "pkg_curated")
        assert "curated" not in e.badges
        # Seeded without an explicit publisher_kind, so it reads as 'user' —
        # membership of the Admin group no longer promotes it.
        assert e.publisher_kind in (None, "user")

    def test_marks_in_stack_from_admin_subscriptions(self, conn):
        """``in_stack`` reflects the admin's own subscriptions — the
        admin-view stack tab depends on this same signal."""
        _seed_pkg(conn, pkg_id="pkg_sub", name="Subscribed")
        _seed_pkg(conn, pkg_id="pkg_unsub", name="Unsubscribed")
        conn.execute(
            "INSERT INTO user_stack_subscriptions(user_id, resource_type, resource_id) "
            "VALUES ('u_admin', 'data_package', 'pkg_sub')"
        )
        resolver = StackResolver(conn)
        entries = resolver.browse_admin("u_admin", ResourceType.DATA_PACKAGE)
        by_id = {e.id: e for e in entries}
        assert by_id["pkg_sub"].in_stack is True
        assert by_id["pkg_unsub"].in_stack is False

    def test_soft_deleted_packages_excluded(self, conn):
        """A package with ``deleted_at`` set must NOT surface in admin
        Browse. The row stays in the DB for the Undo window, but every
        /catalog render mustn't show it (symptom user reported:
        "zobrazujeme i smazane data packages")."""
        from datetime import datetime, timezone as _tz
        _seed_pkg(conn, pkg_id="pkg_live", name="Live")
        _seed_pkg(conn, pkg_id="pkg_gone", name="Tombstoned")
        conn.execute(
            "UPDATE data_packages SET deleted_at = ? WHERE id = ?",
            [datetime.now(_tz.utc), "pkg_gone"],
        )
        resolver = StackResolver(conn)
        entries = resolver.browse_admin("u_admin", ResourceType.DATA_PACKAGE)
        ids = {e.id for e in entries}
        assert "pkg_live" in ids
        assert "pkg_gone" not in ids, (
            "soft-deleted packages must not appear in admin Browse"
        )

    def test_required_grant_propagates_to_requirement_field(self, conn):
        """A required grant on one of the admin's groups must surface as
        ``requirement='required'`` so the macro renders the disabled
        ``In stack (automatic)`` footer instead of an actionable Remove
        button (which would 400 from /api/stack/unsubscribe).

        Regression: previously ``browse_admin`` passed an empty
        ``required_ids`` to ``_fetch_entries`` and every package — even
        ones the admin's own group required — came back as 'available'.
        """
        import uuid
        _seed_pkg(conn, pkg_id="pkg_must", name="Must-have")
        _seed_pkg(conn, pkg_id="pkg_opt", name="Optional")
        # Admin is in Admin group → seed a required grant on that group.
        admin_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = 'Admin'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO resource_grants"
            "(id, group_id, resource_type, resource_id, requirement, "
            " assigned_at, assigned_by) "
            "VALUES (?, ?, 'data_package', 'pkg_must', 'required', "
            "        CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), admin_gid],
        )
        resolver = StackResolver(conn)
        entries = resolver.browse_admin("u_admin", ResourceType.DATA_PACKAGE)
        by_id = {e.id: e for e in entries}
        assert by_id["pkg_must"].requirement == "required"
        # Required ⇒ in_stack=True by convention (the macro relies on this).
        assert by_id["pkg_must"].in_stack is True
        assert by_id["pkg_opt"].requirement == "available"


class TestBrowseAdminMemoryDomain:
    def test_returns_all_memory_domains(self, conn):
        """Memory domains take the same admin god-mode path."""
        # _ensure_schema seeds a 'finance' domain — adequate for the smoke
        # check. We just need to confirm browse_admin walks the domain
        # table without raising and surfaces a Memory Domain entry.
        resolver = StackResolver(conn)
        entries = resolver.browse_admin("u_admin", ResourceType.MEMORY_DOMAIN)
        assert any(e.name == "Finance" for e in entries)


class TestBrowseAdminUnsupportedType:
    def test_raises_for_table_resource_type(self, conn):
        """The method only knows how to enumerate the two resource types
        with their own SELECT branch in ``_fetch_entries``."""
        resolver = StackResolver(conn)
        with pytest.raises(ValueError, match="browse_admin"):
            resolver.browse_admin("u_admin", ResourceType.TABLE)
