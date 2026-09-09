"""export_watermarks — the durable cursor for a scheduled push export
(design 2026-09-08, §3.12, Task 11).

One row per named sink. The ``conversation-export`` worker job kind
(``app/worker/kinds_conversation_export.py``) reads the row named
``"conversation_export"`` to know which conversations it has already
delivered to ``observability.conversation_export.endpoint``, and advances
it only after that endpoint answers 2xx.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
This is a brand-new table landing after the DuckDB app-state backend was
frozen: no ``src/db.py`` step, no DuckDB repository sibling, registered
``PG``-only in ``src.repositories._REGISTRY``.

Revision ID: 0114_export_watermarks
Revises: 0113_llm_observability
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0114_export_watermarks"
down_revision: str | None = "0113_llm_observability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "export_watermarks",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("export_watermarks")
