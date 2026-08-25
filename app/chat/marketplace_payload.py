"""The caller's RBAC-filtered marketplace, as files a chat session can load.

A plugin is not just its skills: it ships agents, slash commands, hooks and MCP
servers, and the whole point of the marketplace is that a user's stack reaches
their agent. This module produces the two shapes that make that happen, from the
one content builder the analyst-facing channels already use
(``app.marketplace_server.packager._collect_members`` — the same bytes
``agnes refresh-marketplace`` downloads), so no surface ships a different
marketplace than another.

**Shape 1 — the marketplace tree** (:func:`export_marketplace_tree`). The
filtered marketplace written to disk exactly as the served ZIP lays it out
(``.claude-plugin/marketplace.json`` + ``plugins/<prefixed_name>/…``). Claude
Code registers a plain directory as a marketplace and installs from it entirely
offline (``claude plugin marketplace add <dir>`` + ``claude plugin install
<name>@agnes --scope project``), which is what the e2b/docker sandbox does with
it. That is the faithful delivery: real plugins, so hooks keep their
``${CLAUDE_PLUGIN_ROOT}``, MCP servers register as ``plugin:<plugin>:<server>``,
and agents/commands keep the ``<plugin>:<name>`` namespace the usage-event
attribution in ``docs/PLATFORM_SETUP.md`` reads.

**Shape 2 — loose components** (:func:`materialize_plugin_components`). The same
plugins flattened into project-scope files — ``.claude/skills/<name>/``,
``.claude/agents/<name>.md``, ``.claude/commands/<name>.md``, plus merged
``hooks`` and ``.mcp.json`` entries. For the ``kai-agent`` provider, whose
sandbox Agnes never enters: the workspace tarball is the only project scope we
control there, so a real plugin install (which writes state into the CLI's HOME)
is impossible and this is the best faithful alternative. Every component type
still reaches the agent; what is lost is the plugin namespace, which is why
``app.chat.skills_catalog`` reports different invocation tokens per delivery
mode.

Both shapes are best-effort at the callsite, never here: this module raises, and
each caller decides that a marketplace failure must not cost someone their
session.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Iterable, Optional

import duckdb

logger = logging.getLogger(__name__)

#: Where the marketplace tree lands inside a chat workspace. Under ``.claude``
#: so it travels with the tree the sandbox already mounts, and so
#: ``WorkdirManager.purge_user`` / a template reinit clean it up for free.
MARKETPLACE_TREE_SUBDIR = ".claude/agnes-marketplace"

#: The marketplace name plugins are installed under (``<plugin>@<this>``). Must
#: match what the exported ``marketplace.json`` declares — it is the same
#: ``MARKETPLACE_NAME`` the served manifest carries, so a sandbox and a laptop
#: name the same plugin identically.
MARKETPLACE_NAME = "agnes"


def _resolved_plugins(conn: duckdb.DuckDBPyConnection, user: dict) -> list[dict]:
    from src.marketplace_filter import resolve_user_marketplace

    return resolve_user_marketplace(conn, user)


def export_marketplace_tree(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    dest: Path,
    *,
    plugins: Optional[list[dict]] = None,
) -> list[str]:
    """Write the caller's filtered marketplace under ``dest``; return plugin names.

    The returned names are ``manifest_name`` values — the plugin identity Claude
    Code uses, so a caller can build the ``<name>@agnes`` refs
    ``claude plugin install`` expects.

    ``dest`` is replaced, not merged: a plugin dropped from the stack has to
    disappear from the tree, and reconciling file-by-file inside someone else's
    layout is how a stale plugin survives. The write goes to a sibling temp
    directory and is swapped in, so a session spawning while this runs sees
    either the old tree or the new one, never half of one.

    Returns ``[]`` and writes nothing when the caller's stack is empty (an empty
    marketplace is not worth registering, and Claude Code would reject a
    manifest with no plugins).
    """
    from app.marketplace_server.packager import _collect_members, compute_etag_for_user

    if plugins is None:
        etag, plugins = compute_etag_for_user(conn, user)
    else:
        etag, _ = compute_etag_for_user(conn, user)

    if not plugins:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        return []

    staging = dest.parent / f".{dest.name}.staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    for arcname, payload, executable in _collect_members(plugins, etag):
        target = staging / arcname
        # `_collect_members` builds every arcname from `prefixed_name` +
        # marketplace-relative paths that `escapes_base` already contained, but
        # this writes to the filesystem rather than into a zip entry — so the
        # containment is re-checked here rather than trusted across the hop.
        if not _within(target, staging):
            logger.warning("marketplace payload: refusing out-of-tree member %r", arcname)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        if executable:
            target.chmod(0o755)

    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    staging.replace(dest)
    return [p["manifest_name"] for p in plugins]


def _within(path: Path, base: Path) -> bool:
    """True when ``path`` stays inside ``base`` once ``..`` segments collapse.

    Lexical (``normpath``) rather than ``resolve()``: the target does not exist
    yet, and resolving would follow a symlink an earlier member could have
    planted — which is the escape this exists to reject, not a path to trust.
    """
    import os

    base_str = os.path.normpath(str(base))
    return os.path.normpath(str(path)).startswith(base_str + os.sep)


#: Per-component-type destination inside a project scope, for the loose-file
#: shape. Keyed by the plugin's own subdirectory.
_COMPONENT_DIRS = {
    "skills": ".claude/skills",
    "agents": ".claude/agents",
    "commands": ".claude/commands",
}


def materialize_plugin_components(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    *,
    plugins: Optional[list[dict]] = None,
) -> tuple[dict[str, Path], dict, dict]:
    """Flatten the caller's plugins into project-scope files.

    Returns ``(files, hooks, mcp_servers)``:

    - ``files`` maps a project-relative POSIX path to the source file, covering
      every ``skills/``, ``agents/`` and ``commands/`` entry each plugin ships.
      A skill keeps its directory (so its ``references/`` travel with it); an
      agent and a command are single markdown files by convention.
    - ``hooks`` is the merged ``hooks`` mapping from every plugin's
      ``hooks/hooks.json``, ready to write into a settings file.
    - ``mcp_servers`` is the merged ``mcpServers`` mapping from every plugin's
      ``.mcp.json``.

    Name clashes resolve last-wins in the resolver's deterministic order, the
    same rule ``merged_skills`` applies to the menu, so what the composer offers
    and what lands here agree.

    Hook commands referencing ``${CLAUDE_PLUGIN_ROOT}`` cannot work in this
    shape — there is no installed plugin root — so such a hook is dropped with a
    warning rather than shipped to fail at tool time. That asymmetry is the
    honest cost of the flattened shape, and the reason the plugin-install shape
    is preferred wherever Agnes can run it.
    """
    if plugins is None:
        plugins = _resolved_plugins(conn, user)

    from src.marketplace_filter import escapes_base, is_unserved_path

    files: dict[str, Path] = {}
    hooks: dict = {}
    mcp_servers: dict = {}

    for plugin in plugins:
        for root in _plugin_roots(plugin):
            if root is None or not root.is_dir():
                continue
            bases = [root.resolve()]
            for kind, dest_dir in _COMPONENT_DIRS.items():
                src = root / kind
                if not src.is_dir():
                    continue
                for path in sorted(src.rglob("*")):
                    if not path.is_file() or path.is_symlink():
                        continue
                    rel = path.relative_to(src)
                    if is_unserved_path(rel.parts) or escapes_base(path, bases):
                        continue
                    files[f"{dest_dir}/{rel.as_posix()}"] = path
            _merge_json_block(root / "hooks" / "hooks.json", "hooks", hooks, plugin)
            _merge_json_block(root / ".mcp.json", "mcpServers", mcp_servers, plugin)

    return files, hooks, mcp_servers


def _plugin_roots(plugin: dict) -> Iterable[Optional[Path]]:
    """Every on-disk root of one resolved plugin — see
    ``app.chat.skills_catalog._plugin_dirs`` for why a Store bundle has many."""
    if plugin.get("bundle_dirs"):
        return list(plugin["bundle_dirs"])
    return [plugin.get("plugin_dir")]


def _merge_json_block(path: Path, key: str, into: dict, plugin: dict) -> None:
    """Merge ``path``'s ``key`` object into ``into``, skipping what cannot work.

    Unreadable or malformed JSON is a curator's bug in one plugin; it must not
    cost the caller every other plugin's content, so it is logged and skipped.
    """
    if not path.is_file():
        return
    try:
        block = json.loads(path.read_text(encoding="utf-8")).get(key) or {}
    except (OSError, ValueError):
        logger.warning("marketplace payload: unreadable %s in %s", path.name, plugin.get("manifest_name"))
        return
    if not isinstance(block, dict):
        return
    for name, value in block.items():
        if "CLAUDE_PLUGIN_ROOT" in json.dumps(value):
            logger.warning(
                "marketplace payload: dropping %s entry %r from %s — it needs an installed plugin root",
                key,
                name,
                plugin.get("manifest_name"),
            )
            continue
        into[name] = value
