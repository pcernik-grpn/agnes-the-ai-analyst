"""Tests for the ``is_sso_user`` flag on /api/users.

Pins the rules used by the admin UI to hide password / delete affordances
for accounts managed by an external SSO provider (Google Workspace today):

  1. Group with ``created_by = 'system:google-sync'``        → SSO
  2. ``Admin`` system group + AGNES_GROUP_ADMIN_EMAIL set    → SSO
  3. ``Everyone`` system group + AGNES_GROUP_EVERYONE_EMAIL  → SSO
  4. No groups, or only admin-created custom groups          → NOT SSO

Also pins the JS-side guard in ``admin_users.html`` so a renderer regression
that drops the conditional shows up in CI.
"""

import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        from src.db import close_system_db

        close_system_db()
        yield tmp
        close_system_db()


def _seed_admin():
    """Create an admin user (Admin system-group member) and return (id, jwt)."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@test", name="Admin")
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
        UserGroupMembersRepository(conn).add_member(uid, admin_gid, source="system_seed")
        return uid, create_access_token(user_id=uid, email="admin@test")
    finally:
        conn.close()


def _create_user(email: str) -> str:
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0])
        return uid
    finally:
        conn.close()


def _add_to_group(user_id: str, group_id: str, source: str) -> None:
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        UserGroupMembersRepository(conn).add_member(user_id, group_id, source=source)
    finally:
        conn.close()


def _create_group(name: str, created_by: str | None) -> str:
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    try:
        return UserGroupsRepository(conn).create(name=name, created_by=created_by)["id"]
    finally:
        conn.close()


def _system_group_id(name: str) -> str:
    from src.db import get_system_db

    conn = get_system_db()
    try:
        return conn.execute("SELECT id FROM user_groups WHERE name = ?", [name]).fetchone()[0]
    finally:
        conn.close()


def _user_payload(client: TestClient, token: str, user_id: str) -> dict:
    """Fetch the list, return the row for the given user_id."""
    resp = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    matches = [u for u in resp.json() if u["id"] == user_id]
    assert matches, f"user {user_id} not in /api/users response"
    return matches[0]


def test_user_with_no_groups_is_not_sso(fresh_db):
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("local@test")

    payload = _user_payload(client, token, uid)
    assert payload["is_sso_user"] is False


def test_user_with_only_custom_admin_group_is_not_sso(fresh_db):
    """Admin-created (created_by != 'system:google-sync') custom group does
    not flip the user to SSO — local accounts in custom groups stay local."""
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("local@test")
    gid = _create_group("data-team", created_by="admin@test")
    _add_to_group(uid, gid, source="admin")

    payload = _user_payload(client, token, uid)
    assert payload["is_sso_user"] is False


def test_user_with_google_sync_group_is_sso(fresh_db):
    """Group whose ``created_by = 'system:google-sync'`` flips the user."""
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("g@test")
    gid = _create_group("eng@workspace.test", created_by="system:google-sync")
    _add_to_group(uid, gid, source="google_sync")

    payload = _user_payload(client, token, uid)
    assert payload["is_sso_user"] is True


def test_admin_system_group_alone_is_not_sso_without_env_mapping(fresh_db):
    """The Admin system row exists on every install. Without the env
    mapping, membership in it is *not* an SSO signal — local admins exist."""
    from app.main import app
    from src.db import SYSTEM_ADMIN_GROUP

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("local-admin@test")
    _add_to_group(uid, _system_group_id(SYSTEM_ADMIN_GROUP), source="admin")

    payload = _user_payload(client, token, uid)
    assert payload["is_sso_user"] is False


def test_admin_system_group_is_sso_when_env_mapped(fresh_db, monkeypatch):
    """AGNES_GROUP_ADMIN_EMAIL set → Admin membership counts as SSO."""
    from app.main import app
    from src.db import SYSTEM_ADMIN_GROUP

    monkeypatch.setenv("AGNES_GROUP_ADMIN_EMAIL", "admins@workspace.test")

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("g-admin@test")
    _add_to_group(uid, _system_group_id(SYSTEM_ADMIN_GROUP), source="google_sync")

    payload = _user_payload(client, token, uid)
    assert payload["is_sso_user"] is True


def test_everyone_membership_is_never_sso_even_with_the_legacy_env_set(fresh_db, monkeypatch):
    """The Everyone branch of ``_is_sso_user`` is gone with the mapping.

    Setting ``AGNES_GROUP_EVERYONE_EMAIL`` used to make a ``google_sync``
    membership in the seeded ``Everyone`` row count as SSO-managed, because
    that row WAS the Workspace group. 0098 gave the Workspace group a group
    of its own, whose ``created_by='system:google-sync'`` triggers the first
    and broader branch — so the same accounts are still detected, and a
    stale env value can no longer lock an admin out of managing a local one.
    """
    from app.main import app
    from src.db import SYSTEM_EVERYONE_GROUP

    monkeypatch.setenv("AGNES_GROUP_EVERYONE_EMAIL", "everyone@workspace.test")

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("g-user@test")
    _add_to_group(uid, _system_group_id(SYSTEM_EVERYONE_GROUP), source="google_sync")

    payload = _user_payload(client, token, uid)
    assert payload["is_sso_user"] is False


def test_everyone_system_group_alone_is_not_sso_without_env_mapping(fresh_db):
    """Without the env mapping, Everyone membership stays local."""
    from app.main import app
    from src.db import SYSTEM_EVERYONE_GROUP

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("local@test")
    _add_to_group(uid, _system_group_id(SYSTEM_EVERYONE_GROUP), source="admin")

    payload = _user_payload(client, token, uid)
    assert payload["is_sso_user"] is False


def test_system_seed_membership_in_env_mapped_everyone_is_not_sso(fresh_db, monkeypatch):
    """Devin BUG_0002 on PR #142: the v13 migration backfills every existing
    user into the Everyone system group with source='system_seed'. If an
    operator later sets AGNES_GROUP_EVERYONE_EMAIL, the system-group branch
    of _is_sso_user would (without the source check) flip every backfilled
    user to is_sso_user=True — locking the admin out of password reset /
    delete on accounts the IdP doesn't actually own. The source='google_sync'
    requirement on the system-group branches keeps system_seed memberships
    locally manageable even when the group is env-mapped."""
    from app.main import app
    from src.db import SYSTEM_EVERYONE_GROUP

    monkeypatch.setenv("AGNES_GROUP_EVERYONE_EMAIL", "everyone@workspace.test")

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("v13-backfilled@test")
    # The v13 migration uses source='system_seed' for the Everyone backfill.
    _add_to_group(uid, _system_group_id(SYSTEM_EVERYONE_GROUP), source="system_seed")

    payload = _user_payload(client, token, uid)
    assert payload["is_sso_user"] is False, (
        "system_seed membership in an env-mapped Everyone group must not "
        "flip is_sso_user — the IdP doesn't own this membership"
    )


def test_new_user_via_api_stays_local_when_everyone_env_mapped(fresh_db, monkeypatch):
    """Issue #748 regression: ``ensure_everyone_membership`` is a no-op when
    AGNES_GROUP_EVERYONE_EMAIL is set, so a user freshly created through
    POST /api/users must NOT gain a system_seed Everyone row and must NOT
    be flagged is_sso_user — the auto-grant-at-creation helper must never
    fight the Workspace-authoritative membership set."""
    from app.main import app

    monkeypatch.setenv("AGNES_GROUP_EVERYONE_EMAIL", "everyone@workspace.test")

    client = TestClient(app)
    _, token = _seed_admin()

    resp = client.post(
        "/api/users",
        json={"email": "fresh-mapped@test", "name": "Fresh"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    uid = resp.json()["id"]

    payload = _user_payload(client, token, uid)
    assert payload["is_sso_user"] is False


def test_admin_source_membership_in_env_mapped_admin_is_not_sso(fresh_db, monkeypatch):
    """Mirror of the Everyone case for the Admin system group: a manually-
    added (source='admin') membership in env-mapped Admin must not be
    treated as SSO — only google_sync source is owned by the IdP."""
    from app.main import app
    from src.db import SYSTEM_ADMIN_GROUP

    monkeypatch.setenv("AGNES_GROUP_ADMIN_EMAIL", "admins@workspace.test")

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("local-admin-in-mapped@test")
    _add_to_group(uid, _system_group_id(SYSTEM_ADMIN_GROUP), source="admin")

    payload = _user_payload(client, token, uid)
    assert payload["is_sso_user"] is False


def test_admin_users_template_gates_password_buttons_on_is_sso_user(fresh_db):
    """Pin the JS-side guard: list-view template must wrap the Reset /
    Set pwd / Delete buttons in a ``u.is_sso_user`` ternary so a renderer
    regression that drops the conditional surfaces in CI."""
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    resp = client.get(
        "/admin/users",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "u.is_sso_user" in body, "list-view template must reference u.is_sso_user"
    # The five-control row (Detail · Tokens · Reset · Set pwd · Delete) became
    # one `⋯` menu built per row in JS, so the gate reads `data-sso` off the
    # button rather than branching in Jinja, and the three items carry
    # `data-act` handles. Same contract: the password and delete actions exist
    # ONLY in the non-SSO branch — an SSO account has no password Agnes owns.
    assert 'data-sso="${u.is_sso_user ? "1" : ""}"' in body, "the row must publish the SSO flag"
    gated = body.split("btn.dataset.sso ?")[1].split("`}`")[0]
    for act in ('data-act="reset"', 'data-act="setpwd"', 'data-act="delete"'):
        assert act in gated, f"{act} must sit inside the non-SSO branch"


def test_admin_user_detail_template_gates_password_buttons_on_is_sso_user(fresh_db):
    """Detail page must hide reset-pw-btn / delete-user-btn for SSO users."""
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("victim@test")
    resp = client.get(
        f"/admin/users/{uid}",
        headers={"Accept": "text/html"},
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The JS reads userState.is_sso_user and toggles display on the two buttons.
    assert "userState.is_sso_user" in body
    assert 'id="reset-pw-btn"' in body
    assert 'id="delete-user-btn"' in body


# ── Server-side enforcement ──────────────────────────────────────────────
# UI hides the buttons; these tests pin that the API rejects the same
# operations even when called directly with a valid admin token. Without
# this, a curl-savvy admin could bypass the UI guard and reset a Google
# Workspace account's password locally.


def _make_sso_user(email: str) -> str:
    """Create a user and stamp them with a google_sync-sourced custom group."""
    uid = _create_user(email)
    gid = _create_group(f"{email}-team@workspace.test", created_by="system:google-sync")
    _add_to_group(uid, gid, source="google_sync")
    return uid


def test_reset_password_rejects_sso_user(fresh_db):
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    sso_uid = _make_sso_user("g@test")

    resp = client.post(
        f"/api/users/{sso_uid}/reset-password",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409, resp.text
    assert "sso" in resp.json()["detail"].lower()


def test_reset_password_allows_local_user(fresh_db):
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("local@test")

    resp = client.post(
        f"/api/users/{uid}/reset-password",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert "reset_url" in resp.json()


def test_set_password_rejects_sso_user(fresh_db):
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    sso_uid = _make_sso_user("g@test")

    resp = client.post(
        f"/api/users/{sso_uid}/set-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": "supersecret123"},
    )
    assert resp.status_code == 409, resp.text
    assert "sso" in resp.json()["detail"].lower()


def test_set_password_allows_local_user(fresh_db):
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("local@test")

    resp = client.post(
        f"/api/users/{uid}/set-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": "supersecret123"},
    )
    assert resp.status_code == 204, resp.text


def test_delete_user_rejects_sso_user(fresh_db):
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    sso_uid = _make_sso_user("g@test")

    resp = client.delete(
        f"/api/users/{sso_uid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409, resp.text
    assert "sso" in resp.json()["detail"].lower()


def test_delete_user_allows_local_user(fresh_db):
    from app.main import app

    client = TestClient(app)
    _, token = _seed_admin()
    uid = _create_user("local@test")

    resp = client.delete(
        f"/api/users/{uid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204, resp.text


def test_delete_user_rejects_sso_user_in_admin_group_when_env_mapped(fresh_db, monkeypatch):
    """Pin the env-mapping branch end-to-end: a user in `Admin` *because*
    AGNES_GROUP_ADMIN_EMAIL maps it from Google must also be locked from
    deletion via the server-side guard."""
    from app.main import app
    from src.db import SYSTEM_ADMIN_GROUP

    monkeypatch.setenv("AGNES_GROUP_ADMIN_EMAIL", "admins@workspace.test")

    client = TestClient(app)
    _, token = _seed_admin()
    # Second admin so the last-active-admin safeguard does not fire first.
    uid = _create_user("g-admin@test")
    _add_to_group(uid, _system_group_id(SYSTEM_ADMIN_GROUP), source="google_sync")

    resp = client.delete(
        f"/api/users/{uid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409, resp.text
    assert "sso" in resp.json()["detail"].lower()
