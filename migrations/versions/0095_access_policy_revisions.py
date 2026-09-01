"""access_policy_revisions — one row per saved state of a table's access
policy, so the admin UI can offer "restore this version" (#1979 K1-sweep
finding 1).

Why a new table rather than reading the audit trail: commit 34ffc42
deliberately redacts ``access_policy_sql`` out of ``audit_log.params``
(content never enters the trail). An audit row therefore records that a
policy changed, by whom and when — but never what it WAS, and restoring a
version needs the body. The trail keeps events; this keeps bodies.

Why not more columns on ``table_registry``: that row holds the CURRENT
policy (``access_policy_sql``/``_note``/``_updated_at``/``_updated_by``),
one state at a time, and it is a frozen pre-A3 DuckDB<->PG pair. History is
a different cardinality (many per table) and a different lifetime (it
outlives every individual edit).

``table_id`` is not a foreign key on purpose: unregistering a table drops
its revisions explicitly (``AccessPolicyRevisionsPgRepository.
delete_for_table``, called from ``DELETE /api/admin/registry/{id}``), so a
re-registered id — table ids are derived from names — cannot inherit a
stranger's policy bodies. A FK would additionally make this post-A3 PG-only
table a hard dependency of a frozen pre-A3 one.

PG-first ratchet (A3): brand-new app-state table, Alembic-only — no matching
DuckDB ``_vN_to_v(N+1)`` step, ``SCHEMA_VERSION`` does not move.

Revision ID: 0095_access_policy_revisions
Revises: 0094_extraction_runs
Create Date: 2026-09-01
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0095_access_policy_revisions"
down_revision: Union[str, None] = "0094_extraction_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "access_policy_revisions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("table_id", sa.String(), nullable=False),
        # NULL means this revision IS the clear — the row that records when
        # protection was removed, which is the one an inheriting admin most
        # needs to be able to find (and, having found it, to restore the
        # policy that preceded it).
        sa.Column("policy_sql", sa.Text(), nullable=True),
        sa.Column("policy_note", sa.Text(), nullable=True),
        # Snapshot of `table_registry.policy_mapping` at save time. Restoring
        # a revision does not flip it back; it is here so a reader can see
        # the shape the policy was written against.
        sa.Column("policy_mapping", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("saved_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        # Nullable: a backfilled baseline whose `access_policy_updated_by`
        # was never recorded is still a real revision, and "unknown" is an
        # honest answer where an invented one is not.
        sa.Column("saved_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_access_policy_revisions_table_saved",
        "access_policy_revisions",
        ["table_id", "saved_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_access_policy_revisions_table_saved", "access_policy_revisions")
    op.drop_table("access_policy_revisions")
