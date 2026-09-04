"""facts_ingest_runs.edges_skipped_missing_endpoint — visible count of edges
`ingest_batch` skipped because their `src`/`dst` fact was gone by the time
the edge INSERT ran (PG-only, A3 ratchet; column added to an already
PG-only table, per docs/migrations.md -> "Adding a PG-only feature").

Live finding (two facts-extraction passes plus SharePoint crawls sharing
one PG connection pool, 2026-09): an `edges` INSERT occasionally hit
`ForeignKeyViolation: Key (src)=(f_...) is not present in table "facts"` —
the endpoint fact resolved fine moments earlier but was deleted since,
either by a concurrent `ingest_batch` call's own end-of-call
`sweep_orphans()` racing a not-yet-committed sibling write, or by two
passes independently merging/deduplicating the same entity.
`src/repositories/facts_pg.py::EdgeEndpointMissing` now catches this and
`ingest_batch` skips only the one affected edge rather than failing the
whole batch. This column makes that skip visible on the run report, same
shape as `claims_rejected_count`/`source_urls_rejected_count` on this same
table: a plain integer the source card's badge can read without decoding
JSONB — no itemized detail list, since the endpoint node ids are already
in the ingest logs and this counts a race, not a producer mistake to fix.

Revision ID: 0099_ingest_runs_edges_skipped
Revises: 0098_corpus_chunks_file_id_index
Create Date: 2026-09-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0099_ingest_runs_edges_skipped"
down_revision: Union[str, None] = "0098_corpus_chunks_file_id_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "facts_ingest_runs",
        sa.Column("edges_skipped_missing_endpoint", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("facts_ingest_runs", "edges_skipped_missing_endpoint")
