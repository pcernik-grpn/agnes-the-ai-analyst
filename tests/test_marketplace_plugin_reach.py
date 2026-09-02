"""End-to-end coverage for how far a marketplace plugin reaches.

Two questions, two owners, and keeping them apart is what this module is
about. ``/admin/marketplaces`` answers whether a plugin is AVAILABLE on the
instance at all (Off / Available); ``/admin/access`` answers WHO gets an
available one, and at which tier. There used to be a third answer —
``marketplace_plugins.is_system``, set from the marketplaces page, meaning
"every account, automatically" — which made that page a second writer of
distribution with its own vocabulary for a tier Access already had. Migration
0098 deleted it; the state is now an ordinary grant carrying
``scope='everyone'`` and ``requirement='required'``.

Tests in this module exercise:

* the everyone-scoped grant as the one way to reach every account, and the
  422s that keep it coherent
* refusal of the bypass paths — unsubscribe, uninstall
* no catch-up needed — a group or user created afterwards is reached with no
  row written for it
* availability is not distribution — disabling hides a plugin without
  touching its grants, and re-enabling restores exactly its old reach
* sync preservation — a re-sync of the marketplace doesn't disturb grants

Mirrors the helper pattern in ``test_marketplace_api.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db
    close_system_db()
    app = shared_app
    yield TestClient(app)
    close_system_db()


def _create_user(client, email, password="UserPass1!", admin: bool = False):
    """Create a user and return (user_id, cookies). When ``admin=True``
    the user is added to the seeded Admin system group so
    ``require_admin`` passes.

    Also joins the seeded ``Everyone`` group, which every real creation path
    does (``app.auth.group_sync.ensure_everyone_membership``, called from
    OAuth first sign-in, bootstrap, admin create and the import stubs). This
    inserts through the repo rather than the API, so without it the accounts
    here would be memberless — a state no live instance produces, and one
    that hides whether an everyone-scoped grant reaches an ordinary user on
    the DuckDB backend, where the scope is carried by that group.
    """
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
    from app.auth.group_sync import ensure_everyone_membership

    conn.close()
    ensure_everyone_membership(user_id, added_by="test")
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


def _carrier_group_id() -> str:
    """The group an everyone-scoped grant is stored against.

    ``resource_grants.group_id`` is NOT NULL, so the scope needs a row to
    live on; every everyone-grant using the same carrier is what leaves the
    UNIQUE index enforcing one per resource. See
    ``src.grant_scopes.carrier_group_id``.
    """
    from src.grant_scopes import carrier_group_id

    gid = carrier_group_id()
    assert gid, "the seeded Everyone group is missing — no carrier to write onto"
    return gid


def _grant_to_everyone(
    client,
    cookies,
    *,
    resource_id: str = "mkt-x/alpha",
    requirement: str = "required",
):
    """Give a plugin to every account, through the one writer of grants.

    This replaces ``POST .../plugins/{name}/system``. Same reach, same tier,
    one page — and now expressible for every grantable type rather than for
    plugins alone.
    """
    return client.post(
        "/api/admin/grants",
        json={
            "group_id": _carrier_group_id(),
            "resource_type": "marketplace_plugin",
            "resource_id": resource_id,
            "requirement": requirement,
            "scope": "everyone",
        },
        cookies=cookies,
    )


# ---------------------------------------------------------------------------
# The everyone-scoped grant
# ---------------------------------------------------------------------------


class TestGrantingToEveryone:
    def test_requires_admin(self, web_client):
        _seed_marketplace_with_plugin()
        _, cookies = _create_user(web_client, "user@x.com", admin=False)
        r = _grant_to_everyone(web_client, cookies)
        assert r.status_code in (401, 403)

    def test_creates_exactly_one_row(self, web_client):
        """One row, whichever backend is under it.

        ``scope`` comes back NULL here: these tests run on the DuckDB
        app-state backend, whose ladder is frozen (A3), so the column is
        accepted and dropped and the row is an ordinary grant on the carrier
        group — which holds every account, so it reaches the same people. The
        column round-tripping is asserted where it exists, in
        ``tests/db_pg/test_rbac_contract.py``.
        """
        _seed_marketplace_with_plugin()
        _, cookies = _create_user(web_client, "admin@x.com", admin=True)

        r = _grant_to_everyone(web_client, cookies)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["requirement"] == "required"
        assert body["scope"] is None, (
            "the frozen DuckDB ladder has no `scope` column; a value here "
            "would mean the accept-and-drop contract had changed"
        )

        from src.db import get_system_db

        conn = get_system_db()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM resource_grants "
                "WHERE resource_type = 'marketplace_plugin' AND resource_id = 'mkt-x/alpha'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1, "reaching everyone is ONE row, not one per group"

    def test_second_grant_on_the_same_resource_is_a_409(self, web_client):
        """The UNIQUE index is what keeps "everyone" single-valued, and it
        works because every everyone-grant shares one carrier."""
        _seed_marketplace_with_plugin()
        _, cookies = _create_user(web_client, "admin@x.com", admin=True)
        assert _grant_to_everyone(web_client, cookies).status_code == 201
        assert _grant_to_everyone(web_client, cookies).status_code == 409

    def test_a_withheld_type_refuses_the_scope(self, web_client):
        """Absent, not disabled. ``slack_channel``'s grantee is a CHANNEL,
        not an audience, so "give it to everyone" answers a question nobody
        asked — and storing it would be a claim no read path honours."""
        _, cookies = _create_user(web_client, "admin@x.com", admin=True)
        r = web_client.post(
            "/api/admin/grants",
            json={
                "group_id": _carrier_group_id(),
                "resource_type": "slack_channel",
                "resource_id": "C0123ABCD",
                "scope": "everyone",
            },
            cookies=cookies,
        )
        assert r.status_code == 422, r.text
        assert "everyone scope" in r.json()["detail"]

    def test_an_unknown_scope_is_a_422_not_a_500(self, web_client):
        _seed_marketplace_with_plugin()
        _, cookies = _create_user(web_client, "admin@x.com", admin=True)
        r = web_client.post(
            "/api/admin/grants",
            json={
                "group_id": _carrier_group_id(),
                "resource_type": "marketplace_plugin",
                "resource_id": "mkt-x/alpha",
                "scope": "everybody",
            },
            cookies=cookies,
        )
        assert r.status_code == 422, r.text


class TestGuards:
    def test_unsubscribe_via_my_stack_refused(self, web_client):
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        _grant_to_everyone(web_client, admin_cookies)
        # require_admin grants the admin access via the Admin group seed,
        # so they'll see the plugin in their stack and can attempt the
        # toggle. The guard should refuse.
        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": False}, cookies=admin_cookies,
        )
        assert r.status_code == 409
        # ONE refusal code, not two. `cannot_unsubscribe_system_plugin` and
        # `cannot_unsubscribe_required_plugin` were the same refusal for the
        # same reason, split only by which of two mechanisms had made the
        # plugin mandatory.
        assert r.json()["detail"] == "cannot_unsubscribe_required_plugin"

    def test_uninstall_via_marketplace_refused(self, web_client):
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        _grant_to_everyone(web_client, admin_cookies)
        r = web_client.delete(
            "/api/marketplace/curated/mkt-x/alpha/install",
            cookies=admin_cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "cannot_uninstall_required_plugin"

    def test_hand_set_group_grant_beside_an_everyone_grant_is_revocable(self, web_client):
        """A group grant and an everyone grant on the same plugin are two
        independent rows, and revoking the narrower one is honest.

        The v39 guard refused this with 409 ``cannot_revoke_system_grant``,
        protecting rows a fanout had machine-written. Nothing fans out now:
        the only group grant that can exist here is one an admin made, and
        trapping it would trap the admin's own work. Revoking it also removes
        the plugin from nobody — the everyone-scoped grant beside it still
        reaches them.
        """
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        gid = _add_group("engineers")

        from src.repositories import resource_grants_repo
        grant_id = resource_grants_repo().create(
            group_id=gid,
            resource_type="marketplace_plugin",
            resource_id="mkt-x/alpha",
            assigned_by="admin@x.com",
        )
        _grant_to_everyone(web_client, admin_cookies)

        r = web_client.delete(
            f"/api/admin/grants/{grant_id}", cookies=admin_cookies,
        )
        assert r.status_code in (200, 204), r.text

        # Still reaching everyone — the OTHER row carries that, and it was
        # not the one revoked.
        from src.marketplace_filter import everyone_required_plugin_keys

        assert ("mkt-x", "alpha") in everyone_required_plugin_keys()

    def test_group_delete_with_a_grant_on_a_plugin_everyone_has_is_not_500(
        self, web_client,
    ):
        """Regression cover, kept but re-premised. The original bug was an
        operator stuck with an undeletable group, because marking a plugin
        Automatic had auto-materialized a grant into that group that the
        delete cascade then tripped over. Nothing auto-materializes now, so
        the remaining way to reach the same shape is a HAND-SET grant on a
        plugin that everyone also has. It must still delete cleanly."""
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        _grant_to_everyone(web_client, admin_cookies)
        gid = _add_group("doomed-by-hand-set-grant")

        from src.repositories import resource_grants_repo
        resource_grants_repo().create(
            group_id=gid,
            resource_type="marketplace_plugin",
            resource_id="mkt-x/alpha",
            assigned_by="admin@x.com",
        )

        r = web_client.delete(
            f"/api/admin/groups/{gid}", cookies=admin_cookies,
        )
        assert r.status_code != 500, (
            f"group delete returned 500 for a group holding a grant on an "
            f"Automatic plugin; body={r.text}"
        )

    def test_subscribe_via_my_stack_still_allowed(self, web_client):
        """The guard refuses unsubscribe only — explicit subscribe must
        keep working since the row already exists (idempotent)."""
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        _grant_to_everyone(web_client, admin_cookies)
        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": True}, cookies=admin_cookies,
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Creation hooks
# ---------------------------------------------------------------------------


class TestNoCreationHooksNeeded:
    """The creation hooks are gone with the fanout they existed to run.

    A group or user created AFTER the decision used to need a hook to catch
    it up — five call sites across user-create and one in group-create, each
    soft-failing so a hiccup never blocked provisioning. An everyone-scoped
    grant makes "catching up" meaningless: there is nothing to inherit.
    These tests assert the OUTCOME the hooks were chasing, which is the part
    that actually mattered.
    """

    def test_group_created_afterwards_gets_no_catch_up_row(self, web_client):
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        _grant_to_everyone(web_client, admin_cookies)

        r = web_client.post(
            "/api/admin/groups",
            json={"name": "post-mark-group", "description": "test"},
            cookies=admin_cookies,
        )
        assert r.status_code in (200, 201), r.text
        new_gid = r.json()["id"]

        from src.repositories import marketplace_plugins_repo
        from src.db import get_system_db
        conn = get_system_db()
        try:
            grant = conn.execute(
                "SELECT 1 FROM resource_grants "
                "WHERE group_id = ? AND resource_type = 'marketplace_plugin' "
                "AND resource_id = 'mkt-x/alpha'",
                [new_gid],
            ).fetchone()
        finally:
            conn.close()
        assert grant is None, "no grant should be written for a new group"

        # And a MEMBER of that new group is still reached — through the
        # everyone-scoped grant, not through the group. Asserting the group
        # itself is served would be asserting the old flag's shape: `is_system`
        # bypassed groups entirely, so `list_granted_for_groups([new_gid])`
        # returned the plugin for a group that had been granted nothing. A
        # scope belongs to the AUDIENCE, and the audience is every account.
        member_id, _ = _create_user(web_client, "joiner@example.com")
        web_client.post(
            f"/api/admin/groups/{new_gid}/members",
            json={"user_id": member_id},
            cookies=admin_cookies,
        )
        served = {
            (r["marketplace_id"], r["name"])
            for r in marketplace_plugins_repo().list_granted_for_groups(
                _group_ids_for(member_id),
            )
        }
        assert ("mkt-x", "alpha") in served

    def test_user_created_after_mark_is_served_without_a_subscription(self, web_client):
        _seed_marketplace_with_plugin()
        _, admin_cookies = _create_user(
            web_client, "admin@x.com", admin=True,
        )
        _grant_to_everyone(web_client, admin_cookies)

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
        from src.marketplace_filter import required_plugin_keys
        conn = get_system_db()
        try:
            sub = conn.execute(
                "SELECT 1 FROM user_plugin_optouts "
                "WHERE user_id = ? AND marketplace_id = 'mkt-x' "
                "AND plugin_name = 'alpha'",
                [new_uid],
            ).fetchone()
            assert sub is None, "no subscription should be written for a new user"
            assert ("mkt-x", "alpha") in required_plugin_keys(conn, new_uid), (
                "the new user still gets it at the Automatic tier"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Sync preservation
# ---------------------------------------------------------------------------


def test_resync_leaves_a_plugins_grants_alone(tmp_path, monkeypatch):
    """``replace_for_marketplace`` re-runs on every sync and must not touch
    who has the plugin.

    This used to assert that the ``is_system`` FLAG survived a re-sync — the
    upsert deliberately excluded it from both the INSERT and the UPDATE SET.
    The flag is gone and reach lives in ``resource_grants`` now, which the
    plugin upsert cannot reach at all. That makes the invariant structural
    rather than a column the next contributor could add to a SET list, and
    this test pins the outcome either way.

    Uses ``tmp_path`` directly (no web_client) because the test only
    exercises the repo, not any API surface — but we still need a fresh
    DATA_DIR so we don't inherit state from a sibling test that populated
    ``store_entities`` etc. through the migration ladder.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "analytics").mkdir(exist_ok=True)
    (tmp_path / "extracts").mkdir(exist_ok=True)
    from src.db import close_system_db, get_system_db
    from src.repositories.marketplace_plugins import MarketplacePluginsRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_groups import UserGroupsRepository

    close_system_db()
    conn = get_system_db()
    try:
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            ["resync-test", "Resync", "https://example.test/r.git", datetime.now(timezone.utc)],
        )
        repo = MarketplacePluginsRepository(conn)
        repo.replace_for_marketplace(
            "resync-test",
            [{"name": "alpha", "version": "1.0", "description": "v1"}],
        )

        gid = UserGroupsRepository(conn).create(name="resync-audience")["id"]
        grant_id = ResourceGrantsRepository(conn).create(
            group_id=gid,
            resource_type="marketplace_plugin",
            resource_id="resync-test/alpha",
            requirement="required",
        )

        repo.replace_for_marketplace(
            "resync-test",
            [{"name": "alpha", "version": "2.0", "description": "v2-updated"}],
        )

        row = conn.execute(
            "SELECT version, description FROM marketplace_plugins "
            "WHERE marketplace_id = 'resync-test' AND name = 'alpha'"
        ).fetchone()
        assert row[0] == "2.0"
        assert row[1] == "v2-updated"

        grant = ResourceGrantsRepository(conn).get(grant_id)
        assert grant is not None, "a re-sync dropped a grant on the plugin"
        assert grant["requirement"] == "required", "a re-sync downgraded the tier"
    finally:
        conn.close()
        close_system_db()


# ---------------------------------------------------------------------------
# Disabled-plugin invariants at the HTTP boundary
# ---------------------------------------------------------------------------


def test_a_disabled_plugin_can_still_be_granted_and_is_served_to_nobody(web_client):
    """Availability and distribution are separate switches, and this is the
    case that proves it.

    A disabled plugin used to REFUSE the mark with 409, because disabling
    cleared ``is_system`` in the same UPDATE and re-enabling deliberately did
    not restore it — so marking a disabled plugin would have resurrected it
    as a mandatory default on the next enable. That whole contract existed
    because one write did two jobs.

    Now: granting a disabled plugin is accepted (Access decides who gets it),
    and the plugin is served to nobody while it is off (Marketplaces decides
    whether it exists). Turning it back on restores exactly the reach the
    grant describes, with nothing to re-set by hand.
    """
    _seed_marketplace_with_plugin()
    user_id, cookies = _create_user(web_client, "admin@x.com", admin=True)

    dis = web_client.post("/api/marketplaces/mkt-x/plugins/alpha/disable", cookies=cookies)
    assert dis.status_code == 200, dis.text

    assert _grant_to_everyone(web_client, cookies).status_code == 201

    from src.repositories import marketplace_plugins_repo

    served = {
        (r["marketplace_id"], r["name"])
        for r in marketplace_plugins_repo().list_granted_for_groups(_group_ids_for(user_id))
    }
    assert ("mkt-x", "alpha") not in served, "a disabled plugin was served to a grantee"

    en = web_client.post("/api/marketplaces/mkt-x/plugins/alpha/enable", cookies=cookies)
    assert en.status_code == 200, en.text

    served = {
        (r["marketplace_id"], r["name"])
        for r in marketplace_plugins_repo().list_granted_for_groups(_group_ids_for(user_id))
    }
    assert ("mkt-x", "alpha") in served, (
        "re-enabling did not restore the reach the grant describes — the grant "
        "was never touched by the disable, so nothing should need re-setting"
    )


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


# ---------------------------------------------------------------------------
# Reaching everyone is one row, and it does not fan out
# ---------------------------------------------------------------------------


class TestReachingEveryoneDoesNotMaterialize:
    """An everyone-scoped grant is ONE row. It writes no grant per group and
    no subscription per user.

    That is what makes revoking it exact — there is nothing left behind to
    retract — and it is what a grant an admin set by hand for one group is
    protected by: the two rows are independent. Marking a plugin "system"
    once fanned out into both tables, which is why unmarking could not
    retract its own work; a read-time resolution of the flag fixed the fanout
    but left two names for one idea. The scope removes the second name.
    """

    def test_one_row_and_no_subscriptions(self, web_client):
        _seed_marketplace_with_plugin()
        _add_group("engineers")
        _create_user(web_client, "member@example.com")
        _, admin = _create_user(web_client, "admin@example.com", admin=True)

        r = _grant_to_everyone(web_client, {"access_token": admin["access_token"]})
        assert r.status_code == 201, r.text

        from src.db import get_system_db

        conn = get_system_db()
        try:
            grants = conn.execute(
                "SELECT COUNT(*) FROM resource_grants "
                "WHERE resource_type = 'marketplace_plugin' "
                "  AND resource_id = 'mkt-x/alpha'",
            ).fetchone()[0]
            subs = conn.execute(
                "SELECT COUNT(*) FROM user_stack_subscriptions "
                "WHERE resource_type = 'marketplace_plugin' "
                "  AND resource_id = 'mkt-x/alpha'",
            ).fetchone()[0]
        finally:
            conn.close()
        assert grants == 1, "reaching everyone must not fan a grant out per group"
        assert subs == 0, "reaching everyone must not fan a subscription out per user"

    def test_served_and_required_without_a_subscription(self, web_client):
        """No subscription row, still served, still at the Automatic tier."""
        _seed_marketplace_with_plugin()
        _add_group("engineers")
        user_id, _ = _create_user(web_client, "reader@example.com")
        _, admin = _create_user(web_client, "admin2@example.com", admin=True)
        _grant_to_everyone(web_client, {"access_token": admin["access_token"]})

        from src.db import get_system_db
        from src.marketplace_filter import required_plugin_keys
        from src.repositories import marketplace_plugins_repo

        conn = get_system_db()
        try:
            granted = {
                (r["marketplace_id"], r["name"])
                for r in marketplace_plugins_repo().list_granted_for_groups(
                    _group_ids_for(user_id),
                )
            }
            required = required_plugin_keys(conn, user_id)
        finally:
            conn.close()
        assert ("mkt-x", "alpha") in granted, "visible without a per-group grant"
        assert ("mkt-x", "alpha") in required, "and at the Automatic tier"

    def test_revoking_the_everyone_grant_leaves_a_group_grant_alone(self, web_client):
        """The bug this whole effort started from: turning "everyone" off used
        to be unable to retract its own rows, so it retracted nothing and said
        so. Two independent rows make it exact in both directions."""
        _seed_marketplace_with_plugin()
        gid = _add_group("engineers")
        _, admin = _create_user(web_client, "admin3@example.com", admin=True)
        cookies = {"access_token": admin["access_token"]}

        from src.db import get_system_db
        from src.repositories import resource_grants_repo

        resource_grants_repo().create(
            group_id=gid,
            resource_type="marketplace_plugin",
            resource_id="mkt-x/alpha",
            assigned_by="admin3",
        )
        everyone_grant_id = _grant_to_everyone(web_client, cookies).json()["id"]

        r = web_client.delete(f"/api/admin/grants/{everyone_grant_id}", cookies=cookies)
        assert r.status_code in (200, 204), r.text

        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT group_id FROM resource_grants "
                "WHERE resource_type = 'marketplace_plugin' "
                "  AND resource_id = 'mkt-x/alpha'",
            ).fetchall()
        finally:
            conn.close()
        assert [r[0] for r in rows] == [gid], (
            "the admin's own group grant must survive revoking the everyone grant"
        )


def _group_ids_for(user_id: str) -> list[str]:
    from src.db import get_system_db
    conn = get_system_db()
    try:
        rows = conn.execute(
            "SELECT group_id FROM user_group_members WHERE user_id = ?", [user_id],
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()
