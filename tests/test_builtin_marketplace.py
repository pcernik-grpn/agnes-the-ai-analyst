"""Tests for the built-in marketplace seeding mechanism (v77).

Covers:
- seed_builtin_marketplace() is idempotent (safe to call on every boot).
- The registry row has is_builtin=TRUE.
- The plugin cache is populated after seeding.
- is_builtin rows are skipped by sync_marketplaces().
- admin_disabled filter works end-to-end through list_granted_for_groups.
- The bundled content directory exists and has the expected structure.
"""

from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# Bundled content structure
# ---------------------------------------------------------------------------


def test_builtin_content_dir_exists():
    """The bundled _builtin_marketplace/ tree ships in the package."""
    from src.marketplace import _BUILTIN_CONTENT_DIR

    assert _BUILTIN_CONTENT_DIR.is_dir(), f"Bundled content missing at {_BUILTIN_CONTENT_DIR}"


def test_builtin_marketplace_json_present():
    """The root marketplace.json manifest is present and parsable."""
    import json

    from src.marketplace import _BUILTIN_CONTENT_DIR, PLUGIN_MANIFEST_REL

    manifest = _BUILTIN_CONTENT_DIR / PLUGIN_MANIFEST_REL
    assert manifest.is_file(), f"marketplace.json missing at {manifest}"
    data = json.loads(manifest.read_text())
    assert isinstance(data, dict)
    plugins = data.get("plugins")
    assert isinstance(plugins, list) and len(plugins) >= 2, (
        "marketplace.json must list at least agnes-analyst and agnes-operator"
    )
    names = {p["name"] for p in plugins}
    assert "agnes-analyst" in names
    assert "agnes-operator" in names


def test_builtin_plugin_dirs_exist():
    """Both plugin directories are present under plugins/.

    The SKILL.md must live at ``skills/<name>/SKILL.md`` — the canonical
    Claude Code plugin layout, and the only one both Claude Code and
    ``list_inner_skills`` discover. A SKILL.md at the plugin root loads
    nowhere (see test_builtin_plugin_skills_are_discoverable).
    """
    from src.marketplace import _BUILTIN_CONTENT_DIR

    for slug in ("agnes-analyst", "agnes-operator"):
        plugin_dir = _BUILTIN_CONTENT_DIR / "plugins" / slug
        assert plugin_dir.is_dir(), f"Plugin dir missing: {plugin_dir}"
        plugin_json = plugin_dir / ".claude-plugin" / "plugin.json"
        assert plugin_json.is_file(), f"plugin.json missing: {plugin_json}"
        skill_md = plugin_dir / "skills" / slug / "SKILL.md"
        assert skill_md.is_file(), f"SKILL.md missing: {skill_md}"
        assert not (plugin_dir / "SKILL.md").exists(), (
            f"SKILL.md must not sit at the plugin root: {plugin_dir / 'SKILL.md'}"
        )


def test_builtin_plugin_skills_are_discoverable():
    """The bundled plugins expose their skill through the same enumeration
    the marketplace listing and the served feed use.

    Regression guard: both plugins shipped their SKILL.md at the plugin root,
    so list_inner_skills() returned [] — the plugins installed fine but
    contributed no skill to any surface.
    """
    from src.marketplace import _BUILTIN_CONTENT_DIR
    from src.marketplace_listing import list_inner_skills

    expected = {
        # agnes-web-guide is the marketplace mirror of the bundled chat skill;
        # tests/test_web_guide_skill_sync.py pins it byte-identical.
        "agnes-analyst": ["agnes-analyst", "agnes-web-guide"],
        "agnes-operator": ["agnes-operator"],
    }
    for slug, skills in expected.items():
        plugin_dir = _BUILTIN_CONTENT_DIR / "plugins" / slug
        assert list_inner_skills(plugin_dir) == skills


# ---------------------------------------------------------------------------
# Seeding (DuckDB, isolated in-memory)
# ---------------------------------------------------------------------------


def _setup_duckdb_repos(tmp_path: Path):
    """Bootstrap a fresh DuckDB system DB and return repo factories."""
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    db_path = str(tmp_path / "system.duckdb")
    conn = _open_duckdb(db_path)
    _ensure_schema(conn)
    return conn


def test_seed_builtin_marketplace_idempotent(tmp_path, monkeypatch):
    """seed_builtin_marketplace() is safe to call multiple times — the registry
    row, plugin cache, and RBAC grants are all upsert/idempotent."""
    conn = _setup_duckdb_repos(tmp_path)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Route all repos to this single DuckDB connection.
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from src.marketplace import seed_builtin_marketplace, BUILTIN_MARKETPLACE_SLUG

    seed_builtin_marketplace()
    seed_builtin_marketplace()  # second call must not raise or duplicate

    reg_rows = conn.execute(
        "SELECT id, is_builtin, curator_name FROM marketplace_registry WHERE id = ?",
        [BUILTIN_MARKETPLACE_SLUG],
    ).fetchall()
    assert len(reg_rows) == 1, "Registry row must be exactly one after two seed calls"
    assert reg_rows[0][1] is True, "is_builtin must be TRUE"
    # Owner/attribution: the built-in marketplace is curated by the platform
    # itself, surfaced as "Agnes" in the admin/browse UI.
    assert reg_rows[0][2] == "Agnes", "built-in marketplace must be owned/curated by 'Agnes'"

    conn.close()


def test_seed_builtin_marketplace_clears_stale_last_error(tmp_path, monkeypatch):
    """A last_error stamped by the old pre-fix sync path (which tried to
    git-clone the builtin:// sentinel) must self-heal on the next re-seed —
    there is no other way to clear it: the admin UI hides "Sync now" for
    is_builtin rows, and sync_one() now refuses built-in rows before ever
    touching last_error again."""
    conn = _setup_duckdb_repos(tmp_path)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from src.marketplace import seed_builtin_marketplace, BUILTIN_MARKETPLACE_SLUG
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    seed_builtin_marketplace()

    reg = MarketplaceRegistryRepository(conn)
    reg.update_sync_status(
        BUILTIN_MARKETPLACE_SLUG,
        error="git clone failed: 'remote-builtin' is not a git command",
    )
    row = reg.get(BUILTIN_MARKETPLACE_SLUG)
    assert row["last_error"] is not None, "precondition: error stamped"

    # Simulate a reboot: seed_builtin_marketplace() re-registers the row.
    seed_builtin_marketplace()

    row = reg.get(BUILTIN_MARKETPLACE_SLUG)
    assert row["last_error"] is None, "stale error on a built-in row must self-heal on re-seed"

    conn.close()


def test_register_does_not_clear_last_error_for_non_builtin(tmp_path, monkeypatch):
    """Regression guard: the self-heal is scoped to is_builtin. Re-registering
    (e.g. an admin edit/PATCH) a normal marketplace must leave a real
    last_error untouched."""
    conn = _setup_duckdb_repos(tmp_path)
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository

    reg = MarketplaceRegistryRepository(conn)
    reg.register(id="normal-heal", name="Normal", url="https://example.test/normal.git")
    reg.update_sync_status("normal-heal", error="real git clone failure")
    row = reg.get("normal-heal")
    assert row["last_error"] == "real git clone failure"

    # Re-register, as an admin edit (PATCH) would.
    reg.register(id="normal-heal", name="Normal Updated", url="https://example.test/normal.git")

    row = reg.get("normal-heal")
    assert row["name"] == "Normal Updated"
    assert row["last_error"] == "real git clone failure", (
        "re-registering a non-builtin row must not clear a real last_error"
    )

    conn.close()


def test_seed_builtin_marketplace_populates_plugin_cache(tmp_path, monkeypatch):
    """After seeding, marketplace_plugins has rows for the built-in plugins."""
    conn = _setup_duckdb_repos(tmp_path)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from src.marketplace import seed_builtin_marketplace, BUILTIN_MARKETPLACE_SLUG

    seed_builtin_marketplace()

    rows = conn.execute(
        "SELECT name FROM marketplace_plugins WHERE marketplace_id = ? ORDER BY name",
        [BUILTIN_MARKETPLACE_SLUG],
    ).fetchall()
    names = {r[0] for r in rows}
    assert "agnes-analyst" in names
    assert "agnes-operator" in names

    conn.close()


def test_seed_builtin_marketplace_seeds_rbac_grants(tmp_path, monkeypatch):
    """After seeding, resource_grants exist for Everyone→analyst and Admin→operator."""
    conn = _setup_duckdb_repos(tmp_path)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from src.marketplace import seed_builtin_marketplace, BUILTIN_MARKETPLACE_SLUG

    seed_builtin_marketplace()

    slug = BUILTIN_MARKETPLACE_SLUG
    expected = {
        f"{slug}/agnes-analyst": "Everyone",
        f"{slug}/agnes-operator": "Admin",
    }
    for resource_id, group_name in expected.items():
        row = conn.execute(
            """SELECT rg.id FROM resource_grants rg
               JOIN user_groups ug ON ug.id = rg.group_id
               WHERE rg.resource_type = 'marketplace_plugin'
                 AND rg.resource_id = ?
                 AND ug.name = ?""",
            [resource_id, group_name],
        ).fetchone()
        assert row is not None, f"Missing grant: {group_name} -> {resource_id}"

    conn.close()


# ---------------------------------------------------------------------------
# sync_marketplaces skips is_builtin rows
# ---------------------------------------------------------------------------


def test_sync_marketplaces_skips_builtin(tmp_path, monkeypatch):
    """sync_marketplaces() must not attempt to git-clone is_builtin rows."""
    conn = _setup_duckdb_repos(tmp_path)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    # Insert a built-in row directly.
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url, is_builtin) "
        "VALUES ('agnes-builtin', 'Built-in', 'builtin://agnes-builtin', TRUE)"
    )
    # Insert a normal (admin-registered) row.
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url, is_builtin) "
        "VALUES ('normal-mkt', 'Normal', 'https://example.test/normal.git', FALSE)"
    )

    synced_ids: list[str] = []

    def fake_sync_spec(spec):
        synced_ids.append(spec["id"])
        raise ValueError("fake-abort")  # prevent actual git ops

    monkeypatch.setattr("src.marketplace._sync_spec", fake_sync_spec)

    from src.marketplace import sync_marketplaces

    sync_marketplaces()

    assert "agnes-builtin" not in synced_ids, "sync_marketplaces must skip is_builtin rows"
    # The normal row was attempted (and failed with our fake abort).
    assert "normal-mkt" in synced_ids

    conn.close()


# ---------------------------------------------------------------------------
# sync_one refuses is_builtin rows
# ---------------------------------------------------------------------------


def test_sync_one_refuses_builtin_and_leaves_content_intact(tmp_path, monkeypatch):
    """`sync_one()` must refuse a built-in row before touching anything.

    `sync_marketplaces()` filtered built-in rows out from the start, but the
    per-row path (admin "Sync now" button, `agnes admin marketplace sync`) did
    not. It handed the `builtin://` sentinel to `_sync_spec`, whose clone
    branch **rmtree's the target directory first** — the baked tree has no
    `.git`, so `is_git` is False — and only then ran a `git clone` that could
    never succeed (`git: 'remote-builtin' is not a git command`). Net effect of
    one click: the seeded content was deleted and the row stamped with a
    `last_error` the nightly sync never clears.
    """
    import pytest

    conn = _setup_duckdb_repos(tmp_path)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from app.utils import get_marketplaces_dir
    from src.marketplace import (
        BUILTIN_MARKETPLACE_SLUG,
        MarketplaceNotSyncable,
        seed_builtin_marketplace,
        sync_one,
    )

    seed_builtin_marketplace()
    baked = get_marketplaces_dir() / BUILTIN_MARKETPLACE_SLUG
    assert baked.is_dir(), "precondition: seeding baked the bundled content"

    def fail_if_called(spec):  # pragma: no cover - must never run
        raise AssertionError(f"_sync_spec must not run for a built-in row: {spec['id']}")

    monkeypatch.setattr("src.marketplace._sync_spec", fail_if_called)

    with pytest.raises(MarketplaceNotSyncable):
        sync_one(BUILTIN_MARKETPLACE_SLUG)

    # Content survives — this is the destructive half of the bug.
    assert baked.is_dir(), "refused sync must not delete the baked content"
    from src.marketplace import PLUGIN_MANIFEST_REL

    assert (baked / PLUGIN_MANIFEST_REL).is_file(), "manifest must survive a refused sync"

    # And the row is untouched: no last_error to leave the marketplace
    # permanently red in the admin table / "error" in marketplace health.
    row = conn.execute(
        "SELECT last_error, last_synced_at FROM marketplace_registry WHERE id = ?",
        [BUILTIN_MARKETPLACE_SLUG],
    ).fetchone()
    assert row[0] is None, f"refused sync must not stamp last_error (got {row[0]!r})"
    assert row[1] is None, "refused sync must not stamp last_synced_at"

    conn.close()


def test_sync_one_still_syncs_normal_rows(tmp_path, monkeypatch):
    """The guard is scoped to is_builtin — admin-registered rows still sync."""
    conn = _setup_duckdb_repos(tmp_path)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url, is_builtin) "
        "VALUES ('normal-mkt', 'Normal', 'https://example.test/normal.git', FALSE)"
    )

    called: list[str] = []

    def fake_sync_spec(spec):
        called.append(spec["id"])
        return {"id": spec["id"], "name": spec["id"], "action": "clone", "commit": "a" * 40, "path": "/tmp/x"}

    monkeypatch.setattr("src.marketplace._sync_spec", fake_sync_spec)
    monkeypatch.setattr("src.marketplace._refresh_plugin_cache", lambda slug, commit_sha=None: 0)

    from src.marketplace import sync_one

    result = sync_one("normal-mkt")

    assert called == ["normal-mkt"]
    assert result["commit"] == "a" * 40

    conn.close()


def test_boot_seed_clears_a_stale_sync_error(tmp_path, monkeypatch):
    """TCRD-219: before sync_one() learned to refuse built-in rows, one
    "Sync now" click stamped a `last_error` that nothing ever cleared — the
    nightly sync skips built-in rows, so it never ran, never succeeded, and
    never removed the stamp. The boot re-seed is the honest place to clear
    it: for the built-in row the content is re-baked from the bundle in the
    same breath, so any previous sync error is stale by construction."""
    conn = _setup_duckdb_repos(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from src.marketplace import seed_builtin_marketplace, BUILTIN_MARKETPLACE_SLUG
    from src.repositories import marketplace_registry_repo

    seed_builtin_marketplace()
    # The pre-fix wound: a manual sync attempt stamped its failure.
    marketplace_registry_repo().update_sync_status(
        BUILTIN_MARKETPLACE_SLUG, error="fatal: 'builtin' helper not found"
    )
    assert (
        marketplace_registry_repo().get(BUILTIN_MARKETPLACE_SLUG)["last_error"] is not None
    ), "precondition: the stale stamp is in place"

    seed_builtin_marketplace()

    assert (
        marketplace_registry_repo().get(BUILTIN_MARKETPLACE_SLUG)["last_error"] is None
    ), "boot re-seed must clear the stale sync error it makes moot"
    conn.close()


def test_contributed_registry_row_seed_clears_a_stale_sync_error(tmp_path, monkeypatch):
    """Same wound, other bundled row. The contributed marketplace's registry
    row is (re-)asserted whenever a skill is contributed; a stale sync error
    on it is exactly as non-actionable — after the sync_one() guard, no sync
    can ever run against it again, so the stamp could otherwise never clear."""
    conn = _setup_duckdb_repos(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from src.skill_contribution import _ensure_registry_row, CONTRIBUTED_MARKETPLACE_SLUG
    from src.repositories import marketplace_registry_repo

    _ensure_registry_row(registered_by="test")
    marketplace_registry_repo().update_sync_status(
        CONTRIBUTED_MARKETPLACE_SLUG, error="fatal: 'builtin' helper not found"
    )

    _ensure_registry_row(registered_by="test")

    assert (
        marketplace_registry_repo().get(CONTRIBUTED_MARKETPLACE_SLUG)["last_error"] is None
    ), "re-asserting the contributed registry row must clear the stale sync error"
    conn.close()


def test_admin_table_shows_no_sync_state_for_builtin_rows():
    """The sync-state cell renders `failed <date>` / `never` from
    last_error/last_synced_at — states a bundled row can never leave, since
    nothing syncs it. The cell must branch on is_builtin before it reads
    either field, the same way the URL cell already does."""
    template = Path("app/web/templates/admin_marketplaces.html").read_text(encoding="utf-8")
    # The lastSync const must consult is_builtin before last_error.
    last_sync_def = template.split("const lastSync")[1].split(";")[0]
    assert "is_builtin" in last_sync_def, (
        "the sync-state cell must branch on m.is_builtin — a bundled row "
        "otherwise shows 'failed'/'never' for a sync that can never run"
    )


def test_hub_sync_signal_ignores_bundled_rows(tmp_path, monkeypatch):
    """Live-run find (TCRD-219 family): the /admin hub's "Marketplace sync"
    signal counted bundled rows as broken — they never sync, so
    `last_synced_at` is NULL forever and every instance showed a permanent
    "Needs fixing" row for content that ships inside the image."""
    conn = _setup_duckdb_repos(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from src.repositories import marketplace_registry_repo

    marketplace_registry_repo().register(
        id="agnes-builtin", name="Agnes Built-in", url="builtin://agnes-builtin", is_builtin=True
    )

    from app.web.admin_signals import _resolve_marketplace_sync

    assert _resolve_marketplace_sync() is None, (
        "a bundled row must not read as a stale sync — it has no git remote to sync from"
    )

    # A real registered marketplace that never synced still counts.
    marketplace_registry_repo().register(id="real-mp", name="Real", url="https://example.test/r.git")
    sig = _resolve_marketplace_sync()
    assert sig is not None and sig.count == 1
    conn.close()
