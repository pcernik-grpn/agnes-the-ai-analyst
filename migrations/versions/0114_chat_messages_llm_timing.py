"""chat_messages.llm_calls / llm_duration_ms / llm_ttfb_ms — make a chat
turn's LLM latency measurable, next to its measured cost.

The secret broker forwards every completion of a turn and already parses
each one's token usage onto the session's turn counters; the manager sums
them onto the assistant message (migration 0092 added the prompt-cache
halves). Nothing recorded how long those completions TOOK: the per-turn
wall time had to be inferred from log timestamps, and the observability
KPIs had no measured LLM latency at all. The broker now records each
completion's wall time (request start → last upstream byte) and
time-to-first-byte alongside its usage, and the turn's sums land here:
how many completions, their total duration, their total first-byte wait.

All three nullable with no default: NULL means "not recorded" (a row
written before this migration, by the frozen DuckDB app-state backend
which gets no matching column, or by a turn whose completions never
transited the broker) and must not be read as a measured zero — the
cost readout reports it as ``timing_accounting: unavailable``.

PG-first ratchet (A3): a schema CHANGE on an existing PG-only repository
table (``src/repositories/chat_messages_pg.py``) — Alembic-only, no matching
DuckDB ``_vN_to_v(N+1)`` step.

Revision ID: 0114_chat_messages_llm_timing
Revises: 0113_data_apps_data_identity
Create Date: 2026-09-08
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0114_chat_messages_llm_timing"
down_revision: Union[str, None] = "0113_data_apps_data_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("llm_calls", sa.Integer(), nullable=True))
    op.add_column("chat_messages", sa.Column("llm_duration_ms", sa.Integer(), nullable=True))
    op.add_column("chat_messages", sa.Column("llm_ttfb_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "llm_ttfb_ms")
    op.drop_column("chat_messages", "llm_duration_ms")
    op.drop_column("chat_messages", "llm_calls")
