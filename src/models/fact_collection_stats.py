"""SQLAlchemy models behind the fact-graph collection-stats summary
(TCRD-296 synthesis E.21) — PG-only, A3 ratchet: no DuckDB sibling.

Mirrors ``migrations/versions/0104_fact_collection_stats.py``. See
``src/repositories/facts_pg.py``'s "Collection stats summary" section for
the maintenance contract (incremental on ``add_claim``, scoped rebuild on
the delete/reassign/merge/consolidation paths) and why every read of these
tables falls back to the original claims-scan query when a row is missing.

``FactCollectionMembership``/``EdgeCollectionMembership`` are the candidacy
index: one row per (collection, subject) that has at least one claim in
that collection — reading "does this subject have a claim in this
collection" becomes a primary-key lookup instead of a `claims` table scan.
``FactCollectionStats`` is the aggregate: one row per collection, the
numbers ``approximate_counts_for_collections``/the Library index/the admin
graph-counts card need without a per-request `GROUP BY` over `claims`.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class FactCollectionStats(Base):
    __tablename__ = "fact_collection_stats"

    corpus_id: Mapped[str] = mapped_column(String, primary_key=True)
    facts_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    claims_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    edges_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    documents_with_claims: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


class FactCollectionMembership(Base):
    __tablename__ = "fact_collection_membership"
    __table_args__ = (sa.Index("idx_fact_collection_membership_fact_id", "fact_id"),)

    corpus_id: Mapped[str] = mapped_column(String, primary_key=True)
    fact_id: Mapped[str] = mapped_column(String, ForeignKey("facts.id", ondelete="CASCADE"), primary_key=True)
    claims_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    #: COUNT(DISTINCT corpus_file_id) for this (fact, collection) pair — the
    #: exact number ``facet_top_values_for_collections`` ranks entity facet
    #: values by, kept alongside ``claims_count`` (several quotes from the
    #: SAME document must not inflate the document tally).
    documents_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)


class EdgeCollectionMembership(Base):
    __tablename__ = "edge_collection_membership"
    __table_args__ = (sa.Index("idx_edge_collection_membership_edge_id", "edge_id"),)

    corpus_id: Mapped[str] = mapped_column(String, primary_key=True)
    edge_id: Mapped[str] = mapped_column(String, ForeignKey("edges.id", ondelete="CASCADE"), primary_key=True)
    claims_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
