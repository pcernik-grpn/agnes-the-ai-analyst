"""corpus_file_events — append-only observed-change log (PG-only, A3 ratchet).

Backs the SharePoint "what changed between A and B" feed (``GET
/api/admin/sharepoint/connections/{id}/changes``). Survey before adding
this table (see the SharePoint changes-feed design note): ``corpus_files``'
own ``created_at``/``updated_at`` cleanly derive an "added" event, but a
snapshot table can never answer "was the last touch a content update or a
metadata-only rename" after the fact — both just bump ``updated_at`` to the
same current row, with no record of what it looked like before. Deletions
are the same problem one step further: the row (and its cascade-deleted
``corpus_file_sources`` mapping) is simply gone, and the only surviving
trace — a generic ``collection.file_delete`` audit_log row keyed on
``file_id``/``collection_id`` — carries no filename/path. This table
persists, at write time, the classification ``app/api/collections.py``'s
existing upsert/delete logic already computes as a side effect of its own
matching (stable-id/path lookup, sha256 comparison) — it adds no new
business logic, only a durable record of a decision already made.

PG-first ratchet (A3): brand-new app-state table, Alembic-only — there is no
matching DuckDB ``_vN_to_v(N+1)`` step and ``SCHEMA_VERSION`` does not move;
``src/repositories/corpus_files.py`` (DuckDB) gains no capability that
depends on this table. Writes from ``app/api/collections.py`` are
best-effort (never allowed to fail the upload/delete they describe) and
silently no-op on a DuckDB-backed instance, exactly like
``facts_ingest_runs``'s own best-effort report write.

Renumbered before merge: this was cut as ``0086_corpus_file_events`` against
``0085_alias_edge_backfill``, but ``0086_claims_audience`` landed on that same
parent first. Keeping the original id would have left the ladder with two heads
and broken ``alembic upgrade head`` outright — invisible on this branch alone,
which is why CI was green. The schema change itself is untouched; only the
revision id and the parent it chains onto moved.

Revision ID: 0087_corpus_file_events
Revises: 0086_claims_audience
Create Date: 2026-08-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0087_corpus_file_events"
down_revision: Union[str, None] = "0086_claims_audience"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "corpus_file_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("source_stable_id", sa.String(), nullable=True),
        sa.Column("change", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=True),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_corpus_file_events_corpus_observed",
        "corpus_file_events",
        ["corpus_id", "observed_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_corpus_file_events_corpus_observed", "corpus_file_events")
    op.drop_table("corpus_file_events")
