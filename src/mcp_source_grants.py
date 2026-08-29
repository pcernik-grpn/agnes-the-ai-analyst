"""Default-visibility RBAC seeding for MCP sources (TCRD-236).

MCP sources ("`/admin` MCP sources", ``app/api/admin_mcp.py``) predate a
grantable resource type of their own — visibility of a whole server was
governed only by the per-TOOL ``tool_grants`` table (Universal MCP, RFC #461
M5). ``ResourceType.MCP_SOURCE`` (``app/resource_types.py``) adds a coarser,
source-wide gate consumed at the two seams that decide what a caller can
discover — ``app/api/mcp_passthrough.py::_visible_passthrough_tools`` and
``app/api/mcp/tools_generator.py::_allowed_passthrough_names`` — and, for the
call itself, ``app/api/mcp_policy.py::enforce_passthrough_access``. That gate
(``app/api/mcp_policy.py::visible_mcp_source_ids``) is ANDed with the
existing per-tool grant — it never widens what ``tool_grants`` already
allows, and it treats a source with NO ``mcp_source`` grant row at all as
visible-to-everyone, so the functions here are a belt-and-suspenders
convenience (an auditable, checked "Everyone" row on /admin/access), not the
only thing standing between "flip a switch" and "every user loses every MCP
connection overnight".

Two call sites:

* :func:`seed_default_mcp_source_grants` — idempotent boot-time sweep. Grants
  ``Everyone`` on every ALREADY-REGISTERED source that holds no ``mcp_source``
  grant row for any group yet. Safe on every startup: a source an admin has
  since narrowed (removed Everyone, granted only one group) holds a grant row
  of its own and is left alone forever after — exactly the seeding pattern
  ``src/marketplace.py::seed_builtin_marketplace`` uses for the built-in
  marketplace's RBAC rows.
* :func:`ensure_default_mcp_source_grant` — called once at registration time
  (``app/api/admin_mcp.py::create_mcp_source``) so a source registered AFTER
  this ships gets the same "works like today" default instead of landing
  invisible to every non-admin until the next restart.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.resource_types import ResourceType

logger = logging.getLogger(__name__)

#: The group every already-registered (and newly registered) MCP source is
#: grandfathered onto, absent any narrower grant an admin writes later.
_DEFAULT_GROUP_NAME = "Everyone"


def _everyone_group_id() -> Optional[str]:
    from src.repositories import user_groups_repo

    everyone = user_groups_repo().get_by_name(_DEFAULT_GROUP_NAME)
    if not everyone:
        logger.warning(
            "mcp_source RBAC seed: %r system group not found; skipping",
            _DEFAULT_GROUP_NAME,
        )
        return None
    return everyone["id"]


def ensure_default_mcp_source_grant(source_id: str) -> None:
    """Grant ``Everyone`` on ``source_id`` — call right after a fresh MCP
    source row is inserted (``create_mcp_source``).

    A brand-new ``source_id`` cannot already hold a grant (resource_grants is
    keyed by resource_id, and the id was just generated), so this always
    writes the row; ``ensure_grant`` is itself idempotent (INSERT-or-ignore)
    so a duplicate call is harmless.
    """
    group_id = _everyone_group_id()
    if not group_id:
        return
    from src.repositories import resource_grants_repo

    resource_grants_repo().ensure_grant(
        group_id=group_id,
        resource_type=ResourceType.MCP_SOURCE.value,
        resource_id=source_id,
    )
    logger.info("mcp_source RBAC seed: granted Everyone -> mcp_source:%s", source_id)


def seed_default_mcp_source_grants() -> None:
    """Idempotently grandfather every already-registered MCP source onto
    ``Everyone`` at boot. Safe to call on every startup.

    A source that already holds a grant row for ANY group — including one
    this seed wrote on a previous boot — is left untouched, so an admin's
    later narrowing (removing Everyone, granting a specific group instead)
    survives every subsequent restart.
    """
    group_id = _everyone_group_id()
    if not group_id:
        return
    from src.repositories import mcp_sources_repo, resource_grants_repo

    sources = mcp_sources_repo().list_all()
    if not sources:
        return
    grants_repo = resource_grants_repo()
    already_granted = {g["resource_id"] for g in grants_repo.list_all(resource_type=ResourceType.MCP_SOURCE.value)}
    seeded = 0
    for src in sources:
        if src["id"] in already_granted:
            continue
        grants_repo.ensure_grant(
            group_id=group_id,
            resource_type=ResourceType.MCP_SOURCE.value,
            resource_id=src["id"],
        )
        seeded += 1
    if seeded:
        logger.info("mcp_source RBAC seed: grandfathered %d source(s) onto Everyone", seeded)
