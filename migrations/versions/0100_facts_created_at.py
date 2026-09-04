"""facts.created_at — grace-period anchor for the orphan sweep (PG-only, A3
ratchet; column added to an already PG-only table, per docs/migrations.md
-> "Adding a PG-only feature").

Live finding (several concurrent SharePoint facts-extraction passes sharing
one Postgres, 2026-09): `FactsPgRepository.ingest_batch` ends every batch
with `sweep_orphans()`, which deletes any fact with zero claims and zero
claimed incident edges. A fact minted moments ago — as a node whose own
evidence is still deferred/rejected, or purely as an edge endpoint
(`_endpoint()`'s fallback, its own transaction, committed before the edge
that anchors it) — looks exactly like a genuine orphan to a CONCURRENT
pass's own end-of-batch sweep, which raced ahead and deleted it before this
pass could attach its claim: 75 447 subjects deleted against 10 784 created
in 30 minutes on one instance, ~13% of documents failing with
`ForeignKeyViolation` on `claims.fact_id`/`edges.src`.

`created_at` lets `sweep_orphans()` skip any subject younger than a grace
period (default 15 minutes, `FactsPgRepository._ORPHAN_SWEEP_GRACE_S`) —
long enough to outlast the gap between a batch's own commits, however many
concurrent passes are running. `edges` needs no equivalent column: an edge
only ever commits already carrying its final claim set for the batch (or
legitimately zero, e.g. a claimless `possible_duplicate_of` proposal, exempt
from the sweep already), so there is no analogous "temporarily zero-claim"
window a concurrent pass could observe.

Revision ID: 0100_facts_created_at
Revises: 0099_ingest_runs_edges_skipped
Create Date: 2026-09-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0100_facts_created_at"
down_revision: Union[str, None] = "0099_ingest_runs_edges_skipped"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "facts",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("facts", "created_at")
