"""usage_turns — per-assistant-turn token usage (incl. prompt cache).

``usage_session_summary`` holds one row per session, which cannot answer
"which model" or "how much of this was a cheap cached re-read" once a session
mixes models and caching — both splits only exist at the turn grain. This
table records them where they happen; the summary stays the fast total.

``(session_file, turn_uuid)`` is UNIQUE so that writing turns is idempotent:
the usage processor re-walks a whole session file every time its hash
changes, and the chat manager may retry a persist. Both writers insert with
``ON CONFLICT DO NOTHING``, so a re-process can never double-count a user's
tokens. The ``(user_id, occurred_at)`` index serves the self-scoped windowed
read every usage surface makes ("my tokens, last 30 days").

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately NO matching ``_vN_to_v(N+1)`` step in ``src/db.py`` and
``SCHEMA_VERSION`` stays at ``FROZEN_DUCKDB_SCHEMA_VERSION``: the DuckDB
app-state backend is frozen, so this table exists on Postgres only and its
repository (``src/repositories/usage_turns_pg.py``) has no DuckDB sibling. A
DuckDB-backed instance reaching the feature gets the typed
``501 requires_postgres_backend`` (``tests/test_usage_turns_requires_pg.py``).
See ``docs/migrations.md`` -> "Adding a PG-only feature (post-A3)".

Revision ID: 0094_usage_turns
Revises: 0093_merge_train23_semantic
Create Date: 2026-08-31

Chained onto ``0093_merge_train23_semantic`` — the chain's ACTUAL single head
— rather than onto ``0086_claims_audience`` as the implementation plan
(written before merge train 23 landed) says. Pointing at ``0086`` today would
fork the graph into a third head, which is the exact defect ``0091`` and
``0093`` were written to repair: with two heads ``alembic upgrade head``
refuses ("Multiple head revisions are present"), failing every Postgres-backed
test at fixture setup and a real deployment's migrate step the same way. If
this revision is ever replayed onto a base that predates merge train 23,
re-chain it (parent only — keep the revision id) exactly as
``0087_resource_source_tags`` documents.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0094_usage_turns"
down_revision: Union[str, None] = "0093_merge_train23_semantic"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "usage_turns",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_file", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("surface", sa.String(), server_default=sa.text("'claude_code'"), nullable=False),
        sa.Column("turn_uuid", sa.String(), nullable=False),
        sa.Column("parent_uuid", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_creation_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processor_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_file", "turn_uuid", name="uq_usage_turns_file_turn"),
    )
    op.create_index("idx_usage_turns_user_time", "usage_turns", ["user_id", "occurred_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_usage_turns_user_time", table_name="usage_turns")
    op.drop_table("usage_turns")
