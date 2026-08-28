"""Deterministic schema snapshot for round-trip and drift tests.

We don't shell out to ``pg_dump`` — it isn't always present in the test
runner, and its output format varies between PG minor versions. Instead,
read ``information_schema`` via SQLAlchemy and emit a sorted, hashable
dict that two snapshots can compare with ``==``.

Coverage:
  - tables (name + ordered columns: name, type, nullable, default)
  - primary keys
  - foreign keys (table → constrained columns → referred table/columns)
  - unique constraints
  - indexes (name + columns + unique flag)
  - check constraints (name + sqltext)

Intentionally NOT covered (yet):
  - sequences (Alembic manages them via the column DEFAULT; comparing
    sequence names per-revision adds noise without catching real drift)
  - functions / triggers (Agnes doesn't use them today)
"""

from __future__ import annotations

from typing import Any, Dict, List

import sqlalchemy as sa
from sqlalchemy.engine import Engine


def _table_snapshot(inspector, table: str) -> Dict[str, Any]:
    cols = [
        {
            "name": c["name"],
            "type": str(c["type"]),
            "nullable": c["nullable"],
            "default": str(c.get("default")) if c.get("default") is not None else None,
        }
        for c in inspector.get_columns(table, schema="public")
    ]
    cols.sort(key=lambda c: c["name"])

    pk = inspector.get_pk_constraint(table, schema="public") or {}
    pk_columns = sorted(pk.get("constrained_columns") or [])

    fks: List[Dict[str, Any]] = []
    for fk in inspector.get_foreign_keys(table, schema="public") or []:
        fks.append(
            {
                "constrained_columns": sorted(fk.get("constrained_columns") or []),
                "referred_table": fk.get("referred_table"),
                "referred_columns": sorted(fk.get("referred_columns") or []),
            }
        )
    fks.sort(key=lambda f: (f["referred_table"], tuple(f["constrained_columns"])))

    uniques: List[Dict[str, Any]] = []
    for u in inspector.get_unique_constraints(table, schema="public") or []:
        uniques.append({"columns": sorted(u.get("column_names") or [])})
    uniques.sort(key=lambda u: tuple(u["columns"]))

    indexes: List[Dict[str, Any]] = []
    for idx in inspector.get_indexes(table, schema="public") or []:
        # A functional/expression index (e.g. a COALESCE(...) element) has
        # ``None`` at that position in ``column_names``; the PG dialect
        # aligns a parallel ``expressions`` list with the raw SQL text for
        # every element (plain columns included), so fall back to it
        # per-position instead of sorting a list that mixes str and None.
        raw_cols = idx.get("column_names") or []
        exprs = idx.get("expressions") or raw_cols
        names = [c if c is not None else e for c, e in zip(raw_cols, exprs)]
        indexes.append(
            {
                "columns": sorted(names),
                "unique": bool(idx.get("unique")),
            }
        )
    indexes.sort(key=lambda i: (tuple(i["columns"]), i["unique"]))

    checks: List[Dict[str, Any]] = []
    for ck in inspector.get_check_constraints(table, schema="public") or []:
        checks.append({"sqltext": ck.get("sqltext")})
    checks.sort(key=lambda c: c.get("sqltext") or "")

    return {
        "columns": cols,
        "primary_key": pk_columns,
        "foreign_keys": fks,
        "unique_constraints": uniques,
        "indexes": indexes,
        "check_constraints": checks,
    }


def snapshot_schema(engine: Engine, exclude: tuple[str, ...] = ("alembic_version",)) -> Dict[str, Any]:
    """Return a deterministic, comparable snapshot of the PG ``public``
    schema. Excludes ``alembic_version`` by default because the row in
    that table varies by revision-of-the-moment, which is exactly what
    we're trying to vary.
    """
    inspector = sa.inspect(engine)
    tables = sorted(t for t in inspector.get_table_names(schema="public") if t not in exclude)
    return {t: _table_snapshot(inspector, t) for t in tables}
