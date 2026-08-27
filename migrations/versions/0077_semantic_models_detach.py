"""semantic_models detach/override columns (F3)

PG-only (A3 PG-first ratchet — the DuckDB app-state ladder is frozen at
``FROZEN_DUCKDB_SCHEMA_VERSION``, so there is no matching ``_vN_to_v(N+1)``
step in ``src/db.py``). ``semantic_models`` is an existing frozen DuckDB<->PG
pair, but a genuine schema change on it follows "Adding a PG-only feature"
(``docs/migrations.md``), not the existing-pair-method path — the DuckDB side
of this repo gains no capability that depends on these columns.

Six plain additive columns, no backfill:

- ``sync_mode`` — ``'synced'`` (default) | ``'detached'``.
- ``detached_at`` / ``detached_by`` — audit: when/who last detached this row.
- ``detach_base_hash`` — ``content_hash`` frozen at the moment of detach.
- ``source_content_hash`` — latest hash the importer has observed from the
  source since detach (kept fresh by every sync pass while detached, never
  by upsert). Compared against ``detach_base_hash`` for the "source changed
  since you detached" staleness indicator.
- ``source_missing_since`` — when the source first stopped sending this slug
  while the row was detached; NULL means the source still has it.

Revision ID: 0077_semantic_models_detach
Revises: 0076_semantic_draft_pending
Create Date: 2026-08-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0077_semantic_models_detach"
down_revision: Union[str, None] = "0076_semantic_draft_pending"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = [
    ("sync_mode", sa.String(), False, "synced"),
    ("detached_at", sa.DateTime(timezone=True), True, None),
    ("detached_by", sa.String(), True, None),
    ("detach_base_hash", sa.String(), True, None),
    ("source_content_hash", sa.String(), True, None),
    ("source_missing_since", sa.DateTime(timezone=True), True, None),
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "semantic_models" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("semantic_models")}
    for name, col_type, nullable, default in _COLUMNS:
        if name in existing_cols:
            continue
        kwargs = {"nullable": nullable}
        if default is not None:
            kwargs["server_default"] = sa.text(f"'{default}'")
        op.add_column("semantic_models", sa.Column(name, col_type, **kwargs))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "semantic_models" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("semantic_models")}
    for name, _col_type, _nullable, _default in _COLUMNS:
        if name in existing_cols:
            op.drop_column("semantic_models", name)
