"""SQLAlchemy models behind issue reporting (step 1).

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline"):
no ``src/db.py`` ladder step and no DuckDB repository sibling. See
``docs/migrations.md`` -> "Adding a PG-only feature (post-A3)" and the design
``docs/superpowers/specs/2026-09-09-issue-reporting-step1-design.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Sequence, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base

ISSUE_KINDS: tuple[str, ...] = ("bug", "wrong_answer", "request", "question", "other")
ISSUE_STATUSES: tuple[str, ...] = ("open", "resolved")
ISSUE_SURFACES: tuple[str, ...] = ("web", "cli", "mcp")
COMMENT_AUTHOR_KINDS: tuple[str, ...] = ("reporter", "admin")

ISSUE_NUMBER_SEQ = Sequence("issue_reports_number_seq")


class IssueReport(Base):
    """One report — a bug, a wrong answer, a missing thing, or "I don't get it".

    ``created_by`` is the user id (not the email): the internal-table row
    filter (``connectors/internal/access.py``) keys ``filter_kind='user_id'``
    on ``user["id"]``. ``number`` is the human id (``#42``); every client
    accepts it interchangeably with ``id``.
    """

    __tablename__ = "issue_reports"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    number: Mapped[int] = mapped_column(
        Integer, ISSUE_NUMBER_SEQ, nullable=False, unique=True, server_default=ISSUE_NUMBER_SEQ.next_value()
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'bug'"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'open'"))
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_by_email: Mapped[str | None] = mapped_column(String, nullable=True)
    source_surface: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'web'"))
    page_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_issue_reports_status", "status"),
        Index("idx_issue_reports_created_by", "created_by"),
    )


class IssueComment(Base):
    """A public note on an issue by its reporter or an admin.

    ``issue_owner_id`` is denormalized from ``issue_reports.created_by`` so
    the internal table ``agnes_issue_comments`` can filter rows per user
    without a join — a reporter must see an admin's reply on THEIR issue.
    """

    __tablename__ = "issue_comments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    issue_id: Mapped[str] = mapped_column(String, ForeignKey("issue_reports.id", ondelete="CASCADE"), nullable=False)
    issue_owner_id: Mapped[str] = mapped_column(String, nullable=False)
    author_id: Mapped[str | None] = mapped_column(String, nullable=True)
    author_email: Mapped[str | None] = mapped_column(String, nullable=True)
    author_kind: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        Index("idx_issue_comments_issue_created", "issue_id", "created_at"),
        Index("idx_issue_comments_owner", "issue_owner_id"),
    )
