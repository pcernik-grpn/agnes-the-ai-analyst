"""SQLAlchemy models for the lookup cluster:
view_ownership, column_metadata, bq_metadata_cache, user_sync_settings.

Mirrors:
  - ``view_ownership``      (src/db.py:100-104)
  - ``column_metadata``     (src/db.py:354-363)
  - ``bq_metadata_cache``   (src/db.py:689-710)
  - ``user_sync_settings``  (src/db.py:116-124)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, PrimaryKeyConstraint, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class ViewOwnership(Base):
    __tablename__ = "view_ownership"

    view_name: Mapped[str] = mapped_column(String, primary_key=True)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class ColumnMetadata(Base):
    __tablename__ = "column_metadata"

    table_id: Mapped[str] = mapped_column(String, nullable=False)
    column_name: Mapped[str] = mapped_column(String, nullable=False)
    basetype: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[str] = mapped_column(
        String, server_default=text("'manual'"), nullable=False
    )
    source: Mapped[str] = mapped_column(
        String, server_default=text("'manual'"), nullable=False
    )
    # v125 (0073_column_meta_source_ref_v125): per-connection provenance,
    # mirrors metric_definitions/glossary_terms.source_ref (v107).
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        PrimaryKeyConstraint("table_id", "column_name"),
        Index("ix_column_metadata_table_id", "table_id"),
    )


class BqMetadataCache(Base):
    __tablename__ = "bq_metadata_cache"

    table_id: Mapped[str] = mapped_column(String, primary_key=True)
    rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    partition_by: Mapped[str | None] = mapped_column(String, nullable=True)
    clustered_by: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String, nullable=True)
    known_columns: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_msg: Mapped[str | None] = mapped_column(String, nullable=True)


class UserSyncSettings(Base):
    __tablename__ = "user_sync_settings"

    user_id: Mapped[str] = mapped_column(String, nullable=False)
    dataset: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), nullable=False
    )
    table_mode: Mapped[str] = mapped_column(
        String, server_default=text("'all'"), nullable=False
    )
    tables: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "dataset"),
        Index("ix_user_sync_settings_user_id", "user_id"),
    )
