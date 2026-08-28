"""facts_ingest_runs — persisted run reports for POST /api/facts/ingest
(PG-only, A3 ratchet).

Every ingest batch (spec §7.2) already RETURNS a run report; this table
persists one row per batch so the source card (spec §13.2 "Source card") has
something to read its pipeline-strip counts and error badges from without an
admin having to have been watching the live response. Written by
``src/repositories/facts_ingest_runs_pg.py`` — a repository distinct from
``src/repositories/facts_pg.py`` because a run-report write must NEVER roll
back an ingest (``app/api/facts.py::facts_ingest`` writes it AFTER
``ingest_batch()`` commits, log-and-continue on failure).

PG-first ratchet (A3): brand-new app-state table, Alembic-only — no matching
DuckDB ``_vN_to_v(N+1)`` step, ``SCHEMA_VERSION`` does not move.

Revision ID: 0077_facts_ingest_runs
Revises: 0076_facts_tables
Create Date: 2026-08-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0077_facts_ingest_runs"
down_revision: Union[str, None] = "0076_facts_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "facts_ingest_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("corpus_ids", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("caller", sa.String(), nullable=False),
        sa.Column("documents_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claims_written", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claims_rejected_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claims_rejected", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("deferred", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("subjects_created", sa.Integer(), server_default="0", nullable=False),
        sa.Column("subjects_deleted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("review_items", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_facts_ingest_runs_created_at", "facts_ingest_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_facts_ingest_runs_created_at", "facts_ingest_runs")
    op.drop_table("facts_ingest_runs")
