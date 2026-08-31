"""claims.audience — Slice 4b (2026-08-30 sharepoint-acl-mirroring plan,
Task 10; spec 2026-08-28-sharepoint-acl-mirroring-design.md §4.2-§4.4):
index-time audience-variant tagging for evidence claims.

Adds ``claims.audience`` (nullable ``TEXT`` — an untagged claim,
i.e. ``NULL``, stays unrestricted-within-collection, today's behavior) and
an index over ``(corpus_id, audience)`` backing the visibility predicate's
audience selector (``src/repositories/facts_pg.py``'s
``FactsPgRepository._visibility_predicate``): among a caller's readable
claims, the predicate now ALSO requires either an untagged claim outside a
MUST NOT-tiered collection, or a tagged claim whose ``(corpus_id,
audience)`` pair the caller holds — one AND term, the same single place
every existing visibility check already funnels through.

PG-first ratchet (A3): a schema CHANGE on an existing PG-only table
(``claims`` has no DuckDB sibling at all — see ``0076_facts_tables.py``) —
Alembic-only, no matching DuckDB ``_vN_to_v(N+1)`` step.

Revision ID: 0086_claims_audience
Revises: 0085_alias_edge_backfill
Create Date: 2026-08-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0086_claims_audience"
down_revision: Union[str, None] = "0085_alias_edge_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("claims", sa.Column("audience", sa.Text(), nullable=True))
    op.create_index("idx_claims_corpus_audience", "claims", ["corpus_id", "audience"])


def downgrade() -> None:
    op.drop_index("idx_claims_corpus_audience", "claims")
    op.drop_column("claims", "audience")
