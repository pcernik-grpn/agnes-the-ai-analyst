"""Auth remainder gap closure (F2a — audit-full-coverage plan, Task 3).

Covers what upstream's login-audit closure (``app/auth/login_audit.py``)
left open: PAT enumeration (the three GET routes in ``app/api/tokens.py``)
and the select-mode Keboola project import
(``app/api/keboola_login_projects.py``).
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from app.auth import keboola_provisioning as kprov
from app.auth.providers import keboola_projects as kp
from app.auth.providers import keboola_verify as kv
from connectors.keboola.storage_api import KeboolaStorageClient

BASE = "/api/auth/keboola/projects"
STACK = "https://connection.example.com"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestTokenListAudit:
    def test_admin_tokens_list_writes_admin_all_scope(self, tmp_path, monkeypatch, seeded_app):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        client = seeded_app["client"]
        resp = client.get("/auth/admin/tokens", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="token.list", limit=10)
        matches = [r for r in rows if json.loads(r["params"]).get("scope") == "admin_all"]
        assert matches, "GET /auth/admin/tokens did not write a token.list row"
        assert matches[0]["user_id"] == "admin1"

    def test_own_tokens_list_writes_own_scope(self, tmp_path, monkeypatch, seeded_app):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        client = seeded_app["client"]
        resp = client.get("/auth/tokens", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="token.list", limit=10)
        matches = [r for r in rows if json.loads(r["params"]).get("scope") == "own" and r["user_id"] == "analyst1"]
        assert matches, "GET /auth/tokens did not write a token.list row"

    def test_get_one_token_writes_one_scope(self, tmp_path, monkeypatch, seeded_app):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        client = seeded_app["client"]
        created = client.post(
            "/auth/tokens",
            json={"name": "t1"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert created.status_code == 201
        token_id = created.json()["id"]

        resp = client.get(f"/auth/tokens/{token_id}", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="token.list", limit=10)
        matches = [r for r in rows if json.loads(r["params"]).get("scope") == "one"]
        assert matches, "GET /auth/tokens/{token_id} did not write a token.list row"


@pytest.fixture
def select_env(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    from app.secrets_vault import _reset_ephemeral_key_for_tests

    _reset_ephemeral_key_for_tests()
    monkeypatch.setattr(kv, "stack_url", lambda: STACK)
    monkeypatch.setattr("app.api.admin._validate_url_not_private", lambda url, field_name="url": None)
    monkeypatch.setattr(kv, "multi_project_mode", lambda: "select")
    monkeypatch.setattr(kv, "is_wildcard_project", lambda: True)
    monkeypatch.setattr(kp, "exchange_project_pat", lambda tok, pid, *, read_only: f"pat-{pid}")
    monkeypatch.setattr(
        KeboolaStorageClient,
        "verify_token",
        lambda self: {
            "isMasterToken": False,
            "owner": {"id": int(self.token.split("-", 1)[1]), "name": "P"},
        },
    )
    return seeded_app


class TestKeboolaProjectsImportAudit:
    def test_import_writes_audit_row(self, tmp_path, monkeypatch, select_env):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from src.repositories import users_repo

        analyst = users_repo().get_by_email("analyst@test.com")
        kprov.store_pending_discovery(analyst, [kp.DiscoveredProject(id="516", name="A", role="admin")], "at-1")
        resp = select_env["client"].post(
            BASE, json={"project_ids": ["516"]}, headers=_auth(select_env["analyst_token"])
        )
        assert resp.status_code == 200

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="keboola.projects_import", limit=10)
        assert rows, "POST /api/auth/keboola/projects did not write a keboola.projects_import row"
        assert rows[0]["user_id"] == "analyst1"
        assert json.loads(rows[0]["params"]).get("project_ids") == ["516"]
