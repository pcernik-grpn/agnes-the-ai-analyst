#!/usr/bin/env python3
"""Does anything on this instance still depend on a direct `table` grant?

One number decides the effort's ticket 17, and it is not guessable from the
code: a `resource_grants(group, 'table', id)` row grants an analyst NOTHING
(`src/rbac.py::can_access_table` intersects the caller's data packages with
the packages containing the table and never reads a table grant), so its only
remaining reader is agent scoping — the union in
`src/agent_scope_intersection.py::_axis_allowed_ids`, `base |
_package_table_ids(pkgs)`.

So the question is whether any agent's scope names a bare table. If none
does, every direct `table` grant on the instance is inert and can go. If some
do, those are the rows that would lose reach, and the next question is whether
their tables are already in a package the owner holds.

Run it against a REAL instance, or a copy. Read-only — SELECTs and nothing
else. Works on either app-state backend and detects which:

    scripts/audit_table_grants.py                       # uses $AGNES_DB_URL
    scripts/audit_table_grants.py --url "$DATABASE_URL"
    scripts/audit_table_grants.py --url /data/state/system.duckdb
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Tuple


#: Every question worth asking in one pass, so an operator runs one command.
#: `agent_scope` may be absent on an older instance — each query degrades to
#: "n/a" on its own rather than taking the whole audit down.
QUERIES: List[Tuple[str, str, str]] = [
    (
        "direct table grants",
        "SELECT COUNT(*) FROM resource_grants WHERE resource_type = 'table'",
        "rows the add-to-group dialog writes. None of them grants analyst access.",
    ),
    (
        "agents",
        "SELECT COUNT(*) FROM agents",
        "",
    ),
    (
        "agents scoped to selected tables",
        "SELECT COUNT(*) FROM agents WHERE tables_mode = 'selected'",
        "only these can read a table grant at all.",
    ),
    (
        "scope rows naming a BARE TABLE",
        "SELECT COUNT(*) FROM agent_scope WHERE item_type = 'table'",
        "THE NUMBER. 0 means every direct table grant is inert.",
    ),
    (
        "scope rows naming a package",
        "SELECT COUNT(*) FROM agent_scope WHERE item_type = 'data_package'",
        "the route the builder actually offers.",
    ),
]


def _is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://", "postgresql+"))


def _run(url: str) -> List[Tuple[str, object, str]]:
    out: List[Tuple[str, object, str]] = []
    if _is_postgres(url):
        import sqlalchemy as sa

        engine = sa.create_engine(url)
        try:
            with engine.connect() as conn:
                for label, sql, note in QUERIES:
                    try:
                        out.append((label, conn.execute(sa.text(sql)).scalar_one(), note))
                    except Exception as exc:  # noqa: BLE001 - one bad table must not end the audit
                        conn.rollback()
                        out.append((label, f"n/a ({type(exc).__name__})", note))
        finally:
            engine.dispose()
        return out

    # Through `_open_duckdb`, not `duckdb.connect`: it pins the session
    # timezone to UTC, and a guard ratchets every call site.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(url, read_only=True)
    try:
        for label, sql, note in QUERIES:
            try:
                out.append((label, conn.execute(sql).fetchone()[0], note))
            except Exception as exc:  # noqa: BLE001
                out.append((label, f"n/a ({type(exc).__name__})", note))
    finally:
        conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--url",
        default=os.environ.get("AGNES_DB_URL") or os.environ.get("DATABASE_URL", ""),
        help="Postgres URL, or a path to a system.duckdb file. Defaults to $AGNES_DB_URL, then $DATABASE_URL.",
    )
    args = ap.parse_args()
    if not args.url:
        print(
            "Nothing to connect to. Pass --url with either a Postgres URL or a\n"
            "path to system.duckdb, or set $AGNES_DB_URL / $DATABASE_URL.",
            file=sys.stderr,
        )
        return 2

    backend = "postgres" if _is_postgres(args.url) else "duckdb"
    print(f"app-state backend: {backend}\n")
    rows = _run(args.url)
    width = max(len(label) for label, _, _ in rows)
    for label, value, note in rows:
        print(f"  {label.rjust(width)}  {value}" + (f"    {note}" if note else ""))

    bare = next((v for label, v, _ in rows if "BARE TABLE" in label), None)
    print()
    if bare == 0:
        print(
            "No agent scope names a bare table, so every direct `table` grant on\n"
            "this instance is inert: nothing reads it. They can be removed, and\n"
            "the effort's ticket 17 closes with nothing to relocate."
        )
    elif isinstance(bare, int):
        print(
            f"{bare} agent scope row(s) name a bare table. THOSE are the rows that\n"
            "would lose reach if direct table grants went away. Next question:\n"
            "are their tables already in a data package the agent's owner holds?\n"
            "If yes the grant is still redundant; if no, the union is load-bearing\n"
            "and needs a home."
        )
    else:
        print(
            "Could not read `agent_scope` — likely an instance predating it.\n"
            "Without that number ticket 17 stays open: assuming nobody depends on\n"
            "the union is exactly the assumption the governing rule forbids."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
