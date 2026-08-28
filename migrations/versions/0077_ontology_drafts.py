"""ontology_drafts — the ontology builder's working state (PG-only, A3 ratchet).

Fact-graph-over-Collections §13.2 ("Ontology builder"): a draft holds the
node/edge types an admin is authoring, the document sample picked for
dry-run, and the last translation leftover report, so Save
(``POST /api/admin/ontology/drafts/{id}/save``) is the only write that
touches the live ontology (``semantic_models``) — every other edit stays
confined to this table.

PG-first ratchet (A3): brand-new app-state surface added after the freeze —
Alembic-only, no matching DuckDB `_vN_to_v(N+1)` step, `SCHEMA_VERSION` does
not move. `src/repositories/ontology_drafts_pg.py` is the only repository;
there is no DuckDB sibling.

Revision ID: 0077_ontology_drafts
Revises: 0076_facts_tables
Create Date: 2026-08-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0077_ontology_drafts"
down_revision: Union[str, None] = "0076_facts_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ontology_drafts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("node_types", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("edge_types", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("document_sample", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("leftover_report", sa.Text(), nullable=True),
        sa.Column("dry_run_results", JSONB(), nullable=True),
        sa.Column("saved_model_slug", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_ontology_drafts_created_by", "ontology_drafts", ["created_by"])


def downgrade() -> None:
    op.drop_index("idx_ontology_drafts_created_by", "ontology_drafts")
    op.drop_table("ontology_drafts")
