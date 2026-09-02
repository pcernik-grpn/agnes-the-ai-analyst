"""Alembic 0098 — an upgrade must never change who can see what.

THE ONE CHECK THAT TESTS THE GOVERNING RULE. Everything else about this
migration is a unit test; this snapshots the effective ``(person, thing,
tier)`` set on a seeded instance, runs the migration, snapshots it again, and
requires the two to be IDENTICAL. A migration that widens access is a defect
even when the model it produces is correct — and one that narrows it is too.

The two snapshots are written as raw SQL over the tables rather than by
calling the application's resolvers, deliberately. The resolvers changed in
this same commit; asking them whether they still agree with themselves would
prove nothing. These state the OLD rules and the NEW rules independently, in
the smallest form that captures them:

- **Before.** A grant reaches the members of its ``group_id``. A plugin
  flagged ``is_system`` reaches EVERY account, at the required tier, without
  any grant row.
- **After.** A grant with no ``scope`` reaches the members of its
  ``group_id``. A grant with ``scope='everyone'`` reaches every account, and
  its ``group_id`` (the carrier) is ignored.

Both apply the one filter that is genuinely unchanged on either side of the
migration: an ``admin_disabled`` plugin is served to nobody regardless of
grants.

Three instance shapes, because only the differences between them exercise
the ordering that makes this migration safe:

1. no ``AGNES_GROUP_EVERYONE_EMAIL`` — the common case;
2. with it set — the only shape that runs step 2, the Workspace-narrowing
   conversion, whose ORDER relative to step 3 is what keeps a mirrored
   instance from silently widening to every account;
3. a hand-set grant on a system plugin — the row step 4 must not touch.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Dict, Tuple

import sqlalchemy as sa


REPO_ROOT = Path(__file__).resolve().parents[2]

BEFORE_REVISION = "0097_resource_grants_scope"
AFTER_REVISION = "0098_everyone_becomes_a_scope"

#: A (user_id, resource_type, resource_id) -> tier map. `required` wins over
#: `available` when a person is reached twice, which is what the resolvers do.
Pairs = Dict[Tuple[str, str, str], str]


def _alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = db_url
    return cfg


def _collapse(rows) -> Pairs:
    """Strongest tier per (person, thing). Two grants reaching one person is
    ordinary — a group grant and an everyone grant, say — and the tier the
    resolvers honour is the stronger of the two."""
    out: Pairs = {}
    for user_id, rtype, rid, req in rows:
        key = (user_id, rtype, rid)
        tier = req or "available"
        if out.get(key) == "required":
            continue
        out[key] = tier
    return out


#: What a (person, thing) snapshot must not pretend to cover.
#:
#: An ``admin_disabled`` plugin is served to nobody whatever the grants say —
#: identical on both sides of the migration, and stated in both snapshots so
#: neither can claim reach the serve path does not give.
#:
#: A ``slack_channel`` grant reaches no PERSON at all. It marks a channel
#: open, and the enforcement path
#: (``services/slack_bot/binding.is_channel_allowlisted``) reads it off the
#: seeded group's id without consulting anybody's membership. Counting it as
#: person-reach would make step 2 look like a narrowing on a mirrored
#: instance — the members move, the marker deliberately does not — when what
#: actually happens is that the channel stays open. The marker is asserted
#: directly in the step-2 test instead.
_REACH_EXCLUSIONS = """
    rtype <> 'slack_channel'
    AND NOT (
        rtype = 'marketplace_plugin'
        AND EXISTS (
            SELECT 1 FROM marketplace_plugins mp
            WHERE mp.marketplace_id || '/' || mp.name = rid
              AND mp.admin_disabled = TRUE
        )
    )
"""


def _pairs_before(conn) -> Pairs:
    """The effective set under the OLD rules (group grants + the flag)."""
    sql = f"""
    SELECT user_id, rtype, rid, req FROM (
        SELECT m.user_id       AS user_id,
               rg.resource_type AS rtype,
               rg.resource_id   AS rid,
               rg.requirement   AS req
        FROM resource_grants rg
        JOIN user_group_members m ON m.group_id = rg.group_id

        UNION ALL

        -- `is_system` reached every account, at the required tier, with no
        -- grant row anywhere saying so. That is the whole reason the flag
        -- and the grants could disagree about who "everyone" was.
        SELECT u.id, 'marketplace_plugin',
               mp.marketplace_id || '/' || mp.name, 'required'
        FROM users u
        CROSS JOIN marketplace_plugins mp
        WHERE mp.is_system = TRUE AND mp.admin_disabled = FALSE
    ) reach
    WHERE {_REACH_EXCLUSIONS}
    """
    return _collapse(conn.execute(sa.text(sql)).all())


def _pairs_after(conn) -> Pairs:
    """The effective set under the NEW rules (scope on the grant)."""
    sql = f"""
    SELECT user_id, rtype, rid, req FROM (
        SELECT m.user_id, rg.resource_type, rg.resource_id, rg.requirement
        FROM resource_grants rg
        JOIN user_group_members m ON m.group_id = rg.group_id
        WHERE rg.scope IS NULL

        UNION ALL

        -- An everyone-scoped grant reaches every account. `group_id` is a
        -- carrier and is deliberately not joined on: an account in no group
        -- at all is reached, which is what the old group model could not say.
        SELECT u.id, rg.resource_type, rg.resource_id, rg.requirement
        FROM resource_grants rg
        CROSS JOIN users u
        WHERE rg.scope = 'everyone'
    ) reach(user_id, rtype, rid, req)
    WHERE {_REACH_EXCLUSIONS}
    """
    return _collapse(conn.execute(sa.text(sql)).all())


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


def _seed_system_groups(conn) -> Dict[str, str]:
    """Alembic does not seed Admin/Everyone — the app does, at boot. Do what
    it does, so the migration meets the instance shape it will meet live."""
    ids: Dict[str, str] = {}
    for name, description in (("Admin", "System: full access"), ("Everyone", "System: default group")):
        conn.execute(
            sa.text(
                "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                "VALUES (:id, :n, :d, TRUE, 'system:seed') "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"id": uuid.uuid4().hex, "n": name, "d": description},
        )
        ids[name] = conn.execute(sa.text("SELECT id FROM user_groups WHERE name = :n"), {"n": name}).scalar_one()
    return ids


def _add_user(conn, label: str) -> str:
    uid = f"u-{label}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        sa.text("INSERT INTO users (id, email, name) VALUES (:id, :e, :n)"),
        {"id": uid, "e": f"{uid}@example.com", "n": label},
    )
    return uid


def _add_member(conn, user_id: str, group_id: str, source: str = "system_seed") -> None:
    conn.execute(
        sa.text(
            "INSERT INTO user_group_members (user_id, group_id, source, added_by) "
            "VALUES (:u, :g, :s, 'test') ON CONFLICT (user_id, group_id) DO NOTHING"
        ),
        {"u": user_id, "g": group_id, "s": source},
    )


def _add_group(conn, name: str, created_by: str = "admin@example.com") -> str:
    gid = uuid.uuid4().hex
    conn.execute(
        sa.text("INSERT INTO user_groups (id, name, is_system, created_by) VALUES (:id, :n, FALSE, :cb)"),
        {"id": gid, "n": name, "cb": created_by},
    )
    return gid


#: The five types that ALSO carry a per-type FK column (migration 0013), and
#: the column each uses. A CHECK constraint enforces "exactly this one is
#: non-NULL", so a grant on one of them cannot be inserted the short way, and
#: the FK means its parent row has to exist first.
_PER_TYPE_COLUMN = {
    "table": "resource_id_table",
    "data_package": "resource_id_data_package",
    "memory_domain": "resource_id_memory_domain",
    "memory_item": "resource_id_memory_item",
    "recipe": "resource_id_recipe",
}


def _add_grant(
    conn,
    group_id: str,
    resource_type: str,
    resource_id: str,
    requirement: str = "available",
    source: str | None = None,
) -> str:
    grant_id = uuid.uuid4().hex
    cols = ["id", "group_id", "resource_type", "resource_id", "requirement", "source"]
    vals = [":id", ":g", ":rt", ":ri", ":req", ":src"]
    per_type = _PER_TYPE_COLUMN.get(resource_type)
    if per_type:
        cols.append(per_type)
        vals.append(":ri")
    conn.execute(
        sa.text(f"INSERT INTO resource_grants ({', '.join(cols)}) VALUES ({', '.join(vals)})"),
        {"id": grant_id, "g": group_id, "rt": resource_type, "ri": resource_id, "req": requirement, "src": source},
    )
    return grant_id


def _add_data_package(conn, package_id: str) -> None:
    conn.execute(
        sa.text("INSERT INTO data_packages (id, slug, name) VALUES (:id, :id, :id)"),
        {"id": package_id},
    )


def _add_memory_domain(conn, domain_id: str) -> None:
    conn.execute(
        sa.text("INSERT INTO memory_domains (id, slug, name) VALUES (:id, :id, :id)"),
        {"id": domain_id},
    )


def _add_plugin(conn, marketplace_id: str, name: str, *, is_system: bool, disabled: bool = False) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO marketplace_registry (id, name, url) "
            "VALUES (:id, :id, 'https://example.com/m.git') ON CONFLICT (id) DO NOTHING"
        ),
        {"id": marketplace_id},
    )
    conn.execute(
        sa.text(
            "INSERT INTO marketplace_plugins (marketplace_id, name, is_system, admin_disabled) VALUES (:m, :n, :s, :d)"
        ),
        {"m": marketplace_id, "n": name, "s": is_system, "d": disabled},
    )


def _seed_instance(
    engine,
    *,
    hand_set_grant_on_system_plugin: bool = False,
    include_account_outside_everyone: bool = False,
) -> Dict[str, str]:
    """One instance, carrying every shape this migration has to preserve.

    Returned ids let a caller assert on specific rows afterwards; the
    snapshot comparison itself needs none of them.
    """
    with engine.begin() as conn:
        groups = _seed_system_groups(conn)
        everyone, admin = groups["Everyone"], groups["Admin"]
        analysts = _add_group(conn, "analysts@example.com", created_by="system:google-sync")

        # Two people in Everyone, one of them also in a real group and in
        # Admin. `include_account_outside_everyone` adds a third who is in NO
        # group — the account whose reach an unguarded step 3 would widen,
        # and the normal state of a non-mirrored account on a mirrored
        # instance.
        u_plain = _add_user(conn, "plain")
        u_analyst = _add_user(conn, "analyst")
        _add_member(conn, u_plain, everyone)
        _add_member(conn, u_analyst, everyone)
        _add_member(conn, u_analyst, analysts, source="google_sync")
        _add_member(conn, u_analyst, admin)
        u_outside = _add_user(conn, "outside") if include_account_outside_everyone else ""

        # Grants on Everyone, across the tiers and across a type that DOES
        # take an everyone-scope and one that does not.
        _add_data_package(conn, "agnes-usage")
        _add_memory_domain(conn, "dom-1")
        _add_grant(conn, everyone, "chat", "chat", source="chat_seed")
        _add_grant(conn, everyone, "data_package", "agnes-usage", requirement="required")
        # `slack_channel` on Everyone is a marker, not an audience — the
        # allowlist check reads it off this exact group id.
        _add_grant(conn, everyone, "slack_channel", "C0123ABCD")
        # A withheld type that IS membership-scoped: stays a group grant.
        _add_grant(conn, everyone, "memory_domain", "dom-1")
        # A grant on a real group, to prove step 2 leaves other groups alone.
        _add_grant(conn, analysts, "collection", "col-analysts", requirement="required")

        _add_plugin(conn, "acme", "mandatory", is_system=True)
        _add_plugin(conn, "acme", "optional", is_system=False)
        # is_system AND disabled: reaches nobody today (both readers filter
        # admin_disabled), so step 4 must not write it a grant.
        _add_plugin(conn, "acme", "hidden", is_system=True, disabled=True)
        _add_grant(conn, analysts, "marketplace_plugin", "acme/optional")

        hand_set = None
        if hand_set_grant_on_system_plugin:
            # The row step 4 must not touch: a grant an admin typed, on a
            # plugin that also carries the flag. It has no `source` (the
            # column postdates most real rows), which is exactly why the
            # migration cannot tell it from a fanned-out one — and so leaves
            # every source-less row alone.
            hand_set = _add_grant(conn, analysts, "marketplace_plugin", "acme/mandatory")

    return {
        "everyone": everyone,
        "admin": admin,
        "analysts": analysts,
        "u_plain": u_plain,
        "u_analyst": u_analyst,
        "u_outside": u_outside,
        "hand_set_grant": hand_set or "",
    }


# ---------------------------------------------------------------------------
# the governing-rule tests
# ---------------------------------------------------------------------------


def _run(
    pg_engine,
    monkeypatch,
    *,
    everyone_email: str | None,
    hand_set: bool = False,
    account_outside_everyone: bool = False,
):
    """Seed at 0097, snapshot, upgrade to 0098, snapshot. Returns both plus
    the seeded ids."""
    from alembic import command

    if everyone_email:
        monkeypatch.setenv("AGNES_GROUP_EVERYONE_EMAIL", everyone_email)
    else:
        monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, BEFORE_REVISION)
    ids = _seed_instance(
        pg_engine,
        hand_set_grant_on_system_plugin=hand_set,
        include_account_outside_everyone=account_outside_everyone,
    )

    with pg_engine.connect() as conn:
        before = _pairs_before(conn)

    command.upgrade(cfg, AFTER_REVISION)

    with pg_engine.connect() as conn:
        after = _pairs_after(conn)
    return before, after, ids


def _diff_message(before: Pairs, after: Pairs) -> str:
    gained = {k: v for k, v in after.items() if k not in before}
    lost = {k: v for k, v in before.items() if k not in after}
    changed = {k: (before[k], after[k]) for k in before if k in after and before[k] != after[k]}
    return (
        f"\nWIDENED (access the upgrade added): {sorted(gained.items())}"
        f"\nNARROWED (access the upgrade removed): {sorted(lost.items())}"
        f"\nTIER CHANGED: {sorted(changed.items())}"
    )


def test_0098_preserves_effective_access_without_the_workspace_narrowing(pg_engine, monkeypatch):
    """The common instance shape. Steps 3 and 4 only."""
    before, after, _ = _run(pg_engine, monkeypatch, everyone_email=None)
    assert before, "snapshot is empty — the seed did not take, so this proves nothing"
    assert after == before, _diff_message(before, after)


def test_0098_preserves_effective_access_with_the_workspace_narrowing(pg_engine, monkeypatch):
    """The mirrored shape — the only one that runs step 2.

    ``Everyone`` here is a SUBSET of the accounts on the instance (the seed
    leaves ``u_outside`` out of it), so this is the shape where reading
    Everyone as unconditional before converting it would hand that account
    everything the group had.
    """
    before, after, _ = _run(
        pg_engine,
        monkeypatch,
        everyone_email="everyone@example.com",
        account_outside_everyone=True,
    )
    assert before, "snapshot is empty — the seed did not take, so this proves nothing"
    assert after == before, _diff_message(before, after)


def test_0098_preserves_effective_access_with_a_hand_set_grant_on_a_system_plugin(pg_engine, monkeypatch):
    """The row step 4 must not touch, and does not."""
    before, after, ids = _run(pg_engine, monkeypatch, everyone_email=None, hand_set=True)
    assert after == before, _diff_message(before, after)

    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT group_id, requirement, scope, source FROM resource_grants WHERE id = :id"),
            {"id": ids["hand_set_grant"]},
        ).first()
    assert row is not None, "step 4 deleted a hand-set grant — that is a narrowing"
    assert row[0] == ids["analysts"], "the hand-set grant was repointed"
    assert row[2] is None, "the hand-set grant was given an everyone-scope it never had"
    assert row[3] is None, "the hand-set grant's (absent) provenance was invented"


# ---------------------------------------------------------------------------
# step-by-step: what the migration produced, not only that reach held
# ---------------------------------------------------------------------------


def test_0098_step2_converts_the_narrowing_to_a_named_synced_group(pg_engine, monkeypatch):
    """The mapping becomes a group an admin recognises, still Workspace-fed.

    Named after the Workspace email — not a generated name — and carrying
    ``created_by='system:google-sync'`` so ``apply_user_groups`` recognises
    it as one of its own and keeps writing its membership instead of
    creating a second group for the same email.
    """
    email = "everyone@example.com"
    _, _, ids = _run(pg_engine, monkeypatch, everyone_email=email)

    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT id, created_by, is_system FROM user_groups WHERE name = :n"),
            {"n": email},
        ).first()
        assert row is not None, "the converted group was not created"
        target_id, created_by, is_system = row
        assert created_by == "system:google-sync"
        assert is_system is False, "the converted group is an ordinary audience, not a system row"

        members = {
            r[0]: r[1]
            for r in conn.execute(
                sa.text("SELECT user_id, source FROM user_group_members WHERE group_id = :g"),
                {"g": target_id},
            ).all()
        }
        assert ids["u_plain"] in members and ids["u_analyst"] in members
        assert ids["u_outside"] not in members, "conversion invented a member"

        # Audience grants moved; the slack_channel marker did not.
        moved = {
            r[0]
            for r in conn.execute(
                sa.text("SELECT resource_type FROM resource_grants WHERE group_id = :g"),
                {"g": target_id},
            ).all()
        }
        assert {"chat", "data_package", "memory_domain"} <= moved
        assert "slack_channel" not in moved, (
            "a slack_channel grant was repointed — the allowlist check reads it "
            "off the Everyone group id, so this switches Agnes off in the channel"
        )

        left_behind = {
            r[0]
            for r in conn.execute(
                sa.text("SELECT resource_type FROM resource_grants WHERE group_id = :g"),
                {"g": ids["everyone"]},
            ).all()
        }
        # The marker, plus step 4's everyone-scoped plugin grant, which uses
        # this group as its CARRIER rather than as an audience.
        assert left_behind == {"slack_channel", "marketplace_plugin"}

        # Nothing is everyone-scoped on a mirrored instance: what was on
        # `Everyone` was never everyone, and step 3 finds nothing to convert.
        scoped = conn.execute(sa.text("SELECT COUNT(*) FROM resource_grants WHERE scope = 'everyone'")).scalar_one()
        assert scoped == 1, "only the ex-is_system plugin grant should be everyone-scoped here"


def test_0098_step3_scopes_only_the_types_that_take_an_audience(pg_engine, monkeypatch):
    """``scope`` says something about a ``chat`` grant and nothing about a
    ``slack_channel`` one, so only the first gets it."""
    _, _, ids = _run(pg_engine, monkeypatch, everyone_email=None)

    with pg_engine.connect() as conn:
        rows = {
            r[0]: r[1]
            for r in conn.execute(
                sa.text("SELECT resource_type, scope FROM resource_grants WHERE group_id = :g"),
                {"g": ids["everyone"]},
            ).all()
        }
    assert rows["chat"] == "everyone"
    assert rows["data_package"] == "everyone"
    # Withheld — each for its own reason; see grant_scopes.SCOPE_WITHHELD_TYPES.
    assert rows["slack_channel"] is None
    assert rows["memory_domain"] is None


def test_0098_step4_turns_the_flag_into_one_required_everyone_grant(pg_engine, monkeypatch):
    """One grant per system plugin, at the tier the flag meant — and none for
    a disabled one, which reached nobody."""
    _, _, ids = _run(pg_engine, monkeypatch, everyone_email=None)

    with pg_engine.connect() as conn:
        rows = {
            r[0]: (r[1], r[2], r[3], r[4])
            for r in conn.execute(
                sa.text(
                    "SELECT resource_id, group_id, requirement, scope, source "
                    "FROM resource_grants WHERE resource_type = 'marketplace_plugin' "
                    "AND scope = 'everyone'"
                )
            ).all()
        }
    assert set(rows) == {"acme/mandatory"}, (
        "expected exactly the enabled system plugin; a disabled one reaches "
        "nobody today, so granting it would be a widening"
    )
    group_id, requirement, scope, source = rows["acme/mandatory"]
    assert group_id == ids["everyone"], "the carrier should be the seeded group"
    assert requirement == "required"
    assert scope == "everyone"
    assert source == "system_plugin_migration", "an admin meeting this grant is owed its provenance"


def test_0098_step4_drops_fanned_out_rows_but_never_source_less_ones(pg_engine, monkeypatch):
    """``source='marketplace_required'`` is exact; NULL is not, so NULL stays.

    No live row actually carries that source — the fanout was removed in the
    same change that added the column — so this pins the safety net rather
    than a path with traffic. The half that matters in production is the
    second assertion: a source-less row survives.
    """
    from alembic import command

    monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, BEFORE_REVISION)
    ids = _seed_instance(pg_engine)

    with pg_engine.begin() as conn:
        fanned = _add_grant(
            conn,
            ids["analysts"],
            "marketplace_plugin",
            "acme/optional-2",
            requirement="required",
            source="marketplace_required",
        )
        source_less = _add_grant(conn, ids["admin"], "marketplace_plugin", "acme/optional-3")

    command.upgrade(cfg, AFTER_REVISION)

    with pg_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT 1 FROM resource_grants WHERE id = :id"), {"id": fanned}).first() is None, (
            "a row the old fanout wrote survived"
        )
        assert (
            conn.execute(sa.text("SELECT 1 FROM resource_grants WHERE id = :id"), {"id": source_less}).first()
            is not None
        ), "a source-less row was deleted — it cannot be told from a hand-set grant"


def test_0098_leaves_the_is_system_column_in_place_but_inert(pg_engine, monkeypatch):
    """The flag is dead but PRESENT, and that is deliberate.

    An earlier draft dropped it here. Nothing in this repo can stage a column
    drop within a release: ``ensure_pg_at_head`` self-migrates to head at
    boot, and the role-split recreate walks the ``api`` replicas one at a
    time — so old code would still be selecting ``is_system`` after it was
    gone and get ``UndefinedColumn``, not a graceful degrade. This is the
    EXPAND half; the drop ships as its own release once the fleet has
    converged on readers that ignore it.

    So what matters is that the column is INERT, not absent: no row still
    carries TRUE, and nothing can read a stale mandatory state out of it.
    """
    _run(pg_engine, monkeypatch, everyone_email=None)
    with pg_engine.connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = 'marketplace_plugins'")
            ).all()
        }
        still_flagged = conn.execute(
            sa.text("SELECT COUNT(*) FROM marketplace_plugins WHERE is_system = TRUE")
        ).scalar_one()
    assert "is_system" in cols, (
        "the drop belongs in a later release — an old replica mid-rollout still selects this column"
    )
    assert "admin_disabled" in cols, "availability is a different question and stays"
    assert still_flagged == 0, (
        "a plugin is still flagged after the cutover, so the column and the grants "
        "disagree about who gets it"
    )


def test_0098_downgrade_restores_the_flag_and_unscopes_the_grants(pg_engine, monkeypatch):
    """A rollback gets the columns back. It does NOT get step 2 back — see
    the revision docstring; the release note says so rather than implying a
    clean rollback."""
    from alembic import command

    monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, BEFORE_REVISION)
    _seed_instance(pg_engine)
    command.upgrade(cfg, AFTER_REVISION)
    command.downgrade(cfg, BEFORE_REVISION)

    with pg_engine.connect() as conn:
        flagged = {
            r[0] for r in conn.execute(sa.text("SELECT name FROM marketplace_plugins WHERE is_system = TRUE")).all()
        }
        assert flagged == {"mandatory"}, "the flag did not come back on the plugin that carried it"
        remaining = conn.execute(sa.text("SELECT COUNT(*) FROM resource_grants WHERE scope IS NOT NULL")).scalar_one()
        assert remaining == 0, "a scope survived a downgrade to before the column meant anything"


def test_0098_leaves_everyone_grants_group_scoped_when_an_account_sits_outside(pg_engine, monkeypatch):
    """The guard on step 3 — the governing rule refusing a conversion.

    An account outside the seeded group means the group is not everyone, and
    scoping its grants would hand that account all of them. So the rows stay
    as they are, reaching exactly who they reached. Preserving access beats
    completing the model.
    """
    before, after, ids = _run(pg_engine, monkeypatch, everyone_email=None, account_outside_everyone=True)
    assert after == before, _diff_message(before, after)

    with pg_engine.connect() as conn:
        scopes = {
            r[0]: r[1]
            for r in conn.execute(
                sa.text("SELECT resource_type, scope FROM resource_grants WHERE group_id = :g"),
                {"g": ids["everyone"]},
            ).all()
        }
    assert scopes["chat"] is None, "step 3 converted a grant on a group that is not everyone"
    assert scopes["data_package"] is None
    # Step 4 is NOT guarded, and must not be: `is_system` already reached
    # every account unconditionally, including this one, so an
    # everyone-scoped grant reaches exactly the same people.
    assert scopes["marketplace_plugin"] == "everyone"


def test_0098_an_everyone_scope_reaches_an_account_added_after_the_upgrade(pg_engine, monkeypatch):
    """The capability the group model could not express.

    Not a preservation check — this one asserts the migration bought
    something. An account created afterwards, before anything writes it a
    membership, is reached by every everyone-scoped grant and by no group
    grant. Under the old model only ``is_system`` reached it, and a default
    like "everyone can use chat" missed it until a membership row landed.
    """
    _, _, ids = _run(pg_engine, monkeypatch, everyone_email=None)
    with pg_engine.begin() as conn:
        newcomer = _add_user(conn, "newcomer")
    with pg_engine.connect() as conn:
        after = _pairs_after(conn)

    reached = {(rt, rid) for (uid, rt, rid) in after if uid == newcomer}
    assert ("chat", "chat") in reached, "an everyone-scoped grant did not reach a memberless account"
    assert ("marketplace_plugin", "acme/mandatory") in reached
    assert ("collection", "col-analysts") not in reached, "a group grant leaked to a non-member"
    assert ids["everyone"], "sanity: the seeded group still exists as the carrier"
