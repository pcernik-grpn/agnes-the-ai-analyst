"""Tests for app.chat.skills_catalog — the web chat slash-menu source.

Covers the two independent sources (bundled workspace-template skills +
RBAC-filtered marketplace/store plugin skills), the merge/shadowing rule
(marketplace wins name clashes), non-fatal per-source degradation, and the
(currently empty, checked-not-assumed) recognized-commands list.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.chat.skills_catalog import (
    list_bundled_skills,
    list_marketplace_commands,
    list_marketplace_skills,
    merged_commands,
    merged_skills,
)


# ---------------------------------------------------------------------------
# DB fixture + marketplace/store seeding helpers (mirrors
# tests/test_marketplace_filter_store.py's conventions).
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db

    conn = get_system_db()
    yield conn
    conn.close()


def _register_marketplace(conn, *, id: str, plugins: list[dict]) -> None:
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
        [id, id.upper(), f"https://example.test/{id}.git", datetime.now(timezone.utc)],
    )
    for p in plugins:
        conn.execute(
            "INSERT INTO marketplace_plugins (marketplace_id, name, version, raw, updated_at) VALUES (?, ?, ?, ?, ?)",
            [id, p["name"], p.get("version"), json.dumps(p), datetime.now(timezone.utc)],
        )


def _make_user(conn, *, user_id: str, email: str) -> None:
    from src.repositories.users import UserRepository

    UserRepository(conn).create(id=user_id, email=email, name=email.split("@")[0])


def _make_group(conn, *, name: str) -> str:
    from src.repositories.user_groups import UserGroupsRepository

    return UserGroupsRepository(conn).create(name=name)["id"]


def _add_member(conn, *, user_id: str, group_id: str) -> None:
    from src.repositories.user_group_members import UserGroupMembersRepository

    UserGroupMembersRepository(conn).add_member(user_id, group_id, source="admin")


def _grant(conn, *, group_id: str, marketplace: str, plugin: str) -> None:
    from src.repositories.resource_grants import ResourceGrantsRepository

    ResourceGrantsRepository(conn).create(
        group_id=group_id, resource_type="marketplace_plugin", resource_id=f"{marketplace}/{plugin}"
    )


def _subscribe(conn, *, user_id: str, marketplace: str, plugin: str) -> None:
    from src.repositories.user_curated_subscriptions import (
        UserCuratedSubscriptionsRepository,
    )

    UserCuratedSubscriptionsRepository(conn).subscribe(user_id, marketplace, plugin)


def _grant_and_subscribe(conn, *, user_id: str, marketplace: str, plugin: str) -> None:
    gid = _make_group(conn, name=f"G-{marketplace}-{plugin}-{user_id}")
    _grant(conn, group_id=gid, marketplace=marketplace, plugin=plugin)
    _add_member(conn, user_id=user_id, group_id=gid)
    _subscribe(conn, user_id=user_id, marketplace=marketplace, plugin=plugin)


def _write_skill_md(path: Path, *, name: str | None, description: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    if description is not None:
        lines.append(f"description: {description}")
    lines.append("---")
    lines.append("")
    lines.append("Body text.")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# list_bundled_skills
# ---------------------------------------------------------------------------


class TestListBundledSkills:
    def test_reads_name_and_description_from_frontmatter(self, tmp_path):
        template = tmp_path / "bundled"
        _write_skill_md(
            template / ".claude" / "skills" / "connector-asana" / "SKILL.md",
            name="connector-asana",
            description="How to use the Asana connector.",
        )
        out = list_bundled_skills(template)
        assert out == [
            {
                "name": "connector-asana",
                "description": "How to use the Asana connector.",
                "source": "bundled",
            }
        ]

    def test_falls_back_to_directory_name_and_null_description(self, tmp_path):
        template = tmp_path / "bundled"
        _write_skill_md(
            template / ".claude" / "skills" / "no-frontmatter-name" / "SKILL.md",
            name=None,
        )
        out = list_bundled_skills(template)
        assert out == [{"name": "no-frontmatter-name", "description": None, "source": "bundled"}]

    def test_missing_skills_dir_returns_empty_list(self, tmp_path):
        assert list_bundled_skills(tmp_path / "does-not-exist") == []

    def test_ignores_non_directory_and_skillless_entries(self, tmp_path):
        skills_dir = tmp_path / "bundled" / ".claude" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "stray-file.txt").write_text("noise", encoding="utf-8")
        (skills_dir / "empty-dir").mkdir()
        assert list_bundled_skills(tmp_path / "bundled") == []


# ---------------------------------------------------------------------------
# The real bundled template (app/initial_workspace_default) — the extras
# skill is the FIRST bundled skill to ship, so this also confirms the
# "missing .claude/skills dir is normal" branch stops firing once content
# actually lands there.
# ---------------------------------------------------------------------------


def test_extras_skill_bundled(monkeypatch):
    """With the feature ON, the bundled data-apps skill is listed."""
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    from app.chat.skills_catalog import BUNDLED_TEMPLATE_DIR, list_bundled_skills

    entries = list_bundled_skills(BUNDLED_TEMPLATE_DIR)
    names = {s["name"] for s in entries}
    assert "agnes-data-apps-extras" in names
    entry = next(s for s in entries if s["name"] == "agnes-data-apps-extras")
    assert entry["source"] == "bundled"
    assert entry["description"]


def test_a_feature_gated_skill_is_not_offered_when_its_feature_is_off(monkeypatch):
    """Devin Review on #1239: the slash menu advertised a pruned skill.

    This catalog reads the SHIPPED template, not the user's converged
    workspace, so `_prune_disabled_feature_skills` deleting the skill from the
    sandbox changed nothing here — the composer kept offering it, and invoking
    it did nothing because the files were gone. Both now consult one gate.
    """
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "0")
    from app.chat.skills_catalog import BUNDLED_TEMPLATE_DIR, list_bundled_skills

    names = {s["name"] for s in list_bundled_skills(BUNDLED_TEMPLATE_DIR)}
    assert "agnes-data-apps-extras" not in names, "the menu offers a skill this instance does not have"


def test_an_ungated_bundled_skill_is_unaffected(monkeypatch):
    """The gate must subtract only what it names."""
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "0")
    from app.chat.workdir import _FEATURE_GATED_SKILLS, skill_disabled_on_this_instance

    assert skill_disabled_on_this_instance("agnes-data-apps-extras") is True
    assert skill_disabled_on_this_instance("some-other-skill") is False
    assert "some-other-skill" not in _FEATURE_GATED_SKILLS


# ---------------------------------------------------------------------------
# list_marketplace_skills
# ---------------------------------------------------------------------------


class TestListMarketplaceSkills:
    def test_nested_skills_dir_convention(self, db_conn, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "p1"
        _write_skill_md(
            plugin_dir / "skills" / "my-skill" / "SKILL.md",
            name="my-skill",
            description="Does a thing.",
        )

        out = list_marketplace_skills(db_conn, {"id": "u1"})
        assert out == [{"name": "my-skill", "description": "Does a thing.", "source": "marketplace"}]

    def test_root_level_skill_md_convention(self, db_conn, tmp_path, monkeypatch):
        """Single-skill plugins (e.g. the built-in marketplace) ship SKILL.md
        directly at the plugin root, not under skills/<name>/."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "agnes-analyst", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="agnes-analyst")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "agnes-analyst"
        _write_skill_md(plugin_dir / "SKILL.md", name=None)  # no frontmatter name

        out = list_marketplace_skills(db_conn, {"id": "u1"})
        # Falls back to the plugin's own directory name.
        assert out == [{"name": "agnes-analyst", "description": None, "source": "marketplace"}]

    def test_store_bundle_skills_scanned_via_bundle_dirs(self, db_conn, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import uuid

        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.user_store_installs import UserStoreInstallsRepository

        _make_user(db_conn, user_id="owner", email="owner@x")
        _make_user(db_conn, user_id="u1", email="u1@x")
        eid = uuid.uuid4().hex
        StoreEntitiesRepository(db_conn).create(
            id=eid,
            owner_user_id="owner",
            owner_username="owner",
            type="skill",
            name="my-store-skill",
            description="d",
            category=None,
            version="abc1234567890def",
            file_size=10,
            visibility_status="approved",
        )
        UserStoreInstallsRepository(db_conn).install("u1", eid)

        from app.utils import get_store_dir

        plugin_dir = get_store_dir() / eid / "plugin"
        _write_skill_md(
            plugin_dir / "skills" / "my-store-skill" / "SKILL.md",
            name="my-store-skill",
            description="Uploaded via the Store.",
        )

        out = list_marketplace_skills(db_conn, {"id": "u1"})
        assert out == [
            {
                "name": "my-store-skill",
                "description": "Uploaded via the Store.",
                "source": "marketplace",
            }
        ]

    def test_no_grants_yields_empty_list(self, db_conn):
        assert list_marketplace_skills(db_conn, {"id": "nobody"}) == []


# ---------------------------------------------------------------------------
# merged_skills — shadowing + non-fatal degradation
# ---------------------------------------------------------------------------


class TestMergedSkills:
    def test_marketplace_wins_name_clash_with_bundled(self, db_conn, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        template = tmp_path / "bundled"
        _write_skill_md(
            template / ".claude" / "skills" / "shared-name" / "SKILL.md",
            name="shared-name",
            description="Bundled description.",
        )

        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "p1"
        _write_skill_md(
            plugin_dir / "skills" / "shared-name" / "SKILL.md",
            name="shared-name",
            description="Marketplace description.",
        )

        out = merged_skills(template, db_conn, {"id": "u1"})
        assert out == [
            {
                "name": "shared-name",
                "description": "Marketplace description.",
                "source": "marketplace",
            }
        ]

    def test_sorted_by_name_and_both_sources_present(self, db_conn, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        template = tmp_path / "bundled"
        _write_skill_md(template / ".claude" / "skills" / "zzz-bundled" / "SKILL.md", name="zzz-bundled")

        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "p1"
        _write_skill_md(plugin_dir / "skills" / "aaa-market" / "SKILL.md", name="aaa-market")

        out = merged_skills(template, db_conn, {"id": "u1"})
        assert [s["name"] for s in out] == ["aaa-market", "zzz-bundled"]
        assert {s["source"] for s in out} == {"bundled", "marketplace"}

    def test_bundled_source_failure_still_returns_marketplace_skills(self, db_conn, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "p1"
        _write_skill_md(plugin_dir / "skills" / "still-here" / "SKILL.md", name="still-here")

        import app.chat.skills_catalog as mod

        def _boom(_bundled_template_dir):
            raise RuntimeError("bundled source exploded")

        monkeypatch.setattr(mod, "list_bundled_skills", _boom)

        with caplog.at_level("WARNING"):
            out = mod.merged_skills(tmp_path / "irrelevant", db_conn, {"id": "u1"})

        assert out == [{"name": "still-here", "description": None, "source": "marketplace"}]
        assert "bundled source failed to list" in caplog.text

    def test_marketplace_source_failure_still_returns_bundled_skills(self, db_conn, tmp_path, monkeypatch, caplog):
        template = tmp_path / "bundled"
        _write_skill_md(template / ".claude" / "skills" / "still-here" / "SKILL.md", name="still-here")

        import app.chat.skills_catalog as mod

        def _boom(_conn, _user):
            raise RuntimeError("marketplace resolver exploded")

        monkeypatch.setattr(mod, "list_marketplace_skills", _boom)

        with caplog.at_level("WARNING"):
            out = mod.merged_skills(template, db_conn, {"id": "u1"})

        assert out == [{"name": "still-here", "description": None, "source": "bundled"}]
        assert "marketplace source failed to list" in caplog.text


# ---------------------------------------------------------------------------
# marketplace slash commands — the token depends on the delivery mode
# ---------------------------------------------------------------------------


def _write_command_md(path: Path, *, description: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if description is not None:
        lines.append(f"description: {description}")
    lines += ["---", "", "Body."]
    path.write_text("\n".join(lines), encoding="utf-8")


class TestMarketplaceCommands:
    """Verified against the sandboxed CLI (Claude Code 2.1.218): a plugin's
    command is advertised as `/<plugin>:<command>` while a plugin's SKILL is
    advertised bare — so the menu has to namespace one and not the other, and
    only when the plugin was really installed."""

    def _seed(self, db_conn, tmp_path, monkeypatch, *, command: str = "kbl-ship") -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "kbl", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="kbl")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "kbl"
        _write_command_md(plugin_dir / "commands" / f"{command}.md", description="Ship it.")

    def test_plugin_delivery_namespaces_the_command(self, db_conn, tmp_path, monkeypatch):
        from app.chat.skills_catalog import DELIVERY_PLUGIN

        self._seed(db_conn, tmp_path, monkeypatch)

        out = list_marketplace_commands(db_conn, {"id": "u1"}, delivery=DELIVERY_PLUGIN)

        assert out == [{"name": "kbl:kbl-ship", "description": "Ship it.", "source": "marketplace"}]

    def test_flattened_delivery_leaves_the_command_bare(self, db_conn, tmp_path, monkeypatch):
        """kai-agent gets loose project files, where a command has no plugin
        namespace to be reached through."""
        from app.chat.skills_catalog import DELIVERY_PROJECT

        self._seed(db_conn, tmp_path, monkeypatch)

        out = list_marketplace_commands(db_conn, {"id": "u1"}, delivery=DELIVERY_PROJECT)

        assert [c["name"] for c in out] == ["kbl-ship"]

    def test_delivery_none_offers_no_commands(self, db_conn, tmp_path, monkeypatch):
        from app.chat.skills_catalog import DELIVERY_NONE

        self._seed(db_conn, tmp_path, monkeypatch)

        assert merged_commands(db_conn, {"id": "u1"}, delivery=DELIVERY_NONE) == []

    def test_no_plugins_yields_no_commands(self, db_conn):
        assert merged_commands(db_conn, {"id": "nobody"}) == []

    def test_agents_are_delivered_but_never_offered_as_commands(self, db_conn, tmp_path, monkeypatch):
        """An agent is dispatched by the Task tool, so a menu entry for one would
        insert a token nothing resolves."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "kbl", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="kbl")

        from app.utils import get_marketplaces_dir

        plugin_dir = get_marketplaces_dir() / "mkt" / "plugins" / "kbl"
        _write_command_md(plugin_dir / "agents" / "kbl-reviewer.md", description="Reviews things.")

        assert merged_commands(db_conn, {"id": "u1"}) == []


# ---------------------------------------------------------------------------
# Delivery gate + naming contract (#1552)
# ---------------------------------------------------------------------------


class _Cfg:
    def __init__(self, bootstrap_marketplace: bool, provider: str = "e2b"):
        self.bootstrap_marketplace = bootstrap_marketplace
        self.provider = provider


class TestMarketplaceDelivery:
    def test_no_chat_runtime_delivers_nothing(self):
        """No config loaded → nothing to deliver into. Offering the skills anyway
        is the failure mode, so `None` must not resolve to a delivering value."""
        from app.chat.skills_catalog import DELIVERY_NONE, marketplace_delivery

        assert marketplace_delivery(None) == DELIVERY_NONE

    def test_flag_off_delivers_nothing(self):
        from app.chat.skills_catalog import DELIVERY_NONE, marketplace_delivery

        assert marketplace_delivery(_Cfg(False)) == DELIVERY_NONE

    @pytest.mark.parametrize("provider", ["e2b", "docker"])
    def test_a_sandbox_agnes_enters_gets_real_plugins(self, provider):
        """Agnes ships the marketplace as a directory and the sandbox's own CLI
        installs from it offline — so hooks, MCP servers and the `<plugin>:<name>`
        namespace all survive."""
        from app.chat.skills_catalog import DELIVERY_PLUGIN, marketplace_delivery

        assert marketplace_delivery(_Cfg(True, provider)) == DELIVERY_PLUGIN

    def test_the_embedded_engine_gets_flattened_components(self):
        """kai-agent runs the agent in a sandbox Agnes never enters, so a plugin
        install (which writes the CLI's HOME registry) is out of reach; the
        components ride the workspace tarball as loose project files instead."""
        from app.chat.skills_catalog import DELIVERY_PROJECT, marketplace_delivery

        assert marketplace_delivery(_Cfg(True, "kai-agent")) == DELIVERY_PROJECT


class TestMenuNeverOffersAnUndeliveredSkill:
    def test_delivery_none_omits_the_marketplace_source(self, db_conn, tmp_path, monkeypatch):
        """The bug this change exists to remove: with nothing delivering
        marketplace skills, a menu row for one inserts `/name` and the agent
        answers "Unknown command"."""
        from app.chat.skills_catalog import DELIVERY_NONE

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        template = tmp_path / "bundled"
        _write_skill_md(template / ".claude" / "skills" / "bundled-one" / "SKILL.md", name="bundled-one")

        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")

        from app.utils import get_marketplaces_dir

        _write_skill_md(
            get_marketplaces_dir() / "mkt" / "plugins" / "p1" / "skills" / "keboola-cli" / "SKILL.md",
            name="keboola-cli",
        )

        out = merged_skills(template, db_conn, {"id": "u1"}, delivery=DELIVERY_NONE)

        assert [s["name"] for s in out] == ["bundled-one"]

    def test_delivery_project_offers_them(self, db_conn, tmp_path, monkeypatch):
        from app.chat.skills_catalog import DELIVERY_PROJECT

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")

        from app.utils import get_marketplaces_dir

        _write_skill_md(
            get_marketplaces_dir() / "mkt" / "plugins" / "p1" / "skills" / "keboola-cli" / "SKILL.md",
            name="keboola-cli",
        )

        out = merged_skills(tmp_path / "empty", db_conn, {"id": "u1"}, delivery=DELIVERY_PROJECT)

        assert [s["name"] for s in out] == ["keboola-cli"]


class TestSkillNamesStayBare:
    def test_a_marketplace_skill_is_named_without_its_plugin(self, db_conn, tmp_path, monkeypatch):
        """Verified against the sandboxed CLI (Claude Code 2.1.218): a plugin's
        skill is advertised as `{"name": "keboola-cli", "description":
        "(demo-plugin) …"}` — the owning plugin appears in the DESCRIPTION, never
        in the command token. Prefixing the name here (`demo-plugin:keboola-cli`)
        is what would make the menu insert an unknown command, so this locks the
        bare form in."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "demo-plugin", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="demo-plugin")

        from app.utils import get_marketplaces_dir

        _write_skill_md(
            get_marketplaces_dir() / "mkt" / "plugins" / "demo-plugin" / "skills" / "keboola-cli" / "SKILL.md",
            name="keboola-cli",
        )

        out = list_marketplace_skills(db_conn, {"id": "u1"})

        assert [s["name"] for s in out] == ["keboola-cli"]
        assert all(":" not in s["name"] for s in out)


class TestOneWalkFeedsMenuAndDelivery:
    """The invariant that keeps the surfaces from drifting: what the composer
    offers and what the flattened delivery writes come from ONE walk
    (`plugin_skill_entries`). When they were two, a plugin whose SKILL.md sits at
    its root was listed by the menu and skipped by the delivery — a menu entry
    nothing answers to. Found by Devin Review on #1552."""

    def _seed(self, db_conn, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _register_marketplace(db_conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
        _make_user(db_conn, user_id="u1", email="u1@x")
        _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="p1")

        from app.utils import get_marketplaces_dir

        return get_marketplaces_dir() / "mkt" / "plugins" / "p1"

    def test_nested_skills_are_named_and_delivered_alike(self, db_conn, tmp_path, monkeypatch):
        from app.chat.marketplace_payload import materialize_plugin_components

        plugin_dir = self._seed(db_conn, tmp_path, monkeypatch)
        _write_skill_md(plugin_dir / "skills" / "alpha" / "SKILL.md", name="alpha")
        _write_skill_md(plugin_dir / "skills" / "beta" / "SKILL.md", name="beta")

        menu = [s["name"] for s in list_marketplace_skills(db_conn, {"id": "u1"})]
        files, _hooks, _mcp = materialize_plugin_components(db_conn, {"id": "u1"})

        assert menu == ["alpha", "beta"]
        for name in menu:
            assert f".claude/skills/{name}/SKILL.md" in files

    def test_a_root_level_skill_is_delivered_not_just_offered(self, db_conn, tmp_path, monkeypatch):
        """Single-skill plugins (e.g. the built-in marketplace) ship SKILL.md at
        the plugin root. The menu has always found those; the delivery must too."""
        from app.chat.marketplace_payload import materialize_plugin_components

        plugin_dir = self._seed(db_conn, tmp_path, monkeypatch)
        _write_skill_md(plugin_dir / "SKILL.md", name="root-level")

        menu = [s["name"] for s in list_marketplace_skills(db_conn, {"id": "u1"})]
        files, _hooks, _mcp = materialize_plugin_components(db_conn, {"id": "u1"})

        assert menu == ["root-level"]
        assert files[".claude/skills/root-level/SKILL.md"] == plugin_dir / "SKILL.md"

    def test_a_root_level_skill_does_not_drag_the_plugin_in(self, db_conn, tmp_path, monkeypatch):
        """Its "directory" is the whole plugin — for a root-source plugin, the
        whole marketplace clone — so only the SKILL.md itself may travel."""
        from app.chat.marketplace_payload import materialize_plugin_components

        plugin_dir = self._seed(db_conn, tmp_path, monkeypatch)
        _write_skill_md(plugin_dir / "SKILL.md", name="root-level")
        (plugin_dir / "commands").mkdir(parents=True, exist_ok=True)
        (plugin_dir / "commands" / "unrelated.md").write_text("---\n---\nBody.", encoding="utf-8")

        files, _hooks, _mcp = materialize_plugin_components(db_conn, {"id": "u1"})

        skill_members = [k for k in files if k.startswith(".claude/skills/root-level/")]
        assert skill_members == [".claude/skills/root-level/SKILL.md"]
        # The command is still delivered — as a command, in its own place.
        assert ".claude/commands/unrelated.md" in files

    def test_delivery_folder_follows_the_frontmatter_not_the_source_folder(self, db_conn, tmp_path, monkeypatch):
        """Verified against the CLI: a skill in `folder-name/` whose frontmatter
        says `frontmatter-name` is invoked as `/frontmatter-name`. So the folder
        it is delivered INTO must carry the frontmatter name, or the offered token
        and the delivered directory disagree. Found by Devin Review on #1552."""
        from app.chat.marketplace_payload import materialize_plugin_components

        plugin_dir = self._seed(db_conn, tmp_path, monkeypatch)
        _write_skill_md(plugin_dir / "skills" / "folder-name" / "SKILL.md", name="frontmatter-name")

        menu = [s["name"] for s in list_marketplace_skills(db_conn, {"id": "u1"})]
        files, _hooks, _mcp = materialize_plugin_components(db_conn, {"id": "u1"})

        assert menu == ["frontmatter-name"]
        assert ".claude/skills/frontmatter-name/SKILL.md" in files
        assert not any(k.startswith(".claude/skills/folder-name/") for k in files)

    def test_supporting_files_travel_under_the_new_name(self, db_conn, tmp_path, monkeypatch):
        from app.chat.marketplace_payload import materialize_plugin_components

        plugin_dir = self._seed(db_conn, tmp_path, monkeypatch)
        _write_skill_md(plugin_dir / "skills" / "folder-name" / "SKILL.md", name="frontmatter-name")
        (plugin_dir / "skills" / "folder-name" / "references").mkdir(parents=True, exist_ok=True)
        (plugin_dir / "skills" / "folder-name" / "references" / "deep.md").write_text("detail", encoding="utf-8")

        files, _hooks, _mcp = materialize_plugin_components(db_conn, {"id": "u1"})

        assert ".claude/skills/frontmatter-name/references/deep.md" in files
