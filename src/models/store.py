"""SQLAlchemy models for the marketplace + store + flea cluster:
marketplace_registry, marketplace_plugins, store_entities,
user_store_installs, user_plugin_optouts, store_submissions,
store_entity_votes, store_lint_runs, store_lint_findings,
store_lint_dismissals, store_lint_entity_state.

Also includes user_curated_subscriptions (from src/repositories/user_curated_subscriptions.py).

Mirrors src/db.py:379-431, 541-597, 634-666.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class MarketplaceRegistry(Base):
    __tablename__ = "marketplace_registry"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    branch: Mapped[str | None] = mapped_column(String, nullable=True)
    token_env: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    registered_by: Mapped[str | None] = mapped_column(String, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_commit_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    curator_name: Mapped[str | None] = mapped_column(String, nullable=True)
    curator_email: Mapped[str | None] = mapped_column(String, nullable=True)
    # System-seeded built-in marketplace (bundled in the wheel). The nightly
    # git-sync path skips is_builtin=TRUE rows (nothing to fetch).
    is_builtin: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    # v87: pin to a fixed tag name or full 40-char commit SHA (issue #781).
    # Mutually exclusive with `branch`, enforced at the admin API layer.
    ref: Mapped[str | None] = mapped_column(String, nullable=True)


class MarketplacePlugin(Base):
    __tablename__ = "marketplace_plugins"

    marketplace_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    author_name: Mapped[str | None] = mapped_column(String, nullable=True)
    homepage: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    source_type: Mapped[str | None] = mapped_column(String, nullable=True)
    source_spec: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    cover_photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    video_url: Mapped[str | None] = mapped_column(String, nullable=True)
    doc_links: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # DEAD SINCE 0098, and still here on purpose. It said "every account gets
    # this plugin, automatically" — one cell of a WHO x CHOICE grid wearing a
    # name that described neither axis, and a second implementation of an idea
    # `resource_grants` already had. It is now an ordinary grant
    # (scope='everyone', requirement='required') and nothing reads this column.
    #
    # Dropping it is the CONTRACT half of an expand/contract pair and ships in
    # a later release: an old `api` replica still selecting it during a rolling
    # recreate would get `UndefinedColumn`, and this repo cannot stage a drop
    # within one release (see 0098's MID-FLIGHT note). Delete this column, this
    # comment, and the reconciler's clear-the-flag step together.
    is_system: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    # Admin per-plugin disable for built-in plugins — instance-wide, distinct
    # from per-user opt-outs. Disabled plugins are filtered from the served feed.
    admin_disabled: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)

    __table_args__ = (PrimaryKeyConstraint("marketplace_id", "name"),)


class StoreEntity(Base):
    __tablename__ = "store_entities"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False)
    owner_username: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[str] = mapped_column(String, nullable=False)
    photo_path: Mapped[str | None] = mapped_column(String, nullable=True)
    video_url: Mapped[str | None] = mapped_column(String, nullable=True)
    doc_paths: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    install_count: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    visibility_status: Mapped[str] = mapped_column(String, server_default=text("'pending'"), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_by: Mapped[str | None] = mapped_column(String, nullable=True)
    version_no: Mapped[int] = mapped_column(Integer, server_default=text("1"), nullable=False)
    version_history: Mapped[list | None] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    # v50+ flea-market polish — separate human-display title/tagline from the
    # internal entity ``name`` (which is the npm-style slug). ``synthetic_name``
    # is the LLM-generated fallback when the publisher didn't pick a title.
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    tagline: Mapped[str | None] = mapped_column(String, nullable=True)
    synthetic_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # v104 trust line — who stands behind the item (publisher_kind) and the
    # org's advisory review of a user-published item (verification_*). Mirrors
    # migration 0051_store_publisher_v104 + DuckDB _v109_to_v110.
    publisher_kind: Mapped[str] = mapped_column(String, server_default=text("'user'"), nullable=False)
    verification_state: Mapped[str] = mapped_column(String, server_default=text("'none'"), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String, nullable=True)
    verification_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_store_entities_owner_name"),
        CheckConstraint(
            "type IN ('skill','agent','plugin')",
            name="ck_store_entities_type",
        ),
        CheckConstraint(
            "visibility_status IN ('pending','approved','hidden','archived')",
            name="ck_store_entities_visibility",
        ),
        CheckConstraint(
            "publisher_kind IN ('organization', 'user')",
            name="ck_store_entities_publisher_kind",
        ),
        CheckConstraint(
            "verification_state IN ('none', 'requested', 'verified', 'changes_requested')",
            name="ck_store_entities_verification_state",
        ),
        Index("idx_store_entities_publisher_kind", "publisher_kind"),
    )


class UserStoreInstall(Base):
    __tablename__ = "user_store_installs"

    user_id: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (PrimaryKeyConstraint("user_id", "entity_id"),)


class UserPluginOptout(Base):
    __tablename__ = "user_plugin_optouts"

    user_id: Mapped[str] = mapped_column(String, nullable=False)
    marketplace_id: Mapped[str] = mapped_column(String, nullable=False)
    plugin_name: Mapped[str] = mapped_column(String, nullable=False)
    opted_out_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (PrimaryKeyConstraint("user_id", "marketplace_id", "plugin_name"),)


class StoreSubmission(Base):
    __tablename__ = "store_submissions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    submitter_id: Mapped[str] = mapped_column(String, nullable=False)
    submitter_email: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    inline_checks: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    llm_findings: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reviewed_by_model: Mapped[str | None] = mapped_column(String, nullable=True)
    override_by: Mapped[str | None] = mapped_column(String, nullable=True)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bundle_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    bundle_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        Index("idx_store_submissions_status", "status"),
        Index("idx_store_submissions_entity", "entity_id"),
    )


# Note: ``UserCuratedSubscription`` is intentionally NOT a separate model.
# The "curated subscriptions" Python repository (``user_curated_subscriptions.py``)
# uses the existing ``user_plugin_optouts`` table — pre-v28 a row meant
# opt-OUT; v28 inverts the semantic (row presence = subscribed). See the
# docstring in src/repositories/user_curated_subscriptions.py for context.


class UserStackSubscription(Base):
    __tablename__ = "user_stack_subscriptions"

    user_id: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    subscribed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "resource_type", "resource_id"),
        Index("idx_user_stack_subscriptions_user", "user_id"),
    )


class StoreEntityVote(Base):
    """Per-user thumbs up/down rating on a store / marketplace entity (#398).

    Mirrors ``KnowledgeVote``: one row per (entity, user). The repo upserts on
    the PK so a re-vote flips ``vote``; a clear deletes the row. Mirrors DuckDB
    ``src/db.py`` ``store_entity_votes`` (v76).
    """

    __tablename__ = "store_entity_votes"

    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    vote: Mapped[int | None] = mapped_column(Integer, nullable=True)
    voted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (PrimaryKeyConstraint("entity_id", "user_id"),)


class StoreLintRun(Base):
    """v89 skill lint run — one row per lint pass (scheduler | admin | publish
    trigger). Mirrors DuckDB ``src/db.py`` ``store_lint_runs``."""

    __tablename__ = "store_lint_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    trigger: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entities_linted: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    entities_skipped: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    findings_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)


class StoreLintFinding(Base):
    """v89 skill lint finding — the *current* generation of findings for a
    store entity (replace-on-relint; no history kept here). Mirrors DuckDB
    ``src/db.py`` ``store_lint_findings``."""

    __tablename__ = "store_lint_findings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    rule_id: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(String, nullable=False)
    evidence: Mapped[str | None] = mapped_column(String, server_default=text("'{}'"), nullable=True)
    doc_url: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("idx_store_lint_findings_entity", "entity_id"),)


class StoreLintDismissal(Base):
    """v89 per-(entity, rule) admin dismissal, keyed to the ``content_hash``
    it was dismissed against — a content change auto-resets it. Mirrors
    DuckDB ``src/db.py`` ``store_lint_dismissals``."""

    __tablename__ = "store_lint_dismissals"

    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    rule_id: Mapped[str] = mapped_column(String, nullable=False)
    dismissed_by: Mapped[str] = mapped_column(String, nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (PrimaryKeyConstraint("entity_id", "rule_id"),)


class StoreLintEntityState(Base):
    """v89 last-lint marker per entity, written even when the lint produced
    zero findings — the unchanged-content skip reads the hash from here, so
    clean skills skip too. Mirrors DuckDB ``src/db.py``
    ``store_lint_entity_state``."""

    __tablename__ = "store_lint_entity_state"

    entity_id: Mapped[str] = mapped_column(String, primary_key=True)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    linted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
