#!/usr/bin/env python3
"""Snapshot who can see what, on a REAL instance, either side of 0098.

The governing rule of the everyone-scope change is that **an upgrade must
never change who can see what**. The test suites assert that too — on both
backends, across three instance shapes — but they assert it about data they
invented. A seeded fixture contains what its author thought to put in it,
which is precisely the blind spot a real instance exposes: the grant on a
resource type nobody considered, the account outside every group, the
plugin flagged in 2026 and forgotten.

So this is the check that actually tests the rule, and it is meant to be
run by an operator against a COPY of a live instance before the release
goes out:

    # 1. take a copy. Postgres:
    pg_dump "$LIVE_URL" | psql "$COPY_URL"
    #    or DuckDB: cp -a /data /data-copy

    # 2. snapshot the copy BEFORE upgrading it
    scripts/verify_everyone_scope_migration.py snapshot \\
        --url "$COPY_URL" --out before.json

    # 3. upgrade the copy (alembic on PG; boot the new image on DuckDB, which
    #    runs src/system_plugin_reconcile at startup)
    AGNES_DB_URL="$COPY_URL" alembic upgrade head

    # 4. snapshot again, and compare
    scripts/verify_everyone_scope_migration.py snapshot \\
        --url "$COPY_URL" --out after.json
    scripts/verify_everyone_scope_migration.py compare before.json after.json

``compare`` exits non-zero on ANY difference and prints the (person, thing)
pairs gained and lost. A gain is a widening and a loss is a narrowing;
neither is acceptable, and the release should not go out on a diff.

WHY THE RULES ARE SPELLED OUT HERE rather than imported: ``snapshot`` has
to run against BOTH the old code's semantics and the new code's, and the
application only ever implements one of them at a time. Importing the
resolvers would make step 2 answer with whichever version happens to be
checked out — and on the old code that is the same question step 4 asks,
so the two snapshots would agree by construction and prove nothing. That
mistake was made once already while writing the DuckDB test for this and
is worth stating loudly.

Detection is automatic, and NOT by looking for the column: ``0097`` adds
``resource_grants.scope`` and ``0098`` does the data work, so a "before"
snapshot is taken with the column already present. On Postgres this asks
Alembic whether ``0098`` is in the current revision's ancestry; on DuckDB,
which runs no Alembic, the marker is whether any plugin still carries the
``is_system`` flag the reconciler clears last.

Read-only. It opens the database, SELECTs, and writes a JSON file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Tuple


# --------------------------------------------------------------------------
# The two rule sets, as SQL. Neither imports application code — see the
# module docstring for why that is the whole point.
# --------------------------------------------------------------------------

#: Plugins nobody is served whatever the grants say. Identical on both sides
#: of the migration, and applied on both so a diff can never be an artifact
#: of one side modelling the serve path and the other not.
_DISABLED_PLUGIN_EXCLUSION = """
    NOT (
        rtype = 'marketplace_plugin'
        AND EXISTS (
            SELECT 1 FROM marketplace_plugins mp
            WHERE mp.marketplace_id || '/' || mp.name = rid
              AND mp.admin_disabled = TRUE
        )
    )
"""

#: A `slack_channel` grant reaches no PERSON — it marks a channel open, and
#: the allowlist check reads it off the seeded group's id without consulting
#: anybody's membership. Counting it as person-reach would make the
#: membership conversion look like a change when the channel stays exactly
#: as open as it was. Its own invariant is checked separately, below.
_MARKER_EXCLUSION = "rtype <> 'slack_channel'"

_BEFORE_SQL = f"""
SELECT user_id, rtype, rid, req FROM (
    SELECT m.user_id        AS user_id,
           rg.resource_type AS rtype,
           rg.resource_id   AS rid,
           rg.requirement   AS req
    FROM resource_grants rg
    JOIN user_group_members m ON m.group_id = rg.group_id

    UNION ALL

    -- `marketplace_plugins.is_system` reached EVERY account, at the required
    -- tier, with no grant row anywhere saying so. That is the whole reason
    -- the flag and the grants could disagree about who "everyone" was.
    SELECT u.id, 'marketplace_plugin',
           mp.marketplace_id || '/' || mp.name, 'required'
    FROM users u
    CROSS JOIN marketplace_plugins mp
    WHERE mp.is_system = TRUE AND mp.admin_disabled = FALSE
) reach
WHERE {_MARKER_EXCLUSION} AND {_DISABLED_PLUGIN_EXCLUSION}
"""

_AFTER_SQL_PG = f"""
SELECT user_id, rtype, rid, req FROM (
    SELECT m.user_id, rg.resource_type, rg.resource_id, rg.requirement
    FROM resource_grants rg
    JOIN user_group_members m ON m.group_id = rg.group_id
    WHERE rg.scope IS NULL

    UNION ALL

    -- An everyone-scoped grant reaches every account. `group_id` is a
    -- carrier and is deliberately NOT joined on: an account in no group at
    -- all is reached, which is what the old group model could not say.
    SELECT u.id, rg.resource_type, rg.resource_id, rg.requirement
    FROM resource_grants rg
    CROSS JOIN users u
    WHERE rg.scope = 'everyone'
) reach(user_id, rtype, rid, req)
WHERE {_MARKER_EXCLUSION} AND {_DISABLED_PLUGIN_EXCLUSION}
"""

#: The DuckDB ladder is frozen, so that backend has no `scope` column: an
#: everyone-grant is an ordinary grant on the carrier group, which
#: `src/system_plugin_reconcile.py` has made hold every account. So the
#: after-rules there are just the group join — the same shape as the first
#: half of the PG query, with nothing to union.
_AFTER_SQL_DUCKDB = f"""
SELECT user_id, rtype, rid, req FROM (
    SELECT m.user_id, rg.resource_type, rg.resource_id, rg.requirement
    FROM resource_grants rg
    JOIN user_group_members m ON m.group_id = rg.group_id
) reach(user_id, rtype, rid, req)
WHERE {_MARKER_EXCLUSION} AND {_DISABLED_PLUGIN_EXCLUSION}
"""

#: Asserted separately from person-reach, because it is not person-reach: the
#: set of channels Agnes answers in must not change either.
_CHANNELS_SQL = """
SELECT rg.resource_id
FROM resource_grants rg
JOIN user_groups g ON g.id = rg.group_id
WHERE rg.resource_type = 'slack_channel' AND g.name = 'Everyone'
"""


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


def _is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://", "postgresql+"))


def _pg_rows(url: str, sql: str) -> List[tuple]:
    import sqlalchemy as sa

    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(sa.text(sql)).all()]
    finally:
        engine.dispose()


#: The revision that does the DATA work. `0097` merely adds the column, so
#: the presence of `resource_grants.scope` says nothing about which side of
#: the change an instance is on — a "before" snapshot is taken at `0097`,
#: with the column already there. Asking Alembic is the only precise answer.
DATA_REVISION = "0098_everyone_becomes_a_scope"


def _pg_has_run_0098(url: str) -> bool:
    """Whether `0098` is in the ancestry of this database's current revision.

    Ancestry, not equality: `0098` is head today and will not be forever,
    and a check that silently starts answering "before" for every instance
    once `0099` lands would make this whole script quietly useless.
    """
    rows = _pg_rows(url, "SELECT version_num FROM alembic_version")
    if not rows:
        return False
    current = rows[0][0]

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(repo_root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    script = ScriptDirectory.from_config(cfg)
    for rev in script.walk_revisions("base", current):
        if rev.revision == DATA_REVISION:
            return True
    return False


def _duckdb_rows(path: str, sql: str) -> List[tuple]:
    import duckdb

    conn = duckdb.connect(path, read_only=True)
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _duckdb_still_flagged(path: str) -> bool:
    """Whether any plugin still carries `is_system`.

    DuckDB runs no Alembic, so there is no version table to ask. The
    reconciler clears the flag as its last act, which makes the flag itself
    the marker. The COLUMN survives either way — the drop is a later release.

    One instance shape this cannot distinguish: one that never had a system
    plugin. There the flag is absent on both sides, and this reads "after"
    even before the reconciler runs — which is harmless, because on such an
    instance the two rule sets return the same rows anyway. `compare`'s
    opposite-sides check will refuse the pair and say so rather than
    reporting a false pass.
    """
    rows = _duckdb_rows(path, "SELECT COUNT(*) FROM marketplace_plugins WHERE is_system = TRUE")
    return bool(rows and rows[0][0])


# --------------------------------------------------------------------------
# Snapshot / compare
# --------------------------------------------------------------------------


def _collapse(rows) -> Dict[str, str]:
    """Strongest tier per (person, thing). Being reached twice is ordinary —
    a group grant and an everyone grant, say — and the tier the resolvers
    honour is the stronger of the two."""
    out: Dict[str, str] = {}
    for user_id, rtype, rid, req in rows:
        key = f"{user_id}\x1f{rtype}\x1f{rid}"
        if out.get(key) == "required":
            continue
        out[key] = req or "available"
    return out


def snapshot(url: str) -> dict:
    if _is_postgres(url):
        after = _pg_has_run_0098(url)
        sql = _AFTER_SQL_PG if after else _BEFORE_SQL
        pairs = _collapse(_pg_rows(url, sql))
        channels = sorted(r[0] for r in _pg_rows(url, _CHANNELS_SQL))
        backend = "postgres"
    else:
        after = not _duckdb_still_flagged(url)
        sql = _AFTER_SQL_DUCKDB if after else _BEFORE_SQL
        pairs = _collapse(_duckdb_rows(url, sql))
        channels = sorted(r[0] for r in _duckdb_rows(url, _CHANNELS_SQL))
        backend = "duckdb"
    return {
        "backend": backend,
        "side": "after" if after else "before",
        "rules": "new" if after else "old",
        "pairs": pairs,
        "open_slack_channels": channels,
        "counts": {"pairs": len(pairs), "channels": len(channels)},
    }


def compare(before: dict, after: dict) -> Tuple[bool, List[str]]:
    problems: List[str] = []

    if before.get("side") != "before" or after.get("side") != "after":
        problems.append(
            f"the two snapshots are not opposite sides of the migration "
            f"(got {before.get('side')!r} then {after.get('side')!r}) — "
            f"comparing a database to itself proves nothing"
        )
    if before.get("backend") != after.get("backend"):
        problems.append(f"different backends ({before.get('backend')} vs {after.get('backend')})")

    bp, ap = before.get("pairs", {}), after.get("pairs", {})
    if not bp:
        problems.append(
            "the BEFORE snapshot is empty — nothing was reached by anything, so "
            "an identical AFTER would prove nothing. Wrong database?"
        )

    gained = sorted(set(ap) - set(bp))
    lost = sorted(set(bp) - set(ap))
    changed = sorted(k for k in bp if k in ap and bp[k] != ap[k])

    for key in gained:
        u, t, r = key.split("\x1f")
        problems.append(f"WIDENED: {u} now reaches {t}:{r} ({ap[key]})")
    for key in lost:
        u, t, r = key.split("\x1f")
        problems.append(f"NARROWED: {u} no longer reaches {t}:{r} (was {bp[key]})")
    for key in changed:
        u, t, r = key.split("\x1f")
        problems.append(f"TIER CHANGED: {u} on {t}:{r}: {bp[key]} -> {ap[key]}")

    bc = set(before.get("open_slack_channels", []))
    ac = set(after.get("open_slack_channels", []))
    for cid in sorted(bc - ac):
        problems.append(f"SLACK CHANNEL CLOSED: {cid} — Agnes stops answering there")
    for cid in sorted(ac - bc):
        problems.append(f"SLACK CHANNEL OPENED: {cid}")

    return (not problems), problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="write a reach snapshot of one database")
    s.add_argument(
        "--url",
        default=os.environ.get("AGNES_DB_URL", ""),
        help="Postgres URL, or a path to a system.duckdb file. Defaults to $AGNES_DB_URL.",
    )
    s.add_argument("--out", required=True, help="where to write the JSON snapshot")

    c = sub.add_parser("compare", help="diff two snapshots; non-zero on any change")
    c.add_argument("before")
    c.add_argument("after")

    args = ap.parse_args()

    if args.cmd == "snapshot":
        if not args.url:
            print("--url (or $AGNES_DB_URL) is required", file=sys.stderr)
            return 2
        snap = snapshot(args.url)
        with open(args.out, "w") as fh:
            json.dump(snap, fh, indent=2, sort_keys=True)
        print(
            f"{snap['backend']} / {snap['side']} ({snap['rules']} rules): "
            f"{snap['counts']['pairs']} (person, thing) pairs, "
            f"{snap['counts']['channels']} open channel(s) -> {args.out}"
        )
        return 0

    with open(args.before) as fh:
        before = json.load(fh)
    with open(args.after) as fh:
        after = json.load(fh)
    ok, problems = compare(before, after)
    if ok:
        print(
            f"IDENTICAL — {before['counts']['pairs']} (person, thing) pairs and "
            f"{before['counts']['channels']} open channel(s) unchanged across the "
            f"migration on this {before['backend']} instance."
        )
        return 0
    print(f"{len(problems)} problem(s):", file=sys.stderr)
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    print(
        "\nAn upgrade must never change who can see what. Do not ship on this diff.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
