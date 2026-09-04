"""The governing rule on the backend Alembic never reaches.

`tests/db_pg/test_alembic_0098_everyone_scope.py` snapshots the effective
(person, thing) set across migration 0098 and requires it identical. That
test runs on Postgres, because that is where the migration lives — which
means it proved the rule exactly where it was never at risk.

A DuckDB app-state instance runs no Alembic and its own ladder is frozen
(A3), so the same guarantee has to come from
`src.system_plugin_reconcile.reconcile_system_plugin_flags` at boot. This
module holds it to the same standard: snapshot who reaches what, reconcile,
snapshot again, require identical — through the real read paths this time
(`list_granted_for_groups`, `required_plugin_keys`) rather than a hand-written
model of them, since on this backend there is no migration to model.

Three shapes, and the middle one is the whole reason this module exists:

1. no `AGNES_GROUP_EVERYONE_EMAIL` — the common case;
2. WITH it set, where the seeded group held a SUBSET of the accounts, so
   retiring the narrowing without converting the data first would quietly
   drop every account that was in it;
3. a hand-set grant beside the flag — the row the reconciliation must not
   touch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Set, Tuple


def _conn(tmp_path: Path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    return conn


def _everyone_id(conn) -> str:
    row = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone' AND is_system").fetchone()
    assert row, "the seeded Everyone group is missing"
    return row[0]


def _add_user(conn, uid: str) -> str:
    conn.execute(
        "INSERT INTO users (id, email, name) VALUES (?, ?, ?)",
        [uid, f"{uid}@example.com", uid],
    )
    return uid


def _add_group(conn, name: str, created_by: str = "test") -> str:
    import uuid

    gid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO user_groups (id, name, is_system, created_by) VALUES (?, ?, FALSE, ?)",
        [gid, name, created_by],
    )
    return gid


def _add_member(conn, uid: str, gid: str, source: str = "system_seed") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO user_group_members (user_id, group_id, source, added_by) VALUES (?, ?, ?, 'test')",
        [uid, gid, source],
    )


def _add_plugin(conn, marketplace_id: str, name: str, *, is_system: bool, disabled: bool = False) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
        [
            marketplace_id,
            marketplace_id,
            f"https://example.test/{marketplace_id}.git",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ],
    )
    conn.execute(
        "INSERT INTO marketplace_plugins (marketplace_id, name, is_system, admin_disabled) VALUES (?, ?, ?, ?)",
        [marketplace_id, name, is_system, disabled],
    )


def _add_grant(conn, gid: str, resource_type: str, resource_id: str, requirement: str = "available") -> str:
    import uuid

    grant_id = str(uuid.uuid4())
    per_type = {
        "data_package": "resource_id_data_package",
        "memory_domain": "resource_id_memory_domain",
    }.get(resource_type)
    cols = ["id", "group_id", "resource_type", "resource_id", "requirement"]
    vals = [grant_id, gid, resource_type, resource_id, requirement]
    if per_type:
        cols.append(per_type)
        vals.append(resource_id)
    conn.execute(
        f"INSERT INTO resource_grants ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})",
        vals,
    )
    return grant_id


#: (user_id, marketplace_id, plugin_name) -> "required" | "available"
Reach = Dict[Tuple[str, str, str], str]


def _plugin_reach(conn, user_ids, *, old_rules: bool = False) -> Reach:
    """Who is served which plugin, and at which tier.

    The grant half runs through the REAL reads —
    `list_granted_for_groups` is the visibility chokepoint every served
    surface funnels through, and `required_plugin_keys` is the tier — so this
    cannot pass by agreeing with a wrong model of itself.

    The FLAG half cannot, and that is the point. `old_rules=True` adds back
    what `marketplace_plugins.is_system` used to mean: every account, at the
    required tier, with no grant row anywhere saying so. Asking the current
    readers for the "before" picture would answer that the flag reaches
    nobody — true of the new code, and exactly the change under test — so the
    before side has to be modelled, the same way the Postgres migration test
    models it. Sourced from `git show HEAD~2:src/repositories/`
    `marketplace_plugins.py` (`mp.is_system = TRUE OR ...`, no group filter)
    and `src/marketplace_filter.py` (`list_system_keys()` unioned in).
    """
    from src.marketplace_filter import required_plugin_keys
    from src.repositories.marketplace_plugins import MarketplacePluginsRepository

    plugins = MarketplacePluginsRepository(conn)
    out: Reach = {}
    flagged = []
    if old_rules:
        flagged = [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT marketplace_id, name FROM marketplace_plugins "
                "WHERE is_system = TRUE AND admin_disabled = FALSE"
            ).fetchall()
        ]
    for uid in user_ids:
        gids = [
            r[0] for r in conn.execute("SELECT group_id FROM user_group_members WHERE user_id = ?", [uid]).fetchall()
        ]
        required = required_plugin_keys(conn, uid)
        for row in plugins.list_granted_for_groups(gids):
            key = (uid, row["marketplace_id"], row["name"])
            out[key] = "required" if (row["marketplace_id"], row["name"]) in required else "available"
        for mid, name in flagged:
            out[(uid, mid, name)] = "required"
    return out


def _other_reach(conn, user_ids) -> Set[Tuple[str, str, str]]:
    """Non-plugin grant reach, straight off the junction — the half the
    Workspace-narrowing conversion moves.

    `slack_channel` is excluded for the same reason the Postgres migration
    test excludes it: such a grant reaches no PERSON. It marks a channel
    open, and `services/slack_bot/binding.is_channel_allowlisted` reads it
    off the seeded group's id without consulting anybody's membership.
    Counting it as person-reach would make the membership backfill look like
    a widening — an account joining the group would "gain" a channel marker —
    when what actually happens is that the channel stays exactly as open as
    it was. The marker itself is asserted directly, in
    `test_reconcile_leaves_the_slack_channel_marker_on_the_seeded_group`.
    """
    out: Set[Tuple[str, str, str]] = set()
    for uid in user_ids:
        for r in conn.execute(
            """SELECT rg.resource_type, rg.resource_id
               FROM resource_grants rg
               JOIN user_group_members m ON m.group_id = rg.group_id
               WHERE m.user_id = ?
                 AND rg.resource_type NOT IN ('marketplace_plugin', 'slack_channel')""",
            [uid],
        ).fetchall():
            out.add((uid, r[0], r[1]))
    return out


def _seed(conn, *, narrowed: bool, hand_set: bool = False) -> dict:
    everyone = _everyone_id(conn)
    analysts = _add_group(conn, "analysts@example.com", created_by="system:google-sync")

    users = [_add_user(conn, "u-alice"), _add_user(conn, "u-bob")]
    for uid in users:
        _add_member(conn, uid, everyone, source="google_sync" if narrowed else "system_seed")
    _add_member(conn, users[1], analysts, source="google_sync")
    # On a narrowed instance this account is NOT in the mirrored group, which
    # is the normal state there and the one the conversion must preserve.
    outsider = _add_user(conn, "u-carol")
    if not narrowed:
        _add_member(conn, outsider, everyone, source="system_seed")
    users.append(outsider)

    conn.execute("INSERT INTO data_packages (id, slug, name) VALUES ('pkg-1', 'pkg-1', 'Pkg')")
    _add_grant(conn, everyone, "data_package", "pkg-1", requirement="required")
    _add_grant(conn, everyone, "slack_channel", "C0123ABCD")
    _add_grant(conn, analysts, "data_package", "pkg-1")

    _add_plugin(conn, "acme", "mandatory", is_system=True)
    _add_plugin(conn, "acme", "optional", is_system=False)
    _add_plugin(conn, "acme", "hidden", is_system=True, disabled=True)
    _add_grant(conn, analysts, "marketplace_plugin", "acme/optional")

    hand_set_id = None
    if hand_set:
        hand_set_id = _add_grant(conn, analysts, "marketplace_plugin", "acme/mandatory")

    return {"everyone": everyone, "analysts": analysts, "users": users, "hand_set": hand_set_id}


def _run(tmp_path, monkeypatch, *, narrowed: bool, hand_set: bool = False):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    if narrowed:
        monkeypatch.setenv("AGNES_GROUP_EVERYONE_EMAIL", "everyone@example.com")
    else:
        monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)

    conn = _conn(tmp_path)
    ids = _seed(conn, narrowed=narrowed, hand_set=hand_set)
    users = ids["users"]

    before = (_plugin_reach(conn, users, old_rules=True), _other_reach(conn, users))

    # Hand out CURSORS, not the connection itself — that is what the real
    # `get_system_db` returns, precisely so a caller can close() its handle
    # without closing the shared connection. Passing the raw connection here
    # would make the reconciler's (correct) close() tear down the fixture.
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn.cursor())
    monkeypatch.setattr("src.db.get_system_db", lambda: conn.cursor(), raising=False)
    import src.system_plugin_reconcile as mod

    changed = mod.reconcile_system_plugin_flags()

    after = (_plugin_reach(conn, users), _other_reach(conn, users))
    return before, after, ids, conn, changed


def _diff(before, after) -> str:
    bp, bo = before
    ap, ao = after
    return (
        f"\nPLUGIN gained: {sorted(set(ap) - set(bp))}"
        f"\nPLUGIN lost:   {sorted(set(bp) - set(ap))}"
        f"\nPLUGIN tier changed: {sorted((k, bp[k], ap[k]) for k in bp if k in ap and bp[k] != ap[k])}"
        f"\nOTHER gained:  {sorted(ao - bo)}"
        f"\nOTHER lost:    {sorted(bo - ao)}"
    )


def test_reconcile_preserves_reach_without_the_workspace_narrowing(tmp_path, monkeypatch):
    """The common instance shape."""
    before, after, _ids, conn, changed = _run(tmp_path, monkeypatch, narrowed=False)
    assert before[0], "snapshot is empty — the seed did not take, so this proves nothing"
    assert after == before, _diff(before, after)
    assert changed is True, "the flag should have been converted"
    conn.close()


def test_reconcile_preserves_reach_with_the_workspace_narrowing(tmp_path, monkeypatch):
    """The shape this module exists for.

    Here the seeded group held a subset (``u-carol`` is outside it), so the
    old `is_system` flag reached MORE people than any grant on that group
    could. Converting the flag without first converting the narrowing would
    have handed the plugin to fewer people than had it.
    """
    before, after, _ids, conn, changed = _run(tmp_path, monkeypatch, narrowed=True)
    assert before[0], "snapshot is empty — the seed did not take, so this proves nothing"
    assert after == before, _diff(before, after)
    assert changed is True
    conn.close()


def test_reconcile_leaves_a_hand_set_grant_alone(tmp_path, monkeypatch):
    before, after, ids, conn, _changed = _run(tmp_path, monkeypatch, narrowed=False, hand_set=True)
    assert after == before, _diff(before, after)

    row = conn.execute("SELECT group_id, requirement FROM resource_grants WHERE id = ?", [ids["hand_set"]]).fetchone()
    assert row is not None, "the reconciliation deleted a hand-set grant"
    assert row[0] == ids["analysts"], "the hand-set grant was repointed"
    conn.close()


def test_reconcile_is_idempotent(tmp_path, monkeypatch):
    """Boot-time, so it runs on every boot. The second pass must be a no-op —
    which is what clearing the flag last buys."""
    _before, after, _ids, conn, _changed = _run(tmp_path, monkeypatch, narrowed=False)
    import src.system_plugin_reconcile as mod

    assert mod.reconcile_system_plugin_flags() is False, "a second pass claimed it changed something"
    # New rules on both sides now — the flag is cleared, so there is nothing
    # left for `old_rules` to add back.
    users = [r[0] for r in conn.execute("SELECT id FROM users").fetchall()]
    assert (_plugin_reach(conn, users), _other_reach(conn, users)) == after, "a second pass changed reach"
    conn.close()


def test_reconcile_does_not_grant_a_disabled_plugin(tmp_path, monkeypatch):
    """`is_system` AND `admin_disabled` reached nobody, so granting it would
    be this reconciliation widening access by one plugin."""
    _before, _after, ids, conn, _changed = _run(tmp_path, monkeypatch, narrowed=False)
    granted = {
        r[0]
        for r in conn.execute(
            "SELECT resource_id FROM resource_grants WHERE resource_type = 'marketplace_plugin' AND group_id = ?",
            [ids["everyone"]],
        ).fetchall()
    }
    assert "acme/mandatory" in granted
    assert "acme/hidden" not in granted, "a disabled plugin was granted"
    conn.close()


def test_reconcile_leaves_the_slack_channel_marker_on_the_seeded_group(tmp_path, monkeypatch):
    """A `slack_channel` grant marks a CHANNEL open and the allowlist check
    reads it off the seeded group's id. Repointing it switches Agnes off in
    every channel an admin enabled."""
    _before, _after, ids, conn, _changed = _run(tmp_path, monkeypatch, narrowed=True)
    left = {
        r[0]
        for r in conn.execute(
            "SELECT resource_type FROM resource_grants WHERE group_id = ?", [ids["everyone"]]
        ).fetchall()
    }
    assert "slack_channel" in left, "the channel marker was repointed away from Everyone"
    assert "data_package" not in left, "an audience grant was left behind"
    conn.close()


def test_reconcile_makes_everyone_universal_only_after_converting_the_narrowing(tmp_path, monkeypatch):
    """The ordering that is easy to get backwards.

    On a narrowed instance the seeded group's grants belong to the mirrored
    subset. Adding every account to it BEFORE moving those grants out would
    hand them to the whole instance — a widening, not a conversion. So the
    membership backfill is gated on the conversion having run, and this pins
    both halves: after a narrowed reconcile the group holds every account AND
    none of the subset's audience grants.
    """
    _before, _after, ids, conn, _changed = _run(tmp_path, monkeypatch, narrowed=True)

    members = {
        r[0]
        for r in conn.execute("SELECT user_id FROM user_group_members WHERE group_id = ?", [ids["everyone"]]).fetchall()
    }
    all_users = {r[0] for r in conn.execute("SELECT id FROM users").fetchall()}
    assert members == all_users, "Everyone does not hold every account, so it cannot mean everyone"

    audience_grants = {
        r[0]
        for r in conn.execute(
            "SELECT resource_type FROM resource_grants WHERE group_id = ? "
            "AND resource_type NOT IN ('slack_channel', 'marketplace_plugin')",
            [ids["everyone"]],
        ).fetchall()
    }
    assert audience_grants == set(), (
        "the narrowed group's audience grants are still on Everyone, which now holds "
        "every account — that is the widening the ordering exists to prevent"
    )
    conn.close()


def test_reconcile_does_not_backfill_everyone_on_a_non_narrowed_instance(tmp_path, monkeypatch):
    """The gate on the backfill, from the other side.

    Here the seeded group keeps its grants, so adding a missing account to it
    would hand that account those grants. The reconciliation must leave
    membership alone — the same refusal migration 0098's step-3 guard makes
    on Postgres.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)

    conn = _conn(tmp_path)
    everyone = _everyone_id(conn)
    conn.execute("INSERT INTO data_packages (id, slug, name) VALUES ('pkg-1', 'pkg-1', 'Pkg')")
    _add_grant(conn, everyone, "data_package", "pkg-1", requirement="required")
    inside = _add_user(conn, "u-inside")
    _add_member(conn, inside, everyone)
    outside = _add_user(conn, "u-outside")  # deliberately no membership

    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn.cursor())
    monkeypatch.setattr("src.db.get_system_db", lambda: conn.cursor(), raising=False)
    import src.system_plugin_reconcile as mod

    mod.reconcile_system_plugin_flags()

    reached = _other_reach(conn, [outside])
    assert reached == set(), (
        "an account outside the seeded group was backfilled into it and gained its "
        "grants — the backfill must only run where the conversion emptied the group"
    )
    assert (outside, "data_package", "pkg-1") not in reached
    assert (inside, "data_package", "pkg-1") in _other_reach(conn, [inside])
    conn.close()
