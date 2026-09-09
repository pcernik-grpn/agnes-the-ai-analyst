"""``llm_calls`` and ``chat_message_feedback`` — the LLM observability ledger
and the chat quality signal (design 2026-09-08, §3.7).

PG-ONLY (A3 PG-first ratchet): both tables landed after the DuckDB app-state
backend was frozen — no ``src/db.py`` step, no DuckDB repository sibling.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class LlmCall(Base):
    """One LLM call, from every call site: who / for what / in which turn / at
    what price. Written by the chat broker (``kind='completion'``) and by
    ``trace_generation`` (``kind='generation'``); ``trace_id``/``span_id``
    join it to the exported span when export is on."""

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    workload: Mapped[str | None] = mapped_column(String, nullable=True)
    purpose: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    span_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    upstream: Mapped[str] = mapped_column(String, nullable=False)
    model_requested: Mapped[str | None] = mapped_column(String, nullable=True)
    model_response: Mapped[str | None] = mapped_column(String, nullable=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), server_default=text("0"), nullable=False)
    priced_as: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    stream_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Set when this row's usage was recovered from the broker's bounded
    #: head/tail edge buffers after the full-body mirror overflowed (Task
    #: 2b) — the tokens and price are real, only the content summary was cut
    #: short. ``None`` on a row whose usage was never at risk of truncation.
    response_truncated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    __table_args__ = (
        Index("idx_llm_calls_created", "created_at"),
        Index("idx_llm_calls_session_time", "session_id", "created_at"),
        Index("idx_llm_calls_turn", "turn_id"),
        Index("idx_llm_calls_user_time", "user_id", "created_at"),
        Index("idx_llm_calls_agent_time", "agent_id", "created_at"),
        Index("idx_llm_calls_workload_time", "workload", "created_at"),
    )


class ChatMessageFeedback(Base):
    """Thumbs up/down (plus an optional comment) on one chat turn, one row per
    ``(turn_id, user_id)`` — a second submit updates it."""

    __tablename__ = "chat_message_feedback"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    turn_id: Mapped[str] = mapped_column(String, nullable=False)
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        UniqueConstraint("turn_id", "user_id", name="uq_chat_message_feedback_turn_user"),
        Index("idx_chat_message_feedback_session", "session_id"),
        Index("idx_chat_message_feedback_created", "created_at"),
    )
