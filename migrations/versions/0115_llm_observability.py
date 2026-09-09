"""llm_calls, chat_message_feedback, and turn provenance columns — the LLM
observability ledger and the chat quality signal (design 2026-09-08, §3.7).

``llm_calls`` is the one place that says "an LLM call happened": one row per
call from every call site (chat broker completion, ``trace_generation``
server-side generation), with who ran it, what it was for, which turn/job/
subject it belongs to, the four token kinds, the USD cost as priced at
write time (``priced_as`` keeps the rates beside the figure so a row can be
re-derived after a price change) and the ids (``trace_id``/``span_id``) that
join it to an exported span. ``chat_message_feedback`` is the thumbs
up/down signal on a chat turn, one row per ``(turn_id, user_id)`` — a second
submit updates it rather than piling up duplicates.

``chat_messages.turn_id`` and ``agent_memories.source_turn_id`` /
``source_message_id`` thread the same turn id through the transcript and the
memory notebook, so a written message and a stored memory can both be traced
back to the chat turn that produced them.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
``llm_calls`` and ``chat_message_feedback`` are brand-new tables that landed
after the DuckDB app-state backend was frozen: no ``src/db.py`` step, no
DuckDB repository sibling, registered ``PG``-only in
``src.repositories._REGISTRY``. The two column additions are a schema CHANGE
on existing frozen pairs (``chat_messages`` / ``agent_memories``) — allowed
under the ratchet's "existing pairs stay maintained" rule, Alembic-only, no
matching ``_vN_to_v(N+1)`` step; the DuckDB siblings accept the new keyword
argument and drop it, the same pattern migration ``0092`` established for
``chat_messages.cache_read_tokens`` / ``cache_creation_tokens``.

Revision ID: 0115_llm_observability
Revises: 0114_corpus_chunks_tsv
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0115_llm_observability"
down_revision: str | None = "0114_corpus_chunks_tsv"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("workload", sa.String(), nullable=True),
        sa.Column("purpose", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("turn_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("job_id", sa.String(), nullable=True),
        sa.Column("subject_id", sa.String(), nullable=True),
        sa.Column("trace_id", sa.String(), nullable=True),
        sa.Column("span_id", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("upstream", sa.String(), nullable=False),
        sa.Column("model_requested", sa.String(), nullable=True),
        sa.Column("model_response", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_creation_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("priced_as", JSONB(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("prompt_chars", sa.Integer(), nullable=True),
        sa.Column("completion_chars", sa.Integer(), nullable=True),
        sa.Column("stop_reason", sa.String(), nullable=True),
        sa.Column("stream_complete", sa.Boolean(), nullable=True),
        sa.Column("response_truncated", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_llm_calls_created", "llm_calls", ["created_at"])
    op.create_index("idx_llm_calls_session_time", "llm_calls", ["session_id", "created_at"])
    op.create_index("idx_llm_calls_turn", "llm_calls", ["turn_id"])
    op.create_index("idx_llm_calls_user_time", "llm_calls", ["user_id", "created_at"])
    op.create_index("idx_llm_calls_agent_time", "llm_calls", ["agent_id", "created_at"])
    op.create_index("idx_llm_calls_workload_time", "llm_calls", ["workload", "created_at"])

    op.create_table(
        "chat_message_feedback",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("turn_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", "user_id", name="uq_chat_message_feedback_turn_user"),
    )
    op.create_index("idx_chat_message_feedback_session", "chat_message_feedback", ["session_id"])
    op.create_index("idx_chat_message_feedback_created", "chat_message_feedback", ["created_at"])

    op.add_column("chat_messages", sa.Column("turn_id", sa.String(), nullable=True))
    op.create_index("idx_chat_messages_turn", "chat_messages", ["turn_id"])

    op.add_column("agent_memories", sa.Column("source_turn_id", sa.String(), nullable=True))
    op.add_column("agent_memories", sa.Column("source_message_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_memories", "source_message_id")
    op.drop_column("agent_memories", "source_turn_id")

    op.drop_index("idx_chat_messages_turn", table_name="chat_messages")
    op.drop_column("chat_messages", "turn_id")

    op.drop_index("idx_chat_message_feedback_created", table_name="chat_message_feedback")
    op.drop_index("idx_chat_message_feedback_session", table_name="chat_message_feedback")
    op.drop_table("chat_message_feedback")

    op.drop_index("idx_llm_calls_workload_time", table_name="llm_calls")
    op.drop_index("idx_llm_calls_agent_time", table_name="llm_calls")
    op.drop_index("idx_llm_calls_user_time", table_name="llm_calls")
    op.drop_index("idx_llm_calls_turn", table_name="llm_calls")
    op.drop_index("idx_llm_calls_session_time", table_name="llm_calls")
    op.drop_index("idx_llm_calls_created", table_name="llm_calls")
    op.drop_table("llm_calls")
