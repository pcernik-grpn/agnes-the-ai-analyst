"""SQLAlchemy models for the Collections cluster (v82):
file_corpora, corpus_files, corpus_chunks.

Mirrors DuckDB DDL in src/db.py (_v81_to_v82 / _SYSTEM_SCHEMA).

PG notes:
- corpus_chunks.embedding uses real[] (sa.ARRAY(sa.REAL), float4 — matches the
  DuckDB FLOAT[384] storage precision); pgvector vector(384) is a
  Retrieval-slice option, not a foundation dependency.
- processing_detail stores JSON as VARCHAR text (same as DuckDB side);
  no JSONB cast needed since reads come back as strings.
- CorpusChunk.text_ is mapped to the DB column "text" via __table_args__
  style; we use sa.text_ alias to avoid shadowing the imported sa.text().
- CorpusFileSource (v125-equivalent, PG-only — A3 ratchet, no DuckDB side)
  is the crawler-anchor mapping from the fact-graph-over-Collections design
  (docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md
  §6): maps a producer's stable id to a ``corpus_files`` row so a re-sync or
  a manual path re-upload of the same document preserves that row's id
  instead of cascading its (future) claims away.
- CorpusFileEvent (PG-only — A3 ratchet, no DuckDB side) is the append-only
  observed-change log behind the SharePoint "what changed between A and B"
  feed (``GET /api/admin/sharepoint/connections/{id}/changes``). Written
  best-effort from ``app/api/collections.py`` at the two points that already
  know the classification (upsert-on-upload, delete) — never on the read
  path, and never allowed to fail the upload/delete it describes. A snapshot
  table like ``corpus_files`` cannot answer "was this an update or a
  rename" after the fact (both just bump ``updated_at``), which is why this
  needs its own event row instead of being derived at query time.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import REAL, BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base

_text = sa.text  # alias so column named "text" doesn't shadow it


class FileCorpus(Base):
    __tablename__ = "file_corpora"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    # v102: provenance for the Library's Source facet — 'uploaded' (a person
    # brought the file in) or 'generated' (an agent authored it).
    origin: Mapped[str] = mapped_column(String, server_default="uploaded", nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=_text("CURRENT_TIMESTAMP"),
        nullable=True,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=_text("CURRENT_TIMESTAMP"),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CorpusFile(Base):
    __tablename__ = "corpus_files"
    # At most one row per (corpus_id, path). Plain unique index (NULLs distinct
    # → path=NULL rows exempt); mirrors the DuckDB `_v96_to_v97` index.
    __table_args__ = (sa.Index("idx_corpus_files_corpus_path", "corpus_id", "path", unique=True),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    file_type: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # Set on children extracted from an uploaded archive (K1 bundle ingest);
    # NULL for directly-uploaded files and for the archive row itself.
    parent_file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Optional caller-supplied logical identity for upsert-on-upload; a repeat
    # upload with the same (corpus_id, path) replaces the row. NULL = plain insert.
    path: Mapped[str | None] = mapped_column(String, nullable=True)
    # Five-state lifecycle: pending | processing | indexed | needs_review | rejected
    processing_status: Mapped[str] = mapped_column(String, server_default=_text("'pending'"), nullable=False)
    # JSON text: {tier, vision_used, error, derived_table_id, chunk_count}
    processing_detail: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=_text("CURRENT_TIMESTAMP"),
        nullable=True,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=_text("CURRENT_TIMESTAMP"),
        nullable=True,
    )


class CorpusChunk(Base):
    __tablename__ = "corpus_chunks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String, nullable=False)
    file_id: Mapped[str] = mapped_column(String, nullable=False)
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Column is named "text" in DB; attribute uses same name — we've imported
    # sa.text as _text above so there is no shadowing.
    text: Mapped[str | None] = mapped_column("text", String, nullable=True)
    # real[] (float4): matches the DuckDB FLOAT[384] storage precision so
    # embeddings round-trip identically on both backends; pgvector vector(384)
    # is a Retrieval-slice option.
    embedding: Mapped[list[float] | None] = mapped_column(
        PG_ARRAY(REAL()),
        nullable=True,
    )
    section_path: Mapped[str | None] = mapped_column(String, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox: Mapped[str | None] = mapped_column(String, nullable=True)
    # "metadata" is reserved by SQLAlchemy's Declarative API; map the DB
    # column "metadata" via an explicit column name argument to avoid the clash.
    chunk_metadata: Mapped[str | None] = mapped_column("metadata", String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=_text("CURRENT_TIMESTAMP"),
        nullable=True,
    )


class CorpusFileSource(Base):
    """Crawler-anchor mapping (PG-only, A3 ratchet — see module docstring).

    ``(corpus_id, source_stable_id)`` is the producer's delta key (survives
    rename/move); ``corpus_file_id`` is the row it currently resolves to.
    """

    __tablename__ = "corpus_file_sources"
    __table_args__ = (
        sa.UniqueConstraint("corpus_id", "source_stable_id", name="uq_corpus_file_sources_corpus_stable_id"),
        sa.Index("idx_corpus_file_sources_corpus_doc", "corpus_id", "source_doc_id"),
    )

    corpus_file_id: Mapped[str] = mapped_column(
        String, ForeignKey("corpus_files.id", ondelete="CASCADE"), primary_key=True
    )
    corpus_id: Mapped[str] = mapped_column(String, nullable=False)
    source_stable_id: Mapped[str] = mapped_column(String, nullable=False)
    # Rewritten when a provisional doc_id (sha256(cTag|stable_id)) is
    # replaced by the real one on first content crawl (spec §6).
    source_doc_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)


class CorpusFileEvent(Base):
    """Append-only observed-change log (PG-only, A3 ratchet — see module
    docstring). One row per add/update/rename/delete transition a
    ``corpus_files`` upsert or delete ALREADY classifies as a side effect of
    its own existing logic — this table only persists that classification,
    it never invents new business logic. No foreign key to ``corpus_files``:
    a ``deleted`` row must outlive the row it describes.
    """

    __tablename__ = "corpus_file_events"
    __table_args__ = (sa.Index("idx_corpus_file_events_corpus_observed", "corpus_id", "observed_at", "id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String, nullable=False)
    file_id: Mapped[str] = mapped_column(String, nullable=False)
    source_stable_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # added | updated | renamed | deleted
    change: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str | None] = mapped_column(String, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
