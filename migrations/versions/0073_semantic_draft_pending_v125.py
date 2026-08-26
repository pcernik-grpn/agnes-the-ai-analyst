"""table_registry.semantic_draft_pending_at

Mirrors DuckDB ``_v124_to_v125``. Dedup bookkeeping for wave 2's headless
semantic-model auto-drafting session (semantic-phase5): when set, a draft
has already been queued for this table and a fresh sweep should not queue a
second one. Plain additive column, no backfill — NULL on every existing row
means "no draft pending", the correct reading for a table nothing has swept
yet.

Revision ID: 0073_semantic_draft_pending_v125
Revises: 0072_sync_state_id_v124
Create Date: 2026-08-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0073_semantic_draft_pending_v125"
down_revision: Union[str, None] = "0072_sync_state_id_v124"
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
