"""Receive an externally-generated Claude Code skill and publish it into a
local, sync-immune "contributed" marketplace.

This is the server-side landing for an external "Load skill to Agnes" button:
a process-recording / mining tool generates a ``SKILL.md`` and the operator
pastes it into Agnes (while logged in as admin). We wrap that single skill in a
minimal one-skill plugin and make it show up in the marketplace browse + the
Claude Code feed.

Why a dedicated marketplace instead of an existing git one:

* Admin-registered marketplaces are git working copies. The nightly sync runs
  ``git reset --hard FETCH_HEAD`` (see ``src.marketplace._sync_spec``), which
  would wipe any locally-written skill on the next 03:00 UTC pass.
* The built-in marketplace already demonstrates the escape hatch: a registry
  row with ``is_builtin=TRUE`` is skipped by the nightly sync and carries a
  sentinel URL that is never git-cloned (see ``seed_builtin_marketplace``).

The contributed marketplace reuses that exact pattern under a *different* slug,
so (a) the nightly sync never resets it and (b) the boot re-seed of the
built-in marketplace (which ``rmtree``'s only ``agnes-builtin``) never wipes
it. A contributed skill is therefore durable across both restarts and syncs.

No new schema, no new repository method — this composes existing primitives
(``marketplace_registry_repo``, ``_refresh_plugin_cache``,
``resource_grants_repo``), so there is no DuckDB<->Postgres parity surface to
maintain here.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

from app.utils import get_marketplaces_dir
from src.marketplace import _lock, _refresh_plugin_cache, is_valid_slug
from src.marketplace_listing import _parse_frontmatter

logger = logging.getLogger(__name__)

#: Slug for the local marketplace that receives externally-contributed skills.
#: Distinct from ``BUILTIN_MARKETPLACE_SLUG`` so the built-in boot re-seed never
#: touches it; ``is_builtin=TRUE`` so the nightly git-sync skips it.
CONTRIBUTED_MARKETPLACE_SLUG = "agnes-contributed"

#: Sentinel URL stored in the registry row. Never used for git operations;
#: only satisfies the NOT NULL constraint on ``marketplace_registry.url``.
_CONTRIBUTED_SENTINEL_URL = "builtin://agnes-contributed"

#: Default group a freshly contributed skill is granted to. "Admin" gives a
#: curate-first posture: only admins see it until they re-grant it wider on
#: ``/admin/access``. Pass ``grant_group="Everyone"`` to publish instance-wide.
DEFAULT_GRANT_GROUP = "Admin"

_MARKETPLACE_NAME = "Agnes Contributed"
_MARKETPLACE_DESCRIPTION = (
    "Skills contributed from external tools (e.g. process recordings turned "
    "into Claude Code skills). Published in place by an admin; never git-synced."
)


class SkillContributionError(Exception):
    """Raised when a pasted skill cannot be turned into a publishable plugin."""


def _slugify(value: str) -> str:
    """Lower-case, hyphenate, and strip to the marketplace slug charset.

    Mirrors the ``[a-z0-9][a-z0-9_-]{0,63}`` shape that ``is_valid_slug``
    enforces so the result is safe both as a directory name and as a plugin
    identifier.
    """
    s = re.sub(r"[^a-z0-9_-]+", "-", (value or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-_")
    return s[:64]


def _ensure_registry_row(registered_by: Optional[str]) -> None:
    """Idempotently upsert the contributed-marketplace registry row."""
    from src.repositories import marketplace_registry_repo

    marketplace_registry_repo().register(
        id=CONTRIBUTED_MARKETPLACE_SLUG,
        name=_MARKETPLACE_NAME,
        url=_CONTRIBUTED_SENTINEL_URL,
        description=_MARKETPLACE_DESCRIPTION,
        registered_by=registered_by or "system:contribute",
        curator_name="Contributed",
        is_builtin=True,
    )
    # TCRD-219: same stale-stamp problem as the built-in row — after the
    # sync_one() guard no sync can ever touch this row again, so a failure
    # stamped by a pre-guard "Sync now" click would otherwise be permanent.
    marketplace_registry_repo().clear_sync_error(CONTRIBUTED_MARKETPLACE_SLUG)


def _upsert_manifest_entry(repo_root: Path, plugin_name: str, description: str) -> None:
    """Add (or replace) the plugin's entry in ``.claude-plugin/marketplace.json``.

    Creates the manifest on first contribution. Existing entries for the same
    plugin name are replaced so re-contributing the same skill is idempotent.
    """
    manifest_path = repo_root / ".claude-plugin" / "marketplace.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    data: Dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}

    data.setdefault("name", _MARKETPLACE_NAME)
    data.setdefault("description", _MARKETPLACE_DESCRIPTION)

    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        plugins = []
    plugins = [p for p in plugins if not (isinstance(p, dict) and p.get("name") == plugin_name)]
    plugins.append(
        {
            "name": plugin_name,
            "version": "1.0.0",
            "description": description,
            "source": f"./plugins/{plugin_name}",
        }
    )
    data["plugins"] = plugins
    manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _grant_to_group(group_name: str, plugin_name: str) -> bool:
    """Grant the plugin to a group so it appears in the served marketplace.

    Returns False (and logs) when the group does not exist — the skill is
    still on disk and cached, just not visible to anyone until granted.
    """
    from src.repositories import resource_grants_repo, user_groups_repo

    group = user_groups_repo().get_by_name(group_name)
    if not group:
        logger.warning("contribute: group %r not found; skill left ungranted", group_name)
        return False
    resource_grants_repo().ensure_grant(
        group_id=group["id"],
        resource_type="marketplace_plugin",
        resource_id=f"{CONTRIBUTED_MARKETPLACE_SLUG}/{plugin_name}",
    )
    return True


def contribute_skill(
    skill_md: str,
    *,
    registered_by: Optional[str] = None,
    grant_group: str = DEFAULT_GRANT_GROUP,
) -> Dict[str, Any]:
    """Publish a single pasted ``SKILL.md`` into the contributed marketplace.

    Wraps the skill in a minimal one-skill plugin, refreshes the plugin cache,
    and grants it to ``grant_group`` so it shows up. Returns the resolved names
    and deep-link URLs so the caller can offer "open it in Agnes".

    Raises:
        SkillContributionError: empty body or missing ``name:`` frontmatter.
    """
    text = (skill_md or "").strip()
    if not text:
        raise SkillContributionError("Skill is empty.")

    fm = _parse_frontmatter(text)
    raw_name = (fm.get("name") or "").strip()
    if not raw_name:
        raise SkillContributionError(
            "Skill has no `name:` in its frontmatter. Add a YAML frontmatter "
            "block (--- name: ... ---) at the top of the SKILL.md."
        )

    plugin_name = _slugify(raw_name)
    if not is_valid_slug(plugin_name):
        raise SkillContributionError(f"Could not derive a valid plugin name from {raw_name!r}.")
    description = (fm.get("description") or "").strip() or f"Contributed skill: {raw_name}"

    # Serialize the disk write + manifest read-modify-write + cache refresh
    # under the same process-wide lock the nightly sync uses (src.marketplace._lock),
    # so two concurrent contributions — or a contribution racing a sync — can't
    # lose a manifest entry to a read-modify-write race.
    with _lock:
        # 1. Registry row (idempotent, sync-immune).
        _ensure_registry_row(registered_by)

        # 2. Write the plugin tree on disk: a one-skill plugin where the plugin
        #    name == the skill name (simplest shape that renders a skill detail).
        repo_root = get_marketplaces_dir() / CONTRIBUTED_MARKETPLACE_SLUG
        plugin_root = repo_root / "plugins" / plugin_name
        skill_dir = plugin_root / "skills" / plugin_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (plugin_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps(
                {"name": plugin_name, "version": "1.0.0", "description": description},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")

        # 3. Register the plugin in the marketplace manifest.
        _upsert_manifest_entry(repo_root, plugin_name, description)

        # 4. Refresh the plugin cache (writes marketplace_plugins, backend-aware).
        plugin_count = _refresh_plugin_cache(CONTRIBUTED_MARKETPLACE_SLUG)

        # 5. Grant so it shows up in the served feed + browse page.
        granted = _grant_to_group(grant_group, plugin_name)

    detail_url = f"/marketplace/curated/{CONTRIBUTED_MARKETPLACE_SLUG}/{plugin_name}"
    skill_url = f"{detail_url}/skill/{plugin_name}"
    logger.info(
        "contribute: published skill %r as plugin %r (granted=%s, plugins_now=%d)",
        raw_name,
        plugin_name,
        granted,
        plugin_count,
    )
    return {
        "skill_name": raw_name,
        "plugin_name": plugin_name,
        "description": description,
        "marketplace_slug": CONTRIBUTED_MARKETPLACE_SLUG,
        "granted_group": grant_group if granted else None,
        "detail_url": detail_url,
        "skill_url": skill_url,
        "plugin_count": plugin_count,
    }
