"""Server-normalized skills + commands catalog for the web chat slash menu.

``GET /api/chat/skills`` (``app/api/chat.py``) needs to tell the composer
what is actually invokable in a given user's chat sandbox. Two independent
sources feed it:

  - **bundled** — skills shipped in the chat sandbox's bundled workspace
    template. ``app/chat/workdir.py``'s ``WorkdirManager`` copies this same
    ``<bundled_template_dir>/.claude/skills`` tree into every session
    (``prepare_ephemeral_session_dir``) / the whole template
    (``initialize_default_workspace``, used by ``ensure_user_workdir``) —
    this module reads the identical directory so the catalog matches what
    actually lands on disk.
  - **marketplace** — the caller's RBAC-filtered marketplace/store plugin
    set, resolved via ``src.marketplace_filter.resolve_user_marketplace``.
    That resolver is also what backs the served marketplace ZIP/git
    endpoints ``agnes refresh-marketplace`` pulls from — the same command
    ``app/chat/runner.py``'s ``_bootstrap_marketplace`` runs inside the
    sandbox — so this list matches what is actually installed there.

Shadowing: when a skill name appears in both sources, the **marketplace**
entry wins — it is the more user-specific grant. Merging happens in
``merged_skills``, which also isolates each source so a failure in one
(unreadable directory, resolver exception) never blocks the other; the
failing source is logged as a warning and treated as empty.

**Names are bare, deliberately.** A skill is invoked as ``/<name>`` on both
delivery paths — verified against the sandboxed CLI (Claude Code 2.1.218)
rather than assumed: a skill installed as part of a *plugin* is advertised by
the CLI's init handshake as ``{"name": "keboola-cli", "description":
"(demo-plugin) …"}``, and a *project* skill (``.claude/skills/<dir>/SKILL.md``)
as ``{"name": "project-scope-probe", …}``. The owning plugin shows up in the
description, never in the command token, so this module must NOT prefix a
marketplace skill with its plugin name — doing so is exactly what would make
the menu insert an unknown command.

**Delivery** (:func:`marketplace_delivery`) is the other half of the contract:
a marketplace skill is only invokable if something actually put it in the
agent's project scope. Agnes materializes the skill directories server-side —
into the per-user chat workspace for e2b/docker (``app/chat/workdir.py``), into
the workspace tarball for kai-agent (``app/api/kai.py``) — from this module's
own walk, so all three surfaces name the same set. With
``chat.bootstrap_marketplace`` off nothing is materialized, delivery is
``none``, and ``merged_skills`` omits the marketplace source entirely: a menu
entry for an undelivered skill is exactly what produced "Unknown command:
/keboola-cli" (#1552).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import duckdb

from src.marketplace_filter import resolve_user_marketplace
from src.store_guardrails._frontmatter import parse_frontmatter

logger = logging.getLogger(__name__)

# Same literal ``app/chat/workdir.py``'s ``WorkdirManager`` is constructed
# with in ``app/main.py`` / ``app/api/users.py`` (``bundled_template_dir``).
# Resolved relative to the repo root (not CWD) so this module works the same
# regardless of where the process was launched from.
BUNDLED_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "app" / "initial_workspace_default"

#: ``marketplace_delivery`` results — HOW the caller's stack plugins reach the
#: agent, which decides what a component is called once it is there.
#:
#: ``plugin``  — e2b / docker: the server writes the filtered marketplace as a
#:               directory in the workspace and the sandbox's own CLI installs
#:               real plugins from it, offline (``app/chat/marketplace_payload
#:               .export_marketplace_tree`` + ``app/chat/runner.py::
#:               _register_workspace_marketplace``). Faithful: hooks keep
#:               ``${CLAUDE_PLUGIN_ROOT}``, MCP servers register as
#:               ``plugin:<plugin>:<server>``, and agents/commands keep the
#:               ``<plugin>:<name>`` namespace.
#: ``project`` — kai-agent: Agnes never enters that provider's sandbox, so a
#:               plugin install (which writes the CLI's HOME registry) is out of
#:               reach and the plugins are flattened into project-scope files in
#:               the workspace tarball (``app/api/kai.py``). Every component type
#:               still arrives; the plugin namespace does not.
#: ``none``    — ``chat.bootstrap_marketplace`` is off; nothing is delivered, so
#:               the menu must not offer anything from the marketplace.
DELIVERY_PLUGIN = "plugin"
DELIVERY_PROJECT = "project"
DELIVERY_NONE = "none"

#: Providers Agnes cannot run a CLI inside — the workspace payload is the only
#: project scope it controls, so components ship flattened. A set so a fourth
#: engine-style provider is one entry, not another branch.
_FLATTENED_DELIVERY_PROVIDERS = frozenset({"kai-agent"})


def marketplace_delivery(chat_config: object) -> str:
    """How this configuration delivers the caller's marketplace plugins.

    One resolver for every consumer that must agree: ``GET /api/chat/skills``
    (what the composer offers, and under which token), ``app/chat/workdir.py``
    (what an e2b/docker workspace receives) and ``app/api/kai.py``'s workspace
    archive (what the engine's project scope receives). Splitting the decision
    would let the menu offer a token no sandbox answers to — the drift this
    whole change exists to remove.

    ``None``/missing config resolves to :data:`DELIVERY_NONE` rather than
    guessing: with no chat runtime loaded there is nothing to deliver into, and
    offering the plugins anyway is the failure mode, not the safe default.
    """
    if chat_config is None or not getattr(chat_config, "bootstrap_marketplace", False):
        return DELIVERY_NONE
    provider = str(getattr(chat_config, "provider", "") or "").strip().lower()
    return DELIVERY_PROJECT if provider in _FLATTENED_DELIVERY_PROVIDERS else DELIVERY_PLUGIN


def _read_skill_md(skill_md: Path) -> tuple[str, Optional[str]]:
    """Return ``(name, description)`` for one ``SKILL.md``, frontmatter-first.

    Frontmatter is the source of truth. ``name`` falls back to the SKILL.md's
    parent directory name (the "skill directory") when frontmatter has no
    ``name``; ``description`` falls back to ``None`` (no second line in the
    UI) rather than an empty string.
    """
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(text)
    name = (fm.get("name") or "").strip() or skill_md.parent.name
    description = (fm.get("description") or "").strip() or None
    return name, description


def list_bundled_skills(bundled_template_dir: Path) -> list[dict]:
    """Skills shipped in the chat sandbox's bundled workspace template.

    ``bundled_template_dir`` is the template root (e.g.
    ``app/initial_workspace_default``); this looks under its
    ``.claude/skills/<name>/SKILL.md`` — the same layout
    ``WorkdirManager.prepare_ephemeral_session_dir`` copies from. A missing
    ``.claude/skills`` directory is normal (nothing bundled yet), not an
    error — returns an empty list.
    """
    out: list[dict] = []
    skills_dir = bundled_template_dir / ".claude" / "skills"
    if not skills_dir.is_dir():
        return out
    # The same feature gate the sandbox prune uses. This list reads the
    # SHIPPED template, not the user's converged workspace, so without it the
    # composer's slash menu kept advertising a skill that
    # `_prune_disabled_feature_skills` had deleted from the sandbox —
    # invoking it did nothing, because the files were gone. Gating here rather
    # than reading the workspace keeps one source of truth (the flag) and
    # avoids making the menu depend on a filesystem convergence having already
    # run for this user. (Devin Review on #1239.)
    from app.chat.workdir import skill_disabled_on_this_instance

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        if skill_disabled_on_this_instance(skill_dir.name):
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            name, description = _read_skill_md(skill_md)
        except OSError:
            logger.warning("chat skills: unreadable bundled SKILL.md %s", skill_md, exc_info=True)
            continue
        out.append({"name": name, "description": description, "source": "bundled"})
    return out


def _plugin_dirs(plugin: dict) -> list[Path]:
    """Every on-disk root to search for ``SKILL.md`` for one resolved plugin.

    Most entries have a single ``plugin_dir``; the synthetic Store "flea"
    bundle entry (``resolve_user_marketplace``'s ``source="store-bundle"``)
    has no single root — instead ``bundle_dirs`` lists one directory per
    bundled skill/agent upload.
    """
    if plugin.get("bundle_dirs"):
        return list(plugin["bundle_dirs"])
    plugin_dir = plugin.get("plugin_dir")
    return [plugin_dir] if plugin_dir is not None else []


def _marketplace_skill_entries(conn: duckdb.DuckDBPyConnection, user: dict) -> list[dict]:
    """``[{name, description, dir}]`` for every marketplace skill the caller has.

    The single walk both consumers share: :func:`list_marketplace_skills` turns
    it into menu rows and :func:`iter_marketplace_skill_dirs` hands the
    directories to ``app/api/kai.py``'s workspace archive. Sharing the walk is
    what makes the ``project`` delivery path ship exactly the set the menu
    advertises, instead of two scans that can disagree.

    ``name`` is the frontmatter name (directory-name fallback), i.e. the
    ``/<name>`` token — so a copy made under that name has its directory,
    frontmatter and slash command all agreeing. ``dir`` is the SKILL.md's
    parent, so a skill's own ``references/`` files travel with it.

    A plugin's ``SKILL.md`` may live directly at its root (single-skill
    plugins, e.g. the built-in marketplace) or under ``skills/<name>/
    SKILL.md`` (multi-skill plugins, the curated-marketplace and Store
    convention) — this looks anywhere under the plugin root, mirroring
    ``src/store_guardrails/manifest_check.py``'s same defensive scan.

    Order is the resolver's (deterministic), then path, so a duplicate name
    resolves the same way in both consumers.
    """
    out: list[dict] = []
    for plugin in resolve_user_marketplace(conn, user):
        for plugin_dir in _plugin_dirs(plugin):
            if plugin_dir is None or not plugin_dir.is_dir():
                continue
            for skill_md in sorted(plugin_dir.rglob("SKILL.md")):
                try:
                    name, description = _read_skill_md(skill_md)
                except OSError:
                    logger.warning("chat skills: unreadable marketplace SKILL.md %s", skill_md, exc_info=True)
                    continue
                out.append({"name": name, "description": description, "dir": skill_md.parent})
    return out


def iter_marketplace_skill_dirs(conn: duckdb.DuckDBPyConnection, user: dict) -> list[tuple[str, Path]]:
    """``[(skill_name, skill_dir)]`` for the ``project`` delivery path.

    Used by ``app/api/kai.py`` to overlay the caller's marketplace skills into
    the workspace tarball the kai-agent engine materializes into its project
    scope — that provider spawns no runner of ours, so this archive is the only
    project scope Agnes controls there.
    """
    return [(e["name"], e["dir"]) for e in _marketplace_skill_entries(conn, user)]


def list_marketplace_skills(conn: duckdb.DuckDBPyConnection, user: dict) -> list[dict]:
    """The caller's RBAC-filtered marketplace/store plugin skills.

    Uses ``resolve_user_marketplace`` (admin-granted-and-subscribed
    marketplace plugins, unioned with the caller's Store installs) — the
    same composition ``agnes refresh-marketplace`` fetches server-side and
    ``app/chat/runner.py``'s ``_bootstrap_marketplace`` installs into the
    live sandbox.

    Names are bare (never ``<plugin>:<skill>``) — see the module docstring for
    the CLI handshake this was verified against.
    """
    return [
        {"name": e["name"], "description": e["description"], "source": "marketplace"}
        for e in _marketplace_skill_entries(conn, user)
    ]


def merged_skills(
    bundled_template_dir: Path,
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    *,
    delivery: str = DELIVERY_PLUGIN,
) -> list[dict]:
    """Merge bundled + marketplace skills into one deterministic list.

    ``delivery`` is :func:`marketplace_delivery`'s answer for the running
    configuration. :data:`DELIVERY_NONE` drops the marketplace source entirely:
    with ``chat.bootstrap_marketplace`` off, nothing materializes those skills
    into the session, and a menu row for one is a promise the agent cannot keep
    (``/keboola-cli`` → "Unknown command", #1552). It defaults to
    :data:`DELIVERY_PLUGIN` — a delivering value — so a caller that forgets the
    argument offers exactly what it offered before this argument existed. Skill
    names are identical under both delivering modes (a plugin's skill is
    exposed bare, same as a project skill), so only :func:`list_marketplace_commands`
    actually branches on it.

    Marketplace entries win name clashes against bundled ones (more
    user-specific). Either source failing to list is logged as a warning
    and treated as empty rather than failing the whole request — the other
    source's skills still reach the caller. The result is sorted by name so
    the composer's filterable menu has a stable order.
    """
    try:
        bundled = list_bundled_skills(bundled_template_dir)
    except Exception:
        logger.warning("chat skills: bundled source failed to list", exc_info=True)
        bundled = []

    if delivery == DELIVERY_NONE:
        marketplace: list[dict] = []
    else:
        try:
            marketplace = list_marketplace_skills(conn, user)
        except Exception:
            logger.warning("chat skills: marketplace source failed to list", exc_info=True)
            marketplace = []

    by_name: dict[str, dict] = {s["name"]: s for s in bundled}
    for s in marketplace:
        by_name[s["name"]] = s  # marketplace wins name clashes

    return sorted(by_name.values(), key=lambda s: s["name"])


def list_marketplace_commands(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    *,
    delivery: str = DELIVERY_PLUGIN,
) -> list[dict]:
    """Slash commands the caller's stack plugins ship (``commands/*.md``).

    Unlike a skill, a command's invocation token DEPENDS on the delivery mode —
    verified against the sandboxed CLI (Claude Code 2.1.218), not assumed:

    ===============  ==========================  =====================
    component        installed as a plugin       flattened to project
    ===============  ==========================  =====================
    skill            ``/keboola-cli``            ``/keboola-cli``
    command          ``/kbl:kbl-ship``           ``/kbl-ship``
    agent            ``kbl:kbl-reviewer``        ``kbl-reviewer``
    ===============  ==========================  =====================

    So a skill is bare either way, and a command is namespaced by its plugin
    exactly when Agnes could install the plugin for real. Getting this backwards
    in either direction produces the original bug — a menu entry the agent
    answers "Unknown command" to.

    Agents are deliberately absent from this listing: they are dispatched by the
    Task tool, not by a slash command, so offering one in the composer's menu
    would insert a token nothing resolves. They ARE delivered.

    :data:`DELIVERY_NONE` is handled by the caller (:func:`merged_commands`),
    which omits the whole source.
    """
    out: list[dict] = []
    for plugin in resolve_user_marketplace(conn, user):
        namespace = plugin.get("manifest_name") or plugin.get("original_name") or ""
        for plugin_dir in _plugin_dirs(plugin):
            if plugin_dir is None or not plugin_dir.is_dir():
                continue
            commands_dir = plugin_dir / "commands"
            if not commands_dir.is_dir():
                continue
            for md in sorted(commands_dir.rglob("*.md")):
                try:
                    fm = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    logger.warning("chat skills: unreadable marketplace command %s", md, exc_info=True)
                    continue
                local = md.stem
                name = f"{namespace}:{local}" if (delivery == DELIVERY_PLUGIN and namespace) else local
                description = (fm.get("description") or "").strip() or None
                out.append({"name": name, "description": description, "source": "marketplace"})
    return out


def merged_commands(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    *,
    delivery: str = DELIVERY_PLUGIN,
) -> list[dict]:
    """Every slash command the composer may offer, deterministically ordered.

    Only marketplace-plugin commands today: the bundled chat workspace template
    ships no ``.claude/commands/*.md`` (checked, not assumed — contrast the
    LOCAL analyst workspace, which gets them from ``cli/templates/commands/``
    via ``agnes init``, a different code path), and the runner still performs no
    slash-command parsing of its own — every ``user_msg`` is forwarded verbatim
    and resolved by the sandboxed CLI.

    A failure to list degrades to an empty list with a warning, matching
    ``merged_skills``: a broken marketplace must not empty the whole menu.
    """
    if delivery == DELIVERY_NONE:
        return []
    try:
        commands = list_marketplace_commands(conn, user, delivery=delivery)
    except Exception:
        logger.warning("chat commands: marketplace source failed to list", exc_info=True)
        return []
    by_name: dict[str, dict] = {c["name"]: c for c in commands}
    return sorted(by_name.values(), key=lambda c: c["name"])
