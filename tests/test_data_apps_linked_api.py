"""REST tests for linked data apps: ``?kind=`` filter + PATCH description override.

Mirrors the env idiom of ``tests/test_data_apps_preview.py`` (real user/token/app
rows + TestClient, data_apps enabled).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
import yaml


def _auth(pat: str) -> dict:
    return {"Authorization": f"Bearer {pat}"}


def _enable_data_apps(data_dir) -> None:
    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    import app.instance_config as instance_config

    instance_config._instance_config = None


@pytest.fixture
def linked_env(e2e_env, monkeypatch, shared_app):
    from app.auth.jwt import create_access_token
    from fastapi.testclient import TestClient
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.data_apps import DataAppsRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    data_dir = e2e_env["data_dir"]
    _enable_data_apps(data_dir)

    conn = get_system_db()
    try:
        users = UserRepository(conn)
        users.create(id="admin1", email="admin@test.local", name="Admin")
        users.create(id="grantee1", email="grantee@test.local", name="Grantee")
        users.create(id="stranger1", email="stranger@test.local", name="Stranger")

        ug = UserGroupsRepository(conn)
        # make admin1 an Admin
        admin_gid = ug.get_by_name("Admin")["id"]
        UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="test")
        gid = ug.create("Analysts", is_system=False)["id"]
        UserGroupMembersRepository(conn).add_member("grantee1", gid, source="test")

        token_repo = AccessTokenRepository(conn)
        pats: dict[str, str] = {}
        for uid, email in [
            ("admin1", "admin@test.local"),
            ("grantee1", "grantee@test.local"),
            ("stranger1", "stranger@test.local"),
        ]:
            tid = str(uuid.uuid4())
            jwt_token = create_access_token(uid, email, token_id=tid, typ="pat")
            token_repo.create(
                id=tid,
                user_id=uid,
                name=f"{uid}-pat",
                token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
                prefix=tid.replace("-", "")[:8],
                expires_at=None,
            )
            pats[uid] = jwt_token

        apps = DataAppsRepository(conn)
        # a linked app (granted to Analysts) + a hosted app (owned by admin)
        apps.upsert_linked(
            slug="kbc-sales",
            source_ref="conn1:sales",
            name="Sales dashboard",
            description="synced desc",
            external_url="https://example.com/apps/sales",
        )
        apps.create(slug="hosted-x", name="Hosted X", owner_user_id="admin1")

        ResourceGrantsRepository(conn).create(group_id=gid, resource_type="data_app", resource_id="kbc-sales")
    finally:
        conn.close()

    client = TestClient(shared_app)
    return {"client": client, "pats": pats}


def test_grantee_sees_linked_app_with_kind_and_external_url(linked_env):
    c, pats = linked_env["client"], linked_env["pats"]
    r = c.get("/api/data-apps?kind=linked", headers=_auth(pats["grantee1"]))
    assert r.status_code == 200, r.text
    apps = {a["slug"]: a for a in r.json()}
    assert "kbc-sales" in apps
    app = apps["kbc-sales"]
    assert app["kind"] == "linked"
    assert app["url"] == "https://example.com/apps/sales"
    assert app["effective_description"] == "synced desc"
    # hosted app is filtered out by kind=linked
    assert "hosted-x" not in apps


def test_kind_hosted_excludes_linked(linked_env):
    c, pats = linked_env["client"], linked_env["pats"]
    r = c.get("/api/data-apps?kind=hosted", headers=_auth(pats["admin1"]))
    assert r.status_code == 200
    slugs = {a["slug"] for a in r.json()}
    assert "hosted-x" in slugs
    assert "kbc-sales" not in slugs


def test_stranger_does_not_see_linked_app(linked_env):
    c, pats = linked_env["client"], linked_env["pats"]
    r = c.get("/api/data-apps?kind=linked", headers=_auth(pats["stranger1"]))
    assert r.status_code == 200
    assert r.json() == []


def test_invalid_kind_400s(linked_env):
    c, pats = linked_env["client"], linked_env["pats"]
    r = c.get("/api/data-apps?kind=bogus", headers=_auth(pats["admin1"]))
    assert r.status_code == 400


def test_source_filter_scopes_to_one_connection(linked_env):
    c, pats = linked_env["client"], linked_env["pats"]
    # add a second linked app under a different connection
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).upsert_linked(
            slug="kbc-other",
            source_ref="conn2:other",
            name="Other",
            description="",
            external_url="https://example.com/apps/other",
        )
    finally:
        conn.close()
    r = c.get("/api/data-apps?kind=linked&source=conn1", headers=_auth(pats["admin1"]))
    assert r.status_code == 200
    slugs = {a["slug"] for a in r.json()}
    assert slugs == {"kbc-sales"}  # conn1 only; conn2's kbc-other excluded


def test_admin_sets_description_override(linked_env):
    c, pats = linked_env["client"], linked_env["pats"]
    r = c.patch("/api/data-apps/kbc-sales", json={"description": "admin desc"}, headers=_auth(pats["admin1"]))
    assert r.status_code == 200, r.text
    assert r.json()["effective_description"] == "admin desc"
    # reflected in the list
    r2 = c.get("/api/data-apps?kind=linked", headers=_auth(pats["grantee1"]))
    app = next(a for a in r2.json() if a["slug"] == "kbc-sales")
    assert app["effective_description"] == "admin desc"


def test_non_admin_cannot_set_description(linked_env):
    c, pats = linked_env["client"], linked_env["pats"]
    r = c.patch("/api/data-apps/kbc-sales", json={"description": "x"}, headers=_auth(pats["grantee1"]))
    assert r.status_code == 403


def test_description_override_rejected_on_hosted(linked_env):
    c, pats = linked_env["client"], linked_env["pats"]
    r = c.patch("/api/data-apps/hosted-x", json={"description": "x"}, headers=_auth(pats["admin1"]))
    assert r.status_code == 409  # not managed


def _hide_linked(slug="kbc-sales"):
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        # reconcile with an empty keep-list for this connection → hides the app
        DataAppsRepository(conn).soft_delete_missing_linked(source_ref_prefix="conn1:", keep_source_refs=[])
    finally:
        conn.close()


def test_hidden_linked_app_absent_from_list(linked_env):
    c, pats = linked_env["client"], linked_env["pats"]
    _hide_linked()
    r = c.get("/api/data-apps?kind=linked", headers=_auth(pats["grantee1"]))
    assert r.status_code == 200
    assert r.json() == []


def test_hidden_linked_app_404_on_detail_and_patch(linked_env):
    c, pats = linked_env["client"], linked_env["pats"]
    _hide_linked()
    # a previously-granted analyst can no longer read the gone app…
    assert c.get("/api/data-apps/kbc-sales", headers=_auth(pats["grantee1"])).status_code == 404
    # …and an admin can't PATCH it either (it's soft-deleted, not just ungranted)
    assert (
        c.patch("/api/data-apps/kbc-sales", json={"description": "x"}, headers=_auth(pats["admin1"])).status_code == 404
    )


def test_hosted_lifecycle_actions_reject_linked_apps(linked_env):
    """deploy/stop/logs/readiness are hosted-only: `state` doubles as the
    reconciler's scope filter, so a stop that rewrote it would knock the row
    out of the sync's control permanently (Devin Review on #1116)."""
    c, pats = linked_env["client"], linked_env["pats"]
    admin = _auth(pats["admin1"])
    for method, path, body in (
        ("post", "/api/data-apps/kbc-sales/deploy", {"mode": "stable"}),
        ("post", "/api/data-apps/kbc-sales/stop", None),
        ("get", "/api/data-apps/kbc-sales/logs", None),
        ("get", "/api/data-apps/kbc-sales/readiness", None),
        ("post", "/api/data-apps/kbc-sales/git-credential", None),
        ("post", "/api/data-apps/kbc-sales/preview-grant", None),
        ("post", "/api/data-apps/kbc-sales/drafts", {"branch": "dev"}),
        ("put", "/api/data-apps/kbc-sales/secrets", {"secrets": {}}),
    ):
        kwargs = {"json": body} if body is not None else {}
        resp = getattr(c, method)(path, headers=admin, **kwargs)
        assert resp.status_code == 400, (path, resp.status_code, resp.text)
        assert "linked_app_not_hosted" in resp.text
    # State untouched — the reconciler still owns the row.
    detail = c.get("/api/data-apps/kbc-sales", headers=admin)
    assert detail.status_code == 200


def test_lifecycle_guard_does_not_leak_kind_to_strangers(linked_env):
    """Authorization runs BEFORE the hosted-only guard: a caller with no
    rights gets a plain 403, not the distinctive linked_app_not_hosted 400
    that would confirm the slug exists and is externally hosted."""
    c, pats = linked_env["client"], linked_env["pats"]
    resp = c.post("/api/data-apps/kbc-sales/stop", headers=_auth(pats["stranger1"]))
    assert resp.status_code == 403, resp.text
    assert "linked_app_not_hosted" not in resp.text


def test_delete_linked_is_registry_only_retire_path(linked_env):
    """DELETE on a linked row is the admin's orphan-cleanup path (a source
    unregistered without a final reconcile leaves rows the sync can never
    hide): registry-only removal, no hosted teardown (Devin Review on
    #1116)."""
    c, pats = linked_env["client"], linked_env["pats"]
    admin = _auth(pats["admin1"])
    resp = c.delete("/api/data-apps/kbc-sales", headers=admin)
    assert resp.status_code == 204, resp.text
    assert c.get("/api/data-apps/kbc-sales", headers=admin).status_code == 404


def test_kind_linked_filters_in_sql_not_post_fetch(linked_env, monkeypatch):
    """?kind=linked must ride list_linked() (SQL-side filter) — linked-row
    counts are upstream-data-driven and can exceed list()'s page cap, where
    post-filtering a capped page silently drops apps (Devin Review on #1116).
    Pinned by making list() return nothing: the linked pool must still show."""
    from src.repositories.data_apps import DataAppsRepository

    c, pats = linked_env["client"], linked_env["pats"]
    monkeypatch.setattr(DataAppsRepository, "list", lambda self, **kw: [])
    r = c.get("/api/data-apps?kind=linked", headers=_auth(pats["admin1"]))
    assert r.status_code == 200, r.text
    assert "kbc-sales" in {a["slug"] for a in r.json()}


def test_admin_can_delete_a_hidden_linked_row(linked_env):
    """A retired (soft-deleted) linked row must stay deletable — otherwise it
    and its grants linger forever, since every read surface 404s it and the
    reconciler can no longer touch it (Devin Review on #1116)."""
    c, pats = linked_env["client"], linked_env["pats"]
    admin = _auth(pats["admin1"])
    _hide_linked()
    assert c.get("/api/data-apps/kbc-sales", headers=admin).status_code == 404
    assert c.delete("/api/data-apps/kbc-sales", headers=admin).status_code == 204

    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        assert DataAppsRepository(conn).get_by_slug("kbc-sales") is None
    finally:
        conn.close()
