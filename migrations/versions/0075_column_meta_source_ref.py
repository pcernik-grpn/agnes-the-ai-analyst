"""column_metadata.source_ref

Nullable per-connection provenance for a dataset field written by the
semantic-layer projector (``src.semantic.projection.project_document``) —
the same addition ``0054_semantic_source_ref_v107`` made to
``metric_definitions`` / ``glossary_terms``. Postgres-only: the A3 PG-first
ratchet (``docs/migrations.md`` -> "Adding a PG-only feature") freezes the
DuckDB app-state schema, so this column has no DuckDB counterpart —
``src/repositories/column_metadata.py``'s ``save()`` accepts ``source_ref``
for signature parity with the Postgres repo but does not persist it.

Revision ID: 0075_column_meta_source_ref
Revises: 0074_llm_usage_caller_user_id
Create Date: 2026-08-26

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Kept to 32 chars — alembic_version.version_num is VARCHAR(32); a longer id
# truncates and breaks every later revision's WHERE clause (verified live:
# StringDataRightTruncation on this exact migration during development).
revision: str = "0075_column_meta_source_ref"
down_revision: Union[str, None] = "0074_llm_usage_caller_user_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("column_metadata", sa.Column("source_ref", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("column_metadata", "source_ref")
