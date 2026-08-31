"""Tests for ``app.services.stack_resolver.StackResolver``.

Covers the auto-membership stack model:
* API surface — browse / stack / is_required / add_to_stack /
  remove_from_stack / memory_item_is_required
* Resolution algorithm — stack membership = required ∪ available grants
  (auto, no subscription needed); ``materialized`` = required ∪ subscribed
  (local-download opt-in)
* Required precedence — OR across grants
* Memory item-level Required precedence — per-group MEMORY_ITEM
  override, then global item.is_required
"""

import duckdb
import pytest
from fastapi import HTTPException

from app.resource_types import ResourceType
from app.services.stack_resolver import ResourceEntry, StackResolver
from src.db import _ensure_schema


# -------- Fixtures ---------------------------------------------------------


@pytest.fixture(autouse=True)
def _auto_membership_mode(monkeypatch):
    """This suite pins the AUTO-membership semantics, which are opt-in since
    the classic subscribe model became the default again (spec
    2026-08-07-default-chrome-ux-parity). Classic-mode contracts live in
    tests/test_stack_membership_modes.py."""
    monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    _ensure_schema(c)
    # Seed two groups
    c.execute("INSERT INTO user_groups(id, name) VALUES ('g_sales', 'Sales')")
    c.execute("INSERT INTO user_groups(id, name) VALUES ('g_eng', 'Engineering')")
    # User is in both groups
    c.execute("INSERT INTO user_group_members(user_id, group_id, source) VALUES ('u1', 'g_sales', 'admin')")
    c.execute("INSERT INTO user_group_members(user_id, group_id, source) VALUES ('u1', 'g_eng', 'admin')")
    # Two data packages
    c.execute(
        "INSERT INTO data_packages(id, slug, name, description, icon, color) "
        "VALUES ('pkg_sales', 'sales', 'Sales bundle', 'Sales', '📦', '#abc')"
    )
    c.execute(
        "INSERT INTO data_packages(id, slug, name, description, icon, color) "
        "VALUES ('pkg_compliance', 'compliance', 'Compliance', 'Mandatory', '⚖️', '#fbb')"
    )
    return c


def _grant(conn, group_id, resource_type, resource_id, requirement="available"):
    """Insert one resource_grant row."""
    import uuid

    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), group_id, resource_type, resource_id, requirement],
    )


# -------- Browse + Stack ---------------------------------------------------


class TestBrowse:
    def test_browse_includes_available_grants(self, conn):
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        resolver = StackResolver(conn)
        entries = resolver.browse("u1", ResourceType.DATA_PACKAGE)
        ids = {e.id for e in entries}
        assert "pkg_sales" in ids

    def test_browse_includes_required_grants(self, conn):
        _grant(conn, "g_sales", "data_package", "pkg_compliance", "required")
        resolver = StackResolver(conn)
        entries = resolver.browse("u1", ResourceType.DATA_PACKAGE)
        comp = next(e for e in entries if e.id == "pkg_compliance")
        assert comp.requirement == "required"
        assert comp.in_stack is True  # required entries are always in_stack
        assert comp.materialized is True  # required is always downloaded

    def test_browse_available_grant_is_always_in_stack(self, conn):
        """Auto-membership: an available grant is in_stack immediately,
        with no subscription needed — this replaced the old opt-in model
        where in_stack stayed False until an explicit subscribe."""
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        resolver = StackResolver(conn)
        entries = resolver.browse("u1", ResourceType.DATA_PACKAGE)
        e = next(x for x in entries if x.id == "pkg_sales")
        assert e.in_stack is True

    def test_browse_materialized_reflects_subscription(self, conn):
        """``materialized`` (local-download opt-in) still requires an
        explicit subscribe — only ``in_stack`` is automatic now."""
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        resolver = StackResolver(conn)
        # No subscription yet → not materialized, though already in_stack.
        entries = resolver.browse("u1", ResourceType.DATA_PACKAGE)
        e = next(x for x in entries if x.id == "pkg_sales")
        assert e.in_stack is True
        assert e.materialized is False
        # Subscribe (= "download locally") → materialized flips to True.
        resolver.add_to_stack("u1", ResourceType.DATA_PACKAGE, "pkg_sales")
        entries = resolver.browse("u1", ResourceType.DATA_PACKAGE)
        e = next(x for x in entries if x.id == "pkg_sales")
        assert e.in_stack is True
        assert e.materialized is True


class TestStack:
    def test_stack_includes_available_without_subscription(self, conn):
        """Auto-membership: an available grant is in the effective stack
        immediately, no subscription needed."""
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        resolver = StackResolver(conn)
        entries = list(resolver.stack("u1", ResourceType.DATA_PACKAGE))
        ids = {e.id for e in entries}
        assert "pkg_sales" in ids
        e = next(x for x in entries if x.id == "pkg_sales")
        assert e.in_stack is True
        assert e.materialized is False

    def test_stack_includes_available_with_subscription(self, conn):
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        resolver = StackResolver(conn)
        resolver.add_to_stack("u1", ResourceType.DATA_PACKAGE, "pkg_sales")
        entries = list(resolver.stack("u1", ResourceType.DATA_PACKAGE))
        ids = {e.id for e in entries}
        assert "pkg_sales" in ids
        e = next(x for x in entries if x.id == "pkg_sales")
        assert e.materialized is True

    def test_stack_includes_required_grants(self, conn):
        _grant(conn, "g_sales", "data_package", "pkg_compliance", "required")
        resolver = StackResolver(conn)
        entries = list(resolver.stack("u1", ResourceType.DATA_PACKAGE))
        ids = {e.id for e in entries}
        assert "pkg_compliance" in ids
        e = next(x for x in entries if x.id == "pkg_compliance")
        assert e.materialized is True  # required is always downloaded

    def test_zombie_subscription_filtered_when_no_grant(self, conn):
        """Subscription exists but there is no grant at all — the entry
        is excluded from stack() (it's not authorized, regardless of the
        local-download subscription row)."""
        resolver = StackResolver(conn)
        # Hand-insert a subscription without a corresponding grant.
        conn.execute(
            "INSERT INTO user_stack_subscriptions(user_id, resource_type, resource_id) "
            "VALUES ('u1', 'data_package', 'pkg_zombie')"
        )
        ids = {e.id for e in resolver.stack("u1", ResourceType.DATA_PACKAGE)}
        assert "pkg_zombie" not in ids


# -------- Required precedence ---------------------------------------------


class TestRequiredPrecedence:
    def test_required_wins_when_any_grant_requires(self, conn):
        # g_sales says available; g_eng says required → required wins.
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        _grant(conn, "g_eng", "data_package", "pkg_sales", "required")
        resolver = StackResolver(conn)
        assert resolver.is_required("u1", ResourceType.DATA_PACKAGE, "pkg_sales") is True

    def test_available_only_when_no_required_grant(self, conn):
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        resolver = StackResolver(conn)
        assert resolver.is_required("u1", ResourceType.DATA_PACKAGE, "pkg_sales") is False

    def test_no_grant_means_not_required(self, conn):
        resolver = StackResolver(conn)
        assert resolver.is_required("u1", ResourceType.DATA_PACKAGE, "pkg_other") is False


# -------- add / remove from stack ------------------------------------------


class TestAddToStack:
    def test_add_to_stack_creates_subscription(self, conn):
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        resolver = StackResolver(conn)
        resolver.add_to_stack("u1", ResourceType.DATA_PACKAGE, "pkg_sales")
        row = conn.execute(
            "SELECT 1 FROM user_stack_subscriptions "
            "WHERE user_id='u1' AND resource_type='data_package' "
            "AND resource_id='pkg_sales'"
        ).fetchone()
        assert row is not None

    def test_add_to_required_rejected(self, conn):
        _grant(conn, "g_sales", "data_package", "pkg_compliance", "required")
        resolver = StackResolver(conn)
        with pytest.raises(HTTPException) as exc_info:
            resolver.add_to_stack("u1", ResourceType.DATA_PACKAGE, "pkg_compliance")
        assert exc_info.value.status_code == 400


class TestRemoveFromStack:
    def test_remove_drops_subscription(self, conn):
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        resolver = StackResolver(conn)
        resolver.add_to_stack("u1", ResourceType.DATA_PACKAGE, "pkg_sales")
        resolver.remove_from_stack("u1", ResourceType.DATA_PACKAGE, "pkg_sales")
        row = conn.execute(
            "SELECT 1 FROM user_stack_subscriptions "
            "WHERE user_id='u1' AND resource_type='data_package' "
            "AND resource_id='pkg_sales'"
        ).fetchone()
        assert row is None

    def test_remove_required_rejected(self, conn):
        _grant(conn, "g_sales", "data_package", "pkg_compliance", "required")
        resolver = StackResolver(conn)
        with pytest.raises(HTTPException) as exc_info:
            resolver.remove_from_stack("u1", ResourceType.DATA_PACKAGE, "pkg_compliance")
        assert exc_info.value.status_code == 400


# -------- Memory item-level Required precedence (Section 4.4) --------------


class TestMemoryItemIsRequired:
    """Top-down precedence:
    1) any grant(MEMORY_ITEM, requirement='required')  → True
    2) any grant(MEMORY_ITEM, requirement='available') → False
    3) item.is_required = TRUE                          → True
    4) otherwise                                        → False
    """

    def test_global_flag_true_makes_item_required(self, conn):
        resolver = StackResolver(conn)
        assert resolver.memory_item_is_required("u1", "k1", True) is True

    def test_global_flag_false_keeps_item_not_required(self, conn):
        resolver = StackResolver(conn)
        assert resolver.memory_item_is_required("u1", "k1", False) is False

    def test_per_group_required_overrides_global_false(self, conn):
        _grant(conn, "g_sales", "memory_item", "k1", "required")
        resolver = StackResolver(conn)
        assert resolver.memory_item_is_required("u1", "k1", False) is True

    def test_per_group_available_overrides_global_true(self, conn):
        # Per-group 'available' means "force-NOT-required for this group"
        # — overrides the global is_required=TRUE flag.
        _grant(conn, "g_sales", "memory_item", "k1", "available")
        resolver = StackResolver(conn)
        assert resolver.memory_item_is_required("u1", "k1", True) is False

    def test_required_grant_wins_over_available_grant(self, conn):
        # Same precedence rule as DATA_PACKAGE — OR across grants.
        _grant(conn, "g_sales", "memory_item", "k1", "available")
        _grant(conn, "g_eng", "memory_item", "k1", "required")
        resolver = StackResolver(conn)
        assert resolver.memory_item_is_required("u1", "k1", False) is True


# -------- Browse for MEMORY_DOMAIN ----------------------------------------


class TestBrowseMemoryDomain:
    def test_memory_domain_entries_carry_metadata(self, conn):
        md_id = conn.execute("SELECT id FROM memory_domains WHERE slug='finance'").fetchone()[0]
        _grant(conn, "g_sales", "memory_domain", md_id, "required")
        resolver = StackResolver(conn)
        entries = resolver.browse("u1", ResourceType.MEMORY_DOMAIN)
        e = next(x for x in entries if x.id == md_id)
        assert e.name == "Finance"
        assert e.requirement == "required"


# -------- ResourceEntry shape ----------------------------------------------


class TestResourceEntryShape:
    def test_dataclass_default_in_stack_false(self):
        e = ResourceEntry(id="x", name="X")
        assert e.in_stack is False
        assert e.materialized is False
        assert e.requirement == "available"


# -------- Lifecycle status: draft / coming-soon ----------------------------


class TestLifecycleStatusGating:
    """The ``status`` picker makes two promises the resolver has to keep.

    ``draft`` says "admin-only, hidden from analysts" and ``coming-soon``
    says "visible but not usable yet". Both were presentational only — the
    status drove a pill and a hero filter and nothing else — so a granted
    draft package landed in the analyst's Library fully materialized, and
    ``coming-soon`` delivered parquets like any other row.

    ``browse()``/``stack()``/``_fetch_entries`` are the single chokepoint the
    pull manifest also runs through (``app/api/sync.py`` builds its
    ``data_packages`` section from ``resolver.stack``), so gating here is what
    makes the labels true on every surface at once.
    """

    def test_draft_package_is_hidden_from_browse(self, conn):
        conn.execute("UPDATE data_packages SET status = 'draft' WHERE id = 'pkg_sales'")
        _grant(conn, "g_sales", "data_package", "pkg_sales", "required")
        entries = StackResolver(conn).browse("u1", ResourceType.DATA_PACKAGE)
        assert "pkg_sales" not in {e.id for e in entries}

    def test_draft_package_is_absent_from_stack(self, conn):
        """The stack feeds the pull manifest — a draft must not deliver."""
        conn.execute("UPDATE data_packages SET status = 'draft' WHERE id = 'pkg_sales'")
        _grant(conn, "g_sales", "data_package", "pkg_sales", "required")
        entries = StackResolver(conn).stack("u1", ResourceType.DATA_PACKAGE)
        assert "pkg_sales" not in {e.id for e in entries}

    def test_non_draft_package_is_unaffected(self, conn):
        """The gate is narrow: prod is the default and must not be touched."""
        _grant(conn, "g_sales", "data_package", "pkg_sales", "required")
        entries = StackResolver(conn).browse("u1", ResourceType.DATA_PACKAGE)
        assert "pkg_sales" in {e.id for e in entries}

    def test_coming_soon_is_visible_but_never_materialized(self, conn):
        """'Visible but not usable yet' — it browses, it does not download."""
        conn.execute("UPDATE data_packages SET status = 'coming-soon' WHERE id = 'pkg_sales'")
        _grant(conn, "g_sales", "data_package", "pkg_sales", "required")
        entries = StackResolver(conn).browse("u1", ResourceType.DATA_PACKAGE)
        entry = next(e for e in entries if e.id == "pkg_sales")
        assert entry.materialized is False

    def test_coming_soon_is_absent_from_stack(self, conn):
        """Not usable yet means the manifest must not carry it either."""
        conn.execute("UPDATE data_packages SET status = 'coming-soon' WHERE id = 'pkg_sales'")
        _grant(conn, "g_sales", "data_package", "pkg_sales", "required")
        entries = StackResolver(conn).stack("u1", ResourceType.DATA_PACKAGE)
        assert "pkg_sales" not in {e.id for e in entries}

    def test_browse_admin_still_shows_draft(self, conn):
        """Admins author drafts, so their own Browse must keep showing them."""
        conn.execute("UPDATE data_packages SET status = 'draft' WHERE id = 'pkg_sales'")
        entries = StackResolver(conn).browse_admin("u1", ResourceType.DATA_PACKAGE)
        assert "pkg_sales" in {e.id for e in entries}

    def test_draft_is_hidden_from_a_principal_intersection(self, conn):
        """An agent inherits its owner's reach, so it must not see a draft
        the owner cannot see either."""
        conn.execute("UPDATE data_packages SET status = 'draft' WHERE id = 'pkg_sales'")

        class _P:
            intersection = {"data_package": frozenset({"pkg_sales"})}

        import app.auth.session_principal as sp

        original = sp.PRINCIPAL_TYPES
        sp.PRINCIPAL_TYPES = (_P,)
        try:
            entries = StackResolver(conn).stack(_P(), ResourceType.DATA_PACKAGE)
        finally:
            sp.PRINCIPAL_TYPES = original
        assert "pkg_sales" not in {e.id for e in entries}


class TestSubscribeHonoursStatus:
    """The gate above hides a status from every READ. Subscribing is the write
    that was left behind, and it was the one a member could actually reach.

    A subscription is a request for a local copy, and ``stack()`` — which
    builds the pull manifest — refuses to carry a hidden or undeliverable
    status. So the write returned ``{"subscribed": true}`` for a download that
    could never arrive: no error, no row in the manifest, and nothing on any
    screen to explain the silence. Refusing in ``add_to_stack`` rather than in
    the endpoint is what makes it true of all four surfaces at once (Library,
    chat, CLI, MCP), since every one of them subscribes through this method.
    """

    def test_subscribe_to_a_draft_is_a_404(self, conn):
        """404, not 403 — every read behaves as though a draft is not there,
        and the write has no business being the one surface that admits it
        exists."""
        import pytest
        from fastapi import HTTPException

        conn.execute("UPDATE data_packages SET status = 'draft' WHERE id = 'pkg_sales'")
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        with pytest.raises(HTTPException) as exc:
            StackResolver(conn).add_to_stack("u1", ResourceType.DATA_PACKAGE, "pkg_sales")
        assert exc.value.status_code == 404

    def test_subscribe_to_coming_soon_is_a_409(self, conn):
        """'Visible but not usable yet' is a different answer from 'not there':
        the caller found a real thing and asked too early, so the refusal says
        so and the id stays namable."""
        import pytest
        from fastapi import HTTPException

        conn.execute("UPDATE data_packages SET status = 'coming-soon' WHERE id = 'pkg_sales'")
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        with pytest.raises(HTTPException) as exc:
            StackResolver(conn).add_to_stack("u1", ResourceType.DATA_PACKAGE, "pkg_sales")
        assert exc.value.status_code == 409
        assert exc.value.detail == "not_available_yet"

    def test_prod_still_subscribes(self, conn):
        """The gate is narrow — the ordinary path must be untouched."""
        _grant(conn, "g_sales", "data_package", "pkg_sales", "available")
        StackResolver(conn).add_to_stack("u1", ResourceType.DATA_PACKAGE, "pkg_sales")
        entries = StackResolver(conn).stack("u1", ResourceType.DATA_PACKAGE)
        assert next(e for e in entries if e.id == "pkg_sales").materialized is True

    def test_a_type_without_a_status_is_not_blocked(self, conn):
        """Only packages and domains carry a status. Every other type must
        subscribe as before rather than trip a lookup that does not apply."""
        _grant(conn, "g_sales", "collection", "col_1", "available")
        StackResolver(conn).add_to_stack("u1", ResourceType.COLLECTION, "col_1")
