"""Tests for the internal per-app git hosting surface.

Two layers:

  - Bare-repo unit tests (`src/data_apps/git_repos.py`) — real `git`
    subprocess against a scratch repo, no FastAPI involved.
  - HTTP-layer authz tests for `/data-apps.git/{slug}/{path:path}` — mirrors
    the auth-matrix style of `tests/test_marketplace_server_git.py`, using
    FastAPI's `TestClient` (an `info/refs` GET is a plain HTTP request, so
    no real git client is needed to assert authz).
"""

from __future__ import annotations

import base64
import hashlib
import subprocess
import uuid
from datetime import datetime, timezone

import pytest

from src.data_apps.git_repos import fast_forward_live, init_app_repo, repo_path, resolve_ref


def _basic(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


class TestBareRepo:
    def test_init_and_fast_forward(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        p = init_app_repo("sales")
        assert (p / "HEAD").exists()
        # push a commit into the bare repo from a scratch clone
        work = tmp_path / "work"
        subprocess.run(["git", "clone", str(p), str(work)], check=True, capture_output=True)
        (work / "f.txt").write_text("hi")
        subprocess.run(["git", "-C", str(work), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "c1"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD:main"], check=True, capture_output=True)
        sha = fast_forward_live("sales")
        assert resolve_ref("sales", "agnes-live") == sha

    def test_init_app_repo_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        p1 = init_app_repo("idem")
        p2 = init_app_repo("idem")
        assert p1 == p2
        assert (p2 / "HEAD").exists()

    def test_resolve_ref_missing_repo_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert resolve_ref("does-not-exist", "HEAD") is None

    def test_fast_forward_live_empty_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        init_app_repo("empty")
        # `resolve_ref` uses `rev-parse --verify <ref>^{commit}`, which fails
        # (non-zero exit) on an unborn HEAD — unlike plain `rev-parse HEAD`,
        # which lenient-echoes back the literal string "HEAD" with exit 0 on
        # a fresh bare repo. So `fast_forward_live` sees both `resolve_ref`
        # calls come back None and raises its documented ValueError, rather
        # than reaching `update-ref` with a bogus target.
        with pytest.raises(ValueError, match="no commits to deploy"):
            fast_forward_live("empty")

    def test_fast_forward_live_explicit_sha(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        p = init_app_repo("pinned")
        work = tmp_path / "work2"
        subprocess.run(["git", "clone", str(p), str(work)], check=True, capture_output=True)
        (work / "a.txt").write_text("a")
        subprocess.run(["git", "-C", str(work), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "c1"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD:main"], check=True, capture_output=True)
        first_sha = resolve_ref("pinned", "main")

        (work / "a.txt").write_text("b")
        subprocess.run(["git", "-C", str(work), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "c2"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD:main"], check=True, capture_output=True)

        # Pin agnes-live to the *older* commit explicitly.
        result = fast_forward_live("pinned", sha=first_sha)
        assert result == first_sha
        assert resolve_ref("pinned", "agnes-live") == first_sha


def test_ensure_and_delete_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.data_apps.git_repos import delete_branch, ensure_branch

    # NOTE: brief used slug "g", but SLUG_RE requires >=2 chars; using "gg"
    # here to satisfy that pre-existing constraint while keeping the test intent.
    init_app_repo("gg")
    # seed main with one commit
    work = tmp_path / "w"
    subprocess.run(
        ["git", "clone", str(tmp_path / "apps" / "git" / "gg.git"), str(work)], check=True, capture_output=True
    )
    (work / "f").write_text("x")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "c"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD:main"], check=True, capture_output=True)
    ensure_branch("gg", "init", base="main")
    assert resolve_ref("gg", "init") == resolve_ref("gg", "main")
    ensure_branch("gg", "init")  # idempotent, no raise
    delete_branch("gg", "init")
    assert resolve_ref("gg", "init") is None
    with pytest.raises(ValueError):
        delete_branch("gg", "main")


class TestSlugValidation:
    """`repo_path` must reject any slug that doesn't match `SLUG_RE`
    (`src.data_apps.spec`) before it ever touches the filesystem — a
    path-traversal or multi-segment slug must never resolve to a path
    outside `${DATA_DIR}/apps/git/`."""

    def test_valid_slug_ok(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        p = repo_path("ok-slug")
        assert p == tmp_path / "apps" / "git" / "ok-slug.git"

    def test_path_traversal_slug_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        with pytest.raises(ValueError, match="invalid data app slug"):
            repo_path("../evil")

    def test_multi_segment_slug_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        with pytest.raises(ValueError, match="invalid data app slug"):
            repo_path("a/b")

    def test_uppercase_slug_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        with pytest.raises(ValueError, match="invalid data app slug"):
            repo_path("UPPER")


@pytest.fixture
def data_apps_git_env(e2e_env, monkeypatch, shared_app):
    """Same shape as `git_env` in tests/test_marketplace_server_git.py:
    real user + PAT rows so `resolve_token_to_user` passes, plus a
    `data_apps` row and its bare repo on disk, with the feature flag on."""
    import yaml

    from app.auth.jwt import create_access_token
    from src.data_apps.git_repos import init_app_repo
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.data_apps import DataAppsRepository

    data_dir = e2e_env["data_dir"]

    # Enable data_apps in instance.yaml (state/ dir already created by e2e_env).
    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    import app.instance_config as instance_config

    instance_config._instance_config = None

    conn = get_system_db()
    try:
        t = datetime.now(timezone.utc)
        users = UserRepository(conn)
        users.create(id="owner1", email="owner@test.local", name="Owner")
        users.create(id="other1", email="other@test.local", name="Other")
        users.create(id="admin1", email="admin@test.local", name="Admin")

        ug = UserGroupsRepository(conn)
        ug.ensure_system("Admin", "system")
        ug.ensure_system("Everyone", "system")
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]

        ugm = UserGroupMembersRepository(conn)
        ugm.add_member("admin1", admin_gid, source="system_seed")

        apps = DataAppsRepository(conn)
        apps.create(slug="sales", name="Sales App", owner_user_id="owner1")

        token_repo = AccessTokenRepository(conn)
        pats: dict[str, str] = {}
        for uid, email in [
            ("owner1", "owner@test.local"),
            ("other1", "other@test.local"),
            ("admin1", "admin@test.local"),
        ]:
            tid = str(uuid.uuid4())
            jwt = create_access_token(uid, email, token_id=tid, typ="pat")
            token_repo.create(
                id=tid,
                user_id=uid,
                name=f"{uid}-pat",
                token_hash=hashlib.sha256(jwt.encode()).hexdigest(),
                prefix=tid.replace("-", "")[:8],
                expires_at=None,
            )
            pats[uid] = jwt
        _ = t

        # A `data-app-git:sales` scoped credential, minted the same way
        # `app.api.data_apps._mint_git_credential` does — used to assert
        # this router (and only this router) accepts it.
        git_tid = str(uuid.uuid4())
        git_jwt = create_access_token(
            "owner1",
            "owner@test.local",
            token_id=git_tid,
            typ="pat",
            extra_claims={"scope": "data-app-git:sales"},
        )
        token_repo.create(
            id=git_tid,
            user_id="owner1",
            name="data-app-git:sales",
            token_hash=hashlib.sha256(git_jwt.encode()).hexdigest(),
            prefix=git_tid.replace("-", "")[:8],
            expires_at=None,
        )
    finally:
        conn.close()

    init_app_repo("sales")

    app = shared_app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    return {
        "app": app,
        "client": client,
        "owner_pat": pats["owner1"],
        "other_pat": pats["other1"],
        "admin_pat": pats["admin1"],
        "git_scoped_pat": git_jwt,
        "data_dir": data_dir,
    }


class TestDataAppsGitHttp:
    def test_requires_auth(self, data_apps_git_env):
        c = data_apps_git_env["client"]
        r = c.get("/data-apps.git/sales/info/refs?service=git-upload-pack")
        assert r.status_code == 401

    def test_push_denied_for_non_owner(self, data_apps_git_env):
        c = data_apps_git_env["client"]
        r = c.get(
            "/data-apps.git/sales/info/refs?service=git-receive-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["other_pat"])},
        )
        assert r.status_code == 403

    def test_push_allowed_for_owner(self, data_apps_git_env):
        c = data_apps_git_env["client"]
        r = c.get(
            "/data-apps.git/sales/info/refs?service=git-receive-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["owner_pat"])},
        )
        assert r.status_code == 200
        assert "git-receive-pack" in r.headers["content-type"]

    def test_push_allowed_for_admin(self, data_apps_git_env):
        c = data_apps_git_env["client"]
        r = c.get(
            "/data-apps.git/sales/info/refs?service=git-receive-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["admin_pat"])},
        )
        assert r.status_code == 200

    def test_read_allowed_for_owner(self, data_apps_git_env):
        c = data_apps_git_env["client"]
        r = c.get(
            "/data-apps.git/sales/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["owner_pat"])},
        )
        assert r.status_code == 200
        assert "git-upload-pack" in r.headers["content-type"]

    def test_read_denied_for_non_owner_without_grant(self, data_apps_git_env):
        c = data_apps_git_env["client"]
        r = c.get(
            "/data-apps.git/sales/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["other_pat"])},
        )
        assert r.status_code == 403

    def test_read_allowed_via_resource_grant(self, data_apps_git_env):
        from src.db import get_system_db
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.resource_grants import ResourceGrantsRepository

        conn = get_system_db()
        try:
            ug = UserGroupsRepository(conn)
            grp = ug.create(name="SalesViewers", description="read access to sales app")
            gid = grp["id"]
            UserGroupMembersRepository(conn).add_member("other1", gid, source="admin")
            ResourceGrantsRepository(conn).create(group_id=gid, resource_type="data_app", resource_id="sales")
        finally:
            conn.close()

        c = data_apps_git_env["client"]
        r = c.get(
            "/data-apps.git/sales/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["other_pat"])},
        )
        assert r.status_code == 200

        # A grant is read-only — push must still be denied.
        r2 = c.get(
            "/data-apps.git/sales/info/refs?service=git-receive-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["other_pat"])},
        )
        assert r2.status_code == 403

    def test_unknown_slug_404s(self, data_apps_git_env):
        c = data_apps_git_env["client"]
        r = c.get(
            "/data-apps.git/does-not-exist/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["owner_pat"])},
        )
        assert r.status_code == 404

    def test_syntactically_invalid_slug_404s_not_500(self, data_apps_git_env):
        """A URL-encoded path-traversal slug (`..%2Fevil`) must never reach
        `repo_path`'s SLUG_RE check as a 500 — `data_apps_repo().get_by_slug`
        looks it up first and finds no such registry row, so it 404s there
        (the `try/except ValueError` around `repo_path` is defense-in-depth
        for the theoretical case a malformed slug ever reaches that far)."""
        c = data_apps_git_env["client"]
        r = c.get(
            "/data-apps.git/..%2Fevil/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["owner_pat"])},
        )
        assert r.status_code == 404

    def test_disabled_flag_404s(self, data_apps_git_env):
        import app.instance_config as instance_config
        from app.instance_config import get_data_apps_config

        # Flip the cached config off without touching disk.
        original = instance_config._instance_config
        instance_config._instance_config = {**(original or {}), "data_apps": {"enabled": False}}
        try:
            assert get_data_apps_config().get("enabled") is False
            c = data_apps_git_env["client"]
            r = c.get(
                "/data-apps.git/sales/info/refs?service=git-upload-pack",
                headers={"Authorization": _basic("x", data_apps_git_env["owner_pat"])},
            )
            assert r.status_code == 404
            assert r.json()["detail"] == "data_apps_disabled"
        finally:
            instance_config._instance_config = original


class TestDataAppsGitScopedCredential:
    """A `data-app-git:<slug>` credential (as minted by
    `_mint_git_credential`) must work here — this is the one surface it's
    meant for — but nowhere else (see `TestGitScopeRejectedOnJsonApi` in
    tests/test_data_apps_api.py)."""

    def test_git_scoped_pat_allowed_for_read(self, data_apps_git_env):
        c = data_apps_git_env["client"]
        r = c.get(
            "/data-apps.git/sales/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["git_scoped_pat"])},
        )
        assert r.status_code == 200
        assert "git-upload-pack" in r.headers["content-type"]

    def test_git_scoped_pat_allowed_for_push(self, data_apps_git_env):
        c = data_apps_git_env["client"]
        r = c.get(
            "/data-apps.git/sales/info/refs?service=git-receive-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["git_scoped_pat"])},
        )
        assert r.status_code == 200

    def test_git_scoped_pat_pinned_to_its_own_slug(self, data_apps_git_env):
        """A `data-app-git:sales` token must not authenticate against a
        differently-named repo, even one the same user owns."""
        from src.data_apps.git_repos import init_app_repo
        from src.repositories.data_apps import DataAppsRepository
        from src.db import get_system_db

        conn = get_system_db()
        try:
            DataAppsRepository(conn).create(slug="other", name="Other App", owner_user_id="owner1")
        finally:
            conn.close()
        init_app_repo("other")

        c = data_apps_git_env["client"]
        r = c.get(
            "/data-apps.git/other/info/refs?service=git-upload-pack",
            headers={"Authorization": _basic("x", data_apps_git_env["git_scoped_pat"])},
        )
        assert r.status_code == 403
