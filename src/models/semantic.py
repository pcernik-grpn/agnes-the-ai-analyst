"""SQLAlchemy models for the open semantic-layer contract (v116):
semantic_models, semantic_sources, data_package_semantic_models.

Mirrors ``src/db.py::_v115_to_v116``. ``semantic_models`` stores the
canonical Apache Ossie document; ``metric_definitions`` and
``glossary_terms`` (``src/models/config.py``) remain the flat projections
queries actually read, derived from it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, PrimaryKeyConstraint, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class SemanticModel(Base):
    """One stored Apache Ossie semantic-layer document.

    ``document`` is the adapter's output verbatim — never re-serialized, so
    ``export`` hands back exactly what the source wrote. ``document_json`` is
    the parsed form, kept alongside for querying without a YAML round-trip.
    """

    __tablename__ = "semantic_models"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    document: Mapped[str] = mapped_column(Text, nullable=False)
    document_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    spec_version: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, server_default=text("'manual'"), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, server_default=text("'valid'"), nullable=False)
    validation_errors: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    # F3 detach/override (PG-only, A3 ratchet — migrations/versions/
    # 0090_semantic_models_detach.py). DuckDB gains no capability that
    # depends on these columns.
    sync_mode: Mapped[str] = mapped_column(String, server_default=text("'synced'"), nullable=False)
    detached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detached_by: Mapped[str | None] = mapped_column(String, nullable=True)
    detach_base_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    source_content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    source_missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("idx_semantic_models_origin", "source", "source_ref", "slug", unique=True),)


class SemanticSource(Base):
    """A configured feed of semantic-layer documents (git repo, upload, or
    an existing connection) plus its last-sync outcome."""

    __tablename__ = "semantic_sources"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    adapter: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool | None] = mapped_column(Boolean, server_default=text("true"), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class DataPackageSemanticModel(Base):
    """Bridge between ``data_packages`` and ``semantic_models`` (M:N).

    No FK declared, matching the DuckDB DDL — repository code clears the
    junction explicitly (same asymmetric-junction precedent as
    ``DataPackageTable`` / ``KnowledgeItemDomain``).
    """

    __tablename__ = "data_package_semantic_models"

    package_id: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (PrimaryKeyConstraint("package_id", "model_id"),)
