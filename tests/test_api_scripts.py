"""Tests for scripts and settings API endpoints."""

import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setenv("SCRIPT_TIMEOUT", "10")

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    from tests.helpers.auth import grant_admin

    conn = get_system_db()
    user_repo = UserRepository(conn)
    user_repo.create(id="admin1", email="admin@acme.com", name="Admin")
    user_repo.create(id="analyst1", email="analyst@acme.com", name="Analyst")
    grant_admin(conn, "admin1")

    # Grant analyst1 access to "sales" + "support" tables via resource_grants
    # (tests below exercise enable-dataset gates that require an explicit grant).
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    grp = UserGroupsRepository(conn).create(
        name="api-scripts-test", description="test", created_by="test",
    )
    UserGroupMembersRepository(conn).add_member(
        "analyst1", grp["id"], source="admin", added_by="test",
    )
    grants = ResourceGrantsRepository(conn)
    grants.create(group_id=grp["id"], resource_type="table", resource_id="sales",
                  assigned_by="test")
    grants.create(group_id=grp["id"], resource_type="table", resource_id="support",
                  assigned_by="test")
    conn.close()

    app = shared_app
    test_client = TestClient(app)
    admin_token = create_access_token("admin1", "admin@acme.com")
    analyst_token = create_access_token("analyst1", "analyst@acme.com")

    return test_client, admin_token, analyst_token


class TestScriptsAPI:
    def test_list_scripts_empty(self, client):
        c, admin_token, _ = client
        resp = c.get("/api/scripts", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_deploy_and_list(self, client):
        c, admin_token, _ = client
        headers = {"Authorization": f"Bearer {admin_token}"}

        resp = c.post("/api/scripts/deploy", json={
            "name": "hello", "source": "print('hello world')",
        }, headers=headers)
        assert resp.status_code == 201
        script_id = resp.json()["id"]

        resp = c.get("/api/scripts", headers=headers)
        assert resp.json()["count"] == 1

    def test_run_script(self, client):
        c, admin_token, _ = client
        headers = {"Authorization": f"Bearer {admin_token}"}

        resp = c.post("/api/scripts/run", json={
            "source": "print('hello from script')", "name": "test",
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] == 0
        assert "hello from script" in data["stdout"]

    def test_run_blocked_import(self, client):
        c, admin_token, _ = client
        headers = {"Authorization": f"Bearer {admin_token}"}

        resp = c.post("/api/scripts/run", json={
            "source": "import subprocess; subprocess.run(['ls'])", "name": "bad",
        }, headers=headers)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "disallowed" in detail or "Blocked" in detail

    def test_deploy_run_undeploy(self, client):
        c, admin_token, _ = client
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        resp = c.post("/api/scripts/deploy", json={
            "name": "calc", "source": "print(2+2)", "schedule": "daily 08:00",
        }, headers=admin_headers)
        script_id = resp.json()["id"]

        resp = c.post(f"/api/scripts/{script_id}/run", headers=admin_headers)
        assert resp.status_code == 200
        assert "4" in resp.json()["stdout"]

        # Undeploy (requires admin)
        resp = c.delete(f"/api/scripts/{script_id}", headers=admin_headers)
        assert resp.status_code == 204


class TestSettingsAPI:
    def test_get_settings(self, client):
        c, _, analyst_token = client
        resp = c.get("/api/settings", headers={"Authorization": f"Bearer {analyst_token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "analyst1"
        # v19: legacy `permissions` field dropped — use /api/me/effective-access
        # if you need the per-user grant breakdown.
        assert "permissions" not in data
        assert "sync_settings" in data

    def test_enable_dataset(self, client):
        c, _, analyst_token = client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        resp = c.put("/api/settings/dataset", json={
            "dataset": "sales", "enabled": True,
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    def test_enable_unauthorized_dataset(self, client):
        c, _, analyst_token = client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        resp = c.put("/api/settings/dataset", json={
            "dataset": "hr_secret", "enabled": True,
        }, headers=headers)
        assert resp.status_code == 403
