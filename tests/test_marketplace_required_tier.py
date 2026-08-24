"""HTTP-surface coverage for the group-scoped Required tier on marketplace
plugins (``resource_grants.requirement='required'``).

The resolver unions required-tier grant keys with explicit subscriptions
(``src/marketplace_filter.py:required_plugin_keys``); resolver-level
semantics are pinned in ``test_marketplace_filter_store.py``. This module
pins the endpoints around it:

* ``GET /api/my-stack`` — required plugin reports ``enabled=True`` +
  ``is_required=True`` with no subscription row
* unsubscribe / uninstall refusals (409), mirroring the v39 ``is_system``
  guards
* an ``available`` grant keeps the pre-existing Model B behavior

Helper pattern shared with ``test_marketplace_plugin_system.py`` (plain
functions imported from there; the client fixture is local because pytest
resolves fixtures per-module).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_marketplace_plugin_system import (
    _add_group,
    _create_user,
    _seed_marketplace_with_plugin,
)


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


def _grant(group_id: str, *, marketplace: str = "mkt-x", plugin: str = "alpha", requirement: str | None = None) -> None:
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository

    conn = get_system_db()
    try:
        ResourceGrantsRepository(conn).create(
            group_id=group_id,
            resource_type="marketplace_plugin",
            resource_id=f"{marketplace}/{plugin}",
            requirement=requirement,
        )
    finally:
        conn.close()


def _add_member(user_id: str, group_id: str) -> None:
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        UserGroupMembersRepository(conn).add_member(user_id, group_id, source="admin")
    finally:
        conn.close()


def _seed_required_for(web_client, *, requirement="required"):
    """User in one group holding a grant on mkt-x/alpha at ``requirement``."""
    _seed_marketplace_with_plugin()
    user_id, cookies = _create_user(web_client, "user@x.com")
    gid = _add_group("engineers")
    _add_member(user_id, gid)
    _grant(gid, requirement=requirement)
    return user_id, cookies


class TestMyStackView:
    def test_required_plugin_enabled_and_locked(self, web_client):
        _, cookies = _seed_required_for(web_client)
        r = web_client.get("/api/my-stack", cookies=cookies)
        assert r.status_code == 200, r.text
        curated = r.json()["curated"]
        assert len(curated) == 1
        entry = curated[0]
        # Served without a subscription row → the toggle must report ON,
        # and is_required drives the locked UI state (like is_system).
        assert entry["enabled"] is True
        assert entry["is_required"] is True
        assert entry["is_system"] is False

    def test_available_plugin_stays_opt_in(self, web_client):
        _, cookies = _seed_required_for(web_client, requirement="available")
        r = web_client.get("/api/my-stack", cookies=cookies)
        assert r.status_code == 200, r.text
        entry = r.json()["curated"][0]
        assert entry["enabled"] is False
        assert entry["is_required"] is False


class TestGuards:
    def test_unsubscribe_required_plugin_refused(self, web_client):
        _, cookies = _seed_required_for(web_client)
        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": False},
            cookies=cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "cannot_unsubscribe_required_plugin"

    def test_uninstall_required_plugin_refused(self, web_client):
        _, cookies = _seed_required_for(web_client)
        r = web_client.delete(
            "/api/marketplace/curated/mkt-x/alpha/install",
            cookies=cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "cannot_uninstall_required_plugin"

    def test_subscribe_required_plugin_is_allowed_noop(self, web_client):
        """Subscribe stays allowed (idempotent no-op on the served set),
        mirroring the is_system toggle contract."""
        _, cookies = _seed_required_for(web_client)
        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": True},
            cookies=cookies,
        )
        assert r.status_code == 200, r.text

    def test_unsubscribe_available_plugin_still_works(self, web_client):
        _, cookies = _seed_required_for(web_client, requirement="available")
        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": True},
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
        r = web_client.put(
            "/api/my-stack/curated/mkt-x/alpha",
            json={"enabled": False},
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
