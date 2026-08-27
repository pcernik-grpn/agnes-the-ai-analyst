"""SQLAlchemy models backing the cross-domain semantic coverage report.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline"):
these tables landed after the DuckDB app-state backend was frozen, so there
is no ``src/db.py`` ladder step and no DuckDB repository sibling. See
``docs/migrations.md`` -> "Adding a PG-only feature".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class ResourceSourceTag(Base):
    """ "This skill / agent / knowledge domain is about THAT data source."

    Coverage asks a question no existing table can answer: does a connected
    data source have someone's know-how attached to it — a skill that queries
    it, an agent specialized in it, a memory domain that documents it? Those
    three live in their own tables (``marketplace_plugins``, ``agents``,
    ``memory_domains``) with no notion of a source, so the link is recorded
    here.

    Deliberately NOT a ``resource_grants`` row: a grant answers "which group
    may reach this", an entirely different question with an entirely
    different lifecycle. ``resource_type`` reuses the
    :class:`app.resource_types.ResourceType` vocabulary and ``resource_id``
    the same path convention a grant uses for that type, so a tag and a grant
    name the same object the same way.
    """

    __tablename__ = "resource_source_tags"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    tagged_by: Mapped[str | None] = mapped_column(String, nullable=True)
    tagged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        # One tag per (type, resource, source): tagging the same skill to the
        # same source twice means nothing new, and duplicate rows would make
        # the coverage roll-up count one skill as several.
        UniqueConstraint("resource_type", "resource_id", "source_id", name="uq_resource_source_tags_triple"),
        # The report's hot path is "everything tagged to THIS source", once
        # per connected source per page view.
        Index("idx_resource_source_tags_source", "source_id"),
    )
