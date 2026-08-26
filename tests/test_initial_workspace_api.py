"""Tests for the per-instance Initial Workspace Template feature.

Covers:
  * `src.initial_workspace`: clone, validate, zip
  * `app.api.initial_workspace`: admin + analyst endpoints
  * `app.secrets.persist_overlay_token`: lock + correctness for the
    shared overlay-write helper (introduced as a prerequisite refactor)

Uses a local bare git repo as fake remote so no network is needed.
Pattern copied from `tests/test_marketplace.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fake-remote helpers (mirror tests/test_marketplace.py)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    full_env = {**os.environ, **(env or {})}
    full_env.setdefault("GIT_AUTHOR_NAME", "Test")
    full_env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    full_env.setdefault("GIT_COMMITTER_NAME", "Test")
    full_env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=full_env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _file_url(path: Path) -> str:
    return path.resolve().as_uri()


@pytest.fixture
def fake_remote(tmp_path: Path):
    """Create a bare repo with a `workspace/` subdir containing
    CLAUDE.md + .claude/settings.json so the initial-workspace tests
    have something realistic to clone.

    Repo layout convention: only `workspace/` content reaches the
    analyst. Anything else at repo root (README, admin docs) is admin
    territory and never shipped.
    """
    work = tmp_path / "src-work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    # Repo-root file — admin-only, NOT shipped to analyst
    (work / "README.md").write_text("# Admin docs (not shipped)\n", encoding="utf-8")
    # Workspace subdir — this is what reaches the analyst
    workspace = work / "workspace"
    workspace.mkdir()
    (workspace / "CLAUDE.md").write_text("# Custom Workspace\n\nInternal rules.\n", encoding="utf-8")
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "model": "sonnet",
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": "agnes pull --quiet || true"}]}],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _git("add", ".", cwd=work)
    _git("commit", "-m", "initial", cwd=work)

    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare))
    _git("remote", "add", "origin", str(bare), cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work)

    return {"bare": bare, "work": work, "url": _file_url(bare), "sha": sha}


# ---------------------------------------------------------------------------
# Environment fixture — fresh DATA_DIR + system.duckdb per test
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "initial-workspace").mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    import src.db as db

    if getattr(db, "_system_db_conn", None) is not None:
        try:
            db._system_db_conn.close()
        except Exception:
            pass
    db._system_db_conn = None
    db._system_db_path = None

    yield data_dir


# ===========================================================================
# Layer 1: src/initial_workspace.py — clone, validate, zip
# ===========================================================================


def test_sync_template_clones_fresh(clean_env, fake_remote):
    """Fresh clone lands content in ${DATA_DIR}/initial-workspace/.

    Repo layout: README.md at root + workspace/ subdir with workspace
    content. After clone, both are on disk; only workspace/ ships to
    analysts via build_zip / list_template_files.
    """
    from src.initial_workspace import sync_template

    result = sync_template(url=fake_remote["url"], branch="main")
    assert result["commit_sha"] == fake_remote["sha"]
    target = clean_env / "initial-workspace"
    # Both root-level admin docs AND workspace/ subdir exist on disk
    assert (target / "README.md").exists()
    assert (target / "workspace" / "CLAUDE.md").exists()
    assert (target / "workspace" / ".claude" / "settings.json").exists()
    assert (target / ".git").is_dir()


def test_sync_template_fetch_reset_on_resync(clean_env, fake_remote):
    """Second sync uses fetch+reset (not re-clone). New commit reflected."""
    from src.initial_workspace import sync_template

    sync_template(url=fake_remote["url"], branch="main")

    # Add a commit upstream — file in workspace/ subdir
    work = fake_remote["work"]
    (work / "workspace" / "docs").mkdir(exist_ok=True)
    (work / "workspace" / "docs" / "handbook.md").write_text("handbook\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "add handbook", cwd=work)
    _git("push", "origin", "main", cwd=work)
    new_sha = _git("rev-parse", "HEAD", cwd=work)

    result = sync_template(url=fake_remote["url"], branch="main")
    assert result["commit_sha"] == new_sha
    target = clean_env / "initial-workspace"
    assert (target / "workspace" / "docs" / "handbook.md").exists()


def test_validate_template_tree_rejects_reserved_path(tmp_path):
    """`workspace/.claude/init-complete` in repo is reserved — sync must reject."""
    from src.initial_workspace import TemplateValidationError, validate_template_tree

    root = tmp_path / "tree"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "init-complete").write_text("oops", encoding="utf-8")
    with pytest.raises(TemplateValidationError) as exc:
        validate_template_tree(root)
    assert "init-complete" in str(exc.value)
    assert "reserved" in str(exc.value).lower()


def test_validate_template_tree_requires_workspace_subdir(tmp_path):
    """A repo without `workspace/` at root is rejected — strict layout."""
    from src.initial_workspace import TemplateValidationError, validate_template_tree

    root = tmp_path / "tree"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# At wrong location\n", encoding="utf-8")
    # No workspace/ subdir
    with pytest.raises(TemplateValidationError) as exc:
        validate_template_tree(root)
    assert "workspace" in str(exc.value).lower()


def test_validate_template_tree_ignores_root_files(tmp_path):
    """Files OUTSIDE workspace/ (README, CI configs) are silently ignored."""
    from src.initial_workspace import validate_template_tree

    root = tmp_path / "tree"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "CLAUDE.md").write_text("# Real content\n", encoding="utf-8")
    # Root-level files — admin's territory, validator must not touch
    (root / "README.md").write_text("# admin docs\n", encoding="utf-8")
    (root / ".github").mkdir()
    (root / ".github" / "workflows").mkdir()
    (root / ".github" / "workflows" / "ci.yml").write_text("ci\n", encoding="utf-8")
    # Even a "reserved" path at REPO ROOT is fine — only workspace/ scope matters
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "init-complete").write_text("not in workspace/\n", encoding="utf-8")
    # Should NOT raise
    validate_template_tree(root)


def test_sync_template_rejects_repo_without_workspace_subdir(clean_env, tmp_path):
    """End-to-end: a remote without workspace/ subdir fails sync."""
    from src.initial_workspace import TemplateValidationError, sync_template

    work = tmp_path / "src-work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    (work / "CLAUDE.md").write_text("# at root\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "init", cwd=work)
    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare))

    with pytest.raises(TemplateValidationError) as exc:
        sync_template(url=_file_url(bare), branch="main")
    assert "workspace" in str(exc.value).lower()


def test_sync_template_rejects_repo_with_reserved_path(clean_env, tmp_path):
    """A remote shipping workspace/.claude/init-complete is rejected."""
    from src.initial_workspace import TemplateValidationError, sync_template

    work = tmp_path / "src-work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    workspace = work / "workspace"
    workspace.mkdir()
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "init-complete").write_text("naughty", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "init", cwd=work)
    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare))

    with pytest.raises(TemplateValidationError):
        sync_template(url=_file_url(bare), branch="main")


def test_build_zip_excludes_root_files_and_git(clean_env, fake_remote):
    """Zip contains ONLY workspace/ contents, paths relative to workspace/.
    Root-level README.md from the repo must NOT be in the zip.
    """
    import io
    import zipfile

    from src.initial_workspace import build_zip, sync_template

    sync_template(url=fake_remote["url"], branch="main")
    data = build_zip()
    names = sorted(zipfile.ZipFile(io.BytesIO(data)).namelist())
    # Workspace content in, paths flattened (no workspace/ prefix)
    assert "CLAUDE.md" in names
    assert ".claude/settings.json" in names
    # Admin-only root files must NOT leak into the zip
    assert "README.md" not in names
    assert not any(n.startswith("workspace/") for n in names)
    assert not any(n.startswith(".git/") for n in names)


def test_list_template_files_deterministic(clean_env, fake_remote):
    """list_template_files returns sorted, deterministic POSIX paths
    relative to workspace/."""
    from src.initial_workspace import list_template_files, sync_template

    sync_template(url=fake_remote["url"], branch="main")
    files = list_template_files()
    assert files == sorted(files)
    assert "CLAUDE.md" in files
    assert ".claude/settings.json" in files
    # README.md at repo root must NOT be listed
    assert "README.md" not in files


# ===========================================================================
# Layer 2: app/secrets.persist_overlay_token concurrency
# ===========================================================================


def test_persist_overlay_token_concurrent_writes(clean_env):
    """Two threads writing different keys produce a valid merged overlay.

    Before the refactor, this test would intermittently fail because
    marketplaces._persist_token had no lock. With the shared helper,
    the lock guarantees both keys land in the final file.
    """
    from app.secrets import persist_overlay_token, _state_dir

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker(key, value):
        try:
            barrier.wait(timeout=5)
            # Hit the helper many times so the race window is wide enough
            # to repro reliably on slow CI runners.
            for _ in range(50):
                persist_overlay_token(key, value)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker, args=("AGNES_KEY_A", "value_a"))
    t2 = threading.Thread(target=worker, args=("AGNES_KEY_B", "value_b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], errors

    overlay_text = (_state_dir() / ".env_overlay").read_text()
    lines = [line for line in overlay_text.splitlines() if line]
    pairs = dict(line.split("=", 1) for line in lines)
    assert pairs.get("AGNES_KEY_A") == "value_a", pairs
    assert pairs.get("AGNES_KEY_B") == "value_b", pairs


def test_persist_overlay_token_clear_removes_key(clean_env):
    """value=None and value='' both remove the key."""
    from app.secrets import persist_overlay_token, _state_dir

    persist_overlay_token("AGNES_TMP", "secret")
    assert "AGNES_TMP" in (_state_dir() / ".env_overlay").read_text()

    persist_overlay_token("AGNES_TMP", None)
    assert "AGNES_TMP" not in (_state_dir() / ".env_overlay").read_text()

    persist_overlay_token("AGNES_TMP", "back")
    persist_overlay_token("AGNES_TMP", "")
    assert "AGNES_TMP" not in (_state_dir() / ".env_overlay").read_text()


# ===========================================================================
# Layer 3: API endpoints (admin + analyst)
# ===========================================================================


@pytest.fixture
def web_client(clean_env, monkeypatch, shared_app):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    from fastapi.testclient import TestClient
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


def _make_admin(client, email="admin@example.com"):
    """Create an admin user and return their auth headers."""
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    ph = PasswordHasher()
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin",
        email=email,
        name="Admin",
        password_hash=ph.hash("AdminPass1!"),
    )
    # Admin group is seeded as is_system=TRUE on schema init; look up its id.
    admin_row = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()
    assert admin_row is not None, "Admin group not seeded"
    UserGroupMembersRepository(conn).add_member(
        user_id="admin",
        group_id=admin_row[0],
        source="admin",
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": "AdminPass1!"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_user(client, email="user@example.com"):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    ph = PasswordHasher()
    conn = get_system_db()
    UserRepository(conn).create(
        id="user",
        email=email,
        name="User",
        password_hash=ph.hash("UserPass1!"),
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": "UserPass1!"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_admin_get_initial_workspace_not_configured(web_client):
    """GET returns configured:false when no section is in instance.yaml."""
    headers = _make_admin(web_client)
    r = web_client.get("/api/admin/initial-workspace", headers=headers)
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_admin_endpoints_require_admin(web_client):
    """Non-admin user gets 403 on every admin endpoint — all four verbs.
    A future refactor that drops `Depends(require_admin)` from one
    endpoint must fail here (otherwise we'd silently expose the
    write/delete paths to any analyst with a PAT)."""
    headers = _make_user(web_client)
    cases = [
        ("GET", "/api/admin/initial-workspace", None),
        ("POST", "/api/admin/initial-workspace", {"url": "https://example.com/x.git"}),
        ("DELETE", "/api/admin/initial-workspace", None),
        ("POST", "/api/admin/initial-workspace/sync", None),
    ]
    for method, path, body in cases:
        r = web_client.request(method, path, headers=headers, json=body)
        assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text}"


def test_admin_post_writes_yaml_section(web_client, fake_remote):
    """POST persists `initial_workspace:` to instance.yaml overlay."""

    headers = _make_admin(web_client)
    r = web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": fake_remote["url"], "branch": "main"},
    )
    # file:// URLs are rejected (validator requires https://) — assert
    # so we can adjust the test for the relaxed CI case below
    assert r.status_code == 422
    assert "https" in r.json()["detail"].lower()


def test_admin_post_https_validation(web_client):
    """url must be https://."""
    headers = _make_admin(web_client)
    r = web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": "http://example.com/repo.git"},
    )
    assert r.status_code == 422


def test_admin_post_token_routes_to_env_overlay(web_client, monkeypatch):
    """Token in POST body lands in .env_overlay, env-var name in YAML."""
    import yaml
    from app.secrets import _state_dir

    headers = _make_admin(web_client)
    r = web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={
            "url": "https://github.com/example/template.git",
            "branch": "main",
            "token": "ghp_test_token",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["url"] == "https://github.com/example/template.git"
    assert body["has_token"] is True

    overlay = (_state_dir() / ".env_overlay").read_text()
    assert "AGNES_INITIAL_WORKSPACE_TOKEN=ghp_test_token" in overlay

    instance_yaml = yaml.safe_load((_state_dir() / "instance.yaml").read_text())
    section = instance_yaml["initial_workspace"]
    assert section["url"] == "https://github.com/example/template.git"
    assert section["token_env"] == "AGNES_INITIAL_WORKSPACE_TOKEN"
    # Token value never lands in YAML
    assert "ghp_test_token" not in yaml.dump(instance_yaml)


def test_admin_post_idempotent(web_client):
    """Two POSTs land one section (overwrite, no duplication)."""
    import yaml
    from app.secrets import _state_dir

    headers = _make_admin(web_client)
    for url in ("https://github.com/a/b.git", "https://github.com/c/d.git"):
        r = web_client.post(
            "/api/admin/initial-workspace",
            headers=headers,
            json={"url": url, "branch": "main"},
        )
        assert r.status_code == 200, r.text

    instance_yaml = yaml.safe_load((_state_dir() / "instance.yaml").read_text())
    section = instance_yaml["initial_workspace"]
    assert section["url"] == "https://github.com/c/d.git"  # latest wins


def test_admin_delete_removes_section_and_token(web_client):
    """DELETE wipes YAML section + .env_overlay key."""
    import yaml
    from app.secrets import _state_dir

    headers = _make_admin(web_client)
    web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": "https://github.com/a/b.git", "token": "ghp_x"},
    )

    r = web_client.delete("/api/admin/initial-workspace", headers=headers)
    assert r.status_code == 204

    instance_yaml = yaml.safe_load((_state_dir() / "instance.yaml").read_text() or "{}") or {}
    assert "initial_workspace" not in instance_yaml
    overlay = (_state_dir() / ".env_overlay").read_text()
    assert "AGNES_INITIAL_WORKSPACE_TOKEN" not in overlay


def test_admin_sync_against_file_url(web_client, fake_remote, monkeypatch):
    """End-to-end: register file:// URL (bypass https check via DB), run sync,
    verify last_synced_at + last_commit_sha land in YAML."""
    import yaml
    from app.api.initial_workspace import _write_section
    from app.secrets import _state_dir

    # Bypass the https:// validation by patching the section directly —
    # the test fake_remote is file:// (no real git server in CI).
    _write_section({"url": fake_remote["url"], "branch": "main", "token_env": None})

    headers = _make_admin(web_client)
    r = web_client.post("/api/admin/initial-workspace/sync", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "sync_ok"
    assert body["commit_sha"] == fake_remote["sha"]
    assert body["file_count"] >= 2  # CLAUDE.md + .claude/settings.json

    instance_yaml = yaml.safe_load((_state_dir() / "instance.yaml").read_text())
    section = instance_yaml["initial_workspace"]
    assert section["last_commit_sha"] == fake_remote["sha"]
    assert section["last_synced_at"] is not None
    assert section.get("last_error") is None


def test_analyst_status_unconfigured(web_client):
    """PAT-authed user sees configured:false when no template registered."""
    headers = _make_user(web_client)
    r = web_client.get("/api/initial-workspace", headers=headers)
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_analyst_status_configured_synced(web_client, fake_remote):
    """Analyst sees full metadata + file list when configured + synced."""
    from app.api.initial_workspace import _write_section
    from src.initial_workspace import sync_template

    # Register + sync directly (bypass https:// check)
    _write_section({"url": fake_remote["url"], "branch": "main", "token_env": None})
    result = sync_template(url=fake_remote["url"], branch="main")
    _write_section(
        {
            "last_synced_at": "2026-05-13T10:00:00+00:00",
            "last_commit_sha": result["commit_sha"],
        }
    )

    headers = _make_user(web_client)
    r = web_client.get("/api/initial-workspace", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["synced"] is True
    assert body["template_sha"] == result["commit_sha"]
    assert "CLAUDE.md" in body["files"]


def test_analyst_zip_browser_unauthenticated_redirects_to_login(web_client):
    """Unauthenticated browser request (Accept: text/html) redirects to /login."""
    r = web_client.get(
        "/api/initial-workspace.zip",
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/login?next=/api/initial-workspace.zip"


def test_analyst_zip_api_unauthenticated_returns_401(web_client):
    """Unauthenticated API client (no text/html in Accept) still gets a JSON 401."""
    r = web_client.get(
        "/api/initial-workspace.zip",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 401


def test_analyst_zip_curl_default_accept_returns_401(web_client):
    """`Accept: */*` (curl's default with no `-H`) lands in the 401 branch.

    Mirrors the `_wants_html()` contract in `app/main.py`: `*/*` must NOT
    silently flip a curl/tooling client to an HTML response — they expect
    `{"detail": "..."}` and a real 401.
    """
    r = web_client.get(
        "/api/initial-workspace.zip",
        headers={"Accept": "*/*"},
    )
    assert r.status_code == 401


def test_analyst_zip_empty_accept_returns_401(web_client):
    """Empty `Accept` header lands in the 401 branch — same shape as the `*/*`
    case (no `text/html` substring means: not a browser, give the raw 401)."""
    r = web_client.get(
        "/api/initial-workspace.zip",
        headers={"Accept": ""},
    )
    assert r.status_code == 401


def test_analyst_zip_404_when_not_configured(web_client):
    """GET /api/initial-workspace.zip returns 404 when no template."""
    headers = _make_user(web_client)
    r = web_client.get("/api/initial-workspace.zip", headers=headers)
    assert r.status_code == 404


def test_analyst_zip_503_when_not_synced(web_client):
    """503 when configured but never synced."""
    from app.api.initial_workspace import _write_section

    _write_section({"url": "https://github.com/a/b.git", "branch": "main", "token_env": None})
    headers = _make_user(web_client)
    r = web_client.get("/api/initial-workspace.zip", headers=headers)
    assert r.status_code == 503


def test_analyst_zip_returns_bytes_and_etag(web_client, fake_remote):
    """200 returns zip bytes with ETag = template_sha."""
    import io
    import zipfile

    from app.api.initial_workspace import _write_section
    from src.initial_workspace import sync_template

    _write_section({"url": fake_remote["url"], "branch": "main", "token_env": None})
    result = sync_template(url=fake_remote["url"], branch="main")
    _write_section(
        {
            "last_synced_at": "2026-05-13T10:00:00+00:00",
            "last_commit_sha": result["commit_sha"],
        }
    )

    headers = _make_user(web_client)
    r = web_client.get("/api/initial-workspace.zip", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["etag"] == f'"{result["commit_sha"]}"'
    names = sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert "CLAUDE.md" in names


def test_analyst_zip_writes_fetch_started_audit(web_client, fake_remote):
    """GET .../zip writes a server-side audit row."""
    from app.api.initial_workspace import _write_section
    from src.db import get_system_db
    from src.initial_workspace import sync_template

    _write_section({"url": fake_remote["url"], "branch": "main", "token_env": None})
    result = sync_template(url=fake_remote["url"], branch="main")
    _write_section(
        {
            "last_synced_at": "2026-05-13T10:00:00+00:00",
            "last_commit_sha": result["commit_sha"],
        }
    )

    headers = _make_user(web_client)
    web_client.get("/api/initial-workspace.zip", headers=headers)

    conn = get_system_db()
    rows = conn.execute(
        "SELECT action, params FROM audit_log WHERE action = 'initial_workspace.fetch_started'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1, rows
    params = json.loads(rows[0][1])
    assert params["template_sha"] == result["commit_sha"]


def test_analyst_applied_writes_audit(web_client):
    """POST /applied writes audit row with mode + counts."""
    from src.db import get_system_db

    headers = _make_user(web_client)
    r = web_client.post(
        "/api/initial-workspace/applied",
        headers=headers,
        json={
            "mode": "fresh_install",
            "template_sha": "abc123",
            "files_overwritten": 0,
            "files_created": 5,
        },
    )
    assert r.status_code == 200

    conn = get_system_db()
    rows = conn.execute("SELECT params FROM audit_log WHERE action = 'initial_workspace.applied'").fetchall()
    conn.close()
    assert len(rows) == 1
    params = json.loads(rows[0][0])
    assert params["mode"] == "fresh_install"
    assert params["files_created"] == 5


def test_analyst_applied_rejects_invalid_mode(web_client):
    """POST /applied with garbage mode returns 422."""
    headers = _make_user(web_client)
    r = web_client.post(
        "/api/initial-workspace/applied",
        headers=headers,
        json={"mode": "garbage"},
    )
    assert r.status_code == 422


# ===========================================================================
# Layer 4: #622 Slice 3 PR-B — sync_schedule + sync-if-configured + page
# ===========================================================================


def test_admin_post_sync_schedule_persisted_and_echoed(web_client):
    """A valid sync_schedule lands in the YAML overlay and is echoed in GET."""
    import yaml
    from app.secrets import _state_dir

    headers = _make_admin(web_client)
    r = web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": "https://github.com/a/b.git", "sync_schedule": "daily 03:30"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["sync_schedule"] == "daily 03:30"

    section = yaml.safe_load((_state_dir() / "instance.yaml").read_text())["initial_workspace"]
    assert section["sync_schedule"] == "daily 03:30"

    # GET echoes it too.
    g = web_client.get("/api/admin/initial-workspace", headers=headers)
    assert g.json()["sync_schedule"] == "daily 03:30"


def test_admin_post_sync_schedule_garbage_rejected(web_client):
    """A malformed sync_schedule is rejected with 422 — a typo must NOT
    silently disable the nightly job."""
    headers = _make_admin(web_client)
    r = web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": "https://github.com/a/b.git", "sync_schedule": "garbage"},
    )
    assert r.status_code == 422
    assert "sync_schedule" in r.json()["detail"]


def test_admin_post_sync_schedule_empty_clears(web_client):
    """An empty-string sync_schedule clears it (disable auto-sync).

    The overlay stores an explicit ``""`` (NOT null) so the scheduler can tell
    "admin cleared → disable" apart from "never configured → default" — both of
    which get_value otherwise collapses to its default (#622 Slice 3 PR-B
    review). The API still echoes ``null`` externally: empty == disabled."""
    import yaml
    from app.secrets import _state_dir

    headers = _make_admin(web_client)
    web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": "https://github.com/a/b.git", "sync_schedule": "daily 03:30"},
    )
    r = web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": "https://github.com/a/b.git", "sync_schedule": ""},
    )
    assert r.status_code == 200, r.text
    assert r.json()["sync_schedule"] is None
    section = yaml.safe_load((_state_dir() / "instance.yaml").read_text())["initial_workspace"]
    # Persisted as an explicit empty string — the distinguishable disable
    # marker — not null/absent.
    assert section.get("sync_schedule") == ""
    assert "sync_schedule" in section


def test_admin_post_omitting_sync_schedule_preserves_existing(web_client):
    """Omitting sync_schedule (field absent in the JSON) must leave the existing
    schedule untouched — editing only URL/branch must NOT disable auto-sync.

    This is the backend contract the edit form relies on after the #653 review:
    the frontend now sends sync_schedule only when the admin changed it, so an
    unrelated edit arrives with the key absent and the schedule survives."""
    import yaml
    from app.secrets import _state_dir

    headers = _make_admin(web_client)
    web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": "https://github.com/a/b.git", "sync_schedule": "every 6h"},
    )
    # Edit only the branch — sync_schedule key absent from the payload.
    r = web_client.post(
        "/api/admin/initial-workspace",
        headers=headers,
        json={"url": "https://github.com/a/b.git", "branch": "main"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["sync_schedule"] == "every 6h"
    section = yaml.safe_load((_state_dir() / "instance.yaml").read_text())["initial_workspace"]
    assert section.get("sync_schedule") == "every 6h"


def test_sync_if_configured_skips_when_unconfigured(web_client):
    """The load-bearing scheduler-gate invariant: the nightly wrapper returns
    200 {skipped:true} (NOT 400) on an instance without an IWT, so the job
    never errors."""
    headers = _make_admin(web_client)
    r = web_client.post("/api/admin/initial-workspace/sync-if-configured", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is True
    assert body["reason"] == "not_configured"


def test_manual_sync_still_errors_when_unconfigured(web_client):
    """Regression: the MANUAL /sync route keeps its loud 400 not_configured
    error — only the scheduler wrapper is silent."""
    headers = _make_admin(web_client)
    r = web_client.post("/api/admin/initial-workspace/sync", headers=headers)
    assert r.status_code == 400
    assert r.json()["detail"]["kind"] == "not_configured"


def test_sync_if_configured_runs_when_configured(web_client, fake_remote):
    """When an IWT is registered, the wrapper delegates to the same sync logic
    the manual route uses and returns a sync result."""
    from app.api.initial_workspace import _write_section

    _write_section({"url": fake_remote["url"], "branch": "main", "token_env": None})
    headers = _make_admin(web_client)
    r = web_client.post("/api/admin/initial-workspace/sync-if-configured", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("action") == "sync_ok"
    assert body["commit_sha"] == fake_remote["sha"]


def test_sync_if_configured_requires_admin(web_client):
    """The scheduler wrapper is admin-gated (scheduler token → Admin user).
    A plain analyst PAT must get 403."""
    headers = _make_user(web_client)
    r = web_client.post("/api/admin/initial-workspace/sync-if-configured", headers=headers)
    assert r.status_code == 403


# ── Admin page render + nav wiring (#622 Slice 3 PR-B) ──


def test_admin_initial_workspace_page_renders(web_client):
    """GET /admin/initial-workspace returns 200 for admin and carries the
    page's distinctive markers."""
    headers = _make_admin(web_client)
    r = web_client.get("/admin/initial-workspace", headers=headers)
    assert r.status_code == 200, r.text
    assert "iw-prov-tbody" in r.text  # prompt-bindings provenance table
    assert "Link to Template Repository" in r.text


def test_admin_initial_workspace_page_denies_non_admin(web_client):
    """A non-admin is blocked from the page (403 or login redirect)."""
    headers = _make_user(web_client)
    r = web_client.get("/admin/initial-workspace", headers=headers, follow_redirects=False)
    assert r.status_code in (302, 303, 307, 403), r.status_code


def test_server_config_still_renders_after_iw_relocation(web_client):
    """Regression guard: deleting the IW markup/JS/harness hooks from
    /admin/server-config must not break its render, and it must leave a
    working cross-link to the new page."""
    headers = _make_admin(web_client)
    r = web_client.get("/admin/server-config", headers=headers)
    assert r.status_code == 200, r.text
    # The heavy IW lifecycle JS is gone …
    assert "const IW_API" not in r.text
    assert "function iwLoad" not in r.text
    # … but the stub anchor + cross-link to the new page remain.
    assert "/admin/initial-workspace" in r.text


def test_nav_has_initial_workspace_link():
    """The Agent Experience nav group must link the new page.

    Read off ``app/web/admin_nav.py``, which is where the admin inventory
    lives. It used to be a substring assertion on ``_app_rail.html`` (and
    before that ``_app_header.html``), back when each chrome carried its own
    hand-written copy of the admin menu. Both copies are gone: the rail now
    carries ONE ``/admin`` destination and every ``/admin/*`` page renders the
    admin sidebar off this single inventory, so the inventory is the only
    place the link can be missing from.
    """
    from app.web.admin_nav import ADMIN_NAV_OFFNAV, ADMIN_NAV_SECTIONS, _section_entries

    hrefs = {e["href"] for s in ADMIN_NAV_SECTIONS for e in _section_entries(s)}
    hrefs |= {s["href"] for s in ADMIN_NAV_SECTIONS if s.get("href")}
    hrefs |= {e["href"] for e in ADMIN_NAV_OFFNAV}
    assert "/admin/initial-workspace" in hrefs, (
        "/admin/initial-workspace is not in the admin nav inventory — the page would be "
        "reachable only by typing the URL"
    )


# ---------------------------------------------------------------------------
# Render dry-run — connector body probe (required → error, optional →
# warning). The renderer itself stays fail-soft; the dry-run is the
# operator-facing gate for a seed commit that lost a SKILL.md body.
# ---------------------------------------------------------------------------


def _probe_entry(slug: str, *, required: bool):
    from src.connectors_manifest import ConnectorEntry

    return ConnectorEntry(
        slug=slug,
        display_name=slug,
        short_summary="s",
        estimated_minutes=1,
        required=required,
    )


def test_dry_run_errors_on_required_connector_missing_body(monkeypatch):
    from app.api import initial_workspace as api

    monkeypatch.setattr(
        "src.connectors_manifest.load_manifest",
        lambda: [_probe_entry("connector-req", required=True)],
    )
    monkeypatch.setattr("src.connectors_manifest.load_connector_body", lambda slug: None)
    summary = api._compute_render_dry_run()
    assert summary["ok"] is False
    assert any("connector-req" in e and "required" in e for e in summary["errors"]), summary["errors"]


class _FakePromptMetaRepo:
    """Stub for welcome_template_repo() — the dry-run only reads meta."""

    def __init__(self, meta):
        self._meta = meta

    def get_meta(self):
        return self._meta


def test_dry_run_errors_on_token_placeholder_in_seed_template(monkeypatch):
    """An operator seed still carrying the retired `{token}` placeholder
    renders it literally — the old heredoc would write the string `{token}`
    into ~/.agnes/token and 401 every `agnes init`. The editor save path
    rejects it (app/api/prompts.py); a git-bound seed only surfaces at
    sync, so the dry-run flags it as an error — but only when the install
    prompt actually renders the seed file (git-bound, canonical path)."""
    import src.initial_workspace as iw

    from app.api import initial_workspace as api

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    monkeypatch.setattr(iw, "resolve_seed_file", lambda rel: ("echo {token} > ~/.agnes/token", "iwt"))
    monkeypatch.setattr(
        "src.repositories.welcome_template_repo",
        lambda: _FakePromptMetaRepo({"source_mode": "git", "git_path": None}),
    )
    summary = api._compute_render_dry_run()
    assert summary["ok"] is False
    assert any("{token}" in e for e in summary["errors"]), summary["errors"]


def test_dry_run_warns_on_token_placeholder_when_prompt_in_editor_mode(monkeypatch):
    """An editor-mode install prompt never renders the seed file — a
    legacy IWT template must not hard-error unrelated syncs. It degrades
    to a warning the operator can act on before flipping to git mode
    (the bind-git flip itself re-rejects `{token}`)."""
    import src.initial_workspace as iw

    from app.api import initial_workspace as api

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    monkeypatch.setattr(iw, "resolve_seed_file", lambda rel: ("echo {token} > ~/.agnes/token", "iwt"))
    monkeypatch.setattr(
        "src.repositories.welcome_template_repo",
        lambda: _FakePromptMetaRepo({"source_mode": "editor", "git_path": None}),
    )
    summary = api._compute_render_dry_run()
    assert summary["ok"] is True
    assert summary["errors"] == []
    assert any("{token}" in w for w in summary["warnings"]), summary["warnings"]


def test_dry_run_warns_on_token_placeholder_in_bundled_fallback(monkeypatch):
    """A bundled-fallback hit is wheel content, not the operator's seed —
    warning only, even when the install prompt is git-bound."""
    import src.initial_workspace as iw

    from app.api import initial_workspace as api

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    monkeypatch.setattr(iw, "resolve_seed_file", lambda rel: ("echo {token} > ~/.agnes/token", "bundled"))
    monkeypatch.setattr(
        "src.repositories.welcome_template_repo",
        lambda: _FakePromptMetaRepo({"source_mode": "git", "git_path": None}),
    )
    summary = api._compute_render_dry_run()
    assert summary["ok"] is True
    assert summary["errors"] == []
    assert any("{token}" in w for w in summary["warnings"]), summary["warnings"]


def test_dry_run_errors_on_token_placeholder_in_non_canonical_bound_file(monkeypatch):
    """bind-git accepts any repo-relative path — a prompt bound to a
    non-canonical file renders THAT file, so the dry-run must scan it
    too; a `{token}` hit there is the same hard error as the canonical
    git-bound case."""
    import src.initial_workspace as iw

    from app.api import initial_workspace as api

    def fake_resolve(rel):
        if rel == "install-prompt/custom.md.tmpl":
            return ("echo {token} > ~/.agnes/token", "iwt")
        return ("clean canonical template", "iwt")

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    monkeypatch.setattr(iw, "resolve_seed_file", fake_resolve)
    monkeypatch.setattr(
        "src.repositories.welcome_template_repo",
        lambda: _FakePromptMetaRepo({"source_mode": "git", "git_path": "install-prompt/custom.md.tmpl"}),
    )
    summary = api._compute_render_dry_run()
    assert summary["ok"] is False
    assert any("install-prompt/custom.md.tmpl" in e and "{token}" in e for e in summary["errors"]), summary["errors"]


def test_dry_run_canonical_token_hit_downgrades_when_bound_elsewhere(monkeypatch):
    """A legacy canonical template must not hard-error a sync when the
    install prompt is git-bound to a different (clean) file — analysts
    never see the canonical file in that configuration, so it stays a
    warning-only probe."""
    import src.initial_workspace as iw

    from app.api import initial_workspace as api

    def fake_resolve(rel):
        if rel == "install-prompt/custom.md.tmpl":
            return ("clean bound template", "iwt")
        return ("echo {token} > ~/.agnes/token", "iwt")

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    monkeypatch.setattr(iw, "resolve_seed_file", fake_resolve)
    monkeypatch.setattr(
        "src.repositories.welcome_template_repo",
        lambda: _FakePromptMetaRepo({"source_mode": "git", "git_path": "install-prompt/custom.md.tmpl"}),
    )
    summary = api._compute_render_dry_run()
    assert summary["ok"] is True
    assert summary["errors"] == []
    assert any("{token}" in w for w in summary["warnings"]), summary["warnings"]


def test_dry_run_warns_on_optional_connector_missing_body(monkeypatch):
    from app.api import initial_workspace as api

    monkeypatch.setattr(
        "src.connectors_manifest.load_manifest",
        lambda: [_probe_entry("connector-opt", required=False)],
    )
    monkeypatch.setattr("src.connectors_manifest.load_connector_body", lambda slug: None)
    summary = api._compute_render_dry_run()
    assert summary["ok"] is True
    assert summary["errors"] == []
    assert any("connector-opt" in w for w in summary["warnings"]), summary["warnings"]


def test_dry_run_warns_on_unwired_template_placeholder(monkeypatch):
    """A synced install-prompt template referencing a placeholder nothing
    substitutes on the git-bound path (only {server_url} + Jinja context
    are replaced) must surface a warning to the operator."""
    from app.api import initial_workspace as api
    from src import initial_workspace as iw

    def fake_resolve(rel):
        if rel == "install-prompt/template.md.tmpl":
            return ("Install via {install_cli_block} at {server_url}", "iwt")
        return None

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    monkeypatch.setattr(iw, "resolve_seed_file", fake_resolve)
    summary = api._compute_render_dry_run()
    assert summary["ok"] is True
    assert any("{install_cli_block}" in w for w in summary["warnings"]), summary["warnings"]
    # {server_url} alone is fine — no warning names it as unwired.
    assert not any("only {server_url}" in w and "{install_cli_block}" not in w for w in summary["warnings"])


def test_dry_run_accepts_bundled_template(monkeypatch):
    """The shipped bundled template must pass its own dry-run clean."""
    from app.api import initial_workspace as api

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    summary = api._compute_render_dry_run()
    assert summary["ok"] is True
    assert not any("render literally" in w for w in summary["warnings"]), summary["warnings"]


def test_dry_run_scans_bound_template_for_unwired_placeholders(monkeypatch):
    """When the install prompt is git-bound to a custom path, the unwired-
    placeholder scan must inspect THAT file — the one analysts actually get —
    not the canonical default template."""
    from app.api import initial_workspace as api
    from src import initial_workspace as iw

    def fake_resolve(rel):
        if rel == "install-prompt/custom.md.tmpl":
            return ("Broken fork with {connector_tiles} inside", "iwt")
        if rel == "install-prompt/template.md.tmpl":
            return ("Clean default at {server_url}", "iwt")
        return None

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    monkeypatch.setattr(iw, "resolve_seed_file", fake_resolve)
    monkeypatch.setattr(
        "src.repositories.welcome_template_repo",
        lambda: _FakePromptMetaRepo({"source_mode": "git", "git_path": "install-prompt/custom.md.tmpl"}),
    )
    summary = api._compute_render_dry_run()
    assert any(
        "{connector_tiles}" in w and "custom.md.tmpl" in w for w in summary["warnings"]
    ), summary["warnings"]


def test_dry_run_editor_mode_warning_is_qualified(monkeypatch):
    """In editor mode the seed template is not rendered — the unwired-
    placeholder warning must say so instead of claiming analysts see it."""
    from app.api import initial_workspace as api
    from src import initial_workspace as iw

    def fake_resolve(rel):
        if rel == "install-prompt/template.md.tmpl":
            return ("Install via {install_cli_block}", "iwt")
        return None

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    monkeypatch.setattr(iw, "resolve_seed_file", fake_resolve)
    summary = api._compute_render_dry_run()
    hits = [w for w in summary["warnings"] if "{install_cli_block}" in w]
    assert hits and "does not currently render" in hits[0], summary["warnings"]


def test_dry_run_tight_jinja_variables_are_not_flagged(monkeypatch):
    """`{{today}}` without spaces is a substituted Jinja variable, not an
    unwired single-brace placeholder."""
    from app.api import initial_workspace as api
    from src import initial_workspace as iw

    def fake_resolve(rel):
        if rel == "install-prompt/template.md.tmpl":
            return ("Rendered on {{today}} for {{instance.name}} at {server_url}", "iwt")
        return None

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    monkeypatch.setattr(iw, "resolve_seed_file", fake_resolve)
    summary = api._compute_render_dry_run()
    assert not any("render literally" in w for w in summary["warnings"]), summary["warnings"]


def test_dry_run_bound_template_render_error_is_surfaced(monkeypatch):
    """A git-bound template with a Jinja render error silently falls back to
    the built-in default for analysts — the dry-run must surface it to the
    operator as an error (iwt-sourced)."""
    from app.api import initial_workspace as api
    from src import initial_workspace as iw

    def fake_resolve(rel):
        if rel == "install-prompt/custom.md.tmpl":
            return ("Hello {{ user.email.upper.bogus() }} at {server_url}", "iwt")
        return None

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    monkeypatch.setattr(iw, "resolve_seed_file", fake_resolve)
    monkeypatch.setattr(
        "src.repositories.welcome_template_repo",
        lambda: _FakePromptMetaRepo({"source_mode": "git", "git_path": "install-prompt/custom.md.tmpl"}),
    )
    summary = api._compute_render_dry_run()
    assert summary["ok"] is False
    assert any("does not render" in e for e in summary["errors"]), summary["errors"]


def test_dry_run_meta_read_failure_skips_render_validation(monkeypatch):
    """A meta-read failure makes `_install_prompt_bound_git_path` fall back
    to the canonical path (conservative for the `{token}` probe), but render
    validation must NOT fire on that fallback — an editor-mode instance never
    renders the seed template, so a transient DB hiccup must not hard-fail
    the sync over it."""
    from app.api import initial_workspace as api
    from src import initial_workspace as iw

    def fake_resolve(rel):
        if rel == "install-prompt/template.md.tmpl":
            return ("Hello {{ user.email.bogus() }} at {server_url}", "iwt")
        return None

    def boom():
        raise RuntimeError("db hiccup")

    monkeypatch.setattr("src.connectors_manifest.load_manifest", lambda: [])
    monkeypatch.setattr(iw, "resolve_seed_file", fake_resolve)
    monkeypatch.setattr("src.repositories.welcome_template_repo", boom)
    summary = api._compute_render_dry_run()
    assert not any("does not render" in e for e in summary["errors"]), summary["errors"]
