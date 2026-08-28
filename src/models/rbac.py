"""SQLAlchemy models for users + the RBAC tables.

Mirrors the DuckDB shapes in ``src/db.py``:
  - ``users``               (lines 55-79)
  - ``user_groups``         (lines 434-441)
  - ``user_group_members``  (lines 454-461)
  - ``resource_grants``     (lines 470-478)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    setup_token: Mapped[str | None] = mapped_column(String, nullable=True)
    setup_token_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reset_token: Mapped[str | None] = mapped_column(String, nullable=True)
    reset_token_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))
    last_pull_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # v71: Slack identity binding (NULL until /agnes verification code redeemed).
    slack_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # v77: force a password change on first sign-in for accounts whose password
    # was set by someone else (seed admin from SEED_ADMIN_PASSWORD, admin-set
    # passwords). Cleared when the user sets their own password.
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))
    # Issue #1676: server-side session revocation, PG-only (A3 ratchet — see
    # migrations/versions/0079_session_revoked_before.py). A `typ="session"`
    # JWT whose `iat` predates this timestamp is refused by
    # `app.auth.pat_resolver.resolve_token_to_user` even though its signature
    # and `exp` are still valid — the mechanism `POST /auth/logout` uses to end
    # a session server-side, not merely clear the browser cookie. NULL means "no
    # floor set" (every pre-existing session stays valid until its own `exp` —
    # a deploy of this column does not itself invalidate anything). DuckDB has
    # no matching column (frozen post-A3 schema): the check simply never fires
    # there, and `revoke_sessions()` is a documented no-op on that backend.
    session_revoked_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserGroup(Base):
    __tablename__ = "user_groups"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)


class UserGroupMember(Base):
    __tablename__ = "user_group_members"

    user_id: Mapped[str] = mapped_column(String, nullable=False)
    group_id: Mapped[str] = mapped_column(String, ForeignKey("user_groups.id"), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    added_by: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "group_id"),
        Index("ix_user_group_members_user_id", "user_id"),
        Index("ix_user_group_members_group_id", "group_id"),
        Index("ix_user_group_members_source", "source"),
    )


class ResourceGrant(Base):
    """Generic ``(group, resource_type, resource_id)`` access tuple.

    Per-type FK design (E.3, migration 0013):

    Five of the six ResourceTypes target tables with stable surrogate
    UUIDs. Each gets a dedicated NULLable FK column that carries
    ON DELETE CASCADE, so DB-level integrity is enforced and orphan
    grants are automatically removed when the parent row is deleted.

    +------------------------+--------------------------------+------------------------------+
    | resource_type          | resource_id shape              | per-type FK column           |
    +========================+================================+==============================+
    | ``table``              | ``<table_id>`` (UUID)          | resource_id_table            |
    +------------------------+--------------------------------+------------------------------+
    | ``data_package``       | ``<package_id>`` (UUID)        | resource_id_data_package     |
    +------------------------+--------------------------------+------------------------------+
    | ``memory_domain``      | ``<memory_domain_id>`` (UUID)  | resource_id_memory_domain    |
    +------------------------+--------------------------------+------------------------------+
    | ``memory_item``        | ``<knowledge_item_id>`` (UUID) | resource_id_memory_item      |
    +------------------------+--------------------------------+------------------------------+
    | ``recipe``             | ``<recipe_id>`` (UUID)         | recipes                      |
    +------------------------+--------------------------------+------------------------------+
    | ``marketplace_plugin`` | ``<slug>/<plugin_name>``       | — application-validated only |
    +------------------------+--------------------------------+------------------------------+

    Why ``marketplace_plugin`` (and future/unknown types) stay application-validated:

    ``marketplace_plugin`` ``resource_id`` is a composite ``"<slug>/<plugin_name>"``
    path. The ``marketplace_plugins`` table's PK is ``(marketplace_id, name)``
    where ``marketplace_id`` is the registry UUID, not the slug.  There
    is no single surrogate column to FK against without denormalising
    one of the two tables, so this type remains polymorphic: all five
    per-type columns are NULL for ``marketplace_plugin`` rows.  Any
    resource_type not in the 5 typed set also falls through to the
    "all per-type NULL" branch of the CHECK, allowing future enum members
    to be added without a schema migration.

    Legacy ``resource_id`` column:

    The polymorphic ``resource_id`` column is NOT dropped — existing
    application queries continue to read from it.  For the 5 typed rows
    both ``resource_id`` AND the per-type column carry the same value;
    the per-type column is the FK-enforced source of truth, ``resource_id``
    is the backwards-compatible lookup column.

    DuckDB note: the per-type columns exist on DuckDB too (added in the
    v60 ladder step), but DuckDB's FK and CHECK support is limited — the
    constraints are PG-only.  Application code enforces the invariant on
    both backends.
    """

    __tablename__ = "resource_grants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    group_id: Mapped[str] = mapped_column(String, ForeignKey("user_groups.id"), nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    # Legacy polymorphic column — kept for backwards-compatible app queries.
    # For the 5 typed ResourceTypes the per-type FK column below also carries
    # the value (FK-enforced); for marketplace_plugin this remains the sole
    # source of truth (application-validated, no surrogate FK possible).
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    assigned_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # DuckDB v50+ adds the `requirement` column to flag grants as
    # ``available`` (default) vs ``required`` (must-install for the group).
    # Nullable so legacy rows that predate the column migrate cleanly.
    requirement: Mapped[str | None] = mapped_column(String, nullable=True)

    # --- Per-type FK columns (migration 0013) ---
    # Exactly one of these is non-NULL for each of the 5 typed ResourceTypes;
    # all are NULL for marketplace_plugin rows. A PG CHECK constraint enforces
    # this invariant. ON DELETE CASCADE keeps the grants table clean without
    # requiring periodic orphan-sweep jobs.
    resource_id_table: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("table_registry.id", ondelete="CASCADE"),
        nullable=True,
    )
    resource_id_data_package: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("data_packages.id", ondelete="CASCADE"),
        nullable=True,
    )
    resource_id_memory_domain: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("memory_domains.id", ondelete="CASCADE"),
        nullable=True,
    )
    resource_id_memory_item: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("knowledge_items.id", ondelete="CASCADE"),
        nullable=True,
    )
    resource_id_recipe: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("recipes.id", ondelete="CASCADE"),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "resource_type",
            "resource_id",
            name="uq_resource_grants_group_type_id",
        ),
        # Mirror of migration 0013's polymorphic invariant. The constraint has
        # lived in the DB since that migration; alembic <1.19 never compared
        # check constraints so its absence here went unnoticed — alembic
        # 1.19.0 (2026-08-04) started flagging it as model↔migration drift
        # (issue #1168).
        CheckConstraint(
            """
        (resource_type = 'table'
            AND resource_id_table           IS NOT NULL
            AND resource_id_data_package    IS NULL
            AND resource_id_memory_domain   IS NULL
            AND resource_id_memory_item     IS NULL
            AND resource_id_recipe          IS NULL)
        OR
        (resource_type = 'data_package'
            AND resource_id_table           IS NULL
            AND resource_id_data_package    IS NOT NULL
            AND resource_id_memory_domain   IS NULL
            AND resource_id_memory_item     IS NULL
            AND resource_id_recipe          IS NULL)
        OR
        (resource_type = 'memory_domain'
            AND resource_id_table           IS NULL
            AND resource_id_data_package    IS NULL
            AND resource_id_memory_domain   IS NOT NULL
            AND resource_id_memory_item     IS NULL
            AND resource_id_recipe          IS NULL)
        OR
        (resource_type = 'memory_item'
            AND resource_id_table           IS NULL
            AND resource_id_data_package    IS NULL
            AND resource_id_memory_domain   IS NULL
            AND resource_id_memory_item     IS NOT NULL
            AND resource_id_recipe          IS NULL)
        OR
        (resource_type = 'recipe'
            AND resource_id_table           IS NULL
            AND resource_id_data_package    IS NULL
            AND resource_id_memory_domain   IS NULL
            AND resource_id_memory_item     IS NULL
            AND resource_id_recipe          IS NOT NULL)
        OR
        (resource_type NOT IN ('table', 'data_package', 'memory_domain', 'memory_item', 'recipe')
            AND resource_id_table           IS NULL
            AND resource_id_data_package    IS NULL
            AND resource_id_memory_domain   IS NULL
            AND resource_id_memory_item     IS NULL
            AND resource_id_recipe          IS NULL)
        """,
            name="ck_resource_grants_per_type_fk",
        ),
        Index("ix_resource_grants_group_id", "group_id"),
        Index("ix_resource_grants_resource_type", "resource_type"),
        Index("ix_resource_grants_resource_id", "resource_id"),
    )
