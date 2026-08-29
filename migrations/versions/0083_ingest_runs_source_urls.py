"""facts_ingest_runs.source_urls_rejected(_count) — visible count of a
producer's `documents[].source_url` values Agnes dropped at ingest (O7
follow-up, PG-only, A3 ratchet; column added to an already PG-only table,
per docs/migrations.md -> "Adding a PG-only feature").

`_validate_source_url` (src/repositories/facts_pg.py) already drops an
invalid `source_url` silently rather than reject the surrounding claim — the
right default, but "silently" is exactly the failure class this whole O7
change fixes: a producer-sent value that vanished without a trace. This
column makes the drop visible on the run report instead, same shape as
`claims_rejected_count`/`claims_rejected` on this same table: a plain
integer the source card's badge reads without decoding JSONB, plus the
itemized `{doc_id, reason}` detail list for the drawer.

Revision ID: 0083_ingest_runs_source_urls
Revises: 0082_session_revoked_before
Create Date: 2026-08-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0083_ingest_runs_source_urls"
down_revision: Union[str, None] = "0082_session_revoked_before"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "facts_ingest_runs",
        sa.Column("source_urls_rejected_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "facts_ingest_runs",
        sa.Column("source_urls_rejected", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("facts_ingest_runs", "source_urls_rejected")
    op.drop_column("facts_ingest_runs", "source_urls_rejected_count")
