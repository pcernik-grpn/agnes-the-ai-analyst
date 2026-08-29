"""SQLAlchemy model for ``share_requests`` (PG-only, A3 ratchet — no DuckDB
sibling).

Sharing-approval queue (Track C6): a non-admin owner sharing a Library
``agent`` no longer writes a ``resource_grants`` row directly — it queues a
row here instead, and an admin's approve/reject decides whether the grant
is actually written. Only ``agent`` shares are gated today (``CLAUDE.md``:
"a user can build their own agents freely, but SHARING an agent needs ADMIN
APPROVAL"); other shareable resource types (collections, data apps, corpus
files) are unaffected and never write a row here.

See ``app/services/library_sharing.py::set_shares`` for the intercept point
and ``app/api/share_requests_admin.py`` for the admin approve/reject API.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class ShareRequest(Base):
    __tablename__ = "share_requests"
    __table_args__ = (
        Index("idx_share_requests_status", "status"),
        Index("idx_share_requests_resource", "resource_type", "resource_id"),
        # Structural backstop against a double-submitted PUT racing the
        # application-level idempotency check in `set_shares`: at most one
        # PENDING request per (resource, group) at a time.
        Index(
            "uq_share_requests_pending",
            "resource_type",
            "resource_id",
            "requested_group_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    requested_group_id: Mapped[str] = mapped_column(
        String, ForeignKey("user_groups.id", ondelete="CASCADE"), nullable=False
    )
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    # pending | approved | rejected
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'pending'"))
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
