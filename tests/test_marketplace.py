"""Tests for marketplace registry + sync.

Uses a local bare git repo as a fake remote so no network is needed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers: local bare repo as a fake "remote"
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    full_env = {**os.environ, **(env or {})}
    # Minimal identity so commits work in CI sandboxes without global config.
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
    # git accepts file:// URLs and plain absolute paths as "URLs" for clone/fetch.
    # A file:// URL keeps things OS-agnostic.
    return path.resolve().as_uri()


@pytest.fixture
def fake_remote(tmp_path: Path):
    """Create a bare repo + seed one commit. Returns (bare_path, url, first_sha)."""
    work = tmp_path / "src-work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    (work / "README.md").write_text("initial\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "initial", cwd=work)

    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare))
    # Wire the work tree to push back to the bare remote so we can seed
    # additional commits during tests via _add_commit().
    _git("remote", "add", "origin", str(bare), cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work)

    return {"bare": bare, "work": work, "url": _file_url(bare), "sha": sha}


def _add_commit(fake_remote: dict, filename: str, content: str) -> str:
    """Add a new commit to the fake remote via the working clone + push."""
    work = fake_remote["work"]
    (work / filename).write_text(content, encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", f"add {filename}", cwd=work)
    _git("push", "origin", "main", cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


def _add_tag(fake_remote: dict, tag_name: str) -> str:
    """Tag the working clone's current HEAD and push the tag to the remote.

    Returns the tagged commit's SHA (annotated/lightweight-agnostic — we
    always create a lightweight tag, so the tag "sha" == the commit sha).
    """
    work = fake_remote["work"]
    _git("tag", tag_name, cwd=work)
    _git("push", "origin", tag_name, cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


# ---------------------------------------------------------------------------
# Environment — fresh DATA_DIR + fresh system.duckdb per test
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "marketplaces").mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    # Reset the shared system DB connection so it picks up the new DATA_DIR.
    import src.db as db

    if getattr(db, "_system_db_conn", None) is not None:
        try:
            db._system_db_conn.close()
        except Exception:
            pass
    db._system_db_conn = None
    db._system_db_path = None

    yield data_dir

    if getattr(db, "_system_db_conn", None) is not None:
        try:
            db._system_db_conn.close()
        except Exception:
            pass
    db._system_db_conn = None
    db._system_db_path = None


# ---------------------------------------------------------------------------
# Repository layer
# ---------------------------------------------------------------------------


def test_registry_crud(clean_env):
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = get_system_db()
    try:
        repo = MarketplaceRegistryRepository(conn)

        assert repo.list_all() == []
        assert repo.get("foo") is None

        repo.register(
            id="foo",
            name="Foo",
            url="https://example.com/foo.git",
            branch="main",
            token_env="FOO_TOKEN",
            description="demo",
            registered_by="admin@test.com",
        )
        row = repo.get("foo")
        assert row is not None
        assert row["url"] == "https://example.com/foo.git"
        assert row["branch"] == "main"
        assert row["token_env"] == "FOO_TOKEN"
        assert row["registered_by"] == "admin@test.com"
        assert row["last_synced_at"] is None

        # UPSERT: re-register with new name keeps row count at 1.
        repo.register(id="foo", name="Foo v2", url="https://example.com/foo.git")
        rows = repo.list_all()
        assert len(rows) == 1
        assert rows[0]["name"] == "Foo v2"

        from datetime import datetime, timezone

        repo.update_sync_status(
            "foo",
            commit_sha="abc123",
            synced_at=datetime.now(timezone.utc),
        )
        row = repo.get("foo")
        assert row["last_commit_sha"] == "abc123"
        assert row["last_synced_at"] is not None
        assert row["last_error"] is None

        # Error write
        repo.update_sync_status("foo", error="boom")
        assert repo.get("foo")["last_error"] == "boom"
        # Success after error clears it
        repo.update_sync_status("foo", commit_sha="def456", synced_at=datetime.now(timezone.utc))
        assert repo.get("foo")["last_error"] is None

        repo.unregister("foo")
        assert repo.get("foo") is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# sync_one — clone and update against local bare repo
# ---------------------------------------------------------------------------


def test_sync_one_clone_then_update(clean_env, fake_remote):
    from src.db import get_system_db
    from src.marketplace import sync_one
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(id="hello", name="Hello", url=fake_remote["url"], branch="main")
    finally:
        conn.close()

    result = sync_one("hello")
    assert result["action"] == "clone"
    assert result["commit"] == fake_remote["sha"]
    target = Path(result["path"])
    assert target.is_dir()
    assert (target / "README.md").exists()

    # Registry row updated
    conn = get_system_db()
    try:
        row = MarketplaceRegistryRepository(conn).get("hello")
        assert row["last_commit_sha"] == fake_remote["sha"]
        assert row["last_error"] is None
    finally:
        conn.close()

    new_sha = _add_commit(fake_remote, "new.txt", "hello world")

    result2 = sync_one("hello")
    assert result2["action"] == "update"
    assert result2["commit"] == new_sha
    assert result2["commit"] != fake_remote["sha"]
    assert (Path(result2["path"]) / "new.txt").exists()


def test_sync_one_failure_redacts_token(clean_env, tmp_path, monkeypatch):
    """A bogus HTTPS URL + token should fail with the token redacted from the error."""
    from src.db import get_system_db
    from src.marketplace import sync_one
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    token = "ghp_supersecrettoken1234567890"
    monkeypatch.setenv("AGNES_MARKETPLACE_BOGUS_TOKEN", token)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="bogus",
            name="Bogus",
            # Non-routable IP + unlikely port → git fails fast without real network.
            url="https://127.0.0.1:1/does-not-exist.git",
            token_env="AGNES_MARKETPLACE_BOGUS_TOKEN",
        )
    finally:
        conn.close()

    with pytest.raises(RuntimeError) as ei:
        sync_one("bogus")

    assert token not in str(ei.value)

    conn = get_system_db()
    try:
        row = MarketplaceRegistryRepository(conn).get("bogus")
        assert row["last_error"]
        assert token not in row["last_error"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# sync_marketplaces — collects errors per entry, empty registry = no-op
# ---------------------------------------------------------------------------


def test_sync_marketplaces_empty(clean_env):
    from src.marketplace import sync_marketplaces

    assert sync_marketplaces() == {"synced": [], "errors": []}


def test_sync_marketplaces_mixed(clean_env, fake_remote, monkeypatch):
    from src.db import get_system_db
    from src.marketplace import sync_marketplaces
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(Path(os.environ["DATA_DIR"]) / "no-global"))
    conn = get_system_db()
    try:
        repo = MarketplaceRegistryRepository(conn)
        repo.register(id="good", name="Good", url=fake_remote["url"], branch="main")
        repo.register(id="bad", name="Bad", url="https://127.0.0.1:1/x.git")
    finally:
        conn.close()

    result = sync_marketplaces()
    assert len(result["synced"]) == 1
    assert result["synced"][0]["id"] == "good"
    assert len(result["errors"]) == 1
    assert result["errors"][0]["id"] == "bad"


# ---------------------------------------------------------------------------
# served-cache invalidation on successful sync — issue #1615
#
# sync_marketplaces() already invalidated the ZIP ETag + cowork caches after
# a successful sync; sync_one() did not, so a manual "Sync now" reported a
# fresh commit while /marketplace.zip and the cowork bundle kept serving
# pre-sync bytes until their TTL expired. Both paths must go through the
# same helper so they can't drift again, and neither must invalidate on a
# failed sync (nothing changed, so warm caches are still correct).
# ---------------------------------------------------------------------------


def test_sync_one_invalidates_served_caches_on_success(clean_env, fake_remote, monkeypatch):
    from app.marketplace_server import cowork_packager, packager
    from src.db import get_system_db
    from src.marketplace import sync_one
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    calls: list[str] = []
    monkeypatch.setattr(packager, "invalidate_etag_cache", lambda: calls.append("etag"))
    monkeypatch.setattr(cowork_packager, "invalidate_cache", lambda: calls.append("cowork"))

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(id="hello", name="Hello", url=fake_remote["url"], branch="main")
    finally:
        conn.close()

    sync_one("hello")

    assert calls == ["etag", "cowork"], "a successful manual sync must invalidate both served caches"


def test_sync_one_does_not_invalidate_caches_on_failure(clean_env, tmp_path, monkeypatch):
    from app.marketplace_server import cowork_packager, packager
    from src.db import get_system_db
    from src.marketplace import sync_one
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    calls: list[str] = []
    monkeypatch.setattr(packager, "invalidate_etag_cache", lambda: calls.append("etag"))
    monkeypatch.setattr(cowork_packager, "invalidate_cache", lambda: calls.append("cowork"))

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="bogus",
            name="Bogus",
            url="https://127.0.0.1:1/does-not-exist.git",
        )
    finally:
        conn.close()

    with pytest.raises(RuntimeError):
        sync_one("bogus")

    assert calls == [], "a failed sync must not invalidate warm caches — nothing changed"


def test_sync_marketplaces_invalidates_served_caches_on_success(clean_env, fake_remote, monkeypatch):
    from app.marketplace_server import cowork_packager, packager
    from src.db import get_system_db
    from src.marketplace import sync_marketplaces
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(Path(os.environ["DATA_DIR"]) / "no-global"))
    calls: list[str] = []
    monkeypatch.setattr(packager, "invalidate_etag_cache", lambda: calls.append("etag"))
    monkeypatch.setattr(cowork_packager, "invalidate_cache", lambda: calls.append("cowork"))

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(id="good", name="Good", url=fake_remote["url"], branch="main")
    finally:
        conn.close()

    sync_marketplaces()

    assert calls == ["etag", "cowork"]


def test_sync_marketplaces_skips_invalidation_when_nothing_synced(clean_env, monkeypatch):
    from app.marketplace_server import cowork_packager, packager
    from src.db import get_system_db
    from src.marketplace import sync_marketplaces
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(Path(os.environ["DATA_DIR"]) / "no-global"))
    calls: list[str] = []
    monkeypatch.setattr(packager, "invalidate_etag_cache", lambda: calls.append("etag"))
    monkeypatch.setattr(cowork_packager, "invalidate_cache", lambda: calls.append("cowork"))

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(id="bad", name="Bad", url="https://127.0.0.1:1/x.git")
    finally:
        conn.close()

    sync_marketplaces()

    assert calls == [], "no marketplace synced → nothing to invalidate"


# ---------------------------------------------------------------------------
# ref pinning (tag / commit SHA) — issue #781
# ---------------------------------------------------------------------------


def test_is_valid_ref():
    from src.marketplace import is_full_sha, is_valid_ref

    # valid tags
    assert is_valid_ref("v1.2.3")
    assert is_valid_ref("release/2026.05")
    assert is_valid_ref("a")
    # valid full SHA (40 hex chars, case-insensitive)
    sha = "a" * 40
    assert is_valid_ref(sha)
    assert is_full_sha(sha)
    assert is_full_sha(sha.upper())
    assert not is_full_sha("v1.2.3")
    # invalid: empty, leading dash (flag-injection risk), traversal, lock file
    assert not is_valid_ref("")
    assert not is_valid_ref("-flag")
    assert not is_valid_ref("a..b")
    assert not is_valid_ref("foo.lock")
    assert not is_valid_ref("foo.")
    # short hex string is a valid tag name (not long enough to be a SHA) — accepted
    assert is_valid_ref("abc123")


def test_sync_spec_rejects_branch_and_ref_together(clean_env, fake_remote):
    from src.marketplace import sync_one
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="both", name="Both", url=fake_remote["url"], branch="main", ref="v1.0.0"
        )
    finally:
        conn.close()

    with pytest.raises(ValueError, match="mutually exclusive"):
        sync_one("both")


def test_sync_spec_rejects_invalid_ref_format(clean_env, fake_remote):
    from src.marketplace import sync_one
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="badref", name="BadRef", url=fake_remote["url"], ref="-not-valid"
        )
    finally:
        conn.close()

    with pytest.raises(ValueError, match="not a valid"):
        sync_one("badref")


def test_sync_one_pinned_tag_stays_when_remote_moves(clean_env, fake_remote):
    """A tag pin resolves via the same fetch+reset path as branch, and stays
    fixed across syncs even after the remote's default branch moves on."""
    from src.marketplace import sync_one
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    tag_sha = _add_tag(fake_remote, "v1.0.0")

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(id="tagged", name="Tagged", url=fake_remote["url"], ref="v1.0.0")
    finally:
        conn.close()

    result = sync_one("tagged")
    assert result["action"] == "clone"
    assert result["commit"] == tag_sha

    # Move the remote's default branch forward — the tag itself is untouched.
    new_sha = _add_commit(fake_remote, "moved-on.txt", "the future")
    assert new_sha != tag_sha

    result2 = sync_one("tagged")
    assert result2["action"] == "update"
    assert result2["commit"] == tag_sha
    assert not (Path(result2["path"]) / "moved-on.txt").exists()


def test_sync_one_pinned_sha_stays_when_remote_moves(clean_env, fake_remote):
    """A commit-SHA pin stays fixed across syncs even after the remote's
    default branch moves forward past it."""
    from src.marketplace import sync_one
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    pinned_sha = fake_remote["sha"]

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="shapinned", name="ShaPinned", url=fake_remote["url"], ref=pinned_sha
        )
    finally:
        conn.close()

    result = sync_one("shapinned")
    assert result["action"] == "clone"
    assert result["commit"] == pinned_sha

    new_sha = _add_commit(fake_remote, "moved-on.txt", "the future")
    assert new_sha != pinned_sha

    result2 = sync_one("shapinned")
    assert result2["action"] == "update"
    assert result2["commit"] == pinned_sha
    assert not (Path(result2["path"]) / "moved-on.txt").exists()


def test_sync_one_pinned_sha_mismatch_fails_and_keeps_previous_checkout(clean_env, fake_remote):
    """An unreachable SHA pin fails the sync (RuntimeError) and leaves the
    previously-synced working tree exactly as it was — same contract as any
    other sync failure."""
    from src.marketplace import sync_one
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="mismatch", name="Mismatch", url=fake_remote["url"], branch="main"
        )
    finally:
        conn.close()

    first = sync_one("mismatch")
    assert first["commit"] == fake_remote["sha"]
    target = Path(first["path"])
    assert (target / "README.md").exists()

    # Re-point the same row at an unreachable SHA (simulates a typo/rotated
    # pin) — bypass the API's format validation to write directly via the repo.
    bogus_sha = "deadbeef" * 5
    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="mismatch", name="Mismatch", url=fake_remote["url"], branch=None, ref=bogus_sha
        )
    finally:
        conn.close()

    with pytest.raises(RuntimeError):
        sync_one("mismatch")

    # Previous checkout untouched, and the failure is recorded.
    assert (target / "README.md").exists()
    conn = get_system_db()
    try:
        row = MarketplaceRegistryRepository(conn).get("mismatch")
        assert row["last_error"]
        assert row["last_commit_sha"] == fake_remote["sha"]  # not overwritten
    finally:
        conn.close()


def test_checkout_pinned_sha_falls_back_when_direct_fetch_rejected(tmp_path, monkeypatch):
    """Unit test of the fallback path with a mocked ``_run_git`` — simulates
    a git server that rejects direct-SHA fetches (no
    ``uploadpack.allowReachableSHA1InWant``), which we can't reliably force
    with a local file:// remote in tests."""
    from unittest.mock import patch

    import src.marketplace as marketplace_mod

    target = tmp_path / "repo"
    (target / ".git").mkdir(parents=True)
    sha = "b" * 40

    calls = []
    tokened = []

    def fake_run_git(args, cwd=None, *, url=None, token=None):
        calls.append(list(args))
        tokened.append(token)
        if args[:2] == ["fetch", "--depth"] and args[-1] == sha:
            raise subprocess.CalledProcessError(128, ["git", *args], stderr="not our ref")
        return subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")

    with patch.object(marketplace_mod, "_run_git", side_effect=fake_run_git):
        marketplace_mod._checkout_pinned_sha(target, sha, url="https://example.com/r.git", token="T")

    assert calls[0] == ["fetch", "--depth", "1", "origin", sha]
    # shallow (no .git/shallow file created in this fixture) -> plain fetch
    assert calls[1] == ["fetch", "origin"]
    assert calls[2] == ["checkout", "--detach", sha]
    # Both network calls carry the token — the fallback fetch is the one a
    # SHA-pinned private marketplace depends on, and it was easy to miss.
    assert tokened[0] == "T"
    assert tokened[1] == "T"


# ---------------------------------------------------------------------------
# URL auth helper
# ---------------------------------------------------------------------------


def test_credential_args_are_host_scoped():
    """Replaces test_authenticated_url: the PAT is wired per-host, not embedded.

    Mirrors the old test's case table (no token, https, https+port, non-https)
    so the same boundaries stay covered after the 2026-08-05 audit's F-2 fix.
    """
    from src.marketplace import _CREDENTIAL_HELPER, _credential_args

    # No token → no flags at all
    assert _credential_args("https://example.com/x.git", "") == []
    # HTTPS + token → generic reset first, then a host-scoped helper
    args = _credential_args("https://example.com/org/repo.git", "secret123")
    assert args[:2] == ["-c", "credential.helper="]
    assert args[2] == "-c"
    assert args[3] == f"credential.https://example.com.helper={_CREDENTIAL_HELPER}"
    # The token itself never appears on argv
    assert not any("secret123" in a for a in args)
    # With port → scoping keeps the port
    args = _credential_args("https://host:8443/repo.git", "t")
    assert args[3].startswith("credential.https://host:8443.helper=")
    # Non-HTTPS → no flags (git would not use an https-scoped helper anyway)
    assert _credential_args("file:///tmp/repo.git", "t") == []
    assert _credential_args("http://host/repo.git", "t") == []


def test_sync_never_puts_token_on_git_argv(clean_env, fake_remote, monkeypatch):
    """Clone AND update paths must both keep the PAT out of the command line.

    `fake_remote` yields a file:// URL, so this cannot exercise real HTTPS auth;
    the argv assertion is what it CAN prove, and it fails on the pre-fix code
    because `_authenticated_url` embedded the token in the clone argument. The
    on-disk half is covered by
    tests/test_security_audit_20260805.py::test_f2_scrub_strips_credentials_from_existing_config.
    """
    from src import marketplace as mp

    monkeypatch.setenv("ACME_MARKETPLACE_TOKEN", "SECRET123")
    calls: list[list[str]] = []
    real = mp._run_git

    def spy(args, cwd=None, **kw):
        calls.append(list(args))
        return real(args, cwd, **kw)

    monkeypatch.setattr(mp, "_run_git", spy)

    spec = {"id": "acme", "url": fake_remote["url"], "token_env": "ACME_MARKETPLACE_TOKEN"}
    mp._sync_spec(spec)  # clone
    mp._sync_spec(spec)  # update

    flat = [arg for call in calls for arg in call]
    assert not any("SECRET123" in a for a in flat), f"token on argv: {calls!r}"

    config = mp.get_marketplaces_dir() / "acme" / ".git" / "config"
    assert "SECRET123" not in config.read_text(encoding="utf-8")


def test_is_valid_slug():
    from src.marketplace import is_valid_slug

    assert is_valid_slug("foo")
    assert is_valid_slug("foo-bar")
    assert is_valid_slug("foo_bar_99")
    assert is_valid_slug("a")
    assert not is_valid_slug("")
    assert not is_valid_slug("Foo")
    assert not is_valid_slug("../etc")
    assert not is_valid_slug("foo/bar")
    assert not is_valid_slug("-foo")
    assert not is_valid_slug("a" * 65)


# ---------------------------------------------------------------------------
# Admin API — CRUD + token persistence in .env_overlay
# ---------------------------------------------------------------------------


def test_api_create_with_token_persists_to_overlay(seeded_app, fake_remote):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    data_dir = Path(seeded_app["env"]["data_dir"])

    pat = "ghp_testsecret_abcdef1234567890"
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "Hello",
            "slug": "hello",
            "url": fake_remote["url"].replace("file://", "https://") if False else "https://example.com/hello.git",
            "token": pat,
            "curator_name": "Test Curator",
            "curator_email": "curator@example.com",
        },
    )
    # URL must start with https:// per our validator — the placeholder above
    # is a plain https URL; it's only persisted, not hit by this endpoint.
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "hello"
    assert body["has_token"] is True
    assert "token" not in body  # response never echoes the secret

    overlay = (data_dir / "state" / ".env_overlay").read_text()
    assert f"AGNES_MARKETPLACE_HELLO_TOKEN={pat}" in overlay
    assert os.environ.get("AGNES_MARKETPLACE_HELLO_TOKEN") == pat

    # GET list includes it
    r = client.get("/api/marketplaces", headers=token_headers)
    assert r.status_code == 200
    entries = r.json()
    assert any(e["id"] == "hello" and e["has_token"] for e in entries)


def test_api_rejects_bad_slug_and_non_https(seeded_app):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    curator = {"curator_name": "Curator", "curator_email": "c@example.com"}

    # Bad slug
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={"name": "X", "slug": "../etc", "url": "https://example.com/x.git", **curator},
    )
    assert r.status_code == 400

    # Non-https URL
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={"name": "X", "slug": "xy", "url": "http://example.com/x.git", **curator},
    )
    assert r.status_code == 400

    # SSRF: https but pointing at a private/reserved host — the clone runs
    # server-side, so these must be rejected (audit L2). Chosen so the check
    # short-circuits without an external DNS lookup: `localhost` is a literal
    # deny, and a link-local IP needs no resolution.
    for bad_url in ("https://localhost/x.git", "https://169.254.169.254/x.git"):
        r = client.post(
            "/api/marketplaces",
            headers=token_headers,
            json={"name": "X", "slug": "ssrf", "url": bad_url, **curator},
        )
        assert r.status_code == 400, f"{bad_url} -> {r.status_code}: {r.text}"
        assert "private or reserved" in r.text, r.text


def test_api_ssrf_allowlist_permits_trusted_host(seeded_app, monkeypatch):
    """A deployer-configured allowlist (AGNES_SSRF_ALLOWED_HOSTS) exempts an
    internal host from the private/reserved-network reject — the escape hatch
    for an on-prem git host on a private network. Uses a link-local IP literal
    as both URL host and allowlist entry so the match happens at the hostname
    stage before any DNS resolution (hermetic); register() writes the registry
    row only, no clone runs at request time."""
    monkeypatch.setenv("AGNES_SSRF_ALLOWED_HOSTS", "169.254.169.254")
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    curator = {"curator_name": "Curator", "curator_email": "c@example.com"}
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "Internal",
            "slug": "internal",
            "url": "https://169.254.169.254/x.git",
            **curator,
        },
    )
    assert r.status_code == 201, r.text


def test_ssrf_allowed_hosts_parsing(monkeypatch):
    """get_ssrf_allowed_hosts normalizes a comma-separated env value (strip +
    lowercase + drop empties) and treats an empty string as no allowlist."""
    from app.instance_config import get_ssrf_allowed_hosts

    monkeypatch.setenv("AGNES_SSRF_ALLOWED_HOSTS", " Git.Internal , ghe.corp ,")
    assert get_ssrf_allowed_hosts() == frozenset({"git.internal", "ghe.corp"})

    monkeypatch.setenv("AGNES_SSRF_ALLOWED_HOSTS", "")
    assert get_ssrf_allowed_hosts() == frozenset()


def test_api_rejects_ref_and_branch_together(seeded_app):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    curator = {"curator_name": "Curator", "curator_email": "c@example.com"}

    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "X",
            "slug": "refbranch",
            "url": "https://example.com/x.git",
            "branch": "main",
            "ref": "v1.0.0",
            **curator,
        },
    )
    assert r.status_code == 400
    assert "mutually exclusive" in r.text


def test_api_rejects_invalid_ref_format(seeded_app):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    curator = {"curator_name": "Curator", "curator_email": "c@example.com"}

    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "X",
            "slug": "badref",
            "url": "https://example.com/x.git",
            "ref": "-not-valid",
            **curator,
        },
    )
    assert r.status_code == 400


def test_api_create_and_patch_ref_pin(seeded_app):
    """A ref pin round-trips through create + GET, and can be cleared via
    an empty-string PATCH (switching back to floating branch/HEAD)."""
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    curator = {"curator_name": "Curator", "curator_email": "c@example.com"}

    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "Pinned",
            "slug": "pinned-mp",
            "url": "https://example.com/x.git",
            "ref": "v2.0.0",
            **curator,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ref"] == "v2.0.0"
    assert body["branch"] is None

    # Switching to a branch while a ref pin is active must be rejected
    # unless the ref is explicitly cleared in the same request.
    r = client.patch("/api/marketplaces/pinned-mp", headers=token_headers, json={"branch": "main"})
    assert r.status_code == 400
    assert "mutually exclusive" in r.text

    # Clearing ref + setting branch together works.
    r = client.patch("/api/marketplaces/pinned-mp", headers=token_headers, json={"branch": "main", "ref": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["branch"] == "main"
    assert body["ref"] is None

    # Re-pin to a commit SHA.
    sha = "a" * 40
    r = client.patch("/api/marketplaces/pinned-mp", headers=token_headers, json={"branch": "", "ref": sha})
    assert r.status_code == 200
    body = r.json()
    assert body["ref"] == sha
    assert body["branch"] is None


def test_api_create_requires_curator(seeded_app):
    """v32: curator_name + curator_email are mandatory at create time.

    Three failure shapes are checked: missing both, missing email, malformed
    email. Each must surface a 400 with a curator-specific message so the
    admin form can render a useful toast.
    """
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    # Both missing
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={"name": "X", "slug": "cnone", "url": "https://example.com/x.git"},
    )
    assert r.status_code == 400
    assert "curator_name" in r.text

    # Email missing
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "X",
            "slug": "cmail",
            "url": "https://example.com/x.git",
            "curator_name": "Test",
        },
    )
    assert r.status_code == 400
    assert "curator_email" in r.text

    # Email malformed
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "X",
            "slug": "cbad",
            "url": "https://example.com/x.git",
            "curator_name": "Test",
            "curator_email": "not-an-email",
        },
    )
    assert r.status_code == 400


def test_api_curator_round_trip(seeded_app):
    """Curator fields persist through create + GET list, and a PATCH edit
    updates them. Empty-string curator inputs on PATCH leave the existing
    values unchanged (per the help text on the edit modal)."""
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "Curator Test",
            "slug": "curator-rt",
            "url": "https://example.com/x.git",
            "curator_name": "Alice Original",
            "curator_email": "alice@example.com",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["curator_name"] == "Alice Original"
    assert body["curator_email"] == "alice@example.com"

    # Update curator name only
    r = client.patch(
        "/api/marketplaces/curator-rt",
        headers=token_headers,
        json={"curator_name": "Bob New"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["curator_name"] == "Bob New"
    assert body["curator_email"] == "alice@example.com"  # unchanged

    # Empty-string PATCH leaves the value alone (not a clear)
    r = client.patch(
        "/api/marketplaces/curator-rt",
        headers=token_headers,
        json={"curator_name": "", "curator_email": "  "},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["curator_name"] == "Bob New"
    assert body["curator_email"] == "alice@example.com"

    # Malformed email rejected on PATCH too
    r = client.patch(
        "/api/marketplaces/curator-rt",
        headers=token_headers,
        json={"curator_email": "not-an-email"},
    )
    assert r.status_code == 400


def test_patch_legacy_row_without_curator_is_rejected(seeded_app):
    """Pre-v32 rows can survive in the DB with NULL curator (the column is
    nullable so the migration doesn't break operator instances). But the
    moment an admin opens the edit modal — touches URL, description, name,
    anything — the API must reject the PATCH unless the curator gap is
    closed in the same payload. Otherwise the OWNER_TODO_PLACEHOLDER lingers
    on every /marketplace card forever (PR #234 review #5).
    """
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    # Seed a legacy row directly via the repository — bypasses the API
    # validation, mimicking a row that pre-dates v32.
    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="legacy-mp",
            name="Legacy",
            url="https://example.com/legacy.git",
            # curator_name + curator_email default to None here
        )
    finally:
        conn.close()

    # PATCH that only updates the URL — must 400 because the existing row
    # has no curator and the payload doesn't fill it.
    r = client.patch(
        "/api/marketplaces/legacy-mp",
        headers=token_headers,
        json={"url": "https://example.com/legacy-renamed.git"},
    )
    assert r.status_code == 400, r.text
    assert "curator_name is required" in r.text

    # Same PATCH with curator_name only — still 400 because email is empty.
    r = client.patch(
        "/api/marketplaces/legacy-mp",
        headers=token_headers,
        json={
            "url": "https://example.com/legacy-renamed.git",
            "curator_name": "Late Curator",
        },
    )
    assert r.status_code == 400, r.text
    assert "curator_email is required" in r.text

    # Now fill BOTH — PATCH succeeds, the row carries the new curator.
    r = client.patch(
        "/api/marketplaces/legacy-mp",
        headers=token_headers,
        json={
            "url": "https://example.com/legacy-renamed.git",
            "curator_name": "Late Curator",
            "curator_email": "late@example.com",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["curator_name"] == "Late Curator"
    assert body["curator_email"] == "late@example.com"

    # Subsequent PATCH on the now-fully-formed row that doesn't mention
    # curator at all keeps working (sanity: the gate fires only when the
    # row would persist with empty curator).
    r = client.patch(
        "/api/marketplaces/legacy-mp",
        headers=token_headers,
        json={"description": "Now annotated"},
    )
    assert r.status_code == 200, r.text


def test_api_delete_clears_overlay_binding(seeded_app):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    pat = "ghp_another_test_token"
    client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "Temp",
            "slug": "temp",
            "url": "https://example.com/temp.git",
            "token": pat,
            "curator_name": "Curator",
            "curator_email": "c@example.com",
        },
    )
    assert os.environ.get("AGNES_MARKETPLACE_TEMP_TOKEN") == pat

    r = client.delete("/api/marketplaces/temp?purge=false", headers=token_headers)
    assert r.status_code == 204
    assert os.environ.get("AGNES_MARKETPLACE_TEMP_TOKEN") in (None, "")


def test_refresh_plugin_cache_drops_missing_internal_assets(clean_env, monkeypatch):
    """v32 enrichment drop semantics — when marketplace-metadata references files
    that don't exist on disk (or external URLs that fail to mirror), those
    entries are removed from the served metadata so the UI never shows a
    broken link / image. We exercise the missing-internal-file branch
    directly because it's deterministic without network.
    """
    from src.db import get_system_db
    from src.marketplace import _refresh_plugin_cache, is_valid_slug
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    slug = "drop-test"
    assert is_valid_slug(slug)

    # Stand up a minimal cloned marketplace tree by hand. No git involved —
    # the helper reads from disk directly.
    repo_root = clean_env / "marketplaces" / slug
    (repo_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (repo_root / "plugins" / "demo").mkdir(parents=True, exist_ok=True)

    # Real marketplace.json — single plugin
    (repo_root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "drop-test",
                "owner": {"name": "T"},
                "plugins": [
                    {
                        "name": "demo",
                        "description": "test",
                        "version": "1.0",
                        "source": "./plugins/demo",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    # marketplace-metadata referencing a mix of valid + missing internal paths
    (repo_root / ".claude-plugin" / "marketplace-metadata.json").write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": {
                    "demo": {
                        # cover_photo points at a file that does NOT exist
                        "cover_photo": ".agnes/missing-cover.png",
                        "doc_links": [
                            # Internal path that exists → should survive
                            {"name": "ok-doc", "path": "docs/ok.md"},
                            # Internal path that doesn't exist → dropped
                            {"name": "missing-doc", "path": "docs/missing.md"},
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    # Create the file referenced by the surviving doc_link
    (repo_root / "docs").mkdir(exist_ok=True)
    (repo_root / "docs" / "ok.md").write_text("# ok\n", encoding="utf-8")

    # Register the marketplace so the cache write has a parent row to point at.
    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id=slug,
            name="drop test",
            url="https://example.com/x.git",
            curator_name="C",
            curator_email="c@example.com",
        )
    finally:
        conn.close()

    written = _refresh_plugin_cache(slug)
    assert written == 1

    # Assert the DB row reflects the drops:
    #   cover_photo_url is NULL (missing internal file)
    #   doc_links carries only the surviving entry (ok-doc)
    conn = get_system_db()
    try:
        row = conn.execute(
            "SELECT cover_photo_url, doc_links FROM marketplace_plugins WHERE marketplace_id = ? AND name = ?",
            [slug, "demo"],
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    cover_url, doc_links_json = row
    assert cover_url is None, f"missing internal cover should be dropped, got {cover_url}"

    import json as _json

    doc_links = _json.loads(doc_links_json) if isinstance(doc_links_json, str) else doc_links_json
    assert isinstance(doc_links, list) and len(doc_links) == 1
    assert doc_links[0]["name"] == "ok-doc"


def test_api_sync_endpoint(seeded_app, fake_remote):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    # Register marketplace pointing at our fake remote.
    r = client.post(
        "/api/marketplaces",
        headers=token_headers,
        json={
            "name": "Hello",
            "slug": "sync-hello",
            "url": "https://example.com/placeholder.git",  # URL in DB (not dialed here)
            "curator_name": "Curator",
            "curator_email": "c@example.com",
        },
    )
    assert r.status_code == 201

    # Patch the URL to the local file:// one. PATCH requires https://, so we
    # go around it by writing directly via the repo — simulates an admin
    # that registered then later rotated to a real URL behind a reverse proxy.
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="sync-hello", name="Hello", url=fake_remote["url"], branch="main"
        )
    finally:
        conn.close()

    r = client.post("/api/marketplaces/sync-hello/sync", headers=token_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["commit"] == fake_remote["sha"]
    assert body["action"] == "clone"


def test_api_sync_nonexistent_returns_404(seeded_app):
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.post("/api/marketplaces/missing/sync", headers=token_headers)
    assert r.status_code == 404


def test_api_sync_builtin_returns_409(seeded_app):
    """"Sync now" on a built-in row is refused, not attempted.

    Built-in rows carry a `builtin://` sentinel URL. Before the guard, this
    endpoint passed it to git — which failed with "remote helper 'builtin'
    aborted session" AFTER the clone path had already rmtree'd the seeded
    content — and stamped a `last_error` that the nightly sync (which skips
    built-in rows) never clears.
    """
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="builtin-row",
            name="Built-in",
            url="builtin://builtin-row",
            is_builtin=True,
        )
    finally:
        conn.close()

    r = client.post("/api/marketplaces/builtin-row/sync", headers=token_headers)
    assert r.status_code == 409, r.text
    assert "built-in" in r.json()["detail"]

    # No last_error stamped, and the flag is surfaced so the admin table can
    # drop the button rather than offer an action the API refuses.
    r = client.get("/api/marketplaces", headers=token_headers)
    assert r.status_code == 200, r.text
    row = next(m for m in r.json() if m["id"] == "builtin-row")
    assert row["is_builtin"] is True
    assert row["last_error"] is None


def test_api_delete_builtin_returns_409(seeded_app):
    """A built-in row cannot be deleted, with or without `purge`.

    `agnes-builtin` is re-seeded from the wheel on every boot, so deleting it
    is a no-op the next restart undoes. The contributed marketplace has no
    re-seed — with `purge=true` `delete_marketplace_dir` would take its locally
    written skills with it, permanently. Retiring built-in content is what the
    per-plugin disable endpoint is for.
    """
    client = seeded_app["client"]
    token_headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="builtin-del",
            name="Built-in",
            url="builtin://builtin-del",
            is_builtin=True,
        )
    finally:
        conn.close()

    for query in ("", "?purge=true"):
        r = client.delete(f"/api/marketplaces/builtin-del{query}", headers=token_headers)
        assert r.status_code == 409, f"purge={query!r}: {r.text}"
        assert "built-in" in r.json()["detail"]

    # Row survives both attempts.
    r = client.get("/api/marketplaces", headers=token_headers)
    assert r.status_code == 200, r.text
    assert any(m["id"] == "builtin-del" for m in r.json()), "built-in row must survive a refused delete"


def test_api_requires_admin(seeded_app):
    client = seeded_app["client"]
    analyst_headers = {"Authorization": f"Bearer {seeded_app['analyst_token']}"}
    r = client.get("/api/marketplaces", headers=analyst_headers)
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# delete_marketplace_dir helper
# ---------------------------------------------------------------------------


def test_delete_marketplace_dir(clean_env):
    from src.marketplace import delete_marketplace_dir
    from app.utils import get_marketplaces_dir

    target = get_marketplaces_dir() / "foo"
    target.mkdir(parents=True)
    (target / "a.txt").write_text("x")

    assert delete_marketplace_dir("foo") is True
    assert not target.exists()

    # Idempotent: deleting twice returns False, no exception
    assert delete_marketplace_dir("foo") is False

    with pytest.raises(ValueError):
        delete_marketplace_dir("../etc")


class TestReadPluginsSourceValidation:
    """Layer-1 ingest validation for the plugin ``source`` field (security
    playbook §6: reject at ingest, contain at use). A relative path inside the
    clone — including ``"./"`` for a root-source plugin — is legitimate; an
    absolute path or one with ``..`` segments is curator-hostile and drops the
    plugin. Dict (external) sources pass ingest: they are valid Claude Code
    catalog entries, they just have no local files for Agnes to serve."""

    def _write_manifest(self, plugins: list[dict]) -> None:
        from app.utils import get_marketplaces_dir

        d = get_marketplaces_dir() / "srcmkt" / ".claude-plugin"
        d.mkdir(parents=True, exist_ok=True)
        (d / "marketplace.json").write_text(json.dumps({"name": "srcmkt", "plugins": plugins}), encoding="utf-8")

    def test_root_and_relative_sources_kept(self, clean_env):
        from src.marketplace import read_plugins

        self._write_manifest(
            [
                {"name": "root-plug", "source": "./"},
                {"name": "sub-plug", "source": "./plugins/sub-plug"},
                {"name": "bare-plug"},
            ]
        )
        names = [p["name"] for p in read_plugins("srcmkt")]
        assert names == ["root-plug", "sub-plug", "bare-plug"]

    def test_absolute_source_drops_plugin(self, clean_env):
        from src.marketplace import read_plugins

        self._write_manifest([{"name": "evil", "source": "/etc"}, {"name": "ok", "source": "./"}])
        assert [p["name"] for p in read_plugins("srcmkt")] == ["ok"]

    def test_traversal_source_drops_plugin(self, clean_env):
        from src.marketplace import read_plugins

        self._write_manifest(
            [
                {"name": "evil", "source": "../other"},
                {"name": "evil2", "source": "./x/../../y"},
                {"name": "ok", "source": "./"},
            ]
        )
        assert [p["name"] for p in read_plugins("srcmkt")] == ["ok"]

    def test_external_dict_source_passes_ingest(self, clean_env):
        from src.marketplace import read_plugins

        self._write_manifest([{"name": "ext", "source": {"source": "github", "repo": "acme/ext"}}])
        assert [p["name"] for p in read_plugins("srcmkt")] == ["ext"]


class TestReadPluginsRemoteStringSource:
    """A remote STRING source (``"https://github.com/x/y"``) is a shape Agnes
    already accepts — ``marketplace_metadata_scaffold`` keeps such a plugin and
    only skips skill enumeration (see
    ``test_marketplace_metadata_scaffold.py::
    test_remote_source_skips_enumeration_but_keeps_plugin_fields``). Ingest
    must therefore treat it like the dict form (catalog entry, no local files)
    rather than dropping the row: a dropped row disappears from Browse and
    leaves its ``resource_grants`` dangling. Only paths that escape the clone
    are rejected."""

    def _write_manifest(self, plugins: list[dict]) -> None:
        from app.utils import get_marketplaces_dir

        d = get_marketplaces_dir() / "shapemkt" / ".claude-plugin"
        d.mkdir(parents=True, exist_ok=True)
        (d / "marketplace.json").write_text(json.dumps({"name": "shapemkt", "plugins": plugins}), encoding="utf-8")

    def test_url_and_scp_sources_survive_ingest(self, clean_env, caplog):
        from src.marketplace import read_plugins

        self._write_manifest(
            [
                {"name": "url-plug", "source": "https://github.com/x/y"},
                {"name": "scp-plug", "source": "git@github.com:x/y.git"},
            ]
        )
        with caplog.at_level(logging.WARNING):
            names = [p["name"] for p in read_plugins("shapemkt")]
        assert names == ["url-plug", "scp-plug"]
        # Kept, so nothing is warned about — and certainly not as "unsafe".
        assert [r.getMessage() for r in caplog.records] == []

    def test_escaping_path_is_still_dropped_with_an_accurate_reason(self, clean_env, caplog):
        from src.marketplace import read_plugins

        self._write_manifest(
            [
                {"name": "evil", "source": "../../etc"},
                {"name": "evil2", "source": "/etc"},
                {"name": "ok", "source": "./"},
            ]
        )
        with caplog.at_level(logging.WARNING):
            assert [p["name"] for p in read_plugins("shapemkt")] == ["ok"]
        msg = " ".join(r.getMessage() for r in caplog.records)
        assert "escapes the marketplace clone" in msg

    def test_dict_source_survives_ingest(self, clean_env):
        from src.marketplace import read_plugins

        self._write_manifest([{"name": "ext", "source": {"source": "github", "repo": "acme/ext"}}])
        assert [p["name"] for p in read_plugins("shapemkt")] == ["ext"]


class TestExternalPluginSourceClassifier:
    """``is_external_plugin_source`` decides catalog-entry vs rejection, so its
    edges matter: a Windows-style path is not "external", and a bare
    ``owner/repo`` is indistinguishable from a relative directory."""

    @pytest.mark.parametrize(
        "source",
        ["https://github.com/x/y", "git+ssh://host/x.git", "git@github.com:x/y.git"],
    )
    def test_remote_forms_are_external(self, source):
        from src.marketplace import is_external_plugin_source

        assert is_external_plugin_source(source)

    @pytest.mark.parametrize(
        "source",
        ["./", "./plugins/x", "owner/repo", "../escape", "/etc", r"..\..\windows", "", {"source": "github"}],
    )
    def test_non_remote_forms_are_not_external(self, source):
        from src.marketplace import is_external_plugin_source

        assert not is_external_plugin_source(source)


def test_refresh_plugin_cache_auto_disables_deprecated(clean_env, monkeypatch):
    """A plugin marked ``"deprecated": true`` in marketplace-metadata.json is
    admin-disabled automatically at sync — one upstream commit retires it on
    every consuming instance. One-way: removing the flag later must NOT
    auto-re-enable (the admin decides whether a retired plugin comes back)."""
    from src.db import get_system_db
    from src.marketplace import _refresh_plugin_cache
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    slug = "deprecate-test"
    repo_root = clean_env / "marketplaces" / slug
    (repo_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)

    (repo_root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": slug,
                "owner": {"name": "T"},
                "plugins": [
                    {"name": "old", "version": "1.0", "source": "./plugins/old"},
                    {"name": "fresh", "version": "1.0", "source": "./plugins/fresh"},
                ],
            }
        ),
        encoding="utf-8",
    )
    meta_path = repo_root / ".claude-plugin" / "marketplace-metadata.json"
    meta_path.write_text(
        json.dumps({"version": 1, "plugins": {"old": {"deprecated": True}}}),
        encoding="utf-8",
    )

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id=slug,
            name="deprecate test",
            url="https://example.com/x.git",
            curator_name="C",
            curator_email="c@example.com",
        )
    finally:
        conn.close()

    def _disabled_flags():
        conn = get_system_db()
        try:
            rows = conn.execute(
                "SELECT name, admin_disabled FROM marketplace_plugins WHERE marketplace_id = ?",
                [slug],
            ).fetchall()
        finally:
            conn.close()
        return dict(rows)

    assert _refresh_plugin_cache(slug) == 2
    flags = _disabled_flags()
    assert flags["old"] is True, "deprecated plugin must be auto-disabled at sync"
    assert flags["fresh"] is False, "non-deprecated sibling must stay enabled"

    # One-way: drop the flag upstream, re-sync — the plugin STAYS disabled.
    meta_path.write_text(json.dumps({"version": 1, "plugins": {}}), encoding="utf-8")
    assert _refresh_plugin_cache(slug) == 2
    flags = _disabled_flags()
    assert flags["old"] is True, "removing the flag must never auto-re-enable"


# ---------------------------------------------------------------------------
# route handler shape — issue #1614
#
# FastAPI runs `async def` handlers on the event loop; `def` handlers run in
# a thread pool. trigger_sync's body is fully blocking (subprocess git
# clone, DuckDB writes, a process-wide lock), so it must be declared `def`
# like its sibling trigger_sync_all — otherwise one "Sync now" freezes every
# other request for the duration of the sync.
# ---------------------------------------------------------------------------


def test_trigger_sync_and_trigger_sync_all_are_not_coroutines():
    import inspect

    from app.api.marketplaces import trigger_sync, trigger_sync_all

    assert not inspect.iscoroutinefunction(trigger_sync), (
        "trigger_sync calls the blocking sync_one() directly — as `async def` it would "
        "run on the event loop and block every other request for the duration of the sync"
    )
    assert not inspect.iscoroutinefunction(trigger_sync_all)
