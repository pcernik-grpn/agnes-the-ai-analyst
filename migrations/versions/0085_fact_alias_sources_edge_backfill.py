"""Catch-up backfill: edge-anchor aliases missed by 0084's original shape.

``0084_fact_alias_sources`` shipped with a backfill that walked only claims
on an alias's OWN ``fact_id``. That misses a node which has never carried a
claim of its own and is reachable only as the anchor of an evidenced edge
— the ordinary ``works_in_industry``/``sponsored_by``/``staffed_by``-shaped
ontology row, where the evidence sits on the edge rather than the node. A
live run found 20 of 81 aliases in exactly that shape, all backfilled with
ZERO provenance rows, which makes those names invisible to every caller.

0084 has since been corrected in place, which is enough for an instance
that had not yet run it. An instance that ALREADY applied the original
0084 will never re-run it — Alembic records the revision, not its content
— so those aliases would stay dark forever. This revision is that
instance's only route to the fix.

Deliberately re-runs the WHOLE backfill (both branches) rather than the
edge branch alone: ``ON CONFLICT DO NOTHING`` on the natural primary key
makes it a no-op for every row 0084 already wrote, so one statement is
correct for both populations — a fresh instance that ran the corrected
0084 inserts nothing here, and an instance carrying the original 0084
gains exactly its missing edge-anchor rows. No new visibility is granted
that the corrected 0084 would not already have granted on a fresh install.

Revision ID: 0085_fact_alias_sources_edge_backfill
Revises: 0084_fact_alias_sources
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0085_fact_alias_sources_edge_backfill"
down_revision: Union[str, None] = "0084_fact_alias_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO fact_alias_sources (type, natural_key, corpus_id) "
            "SELECT fa.type, fa.natural_key, c.corpus_id "
            "FROM fact_aliases fa JOIN claims c ON c.fact_id = fa.fact_id "
            "UNION "
            "SELECT fa.type, fa.natural_key, c.corpus_id "
            "FROM fact_aliases fa "
            "JOIN edges e ON (e.src = fa.fact_id OR e.dst = fa.fact_id) "
            "JOIN claims c ON c.edge_id = e.id "
            "ON CONFLICT DO NOTHING"
        )
    )


def downgrade() -> None:
    """No-op: the rows this adds are indistinguishable from 0084's own, and
    dropping them would blank provenance 0084 is entitled to have written.
    ``0084``'s ``downgrade()`` drops the whole table anyway."""
