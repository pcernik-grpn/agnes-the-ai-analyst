"""share_requests — agent-sharing approval queue (Track C6, PG-only, A3
ratchet).

A user may build agents freely, but SHARING one needs admin approval: when a
non-admin owner shares an ``agent`` (``PUT /api/sharing/agent/{id}``), the
would-be ``resource_grants`` write is deferred into a row here instead of
being written immediately (``app/services/library_sharing.py::set_shares``).
An admin decides via ``POST /api/admin/share-requests/{id}/approve`` (writes
the grant through the existing ``resource_grants_repo().ensure_grant``) or
``.../reject`` (no grant, row marked ``rejected``). Sharing into a group by
an admin actor stays instant and never touches this table.

PG-first ratchet (A3): brand-new app-state table, Alembic-only — no matching
DuckDB ``_vN_to_v(N+1)`` step, ``SCHEMA_VERSION`` does not move.
``src/repositories/share_requests_pg.py`` is the only repository; there is
no DuckDB sibling, so this feature fails clean (``501
requires_postgres_backend``) on a DuckDB-backed instance.

Revision ID: 0079_share_requests
Revises: 0078_facts_ingest_runs
Create Date: 2026-08-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0079_share_requests"
down_revision: Union[str, None] = "0078_facts_ingest_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "share_requests",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("requested_group_id", sa.String(), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["requested_group_id"], ["user_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_share_requests_status", "share_requests", ["status"])
    op.create_index("idx_share_requests_resource", "share_requests", ["resource_type", "resource_id"])
    # Structural backstop against a double-submitted PUT racing the
    # application-level idempotency check in `set_shares`: at most one
    # PENDING request per (resource, group) at a time.
    op.create_index(
        "uq_share_requests_pending",
        "share_requests",
        ["resource_type", "resource_id", "requested_group_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_share_requests_pending", table_name="share_requests")
    op.drop_index("idx_share_requests_resource", table_name="share_requests")
    op.drop_index("idx_share_requests_status", table_name="share_requests")
    op.drop_table("share_requests")
