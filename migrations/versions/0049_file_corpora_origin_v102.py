"""add file_corpora.origin (uploaded | generated)

Mirrors DuckDB ``_v109_to_v110``. ``origin`` records artifact provenance for
the Artifacts toolbar's Source facet: every existing artifact is
user-uploaded, so the column defaults to ``'uploaded'``; the future
agent-generated-artefact writer sets ``'generated'``.

Idempotent + guarded so it's a no-op where the column already exists.

Revision ID: 0049_file_corpora_origin_v102
Revises: 0048_mcp_connect_hint_heal_v101
Create Date: 2026-07-28


Filename/version-suffix note: the ``_vNNN`` suffix in this module's
FILENAME reflects an older restack of the DuckDB ladder and is frozen —
the revision id is the deployed alembic_version key and renaming would
only move the mismatch around. The ``Mirrors DuckDB`` line above is the
authoritative mapping.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0049_file_corpora_origin_v102"
down_revision: Union[str, None] = "0045_user_journey_state_v98"  # restacked onto main head
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "file_corpora" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("file_corpora")}
    if "origin" not in cols:
        op.add_column(
            "file_corpora",
            sa.Column("origin", sa.String(), nullable=False, server_default="uploaded"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "file_corpora" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("file_corpora")}
    if "origin" in cols:
        op.drop_column("file_corpora", "origin")
