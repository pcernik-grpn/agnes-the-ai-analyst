"""Acceptance tests for #622 Slice 1 — admin-managed prompts (/admin/prompts).

Covers the schema migration, the source-mode toggle, the two chokepoints
(``build_zip`` overlay + ``/api/welcome`` render), the new REST surface, the
new page + redirects, and the triple-surface coverage gate.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# 1. Migration — v75 columns + backfill
# ---------------------------------------------------------------------------


def test_fresh_db_has_v75_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import SCHEMA_VERSION, get_system_db

    assert SCHEMA_VERSION >= 75
    conn = get_system_db()
    try:
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'instance_templates'"
            ).fetchall()
        }
        assert {"source_mode", "git_path", "base_sha"} <= cols
        # seeded keys default to 'editor'
        rows = conn.execute(
            "SELECT key, source_mode FROM instance_templates WHERE key IN ('welcome','claude_md')"
        ).fetchall()
        assert rows, "expected seeded welcome/claude_md keys"
        assert all(sm == "editor" for _k, sm in rows)
    finally:
        conn.close()


def test_v74_upgrades_to_v75_preserving_content(tmp_path):
    """A simulated v74 instance_templates row upgrades to v75 with content
    intact and source_mode backfilled to 'editor'."""
    db_path = tmp_path / "legacy.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP)")
    conn.execute("INSERT INTO schema_version VALUES (74, current_timestamp)")
    conn.execute(
        "CREATE TABLE instance_templates (key VARCHAR PRIMARY KEY, content TEXT, "
        "previous_content TEXT, updated_at TIMESTAMP, updated_by VARCHAR)"
    )
    conn.execute("INSERT INTO instance_templates (key, content) VALUES ('claude_md', 'KEEP ME')")

    from src.db import _v74_to_v75

    _v74_to_v75(conn)

    row = conn.execute(
        "SELECT content, source_mode, git_path, base_sha FROM instance_templates WHERE key='claude_md'"
    ).fetchone()
    assert row[0] == "KEEP ME"
    assert row[1] == "editor"
    assert row[2] is None and row[3] is None
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 75
    conn.close()


# ---------------------------------------------------------------------------
# Repo-level: get_meta / set_source_mode / bind_git round-trip (DuckDB)
# ---------------------------------------------------------------------------


def test_repo_source_toggle_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import claude_md_template_repo

    repo = claude_md_template_repo()
    repo.set("draft body", updated_by="a@x.com")
    meta = repo.get_meta()
    assert meta["content"] == "draft body"
    assert meta["source_mode"] == "editor"

    # toggle to git must NOT wipe content
    repo.set_source_mode("git", updated_by="a@x.com")
    meta = repo.get_meta()
    assert meta["source_mode"] == "git"
    assert meta["content"] == "draft body"

    # bind_git stamps path + sha
    repo.bind_git("workspace/CLAUDE.md", base_sha="deadbeef", updated_by="a@x.com")
    meta = repo.get_meta()
    assert meta["source_mode"] == "git"
    assert meta["git_path"] == "workspace/CLAUDE.md"
    assert meta["base_sha"] == "deadbeef"

    # back to editor preserves the draft
    repo.set_source_mode("editor", updated_by="a@x.com")
    assert repo.get_meta()["content"] == "draft body"


def test_repo_set_source_mode_rejects_bad_value(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import welcome_template_repo

    with pytest.raises(ValueError):
        welcome_template_repo().set_source_mode("nonsense", updated_by="a@x.com")


# ---------------------------------------------------------------------------
# 3. build_zip chokepoint — THE core fix
# ---------------------------------------------------------------------------


def _make_iwt_clone(tmp_path: Path, *, claude_md: str) -> Path:
    iwt = tmp_path / "initial-workspace"
    ws = iwt / "workspace"
    ws.mkdir(parents=True)
    (ws / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    (ws / "settings.json").write_text("{}", encoding="utf-8")
    return iwt


def _zip_names_and_content(data: bytes):
    zf = zipfile.ZipFile(io.BytesIO(data))
    return {n: zf.read(n).decode("utf-8") for n in zf.namelist()}


def test_build_zip_editor_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _make_iwt_clone(tmp_path, claude_md="FROM CLONE")

    import src.initial_workspace as iw
    from src.repositories import claude_md_template_repo

    # bypass validate_template_tree + force the snapshot to our clone dir
    monkeypatch.setattr(iw, "validate_template_tree", lambda *a, **k: None)

    repo = claude_md_template_repo()
    repo.set("FROM EDITOR OVERRIDE", updated_by="a@x.com")  # editor mode default

    from src.db import get_system_db

    conn = get_system_db()
    try:
        data = iw.build_zip(conn)
    finally:
        conn.close()
    files = _zip_names_and_content(data)
    assert files["CLAUDE.md"] == "FROM EDITOR OVERRIDE"
    # other files still come from the clone
    assert "settings.json" in files


def test_build_zip_git_mode_uses_clone(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _make_iwt_clone(tmp_path, claude_md="FROM CLONE")

    import src.initial_workspace as iw
    from src.repositories import claude_md_template_repo

    monkeypatch.setattr(iw, "validate_template_tree", lambda *a, **k: None)

    repo = claude_md_template_repo()
    repo.set("FROM EDITOR OVERRIDE", updated_by="a@x.com")
    repo.set_source_mode("git", updated_by="a@x.com")

    from src.db import get_system_db

    conn = get_system_db()
    try:
        data = iw.build_zip(conn)
    finally:
        conn.close()
    files = _zip_names_and_content(data)
    assert files["CLAUDE.md"] == "FROM CLONE"


def test_build_zip_no_conn_is_pure_clone(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _make_iwt_clone(tmp_path, claude_md="FROM CLONE")

    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "validate_template_tree", lambda *a, **k: None)
    files = _zip_names_and_content(iw.build_zip())  # no conn
    assert files["CLAUDE.md"] == "FROM CLONE"


# ---------------------------------------------------------------------------
# 4. resolve_prompt — git mode binds to the IWT file
# ---------------------------------------------------------------------------


def test_resolve_prompt_editor_and_git(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    iwt = _make_iwt_clone(tmp_path, claude_md="CLONE CLAUDE")

    import src.initial_workspace as iw
    from src.repositories import claude_md_template_repo

    repo = claude_md_template_repo()
    repo.set("EDITOR CLAUDE", updated_by="a@x.com")

    # editor mode → DB content
    content, mode = iw.resolve_prompt("workspace", None)
    assert (content, mode) == ("EDITOR CLAUDE", "editor")

    # git mode → clone file (force the snapshot to our dir)
    monkeypatch.setattr(iw, "_iwt_snapshot", lambda: iwt)
    repo.bind_git("workspace/CLAUDE.md", base_sha="x", updated_by="a@x.com")
    content, mode = iw.resolve_prompt("workspace", None)
    assert (content, mode) == ("CLONE CLAUDE", "git")

    # git mode bound to a missing file → (None, 'git') → caller falls back
    repo.bind_git("workspace/MISSING.md", base_sha="x", updated_by="a@x.com")
    content, mode = iw.resolve_prompt("workspace", None)
    assert content is None and mode == "git"


def test_bind_then_resolve_round_trip(admin_client, tmp_path, monkeypatch):
    """#638 review: bind through the API and resolve through resolve_prompt
    against the SAME clone — the two path namespaces must agree. The earlier
    tests validated bind and resolve in isolation, which masked the
    workspace-relative vs repo-relative mismatch."""
    client, token = admin_client
    iwt = _make_iwt_clone(tmp_path, claude_md="CLONE CLAUDE")

    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "is_configured", lambda: True)
    monkeypatch.setattr(iw, "_iwt_snapshot", lambda: iwt)

    r = client.post(
        "/api/admin/prompts/workspace/bind-git",
        headers=_hdr(token),
        json={"git_path": "workspace/CLAUDE.md"},
    )
    assert r.status_code == 200, r.text

    content, mode = iw.resolve_prompt("workspace", None)
    assert (content, mode) == ("CLONE CLAUDE", "git"), (
        "the path accepted by bind-git must be the path resolve_prompt reads"
    )


def test_resolve_prompt_rejects_path_traversal(tmp_path, monkeypatch):
    """#638 review: resolve_prompt reads git_path from the DB — a value that
    escapes the IWT root via ``..`` must NOT be readable (defense in depth;
    mirrors the resolve_seed_file hardening)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    iwt = _make_iwt_clone(tmp_path, claude_md="CLONE CLAUDE")
    secret = iwt.parent / "outside_secret.env"
    secret.write_text("TOKEN=supersecret", encoding="utf-8")

    import src.initial_workspace as iw
    from src.repositories import claude_md_template_repo

    monkeypatch.setattr(iw, "_iwt_snapshot", lambda: iwt)
    repo = claude_md_template_repo()
    repo.bind_git("../outside_secret.env", base_sha="x", updated_by="a@x.com")

    content, mode = iw.resolve_prompt("workspace", None)
    assert content is None and mode == "git", "a git_path escaping the IWT root must fall back, not read the file"


def test_resolve_seed_file_rejects_path_traversal(monkeypatch, tmp_path: Path):
    """#622 security: a git_path that escapes the IWT root via ``..`` must not
    be readable through resolve_seed_file — no arbitrary server-file read into
    the workspace zip (review finding on #638). A legit in-root path still
    resolves."""
    iwt = _make_iwt_clone(tmp_path, claude_md="CLONE CLAUDE")
    secret = iwt.parent / "outside_secret.env"
    secret.write_text("TOKEN=supersecret", encoding="utf-8")

    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "_iwt_snapshot", lambda: iwt)

    # `..` escape out of the IWT root → blocked, even though the file exists
    assert iw.resolve_seed_file(f"../{secret.name}") is None
    # a legit in-root file still resolves
    got = iw.resolve_seed_file("workspace/CLAUDE.md")
    assert got is not None and got[0] == "CLONE CLAUDE"


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DISABLE_GUARDRAILS", "1")
    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/auth/bootstrap",
        json={"email": "admin@example.com", "name": "A", "password": "TestPass123!"},
    )
    if resp.status_code == 403:
        pytest.skip("admin already bootstrapped")
    assert resp.status_code == 200, resp.text
    return client, resp.json()["access_token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def test_get_prompt_editor_default(admin_client):
    client, token = admin_client
    for kind in ("install", "workspace"):
        r = client.get(f"/api/admin/prompts/{kind}", headers=_hdr(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == kind
        assert body["source_mode"] == "editor"
        assert body["default"]  # a live default is always rendered
        assert body["iwt_configured"] is False


def test_put_then_get_roundtrip(admin_client):
    client, token = admin_client
    content = "# WS\n{{ user.email }}\n"
    r = client.put("/api/admin/prompts/workspace", headers=_hdr(token), json={"content": content})
    assert r.status_code == 200, r.text
    r = client.get("/api/admin/prompts/workspace", headers=_hdr(token))
    assert r.json()["content"] == content


def test_put_invalid_template_400(admin_client):
    client, token = admin_client
    r = client.put(
        "/api/admin/prompts/workspace",
        headers=_hdr(token),
        json={"content": "{{ undefined_var }}"},
    )
    assert r.status_code == 400


def test_source_toggle_to_git_requires_iwt(admin_client):
    client, token = admin_client
    # No IWT configured → switching to git is refused.
    r = client.post("/api/admin/prompts/workspace/source", headers=_hdr(token), json={"mode": "git"})
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "iwt_not_configured"


def test_bind_git_rejects_token_placeholder_for_install(admin_client, monkeypatch):
    """Git-bound content never passes the editor save guard (PUT is refused
    with prompt_in_git_mode), so bind-git itself must reject an install file
    that still carries the retired `{token}` placeholder."""
    client, token = admin_client
    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "is_configured", lambda: True)

    seed = {
        "install-prompt/legacy.md.tmpl": "curl -H {token} … writes it to ~/.agnes/token",
        "install-prompt/clean.md.tmpl": "reads ~/.agnes/token via --token-file",
    }
    monkeypatch.setattr(iw, "resolve_seed_file", lambda rel: (seed[rel], "iwt") if rel in seed else None)

    r = client.post(
        "/api/admin/prompts/install/bind-git",
        headers=_hdr(token),
        json={"git_path": "install-prompt/legacy.md.tmpl"},
    )
    assert r.status_code == 400, r.text
    assert "{token}" in r.json()["detail"]

    r = client.post(
        "/api/admin/prompts/install/bind-git",
        headers=_hdr(token),
        json={"git_path": "install-prompt/clean.md.tmpl"},
    )
    assert r.status_code == 200, r.text


def test_bind_git_rejects_bare_server_url_placeholder_for_install(admin_client, monkeypatch):
    """Same trap as the `{token}` guard above, for `{server_url}`: git-bound
    content never passes the editor save guard, so bind-git itself must
    reject an install file carrying the un-substitutable single-brace
    placeholder."""
    client, token = admin_client
    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "is_configured", lambda: True)

    seed = {
        "install-prompt/legacy.md.tmpl": "Server: {server_url}\ncurl -fsSL {server_url}/cli/download",
        "install-prompt/clean.md.tmpl": "Server: {{ server.url }}\ncurl -fsSL {{ server.url }}/cli/download",
    }
    monkeypatch.setattr(iw, "resolve_seed_file", lambda rel: (seed[rel], "iwt") if rel in seed else None)

    r = client.post(
        "/api/admin/prompts/install/bind-git",
        headers=_hdr(token),
        json={"git_path": "install-prompt/legacy.md.tmpl"},
    )
    assert r.status_code == 400, r.text
    assert "{server_url}" in r.json()["detail"]

    r = client.post(
        "/api/admin/prompts/install/bind-git",
        headers=_hdr(token),
        json={"git_path": "install-prompt/clean.md.tmpl"},
    )
    assert r.status_code == 200, r.text


def test_bind_git_requires_iwt(admin_client):
    client, token = admin_client
    r = client.post(
        "/api/admin/prompts/workspace/bind-git",
        headers=_hdr(token),
        json={"git_path": "CLAUDE.md"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "iwt_not_configured"


def test_bind_git_validates_path(admin_client, monkeypatch):
    """#638 review: paths are REPO-relative for both kinds — resolve_prompt
    resolves against the repo root, so the workspace prompt binds
    ``workspace/CLAUDE.md``, and the old workspace-RELATIVE namespace
    (bare ``CLAUDE.md``) must be rejected, not silently accepted."""
    client, token = admin_client
    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "is_configured", lambda: True)

    def _fake_resolve_seed_file(rel):
        if rel == "workspace/CLAUDE.md":
            return (Path("/fake/iwt/workspace/CLAUDE.md"), "iwt")
        return None

    monkeypatch.setattr(iw, "resolve_seed_file", _fake_resolve_seed_file)
    # patching the source module is enough — prompts.py imports it
    # function-level, resolving from the module at call time

    # absent path → 400
    r = client.post(
        "/api/admin/prompts/workspace/bind-git",
        headers=_hdr(token),
        json={"git_path": "does/not/exist.md"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["kind"] == "git_path_not_found"

    # workspace-RELATIVE path (the pre-fix namespace) → 400 too: it would
    # resolve nowhere at read time
    r = client.post(
        "/api/admin/prompts/workspace/bind-git",
        headers=_hdr(token),
        json={"git_path": "CLAUDE.md"},
    )
    assert r.status_code == 400, r.text

    # repo-relative path → 200, mode flips to git
    r = client.post(
        "/api/admin/prompts/workspace/bind-git",
        headers=_hdr(token),
        json={"git_path": "workspace/CLAUDE.md"},
    )
    assert r.status_code == 200, r.text
    g = client.get("/api/admin/prompts/workspace", headers=_hdr(token))
    assert g.json()["source_mode"] == "git"
    assert g.json()["git_path"] == "workspace/CLAUDE.md"


def test_put_in_git_mode_409(admin_client, monkeypatch):
    client, token = admin_client
    from src.repositories import claude_md_template_repo

    claude_md_template_repo().set_source_mode("git", updated_by="admin@example.com")
    r = client.put("/api/admin/prompts/workspace", headers=_hdr(token), json={"content": "x"})
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "prompt_in_git_mode"


def test_endpoints_require_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DISABLE_GUARDRAILS", "1")
    from app.main import app

    client = TestClient(app)
    # bootstrap an admin (so the instance isn't in first-run mode), then hit
    # unauthenticated.
    client.post(
        "/auth/bootstrap",
        json={"email": "admin@example.com", "name": "A", "password": "TestPass123!"},
    )
    for method, url, body in [
        ("get", "/api/admin/prompts/install", None),
        ("put", "/api/admin/prompts/install", {"content": "x"}),
        ("post", "/api/admin/prompts/install/source", {"mode": "editor"}),
        ("post", "/api/admin/prompts/install/bind-git", {"git_path": "x"}),
        ("delete", "/api/admin/prompts/install", None),
    ]:
        fn = getattr(client, method)
        resp = fn(url, json=body) if body is not None else fn(url)
        assert resp.status_code in (401, 403), f"{method} {url} → {resp.status_code}"


def test_welcome_render_honors_editor_override(admin_client):
    """/api/welcome (workspace render) returns the editor override content."""
    client, token = admin_client
    client.put(
        "/api/admin/prompts/workspace",
        headers=_hdr(token),
        json={"content": "WORKSPACE OVERRIDE MARKER"},
    )
    r = client.get("/api/welcome", headers=_hdr(token))
    assert r.status_code == 200, r.text
    assert "WORKSPACE OVERRIDE MARKER" in r.json()["content"]


# ---------------------------------------------------------------------------
# Slice 3 (#622) — git file-picker (GET /api/admin/prompts/iwt-files)
# ---------------------------------------------------------------------------


def _make_iwt_clone_with_install(tmp_path: Path, *, claude_md: str) -> Path:
    """Like _make_iwt_clone but also drops an install-prompt template at the
    REPO ROOT (outside workspace/) so we can prove the picker is
    repo-root-relative — list_template_files() can never see this file."""
    iwt = _make_iwt_clone(tmp_path, claude_md=claude_md)
    install_dir = iwt / "install-prompt"
    install_dir.mkdir(parents=True)
    (install_dir / "template.md.tmpl").write_text("INSTALL", encoding="utf-8")
    return iwt


def test_iwt_files_empty_when_unconfigured(admin_client):
    client, token = admin_client
    r = client.get("/api/admin/prompts/iwt-files", headers=_hdr(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["iwt_configured"] is False
    assert body["files"] == []
    assert body["suggested"] == {
        "install": "install-prompt/template.md.tmpl",
        "workspace": "workspace/CLAUDE.md",
    }


def test_iwt_files_lists_repo_root_relative(admin_client, tmp_path, monkeypatch):
    client, token = admin_client
    iwt = _make_iwt_clone_with_install(tmp_path, claude_md="CLONE")

    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "is_configured", lambda: True)
    monkeypatch.setattr(iw, "_iwt_snapshot", lambda: iwt)

    r = client.get("/api/admin/prompts/iwt-files", headers=_hdr(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["iwt_configured"] is True
    # repo-ROOT-relative: BOTH the workspace/ file AND the install file at root
    # show up. list_template_files() (workspace/-relative) could express
    # neither — proves the picker uses a different enumeration.
    assert "workspace/CLAUDE.md" in body["files"]
    assert "install-prompt/template.md.tmpl" in body["files"]


def test_iwt_files_excludes_git_and_symlinks(admin_client, tmp_path, monkeypatch):
    client, token = admin_client
    iwt = _make_iwt_clone(tmp_path, claude_md="CLONE")
    git_dir = iwt / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text("[core]\n", encoding="utf-8")
    # a symlink pointing at a real in-repo file — must be skipped
    (iwt / "workspace" / "link.md").symlink_to(iwt / "workspace" / "CLAUDE.md")

    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "is_configured", lambda: True)
    monkeypatch.setattr(iw, "_iwt_snapshot", lambda: iwt)

    r = client.get("/api/admin/prompts/iwt-files", headers=_hdr(token))
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    assert not any(f.startswith(".git") for f in files), files
    assert "workspace/link.md" not in files, files
    assert "workspace/CLAUDE.md" in files


def test_picker_value_binds(admin_client, tmp_path, monkeypatch):
    """Load-bearing invariant: everything iwt-files lists must be bindable.
    A value returned by the picker is accepted verbatim by bind-git."""
    client, token = admin_client
    iwt = _make_iwt_clone(tmp_path, claude_md="CLONE CLAUDE")

    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "is_configured", lambda: True)
    monkeypatch.setattr(iw, "_iwt_snapshot", lambda: iwt)

    listing = client.get("/api/admin/prompts/iwt-files", headers=_hdr(token)).json()
    assert "workspace/CLAUDE.md" in listing["files"]
    picked = "workspace/CLAUDE.md"

    r = client.post(
        "/api/admin/prompts/workspace/bind-git",
        headers=_hdr(token),
        json={"git_path": picked},
    )
    assert r.status_code == 200, r.text
    content, mode = iw.resolve_prompt("workspace", None)
    assert (content, mode) == ("CLONE CLAUDE", "git")


def test_iwt_files_requires_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DISABLE_GUARDRAILS", "1")
    from app.main import app

    client = TestClient(app)
    client.post(
        "/auth/bootstrap",
        json={"email": "admin@example.com", "name": "A", "password": "TestPass123!"},
    )
    r = client.get("/api/admin/prompts/iwt-files")
    assert r.status_code in (401, 403)


def test_list_iwt_repo_files_unit(tmp_path, monkeypatch):
    """Pure unit on src.initial_workspace.list_iwt_repo_files: sorted order,
    .git exclusion, [] when no clone."""
    import src.initial_workspace as iw

    # no clone → empty
    monkeypatch.setattr(iw, "_iwt_snapshot", lambda: None)
    assert iw.list_iwt_repo_files() == []

    iwt = _make_iwt_clone_with_install(tmp_path, claude_md="CLONE")
    git_dir = iwt / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setattr(iw, "_iwt_snapshot", lambda: iwt)

    files = iw.list_iwt_repo_files()
    assert files == sorted(files), "must be sorted"
    assert not any(f.startswith(".git") for f in files)
    assert "workspace/CLAUDE.md" in files
    assert "install-prompt/template.md.tmpl" in files


# ---------------------------------------------------------------------------
# 9. Page + redirects
# ---------------------------------------------------------------------------


def test_admin_prompts_page_renders(admin_client):
    client, token = admin_client
    r = client.get("/admin/prompts", headers=_hdr(token), follow_redirects=False)
    assert r.status_code == 200, r.text
    assert "Workspace prompt" in r.text and "Install prompt" in r.text


def test_legacy_prompt_pages_redirect(admin_client):
    client, token = admin_client
    for old in ("/admin/agent-prompt", "/admin/workspace-prompt"):
        r = client.get(old, headers=_hdr(token), follow_redirects=False)
        assert r.status_code == 308, f"{old} → {r.status_code}"
        assert r.headers["location"] == "/admin/prompts"


def test_prompt_repo_ignores_duckdb_conn_on_postgres(monkeypatch):
    """#638 review: on the Postgres backend the factory must win even when a
    DuckDB conn is passed — FastAPI handlers hand over get_system_db() conns
    regardless of backend, and binding the DuckDB repo to one reads
    instance_templates from the wrong engine (the /setup regression)."""
    import duckdb as _duckdb

    import src.initial_workspace as iw
    import src.repositories as repos

    sentinel = object()
    monkeypatch.setattr(repos, "use_pg", lambda: True)
    monkeypatch.setattr(repos, "claude_md_template_repo", lambda: sentinel)

    conn = _duckdb.connect(":memory:")
    try:
        assert iw._prompt_repo("workspace", conn) is sentinel, "PG backend + DuckDB conn must route through the factory"
    finally:
        conn.close()


def test_build_zip_renders_overlay_for_user(tmp_path, monkeypatch):
    """#638 review: the analyst-zip path renders the editor override for the
    requesting user — raw Jinja2 (`{{ user.email }}`) must never reach the
    analyst's CLAUDE.md, matching what the non-IWT /api/welcome init ships."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _make_iwt_clone(tmp_path, claude_md="FROM CLONE")

    import src.initial_workspace as iw
    from src.repositories import claude_md_template_repo

    monkeypatch.setattr(iw, "validate_template_tree", lambda *a, **k: None)
    repo = claude_md_template_repo()
    repo.set("Workspace for {{ user.email }}", updated_by="a@x.com")

    from src.db import get_system_db

    conn = get_system_db()
    try:
        data = iw.build_zip(
            conn,
            user={"id": "u1", "email": "alice@example.com", "groups": []},
            server_url="http://test",
        )
    finally:
        conn.close()
    files = _zip_names_and_content(data)
    assert files["CLAUDE.md"] == "Workspace for alice@example.com"


def test_install_prompt_save_rejects_token_placeholder():
    """`{token}` stopped being substituted when the PAT handoff moved to
    /home step 4 — an override still carrying it would emit the literal
    string and every analyst's init would authenticate with garbage. The
    save-time validator must reject it with actionable guidance.
    """
    from fastapi import HTTPException

    from app.api.prompts import _validate_template

    with pytest.raises(HTTPException) as exc_info:
        _validate_template("install", "Set up.\nPersonal access token: {token}\n")
    assert exc_info.value.status_code == 400
    assert "{token}" in str(exc_info.value.detail)
    assert "~/.agnes/token" in str(exc_info.value.detail)

    # The token-free shape stays saveable.
    _validate_template("install", "Set up.\nagnes init --token-file ~/.agnes/token\n")


def test_install_prompt_save_rejects_bare_server_url_placeholder():
    """`{server_url}` (single brace) is the live-default's placeholder,
    substituted by a `str.replace` pass OUTSIDE Jinja
    (compute_default_agent_prompt / _claude_setup_instructions.jinja). An
    override renders through Jinja2 instead, which only processes `{{ }}` —
    an admin who seeds the editor from the live default and saves it
    verbatim would ship a prompt where `{server_url}` reaches the install
    agent as literal, unsubstituted text (no server to contact). The
    save-time validator must reject it with actionable guidance.
    """
    from fastapi import HTTPException

    from app.api.prompts import _validate_template

    with pytest.raises(HTTPException) as exc_info:
        _validate_template("install", "Server: {server_url}\ncurl -fsSL {server_url}/cli/download\n")
    assert exc_info.value.status_code == 400
    assert "{server_url}" in str(exc_info.value.detail)
    assert "{{ server.url }}" in str(exc_info.value.detail)

    # The correctly-escaped Jinja form stays saveable.
    _validate_template("install", "Server: {{ server.url }}\ncurl -fsSL {{ server.url }}/cli/download\n")

    # The bare-placeholder regex itself must not false-positive on a
    # no-space double-brace `{{server_url}}` (valid Jinja syntax, even
    # though it names the wrong/undefined variable — a separate concern the
    # render-time UndefinedError already catches on its own).
    from app.api.prompts import _BARE_SERVER_URL_RE

    assert not _BARE_SERVER_URL_RE.search("{{server_url}}")


# ---------------------------------------------------------------------------
# facts-extraction: the third managed prompt (owner requirement 2026-09-01)
# ---------------------------------------------------------------------------


def test_facts_extraction_is_not_validated_as_a_jinja_template():
    """It is never rendered through Jinja — it is handed to the model as
    literal system text. Validating it AS a template would reject a
    legitimate prompt: the output-format rules contain `{"id": ...}` braces
    a Jinja parser reads as syntax."""
    from app.api.prompts import _validate_template

    # No exception: a prompt full of JSON braces is legal content here.
    _validate_template("facts-extraction", 'Emit {"id": "x", "type": "y"} per line.')


def test_facts_extraction_prompt_is_not_git_bindable():
    """It is not a file in the Initial Workspace Template repo and has no
    seed path, so binding it would point at something no renderer reads.
    Refused with an explanation rather than silently accepted."""
    from fastapi import HTTPException

    from app.api.prompts import _require_git_bindable

    _require_git_bindable("workspace")  # unchanged for the two that are
    _require_git_bindable("install")

    with pytest.raises(HTTPException) as exc_info:
        _require_git_bindable("facts-extraction")
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["kind"] == "prompt_not_git_bindable"


def test_facts_extraction_default_is_the_builtin_prompt():
    from app.api.prompts import _live_default
    from connectors.sharepoint.facts_prompt import DEFAULT_EXTRACTION_PROMPT

    assert _live_default("facts-extraction", None, user={}, server_url="") == DEFAULT_EXTRACTION_PROMPT


def test_facts_extraction_preview_is_the_content_itself():
    """Rendered through nothing, so the preview can never differ from what
    the model is actually sent."""
    import asyncio

    from app.api.prompts import PreviewRequest, preview_prompt

    body = PreviewRequest(content='Rule 1: emit {"id": "x"}.')
    result = asyncio.run(preview_prompt("facts-extraction", body, request=None, user={}, conn=None))
    assert result == {"content": 'Rule 1: emit {"id": "x"}.'}


def test_install_preview_resolves_the_bare_server_url_placeholder(admin_client):
    """The shipped install-prompt default carries `{server_url}` substituted
    at bind time via `str.replace`, not through Jinja — an admin override
    that leaves it untouched (or the seed content itself) must still resolve
    it in the preview response, matching what a real install prompt sends,
    instead of leaving the literal placeholder in the "Copy rendered"
    output."""
    client, token = admin_client
    r = client.post(
        "/api/admin/prompts/install/preview",
        headers=_hdr(token),
        json={"content": "Server: {server_url}\n"},
    )
    assert r.status_code == 200, r.text
    content = r.json()["content"]
    assert "{server_url}" not in content
    assert "Server: http" in content


def test_an_unknown_prompt_kind_still_404s():
    from fastapi import HTTPException

    from app.api.prompts import _validate_kind

    with pytest.raises(HTTPException) as exc_info:
        _validate_kind("nope")
    assert exc_info.value.status_code == 404
    # The hint enumerates every kind, including the new one.
    assert "facts-extraction" in str(exc_info.value.detail["hint"])
