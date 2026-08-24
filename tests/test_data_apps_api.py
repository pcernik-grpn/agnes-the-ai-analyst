"""Tests for the control-plane REST API (`/api/data-apps/...`).

Follows the fixture idiom of ``tests/test_data_apps_git.py``'s
``data_apps_git_env`` — real user + PAT rows via the DuckDB repos, feature
flag flipped on in an ``instance.yaml`` overlay, a real ``TestClient(app)``.

``fake_runner``/``dead_runner`` monkeypatch the module-level
``app.api.data_apps._runner`` indirection (the documented seam) with a stub
recording ``up_calls``/``stop_calls``/``logs_calls`` or one that always
raises ``RunnerUnavailable``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid

import pytest
import yaml
from cryptography.fernet import Fernet

from src.data_apps.runner_client import RunnerError, RunnerUnavailable


def _auth(pat: str) -> dict:
    return {"Authorization": f"Bearer {pat}"}


@pytest.fixture
def api_env(e2e_env, monkeypatch, shared_app):
    """Real user/token/group rows + TestClient(app), data_apps enabled."""
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    data_dir = e2e_env["data_dir"]
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())

    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    import app.instance_config as instance_config

    instance_config._instance_config = None

    conn = get_system_db()
    try:
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
    finally:
        conn.close()

    app = shared_app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    return {
        "client": client,
        "owner_pat": pats["owner1"],
        "other_pat": pats["other1"],
        "admin_pat": pats["admin1"],
        "data_dir": data_dir,
    }


@pytest.fixture
def client_as_user(api_env):
    c = api_env["client"]
    headers = _auth(api_env["owner_pat"])

    class _Wrapped:
        def get(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.get(url, **kw)

        def post(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.post(url, **kw)

        def put(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.put(url, **kw)

        def delete(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.delete(url, **kw)

    return _Wrapped()


@pytest.fixture
def client_as_other_user(api_env):
    c = api_env["client"]
    headers = _auth(api_env["other_pat"])

    class _Wrapped:
        def get(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.get(url, **kw)

        def post(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.post(url, **kw)

        def put(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.put(url, **kw)

        def delete(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.delete(url, **kw)

    return _Wrapped()


@pytest.fixture
def admin_client(api_env):
    c = api_env["client"]
    headers = _auth(api_env["admin_pat"])

    class _Wrapped:
        def get(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.get(url, **kw)

        def post(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.post(url, **kw)

        def put(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.put(url, **kw)

        def delete(self, url, **kw):
            kw.setdefault("headers", headers)
            return c.delete(url, **kw)

    return _Wrapped()


def _on_event_loop() -> bool:
    """True when the caller is executing on the asyncio event-loop thread.

    ``asyncio.get_running_loop()`` only succeeds inside a running loop's own
    thread; from an anyio worker thread (where ``run_in_threadpool`` puts a
    blocking callable) it raises ``RuntimeError``. That makes it a precise,
    non-flaky probe for "did this blocking runner call execute ON the single
    uvicorn event loop" — see ``TestRunnerCallsAreOffloaded``.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _FakeRunner:
    def __init__(self):
        self.up_calls = []
        self.stop_calls = []
        self.logs_calls = []
        # (method, ran_on_event_loop) for every call — see `_on_event_loop`.
        self.thread_checks = []
        self._status = {"container": "running", "ready": True}

    def up(self, slug, spec, config_json):
        self.thread_checks.append(("up", _on_event_loop()))
        self.up_calls.append((slug, spec, config_json))
        return {"container": "running", "ready": True}

    def stop(self, slug, mode="recreate"):
        self.thread_checks.append(("stop", _on_event_loop()))
        self.stop_calls.append((slug, mode))
        return {"container": "stopped", "ready": False}

    def resume(self, slug):
        self.thread_checks.append(("resume", _on_event_loop()))
        return {"container": "running", "ready": True}

    def status(self, slug):
        self.thread_checks.append(("status", _on_event_loop()))
        return self._status

    def logs(self, slug, tail=200):
        self.thread_checks.append(("logs", _on_event_loop()))
        self.logs_calls.append((slug, tail))
        return "log line 1\nlog line 2\n"

    def on_loop(self) -> list[str]:
        return [name for name, on_loop in self.thread_checks if on_loop]


class _DeadRunner:
    def up(self, slug, spec, config_json):
        raise RunnerUnavailable("connection refused")

    def stop(self, slug, mode="recreate"):
        raise RunnerUnavailable("connection refused")

    def resume(self, slug):
        raise RunnerUnavailable("connection refused")

    def status(self, slug):
        raise RunnerUnavailable("connection refused")

    def logs(self, slug, tail=200):
        raise RunnerUnavailable("connection refused")


@pytest.fixture
def fake_runner(monkeypatch):
    import app.api.data_apps as data_apps_api

    runner = _FakeRunner()
    monkeypatch.setattr(data_apps_api, "_runner", lambda: runner)
    return runner


@pytest.fixture
def dead_runner(monkeypatch):
    import app.api.data_apps as data_apps_api

    runner = _DeadRunner()
    monkeypatch.setattr(data_apps_api, "_runner", lambda: runner)
    return runner


def _seed_app_with_commit(data_dir, slug="sapp", owner_id="owner1"):
    """Register a `data_apps` row + materialize its bare repo with one
    commit on `main` — the shape `deploy` needs to succeed."""
    from src.data_apps.git_repos import init_app_repo
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).create(slug=slug, name=slug.upper(), owner_user_id=owner_id)
    finally:
        conn.close()

    repo_dir = init_app_repo(slug)
    work = data_dir / f"work-{slug}"
    subprocess.run(["git", "clone", str(repo_dir), str(work)], check=True, capture_output=True)
    (work / "app.py").write_text("print('hi')")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "c1"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD:main"], check=True, capture_output=True)


def _seed_empty_app(slug="empty1", owner_id="owner1"):
    from src.data_apps.git_repos import init_app_repo
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).create(slug=slug, name=slug.upper(), owner_user_id=owner_id)
    finally:
        conn.close()
    init_app_repo(slug)


def _seed_external_app(slug="eapp", owner_id="owner1", repo_url="https://example.com/org/app.git", repo_branch="main"):
    """Register a `repo_mode="external"` app row — no internal bare repo is
    ever created for these (`init_app_repo` is internal-only at create), so
    deploy must never touch `fast_forward_live`."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).create(
            slug=slug,
            name=slug.upper(),
            owner_user_id=owner_id,
            repo_mode="external",
            repo_url=repo_url,
            repo_branch=repo_branch,
        )
    finally:
        conn.close()


@pytest.fixture
def seeded_repo_with_commit(api_env):
    _seed_app_with_commit(api_env["data_dir"], slug="sapp", owner_id="owner1")
    return "sapp"


@pytest.fixture
def running_idle_app(api_env):
    """A `running` app whose `last_request_at` is far in the past — should
    be picked up by `list_idle`."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="sapp", name="S", owner_user_id="owner1", idle_timeout_s=300)
        repo.set_state(app_id, "running")
        conn.execute(
            "UPDATE data_apps SET last_request_at = now() - INTERVAL 2 HOUR WHERE id = ?",
            [app_id],
        )
    finally:
        conn.close()
    return "sapp"


@pytest.fixture
def running_active_app(api_env):
    """A `running` app whose `last_request_at` is recent (now) — mirrors
    `running_idle_app` but must NOT be picked up by the reap-idle sweep."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="active2", name="Active2", owner_user_id="owner1", idle_timeout_s=300)
        repo.set_state(app_id, "running")
        conn.execute("UPDATE data_apps SET last_request_at = now() WHERE id = ?", [app_id])
    finally:
        conn.close()
    return "active2"


@pytest.fixture
def running_idle_app_with_token(api_env):
    """Like `running_idle_app`, but with a real service token attached —
    proves reap-idle's SLEEP transition must NOT revoke it (unlike explicit
    stop/delete): a sleeping app needs its token to wake later."""
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.data_apps import DataAppsRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="idletok", name="IdleTok", owner_user_id="owner1", idle_timeout_s=300)
        repo.set_state(app_id, "running")
        conn.execute(
            "UPDATE data_apps SET last_request_at = now() - INTERVAL 2 HOUR WHERE id = ?",
            [app_id],
        )
        tid = str(uuid.uuid4())
        jwt_token = create_access_token("owner1", "owner@test.local", token_id=tid, typ="pat")
        AccessTokenRepository(conn).create(
            id=tid,
            user_id="owner1",
            name="data-app:idletok",
            token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
            prefix=tid.replace("-", "")[:8],
            expires_at=None,
        )
        repo.update(app_id, service_token_id=tid)
    finally:
        conn.close()
    return "idletok", tid


@pytest.fixture
def running_dead_container_app(api_env):
    """A `running` row whose container is actually dead. A first-deploy crash
    loop lands in `running` (not `deploying`), so the stale-deploying scan
    never sees it. `updated_at` far in the past (past the start grace);
    `last_request_at` recent so the idle sweep itself would skip it — only the
    reconcile scan should catch it."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="crash1", name="Crash1", owner_user_id="owner1", idle_timeout_s=300)
        repo.set_state(app_id, "running")
        conn.execute(
            "UPDATE data_apps SET updated_at = now() - INTERVAL 20 MINUTE, last_request_at = now() WHERE id = ?",
            [app_id],
        )
    finally:
        conn.close()
    return "crash1"


@pytest.fixture
def stale_deploying_app(api_env):
    """A `deploying` app whose `updated_at` is far in the past — a wake or
    operator-deploy that never finished. Should be recovered (-> `error`)
    by the reap-idle sweep's stale-deploying scan."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="stuck1", name="Stuck1", owner_user_id="owner1")
        repo.set_state(app_id, "deploying", "waking")
        conn.execute(
            "UPDATE data_apps SET updated_at = now() - INTERVAL 20 MINUTE WHERE id = ?",
            [app_id],
        )
    finally:
        conn.close()
    return "stuck1"


@pytest.fixture
def fresh_deploying_app(api_env):
    """A `deploying` app whose `updated_at` is recent — mirrors
    `stale_deploying_app` but must NOT be touched by the sweep (a wake that's
    genuinely still in flight)."""
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        repo = DataAppsRepository(conn)
        app_id = repo.create(slug="fresh1", name="Fresh1", owner_user_id="owner1")
        repo.set_state(app_id, "deploying", "waking")
    finally:
        conn.close()
    return "fresh1"


class TestCrud:
    def test_create_and_quota(self, client_as_user):
        for i in range(3):
            r = client_as_user.post("/api/data-apps", json={"slug": f"a{i}", "name": f"A{i}"})
            assert r.status_code == 201, r.text
        r = client_as_user.post("/api/data-apps", json={"slug": "a3", "name": "A3"})
        assert r.status_code == 403
        assert r.json()["detail"] == "app_quota_exceeded"

    def test_create_quota_race_lease_conflict(self, client_as_user, monkeypatch):
        """When the create-lease can't be acquired for a non-admin caller
        (already held by a concurrent request for the same user), create
        is rejected rather than racing the count-then-create quota check."""
        import app.coordination.factory as coord_factory

        class _AlwaysBusyBackend:
            def lease_acquire(self, name, holder_id, *, ttl_s):
                return False

            def lease_release(self, name, holder_id):
                pass

        monkeypatch.setattr(coord_factory, "coordination", lambda: _AlwaysBusyBackend())
        r = client_as_user.post("/api/data-apps", json={"slug": "racy1", "name": "R"})
        assert r.status_code == 409
        assert r.json()["detail"] == "create_in_progress"

    def test_create_returns_git_url(self, client_as_user):
        r = client_as_user.post("/api/data-apps", json={"slug": "gitcheck", "name": "G"})
        assert r.status_code == 201
        body = r.json()
        assert body["slug"] == "gitcheck"
        assert "data-apps.git/gitcheck" in body["git_url"]
        assert "id" in body

    def test_slug_validation(self, client_as_user):
        r = client_as_user.post("/api/data-apps", json={"slug": "Bad_Slug", "name": "x"})
        assert r.status_code == 400

    def test_reserved_slug_rejected(self, client_as_user):
        """ "detail" is a literal path segment the web UI's
        `GET /apps/detail/{slug}` route owns (app/web/router.py's
        `apps_web_router`) — a data app named "detail" would have its own
        sub-paths swallowed by that route instead of reaching the proxy.
        Rejected at create time (`src.data_apps.spec.RESERVED_SLUGS`) so the
        collision can never happen, rather than relying on route-registration
        order alone."""
        r = client_as_user.post("/api/data-apps", json={"slug": "detail", "name": "x"})
        assert r.status_code == 400
        assert r.json()["detail"] == "reserved_slug"

    def test_git_slug_rejected(self, client_as_user):
        """A data app named "git" would make ``_rmtree_config_dir`` delete
        ``${DATA_DIR}/apps/git`` — which is not that app's config directory but
        the *shared* store every app's bare repo lives in
        (``src.data_apps.git_repos.repo_path`` → ``apps/git/<slug>.git``). One
        `DELETE /api/data-apps/git` would take every other hosted app's git
        history with it. `SLUG_RE` accepts "git", so the create-time guard is
        what has to stop it."""
        r = client_as_user.post("/api/data-apps", json={"slug": "git", "name": "x"})
        assert r.status_code == 400
        assert r.json()["detail"] == "reserved_slug"

    def test_rmtree_config_dir_refuses_the_shared_repo_root(self, tmp_path, monkeypatch):
        """Belt and braces for the check above: even if a "git"-slugged row
        reached the database some other way (a pre-existing row from before the
        guard, a direct insert), the delete path itself must refuse to remove
        the shared repo root."""
        from app.api.data_apps import _rmtree_config_dir

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        repo_root = tmp_path / "apps" / "git"
        (repo_root / "other-app.git").mkdir(parents=True)
        _rmtree_config_dir("git")
        assert (repo_root / "other-app.git").exists(), "the shared bare-repo store was deleted"

    def test_duplicate_slug_conflict(self, client_as_user):
        r1 = client_as_user.post("/api/data-apps", json={"slug": "dupe", "name": "One"})
        assert r1.status_code == 201
        r2 = client_as_user.post("/api/data-apps", json={"slug": "dupe", "name": "Two"})
        assert r2.status_code == 409

    def test_list_hides_secrets(self, client_as_user):
        client_as_user.post("/api/data-apps", json={"slug": "sh", "name": "SH"})
        rows = client_as_user.get("/api/data-apps").json()
        assert rows
        row = next(r for r in rows if r["slug"] == "sh")
        assert "secrets_enc" not in row
        assert "service_token_id" not in row
        assert row["url"] == "/apps/sh/"

    def test_feature_disabled_404s(self, api_env, monkeypatch):
        import app.instance_config as instance_config

        original = instance_config._instance_config
        instance_config._instance_config = {**(original or {}), "data_apps": {"enabled": False}}
        try:
            c = api_env["client"]
            r = c.get("/api/data-apps", headers=_auth(api_env["owner_pat"]))
            assert r.status_code == 404
            assert r.json()["detail"] == "data_apps_disabled"
        finally:
            instance_config._instance_config = original


class TestDetailRbac:
    def test_owner_can_view(self, client_as_user):
        client_as_user.post("/api/data-apps", json={"slug": "rbac1", "name": "R"})
        r = client_as_user.get("/api/data-apps/rbac1")
        assert r.status_code == 200

    def test_stranger_forbidden(self, client_as_user, client_as_other_user):
        client_as_user.post("/api/data-apps", json={"slug": "rbac2", "name": "R"})
        r = client_as_other_user.get("/api/data-apps/rbac2")
        assert r.status_code == 403

    def test_admin_can_view(self, client_as_user, admin_client):
        client_as_user.post("/api/data-apps", json={"slug": "rbac3", "name": "R"})
        r = admin_client.get("/api/data-apps/rbac3")
        assert r.status_code == 200

    def test_granted_group_can_view(self, client_as_user, client_as_other_user, api_env):
        client_as_user.post("/api/data-apps", json={"slug": "rbac4", "name": "R"})

        from src.db import get_system_db
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.resource_grants import ResourceGrantsRepository

        conn = get_system_db()
        try:
            gid = UserGroupsRepository(conn).create(name="Viewers", description="d")["id"]
            UserGroupMembersRepository(conn).add_member("other1", gid, source="admin")
            ResourceGrantsRepository(conn).create(group_id=gid, resource_type="data_app", resource_id="rbac4")
        finally:
            conn.close()

        r = client_as_other_user.get("/api/data-apps/rbac4")
        assert r.status_code == 200

    def test_granted_group_does_not_see_drafts(self, client_as_user, client_as_other_user, seeded_repo_with_commit):
        """A read `resource_grants` row on the parent app lets a non-owner
        view the app itself, but must not expose in-progress draft
        branch/state/URL metadata (that's owner/Admin-only, same gate as
        the draft-mutating endpoints) — a grantee response must omit the
        `drafts` key entirely, not just return it empty."""
        client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"})

        from src.db import get_system_db
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.resource_grants import ResourceGrantsRepository

        conn = get_system_db()
        try:
            gid = UserGroupsRepository(conn).create(name="Viewers", description="d")["id"]
            UserGroupMembersRepository(conn).add_member("other1", gid, source="admin")
            ResourceGrantsRepository(conn).create(group_id=gid, resource_type="data_app", resource_id="sapp")
        finally:
            conn.close()

        r_owner = client_as_user.get("/api/data-apps/sapp")
        assert r_owner.status_code == 200 and len(r_owner.json()["drafts"]) == 1

        r_grantee = client_as_other_user.get("/api/data-apps/sapp")
        assert r_grantee.status_code == 200
        assert "drafts" not in r_grantee.json()

    def test_missing_app_404s(self, client_as_user):
        r = client_as_user.get("/api/data-apps/does-not-exist")
        assert r.status_code == 404


class TestDeploy:
    def test_deploy_happy_path(self, client_as_user, fake_runner, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r.status_code == 200, r.text
        assert fake_runner.up_calls

        slug, spec, config_json = fake_runner.up_calls[0]
        assert slug == "sapp"
        assert "dataApp" in config_json
        # Internal host, now with the push token embedded (the runtime image
        # won't add creds to a plain-HTTP clone URL — src/data_apps/spec.py).
        assert config_json["dataApp"]["git"]["repository"].startswith("http://agnes:")
        assert "@app:8000/data-apps.git/" in config_json["dataApp"]["git"]["repository"]
        assert "secrets" in config_json["dataApp"]

        row = client_as_user.get("/api/data-apps/sapp").json()
        assert row["state"] == "running"
        assert row["deployed_sha"]

    def test_deploy_mints_and_stores_service_token(self, client_as_user, fake_runner, seeded_repo_with_commit):
        client_as_user.post("/api/data-apps/sapp/deploy", json={})

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
        finally:
            conn.close()
        assert row["service_token_id"]

    def test_deploy_forbidden_for_stranger(
        self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit
    ):
        r = client_as_other_user.post("/api/data-apps/sapp/deploy", json={})
        assert r.status_code == 403

    def test_deploy_empty_repo_conflict(self, client_as_user, fake_runner, api_env):
        _seed_empty_app(slug="empty1", owner_id="owner1")
        r = client_as_user.post("/api/data-apps/empty1/deploy", json={})
        assert r.status_code == 409
        assert r.json()["detail"] == "deploy_empty_repo"

    def test_runner_down_sets_error(self, client_as_user, dead_runner, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r.status_code == 502
        assert r.json()["detail"] == "runner_unavailable"

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
        finally:
            conn.close()
        assert row["state"] == "error"

    def test_runner_error_sets_error(self, client_as_user, monkeypatch, seeded_repo_with_commit):
        import app.api.data_apps as data_apps_api

        class _ErrRunner:
            def up(self, slug, spec, config_json):
                raise RunnerError(500, "boom")

        monkeypatch.setattr(data_apps_api, "_runner", lambda: _ErrRunner())
        r = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r.status_code == 502
        # The sidecar ANSWERED — saying "unavailable" sends the operator to
        # look at a process that is in fact healthy. Carry its own words.
        assert r.json()["detail"] == "runner_error: boom"

    def test_runner_error_surfaces_the_runner_detail_verbatim(
        self, client_as_user, monkeypatch, seeded_repo_with_commit
    ):
        """The real incident: a cold 1.3 GB image pull blew docker-py's
        timeout, the retried create raised ImageNotFound, and the runner
        answered 400 `image_not_found` — which the app reported as
        `runner_unavailable`, sending the investigation at a healthy sidecar.
        The runner's own code must reach the caller."""
        import app.api.data_apps as data_apps_api

        class _ImageMissingRunner:
            def up(self, slug, spec, config_json):
                raise RunnerError(400, "image_not_found")

        monkeypatch.setattr(data_apps_api, "_runner", lambda: _ImageMissingRunner())
        r = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r.status_code == 502
        assert r.json()["detail"] == "runner_error: image_not_found"

        # ...and the same words must be readable afterwards off the detail
        # endpoint, which is where a returning operator actually looks.
        detail = client_as_user.get("/api/data-apps/sapp").json()
        assert detail["state"] == "error"
        assert detail["state_detail"] == "image_not_found"

    def test_logs_distinguishes_answered_runner_from_dead_one(
        self, client_as_user, monkeypatch, seeded_repo_with_commit
    ):
        """`GET /logs` collapsed both failure modes too, so the one button an
        operator presses next reported the sidecar as down whatever happened."""
        import app.api.data_apps as data_apps_api

        class _NoContainerRunner:
            def logs(self, slug, tail=200):
                raise RunnerError(404, "not_found")

        monkeypatch.setattr(data_apps_api, "_runner", lambda: _NoContainerRunner())
        r = client_as_user.get("/api/data-apps/sapp/logs")
        assert r.status_code == 502
        assert r.json()["detail"] == "runner_error: not_found"

    def test_deploy_runner_down_rolls_back_new_token(
        self, client_as_user, fake_runner, seeded_repo_with_commit, monkeypatch
    ):
        """A failed redeploy must not leave a dangling, unused service PAT
        live, and must not clobber the still-working previous token."""
        r1 = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r1.status_code == 200

        from src.db import get_system_db
        from src.repositories.access_tokens import AccessTokenRepository
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            old_token_id = DataAppsRepository(conn).get_by_slug("sapp")["service_token_id"]
            tokens_before = {t["id"] for t in AccessTokenRepository(conn).list_for_user("owner1")}
        finally:
            conn.close()
        assert old_token_id

        import app.api.data_apps as data_apps_api

        monkeypatch.setattr(data_apps_api, "_runner", lambda: _DeadRunner())
        r2 = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r2.status_code == 502
        assert r2.json()["detail"] == "runner_unavailable"

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
            tokens_after = AccessTokenRepository(conn).list_for_user("owner1")
            old_token_row = AccessTokenRepository(conn).get_by_id(old_token_id)
        finally:
            conn.close()

        # Row's service_token_id is restored to the pre-attempt (old) value.
        assert row["service_token_id"] == old_token_id
        # The previously-working token must survive a failed redeploy —
        # a sleeping-but-deployed app must still be able to wake with it.
        assert old_token_row["revoked_at"] is None

        # A deploy mints TWO credentials — the app's `data-app:` service token
        # and the container's `data-app-git:` clone token (they differ in scope
        # and lifetime; see `_mint_container_git_token`). Neither reached a
        # container on a failed attempt, so BOTH must be revoked: a live git
        # credential for an app that never deployed is exactly the dangling
        # PAT this test exists to prevent.
        new_token_ids = {t["id"] for t in tokens_after} - tokens_before
        assert len(new_token_ids) == 2, sorted(new_token_ids)
        conn = get_system_db()
        try:
            new_rows = [AccessTokenRepository(conn).get_by_id(tid) for tid in new_token_ids]
        finally:
            conn.close()
        assert all(r["revoked_at"] is not None for r in new_rows), [(r["name"], r["revoked_at"]) for r in new_rows]
        assert any("data-app-git:" in r["name"] for r in new_rows), [r["name"] for r in new_rows]

    def test_deploy_redeploy_revokes_old_stores_new(self, client_as_user, fake_runner, seeded_repo_with_commit):
        r1 = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r1.status_code == 200

        from src.db import get_system_db
        from src.repositories.access_tokens import AccessTokenRepository
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            old_token_id = DataAppsRepository(conn).get_by_slug("sapp")["service_token_id"]
        finally:
            conn.close()

        r2 = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r2.status_code == 200

        conn = get_system_db()
        try:
            new_token_id = DataAppsRepository(conn).get_by_slug("sapp")["service_token_id"]
            old_token_row = AccessTokenRepository(conn).get_by_id(old_token_id)
        finally:
            conn.close()

        assert new_token_id != old_token_id
        assert old_token_row["revoked_at"] is not None

    def test_deploy_external_repo_happy_path(self, client_as_user, fake_runner, api_env):
        """External-repo apps never get an internal bare repo (`init_app_repo`
        is internal-only at create), so deploy must not go through
        `fast_forward_live` — the runtime clones HEAD of `repo_branch` at
        boot instead of a pinned internal sha."""
        _seed_external_app(
            slug="eapp", owner_id="owner1", repo_url="https://example.com/org/app.git", repo_branch="main"
        )
        r = client_as_user.post("/api/data-apps/eapp/deploy", json={})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "running"
        assert r.json()["deployed_sha"] == ""

        assert fake_runner.up_calls
        slug, spec, config_json = fake_runner.up_calls[0]
        assert slug == "eapp"
        git = config_json["dataApp"]["git"]
        assert git["repository"] == "https://example.com/org/app.git"
        assert git["branch"] == "main"
        assert "username" not in git
        assert "#password" not in git

        # Service token is still minted for the platform API even though no
        # internal git credential is handed to the container.
        assert "AGNES_TOKEN" in config_json["dataApp"]["secrets"]

        row = client_as_user.get("/api/data-apps/eapp").json()
        assert row["state"] == "running"
        assert row["deployed_sha"] == ""

    def test_deploy_external_repo_with_sha_rejected(self, client_as_user, fake_runner, api_env):
        _seed_external_app(slug="eapp2", owner_id="owner1")
        r = client_as_user.post("/api/data-apps/eapp2/deploy", json={"sha": "abc"})
        assert r.status_code == 400
        assert r.json()["detail"] == "external_repo_sha_unsupported"
        assert not fake_runner.up_calls

    def test_deploy_dev_mode_on_draft(self, client_as_user, fake_runner, seeded_repo_with_commit):
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
        r = client_as_user.post(f"/api/data-apps/{d['slug']}/deploy", json={"mode": "dev"})
        assert r.status_code == 200, r.text
        slug, spec, cfg = fake_runner.up_calls[-1]
        assert slug == d["slug"]
        assert cfg["dataApp"]["git"]["branch"] == "init"  # draft branch, not agnes-live
        assert cfg["dataApp"]["git"]["repository"].endswith("/data-apps.git/sapp")  # PARENT repo

    def test_deploy_dev_requires_draft(self, client_as_user, fake_runner, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/deploy", json={"mode": "dev"})
        assert r.status_code == 400 and r.json()["detail"] == "dev_requires_draft"

    def test_deploy_prod_on_draft_rejected(self, client_as_user, fake_runner, seeded_repo_with_commit):
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
        r = client_as_user.post(f"/api/data-apps/{d['slug']}/deploy", json={})
        assert r.status_code == 400 and r.json()["detail"] == "prod_on_draft"


class TestStop:
    def test_stop_happy_path(self, client_as_user, fake_runner, seeded_repo_with_commit):
        client_as_user.post("/api/data-apps/sapp/deploy", json={})
        r = client_as_user.post("/api/data-apps/sapp/stop")
        assert r.status_code == 200
        assert fake_runner.stop_calls
        row = client_as_user.get("/api/data-apps/sapp").json()
        assert row["state"] == "stopped"

    def test_stop_forbidden_for_stranger(
        self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit
    ):
        r = client_as_other_user.post("/api/data-apps/sapp/stop")
        assert r.status_code == 403

    def test_stop_revokes_service_token(self, client_as_user, fake_runner, seeded_repo_with_commit):
        """Spec §8/§10: stop must revoke the app's service token (only sleep
        via reap-idle keeps it live, for wake) — see TestReap's
        `test_reap_idle_sleep_does_not_revoke_token` for the contrast."""
        client_as_user.post("/api/data-apps/sapp/deploy", json={})

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            token_id = DataAppsRepository(conn).get_by_slug("sapp")["service_token_id"]
        finally:
            conn.close()
        assert token_id

        r = client_as_user.post("/api/data-apps/sapp/stop")
        assert r.status_code == 200

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
            from src.repositories.access_tokens import AccessTokenRepository

            token_row = AccessTokenRepository(conn).get_by_id(token_id)
        finally:
            conn.close()
        assert row["service_token_id"] == ""
        assert token_row["revoked_at"] is not None


class TestOpLeaseSerialization:
    """`dataapp:op:{slug}` — the lease shared by `deploy_data_app`,
    `stop_data_app`, and the ingress proxy's `_trigger_wake`
    (`app/api/data_apps_proxy.py`) so at most one runner-mutating
    operation is ever in flight per app. Regression coverage for the race
    flagged in PR #1002's review: `deploy_data_app`/`stop_data_app` used
    to call the runner directly with no lease at all, so a manual deploy
    could race an in-flight auto-wake for the same slug and both call
    `runner.up()` concurrently.
    """

    def test_deploy_409s_when_op_lease_held_elsewhere(self, client_as_user, fake_runner, seeded_repo_with_commit):
        """Simulates another in-flight operation (e.g. the proxy's
        `_trigger_wake`, which never explicitly releases the lease — see
        that function's docstring) already holding the lease for this
        slug. `deploy_data_app` must not proceed to `redeploy_current`."""
        from app.api.data_apps import release_op_lease, try_acquire_op_lease

        acquired, holder = try_acquire_op_lease("sapp")
        assert acquired
        try:
            r = client_as_user.post("/api/data-apps/sapp/deploy", json={})
            assert r.status_code == 409
            assert r.json()["detail"] == "operation_in_progress"
            assert not fake_runner.up_calls
        finally:
            release_op_lease("sapp", holder)

    def test_stop_409s_when_op_lease_held_elsewhere(self, client_as_user, fake_runner, seeded_repo_with_commit):
        from app.api.data_apps import release_op_lease, try_acquire_op_lease

        client_as_user.post("/api/data-apps/sapp/deploy", json={})
        fake_runner.up_calls.clear()

        acquired, holder = try_acquire_op_lease("sapp")
        assert acquired
        try:
            r = client_as_user.post("/api/data-apps/sapp/stop")
            assert r.status_code == 409
            assert r.json()["detail"] == "operation_in_progress"
            assert not fake_runner.stop_calls
        finally:
            release_op_lease("sapp", holder)

    def test_delete_409s_when_op_lease_held_elsewhere(self, client_as_user, fake_runner, seeded_repo_with_commit):
        """`DELETE /{slug}` also calls `runner.stop()` (best-effort) — it must
        take the same op lease as deploy/stop so a delete can't race an
        in-flight deploy/wake and hit the unlocked `runner.up()` window."""
        from app.api.data_apps import release_op_lease, try_acquire_op_lease

        acquired, holder = try_acquire_op_lease("sapp")
        assert acquired
        try:
            r = client_as_user.delete("/api/data-apps/sapp")
            assert r.status_code == 409
            assert r.json()["detail"] == "operation_in_progress"
            assert not fake_runner.stop_calls
        finally:
            release_op_lease("sapp", holder)

    def test_concurrent_deploy_calls_never_overlap_inside_runner_up(
        self, client_as_user, monkeypatch, seeded_repo_with_commit
    ):
        """The actual race this lease closes: two `up()` invocations for the
        same slug running at once (`services/apps_runner/api.py::up()` does
        an unlocked check-then-act — get old container, remove, run new).
        Runs two real concurrent `deploy` requests through a runner stub
        whose `up()` blocks on a latch the test only releases AFTER the
        second request has completed — the lease is provably held for the
        second request's entire lifetime, so it must exhaust
        `require_op_lease`'s retries and 409, however slowly a loaded CI
        box schedules it. (An earlier version held `up()` open with a
        fixed 0.5s sleep and relied on the second request's ~0.2s
        retry window elapsing inside it; CI stretch let the first deploy
        release the lease early and both returned 200.)"""
        import threading

        import app.api.data_apps as data_apps_api

        inside = {"current": 0, "peak": 0}
        lock = threading.Lock()
        first_call_entered = threading.Event()
        release_first_call = threading.Event()

        class _BlockingRunner:
            def up(self, slug, spec, config_json):
                with lock:
                    inside["current"] += 1
                    inside["peak"] = max(inside["peak"], inside["current"])
                first_call_entered.set()
                try:
                    assert release_first_call.wait(timeout=30), "test never released the first up() call"
                    return {"container": "running", "ready": True}
                finally:
                    with lock:
                        inside["current"] -= 1

        monkeypatch.setattr(data_apps_api, "_runner", lambda: _BlockingRunner())

        results = []

        def _deploy():
            results.append(client_as_user.post("/api/data-apps/sapp/deploy", json={}))

        t1 = threading.Thread(target=_deploy)
        t1.start()
        try:
            assert first_call_entered.wait(timeout=5), "first deploy never reached runner.up()"
            r2 = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        finally:
            release_first_call.set()
        t1.join(timeout=10)
        assert not t1.is_alive(), "first deploy request never finished"

        assert inside["peak"] == 1, (
            f"two deploys called runner.up() concurrently for the same slug (peak={inside['peak']})"
        )
        assert len(results) == 1
        r1 = results[0]
        # The first request is inside runner.up() (lease held) for the whole
        # of the second request, so the outcome is fully deterministic: the
        # holder succeeds, the latecomer is rejected — never both.
        assert r1.status_code == 200, (r1.status_code, r1.text)
        assert r2.status_code == 409, (r2.status_code, r2.text)
        assert r2.json()["detail"] == "operation_in_progress"


class TestDelete:
    def test_delete_happy_path(self, client_as_user, fake_runner, seeded_repo_with_commit):
        client_as_user.post("/api/data-apps/sapp/deploy", json={})

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            token_id = DataAppsRepository(conn).get_by_slug("sapp")["service_token_id"]
        finally:
            conn.close()
        assert token_id

        r = client_as_user.delete("/api/data-apps/sapp")
        assert r.status_code == 204, r.text
        assert not r.content
        assert fake_runner.stop_calls

        conn = get_system_db()
        try:
            assert DataAppsRepository(conn).get_by_slug("sapp") is None
            from src.repositories.access_tokens import AccessTokenRepository

            token_row = AccessTokenRepository(conn).get_by_id(token_id)
            assert token_row["revoked_at"] is not None
        finally:
            conn.close()

    def test_delete_forbidden_for_stranger(
        self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit
    ):
        r = client_as_other_user.delete("/api/data-apps/sapp")
        assert r.status_code == 403

    def test_delete_rejects_draft_slug(self, client_as_user, seeded_repo_with_commit):
        """A draft is a full `data_apps` row, so `DELETE /{slug}` would
        otherwise happily resolve and delete it — but this route's own
        teardown never deletes the draft's branch on the PARENT's repo
        (only `_teardown_draft`, used by the dedicated drafts route and the
        prod-delete cascade, does). Must reject rather than silently orphan
        the branch."""
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
        r = client_as_user.delete(f"/api/data-apps/{d['slug']}")
        assert r.status_code == 400 and r.json()["detail"] == "use_draft_delete_route"

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            assert DataAppsRepository(conn).get_by_slug(d["slug"]) is not None
        finally:
            conn.close()

    def test_delete_removes_config_dir(self, client_as_user, fake_runner, seeded_repo_with_commit, api_env):
        """The leftover `config.json` under `${DATA_DIR}/apps/<slug>` holds a
        now-revoked JWT — best-effort hygiene cleanup on delete, distinct
        from the git repo directory (which is intentionally kept)."""
        config_dir = api_env["data_dir"] / "apps" / "sapp"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text("{}")

        r = client_as_user.delete("/api/data-apps/sapp")
        assert r.status_code == 204

        assert not config_dir.exists()


class TestSecrets:
    def test_put_secrets_owner(self, client_as_user):
        client_as_user.post("/api/data-apps", json={"slug": "sec1", "name": "S"})
        r = client_as_user.put("/api/data-apps/sec1/secrets", json={"secrets": {"API_KEY": "xyz"}})
        assert r.status_code == 200

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository
        from app.secrets_vault import decrypt_secret

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sec1")
        finally:
            conn.close()
        assert row["secrets_enc"]
        decrypted = json.loads(decrypt_secret(row["secrets_enc"].encode("ascii")))
        assert decrypted == {"API_KEY": "xyz"}

    def test_put_secrets_forbidden_for_stranger(self, client_as_user, client_as_other_user):
        client_as_user.post("/api/data-apps", json={"slug": "sec2", "name": "S"})
        r = client_as_other_user.put("/api/data-apps/sec2/secrets", json={"secrets": {"K": "v"}})
        assert r.status_code == 403

    def test_deploy_includes_secrets_in_config_json(self, client_as_user, fake_runner, seeded_repo_with_commit):
        client_as_user.put("/api/data-apps/sapp/secrets", json={"secrets": {"MY_SECRET": "hunter2"}})
        client_as_user.post("/api/data-apps/sapp/deploy", json={})

        assert fake_runner.up_calls
        _, _, config_json = fake_runner.up_calls[0]
        assert config_json["dataApp"]["secrets"]["#MY_SECRET"] == "hunter2"


class TestLogs:
    def test_logs_owner_only(self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit):
        r = client_as_user.get("/api/data-apps/sapp/logs?tail=50")
        assert r.status_code == 200
        assert fake_runner.logs_calls == [("sapp", 50)]

        r2 = client_as_other_user.get("/api/data-apps/sapp/logs")
        assert r2.status_code == 403


class TestReadiness:
    def test_readiness_for_granted_viewer(
        self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit, api_env
    ):
        client_as_user.post("/api/data-apps/sapp/deploy", json={})

        from src.db import get_system_db
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.resource_grants import ResourceGrantsRepository

        conn = get_system_db()
        try:
            gid = UserGroupsRepository(conn).create(name="ReadyViewers", description="d")["id"]
            UserGroupMembersRepository(conn).add_member("other1", gid, source="admin")
            ResourceGrantsRepository(conn).create(group_id=gid, resource_type="data_app", resource_id="sapp")
        finally:
            conn.close()

        r = client_as_other_user.get("/api/data-apps/sapp/readiness")
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "running"
        assert body["ready"] is True

    def test_readiness_forbidden_for_stranger(
        self, client_as_user, client_as_other_user, fake_runner, seeded_repo_with_commit
    ):
        r = client_as_other_user.get("/api/data-apps/sapp/readiness")
        assert r.status_code == 403

    def test_readiness_created_state_not_ready(self, client_as_user):
        client_as_user.post("/api/data-apps", json={"slug": "notready", "name": "N"})
        r = client_as_user.get("/api/data-apps/notready/readiness")
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "created"
        assert body["ready"] is False


class TestReap:
    def test_reap_idle_skips_app_with_op_lease_held_elsewhere(self, admin_client, fake_runner, running_idle_app):
        """The idle sweep must not race an in-flight deploy/stop/wake for the
        same slug — it should skip a leased app (leaving it running for the
        next tick) rather than blocking, erroring, or calling runner.stop()
        anyway."""
        from app.api.data_apps import release_op_lease, try_acquire_op_lease

        acquired, holder = try_acquire_op_lease("sapp")
        assert acquired
        try:
            r = admin_client.post("/api/data-apps/reap-idle")
            assert r.status_code == 200
            assert r.json()["reaped"] == []
            assert not fake_runner.stop_calls
        finally:
            release_op_lease("sapp", holder)

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
        finally:
            conn.close()
        assert row["state"] == "running"

    def test_reap_idle(self, admin_client, fake_runner, running_idle_app):
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reaped"] == ["sapp"]
        assert fake_runner.stop_calls == [("sapp", "recreate")]

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
        finally:
            conn.close()
        assert row["state"] == "sleeping"

    def test_reap_idle_sleep_does_not_revoke_token(self, admin_client, fake_runner, running_idle_app_with_token):
        """Contrast with `TestStop.test_stop_revokes_service_token`: only
        explicit stop/delete revoke — the idle sweep's sleep transition must
        leave a sleeping app's service token live so it can wake later."""
        slug, token_id = running_idle_app_with_token
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reaped"] == [slug]

        from src.db import get_system_db
        from src.repositories.access_tokens import AccessTokenRepository
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug(slug)
            token_row = AccessTokenRepository(conn).get_by_id(token_id)
        finally:
            conn.close()
        assert row["state"] == "sleeping"
        assert row["service_token_id"] == token_id
        assert token_row["revoked_at"] is None

    def test_reap_idle_requires_admin(self, client_as_user, fake_runner, running_idle_app):
        r = client_as_user.post("/api/data-apps/reap-idle")
        assert r.status_code == 403

    def test_reap_idle_skips_never_deployed_app(self, admin_client, fake_runner, client_as_user):
        """A freshly-created app (state='created', never deployed) is not
        even a reap candidate — `list(state='running')` filters it out
        before the idle-threshold check ever runs. (Previously misnamed
        `test_reap_idle_skips_recently_active` — it never actually
        exercised a 'running' app; see `test_reap_idle_skips_recently_active_running_app`
        below for that case.)"""
        client_as_user.post("/api/data-apps", json={"slug": "active1", "name": "A"})
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reaped"] == []

    def test_reap_idle_skips_recently_active_running_app(self, admin_client, fake_runner, running_active_app):
        """A `running` app whose `last_request_at` is recent must be left
        alone — reap-idle's per-app `idle_timeout_s` check must not fire
        just because the app happens to be in scanning scope."""
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reaped"] == []
        assert fake_runner.stop_calls == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("active2")
        finally:
            conn.close()
        assert row["state"] == "running"

    def test_reap_idle_recovers_stale_deploying_app_when_runner_ready(
        self, admin_client, fake_runner, stale_deploying_app
    ):
        """A `deploying` row stuck past `_DEPLOY_STALE_TIMEOUT_S` is checked
        against the runner before being declared dead: if the runner reports
        the container is actually up and ready (a `readiness` poll that
        never happened to fire, say), the row is recovered to `running`
        rather than errored out from under a perfectly good deploy."""
        fake_runner._status = {"container": "running", "ready": True}
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        body = r.json()
        assert body["recovered"] == ["stuck1"]
        assert body["timed_out"] == []
        assert body["reaped"] == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("stuck1")
        finally:
            conn.close()
        assert row["state"] == "running"

    def test_reap_idle_recovers_stale_deploying_app_when_runner_not_ready(
        self, admin_client, fake_runner, stale_deploying_app
    ):
        """Same stale row, but the runner reports the container absent/not
        ready — genuinely dead, so it's flipped to `error` (not left wedged
        forever), reported separately from `reaped`/`recovered`."""
        fake_runner._status = {"container": "absent", "ready": False}
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        body = r.json()
        assert body["timed_out"] == ["stuck1"]
        assert body["recovered"] == []
        assert body["reaped"] == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("stuck1")
        finally:
            conn.close()
        assert row["state"] == "error"
        assert row["state_detail"] == "wake/deploy timed out"

    def test_reap_idle_leaves_fresh_deploying_app_untouched(self, admin_client, fake_runner, fresh_deploying_app):
        """A `deploying` row that's genuinely still in flight (recent
        `updated_at`) must not be touched by the stale-deploying scan."""
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["timed_out"] == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("fresh1")
        finally:
            conn.close()
        assert row["state"] == "deploying"

    @staticmethod
    def _age_out(slug):
        """Push a row's `updated_at` back past the start grace, the way real time
        does between two scheduler ticks."""
        from src.db import get_system_db

        conn = get_system_db()
        try:
            conn.execute(
                "UPDATE data_apps SET updated_at = now() - INTERVAL 20 MINUTE WHERE slug = ?",
                [slug],
            )
        finally:
            conn.close()

    def test_reap_idle_reconciles_running_app_with_dead_container(
        self, admin_client, fake_runner, running_dead_container_app
    ):
        """A `running` row whose container the runner reports as dead
        (`stopped`/`absent`) is reconciled to `error` — but only once a SECOND
        sweep agrees. The first sighting leaves the row `running` with a pending
        note, because the runner folds Docker's `restarting` into "stopped" and a
        healthy app inside its restart backoff would otherwise be latched to
        `error` the ingress proxy never re-checks."""
        fake_runner._status = {"container": "stopped", "ready": False}

        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reconciled"] == [], "a single dead reading must not be enough"

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("crash1")
        finally:
            conn.close()
        assert row["state"] == "running"
        assert "reconcile-pending" in row["state_detail"]

        self._age_out("crash1")
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reconciled"] == ["crash1"]

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("crash1")
        finally:
            conn.close()
        assert row["state"] == "error"
        assert "running" in row["state_detail"]

    def test_reap_idle_reconcile_clears_a_pending_note_when_the_container_recovers(
        self, admin_client, fake_runner, running_dead_container_app
    ):
        """A transient restart must not accumulate towards `error`. Once the
        container reports `running` again, the pending note is dropped, so the
        next dead reading starts the two-sweep count over."""
        fake_runner._status = {"container": "stopped", "ready": False}
        assert admin_client.post("/api/data-apps/reap-idle").json()["reconciled"] == []

        fake_runner._status = {"container": "running", "ready": True}
        self._age_out("crash1")
        assert admin_client.post("/api/data-apps/reap-idle").json()["reconciled"] == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("crash1")
        finally:
            conn.close()
        assert row["state"] == "running"
        assert "reconcile-pending" not in (row["state_detail"] or "")

        # ...and a dead reading now needs two sweeps again, not one.
        fake_runner._status = {"container": "stopped", "ready": False}
        self._age_out("crash1")
        assert admin_client.post("/api/data-apps/reap-idle").json()["reconciled"] == []

    def test_reap_idle_leaves_healthy_running_app_alone(self, admin_client, fake_runner, running_dead_container_app):
        """Guard against false positives: a `running` row whose container the
        runner still reports as `running` must NOT be reconciled to error,
        even once it is past the start grace."""
        fake_runner._status = {"container": "running", "ready": True}
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reconciled"] == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("crash1")
        finally:
            conn.close()
        assert row["state"] == "running"

    def test_reap_idle_reconcile_does_not_resurrect_running_when_clearing_a_note(
        self, admin_client, fake_runner, running_dead_container_app
    ):
        """The note-clearing write is the one place a live container is written
        back to `running`, and `row` comes from a snapshot taken before the loop
        began. A stop that lands in between leaves the row `sleeping` while the
        container is not yet removed — so the probe still says "running" — and an
        unguarded write would put `running` back over it, leaving the registry
        claiming a container that is gone."""
        fake_runner._status = {"container": "stopped", "ready": False}
        assert admin_client.post("/api/data-apps/reap-idle").json()["reconciled"] == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        def status_with_concurrent_stop(slug):
            conn = get_system_db()
            try:
                repo = DataAppsRepository(conn)
                repo.set_state(repo.get_by_slug(slug)["id"], "sleeping")
            finally:
                conn.close()
            return {"container": "running", "ready": True}

        self._age_out("crash1")
        fake_runner.status = status_with_concurrent_stop
        assert admin_client.post("/api/data-apps/reap-idle").status_code == 200

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("crash1")
        finally:
            conn.close()
        assert row["state"] == "sleeping", "the concurrent stop was clobbered back to running"

    def test_reap_idle_reconcile_takes_no_lease_for_a_healthy_app(
        self, admin_client, fake_runner, monkeypatch, running_dead_container_app
    ):
        """`updated_at` is only bumped by `set_state`/`record_deploy`, never by
        `touch_last_request`, so "stale `updated_at` while running" describes
        essentially every healthy long-lived app. Acquiring the per-slug op
        lease for each of them would contend with a concurrent deploy/stop on a
        healthy app — `require_op_lease` gives up after a few 100 ms retries
        with 409 `operation_in_progress`. A live container must therefore be
        judged without the lease ever being taken."""
        import app.api.data_apps as data_apps_api

        real = data_apps_api.try_acquire_op_lease
        taken: list[str] = []

        def spy(slug):
            taken.append(slug)
            return real(slug)

        monkeypatch.setattr(data_apps_api, "try_acquire_op_lease", spy)
        fake_runner._status = {"container": "running", "ready": True}
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reconciled"] == []
        assert taken == [], f"lease acquired for a healthy app: {taken}"

    def test_reap_idle_reconcile_reprobes_under_the_lease(self, admin_client, fake_runner, running_dead_container_app):
        """A deploy that finishes between the lease-free probe and the lease
        acquisition released the lease, so the container is up again even
        though the row never left `running`. The second probe — paid only for
        an app that already looked dead — must catch that and leave it alone."""
        calls = {"n": 0}

        def status_recovering(slug):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"container": "absent", "ready": False}
            return {"container": "running", "ready": True}

        fake_runner.status = status_recovering
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reconciled"] == []
        assert calls["n"] == 2, "the lease-free probe and the under-lease re-probe must both run"

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("crash1")
        finally:
            conn.close()
        assert row["state"] == "running"

    def test_reap_idle_reconcile_skips_app_with_op_lease_held_elsewhere(
        self, admin_client, fake_runner, running_dead_container_app
    ):
        """The reconcile scan must respect the same per-slug op lease the idle
        loop above it does. `deploy_data_app` holds that lease for the whole
        deploy while leaving the row in `running` with a stale `updated_at`,
        and `apps_runner.up()` removes the old container BEFORE creating the
        new one — so a mid-deploy app legitimately reports `absent` and would
        otherwise be flipped to `error`, which the ingress proxy latches
        (only a manual redeploy clears it)."""
        from app.api.data_apps import release_op_lease, try_acquire_op_lease

        fake_runner._status = {"container": "absent", "ready": False}
        acquired, holder = try_acquire_op_lease("crash1")
        assert acquired
        try:
            r = admin_client.post("/api/data-apps/reap-idle")
            assert r.status_code == 200
            assert r.json()["reconciled"] == []
        finally:
            release_op_lease("crash1", holder)

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("crash1")
        finally:
            conn.close()
        assert row["state"] == "running"

    def test_reap_idle_reconcile_rereads_state_under_the_lease(
        self, admin_client, fake_runner, running_dead_container_app
    ):
        """The row is selected before the lease is taken, so its state can be
        stale by the time the lease is held. Here a concurrent stop lands
        between the scan and the lease acquisition (simulated by mutating the
        row from inside the runner's `status` call): the row is `sleeping`, an
        `absent` container is exactly right for it, and the reconcile must not
        write `error` over a legitimately sleeping app."""
        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        def status_with_concurrent_stop(slug):
            conn = get_system_db()
            try:
                repo = DataAppsRepository(conn)
                repo.set_state(repo.get_by_slug(slug)["id"], "sleeping")
            finally:
                conn.close()
            return {"container": "absent", "ready": False}

        fake_runner.status = status_with_concurrent_stop
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reconciled"] == []

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("crash1")
        finally:
            conn.close()
        assert row["state"] == "sleeping"

    def test_reap_idle_stop_failure_keeps_the_error_it_recorded(self, admin_client, fake_runner, running_idle_app):
        """A runner failure while stopping an idle app must leave the row in
        `error` with the runner's message, as the endpoint's docstring
        promises — and must NOT report the slug as reaped. The `finally` that
        writes `sleeping` runs even on the `except` path's `continue`, so
        without a guard it clobbers the error it had just recorded and the app
        is reported reaped while its container is still live."""

        def failing_stop(slug, mode="recreate"):
            raise RunnerError(500, "daemon busy")

        fake_runner.stop = failing_stop
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200
        assert r.json()["reaped"] == []

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            row = DataAppsRepository(conn).get_by_slug("sapp")
        finally:
            conn.close()
        assert row["state"] == "error"
        assert "daemon busy" in row["state_detail"]


class TestGitCredential:
    def test_mint_git_credential(self, client_as_user, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/git-credential")
        assert r.status_code == 200, r.text
        url = r.json()["git_clone_url"]
        assert "/data-apps.git/sapp" in url
        assert "@" in url and url.startswith("http")

    def test_git_credential_stranger_403(self, client_as_other_user, seeded_repo_with_commit):
        assert client_as_other_user.post("/api/data-apps/sapp/git-credential").status_code == 403

    def test_git_credential_feature_disabled_404s(self, api_env):
        import app.instance_config as instance_config

        original = instance_config._instance_config
        instance_config._instance_config = {**(original or {}), "data_apps": {"enabled": False}}
        try:
            c = api_env["client"]
            r = c.post("/api/data-apps/sapp/git-credential", headers=_auth(api_env["owner_pat"]))
            assert r.status_code == 404
            assert r.json()["detail"] == "data_apps_disabled"
        finally:
            instance_config._instance_config = original

    def test_git_credential_has_24h_ttl(self, client_as_user, seeded_repo_with_commit):
        """The minted PAT is for an authoring session, not a standing
        credential — both the JWT `exp` and the DB row's `expires_at` must
        be set to roughly 24h out (unlike the container's own service
        token, which is deliberately unbounded)."""
        r = client_as_user.post("/api/data-apps/sapp/git-credential")
        assert r.status_code == 200, r.text

        from datetime import datetime, timedelta, timezone

        from src.db import get_system_db
        from src.repositories.access_tokens import AccessTokenRepository

        conn = get_system_db()
        try:
            tokens = AccessTokenRepository(conn).list_for_user("owner1")
        finally:
            conn.close()
        row = next(t for t in tokens if t["name"] == "data-app-git:sapp")

        expires_at = row["expires_at"]
        assert expires_at is not None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        assert now < expires_at <= now + timedelta(hours=24, minutes=1)

    def test_git_credential_uses_public_url_when_configured(self, client_as_user, seeded_repo_with_commit, monkeypatch):
        """The credential must be usable from an analyst laptop, the MCP
        tool, or a remote sandbox -- none of which can resolve
        AGNES_INTERNAL_URL (http://app:8000, the in-cluster hostname).
        Mirrors create_data_app's use of get_public_url() for its own
        git_url, keeping the embedded agnes:<jwt>@ basic-auth."""
        monkeypatch.setenv("PUBLIC_URL", "https://analyst.example.com")
        r = client_as_user.post("/api/data-apps/sapp/git-credential")
        assert r.status_code == 200, r.text
        url = r.json()["git_clone_url"]
        assert url.startswith("https://agnes:"), url
        assert "@analyst.example.com/data-apps.git/sapp" in url

    def test_git_credential_falls_back_to_internal_url_when_public_unset(self, client_as_user, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/git-credential")
        assert r.status_code == 200, r.text
        url = r.json()["git_clone_url"]
        assert url.startswith("http://agnes:"), url
        assert "@app:8000/data-apps.git/sapp" in url

    def test_deploy_config_json_still_uses_internal_url(
        self, client_as_user, fake_runner, seeded_repo_with_commit, monkeypatch
    ):
        """The container-facing clone_url built by redeploy_current must
        stay on AGNES_INTERNAL_URL regardless of PUBLIC_URL -- only the
        credential handed back to a human/agent caller should change."""
        monkeypatch.setenv("PUBLIC_URL", "https://analyst.example.com")
        r = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r.status_code == 200, r.text
        _, _, config_json = fake_runner.up_calls[0]
        # Internal host, now with the push token embedded (the runtime image
        # won't add creds to a plain-HTTP clone URL — src/data_apps/spec.py).
        assert config_json["dataApp"]["git"]["repository"].startswith("http://agnes:")
        assert "@app:8000/data-apps.git/" in config_json["dataApp"]["git"]["repository"]


def _extract_jwt_from_clone_url(url: str) -> str:
    """Pull the JWT out of a `.../agnes:<jwt>@host/...` git clone URL."""
    return url.split("agnes:", 1)[1].split("@", 1)[0]


class TestGitScopeRejectedOnJsonApi:
    """A `data-app-git:<slug>` credential is scoped to the git-over-HTTP
    surface only (`app/api/data_apps_git.py`) — it must never authenticate
    the JSON REST API. Without this, the credential is effectively a
    full-privilege user PAT usable against the whole non-admin API surface,
    escaping the sandboxed-authoring confinement it was minted for."""

    def test_git_scoped_pat_rejected_on_list_data_apps(self, client_as_user, seeded_repo_with_commit):
        mint = client_as_user.post("/api/data-apps/sapp/git-credential")
        jwt = _extract_jwt_from_clone_url(mint.json()["git_clone_url"])

        r = client_as_user.get("/api/data-apps", headers=_auth(jwt))
        assert r.status_code == 401
        assert r.json()["detail"] == "git_scope_token_not_allowed"

    def test_git_scoped_pat_rejected_on_catalog(self, client_as_user, seeded_repo_with_commit):
        mint = client_as_user.post("/api/data-apps/sapp/git-credential")
        jwt = _extract_jwt_from_clone_url(mint.json()["git_clone_url"])

        r = client_as_user.get("/api/catalog/tables", headers=_auth(jwt))
        assert r.status_code == 401
        assert r.json()["detail"] == "git_scope_token_not_allowed"

    def test_normal_pat_still_works_on_json_api(self, client_as_user, seeded_repo_with_commit):
        """Control: an ordinary (unscoped) PAT is unaffected by the new check."""
        r = client_as_user.get("/api/data-apps")
        assert r.status_code == 200


class TestDrafts:
    def test_create_draft(self, client_as_user, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["branch"] == "init"
        assert body["slug"].startswith("sapp--")
        assert "/data-apps.git/sapp" in body["git_clone_url"]

    def test_draft_of_draft_rejected(self, client_as_user, seeded_repo_with_commit):
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "a"}).json()
        r = client_as_user.post(f"/api/data-apps/{d['slug']}/drafts", json={"branch": "b"})
        assert r.status_code == 400 and r.json()["detail"] == "parent_is_draft"

    def test_create_draft_near_max_slug_rejected(self, client_as_user, api_env):
        """A parent slug close to the 40-char SLUG_RE cap leaves no room for
        `--<branch>` before truncation — `_draft_slug` must reject that as
        400 `invalid_slug` rather than silently collapsing to the parent's
        own slug (which would surface as a misleading, branch-independent
        409 `slug_exists`)."""
        long_slug = "p" + "a" * 37 + "9"  # 39 chars, SLUG_RE-valid
        assert len(long_slug) == 39
        _seed_app_with_commit(api_env["data_dir"], slug=long_slug, owner_id="owner1")
        r = client_as_user.post(f"/api/data-apps/{long_slug}/drafts", json={"branch": "init"})
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "invalid_slug"

    def test_create_draft_invalid_branch_rejected(self, client_as_user, seeded_repo_with_commit):
        r = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "Bad Branch"})
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "invalid_branch"

    @pytest.mark.parametrize(
        "branch",
        [
            "a..b",  # double-dot: git rejects as a revision-range-like ref
            "a//b",  # double slash
            "a/",  # trailing slash
            "a.",  # trailing dot
            "x.lock",  # .lock suffix: reserved for git's own lockfiles
        ],
    )
    def test_create_draft_git_invalid_branch_rejected(self, client_as_user, seeded_repo_with_commit, branch):
        """These all pass `_BRANCH_RE`'s charset check but are refused by
        `git update-ref` itself (`ensure_branch` -> `CalledProcessError`).
        Must surface as 400 `invalid_branch`, not an unhandled 500 -- and
        must not leave the just-inserted draft row behind (a retry would
        otherwise hit a misleading 409 `slug_exists`)."""
        r = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": branch})
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "invalid_branch"

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            rows = DataAppsRepository(conn).list_drafts(DataAppsRepository(conn).get_by_slug("sapp")["id"])
        finally:
            conn.close()
        assert rows == []

    def test_create_draft_owner_not_found_500(self, admin_client, seeded_repo_with_commit):
        """If the parent app's owner row is gone by the time the git
        credential is minted, `_mint_git_credential` raises
        `OwnerNotFoundError` — the handler must map that to 500
        `owner_not_found`, same as the sibling `mint_git_credential`
        endpoint, rather than letting it bubble up as an unhandled 500."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            UserRepository(conn).delete("owner1")
        finally:
            conn.close()

        r = admin_client.post("/api/data-apps/sapp/drafts", json={"branch": "orphan"})
        assert r.status_code == 500, r.text
        assert r.json()["detail"] == "owner_not_found"

    def test_get_inlines_drafts(self, client_as_user, seeded_repo_with_commit):
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
        detail = client_as_user.get("/api/data-apps/sapp").json()
        assert any(x["slug"] == d["slug"] and x["branch"] == "init" for x in detail["drafts"])

    def test_get_draft_detail_omits_drafts_key(self, client_as_user, seeded_repo_with_commit):
        """A draft's own detail response has no `drafts` key at all — that
        field is only inlined for prod apps (drafts don't have drafts;
        `create_draft` rejects `parent_is_draft`)."""
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
        detail = client_as_user.get(f"/api/data-apps/{d['slug']}").json()
        assert "drafts" not in detail

    def test_list_hides_drafts(self, client_as_user, seeded_repo_with_commit):
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
        slugs = {a["slug"] for a in client_as_user.get("/api/data-apps").json()}
        assert "sapp" in slugs and d["slug"] not in slugs

    def test_delete_draft(self, client_as_user, fake_runner, seeded_repo_with_commit):
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
        r = client_as_user.delete(f"/api/data-apps/sapp/drafts/{d['slug']}")
        assert r.status_code == 204, r.text
        assert client_as_user.get(f"/api/data-apps/{d['slug']}").status_code == 404
        detail = client_as_user.get("/api/data-apps/sapp").json()
        assert detail["drafts"] == []

    def test_delete_draft_stops_container_and_revokes_token(self, client_as_user, fake_runner, seeded_repo_with_commit):
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
        r = client_as_user.post(f"/api/data-apps/{d['slug']}/deploy", json={"mode": "dev"})
        assert r.status_code == 200, r.text

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            token_id = DataAppsRepository(conn).get_by_slug(d["slug"])["service_token_id"]
        finally:
            conn.close()
        assert token_id

        r = client_as_user.delete(f"/api/data-apps/sapp/drafts/{d['slug']}")
        assert r.status_code == 204, r.text
        assert (d["slug"], "recreate") in fake_runner.stop_calls

        conn = get_system_db()
        try:
            from src.repositories.access_tokens import AccessTokenRepository

            token_row = AccessTokenRepository(conn).get_by_id(token_id)
        finally:
            conn.close()
        assert token_row["revoked_at"] is not None

    def test_delete_draft_409s_when_draft_op_lease_held_elsewhere(
        self, client_as_user, fake_runner, seeded_repo_with_commit
    ):
        """A deployed draft has its own `dataapp:op:{draft_slug}` lease,
        distinct from the parent's — the same lease `deploy_data_app`/
        `stop_data_app` take on the draft's own slug when it's addressed
        directly. Teardown must take it too, or a concurrent wake-on-request
        for the draft can race `runner.up()` against this delete's
        `runner.stop()`."""
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()

        from app.api.data_apps import release_op_lease, try_acquire_op_lease

        acquired, holder = try_acquire_op_lease(d["slug"])
        assert acquired
        try:
            r = client_as_user.delete(f"/api/data-apps/sapp/drafts/{d['slug']}")
            assert r.status_code == 409
            assert r.json()["detail"] == "operation_in_progress"
            assert not fake_runner.stop_calls
        finally:
            release_op_lease(d["slug"], holder)

    def test_delete_draft_removes_branch(self, client_as_user, seeded_repo_with_commit):
        from src.data_apps.git_repos import resolve_ref

        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
        assert resolve_ref("sapp", "init") is not None

        r = client_as_user.delete(f"/api/data-apps/sapp/drafts/{d['slug']}")
        assert r.status_code == 204, r.text
        assert resolve_ref("sapp", "init") is None

    def test_delete_draft_rejects_non_draft(self, client_as_user, seeded_repo_with_commit):
        # deleting the prod slug through the draft route is a 400
        r = client_as_user.delete("/api/data-apps/sapp/drafts/sapp")
        assert r.status_code == 400 and r.json()["detail"] == "not_a_draft"

    def test_delete_draft_unknown_404s(self, client_as_user, seeded_repo_with_commit):
        r = client_as_user.delete("/api/data-apps/sapp/drafts/does-not-exist")
        assert r.status_code == 404
        assert r.json()["detail"] == "data_app_not_found"

    def test_delete_draft_forbidden_for_stranger(self, client_as_user, client_as_other_user, seeded_repo_with_commit):
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
        r = client_as_other_user.delete(f"/api/data-apps/sapp/drafts/{d['slug']}")
        assert r.status_code == 403

    def test_delete_draft_wrong_parent_rejected(self, client_as_user, seeded_repo_with_commit, api_env):
        """A draft belonging to a DIFFERENT parent can't be deleted through
        this parent's `/drafts/{draft_slug}` route — same 400 `not_a_draft`
        as a non-draft slug, since it isn't a draft *of this parent*."""
        _seed_app_with_commit(api_env["data_dir"], slug="otherapp", owner_id="owner1")
        d = client_as_user.post("/api/data-apps/otherapp/drafts", json={"branch": "init"}).json()
        r = client_as_user.delete(f"/api/data-apps/sapp/drafts/{d['slug']}")
        assert r.status_code == 400 and r.json()["detail"] == "not_a_draft"

    def test_deploy_dev_mode_orphaned_draft_parent_not_found(
        self, client_as_user, fake_runner, seeded_repo_with_commit
    ):
        """Carried-over fix: if the draft's parent app has been deleted out
        from under it (bypassing the normal cascade — e.g. a direct repo
        delete), `redeploy_current` must raise loudly rather than silently
        falling back to cloning the draft's own (nonexistent) repo."""
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()

        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        conn = get_system_db()
        try:
            parent = DataAppsRepository(conn).get_by_slug("sapp")
            DataAppsRepository(conn).delete(parent["id"])
        finally:
            conn.close()

        r = client_as_user.post(f"/api/data-apps/{d['slug']}/deploy", json={"mode": "dev"})
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "parent_not_found"
        assert not fake_runner.up_calls

    def test_delete_parent_cascades_drafts(self, client_as_user, fake_runner, seeded_repo_with_commit):
        """Deleting a prod app with live drafts must delete the drafts too
        (rows + branches + containers) — not leave them orphaned."""
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()

        from src.data_apps.git_repos import resolve_ref
        from src.db import get_system_db
        from src.repositories.data_apps import DataAppsRepository

        assert resolve_ref("sapp", "init") is not None

        r = client_as_user.delete("/api/data-apps/sapp")
        assert r.status_code == 204, r.text

        conn = get_system_db()
        try:
            assert DataAppsRepository(conn).get_by_slug("sapp") is None
            assert DataAppsRepository(conn).get_by_slug(d["slug"]) is None
        finally:
            conn.close()
        assert (d["slug"], "recreate") in fake_runner.stop_calls


class TestRunnerCallsAreOffloaded:
    """Every runner-sidecar call reached from an ``async def`` handler must run
    in the thread pool, never inline on the event loop.

    Agnes serves everything from one uvicorn process with one event loop. The
    runner client is synchronous ``httpx`` — and since the cold-image-pull fix
    its ``up`` budget is 600 s (``_UP_TIMEOUT_DEFAULT``, tunable via
    ``APPS_RUNNER_UP_TIMEOUT``). Called inline from ``async def
    deploy_data_app``, one first-deploy-on-a-cold-host would freeze the whole
    process for up to ten minutes: no ``/api/health``, no sign-in, every other
    user's request queued behind an image download. The cheap calls
    (stop/status/logs, 60 s) are the same bug class an order of magnitude
    smaller — and ``/readiness`` is polled on a cadence by every holding page.

    ``app/api/data_apps_proxy.py`` already got this right for the wake path
    (``_run_wake_fn``/``_trigger_wake`` both go through ``run_in_threadpool``);
    these pin the control-plane half. Same convention as
    ``tests/test_event_loop_offload_guard.py``.
    """

    def test_deploy_does_not_run_runner_up_on_the_event_loop(
        self, client_as_user, fake_runner, seeded_repo_with_commit
    ):
        r = client_as_user.post("/api/data-apps/sapp/deploy", json={})
        assert r.status_code == 200, r.text
        assert fake_runner.up_calls
        assert fake_runner.on_loop() == []

    def test_stop_does_not_run_runner_stop_on_the_event_loop(
        self, client_as_user, fake_runner, seeded_repo_with_commit
    ):
        client_as_user.post("/api/data-apps/sapp/deploy", json={})
        fake_runner.thread_checks.clear()
        r = client_as_user.post("/api/data-apps/sapp/stop")
        assert r.status_code == 200, r.text
        assert fake_runner.stop_calls
        assert fake_runner.on_loop() == []

    def test_logs_does_not_run_runner_logs_on_the_event_loop(
        self, client_as_user, fake_runner, seeded_repo_with_commit
    ):
        r = client_as_user.get("/api/data-apps/sapp/logs")
        assert r.status_code == 200, r.text
        assert fake_runner.logs_calls
        assert fake_runner.on_loop() == []

    def test_readiness_does_not_run_runner_status_on_the_event_loop(
        self, client_as_user, fake_runner, seeded_repo_with_commit
    ):
        client_as_user.post("/api/data-apps/sapp/deploy", json={})
        fake_runner.thread_checks.clear()
        r = client_as_user.get("/api/data-apps/sapp/readiness")
        assert r.status_code == 200, r.text
        assert [name for name, _ in fake_runner.thread_checks] == ["status"]
        assert fake_runner.on_loop() == []

    def test_delete_does_not_run_runner_stop_on_the_event_loop(
        self, client_as_user, fake_runner, seeded_repo_with_commit
    ):
        """Covers the draft cascade too — ``_teardown_draft`` is shared with
        ``DELETE /{slug}/drafts/{draft_slug}`` and calls ``runner.stop``."""
        client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"})
        fake_runner.thread_checks.clear()
        r = client_as_user.delete("/api/data-apps/sapp")
        assert r.status_code == 204, r.text
        assert fake_runner.stop_calls
        assert fake_runner.on_loop() == []

    def test_draft_delete_does_not_run_runner_stop_on_the_event_loop(
        self, client_as_user, fake_runner, seeded_repo_with_commit
    ):
        d = client_as_user.post("/api/data-apps/sapp/drafts", json={"branch": "init"}).json()
        fake_runner.thread_checks.clear()
        r = client_as_user.delete(f"/api/data-apps/sapp/drafts/{d['slug']}")
        assert r.status_code == 204, r.text
        assert fake_runner.stop_calls
        assert fake_runner.on_loop() == []

    def test_reap_idle_does_not_run_runner_calls_on_the_event_loop(
        self, admin_client, fake_runner, running_idle_app, stale_deploying_app
    ):
        r = admin_client.post("/api/data-apps/reap-idle")
        assert r.status_code == 200, r.text
        assert fake_runner.stop_calls
        assert fake_runner.on_loop() == []


class TestOpLeaseCoversTheUpTimeout:
    """The per-slug ``dataapp:op:{slug}`` lease must outlive the longest
    operation it serializes.

    The lease is what stops two concurrent deploy/stop/delete/wake calls from
    racing ``services/apps_runner/api.py::up()``'s unlocked check-then-act
    (get old container -> remove -> run new) and landing two containers on the
    same name. A flat 120 s TTL was fine when every runner call was capped at
    60 s; with ``up`` now budgeted at 600 s for a cold image pull, the lease
    would expire mid-deploy and let exactly the concurrent operation it exists
    to prevent through — and the wake path (``_trigger_wake``) never releases
    it explicitly at all, relying on the TTL alone.
    """

    def test_ttl_exceeds_the_up_timeout(self):
        from app.api.data_apps import op_lease_ttl_s
        from src.data_apps.runner_client import up_timeout

        assert op_lease_ttl_s() > up_timeout()

    def test_ttl_follows_a_tuned_up_timeout(self, monkeypatch):
        """An operator on a slow link raises ``APPS_RUNNER_UP_TIMEOUT``; the
        lease has to move with it or the guarantee silently lapses again."""
        from app.api.data_apps import op_lease_ttl_s
        from src.data_apps.runner_client import up_timeout

        monkeypatch.setenv("APPS_RUNNER_UP_TIMEOUT", "1800")
        assert up_timeout() == 1800.0
        assert op_lease_ttl_s() > 1800.0

    def test_acquired_lease_uses_the_derived_ttl(self, api_env, monkeypatch):
        """Not just computed — actually handed to ``lease_acquire``."""
        import app.api.data_apps as data_apps_api

        seen: list[float] = []

        class _Coord:
            def lease_acquire(self, name, holder, ttl_s):
                seen.append(ttl_s)
                return True

        monkeypatch.setattr("app.coordination.factory.coordination", lambda: _Coord())
        acquired, _holder = data_apps_api.try_acquire_op_lease("sapp")
        assert acquired
        assert seen == [data_apps_api.op_lease_ttl_s()]
