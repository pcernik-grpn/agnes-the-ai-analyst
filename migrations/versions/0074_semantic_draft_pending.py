"""table_registry.semantic_draft_pending_at

PG-only (A3 PG-first ratchet — the DuckDB app-state ladder is frozen at
``FROZEN_DUCKDB_SCHEMA_VERSION``, so there is no matching ``_vN_to_v(N+1)``
step in ``src/db.py``). Dedup bookkeeping for wave 2's headless
semantic-model auto-drafting session (semantic-phase5): when set, a draft
has already been queued for this table and a fresh sweep should not queue a
second one. Plain additive column, no backfill — NULL on every existing row
means "no draft pending", the correct reading for a table nothing has swept
yet.

Revision ID: 0074_semantic_draft_pending
Revises: 0073_agent_scope_granted_by
Create Date: 2026-08-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0074_semantic_draft_pending"
down_revision: Union[str, None] = "0073_agent_scope_granted_by"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "table_registry" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("table_registry")}
    if "semantic_draft_pending_at" in existing_cols:
        return
    op.add_column(
        "table_registry",
        sa.Column("semantic_draft_pending_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "table_registry" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("table_registry")}
    if "semantic_draft_pending_at" in existing_cols:
        op.drop_column("table_registry", "semantic_draft_pending_at")
