"""fact_collection_stats / fact_collection_membership / edge_collection_membership
— maintained per-collection summary of the fact graph (TCRD-296 synthesis
E.21), PG-ONLY (A3 ratchet).

On a 2M-claim / 397-collection live instance, every read of the graph's
read surface (``/api/facts/facets``, ``/api/facts/type-map``, the Library
index, admin graph counts) re-derived visibility candidacy by scanning the
whole ``claims`` table — 5-8s per request under crawl load. These three
tables are a maintained index over "which fact/edge has a claim in which
collection" and "how big is this collection" — kept current incrementally
by ``FactsPgRepository.add_claim`` on the hot ingest path, and by a scoped
``rebuild_collection_stats()`` recompute on the bulk delete/reassign/merge/
consolidation paths. See ``src/repositories/facts_pg.py``'s "Collection
stats summary" section and ``docs/architecture.md`` -> "Fact graph
collection-stats summary".

This migration only CREATES the tables — it does not backfill existing
claims. Backfilling ~2M rows grouped by collection inside a migration's own
transaction (which blocks the app from serving traffic until it commits)
was measured against ``docs/migrations.md``'s own guidance for expensive
DDL/data work on an already-large table and rejected as unsafe to run
unattended at process start; see ``rebuild_collection_stats``'s docstring.
**Operators upgrading a live instance must run ``agnes admin facts stats
rebuild`` (or ``POST /api/admin/facts/stats/rebuild``) once after this
migration lands** — every read path that consults these tables falls back
to the pre-existing full-scan query (logged once) until that first rebuild
populates them, so a delayed rebuild degrades to the old behavior rather
than serving wrong answers.

Revision ID: 0104_fact_collection_stats
Revises: 0103_crawl_shards
Create Date: 2026-09-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0104_fact_collection_stats"
down_revision: Union[str, None] = "0103_crawl_shards"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fact_collection_stats",
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.Column("facts_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claims_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("edges_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("documents_with_claims", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("corpus_id"),
    )
    op.create_table(
        "fact_collection_membership",
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.Column("fact_id", sa.String(), nullable=False),
        sa.Column("claims_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("documents_count", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["fact_id"], ["facts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("corpus_id", "fact_id"),
    )
    op.create_index("idx_fact_collection_membership_fact_id", "fact_collection_membership", ["fact_id"], unique=False)
    op.create_table(
        "edge_collection_membership",
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.Column("edge_id", sa.String(), nullable=False),
        sa.Column("claims_count", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["edge_id"], ["edges.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("corpus_id", "edge_id"),
    )
    op.create_index("idx_edge_collection_membership_edge_id", "edge_collection_membership", ["edge_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_edge_collection_membership_edge_id", table_name="edge_collection_membership")
    op.drop_table("edge_collection_membership")
    op.drop_index("idx_fact_collection_membership_fact_id", table_name="fact_collection_membership")
    op.drop_table("fact_collection_membership")
    op.drop_table("fact_collection_stats")
