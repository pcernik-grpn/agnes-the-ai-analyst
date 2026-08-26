"""Integration tests for GET /marketplace/cowork/{prefixed_name}.zip (#464).

RBAC is enforced via the same resolve path as /marketplace.zip — a plugin the
caller isn't granted is absent → 404.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _read_zip(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


@pytest.fixture
def cowork_env(e2e_env, shared_app):
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_curated_subscriptions import (
        UserCuratedSubscriptionsRepository,
    )

    data_dir = e2e_env["data_dir"]

    # One plugin on disk that exercises the transforms: hex version, homepage,
    # a skill with Claude-Code-only frontmatter keys.
    d = data_dir / "marketplaces" / "mkt" / "plugins" / "demo"
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {"name": "demo", "version": "deadbeefcafe", "homepage": "https://internal/x"}
        ),
        encoding="utf-8",
    )
    (d / "skills" / "create").mkdir(parents=True)
    (d / "skills" / "create" / "SKILL.md").write_text(
        "---\nname: create\ndescription: do <x>\nargument-hint: y\n---\nbody\n",
        encoding="utf-8",
    )
    (d / "CLAUDE.md").write_text("ctx", encoding="utf-8")

    conn = get_system_db()
    try:
        t = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?,?,?,?)",
            ["mkt", "Market", "https://example.test/m.git", t],
        )
        raw = {"name": "demo", "version": "deadbeefcafe", "description": "demo plugin"}
        conn.execute(
            "INSERT INTO marketplace_plugins (marketplace_id, name, version, raw, updated_at) "
            "VALUES (?,?,?,?,?)",
            ["mkt", "demo", "deadbeefcafe", json.dumps(raw), t],
        )

        users = UserRepository(conn)
        users.create(id="admin1", email="admin@test.local", name="Admin")
        users.create(id="nope1", email="nope@test.local", name="Nope")

        ug = UserGroupsRepository(conn)
        ug.ensure_system("Admin", "system")
        ug.ensure_system("Everyone", "system")
        admin_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name='Admin'"
        ).fetchone()[0]

        ugm = UserGroupMembersRepository(conn)
        ugm.add_member("admin1", admin_gid, source="system_seed")

        rg = ResourceGrantsRepository(conn)
        rg.create(
            group_id=admin_gid,
            resource_type="marketplace_plugin",
            resource_id="mkt/demo",
        )

        subs = UserCuratedSubscriptionsRepository(conn)
        subs.subscribe("admin1", "mkt", "demo")
    finally:
        conn.close()

    app = shared_app
    return {
        "client": TestClient(app),
        "admin_token": create_access_token("admin1", "admin@test.local"),
        "nope_token": create_access_token("nope1", "nope@test.local"),
    }


class TestCoworkZip:
    def test_granted_user_downloads_transformed_zip(self, cowork_env):
        c = cowork_env["client"]
        resp = c.get(
            "/marketplace/cowork/mkt-demo.zip", headers=_auth(cowork_env["admin_token"])
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "mkt-demo.zip" in resp.headers["content-disposition"]
        files = _read_zip(resp.content)
        assert ".claude-plugin/plugin.json" in files
        assert ".claude-plugin/marketplace.json" not in files
        assert "CLAUDE.md" in files  # reference keeps content
        pj = json.loads(files[".claude-plugin/plugin.json"])
        assert pj["version"] == "0.0.1"
        assert "homepage" not in pj
        skill = files["skills/create/SKILL.md"].decode()
        assert "argument-hint" not in skill

    def test_ungranted_plugin_returns_404(self, cowork_env):
        c = cowork_env["client"]
        resp = c.get(
            "/marketplace/cowork/mkt-demo.zip", headers=_auth(cowork_env["nope_token"])
        )
        assert resp.status_code == 404

    def test_unknown_plugin_returns_404(self, cowork_env):
        c = cowork_env["client"]
        resp = c.get(
            "/marketplace/cowork/mkt-nonexistent.zip",
            headers=_auth(cowork_env["admin_token"]),
        )
        assert resp.status_code == 404

    def test_if_none_match_returns_304(self, cowork_env):
        c = cowork_env["client"]
        first = c.get(
            "/marketplace/cowork/mkt-demo.zip", headers=_auth(cowork_env["admin_token"])
        )
        etag = first.headers["etag"]
        second = c.get(
            "/marketplace/cowork/mkt-demo.zip",
            headers={**_auth(cowork_env["admin_token"]), "If-None-Match": etag},
        )
        assert second.status_code == 304

    def test_missing_auth_returns_401(self, cowork_env):
        resp = cowork_env["client"].get("/marketplace/cowork/mkt-demo.zip")
        assert resp.status_code == 401
