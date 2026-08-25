"""Tests for app.chat.marketplace_payload — the caller's stack as files.

Two shapes, one content source (#1552):

- ``export_marketplace_tree`` writes the filtered marketplace as a directory,
  which Claude Code registers and installs from offline. This is what makes a
  stack plugin a REAL plugin in an e2b/docker sandbox, with its agents, slash
  commands, hooks and MCP servers intact.
- ``materialize_plugin_components`` flattens the same plugins into project-scope
  files, for the kai-agent provider whose sandbox Agnes never enters.

The fixtures mirror ``tests/test_chat_skills_catalog.py``'s conventions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.chat.marketplace_payload import (
    export_marketplace_tree,
    materialize_plugin_components,
)


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


def _grant_and_subscribe(conn, *, user_id: str, marketplace: str, plugin: str) -> None:
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_curated_subscriptions import UserCuratedSubscriptionsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    try:
        UserRepository(conn).create(id=user_id, email=f"{user_id}@x", name=user_id)
    except Exception:
        pass
    gid = UserGroupsRepository(conn).create(name=f"G-{marketplace}-{plugin}")["id"]
    UserGroupMembersRepository(conn).add_member(user_id, gid, source="admin")
    ResourceGrantsRepository(conn).create(
        group_id=gid, resource_type="marketplace_plugin", resource_id=f"{marketplace}/{plugin}"
    )
    UserCuratedSubscriptionsRepository(conn).subscribe(user_id, marketplace, plugin)


def _full_plugin(root: Path, name: str) -> Path:
    """A plugin shipping one of every component type."""
    d = root / name
    (d / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (d / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "description": "probe"}), encoding="utf-8"
    )
    (d / "skills" / "keboola-cli").mkdir(parents=True, exist_ok=True)
    (d / "skills" / "keboola-cli" / "SKILL.md").write_text(
        "---\nname: keboola-cli\ndescription: Skill.\n---\n\nBody.", encoding="utf-8"
    )
    (d / "skills" / "keboola-cli" / "references").mkdir(parents=True, exist_ok=True)
    (d / "skills" / "keboola-cli" / "references" / "deep.md").write_text("detail", encoding="utf-8")
    (d / "agents").mkdir(parents=True, exist_ok=True)
    (d / "agents" / "kbl-reviewer.md").write_text(
        "---\nname: kbl-reviewer\ndescription: Agent.\n---\n\nBody.", encoding="utf-8"
    )
    (d / "commands").mkdir(parents=True, exist_ok=True)
    (d / "commands" / "kbl-ship.md").write_text("---\ndescription: Command.\n---\n\nBody.", encoding="utf-8")
    (d / "hooks").mkdir(parents=True, exist_ok=True)
    (d / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "true"}]}]}}),
        encoding="utf-8",
    )
    (d / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"probe-mcp": {"command": "echo", "args": ["noop"]}}}), encoding="utf-8"
    )
    return d


@pytest.fixture
def stacked(db_conn, tmp_path, monkeypatch):
    """One granted-and-subscribed plugin with every component type on disk."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _register_marketplace(db_conn, id="mkt", plugins=[{"name": "kbl", "version": "1.0.0"}])
    _grant_and_subscribe(db_conn, user_id="u1", marketplace="mkt", plugin="kbl")

    from app.utils import get_marketplaces_dir

    _full_plugin(get_marketplaces_dir() / "mkt" / "plugins", "kbl")
    return db_conn, {"id": "u1"}


# ---------------------------------------------------------------------------
# export_marketplace_tree — the shape a sandbox installs from
# ---------------------------------------------------------------------------


class TestExportMarketplaceTree:
    def test_writes_an_installable_marketplace(self, stacked, tmp_path):
        """`claude plugin marketplace add <dir>` needs the manifest at the root
        and the plugin under the path it declares; without both, the offline
        install the runner performs cannot resolve the plugin."""
        conn, user = stacked
        dest = tmp_path / "tree"

        names = export_marketplace_tree(conn, user, dest)

        assert names == ["kbl"]
        manifest = json.loads((dest / ".claude-plugin" / "marketplace.json").read_text())
        assert [p["name"] for p in manifest["plugins"]] == ["kbl"]
        declared = manifest["plugins"][0]["source"].lstrip("./")
        assert (dest / declared / ".claude-plugin" / "plugin.json").is_file()

    def test_every_component_type_travels(self, stacked, tmp_path):
        """The regression this whole change is about: skills alone is not a
        plugin. Agents, commands, hooks and MCP servers are the product."""
        conn, user = stacked
        dest = tmp_path / "tree"

        export_marketplace_tree(conn, user, dest)

        # The on-disk directory is slug-prefixed (`<marketplace>-<plugin>`) so
        # two marketplaces shipping the same plugin name cannot collide; the
        # manifest's own `source` is the contract, so read it rather than
        # hardcoding the layout.
        manifest = json.loads((dest / ".claude-plugin" / "marketplace.json").read_text())
        plugin = dest / manifest["plugins"][0]["source"].lstrip("./")
        assert (plugin / "skills" / "keboola-cli" / "SKILL.md").is_file()
        assert (plugin / "skills" / "keboola-cli" / "references" / "deep.md").is_file()
        assert (plugin / "agents" / "kbl-reviewer.md").is_file()
        assert (plugin / "commands" / "kbl-ship.md").is_file()
        assert (plugin / "hooks" / "hooks.json").is_file()
        assert (plugin / ".mcp.json").is_file()

    def test_an_empty_stack_removes_a_previous_tree(self, db_conn, tmp_path):
        """A user who unsubscribed from everything must not keep yesterday's
        marketplace — and an empty manifest is not registrable anyway."""
        dest = tmp_path / "tree"
        dest.mkdir()
        (dest / "stale.txt").write_text("old", encoding="utf-8")

        assert export_marketplace_tree(db_conn, {"id": "nobody"}, dest) == []
        assert not dest.exists()

    def test_a_dropped_plugin_disappears(self, stacked, tmp_path, monkeypatch):
        """The tree is replaced, not merged: reconciling inside the old layout is
        how a plugin the user no longer has survives in their sandbox."""
        conn, user = stacked
        dest = tmp_path / "tree"
        export_marketplace_tree(conn, user, dest)
        assert list((dest / "plugins").iterdir()), "no plugin was written"

        from src.repositories.user_curated_subscriptions import UserCuratedSubscriptionsRepository

        UserCuratedSubscriptionsRepository(conn).unsubscribe("u1", "mkt", "kbl")

        assert export_marketplace_tree(conn, user, dest) == []
        assert not dest.exists()

    def test_the_export_is_atomic(self, stacked, tmp_path):
        """A session spawning mid-write must see the whole old tree or the whole
        new one — never a manifest without its plugins."""
        conn, user = stacked
        dest = tmp_path / "tree"

        export_marketplace_tree(conn, user, dest)
        export_marketplace_tree(conn, user, dest)  # rewrite over an existing tree

        manifest = json.loads((dest / ".claude-plugin" / "marketplace.json").read_text())
        plugin = dest / manifest["plugins"][0]["source"].lstrip("./")
        assert (plugin / "skills" / "keboola-cli" / "SKILL.md").is_file()
        assert not list(dest.parent.glob(".*.staging")), "staging directory left behind"


# ---------------------------------------------------------------------------
# materialize_plugin_components — the shape the embedded engine gets
# ---------------------------------------------------------------------------


class TestMaterializePluginComponents:
    def test_all_three_component_dirs_are_flattened(self, stacked):
        conn, user = stacked

        files, _hooks, _mcp = materialize_plugin_components(conn, user)

        assert ".claude/skills/keboola-cli/SKILL.md" in files
        assert ".claude/skills/keboola-cli/references/deep.md" in files
        assert ".claude/agents/kbl-reviewer.md" in files
        assert ".claude/commands/kbl-ship.md" in files

    def test_hooks_and_mcp_servers_are_merged(self, stacked):
        """These have no installed plugin to live in, so they have to become
        project-scope config or they simply do not exist for the agent."""
        conn, user = stacked

        _files, hooks, mcp = materialize_plugin_components(conn, user)

        assert "PreToolUse" in hooks
        assert "probe-mcp" in mcp

    def test_a_hook_needing_a_plugin_root_is_dropped(self, stacked, tmp_path):
        """`${CLAUDE_PLUGIN_ROOT}` cannot resolve in the flattened shape.
        Shipping such a hook anyway would fail at tool-call time, in the middle
        of someone's turn, instead of here."""
        conn, user = stacked

        from app.utils import get_marketplaces_dir

        hooks_json = get_marketplaces_dir() / "mkt" / "plugins" / "kbl" / "hooks" / "hooks.json"
        hooks_json.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [{"hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/bin/gate"}]}],
                        "PostToolUse": [{"hooks": [{"type": "command", "command": "true"}]}],
                    }
                }
            ),
            encoding="utf-8",
        )

        _files, hooks, _mcp = materialize_plugin_components(conn, user)

        assert "PreToolUse" not in hooks
        assert "PostToolUse" in hooks

    def test_a_malformed_plugin_does_not_cost_the_others(self, stacked, tmp_path, monkeypatch):
        conn, user = stacked

        from app.utils import get_marketplaces_dir

        (get_marketplaces_dir() / "mkt" / "plugins" / "kbl" / ".mcp.json").write_text("{not json", encoding="utf-8")

        files, hooks, mcp = materialize_plugin_components(conn, user)

        assert ".claude/skills/keboola-cli/SKILL.md" in files
        assert "PreToolUse" in hooks
        assert mcp == {}

    def test_an_empty_stack_materializes_nothing(self, db_conn):
        assert materialize_plugin_components(db_conn, {"id": "nobody"}) == ({}, {}, {})
