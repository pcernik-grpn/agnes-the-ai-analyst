"""End-to-end coverage for the v39 system plugin tier.

The feature reuses the existing RBAC + subscription tables — marking a
plugin as "system" simply materializes resource_grants + user_plugin_optouts
rows for every existing user_groups + users row, then locks the
corresponding admin/user controls. The resolver itself is unchanged.

Tests in this module exercise:

* mark/unmark endpoints — happy path, idempotency, audit row, fanout count
* refusal of the bypass paths — DELETE grant, unsubscribe, uninstall
* creation hooks — new user / new group inherit the mandatory tier
* sync preservation — a re-sync of the marketplace doesn't reset is_system

Mirrors the helper pattern in ``test_marketplace_api.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db
    close_system_db()
    from app.main import create_app
    app = create_app()
    yield TestClient(app)
    close_system_db()


def _create_user(client, email, password="UserPass1!", admin: bool = False):
    """Create a user and return (user_id, cookies). When ``admin=True``
    the user is added to the seeded Admin system group so
    ``require_admin`` passes."""
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(
        id=user_id, email=email, name=user_id, password_hash=ph.hash(password),
    )
    if admin:
        from tests.helpers.auth import grant_admin
        grant_admin(conn, user_id)
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


def _seed_marketplace_with_plugin(
    *,
    marketplace: str = "mkt-x",
    plugin: str = "alpha",
) -> None:
    """Insert a marketplace + plugin row directly. We bypass the git
    sync path here because none of the system-flag behavior depends on
    plugin content — it's purely a flag + materialization story."""
    from src.db import get_system_db
    conn = get_system_db()
    try:
        existing = conn.execute(
            "SELECT 1 FROM marketplace_registry WHERE id = ?", [marketplace],
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO marketplace_registry (id, name, url, registered_at) "
                "VALUES (?, ?, ?, ?)",
                [marketplace, marketplace.upper(),
                 f"https://example.test/{marketplace}.git",
                 datetime.now(timezone.utc)],
            )
        meta = {"name": plugin, "version": "1.0", "description": "desc"}
        conn.execute(
            "INSERT INTO marketplace_plugins "
            "(marketplace_id, name, description, version, raw, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (marketplace_id, name) DO NOTHING",
            [marketplace, plugin, meta["description"], meta["version"],
             json.dumps(meta), datetime.now(timezone.utc)],
        )
    finally:
        conn.close()


def _add_group(name: str = "engineers") -> str:
    """Create a non-system group and return its id. Mark on this group
    fans out a grant; cleanup on unmark leaves it intact."""
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository
    conn = get_system_db()
    try:
        return UserGroupsRepository(conn).create(name=name)["id"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mark / Unmark endpoint behavior
# ---------------------------------------------------------------------------


class TestMarkUnmark:
    def test_mark_404_when_plugin_missing(self, web_client):
        _, cookies = _create_user(web_client, "admin@x.com", admin=True)
        r = web_client.post(
            "/api/marketplaces/missing/plugins/ghost/system",
            cookies=cookies,
        )
        assert r.status_code == 404

    def test_mark_requires_admin(self, web_client):
        _seed_marketplace_with_plugin()
        _, cookies = _create_user(web_client, "user@x.com", admin=False)
        r = web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=cookies,
        )
        # require_admin returns 403 on non-admins
        assert r.status_code in (401, 403)

    def test_mark_flips_flag_and_fans_out(self, web_client):
        """After mark, every existing user has a subscription row and
        every existing group has a grant row for the marked plugin."""
        _seed_marketplace_with_plugin()
        admin_id, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        # Pre-existing non-admin user + custom group so we can observe
        # both fanout dimensions.
        regular_id, _ = _create_user(web_client, "regular@x.com")
        gid = _add_group("engineers")

        r = web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_system"] is True
        # affected_users counts NEW subscription rows — both admin and
        # regular start un-subscribed, so we expect at least both, plus
        # the seeded scheduler service user that the app may have
        # bootstrapped during create_app.
        assert body["affected_users"] >= 2

        from src.db import get_system_db
        conn = get_system_db()
        try:
            row = conn.execute(
                "SELECT is_system FROM marketplace_plugins "
                "WHERE marketplace_id = 'mkt-x' AND name = 'alpha'"
            ).fetchone()
            assert row[0] is True

            # Subscription row exists for both users.
            for uid in (admin_id, regular_id):
                sub = conn.execute(
                    "SELECT 1 FROM user_plugin_optouts "
                    "WHERE user_id = ? AND marketplace_id = 'mkt-x' "
                    "AND plugin_name = 'alpha'",
                    [uid],
                ).fetchone()
                assert sub is not None, f"subscription missing for {uid}"

            # Grant row exists for engineers + Admin + Everyone (system seeded).
            grant_groups = {
                r[0] for r in conn.execute(
                    "SELECT group_id FROM resource_grants "
                    "WHERE resource_type = 'marketplace_plugin' "
                    "AND resource_id = 'mkt-x/alpha'",
                ).fetchall()
            }
            assert gid in grant_groups, "engineers group never received fanout grant"
        finally:
            conn.close()

    def test_mark_is_idempotent(self, web_client):
        _seed_marketplace_with_plugin()
        _, cookies = _create_user(web_client, "admin@x.com", admin=True)
        first = web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system", cookies=cookies,
        )
        assert first.status_code == 200
        # Second call must succeed and report 0 newly affected — every
        # row was already in place.
        second = web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system", cookies=cookies,
        )
        assert second.status_code == 200
        assert second.json()["affected_users"] == 0
        assert second.json()["affected_groups"] == 0

    def test_unmark_flips_flag_but_keeps_rows(self, web_client):
        """The agreed semantic: unmark only flips the flag. Existing
        grants and subscriptions persist so a confused click doesn't
        rip the plugin out of every user's stack mid-day."""
        _seed_marketplace_with_plugin()
        _, cookies = _create_user(web_client, "admin@x.com", admin=True)
        _, _ = _create_user(web_client, "regular@x.com")
        gid = _add_group("engineers")

        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system", cookies=cookies,
        )
        r = web_client.delete(
            "/api/marketplaces/mkt-x/plugins/alpha/system", cookies=cookies,
        )
        assert r.status_code == 204

        from src.db import get_system_db
        conn = get_system_db()
        try:
            row = conn.execute(
                "SELECT is_system FROM marketplace_plugins "
                "WHERE marketplace_id = 'mkt-x' AND name = 'alpha'"
            ).fetchone()
            assert row[0] is False

            # Subscription rows survive — admin curates cleanup later.
            count = conn.execute(
                "SELECT COUNT(*) FROM user_plugin_optouts "
                "WHERE marketplace_id = 'mkt-x' AND plugin_name = 'alpha'",
            ).fetchone()[0]
            assert count >= 2, "subscriptions should persist past unmark"

            # Grant for engineers survives too.
            grant = conn.execute(
                "SELECT 1 FROM resource_grants "
                "WHERE group_id = ? AND resource_type = 'marketplace_plugin' "
                "AND resource_id = 'mkt-x/alpha'",
                [gid],
            ).fetchone()
            assert grant is not None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Bypass-path guards
# ---------------------------------------------------------------------------


class TestGuards:
    def test_unsubscribe_via_my_stack_refused(self, web_client):
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )
        # require_admin grants the admin access via the Admin group seed,
        # so they'll see the plugin in their stack and can attempt the
        # toggle. The guard should refuse.
        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": False}, cookies=admin_cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "cannot_unsubscribe_system_plugin"

    def test_uninstall_via_marketplace_refused(self, web_client):
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )
        r = web_client.delete(
            "/api/marketplace/curated/mkt-x/alpha/install",
            cookies=admin_cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "cannot_uninstall_system_plugin"

    def test_grant_delete_refused(self, web_client):
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        gid = _add_group("engineers")
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )

        # Find the engineers group's grant for this plugin.
        from src.db import get_system_db
        conn = get_system_db()
        try:
            grant_id = conn.execute(
                "SELECT id FROM resource_grants "
                "WHERE group_id = ? AND resource_type = 'marketplace_plugin' "
                "AND resource_id = 'mkt-x/alpha'",
                [gid],
            ).fetchone()[0]
        finally:
            conn.close()

        r = web_client.delete(
            f"/api/admin/grants/{grant_id}", cookies=admin_cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "cannot_revoke_system_grant"

    def test_group_delete_with_system_grant_returns_clear_error_not_500(
        self, web_client,
    ):
        """A custom group with an auto-materialized system-plugin grant must
        be deletable (or at least fail with a clear 4xx, not 500). Empirical:
        DELETE /api/admin/groups/<id> returns 500 — the in-handler cascade
        deletes resource_grants explicitly first, but the system grant for
        marketplace_plugin/<marketplace>/<plugin> survives or the server
        rejects elsewhere, and the operator is stuck with a group that
        cannot be removed via the CLI or API."""
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )
        gid = _add_group("doomed-by-system-grant")

        # Sanity-check the auto-grant landed.
        from src.db import get_system_db
        conn = get_system_db()
        try:
            grants = conn.execute(
                "SELECT id, resource_type, resource_id FROM resource_grants "
                "WHERE group_id = ?",
                [gid],
            ).fetchall()
        finally:
            conn.close()
        assert any(
            g[1] == "marketplace_plugin" and g[2] == "mkt-x/alpha"
            for g in grants
        ), f"system grant didn't auto-materialize for new group: {grants}"

        r = web_client.delete(
            f"/api/admin/groups/{gid}", cookies=admin_cookies,
        )
        # The point: the handler MUST not 500. Either it succeeds (cascade
        # took care of the system grant — per-group rows are safe to drop,
        # and the next group to be created will re-materialize) or it
        # refuses with a 4xx + actionable detail. 500 leaves the operator
        # stuck with an undeletable group on shared dev.
        assert r.status_code != 500, (
            f"group delete returned 500 for a group with a system grant; "
            f"body={r.text}"
        )

    def test_subscribe_via_my_stack_still_allowed(self, web_client):
        """The guard refuses unsubscribe only — explicit subscribe must
        keep working since the row already exists (idempotent)."""
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )
        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": True}, cookies=admin_cookies,
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Creation hooks
# ---------------------------------------------------------------------------


class TestCreationHooks:
    def test_new_group_inherits_grant(self, web_client):
        """A group created AFTER mark gets the system grant via
        ResourceGrantsRepository.fanout_system_for_group, called from
        UserGroupsRepository.create()."""
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )

        # Create a brand-new group via the admin POST endpoint.
        r = web_client.post(
            "/api/admin/groups",
            json={"name": "post-mark-group", "description": "test"},
            cookies=admin_cookies,
        )
        assert r.status_code in (200, 201), r.text
        new_gid = r.json()["id"]

        from src.db import get_system_db
        conn = get_system_db()
        try:
            grant = conn.execute(
                "SELECT 1 FROM resource_grants "
                "WHERE group_id = ? AND resource_type = 'marketplace_plugin' "
                "AND resource_id = 'mkt-x/alpha'",
                [new_gid],
            ).fetchone()
            assert grant is not None, "new group did not inherit system grant"
        finally:
            conn.close()

    def test_new_user_inherits_subscription(self, web_client):
        """A user created via the admin POST endpoint AFTER mark gets a
        subscription row via fanout_system_for_user."""
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        web_client.post(
            "/api/marketplaces/mkt-x/plugins/alpha/system",
            cookies=admin_cookies,
        )

        r = web_client.post(
            "/api/users",
            json={
                "email": "fresh@example.com",
                "name": "Fresh",
                "send_invite": False,
            },
            cookies=admin_cookies,
        )
        assert r.status_code in (200, 201), r.text
        new_uid = r.json()["id"]

        from src.db import get_system_db
        conn = get_system_db()
        try:
            sub = conn.execute(
                "SELECT 1 FROM user_plugin_optouts "
                "WHERE user_id = ? AND marketplace_id = 'mkt-x' "
                "AND plugin_name = 'alpha'",
                [new_uid],
            ).fetchone()
            assert sub is not None, "new user did not inherit subscription"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Sync preservation
# ---------------------------------------------------------------------------


def test_resync_preserves_is_system(tmp_path, monkeypatch):
    """``replace_for_marketplace`` re-runs every sync. The is_system
    flag MUST survive — it's not in the ON CONFLICT DO UPDATE SET list
    and not in the INSERT VALUES list. Test by faking a sync via the
    repo with the same plugin name.

    Uses ``tmp_path`` directly (no web_client) because the test only
    exercises the repo, not any API surface — but we still need a
    fresh DATA_DIR so we don't inherit state from a sibling test that
    populated ``store_entities`` etc. through the migration ladder.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "analytics").mkdir(exist_ok=True)
    (tmp_path / "extracts").mkdir(exist_ok=True)
    from src.db import close_system_db, get_system_db
    from src.repositories.marketplace_plugins import MarketplacePluginsRepository
    close_system_db()
    conn = get_system_db()
    try:
        # Set up registry + initial plugin.
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            ["resync-test", "Resync", "https://example.test/r.git",
             datetime.now(timezone.utc)],
        )
        repo = MarketplacePluginsRepository(conn)
        repo.replace_for_marketplace(
            "resync-test",
            [{"name": "alpha", "version": "1.0", "description": "v1"}],
        )

        # Mark as system.
        conn.execute(
            "UPDATE marketplace_plugins SET is_system = TRUE "
            "WHERE marketplace_id = 'resync-test' AND name = 'alpha'"
        )

        # Re-sync with updated description.
        repo.replace_for_marketplace(
            "resync-test",
            [{"name": "alpha", "version": "2.0", "description": "v2-updated"}],
        )

        row = conn.execute(
            "SELECT is_system, version, description FROM marketplace_plugins "
            "WHERE marketplace_id = 'resync-test' AND name = 'alpha'"
        ).fetchone()
        assert row[0] is True, "is_system was reset by resync"
        assert row[1] == "2.0"
        assert row[2] == "v2-updated"
    finally:
        conn.close()
        close_system_db()


# ---------------------------------------------------------------------------
# Disabled-plugin invariants at the HTTP boundary
# ---------------------------------------------------------------------------


def test_mark_system_rejected_when_disabled(web_client):
    """A disabled plugin cannot be marked system (409). Disabling clears
    is_system and re-enabling does not restore it, so the backend must reject
    a mark on a disabled plugin — otherwise re-enable would resurrect it as a
    mandatory default. The UI greys the button out, but this endpoint is the
    real state boundary (direct API call / stale-modal race)."""
    _seed_marketplace_with_plugin()
    _, cookies = _create_user(web_client, "admin@x.com", admin=True)

    dis = web_client.post(
        "/api/marketplaces/mkt-x/plugins/alpha/disable", cookies=cookies,
    )
    assert dis.status_code == 200, dis.text

    r = web_client.post(
        "/api/marketplaces/mkt-x/plugins/alpha/system", cookies=cookies,
    )
    assert r.status_code == 409, r.text

    from src.db import get_system_db
    conn = get_system_db()
    try:
        row = conn.execute(
            "SELECT is_system FROM marketplace_plugins "
            "WHERE marketplace_id = 'mkt-x' AND name = 'alpha'"
        ).fetchone()
        assert not row[0], "is_system must stay false on a rejected mark"
    finally:
        conn.close()


def test_get_plugins_exposes_admin_disabled(web_client):
    """GET /plugins surfaces admin_disabled so the Details modal can render the
    switch + DISABLED pill. The flag must move False -> True -> False across
    disable / enable."""
    _seed_marketplace_with_plugin()
    _, cookies = _create_user(web_client, "admin@x.com", admin=True)

    def _flag():
        r = web_client.get("/api/marketplaces/mkt-x/plugins", cookies=cookies)
        assert r.status_code == 200, r.text
        row = next(p for p in r.json() if p["name"] == "alpha")
        return row["admin_disabled"]

    assert _flag() is False
    web_client.post("/api/marketplaces/mkt-x/plugins/alpha/disable", cookies=cookies)
    assert _flag() is True
    web_client.post("/api/marketplaces/mkt-x/plugins/alpha/enable", cookies=cookies)
    assert _flag() is False


def test_delete_marketplace_cascades_through_factory(web_client):
    """DELETE /api/marketplaces/{id} must remove the registry row AND cascade
    the plugin rows, plugin grants, and subscriptions through the active
    backend — the route-wiring layer of the backend-split fix, above the
    per-repo contract tests."""
    _seed_marketplace_with_plugin()
    admin_id, cookies = _create_user(web_client, "admin@x.com", admin=True)
    gid = _add_group("engineers")

    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_curated_subscriptions import (
        UserCuratedSubscriptionsRepository,
    )
    conn = get_system_db()
    try:
        ResourceGrantsRepository(conn).ensure_grant(
            gid, "marketplace_plugin", "mkt-x/alpha", "test",
        )
        UserCuratedSubscriptionsRepository(conn).subscribe(admin_id, "mkt-x", "alpha")
    finally:
        conn.close()

    r = web_client.delete("/api/marketplaces/mkt-x", cookies=cookies)
    assert r.status_code == 204, r.text

    conn = get_system_db()
    try:
        assert conn.execute(
            "SELECT 1 FROM marketplace_registry WHERE id = 'mkt-x'"
        ).fetchone() is None, "registry row survived delete"
        assert conn.execute(
            "SELECT 1 FROM marketplace_plugins WHERE marketplace_id = 'mkt-x'"
        ).fetchone() is None, "plugin rows orphaned after delete"
        assert conn.execute(
            "SELECT 1 FROM resource_grants WHERE resource_id = 'mkt-x/alpha'"
        ).fetchone() is None, "plugin grant orphaned after delete"
        assert conn.execute(
            "SELECT 1 FROM user_plugin_optouts WHERE marketplace_id = 'mkt-x'"
        ).fetchone() is None, "subscription orphaned after delete"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Disable + revoke_grants (one-click retirement)
# ---------------------------------------------------------------------------


def test_disable_with_revoke_grants_removes_all_plugin_grants(web_client):
    """POST /disable with ``{"revoke_grants": true}`` drops every group grant
    on the plugin in the same action — the one-click retirement path. Grants
    on OTHER plugins must survive, including same-group ones."""
    _seed_marketplace_with_plugin()
    _seed_marketplace_with_plugin(marketplace="mkt-x", plugin="beta")
    _, cookies = _create_user(web_client, "admin@x.com", admin=True)
    g1 = _add_group("engineers")
    g2 = _add_group("analysts")

    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    conn = get_system_db()
    try:
        repo = ResourceGrantsRepository(conn)
        repo.ensure_grant(g1, "marketplace_plugin", "mkt-x/alpha", "test")
        repo.ensure_grant(g2, "marketplace_plugin", "mkt-x/alpha", "test")
        repo.ensure_grant(g1, "marketplace_plugin", "mkt-x/beta", "test")
    finally:
        conn.close()

    r = web_client.post(
        "/api/marketplaces/mkt-x/plugins/alpha/disable",
        json={"revoke_grants": True},
        cookies=cookies,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["admin_disabled"] is True
    assert body["revoked_grants"] == 2

    conn = get_system_db()
    try:
        assert conn.execute(
            "SELECT 1 FROM resource_grants WHERE resource_id = 'mkt-x/alpha'"
        ).fetchone() is None, "alpha grants survived revoke_grants"
        assert conn.execute(
            "SELECT 1 FROM resource_grants WHERE resource_id = 'mkt-x/beta'"
        ).fetchone() is not None, "beta grant must survive alpha's retirement"
        row = conn.execute(
            "SELECT admin_disabled FROM marketplace_plugins "
            "WHERE marketplace_id = 'mkt-x' AND name = 'alpha'"
        ).fetchone()
        assert row[0] is True
    finally:
        conn.close()


def test_disable_without_body_keeps_grants(web_client):
    """Plain POST /disable (the pre-existing no-body call shape) must keep
    grants intact — disabling stays reversible; retirement is opt-in."""
    _seed_marketplace_with_plugin()
    _, cookies = _create_user(web_client, "admin@x.com", admin=True)
    gid = _add_group("engineers")

    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    conn = get_system_db()
    try:
        ResourceGrantsRepository(conn).ensure_grant(
            gid, "marketplace_plugin", "mkt-x/alpha", "test",
        )
    finally:
        conn.close()

    r = web_client.post(
        "/api/marketplaces/mkt-x/plugins/alpha/disable", cookies=cookies,
    )
    assert r.status_code == 200, r.text
    assert r.json().get("revoked_grants", 0) == 0

    conn = get_system_db()
    try:
        assert conn.execute(
            "SELECT 1 FROM resource_grants WHERE resource_id = 'mkt-x/alpha'"
        ).fetchone() is not None, "plain disable must NOT touch grants"
    finally:
        conn.close()


def test_get_plugins_exposes_upstream_deprecation(web_client, tmp_path):
    """GET /plugins surfaces the curator-side deprecation fields (read
    on-demand from the cloned repo's marketplace-metadata.json) so the admin
    Details modal can render the DEPRECATED pill + note."""
    _seed_marketplace_with_plugin()
    _seed_marketplace_with_plugin(marketplace="mkt-x", plugin="beta")
    _, cookies = _create_user(web_client, "admin@x.com", admin=True)

    meta_dir = tmp_path / "marketplaces" / "mkt-x" / ".claude-plugin"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "marketplace-metadata.json").write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": {
                    "alpha": {
                        "deprecated": True,
                        "deprecation_note": "generates unreliable output",
                        "replacement": "beta",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    r = web_client.get("/api/marketplaces/mkt-x/plugins", cookies=cookies)
    assert r.status_code == 200, r.text
    rows = {p["name"]: p for p in r.json()}
    assert rows["alpha"]["deprecated"] is True
    assert rows["alpha"]["deprecation_note"] == "generates unreliable output"
    assert rows["alpha"]["replacement"] == "beta"
    assert rows["beta"]["deprecated"] is False
    assert rows["beta"].get("deprecation_note") is None


def test_disable_audit_distinguishes_retirement_from_plain_disable(web_client):
    """A retirement that happened to find zero grants and a plain disable are
    different admin intents — the audit row must tell them apart, so both keys
    are always logged (mirrors mark_plugin_system, which always logs its
    affected_* counts even when zero)."""
    _seed_marketplace_with_plugin()
    _seed_marketplace_with_plugin(marketplace="mkt-x", plugin="beta")
    _, cookies = _create_user(web_client, "admin@x.com", admin=True)

    # alpha: retirement requested, but no grants exist → revoked_grants == 0
    r = web_client.post(
        "/api/marketplaces/mkt-x/plugins/alpha/disable",
        json={"revoke_grants": True},
        cookies=cookies,
    )
    assert r.status_code == 200, r.text
    assert r.json()["revoked_grants"] == 0

    # beta: plain disable
    assert web_client.post(
        "/api/marketplaces/mkt-x/plugins/beta/disable", cookies=cookies,
    ).status_code == 200

    from src.db import get_system_db
    conn = get_system_db()
    try:
        rows = dict(
            conn.execute(
                "SELECT resource, params FROM audit_log "
                "WHERE action = 'marketplace.plugin.disable'"
            ).fetchall()
        )
    finally:
        conn.close()

    def _params(resource):
        raw = rows[f"marketplace:{resource}"]
        return json.loads(raw) if isinstance(raw, str) else raw

    alpha, beta = _params("mkt-x/alpha"), _params("mkt-x/beta")
    assert alpha["revoke_grants_requested"] is True
    assert alpha["revoked_grants"] == 0
    assert beta["revoke_grants_requested"] is False
    assert beta["revoked_grants"] == 0
    assert alpha != beta, "the two intents must not produce identical audit params"
