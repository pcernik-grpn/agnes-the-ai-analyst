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

Revision ID: 0071_sync_state_id_v123
Revises: 0070_builder_scope_v122
Create Date: 2026-08-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0071_sync_state_id_v123"
down_revision: Union[str, None] = "0070_builder_scope_v122"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "sync_state" not in tables or "table_registry" not in tables:
        return

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
            continue
        bind.execute(
            sa.text("UPDATE sync_state SET table_id = :new WHERE table_id = :old"),
            {"new": new_id, "old": table_id},
        )
        if "sync_history" in tables:
            bind.execute(
                sa.text("UPDATE sync_history SET table_id = :new WHERE table_id = :old"),
                {"new": new_id, "old": table_id},
            )
        existing_ids.discard(table_id)
        existing_ids.add(new_id)


def downgrade() -> None:
    # See the module docstring: this backfill cannot be safely inverted.
    pass
