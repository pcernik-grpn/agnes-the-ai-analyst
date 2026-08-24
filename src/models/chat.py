"""SQLAlchemy models for the cloud-chat cluster (DuckDB v70+ parity).

Mirrors:
  - chat_sessions              (src/db.py v68 base + v70 co-presence columns
                                 + v73 sandbox refs + v98 relay_protocol_version)
  - chat_messages              (src/db.py v68 base + v70 sender_email)
  - chat_session_participants  (src/db.py v70)
  - user_workdirs              (src/db.py v68)

The DuckDB side (DuckDB 1.5.x) cannot express three constraints that the
Postgres schema carries here, because PG has no such limitations:

  - ``chat_messages.session_id`` FK → ``chat_sessions.id`` with
    ``ON DELETE CASCADE``. On DuckDB the FK is a plain reference and
    ``ChatRepository.hard_delete_user_sessions`` deletes child messages
    by hand before the parent.

  - ``chat_session_participants.session_id`` FK → ``chat_sessions.id`` with
    ``ON DELETE CASCADE``. On DuckDB the FK is a plain reference and
    ``ChatRepository.hard_delete_user_sessions`` deletes participant rows
    by hand before the parent.

  - Per-surface partial unique indexes enforcing the Slack DM / thread
    uniqueness that ``ChatRepository.get_slack_dm_session`` /
    ``get_slack_thread_session`` enforce in application code on DuckDB:
      * one DM session per ``slack_channel_id`` WHERE ``surface='slack_dm'``
      * one thread session per ``(slack_channel_id, slack_thread_ts)``
        WHERE ``surface='slack_thread'``

  - ``last_message_at`` / ``message_count`` can be UPDATEd directly on PG
    (the DuckDB 1.5.3 FK+index false-violation bug does not apply), so the
    PG repositories keep these columns current instead of deriving them at
    read time.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class ChatSession(Base):
    """Per-session chat transcript header (v68)."""

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_email: Mapped[str] = mapped_column(String, nullable=False)
    surface: Mapped[str] = mapped_column(String, nullable=False)
    slack_channel_id: Mapped[str | None] = mapped_column(String, nullable=True)
    slack_thread_ts: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    is_co_session: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    ephemeral: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    # Sandbox pause/resume refs (v73). Deliberately un-indexed — the DuckDB
    # sibling cannot index them (1.5.3 FK+index bug) and the paused-TTL
    # reaper scan is cheap at chat-session cardinality.
    sandbox_id: Mapped[str | None] = mapped_column(String, nullable=True)
    runner_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sandbox_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # v96: agent-as-API — which agent profile (if any) drove this session.
    # Deliberately unindexed — see the DuckDB _v94_to_v95 ART-index note;
    # this table already carries idx_chat_sessions_user.
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Relay protocol version of the runner these sandbox refs point at (v98,
    # Tier 1 restart-invariant reuse). NULL = unknown/legacy — see
    # app.chat.types.RELAY_PROTOCOL_VERSION's docstring for the full story.
    relay_protocol_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # v111: user-pinned conversations. NULL = not pinned; a timestamp records
    # when the pin happened so the history panel orders pins most-recent-first.
    # Deliberately un-indexed — the DuckDB sibling cannot index a column it
    # UPDATEs after chat_messages rows exist (1.5.3 FK+index bug), and pin state
    # is only read as part of one user's list (idx_chat_sessions_user covers it).
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_chat_sessions_user", "user_email", "last_message_at"),
        # Partial unique indexes — the per-surface Slack uniqueness that the
        # DuckDB side enforces only in ChatRepository application code.
        Index(
            "uq_chat_sessions_slack_dm",
            "slack_channel_id",
            unique=True,
            postgresql_where=text("surface = 'slack_dm'"),
        ),
        Index(
            "uq_chat_sessions_slack_thread",
            "slack_channel_id",
            "slack_thread_ts",
            unique=True,
            postgresql_where=text("surface = 'slack_thread'"),
        ),
    )


class ChatMessage(Base):
    """Single chat message within a session (v68)."""

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    #: The turn's ordered [{type:'text'|'tool', …}] shape — what keeps prose
    #: and tool cards interleaved across a reload, with each tool entry
    #: carrying its own state/result/is_error. `tool_calls` above is its
    #: positionless projection. See app/chat/message_parts.py; DuckDB side is
    #: `_v122_to_v123`. NULL on a row written before schema v123.
    parts: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    sender_email: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (Index("idx_chat_messages_session", "session_id", "created_at"),)


class ChatSessionParticipant(Base):
    """Live membership for co-drive sessions (v70)."""

    __tablename__ = "chat_session_participants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_email: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_chat_session_participants_user", "user_email", "session_id"),
        UniqueConstraint("session_id", "user_email", name="uq_participant_session_user"),
    )


class UserWorkdir(Base):
    """Per-user workspace init markers (v68)."""

    __tablename__ = "user_workdirs"

    user_email: Mapped[str] = mapped_column(String, primary_key=True)
    last_init_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    marketplace_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    initial_workspace_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    agnes_version_at_init: Mapped[str | None] = mapped_column(String, nullable=True)
