"""chat_messages.cache_read_tokens / cache_creation_tokens — make a chat
session's real cost measurable instead of modelled.

``app/chat/runner.py`` read only ``input_tokens``/``output_tokens`` off the
SDK usage object and dropped the two prompt-cache fields. Since
``input_tokens`` counts UNCACHED input only, a long session's context volume
— and therefore its cost — could not be reconstructed after the fact from
``chat_messages`` at all: the dominant term in a many-turn session (re-reading
a large cached prefix) had no column. Any cost comparison between two agent
workflows had to model that term instead of reading it, and a model that
charges cached re-reads at the full input rate overstates the cost of exactly
the shape these surfaces are built for.

Both nullable with no default: NULL means "not recorded" (a row written
before this migration, or by the frozen DuckDB app-state backend, which
gets no matching column) and must not be read as a measured zero — the cost
readout distinguishes the two.

PG-first ratchet (A3): a schema CHANGE on an existing PG-only repository
table (``src/repositories/chat_messages_pg.py`` has no ``_pg``-less sibling
in ``_REGISTRY``) — Alembic-only, no matching DuckDB ``_vN_to_v(N+1)`` step.

Revision ID: 0092_chat_messages_cache_tokens
Revises: 0091_merge_semantic_facts
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0092_chat_messages_cache_tokens"
down_revision: Union[str, None] = "0091_merge_semantic_facts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("cache_read_tokens", sa.Integer(), nullable=True))
    op.add_column("chat_messages", sa.Column("cache_creation_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "cache_creation_tokens")
    op.drop_column("chat_messages", "cache_read_tokens")
