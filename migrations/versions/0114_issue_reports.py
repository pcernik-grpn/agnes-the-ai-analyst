"""issue_reports + issue_comments (issue reporting, step 1)

PG-ONLY (A3 PG-first ratchet): this table pair landed after the DuckDB
app-state backend was frozen, so there is no ``src/db.py`` ladder step and
no DuckDB repository. ``SCHEMA_VERSION`` does not move.

Revision ID: 0114_issue_reports
Revises: 0113_data_apps_data_identity
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0114_issue_reports"
down_revision: str | None = "0113_data_apps_data_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence("issue_reports_number_seq"), if_not_exists=True))
    op.create_table(
        "issue_reports",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "number", sa.Integer(), nullable=False, server_default=sa.text("nextval('issue_reports_number_seq')")
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False, server_default=sa.text("'bug'")),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_by_email", sa.String(), nullable=True),
        sa.Column("source_surface", sa.String(), nullable=False, server_default=sa.text("'web'")),
        sa.Column("page_url", sa.Text(), nullable=True),
        sa.Column("context_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("screenshot_path", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "last_activity_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("webhook_delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("number", name="uq_issue_reports_number"),
    )
    op.create_index("idx_issue_reports_status", "issue_reports", ["status"])
    op.create_index("idx_issue_reports_created_by", "issue_reports", ["created_by"])
    op.create_table(
        "issue_comments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("issue_id", sa.String(), sa.ForeignKey("issue_reports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issue_owner_id", sa.String(), nullable=False),
        sa.Column("author_id", sa.String(), nullable=True),
        sa.Column("author_email", sa.String(), nullable=True),
        sa.Column("author_kind", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
    )
    op.create_index("idx_issue_comments_issue_created", "issue_comments", ["issue_id", "created_at"])
    op.create_index("idx_issue_comments_owner", "issue_comments", ["issue_owner_id"])


def downgrade() -> None:
    op.drop_index("idx_issue_comments_owner", table_name="issue_comments")
    op.drop_index("idx_issue_comments_issue_created", table_name="issue_comments")
    op.drop_table("issue_comments")
    op.drop_index("idx_issue_reports_created_by", table_name="issue_reports")
    op.drop_index("idx_issue_reports_status", table_name="issue_reports")
    op.drop_table("issue_reports")
    op.execute(sa.schema.DropSequence(sa.Sequence("issue_reports_number_seq"), if_exists=True))
