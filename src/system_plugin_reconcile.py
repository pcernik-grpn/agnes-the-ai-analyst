"""What migration 0098 does, for the backend Alembic never reaches.

``src/db_pg.py::ensure_pg_at_head`` runs the Alembic ladder on Postgres. A
DuckDB app-state instance runs NO Alembic, and its own ladder in
``src/db.py`` is frozen under the A3 ratchet — it cannot grow a step. So
without this module a DuckDB instance would take the new code (which reads
reach out of ``resource_grants`` alone) while its data still says
``marketplace_plugins.is_system``, and every plugin an admin had made
automatic would silently reach nobody.

That is the governing rule of this whole change — **an upgrade must never
change who can see what** — failing on the backend the migration cannot
touch. The rule is not a Postgres property, and 0098's snapshot tests only
proved it where it was never at risk.

An idempotent boot-time reconciliation, not a ladder step. Same shape as
``src.marketplace.seed_builtin_marketplace`` and
``app.chat.grant_seed.seed_chat_grant``: safe to run on every boot, cheap
when there is nothing to do, and a no-op on Postgres (where 0098 already
did it, transactionally and with a test that snapshots the effective
(person, thing) set on both sides).

THE ORDER IS THE WHOLE THING, and it is not the order you would guess.

1. **Convert the Workspace narrowing.** Where ``AGNES_GROUP_EVERYONE_EMAIL``
   was set, the seeded ``Everyone`` group was mirrored from a Workspace
   group and held a SUBSET of the accounts. Give the subset its own group,
   named after the email, and move both its members and its grants there.
   0098's step 2, spelled for DuckDB.
2. **Only then, make ``Everyone`` universal** — but ONLY on an instance
   where step 1 actually ran. This is the step with the teeth: adding every
   account to a group that still holds grants would hand those accounts the
   grants, which is a widening. After step 1 the group is grant-free (bar
   the ``slack_channel`` markers, which reach no person), so it is safe
   there and unsafe anywhere else.
3. **Turn the flag into a grant.** One required grant per ``is_system``
   plugin, held by ``Everyone`` — which steps 1 and 2 have just made
   genuinely every account, so it reaches exactly who the flag reached.

Doing 3 before 1 on a narrowed instance writes a grant against a group that
is not everyone, and the plugin quietly stops reaching most of the
instance. Doing 2 before 1 hands the mirrored group's grants to the whole
instance. Neither is recoverable by a later boot, because by then the
evidence of what the group used to mean is gone.

The flag is CLEARED as the last act, which is what makes this idempotent
and what makes the second boot cheap. The column itself survives — see
``src/models/store.py``; dropping it is the contract half of an
expand/contract pair and ships in a later release.

EVERY read and write here goes through ``src.repositories``. An earlier
draft opened the system DuckDB connection directly and ran raw SQL on the
state tables, and two static guards caught it: ``tests/test_backend_split_guard.py`` ratchets
both (a raw connection reads the WRONG BACKEND on a Postgres instance, which
is the bug class that guard exists for), and A3 forbids answering it with a
new DuckDB-only repository. The operations it needs are methods on the
EXISTING repositories instead, each with a real Postgres sibling —
``list_legacy_system_keys`` / ``clear_legacy_system_flags``,
``repoint_group``, ``move_all_members`` / ``add_all_users``. Widening an
allow-list would have turned a guard into decoration.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

#: Mirrors ``migrations/versions/0098_everyone_becomes_a_scope.py``. Inlined
#: for the same reason a migration inlines its constants: this module has to
#: keep describing the shape it shipped against.
ENV_EVERYONE_EMAIL = "AGNES_GROUP_EVERYONE_EMAIL"
SYNC_CREATED_BY = "system:google-sync"

#: A ``slack_channel`` grant on the seeded group is not an audience grant —
#: it marks a channel open, and ``services.slack_bot.binding`` reads it off
#: that exact group id. Repointing it switches Agnes off in every channel an
#: admin enabled.
MARKER_TYPES = ("slack_channel",)


def _everyone_group_id() -> Optional[str]:
    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import user_groups_repo

    row = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    return row["id"] if row else None


def _convert_workspace_narrowing(everyone_id: str) -> bool:
    """0098's step 2. Returns True iff a conversion happened.

    The return value is load-bearing: it is the ONLY thing that authorizes
    the membership backfill below, because it is the only evidence the
    seeded group has been emptied of grants that were never everyone's.
    """
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

    everyone_email = os.environ.get(ENV_EVERYONE_EMAIL, "").strip().lower()
    if not everyone_email:
        return False

    groups = user_groups_repo()
    existing = groups.get_by_name(everyone_email)
    if existing:
        target_id = existing["id"]
    else:
        target_id = groups.create(
            name=everyone_email,
            description=(
                f"Was mapped to the Everyone system group by {ENV_EVERYONE_EMAIL}. "
                "Converted to an ordinary Workspace-synced group; membership still "
                "comes from the same Workspace group."
            ),
            created_by=SYNC_CREATED_BY,
        )["id"]

    moved_members = user_group_members_repo().move_all_members(everyone_id, target_id)
    moved_grants = resource_grants_repo().repoint_group(
        everyone_id, target_id, exclude_types=list(MARKER_TYPES)
    )
    logger.info(
        "system-plugin reconcile: converted the %s narrowing into the ordinary group "
        "%r — %d membership(s) and %d grant(s) moved; %s left on the seeded group",
        ENV_EVERYONE_EMAIL,
        everyone_email,
        moved_members,
        moved_grants,
        "/".join(MARKER_TYPES),
    )
    return True


def _make_everyone_universal(everyone_id: str) -> int:
    """0098 has no counterpart to this, and does not need one.

    On Postgres "every account" is a SCOPE, so the seeded group's membership
    stopped mattering. DuckDB has no such column (frozen ladder), so the only
    way to say "everyone" there is a group that really does hold everyone —
    which this makes true.

    Called ONLY after :func:`_convert_workspace_narrowing` returned True.
    """
    from src.repositories import user_group_members_repo

    added = user_group_members_repo().add_all_users(
        everyone_id, source="system_seed", added_by="system:everyone-scope-reconcile"
    )
    if added:
        logger.info(
            "system-plugin reconcile: added %d account(s) to the Everyone group, "
            "which now means every account on this instance",
            added,
        )
    return added


def _flags_become_grants(everyone_id: str) -> int:
    """0098's step 4, minus the scope column this backend does not have."""
    from src.grant_sources import SYSTEM_PLUGIN_MIGRATION
    from src.repositories import marketplace_plugins_repo, resource_grants_repo

    plugins = marketplace_plugins_repo()
    grants = resource_grants_repo()
    keys = plugins.list_legacy_system_keys()

    for marketplace_id, plugin_name in keys:
        resource_id = f"{marketplace_id}/{plugin_name}"
        held = [
            g
            for g in grants.list_all(resource_type="marketplace_plugin", group_id=everyone_id)
            if g["resource_id"] == resource_id
        ]
        if not held:
            grants.create(
                group_id=everyone_id,
                resource_type="marketplace_plugin",
                resource_id=resource_id,
                assigned_by=SYSTEM_PLUGIN_MIGRATION,
                requirement="required",
            )
        elif (held[0].get("requirement") or "available") != "required":
            # A hand-set grant on the carrier for a plugin the flag ALREADY
            # made mandatory for everyone. Upgrading the tier preserves reach
            # rather than changing it; leaving it Optional would let a member
            # drop a plugin they could not drop yesterday.
            grants.update_requirement(held[0]["id"], "required")

    # Clear the flag LAST — idempotence, and it stops the surviving column
    # contradicting the grants. Unconditional, so a disabled-and-flagged row
    # (never granted, correctly) does not keep the column disagreeing forever.
    cleared = plugins.clear_legacy_system_flags()
    if cleared:
        logger.info(
            "system-plugin reconcile: %d plugin(s) that were 'system' now reach "
            "everyone through a required grant instead (%d flag(s) cleared)",
            len(keys),
            cleared,
        )
    return len(keys)


def reconcile_system_plugin_flags() -> bool:
    """Run the DuckDB half of 0098. Idempotent; True iff anything changed.

    Fail-soft by design, like every other boot-time seeder: an instance that
    cannot reconcile should still start and serve, loudly logged, rather than
    refuse to boot. The cost is bounded — the flag is still set, so the next
    boot retries.
    """
    from src.repositories import use_pg

    if use_pg():
        # 0098 owns this on Postgres, transactionally, and has already run by
        # the time anything calls us.
        return False

    try:
        everyone_id = _everyone_group_id()
        if not everyone_id:
            logger.warning(
                "system-plugin reconcile: the Everyone system group is missing; "
                "skipping. Nothing has been changed, and the next boot retries."
            )
            return False

        converted = _convert_workspace_narrowing(everyone_id)
        if converted:
            _make_everyone_universal(everyone_id)
        changed = _flags_become_grants(everyone_id)
        return converted or bool(changed)
    except Exception:
        logger.exception(
            "system-plugin reconcile failed; plugins that were marked 'system' may "
            "not reach everyone until this succeeds. Retried on the next boot."
        )
        return False
