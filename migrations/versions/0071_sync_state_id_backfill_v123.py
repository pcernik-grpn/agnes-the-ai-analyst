"""sync_state / sync_history id backfill (remediation B1)

Mirrors DuckDB ``_v122_to_v123``. Data-only: no column change.

Every ``sync_state`` writer keyed rows by the table's *name* (``_meta.
table_name`` for connector syncs, ``table_registry.name`` for the
materialized pass) while several admin-status readers (``/api/admin/
registry``, the data-sources pipeline strip, the Tables lens' delivery map)
joined ``sync_state`` against ``table_registry`` on ``id``. The two agree
only when a table's registry id happens to equal its display name (the
common case); a table registered with a display name that isn't already a
valid identifier (spaces, uppercase — e.g. ``name="Web Sessions"``, id
``web_sessions``) showed healthy sync status on one admin surface and
"never synced" on another, from the exact same sync. Writers now resolve
the id themselves going forward (``src.sync_state_key``); this migration is
the one-time catch-up for rows an earlier binary already wrote.

A ``sync_state.table_id`` value that:
  - matches no ``table_registry.name`` is left unchanged (an unregistered
    or since-renamed table) — never dropped, matching the writers' own
    fallback behavior;
  - already equals its target id (``id == name``, or a stray duplicate) is
    a no-op;
  - would collide with a row that ALREADY exists under the target id is
    left unchanged rather than silently dropping one row's history —
    ``table_id`` is the primary key.

``sync_history.table_id`` shares the exact same keying convention (every
``update_sync()`` call inserts both rows under the identical key) but
carries no uniqueness constraint, so its rewrite has no collision case.

``downgrade()`` is deliberately a no-op: nothing here marks which rows this
step renamed, so reverting would require distinguishing them from names
that legitimately equal their id, which isn't recoverable after the fact.

Column-defensive, mirroring the DuckDB sibling: every column this step
names (``sync_state.table_id``, ``table_registry.id``/``.name``,
``sync_history.table_id``) has been part of these tables' shape since they
were created (revision 0004), so a normal Alembic chain always has them by
0071 — but a table missing one is skipped with a log line rather than
raising, the same defensive posture ``_v122_to_v123`` needs for a DuckDB
install replaying the ladder from far enough back that ``sync_state``
predates its modern columns.

Revision ID: 0071_sync_state_id_v123
Revises: 0070_builder_scope_v122
Create Date: 2026-08-24
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0071_sync_state_id_v123"
down_revision: Union[str, None] = "0070_builder_scope_v122"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "sync_state" not in tables or "table_registry" not in tables:
        return

    sync_state_cols = {c["name"] for c in insp.get_columns("sync_state")}
    registry_cols = {c["name"] for c in insp.get_columns("table_registry")}
    if "table_id" not in sync_state_cols or "id" not in registry_cols or "name" not in registry_cols:
        logger.warning(
            "sync_state backfill (0071): skipped — sync_state and/or table_registry is missing an "
            "expected column (sync_state has %s, table_registry has %s); nothing to backfill on a "
            "table shaped like this",
            sorted(sync_state_cols),
            sorted(registry_cols),
        )
        return

    sync_history_has_table_id = "sync_history" in tables and "table_id" in {
        c["name"] for c in insp.get_columns("sync_history")
    }
    if "sync_history" in tables and not sync_history_has_table_id:
        logger.warning(
            "sync_state backfill (0071): sync_history is missing the table_id column — its rows are left untouched"
        )

    name_to_id: dict[str, str] = {}
    for rid, name in bind.execute(sa.text("SELECT id, name FROM table_registry")).fetchall():
        if name:
            name_to_id[name] = rid

    existing_ids = {row[0] for row in bind.execute(sa.text("SELECT table_id FROM sync_state")).fetchall()}

    rows = bind.execute(sa.text("SELECT table_id FROM sync_state")).fetchall()
    for (table_id,) in rows:
        new_id = name_to_id.get(table_id)
        if not new_id or new_id == table_id:
            continue
        if new_id in existing_ids:
            # A row already exists under the target id — leave this one
            # name-keyed rather than raise on the primary-key collision.
            logger.warning(
                "sync_state backfill (0071): leaving %r name-keyed — a row already exists under the target id %r",
                table_id,
                new_id,
            )
            continue
        bind.execute(
            sa.text("UPDATE sync_state SET table_id = :new WHERE table_id = :old"),
            {"new": new_id, "old": table_id},
        )
        if sync_history_has_table_id:
            bind.execute(
                sa.text("UPDATE sync_history SET table_id = :new WHERE table_id = :old"),
                {"new": new_id, "old": table_id},
            )
        existing_ids.discard(table_id)
        existing_ids.add(new_id)


def downgrade() -> None:
    # See the module docstring: this backfill cannot be safely inverted.
    pass
