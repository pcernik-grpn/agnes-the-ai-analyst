"""fact_collection_type_counts — per-type companion to fact_collection_stats
(TCRD-296 gap #81), PG-ONLY (A3 ratchet).

``collection_facts_summary``'s per-type breakdown (the Library collection
page's "27-row" type summary) was the one leg `fact_collection_stats`
(migration ``0105``) did not cover: every render re-derived it with
``SELECT f.type, COUNT(*) FROM visible v JOIN facts f ON f.id = v.subject_id
GROUP BY f.type`` — on a ~291k-file / 801k-fact collection this is a
`Parallel Seq Scan` of the whole ``facts`` table (837k rows) hash-joined to
``fact_collection_membership`` (801k rows) to produce 27 output rows,
measured at 4.5-4.8s per call on a live instance. This table is the same
"maintained, not recomputed" shape as its sibling: one row per
``(corpus_id, type)``, kept current by the SAME three writers
(``FactsPgRepository._bump_collection_stats_impl`` on the hot ingest path,
``_decrement_collection_stats_impl`` on the per-file delete path, and
``_rebuild_one_collection_stats`` on every bulk-mutation/consolidation path
and the explicit admin repair tool), so a caller who can already read
``fact_collection_stats`` without RBAC narrowing (an unrestricted admin —
the same population that already trusts ``facts_count``/``edges_count`` as
an "approximate" number, see ``approximate_counts_for_collections``'s
docstring) can read the per-type breakdown the same way instead of joining
``facts`` for every visible candidate. A narrowed (non-admin) caller's
type breakdown is UNCHANGED — it stays the exact, caller-scoped CTE
computation, because this table (like its sibling) does not, and cannot,
account for per-caller visibility.

This migration only CREATES the table — like ``0105``, it does not
backfill. ``rebuild_collection_stats``/``agnes admin facts stats rebuild``
populates it for every collection already covered by that rebuild path.

Revision ID: 0110_fact_collection_type_counts
Revises: 0109_merge_access_stack
Create Date: 2026-09-06
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0111_fact_collection_type_counts"
down_revision = "0110_sharepoint_crawl_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fact_collection_type_counts",
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("corpus_id", "type"),
    )


def downgrade() -> None:
    op.drop_table("fact_collection_type_counts")
