"""SQLAlchemy models for the ops triad: table_registry, sync_state, sync_history.

Mirrors:
  - ``table_registry``  (src/db.py:290-317)
  - ``sync_state``      (src/db.py:81-91)
  - ``sync_history``    (src/db.py:106-114)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class TableRegistry(Base):
    __tablename__ = "table_registry"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str | None] = mapped_column(String, nullable=True)
    bucket: Mapped[str | None] = mapped_column(String, nullable=True)
    source_table: Mapped[str | None] = mapped_column(String, nullable=True)
    source_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_strategy: Mapped[str] = mapped_column(String, server_default=text("'full_refresh'"), nullable=False)
    query_mode: Mapped[str] = mapped_column(String, server_default=text("'local'"), nullable=False)
    sync_schedule: Mapped[str | None] = mapped_column(String, nullable=True)
    profile_after_sync: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), nullable=False)
    primary_key: Mapped[str | None] = mapped_column(String, nullable=True)
    folder: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    registered_by: Mapped[str | None] = mapped_column(String, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    # v26 Keboola sync-strategy support columns
    incremental_window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_history_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    incremental_column: Mapped[str | None] = mapped_column(String, nullable=True)
    where_filters: Mapped[str | None] = mapped_column(String, nullable=True)
    partition_by: Mapped[str | None] = mapped_column(String, nullable=True)
    partition_granularity: Mapped[str | None] = mapped_column(String, nullable=True)
    initial_load_chunk_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # v40+ BigQuery / catalog-context columns. ``bq_fqn`` and ``partition_col``
    # are BQ-specific; the rest are catalog metadata surfaced in /catalog UI.
    bq_fqn: Mapped[str | None] = mapped_column(String, nullable=True)
    partition_col: Mapped[str | None] = mapped_column(String, nullable=True)
    grain: Mapped[str | None] = mapped_column(String, nullable=True)
    platforms: Mapped[str | None] = mapped_column(String, nullable=True)
    history: Mapped[str | None] = mapped_column(Text, nullable=True)
    gotchas: Mapped[str | None] = mapped_column(Text, nullable=True)
    things_to_know: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_questions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    pairs_well_with: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # v74 (#607): distribution flag decoupled from query_mode. When True the
    # table is kept server-side & queryable via `agnes query --remote`, but
    # `agnes pull` does not download its parquet. Only meaningful for
    # query_mode IN ('local', 'materialized'); ignored for 'remote'.
    server_only: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    # v79: named source connections (spec 2026-06-12). NULL => default
    # connection for this row's source_type.
    connection_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # v116: table access policies (design doc 2026-08-11). One SQL policy
    # per table, substituted for the table on every server-side read when
    # access_policies.enabled is on. NULL access_policy_sql = no policy.
    # access_policy_note is the admin-facing "why" (mandatory at the API
    # layer when a policy is set, not enforced by the column itself).
    # policy_mapping marks this table as referenceable from another
    # table's policy body.
    access_policy_sql: Mapped[str | None] = mapped_column(String, nullable=True)
    access_policy_note: Mapped[str | None] = mapped_column(String, nullable=True)
    access_policy_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_policy_updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    policy_mapping: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    # v125: dedup bookkeeping for wave 2's headless semantic-model
    # auto-drafting session. NULL = no draft pending for this table.
    semantic_draft_pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_table_registry_source_type", "source_type"),
        Index("ix_table_registry_query_mode", "query_mode"),
    )


class SyncState(Base):
    __tablename__ = "sync_state"

    table_id: Mapped[str] = mapped_column(String, primary_key=True)
    last_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uncompressed_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    columns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hash: Mapped[str | None] = mapped_column(String, nullable=True)
    parts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String, server_default=text("'ok'"), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SyncHistory(Base):
    __tablename__ = "sync_history"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    table_id: Mapped[str] = mapped_column(String, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_sync_history_table_id", "table_id"),
        Index("ix_sync_history_synced_at", "synced_at"),
    )
