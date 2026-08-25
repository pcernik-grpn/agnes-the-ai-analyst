"""chat_sessions sandbox pause/resume refs (DuckDB v73 parity).

Three nullable columns tracking the provider sandbox ID, the runner PID,
and the time the session was paused. Un-indexed by design — DuckDB 1.5.3
raises a false FK violation when UPDATE-ing indexed columns of
``chat_sessions`` after any ``chat_messages`` INSERT.

Mirrors DuckDB ``_v72_to_v73``. Additive-only.

Revision ID: 0020_chat_sandbox_refs_v73
Revises: 0019_system_secrets_v72
Create Date: 2026-06-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_chat_sandbox_refs_v73"
down_revision: Union[str, None] = "0019_system_secrets_v72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("sandbox_id", sa.String(), nullable=True))
    op.add_column("chat_sessions", sa.Column("runner_pid", sa.Integer(), nullable=True))
    op.add_column(
        "chat_sessions",
        sa.Column("sandbox_paused_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "sandbox_paused_at")
    op.drop_column("chat_sessions", "runner_pid")
    op.drop_column("chat_sessions", "sandbox_id")
