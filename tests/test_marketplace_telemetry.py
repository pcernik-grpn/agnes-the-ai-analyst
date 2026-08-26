"""Marketplace popularity stats — invocation rollup + sort + Most Popular."""

from __future__ import annotations

import datetime as dt
import json


# ---------------------------------------------------------------------------
# Helpers — reuse seeding pattern from test_marketplace_api.py
# ---------------------------------------------------------------------------


def _seed_curated(user_id: str, marketplace: str, plugin: str) -> None:
    """Seed a curated plugin with RBAC grant for user_id."""
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    conn = get_system_db()
    try:
        exists = conn.execute("SELECT 1 FROM marketplace_registry WHERE id = ?", [marketplace]).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
                [
                    marketplace,
                    marketplace.upper(),
                    f"https://example.test/{marketplace}.git",
                    dt.datetime.now(dt.timezone.utc),
                ],
            )
        conn.execute(
            "INSERT OR IGNORE INTO marketplace_plugins "
            "(marketplace_id, name, description, version, category, raw, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                marketplace,
                plugin,
                "desc",
                "1.0",
                None,
                json.dumps({"name": plugin, "version": "1.0", "description": "desc"}),
                dt.datetime.now(dt.timezone.utc),
            ],
        )
        gname = f"G-{user_id}-{marketplace}-{plugin}"
        gid = UserGroupsRepository(conn).create(name=gname)["id"]
        UserGroupMembersRepository(conn).add_member(user_id, gid, source="admin")
        ResourceGrantsRepository(conn).create(
            group_id=gid,
            resource_type="marketplace_plugin",
            resource_id=f"{marketplace}/{plugin}",
        )
    finally:
        conn.close()


def _seed_rollup(source: str, plugin_name: str, rows: list[tuple]) -> None:
    """Seed marketplace rollup data for `(source, plugin_name)`.

    Writes per-day rows into `usage_marketplace_item_daily` (for the
    trend-pct calculation, which reads daily) AND a single aggregate row
    into `usage_marketplace_item_window` for ``period_label='last_30d'``
    (for the card / detail invocations_30d lookup).

    `rows` is a list of (day_offset, invocations, distinct_users); the
    helper sums them to produce the window snapshot.

    Test data uses ``type='plugin'`` + ``parent_plugin=''`` to model
    curated plugin-level rollup rows (the shape the marketplace cards
    look up).
    """
    from src.db import get_system_db

    # UTC, matching the UTC day-bucketing of the rollup fact and the
    # UTC axis built by _zero_fill_daily_series.
    today = dt.datetime.now(dt.timezone.utc).date()
    conn = get_system_db()
    try:
        type_ = "plugin"
        parent_plugin = ""
        # Per-day daily-fact rows — drive the trend-pct calculation.
        for d_offset, inv, users in rows:
            day = today - dt.timedelta(days=d_offset)
            conn.execute(
                "INSERT OR REPLACE INTO usage_marketplace_item_daily "
                "(day, source, type, parent_plugin, name, count, distinct_users, error_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                [day, source, type_, parent_plugin, plugin_name, inv, users],
            )
        # 30d window snapshot — sum across days within the window. In real
        # data this is a TRUE distinct count from usage_events; tests use
        # sum-of-daily-distinct as an approximation since they control the
        # underlying data shape directly.
        total_inv = sum(inv for offset, inv, _ in rows if offset <= 30)
        total_users = sum(u for offset, _, u in rows if offset <= 30)
        conn.execute(
            "INSERT OR REPLACE INTO usage_marketplace_item_window "
            "(period_label, source, type, parent_plugin, name, invocations, distinct_users) "
            "VALUES ('last_30d', ?, ?, ?, ?, ?, ?)",
            [source, type_, parent_plugin, plugin_name, total_inv, total_users],
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTelemetryDefaults:
    def test_unified_item_has_telemetry_fields_default_zero(self, seeded_app, admin_user):
        """Fresh response (no rollups seeded) returns invocations_30d=0, not omitted."""
        c = seeded_app["client"]
        # Seed one plugin so there is at least one item to check
        _seed_curated("admin1", "telemetry-mp", "no-usage-plugin")
        resp = c.get("/api/marketplace/items?tab=curated&limit=5", headers=admin_user)
        assert resp.status_code == 200
        found = False
        for item in resp.json()["items"]:
            assert "invocations_30d" in item, "missing invocations_30d field"
            assert "distinct_users_30d" in item, "missing distinct_users_30d field"
            assert "trend_pct" in item, "missing trend_pct field"
            assert item["invocations_30d"] == 0
            assert item["distinct_users_30d"] == 0
            assert item["trend_pct"] is None
            found = True
        assert found, "Expected at least one item"


class TestInvocationsReturned:
    def test_invocations_returned_after_rollup_seeded(self, seeded_app, admin_user):
        """Seed rollup for a curated plugin and confirm items endpoint
        returns the correct invocations_30d sum."""
        _seed_curated("admin1", "test-mp", "test-plug")
        # Days 1, 3, 10 ago — all within 30d window
        _seed_rollup(
            "curated",
            "test-plug",
            [
                (1, 100, 10),
                (3, 50, 5),
                (10, 20, 2),
            ],
        )
        c = seeded_app["client"]
        resp = c.get("/api/marketplace/items?tab=curated", headers=admin_user)
        assert resp.status_code == 200
        items = {i["name"]: i for i in resp.json()["items"]}
        assert "test-plug" in items, f"plugin not in response: {list(items)}"
        assert items["test-plug"]["invocations_30d"] == 170  # 100+50+20
        assert items["test-plug"]["distinct_users_30d"] == 17  # 10+5+2

    def test_old_rollups_excluded_from_30d_sum(self, seeded_app, admin_user):
        """Rows older than 30 days must NOT appear in invocations_30d."""
        _seed_curated("admin1", "test-mp2", "old-plug")
        _seed_rollup(
            "curated",
            "old-plug",
            [
                (31, 999, 99),  # outside 30d window
                (1, 5, 1),  # inside
            ],
        )
        c = seeded_app["client"]
        resp = c.get("/api/marketplace/items?tab=curated", headers=admin_user)
        assert resp.status_code == 200
        items = {i["name"]: i for i in resp.json()["items"]}
        assert "old-plug" in items
        assert items["old-plug"]["invocations_30d"] == 5


class TestSortMostUsed:
    def test_sort_most_used_descending(self, seeded_app, admin_user):
        """sort=most_used returns items in descending invocations_30d order."""
        _seed_curated("admin1", "sort-mp", "low-plug")
        _seed_curated("admin1", "sort-mp", "high-plug")
        _seed_rollup("curated", "low-plug", [(1, 10, 1)])
        _seed_rollup("curated", "high-plug", [(1, 500, 50)])

        c = seeded_app["client"]
        resp = c.get(
            "/api/marketplace/items?tab=curated&sort=most_used",
            headers=admin_user,
        )
        assert resp.status_code == 200
        names = [i["name"] for i in resp.json()["items"]]
        # high-plug should come before low-plug
        assert names.index("high-plug") < names.index("low-plug"), f"Expected high-plug before low-plug; got {names}"

    def test_sort_recent_preserves_default_order(self, seeded_app, admin_user):
        """sort=recent (default) doesn't break the existing endpoint contract."""
        _seed_curated("admin1", "order-mp", "alpha-plug")
        c = seeded_app["client"]
        resp1 = c.get(
            "/api/marketplace/items?tab=curated",
            headers=admin_user,
        )
        resp2 = c.get(
            "/api/marketplace/items?tab=curated&sort=recent",
            headers=admin_user,
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert [i["name"] for i in resp1.json()["items"]] == [i["name"] for i in resp2.json()["items"]]


class TestSortTrending:
    def test_sort_trending_excludes_low_prior_invocations(self, seeded_app, admin_user):
        """Items with prior-week invocations < 3 must not appear in trending sort."""
        _seed_curated("admin1", "trend-mp", "noisy-plug")
        # Only recent-week data (prior = 0) — trend_pct is None
        _seed_rollup("curated", "noisy-plug", [(1, 50, 5)])

        c = seeded_app["client"]
        resp = c.get(
            "/api/marketplace/items?tab=curated&sort=trending",
            headers=admin_user,
        )
        assert resp.status_code == 200
        names = [i["name"] for i in resp.json()["items"]]
        assert "noisy-plug" not in names, "noisy-plug should be excluded from trending (prior invocations < 3)"

    def test_sort_trending_includes_item_with_sufficient_prior(self, seeded_app, admin_user):
        """An item with >=3 prior-week invocations must appear in trending sort."""
        _seed_curated("admin1", "trend-mp2", "trend-plug")
        # prior week (8-14 days ago): 12 invocations across 3 days
        # recent week (1-6 days ago): 30 invocations across 3 days
        _seed_rollup(
            "curated",
            "trend-plug",
            [
                (8, 4, 1),
                (10, 4, 1),
                (12, 4, 1),
                (1, 10, 2),
                (3, 10, 2),
                (5, 10, 2),
            ],
        )

        c = seeded_app["client"]
        resp = c.get(
            "/api/marketplace/items?tab=curated&sort=trending",
            headers=admin_user,
        )
        assert resp.status_code == 200
        names = [i["name"] for i in resp.json()["items"]]
        assert "trend-plug" in names, f"trend-plug should appear in trending sort; got {names}"


class TestMostPopularSection:
    def test_most_popular_api_empty_when_no_data(self, seeded_app, admin_user):
        """When no rollups exist, sort=most_used returns items with invocations_30d=0
        — the JS layer uses this to hide the Most Popular section."""
        _seed_curated("admin1", "nodata-mp", "nodata-plug")
        c = seeded_app["client"]
        resp = c.get(
            "/api/marketplace/items?tab=curated&sort=most_used&page_size=8",
            headers=admin_user,
        )
        assert resp.status_code == 200
        items_with_inv = [i for i in resp.json()["items"] if i["invocations_30d"] > 0]
        # No rollups seeded — all zero, JS hides the section
        assert items_with_inv == []

    # test_most_popular_section_placeholder_in_html was retired with the
    # /marketplace browse page itself (folded into /library — the shell now
    # 302s, so there is no template to emit `mp-popular-section`). The API
    # half of the feature is still guarded by the two tests around this
    # comment; a Most Popular shelf inside the Library's kind sections is the
    # seam spec's planned home for the UI half.

    # The "Search in: Curated / Flea Market" scope checkboxes were retired in
    # the redesign — /marketplace is now one unified Browse shelf with a
    # Publisher facet instead (see tests/test_web_marketplace_guide.py +
    # tests/test_store_publisher_verification.py). The former
    # test_search_scope_checkboxes_both_checked_by_default asserted markup that
    # no longer exists and was removed with the feature.

    def test_most_popular_section_visible_with_data(self, seeded_app, admin_user):
        """After seeding usage_plugin_daily, sort=most_used returns items with
        invocations_30d > 0, which the JS uses to un-hide the section."""
        _seed_curated("admin1", "pop-mp", "popular-plug")
        _seed_rollup("curated", "popular-plug", [(1, 100, 10)])

        c = seeded_app["client"]
        resp = c.get(
            "/api/marketplace/items?tab=curated&sort=most_used&page_size=8",
            headers=admin_user,
        )
        assert resp.status_code == 200
        items_with_inv = [i for i in resp.json()["items"] if i["invocations_30d"] > 0]
        assert len(items_with_inv) >= 1, "Expected at least one item with invocations > 0"


class TestDetailTelemetry:
    def test_detail_endpoint_telemetry_absent_when_no_data(self, seeded_app, admin_user):
        """GET /api/marketplace/curated/{mp}/{plugin} returns telemetry=null when
        no rollup data exists."""
        _seed_curated("admin1", "detail-mp", "detail-plug")
        c = seeded_app["client"]
        resp = c.get(
            "/api/marketplace/curated/detail-mp/detail-plug",
            headers=admin_user,
        )
        assert resp.status_code == 200
        assert resp.json()["telemetry"] is None

    def test_detail_endpoint_telemetry_present_with_data(self, seeded_app, admin_user):
        """GET /api/marketplace/curated/{mp}/{plugin} returns telemetry dict with
        invocations_30d and 30-entry daily_series when rollup data exists."""
        _seed_curated("admin1", "detail-mp2", "detail-plug2")
        _seed_rollup(
            "curated",
            "detail-plug2",
            [
                (1, 50, 5),
                (5, 30, 3),
            ],
        )
        c = seeded_app["client"]
        resp = c.get(
            "/api/marketplace/curated/detail-mp2/detail-plug2",
            headers=admin_user,
        )
        assert resp.status_code == 200
        tele = resp.json()["telemetry"]
        assert tele is not None
        assert tele["invocations_30d"] == 80  # 50+30
        assert tele["distinct_users_30d"] == 8  # 5+3
        assert "daily_series" in tele
        assert len(tele["daily_series"]) == 30
        # Each entry has day + invocations
        for entry in tele["daily_series"]:
            assert "day" in entry
            assert "invocations" in entry
        # Total of the series must match invocations_30d
        total = sum(e["invocations"] for e in tele["daily_series"])
        assert total == 80
