"""Integration tests for /marketplace.zip and /marketplace/info.

v13: uses user_group_members + resource_grants (no PluginAccessRepository,
no users.groups JSON). Admin is a regular group for marketplace filtering —
no god-mode shortcut.
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


@pytest.fixture
def marketplace_env(e2e_env, monkeypatch, shared_app):
    """Spin up the FastAPI app with two fake marketplaces populated on disk.

    Populates:
      - marketplace_registry with 2 slugs: 'mkt-a', 'mkt-b'
      - marketplace_plugins with:
          mkt-a: plug-x (v1.0)
          mkt-b: plug-y (v2.0), plug-z (v3.0)
      - DATA_DIR/marketplaces/<slug>/plugins/<plugin>/ with a tiny CLAUDE.md
      - admin user in Admin group with grants for all 3 plugins
      - analyst user in TestGroup with grant for plug-y only
      - nogroups user (only Everyone, no grants)
    """
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    data_dir = e2e_env["data_dir"]

    # Plugin folders on disk — each ships a real .claude-plugin/plugin.json
    # so the synth marketplace.json picks up the plugin's authoritative name
    # (matches what real upstream marketplaces do, and exercises the
    # manifest_name resolution path).
    for slug, plug in [("mkt-a", "plug-x"), ("mkt-b", "plug-y"), ("mkt-b", "plug-z")]:
        d = data_dir / "marketplaces" / slug / "plugins" / plug
        d.mkdir(parents=True, exist_ok=True)
        (d / "CLAUDE.md").write_text(f"# {plug}\nThis is {plug} from {slug}.\n", encoding="utf-8")
        skills = d / "skills"
        skills.mkdir()
        (skills / "hello.md").write_text(f"skill for {plug}", encoding="utf-8")
        (d / ".claude-plugin").mkdir()
        (d / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": plug, "version": "1.0"}),
            encoding="utf-8",
        )

    # DB setup
    conn = get_system_db()
    try:
        t = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
            [
                "mkt-a",
                "Market A",
                "https://example.test/a.git",
                t,
                "mkt-b",
                "Market B",
                "https://example.test/b.git",
                t,
            ],
        )
        for slug, name, ver in [
            ("mkt-a", "plug-x", "1.0"),
            ("mkt-b", "plug-y", "2.0"),
            ("mkt-b", "plug-z", "3.0"),
        ]:
            raw = {"name": name, "version": ver, "source": f"./plugins/{name}", "description": f"{name} from {slug}"}
            conn.execute(
                "INSERT INTO marketplace_plugins (marketplace_id, name, version, raw, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [slug, name, ver, json.dumps(raw), t],
            )

        users = UserRepository(conn)
        users.create(id="admin1", email="admin@test.local", name="Admin")
        users.create(id="analyst1", email="analyst@test.local", name="Analyst")
        users.create(id="nogroups1", email="nobody@test.local", name="Nobody")

        # System groups are seeded by db.init_schema(); look them up.
        ug = UserGroupsRepository(conn)
        ug.ensure_system("Admin", "system")
        ug.ensure_system("Everyone", "system")

        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name='Admin'").fetchone()[0]

        # Create a custom group for the analyst
        tg = ug.create(name="TestGroup", description="granted plug-y only")
        test_group_gid = tg["id"]

        # Assign memberships
        ugm = UserGroupMembersRepository(conn)
        ugm.add_member("admin1", admin_gid, source="system_seed")
        ugm.add_member("analyst1", test_group_gid, source="admin")
        # nogroups1 is only in Everyone (auto-membership, no explicit row needed)

        # Grant plugins via resource_grants
        rg = ResourceGrantsRepository(conn)
        # Admin group gets all 3 plugins
        rg.create(group_id=admin_gid, resource_type="marketplace_plugin", resource_id="mkt-a/plug-x")
        rg.create(group_id=admin_gid, resource_type="marketplace_plugin", resource_id="mkt-b/plug-y")
        rg.create(group_id=admin_gid, resource_type="marketplace_plugin", resource_id="mkt-b/plug-z")
        # TestGroup gets only plug-y
        rg.create(group_id=test_group_gid, resource_type="marketplace_plugin", resource_id="mkt-b/plug-y")

        # Model B (v28+): grant alone is no longer enough — explicitly
        # subscribe each user to every plugin they should see in the
        # served set. Pre-v28 fixtures relied on the auto-included
        # behavior; tests below still expect the same served sets, so we
        # mirror those expectations with explicit subscriptions.
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )

        subs = UserCuratedSubscriptionsRepository(conn)
        subs.subscribe("admin1", "mkt-a", "plug-x")
        subs.subscribe("admin1", "mkt-b", "plug-y")
        subs.subscribe("admin1", "mkt-b", "plug-z")
        subs.subscribe("analyst1", "mkt-b", "plug-y")
    finally:
        conn.close()

    # Tokens
    admin_token = create_access_token("admin1", "admin@test.local")
    analyst_token = create_access_token("analyst1", "analyst@test.local")
    nogroups_token = create_access_token("nogroups1", "nobody@test.local")

    app = shared_app
    client = TestClient(app)
    return {
        "client": client,
        "admin_token": admin_token,
        "analyst_token": analyst_token,
        "nogroups_token": nogroups_token,
        "data_dir": data_dir,
    }


def _read_zip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


class TestMarketplaceInfo:
    def test_admin_sees_all_plugins(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace/info", headers=_auth(marketplace_env["admin_token"]))
        assert resp.status_code == 200
        info = resp.json()
        # `name` in /marketplace/info mirrors what the synth manifest
        # serves — the plugin's authoritative manifest_name (unprefixed
        # in this fixture because plugin.json sets name=<plug>).
        names = {p["name"] for p in info["plugins"]}
        assert names == {"plug-x", "plug-y", "plug-z"}
        # prefixed_name is exposed alongside so operators can still
        # disambiguate a plugin's source marketplace.
        prefixed = {p["prefixed_name"] for p in info["plugins"]}
        assert prefixed == {"mkt-a-plug-x", "mkt-b-plug-y", "mkt-b-plug-z"}
        assert "Admin" in info["groups"]
        assert info["marketplace_name"] == "agnes"
        assert info["plugin_count"] == 3

    def test_analyst_sees_only_granted_plugin(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace/info", headers=_auth(marketplace_env["analyst_token"]))
        assert resp.status_code == 200
        info = resp.json()
        names = {p["name"] for p in info["plugins"]}
        assert names == {"plug-y"}
        assert "TestGroup" in info["groups"]

    def test_user_with_no_groups_sees_empty_payload(self, marketplace_env):
        """Auto-Everyone removal: a user with zero memberships now sees an
        empty groups list and zero plugins (no implicit Everyone fallback)."""
        c = marketplace_env["client"]
        resp = c.get("/marketplace/info", headers=_auth(marketplace_env["nogroups_token"]))
        assert resp.status_code == 200
        info = resp.json()
        assert info["groups"] == []
        assert info["plugins"] == []

    def test_missing_auth_returns_401(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace/info")
        assert resp.status_code == 401


class TestMarketplaceZip:
    def test_admin_zip_contains_all_plugins_with_prefix(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace.zip", headers=_auth(marketplace_env["admin_token"]))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert resp.headers["etag"].startswith('"') and resp.headers["etag"].endswith('"')

        zip_contents = _read_zip(resp.content)
        # Manifest at the expected path
        assert ".claude-plugin/marketplace.json" in zip_contents
        manifest = json.loads(zip_contents[".claude-plugin/marketplace.json"])
        assert manifest["name"] == "agnes"
        # `name` is the plugin's authoritative name from plugin.json — the
        # fixture writes plugin.json with name=<plug>, so unprefixed.
        names = {p["name"] for p in manifest["plugins"]}
        assert names == {"plug-x", "plug-y", "plug-z"}
        # source paths stay slug-prefixed so cross-marketplace dirs don't
        # collide on disk in the flat ZIP / git tree layout.
        sources = {p["source"] for p in manifest["plugins"]}
        assert sources == {
            "./plugins/mkt-a-plug-x",
            "./plugins/mkt-b-plug-y",
            "./plugins/mkt-b-plug-z",
        }
        # Files from every marketplace copied over
        assert "plugins/mkt-a-plug-x/CLAUDE.md" in zip_contents
        assert "plugins/mkt-b-plug-y/CLAUDE.md" in zip_contents
        assert "plugins/mkt-b-plug-z/skills/hello.md" in zip_contents
        # plugin.json is included in each plugin tree so Claude Code can
        # resolve the loaded plugin's namespace from it.
        assert "plugins/mkt-a-plug-x/.claude-plugin/plugin.json" in zip_contents

    def test_analyst_zip_contains_only_granted(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace.zip", headers=_auth(marketplace_env["analyst_token"]))
        assert resp.status_code == 200
        zip_contents = _read_zip(resp.content)
        plugin_dirs = {p.split("/")[1] for p in zip_contents if p.startswith("plugins/")}
        assert plugin_dirs == {"mkt-b-plug-y"}

    def test_if_none_match_returns_304(self, marketplace_env):
        c = marketplace_env["client"]
        headers = _auth(marketplace_env["admin_token"])
        first = c.get("/marketplace.zip", headers=headers)
        etag = first.headers["etag"].strip('"')
        second = c.get(
            "/marketplace.zip",
            headers={**headers, "If-None-Match": f'"{etag}"'},
        )
        assert second.status_code == 304
        assert second.headers["etag"].strip('"') == etag
        assert second.content == b""

    def test_etag_changes_when_content_changes(self, marketplace_env):
        from app.marketplace_server.packager import invalidate_etag_cache

        c = marketplace_env["client"]
        headers = _auth(marketplace_env["admin_token"])
        first = c.get("/marketplace.zip", headers=headers)
        etag1 = first.headers["etag"]

        # Mutate a plugin file on disk → etag must change.
        target = marketplace_env["data_dir"] / "marketplaces" / "mkt-a" / "plugins" / "plug-x" / "CLAUDE.md"
        target.write_text("# plug-x\nMUTATED\n", encoding="utf-8")

        # Invalidate the in-process ETag cache so the next request
        # re-hashes from disk instead of returning the stale cached value.
        invalidate_etag_cache()

        second = c.get("/marketplace.zip", headers=headers)
        etag2 = second.headers["etag"]
        assert etag1 != etag2

    def test_missing_auth_returns_401(self, marketplace_env):
        c = marketplace_env["client"]
        resp = c.get("/marketplace.zip")
        assert resp.status_code == 401

    # --- New tests for ETag + auth coverage ---

    def test_zip_returns_correct_content_with_etag_header(self, marketplace_env):
        """GET /marketplace.zip returns ZIP body with a valid ETag header."""
        c = marketplace_env["client"]
        headers = _auth(marketplace_env["admin_token"])
        resp = c.get("/marketplace.zip", headers=headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        etag = resp.headers["etag"]
        assert etag.startswith('"') and etag.endswith('"')
        # ETag is a 16-char hex string (sha256 prefix)
        etag_val = etag.strip('"')
        assert len(etag_val) == 16
        # Body is a valid ZIP
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            assert ".claude-plugin/marketplace.json" in zf.namelist()

    def test_if_none_match_returns_full_content_when_changed(self, marketplace_env):
        """GET /marketplace.zip with a stale If-None-Match returns full content."""
        c = marketplace_env["client"]
        headers = _auth(marketplace_env["admin_token"])
        c.get("/marketplace.zip", headers=headers)  # warm request; only the conditional GET below is asserted
        stale_etag = "0000000000000000"  # definitely wrong
        second = c.get(
            "/marketplace.zip",
            headers={**headers, "If-None-Match": f'"{stale_etag}"'},
        )
        assert second.status_code == 200
        assert len(second.content) > 0
        # The returned ETag is the real one, not the stale one
        assert second.headers["etag"].strip('"') != stale_etag

    def test_zip_requires_pat_authentication(self, marketplace_env):
        """GET /marketplace.zip without any auth returns 401."""
        c = marketplace_env["client"]
        resp = c.get("/marketplace.zip")
        assert resp.status_code == 401

    def test_zip_with_invalid_token_returns_401(self, marketplace_env):
        """GET /marketplace.zip with a garbage Bearer token returns 401."""
        c = marketplace_env["client"]
        resp = c.get("/marketplace.zip", headers={"Authorization": "Bearer invalid-token"})
        assert resp.status_code == 401

    def test_if_none_match_with_wrong_etag_returns_full_zip(self, marketplace_env):
        """If-None-Match with a non-matching ETag returns 200 + full ZIP."""
        c = marketplace_env["client"]
        headers = _auth(marketplace_env["admin_token"])
        resp = c.get(
            "/marketplace.zip",
            headers={**headers, "If-None-Match": '"wrong-etag-value"'},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

    def test_manifest_falls_back_when_plugin_json_missing(self, marketplace_env):
        """If a plugin's .claude-plugin/plugin.json is absent, the synth
        manifest falls back to the upstream marketplace.json's plugin name
        (= mp.name in DB)."""
        from app.marketplace_server.packager import invalidate_etag_cache

        c = marketplace_env["client"]
        # Remove plug-x's plugin.json on disk
        target = (
            marketplace_env["data_dir"]
            / "marketplaces"
            / "mkt-a"
            / "plugins"
            / "plug-x"
            / ".claude-plugin"
            / "plugin.json"
        )
        target.unlink()
        invalidate_etag_cache()

        resp = c.get("/marketplace.zip", headers=_auth(marketplace_env["admin_token"]))
        assert resp.status_code == 200
        zip_contents = _read_zip(resp.content)
        manifest = json.loads(zip_contents[".claude-plugin/marketplace.json"])
        plug_x = next(p for p in manifest["plugins"] if p["source"] == "./plugins/mkt-a-plug-x")
        assert plug_x["name"] == "plug-x"


class TestBuildInfoEtagCache:
    """/marketplace/info goes through the shared ETag TTL cache.

    It used to call ``marketplace_filter.compute_etag`` directly — a SHA-256
    over every plugin byte on disk, per request — while only /marketplace.zip
    was cached. The AI-Connector page polls /marketplace/info for its package
    list, so on an instance with a large plugin every page view re-hashed the
    full content. Both paths now share one cache entry (unit-level test: the
    resolver is stubbed, no app fixture needed).
    """

    def test_build_info_reuses_cached_etag(self, monkeypatch, tmp_path):
        from app.marketplace_server import packager
        from src import marketplace_filter

        packager.invalidate_etag_cache()
        plugins = [
            {
                "marketplace_id": "mkt",
                "marketplace_slug": "mkt",
                "original_name": "demo",
                "prefixed_name": "mkt-demo",
                "manifest_name": "demo",
                "version": "1.0.0",
                "raw": {"name": "demo", "description": "d"},
                "plugin_dir": tmp_path,
                "source": "marketplace",
            }
        ]
        monkeypatch.setattr(marketplace_filter, "resolve_user_marketplace", lambda conn, user: plugins)
        monkeypatch.setattr(marketplace_filter, "resolve_user_groups", lambda conn, user: [])
        calls: list[int] = []
        real_compute = marketplace_filter.compute_etag

        def counting_compute(p):
            calls.append(1)
            return real_compute(p)

        monkeypatch.setattr(marketplace_filter, "compute_etag", counting_compute)

        user = {"id": "u1", "email": "u@example.test"}
        info1 = packager.build_info(None, user)
        info2 = packager.build_info(None, user)
        assert info1["etag"] == info2["etag"]
        assert len(calls) == 1, "second /marketplace/info re-hashed instead of using the cache"

        # /marketplace.zip's etag resolution shares the same cache entry.
        etag, _ = packager.compute_etag_for_user(None, user)
        assert etag == info1["etag"]
        assert len(calls) == 1
        packager.invalidate_etag_cache()


@pytest.fixture
def root_source_env(e2e_env, shared_app):
    """A curated marketplace whose single plugin declares ``source: "./"`` —
    the plugin IS the repo root (the common single-plugin-repo shape). The
    clone carries a ``.git`` dir and Agnes-only enrichment files that must
    never reach the served tree."""
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
    clone = data_dir / "marketplaces" / "solo-mkt"
    (clone / ".claude-plugin").mkdir(parents=True)
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "solo-mkt",
                "plugins": [{"name": "solo", "source": "./", "version": "0.8.0"}],
            }
        ),
        encoding="utf-8",
    )
    (clone / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "solo", "version": "0.8.0"}), encoding="utf-8"
    )
    (clone / ".claude-plugin" / "marketplace-metadata.json").write_text(
        json.dumps({"plugins": {"solo": {"category": "Productivity"}}}),
        encoding="utf-8",
    )
    (clone / "CLAUDE.md").write_text("# solo\n", encoding="utf-8")
    skills = clone / "skills" / "hello"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: hello\n---\nhi", encoding="utf-8")
    (clone / ".git").mkdir()
    (clone / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (clone / ".agnes").mkdir()
    (clone / ".agnes" / "cover.png").write_bytes(b"\x89PNG")
    # An engine launcher the plugin's hooks invoke via ${CLAUDE_PLUGIN_ROOT} —
    # must survive the trip through Agnes still executable.
    engine_bin = clone / "engine" / "bin"
    engine_bin.mkdir(parents=True)
    launcher = engine_bin / "enginectl"
    launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    launcher.chmod(0o755)

    conn = get_system_db()
    try:
        t = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
            ["solo-mkt", "Solo Market", "https://example.test/solo.git", t],
        )
        raw = {"name": "solo", "source": "./", "version": "0.8.0"}
        conn.execute(
            "INSERT INTO marketplace_plugins (marketplace_id, name, version, raw, updated_at) VALUES (?, ?, ?, ?, ?)",
            ["solo-mkt", "solo", "0.8.0", json.dumps(raw), t],
        )

        UserRepository(conn).create(id="rs-user", email="rs@test.local", name="RS")
        gid = UserGroupsRepository(conn).create(name="RSGroup")["id"]
        UserGroupMembersRepository(conn).add_member("rs-user", gid, source="admin")
        ResourceGrantsRepository(conn).create(
            group_id=gid,
            resource_type="marketplace_plugin",
            resource_id="solo-mkt/solo",
        )
        UserCuratedSubscriptionsRepository(conn).subscribe("rs-user", "solo-mkt", "solo")
    finally:
        conn.close()

    return {
        "client": TestClient(shared_app),
        "token": create_access_token("rs-user", "rs@test.local"),
    }


class TestRootSourcePluginServing:
    def test_zip_serves_root_source_plugin_files(self, root_source_env):
        c = root_source_env["client"]
        resp = c.get("/marketplace.zip", headers=_auth(root_source_env["token"]))
        assert resp.status_code == 200
        files = _read_zip(resp.content)

        assert "plugins/solo-mkt-solo/CLAUDE.md" in files
        assert "plugins/solo-mkt-solo/skills/hello/SKILL.md" in files
        assert "plugins/solo-mkt-solo/.claude-plugin/plugin.json" in files

        manifest = json.loads(files[".claude-plugin/marketplace.json"])
        entry = next(p for p in manifest["plugins"] if p["name"] == "solo")
        assert entry["source"] == "./plugins/solo-mkt-solo"

    def test_zip_excludes_git_and_agnes_only_files(self, root_source_env):
        c = root_source_env["client"]
        resp = c.get("/marketplace.zip", headers=_auth(root_source_env["token"]))
        assert resp.status_code == 200
        names = set(_read_zip(resp.content))
        assert not any(".git/" in n for n in names), sorted(names)
        # The top-level `.agnes/version.json` diagnostic is Agnes's own synth
        # file and stays; the PLUGIN's `.agnes/**` and marketplace-metadata.json
        # (which for a root-source plugin live at the clone root) must not be
        # swept into the plugin subtree.
        plugin_files = {n for n in names if n.startswith("plugins/")}
        assert not any(".agnes/" in n for n in plugin_files), sorted(plugin_files)
        assert not any(n.endswith("marketplace-metadata.json") for n in plugin_files), sorted(plugin_files)

    def test_zip_preserves_executable_bit(self, root_source_env):
        c = root_source_env["client"]
        resp = c.get("/marketplace.zip", headers=_auth(root_source_env["token"]))
        assert resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            exec_mode = zf.getinfo("plugins/solo-mkt-solo/engine/bin/enginectl").external_attr >> 16
            assert exec_mode & 0o111, oct(exec_mode)
            doc_mode = zf.getinfo("plugins/solo-mkt-solo/CLAUDE.md").external_attr >> 16
            assert not doc_mode & 0o111, oct(doc_mode)

    def test_member_set_for_a_root_source_plugin_is_pinned(self, root_source_env):
        """Pins exactly what a `source: "./"` plugin ships.

        Notably the upstream `.claude-plugin/marketplace.json` rides along as
        plugin content. That is inert, not a bug: marketplace discovery reads
        `.claude-plugin/marketplace.json` at the ROOT of a registered
        marketplace only (the same convention `src.marketplace.read_plugins`
        follows), and the served root carries Agnes's own synth manifest.
        Plugin identity comes from `.claude-plugin/plugin.json`, which the
        packager sanitizes. See the exclusion list in
        `app/marketplace_server/packager.py::_collect_members`.
        """
        c = root_source_env["client"]
        resp = c.get("/marketplace.zip", headers=_auth(root_source_env["token"]))
        assert resp.status_code == 200
        names = sorted(_read_zip(resp.content))
        assert names == [
            ".agnes/version.json",
            ".claude-plugin/marketplace.json",
            "plugins/solo-mkt-solo/.claude-plugin/marketplace.json",
            "plugins/solo-mkt-solo/.claude-plugin/plugin.json",
            "plugins/solo-mkt-solo/CLAUDE.md",
            "plugins/solo-mkt-solo/engine/bin/enginectl",
            "plugins/solo-mkt-solo/skills/hello/SKILL.md",
        ]
