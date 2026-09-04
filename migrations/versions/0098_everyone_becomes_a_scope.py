"""Everyone becomes a scope; ``marketplace_plugins.is_system`` is retired, not dropped.

Three spellings of one idea collapse into one. Before this revision an
instance could say "everyone gets this" in three ways that did not agree:

- a grant on the seeded ``Everyone`` group — every account, UNLESS
  ``AGNES_GROUP_EVERYONE_EMAIL`` mirrored that group from a Workspace group,
  in which case a subset;
- ``marketplace_plugins.is_system`` — every account, unconditionally, so on
  a mirrored instance it reached people the group grant did not;
- a ``requirement='required'`` grant on ``Everyone`` — the same as the flag
  on four of five behaviours (see the effort's ticket 03).

Afterwards there is one: ``resource_grants.scope = 'everyone'``.

THE GOVERNING RULE: an upgrade must never change who can see what. Note the
word PERSON: the audience this revision creates reaches accounts a person
signs in as, never a service account and never the identities Agnes seeds
for itself (issue #2256). There is exactly one place that costs anything.
``marketplace_plugins.is_system`` reached every row in ``users``
unconditionally, so step 4's conversion takes a system plugin away from
those non-person accounts — a Claude Code bundle a headless identity never
installs, and an admin who wants one there puts the account in a named
group, which is how it gets everything else it has. Every step below is
chosen so the set of (person, thing) pairs is identical before and after, and the step ORDER is part of that — step 2 has to run before
anything reads ``Everyone`` as unconditional, because doing it the other way
round is exactly how an upgrade widens access on a mirrored instance.
``tests/db_pg/test_alembic_0098_everyone_scope.py`` snapshots the effective
pair set on both instance shapes and requires them identical.

Steps, in order:

1. (revision 0097) add the column. Additive, behaviour-neutral, safe under
   the old code.
1b. Name the seeded system identities (``kind='system'``). A prerequisite,
   not a tidy-up: step 3 refuses to convert while any account sits outside
   the group, and these three never join it, so without this the guard
   fires on every instance and the revision converts nothing at all.
2. Convert the Workspace narrowing. Where ``AGNES_GROUP_EVERYONE_EMAIL`` is
   set, ``Everyone`` was never everyone — it was a real group wearing the
   word. It becomes one: a group named after the Workspace group it mirrored,
   holding the same members with their ``source`` preserved so the sync keeps
   writing them, and holding the grants that were written against the
   pseudo-group. Nobody gains or loses access.
3. Grants still on ``Everyone`` become ``scope='everyone'`` — for the types
   where "everyone" is a coherent audience, and only once the group is
   verified to hold every PERSON and no one else. Both directions are
   guarded: a person outside would gain the group's grants, and a service
   account inside would lose them, since the scope reaches people only. After step 2 on a mirrored instance there
   are none of those left; the rows moved. On every other instance these are
   the genuinely instance-wide ones. The four withheld types
   (``SCOPE_WITHHELD_TYPES``) keep their rows as ordinary grants on the
   group, which reaches the same people. So does everything, on the rare
   instance where some account sits outside the group — see the step's
   docstring for why that is a skip rather than a conversion.
4. Each ``is_system`` plugin becomes one everyone-scoped, required grant,
   and the flag is then cleared so the column cannot contradict it. Then the rows the OLD fanout wrote are dropped, matched
   by ``source='marketplace_required'`` (0096) — an exact match, not a
   guess. Rows with NULL ``source`` are LEFT ALONE: they predate the column,
   cannot be told from a grant an admin typed, and deleting a hand-set grant
   would narrow access. In practice no row carries that source — the fanout
   was removed in the same change that added the column — so the DELETE is a
   safety net rather than the mechanism; the redundant pre-0096 rows simply
   survive as ordinary, now-visible grants.
5. NOT HERE. Dropping ``marketplace_plugins.is_system`` is a separate
   RELEASE, not merely a later revision — see MID-FLIGHT below. The column
   survives this migration, dead and unread.

IRREVERSIBLE IN PRACTICE. ``downgrade`` restores the columns and the
plugin marks, but not step 2: which of the converted group's members were
originally ``Everyone``'s is not recorded, and on pre-0096 data the
distinction between a fanned-out row and a hand-set one is gone for good.
The release note says so rather than implying a clean rollback.

MID-FLIGHT, and this is why the flag is still here. Every step below is safe
to run while OLD code is still serving:

- steps 2 and 3 — an ``Everyone`` grant and an everyone-scoped grant reach the
  same people while every account is auto-joined to ``Everyone``;
- step 4's INSERT — an extra required grant on the carrier reaches exactly who
  the flag already reached, so an old replica serves the same set either way.

``DROP COLUMN is_system`` is the one statement that is NOT, and it is
deliberately absent. An earlier draft of this revision dropped it here and
told the operator to "sequence the release so the readers land before the
writer changes". Nothing in this repo can do that:

- ``src/db_pg.py::ensure_pg_at_head`` self-migrates to ``head`` at app boot
  by default, so the FIRST container on the new image runs the whole
  revision, drop included;
- running ``alembic upgrade head`` from a pre-deploy pipeline step
  (``docs/migrations.md``) is worse, not better — the drop then lands before
  any container is recreated;
- ``scripts/ops/agnes-auto-upgrade.sh``'s role-split recreate takes
  worker+gateway first and then walks the ``api`` replicas ONE AT A TIME,
  each gated on its own ``/readyz`` — a window of minutes in which old-code
  replicas are still serving and still selecting ``is_system``. After the
  drop that is a Postgres ``UndefinedColumn``, not a graceful degrade.
- ``docs/migrations.md`` lists expand/contract discipline — the mechanism
  that would make a staged drop safe — under future work, not landed.

So this is the EXPAND half. The contract half ships in a later release, once
the fleet has converged on code that does not read the column. Until then the
column sits unread on Postgres, and unread on DuckDB too, where the ladder is
frozen and could not have dropped it anyway.
"""

import logging
import os
import uuid
from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision: str = "0098_everyone_becomes_a_scope"
down_revision: Union[str, None] = "0097_resource_grants_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Mirrors ``src.db.SYSTEM_EVERYONE_GROUP``. Inlined: a migration must keep
#: describing the schema it shipped against even after the constant moves.
EVERYONE_GROUP_NAME = "Everyone"

#: Mirrors ``src.grant_scopes.EVERYONE``.
SCOPE_EVERYONE = "everyone"

#: Mirrors ``src.grant_scopes.SCOPE_WITHHELD_TYPES`` — inlined for the same
#: reason as the constants above. These four types do not take an
#: everyone-scope, so step 3 leaves their rows as ordinary grants on the
#: seeded group. That is still reach-preserving: the group holds every
#: account again once the narrowing is converted.
SCOPE_WITHHELD_TYPES = ("slack_channel", "table", "memory_domain", "memory_item")

#: A subset of the above, and the reason step 2 needs one. A
#: ``slack_channel`` row on the seeded ``Everyone`` group is not an audience
#: grant at all — it is how a channel is marked open, and the only group id
#: the enforcement path looks at
#: (``services/slack_bot/binding.is_channel_allowlisted``). Repointing it to
#: the converted group would silently switch Agnes off in every channel an
#: admin had enabled.
MARKER_TYPES = ("slack_channel",)

#: Mirrors ``src.grant_sources``. ``system_plugin_migration`` is new in this
#: revision: it tells an admin why a grant nobody typed exists, which a
#: source-less row could not.
SOURCE_MARKETPLACE_FANOUT = "marketplace_required"
SOURCE_SYSTEM_PLUGIN_MIGRATION = "system_plugin_migration"

#: What ``app/auth/group_sync.py`` stamps on a group it creates for a synced
#: Workspace group. The converted group has to look like one of those, or the
#: next sign-in creates a SECOND group for the same Workspace email.
SYNC_CREATED_BY = "system:google-sync"

ENV_EVERYONE_EMAIL = "AGNES_GROUP_EVERYONE_EMAIL"

#: Mirrors ``src.service_accounts`` — inlined for the same reason as the
#: constants above. ``'human'`` is 0096's server default, so every row that
#: was not created through ``create_service_account`` already carries it.
HUMAN_KIND = "human"
SERVICE_ACCOUNT_KIND = "service"
SYSTEM_IDENTITY_KIND = "system"
SYSTEM_IDENTITY_EMAILS = (
    "scheduler@system.local",
    "semantic-drafter@system.local",
    "memory-curator@system.local",
)


def _everyone_group_id(conn) -> Optional[str]:
    row = conn.execute(
        sa.text(f"SELECT id FROM user_groups WHERE name = '{EVERYONE_GROUP_NAME}'")  # noqa: S608
    ).first()
    return row[0] if row else None


def _step1_mark_system_identities(conn) -> None:
    """Name the identities Agnes seeded for itself, before anything counts
    accounts (issue #2256).

    Three rows — the scheduler, the semantic drafter, the memory curator —
    are created by the instance for its own use, through a bare
    ``users.create()`` that never joins them to any group. They are not
    people, and they are on every instance that has booted, which is what
    makes this a prerequisite rather than a tidy-up: step 3 below refuses to
    convert while a single account sits outside the group, so without this
    step that guard fires everywhere and the whole revision converts nothing.

    Not ``kind='service'``. That kind carries two behaviours these rows
    cannot survive — the semantic drafter mints an interactive token through
    the broker, and the scheduler is a member of the Admin group.
    """
    conn.execute(
        sa.text("UPDATE users SET kind = :k WHERE lower(email) IN :emails").bindparams(
            sa.bindparam("emails", expanding=True)
        ),
        {"k": SYSTEM_IDENTITY_KIND, "emails": list(SYSTEM_IDENTITY_EMAILS)},
    )


def _step2_convert_workspace_narrowing(conn) -> None:
    """``Everyone`` was a subset here. Give the subset its own name.

    No-op unless ``AGNES_GROUP_EVERYONE_EMAIL`` is set — on every other
    instance ``Everyone`` already meant every account and there is nothing
    to convert.
    """
    everyone_email = os.environ.get(ENV_EVERYONE_EMAIL, "").strip().lower()
    if not everyone_email:
        return

    everyone_id = _everyone_group_id(conn)
    if not everyone_id:
        return

    # Reuse a group of that name if one exists. The sync creates groups by
    # email, so an operator who changed the mapping may already have one —
    # and creating a second would split the audience in half.
    row = conn.execute(
        sa.text("SELECT id FROM user_groups WHERE name = :n"),
        {"n": everyone_email},
    ).first()
    if row:
        target_id = row[0]
    else:
        target_id = uuid.uuid4().hex
        conn.execute(
            sa.text(
                "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                "VALUES (:id, :n, :d, FALSE, :cb)"
            ),
            {
                "id": target_id,
                "n": everyone_email,
                "d": (
                    "Was mapped to the Everyone system group by "
                    f"{ENV_EVERYONE_EMAIL}. Converted to an ordinary "
                    "Workspace-synced group; membership still comes from "
                    "the same Workspace group."
                ),
                "cb": SYNC_CREATED_BY,
            },
        )

    # `slack_channel` rows stay behind. See MARKER_TYPES: they name a
    # channel, not an audience, and the allowlist check reads them off this
    # exact group id.
    #
    # Members move with their `source` intact, so the nightly sync keeps
    # ownership of the rows it owns and an admin-added member stays
    # admin-added. ALL of them move, not just the synced ones: the grants
    # move too, so a row left behind in `Everyone` would lose them.
    conn.execute(
        sa.text(
            "INSERT INTO user_group_members (user_id, group_id, source, added_at, added_by) "
            "SELECT user_id, :target, source, added_at, added_by "
            "FROM user_group_members WHERE group_id = :src "
            "ON CONFLICT (user_id, group_id) DO NOTHING"
        ),
        {"target": target_id, "src": everyone_id},
    )
    conn.execute(
        sa.text("DELETE FROM user_group_members WHERE group_id = :src"),
        {"src": everyone_id},
    )

    # Grants repoint. Where the target already holds the same resource, the
    # duplicate is dropped rather than updated — but the surviving row keeps
    # the STRONGER tier first, or a Required grant would silently become
    # Optional and stop landing in those people's workspaces.
    movable = "resource_type NOT IN :markers"
    params = {
        "target": target_id,
        "src": everyone_id,
        "markers": tuple(MARKER_TYPES),
    }
    conn.execute(
        sa.text(
            "UPDATE resource_grants t SET requirement = 'required' "
            f"WHERE t.group_id = :target AND t.{movable} AND EXISTS ("
            "  SELECT 1 FROM resource_grants s "
            "  WHERE s.group_id = :src "
            "    AND s.resource_type = t.resource_type "
            "    AND s.resource_id = t.resource_id "
            "    AND s.requirement = 'required')"
        ).bindparams(sa.bindparam("markers", expanding=True)),
        params,
    )
    conn.execute(
        sa.text(
            "DELETE FROM resource_grants s "
            f"WHERE s.group_id = :src AND s.{movable} AND EXISTS ("
            "  SELECT 1 FROM resource_grants t "
            "  WHERE t.group_id = :target "
            "    AND t.resource_type = s.resource_type "
            "    AND t.resource_id = s.resource_id)"
        ).bindparams(sa.bindparam("markers", expanding=True)),
        params,
    )
    conn.execute(
        sa.text(f"UPDATE resource_grants SET group_id = :target WHERE group_id = :src AND {movable}").bindparams(
            sa.bindparam("markers", expanding=True)
        ),
        params,
    )


def _step3_scope_everyone_grants(conn) -> None:
    """What is left on ``Everyone`` genuinely reaches every account — say so
    in the column, for the types where saying it means anything.

    GUARDED, and the guard is the governing rule doing its job. "Genuinely"
    has to be checked, not assumed: if a single account is not a member of
    the group, converting its grants to ``scope='everyone'`` hands that
    account everything the group had. Rare — no API path can remove a
    ``system_seed`` membership (``remove_member`` takes
    ``require_source='admin'``), so it takes direct SQL or an instance that
    was mirrored when the account was created and un-mirrored later — but
    "rare" is not the standard. The standard is never.

    When the guard fires, the grants stay on the group and reach exactly who
    they reached before. The instance is left with the old shape for those
    rows, deliberately: an operator can add the missing accounts to the
    group and a later release converts them, and until then nobody has
    gained or lost anything. The warning names the count so the choice is
    visible rather than discovered.
    """
    everyone_id = _everyone_group_id(conn)
    if not everyone_id:
        return

    # Nothing to convert is not a decision worth a warning. On a mirrored
    # instance step 2 has just emptied this group, so the guard below would
    # fire on every such upgrade and say nothing true about it.
    convertible = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM resource_grants "
            "WHERE group_id = :g AND scope IS NULL AND resource_type NOT IN :withheld"
        ).bindparams(sa.bindparam("withheld", expanding=True)),
        {"g": everyone_id, "withheld": list(SCOPE_WITHHELD_TYPES)},
    ).scalar_one()
    if not convertible:
        return

    # Widening arm: a PERSON outside the group would gain everything it
    # holds. Only people — a service account and a seeded system identity
    # are not in the everyone audience (#2256), so their absence from the
    # group is the normal state and never a reason to refuse.
    outside = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM users u WHERE u.kind = :human AND NOT EXISTS ("
            "  SELECT 1 FROM user_group_members m "
            "  WHERE m.user_id = u.id AND m.group_id = :g)"
        ),
        {"g": everyone_id, "human": HUMAN_KIND},
    ).scalar_one()
    if outside:
        logger.warning(
            "0098: %d account(s) are not members of the %r group, so its grants "
            "were NOT converted to scope='everyone' — doing so would have given "
            "those accounts access they do not have today. They remain ordinary "
            "group grants and reach exactly who they reach now. Add the missing "
            "accounts to the group if they should have it; a later release "
            "converts the rows once the group holds every account.",
            outside,
            EVERYONE_GROUP_NAME,
        )
        return

    # Narrowing arm, and the reason the guard has two. An admin may add a
    # service account to this group — nothing refuses it, and the people
    # picker offers it deliberately, because a group is the only way a
    # headless identity acquires any authority at all. Such an account holds
    # these grants TODAY. Converting them to a scope that reaches only people
    # would take them away, which is the governing rule broken in the other
    # direction, and the redesigned Access page no longer draws this group's
    # roster to put them back from.
    non_people_inside = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM users u "
            "JOIN user_group_members m ON m.user_id = u.id AND m.group_id = :g "
            "WHERE u.kind <> :human"
        ),
        {"g": everyone_id, "human": HUMAN_KIND},
    ).scalar_one()
    if non_people_inside:
        logger.warning(
            "0098: %d non-person account(s) — service accounts or seeded system "
            "identities — are members of the %r group and hold its grants today. "
            "Those grants were NOT converted to scope='everyone', which reaches "
            "people only, because converting would have taken the access away. "
            "They remain ordinary group grants and reach exactly who they reach "
            "now. Move those accounts to a named group of their own if they "
            "should keep the access; a later release converts the rows once the "
            "group holds people only.",
            non_people_inside,
            EVERYONE_GROUP_NAME,
        )
        return

    conn.execute(
        sa.text(
            "UPDATE resource_grants SET scope = :s "
            "WHERE group_id = :g AND scope IS NULL "
            "  AND resource_type NOT IN :withheld"
        ).bindparams(sa.bindparam("withheld", expanding=True)),
        {"s": SCOPE_EVERYONE, "g": everyone_id, "withheld": list(SCOPE_WITHHELD_TYPES)},
    )


def _carrier_group_id(conn) -> str:
    """``group_id`` for an everyone-scoped grant, creating the seeded row if
    this instance somehow lacks it.

    The column is NOT NULL, so an everyone-grant still needs a group to hang
    on; every one of them using the SAME group is what leaves
    ``UNIQUE (group_id, resource_type, resource_id)`` enforcing one
    everyone-grant per resource. Creating the row beats failing the upgrade
    or, worse, skipping step 4 and quietly taking a mandatory plugin away
    from everybody.
    """
    existing = _everyone_group_id(conn)
    if existing:
        return existing
    new_id = uuid.uuid4().hex
    conn.execute(
        sa.text(
            "INSERT INTO user_groups (id, name, description, is_system, created_by) "
            "VALUES (:id, :n, 'System: default group', TRUE, 'system:seed')"
        ),
        {"id": new_id, "n": EVERYONE_GROUP_NAME},
    )
    return new_id


def _step4_system_flag_becomes_a_grant(conn) -> None:
    """The cutover. One flag becomes one grant, then the fanout's rows go.

    ``admin_disabled = FALSE`` is part of the match, not a tidy-up: both
    readers of the flag (``list_granted_for_groups`` for visibility,
    ``marketplace_filter.required_plugin_keys`` for the tier) filter on it,
    so a disabled plugin marked system reaches NOBODY today. Writing a grant
    for it would be this migration widening access by one plugin.
    """
    system_rows = conn.execute(
        sa.text(
            "SELECT marketplace_id, name FROM marketplace_plugins WHERE is_system = TRUE AND admin_disabled = FALSE"
        )
    ).all()

    if system_rows:
        carrier = _carrier_group_id(conn)
        for marketplace_id, plugin_name in system_rows:
            conn.execute(
                sa.text(
                    "INSERT INTO resource_grants "
                    "(id, group_id, resource_type, resource_id, requirement, source, scope) "
                    "VALUES (:id, :g, 'marketplace_plugin', :rid, 'required', :src, :scope) "
                    "ON CONFLICT (group_id, resource_type, resource_id) DO UPDATE SET "
                    "  requirement = 'required', scope = :scope"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "g": carrier,
                    "rid": f"{marketplace_id}/{plugin_name}",
                    "src": SOURCE_SYSTEM_PLUGIN_MIGRATION,
                    "scope": SCOPE_EVERYONE,
                },
            )

    # The old fanout's rows, matched exactly. `source` is preserved on the
    # DO UPDATE above precisely so a row an admin typed keeps saying so.
    conn.execute(
        sa.text("DELETE FROM resource_grants WHERE source = :src"),
        {"src": SOURCE_MARKETPLACE_FANOUT},
    )

    # Clear the flag LAST, and clear it even where nothing was converted (a
    # disabled-and-flagged row). The column outlives this migration — the
    # drop is a later release — so leaving TRUE behind would leave the column
    # and the grants disagreeing about who gets the plugin, with nothing
    # reading the column to notice.
    #
    # Safe mid-flight, which the DROP was not: an old replica reads
    # `is_system = TRUE OR EXISTS (grant)` for visibility and unions
    # `list_system_keys()` into the tier. Flipping TRUE to FALSE moves a
    # plugin from the first half of that OR to the second, and step 4 has
    # just written the grant that satisfies it.
    conn.execute(sa.text("UPDATE marketplace_plugins SET is_system = FALSE WHERE is_system = TRUE"))


def upgrade() -> None:
    conn = op.get_bind()
    _step1_mark_system_identities(conn)
    _step2_convert_workspace_narrowing(conn)
    _step3_scope_everyone_grants(conn)
    _step4_system_flag_becomes_a_grant(conn)
    # No `op.drop_column("marketplace_plugins", "is_system")`. See MID-FLIGHT
    # in the module docstring: it is the only statement here an old replica
    # cannot survive, and this repo has no way to stage it within a release.


def downgrade() -> None:
    """Re-flag the plugins and un-scope the grants. Step 2 does NOT come back —
    see the module docstring.

    No ``add_column`` here: ``upgrade`` never dropped it.
    """
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE marketplace_plugins mp SET is_system = TRUE WHERE EXISTS ("
            "  SELECT 1 FROM resource_grants rg "
            "  WHERE rg.resource_type = 'marketplace_plugin' "
            "    AND rg.resource_id = mp.marketplace_id || '/' || mp.name "
            "    AND rg.scope = :scope "
            "    AND rg.source = :src)"
        ),
        {"scope": SCOPE_EVERYONE, "src": SOURCE_SYSTEM_PLUGIN_MIGRATION},
    )
    conn.execute(
        sa.text("DELETE FROM resource_grants WHERE source = :src AND scope = :scope"),
        {"src": SOURCE_SYSTEM_PLUGIN_MIGRATION, "scope": SCOPE_EVERYONE},
    )
    # Everyone-scoped grants are already stored against the carrier group, so
    # clearing the scope hands them back to it unchanged.
    conn.execute(
        sa.text("UPDATE resource_grants SET scope = NULL WHERE scope = :scope"),
        {"scope": SCOPE_EVERYONE},
    )
