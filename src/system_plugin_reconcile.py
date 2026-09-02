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
"""

from __future__ import annotations

import logging
import os
import uuid
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


def _column_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
        [table, column],
    ).fetchone()
    return row is not None


def _everyone_group_id(conn) -> Optional[str]:
    from src.db import SYSTEM_EVERYONE_GROUP

    row = conn.execute(
        "SELECT id FROM user_groups WHERE name = ? AND is_system",
        [SYSTEM_EVERYONE_GROUP],
    ).fetchone()
    return row[0] if row else None


def _convert_workspace_narrowing(conn, everyone_id: str) -> bool:
    """0098's step 2. Returns True iff a conversion happened.

    The return value is load-bearing: it is the ONLY thing that authorizes
    step 2's membership backfill, because it is the only evidence that the
    seeded group has been emptied of grants that were never everyone's.
    """
    everyone_email = os.environ.get(ENV_EVERYONE_EMAIL, "").strip().lower()
    if not everyone_email:
        return False

    row = conn.execute("SELECT id FROM user_groups WHERE name = ?", [everyone_email]).fetchone()
    if row:
        target_id = row[0]
    else:
        target_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO user_groups (id, name, description, is_system, created_by) VALUES (?, ?, ?, FALSE, ?)",
            [
                target_id,
                everyone_email,
                (
                    f"Was mapped to the Everyone system group by {ENV_EVERYONE_EMAIL}. "
                    "Converted to an ordinary Workspace-synced group; membership still "
                    "comes from the same Workspace group."
                ),
                SYNC_CREATED_BY,
            ],
        )

    # Members move with their `source` intact, so the sync keeps owning the
    # rows it owns. ALL of them move: the grants move too, so a row left
    # behind would lose them.
    conn.execute(
        "INSERT OR IGNORE INTO user_group_members (user_id, group_id, source, added_at, added_by) "
        "SELECT user_id, ?, source, added_at, added_by "
        "FROM user_group_members WHERE group_id = ?",
        [target_id, everyone_id],
    )
    conn.execute("DELETE FROM user_group_members WHERE group_id = ?", [everyone_id])

    markers = ",".join(["?"] * len(MARKER_TYPES))
    # Stronger tier survives a collision, or a Required grant silently
    # becomes Optional and stops landing in those people's workspaces.
    conn.execute(
        f"""UPDATE resource_grants SET requirement = 'required'
            WHERE group_id = ? AND resource_type NOT IN ({markers}) AND EXISTS (
                SELECT 1 FROM resource_grants s
                WHERE s.group_id = ?
                  AND s.resource_type = resource_grants.resource_type
                  AND s.resource_id = resource_grants.resource_id
                  AND s.requirement = 'required')""",
        [target_id, *MARKER_TYPES, everyone_id],
    )
    conn.execute(
        f"""DELETE FROM resource_grants
            WHERE group_id = ? AND resource_type NOT IN ({markers}) AND EXISTS (
                SELECT 1 FROM resource_grants t
                WHERE t.group_id = ?
                  AND t.resource_type = resource_grants.resource_type
                  AND t.resource_id = resource_grants.resource_id)""",
        [everyone_id, *MARKER_TYPES, target_id],
    )
    conn.execute(
        f"UPDATE resource_grants SET group_id = ? WHERE group_id = ? AND resource_type NOT IN ({markers})",
        [target_id, everyone_id, *MARKER_TYPES],
    )
    logger.info(
        "system-plugin reconcile: converted the %s narrowing into the ordinary group %r",
        ENV_EVERYONE_EMAIL,
        everyone_email,
    )
    return True


def _make_everyone_universal(conn, everyone_id: str) -> int:
    """0098 has no counterpart to this, and does not need one.

    On Postgres "every account" is a SCOPE, so the seeded group's membership
    stopped mattering. DuckDB has no such column (frozen ladder), so the only
    way to say "everyone" there is a group that really does hold everyone —
    which this makes true.

    Called ONLY after :func:`_convert_workspace_narrowing` returned True. On
    any other instance the group still holds grants, and adding an account
    to it would hand that account those grants.
    """
    rows = conn.execute(
        """INSERT INTO user_group_members (user_id, group_id, source, added_by)
           SELECT u.id, ?, 'system_seed', 'system:everyone-scope-reconcile'
           FROM users u
           WHERE NOT EXISTS (
               SELECT 1 FROM user_group_members m
               WHERE m.user_id = u.id AND m.group_id = ?)
           RETURNING 1""",
        [everyone_id, everyone_id],
    ).fetchall()
    if rows:
        logger.info(
            "system-plugin reconcile: added %d account(s) to the Everyone group, "
            "which now means every account on this instance",
            len(rows),
        )
    return len(rows)


def _flags_become_grants(conn, everyone_id: str) -> int:
    """0098's step 4, minus the scope column this backend does not have.

    ``admin_disabled = FALSE`` is part of the match, not tidiness: both
    readers of the flag filtered on it, so a disabled plugin marked system
    reached NOBODY. Writing it a grant would be this reconciliation widening
    access by one plugin.
    """
    from src.grant_sources import SYSTEM_PLUGIN_MIGRATION

    system_rows = conn.execute(
        "SELECT marketplace_id, name FROM marketplace_plugins WHERE is_system = TRUE AND admin_disabled = FALSE"
    ).fetchall()

    for marketplace_id, plugin_name in system_rows:
        resource_id = f"{marketplace_id}/{plugin_name}"
        existing = conn.execute(
            "SELECT id, requirement FROM resource_grants "
            "WHERE group_id = ? AND resource_type = 'marketplace_plugin' AND resource_id = ?",
            [everyone_id, resource_id],
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO resource_grants "
                "(id, group_id, resource_type, resource_id, requirement, assigned_by) "
                "VALUES (?, ?, 'marketplace_plugin', ?, 'required', ?)",
                [str(uuid.uuid4()), everyone_id, resource_id, SYSTEM_PLUGIN_MIGRATION],
            )
        elif (existing[1] or "available") != "required":
            # A hand-set grant on the carrier for a plugin the flag ALREADY
            # made mandatory for everyone. Upgrading the tier preserves reach
            # rather than changing it; leaving it Optional would let a member
            # drop a plugin they could not drop yesterday.
            conn.execute(
                "UPDATE resource_grants SET requirement = 'required' WHERE id = ?",
                [existing[0]],
            )

    # Clear the flag LAST. This is what makes the next boot cheap and stops
    # the column contradicting the grants. `assigned_by` carries the
    # provenance instead, because this backend has no `source` column either.
    if system_rows:
        conn.execute("UPDATE marketplace_plugins SET is_system = FALSE WHERE is_system = TRUE")
        logger.info(
            "system-plugin reconcile: %d plugin(s) that were 'system' now reach "
            "everyone through a required grant instead",
            len(system_rows),
        )
    else:
        # Nothing to convert, but a disabled-and-flagged row would keep the
        # column disagreeing with the grants forever. Clear those too.
        conn.execute("UPDATE marketplace_plugins SET is_system = FALSE WHERE is_system = TRUE")
    return len(system_rows)


def reconcile_system_plugin_flags() -> bool:
    """Run the DuckDB half of 0098. Idempotent; returns True iff it changed
    anything.

    Fail-soft by design, like every other boot-time seeder: an instance that
    cannot reconcile should still start and serve, loudly logged, rather than
    refuse to boot. The cost of the soft failure is bounded — the flag is
    still set, so the next boot retries.
    """
    from src.repositories import use_pg

    if use_pg():
        # 0098 owns this on Postgres, transactionally, and has already run by
        # the time anything calls us.
        return False

    from src.db import get_system_db

    conn = None
    try:
        conn = get_system_db()
        if not _column_exists(conn, "marketplace_plugins", "is_system"):
            return False
        everyone_id = _everyone_group_id(conn)
        if not everyone_id:
            logger.warning(
                "system-plugin reconcile: the Everyone system group is missing; "
                "skipping. Nothing has been changed, and the next boot retries."
            )
            return False

        converted = _convert_workspace_narrowing(conn, everyone_id)
        if converted:
            _make_everyone_universal(conn, everyone_id)
        changed = _flags_become_grants(conn, everyone_id)
        return converted or bool(changed)
    except Exception:
        logger.exception(
            "system-plugin reconcile failed; plugins that were marked 'system' may "
            "not reach everyone until this succeeds. Retried on the next boot."
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing must not mask the above
                pass
