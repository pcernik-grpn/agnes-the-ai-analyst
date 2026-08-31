"""facts_ingest_runs.llm_usage — cost-visibility tally the extraction
producer optionally reports per ingest batch (PG-only, A3 ratchet; column
added to an already PG-only table, per docs/migrations.md -> "Adding a
PG-only feature", same precedent as 0081/0083 on this table).

The extraction producer now sends a per-run ``llm_usage`` block (tokens +
prompt-cache + wall time) alongside its ``documents``/``nodes``/``edges``
(``app/api/facts.py::FactsIngestLlmUsage``) — purely additive metadata, never
part of the ingest fingerprint/idempotency logic. This column persists it
next to the rest of the run report (``GET /api/facts/ingest-runs``).

Deliberately NULLABLE with no server default, unlike ``anonymization``
(0081) and ``source_urls_rejected`` (0083) on this same table: those two are
"a producer that never uses the feature omits the block, and the column
must still read as an always-present empty shape". ``llm_usage`` is
different — a producer that never reports usage at all (every run before
this feature shipped, and any producer build that doesn't yet send it)
should read back as "no usage figure available", not "zero tokens spent",
so ``NULL`` is the honest default, not ``'{}'::jsonb``.

Revision ID: 0086_ingest_runs_llm_usage
Revises: 0085_alias_edge_backfill
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0088_ingest_runs_llm_usage"
down_revision: Union[str, None] = "0087_corpus_file_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "facts_ingest_runs",
        sa.Column("llm_usage", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("facts_ingest_runs", "llm_usage")
