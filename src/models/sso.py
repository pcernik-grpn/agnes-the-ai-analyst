"""SQLAlchemy models for the external SSO login (design 2026-08-28).

PG-only (A3 ratchet): both tables live only on the Postgres app-state
backend — ``migrations/versions/0079_sso_login.py``, no DuckDB sibling.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class SsoConfig(Base):
    """Singleton runtime config for the external identity login (``sso`` slot).

    One external IdP per instance by construction: ``id`` is CHECK-pinned to
    ``'default'``. ``provider_type`` is the protocol discriminator — only
    ``'entra_oidc'`` today; a future SAML integration widens the CHECK, not
    the table shape. ``client_secret_enc`` is a Fernet token
    (``app.secrets_vault``) — write-only through the repo, never selected by
    ``get_config()``.
    """

    __tablename__ = "sso_config"
    __table_args__ = (
        CheckConstraint("id = 'default'", name="ck_sso_config_singleton"),
        CheckConstraint("provider_type IN ('entra_oidc')", name="ck_sso_config_provider_type"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_type: Mapped[str] = mapped_column(String, server_default=text("'entra_oidc'"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    client_id: Mapped[str] = mapped_column(String, nullable=False)
    client_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    allowed_email_domains: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)


class UserExternalIdentity(Base):
    """One external principal bound to one Agnes user (``user_id`` is the PK).

    ``subject`` stores Entra's ``oid`` claim — the immutable per-tenant
    directory object ID that Microsoft Graph reports as the user's ``id`` —
    deliberately NOT the OIDC ``sub``: Entra's ``sub`` is pairwise per app
    registration, so it changes when the customer re-creates the app
    registration and correlates with nothing outside that one client. Do not
    "fix" this to ``sub``.

    ``tenant_id`` stores the validated token's ``tid`` claim (always the
    directory GUID), never the admin-configured tenant string (which may be a
    verified domain). ``oid`` is unique only *within* a tenant, hence the
    ``(provider_type, tenant_id, subject)`` uniqueness key.
    """

    __tablename__ = "user_external_identities"
    __table_args__ = (
        UniqueConstraint("provider_type", "tenant_id", "subject", name="uq_user_external_identities_subject"),
    )

    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    provider_type: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    email_at_link: Mapped[str] = mapped_column(String, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
