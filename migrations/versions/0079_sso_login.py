"""sso_config + user_external_identities — external SSO login (PG-only, A3 ratchet).

Runtime-configured external identity login (Entra ID OIDC, design doc
docs/superpowers/specs/2026-08-28-external-sso-login-design.md): the singleton
``sso_config`` row (admin-entered tenant/client/Fernet-encrypted secret/domain
allowlist/button label) and ``user_external_identities`` capturing the
validated token's ``tid``/``oid`` per Agnes user.

PG-first ratchet (A3): brand-new app-state tables, Alembic-only — there is no
matching DuckDB ``_vN_to_v(N+1)`` step and ``SCHEMA_VERSION`` does not move.

Revision ID: 0079_sso_login
Revises: 0078_facts_ingest_runs
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0079_sso_login"
down_revision: str | None = "0078_facts_ingest_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sso_config",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("provider_type", sa.String(), server_default=sa.text("'entra_oidc'"), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("client_secret_enc", sa.Text(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("allowed_email_domains", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 'default'", name="ck_sso_config_singleton"),
        sa.CheckConstraint("provider_type IN ('entra_oidc')", name="ck_sso_config_provider_type"),
    )
    op.create_table(
        "user_external_identities",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("provider_type", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("email_at_link", sa.String(), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("provider_type", "tenant_id", "subject", name="uq_user_external_identities_subject"),
    )


def downgrade() -> None:
    op.drop_table("user_external_identities")
    op.drop_table("sso_config")
