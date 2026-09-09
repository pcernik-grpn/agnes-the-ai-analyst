"""SQLAlchemy model for ``data_apps`` (v96) — hosted user web apps registry.

Mirrors the DuckDB DDL (``src/db.py``'s ``_DATA_APPS_CREATE_SQL`` / shared by
fresh-install and ``_v95_to_v96``) and the Alembic migration
``migrations/versions/0043_data_apps_v96.py`` column-for-column — with ONE
Postgres-only exception, ``data_identity`` (revision ``0113``): the DuckDB
app-state ladder is frozen (A3), so that column exists on this side alone and
every reader defaults its absence to ``'owner'``
(``src.data_apps.identity.data_identity_of``). Cross-engine behavior parity
(not just schema) is covered by ``tests/db_pg/test_data_apps_contract.py``;
the raw-SQL PG repository lives at ``src/repositories/data_apps_pg.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class DataApp(Base):
    __tablename__ = "data_apps"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, server_default=text("''"), nullable=True)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False)
    repo_mode: Mapped[str] = mapped_column(String, server_default=text("'internal'"), nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    repo_branch: Mapped[str | None] = mapped_column(String, server_default=text("'main'"), nullable=True)
    deployed_sha: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    runtime_tag: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    state: Mapped[str] = mapped_column(String, server_default=text("'created'"), nullable=False)
    state_detail: Mapped[str | None] = mapped_column(Text, server_default=text("''"), nullable=True)
    secrets_enc: Mapped[str | None] = mapped_column(Text, server_default=text("''"), nullable=True)
    env: Mapped[str | None] = mapped_column(Text, server_default=text("'{}'"), nullable=True)
    cpu_limit: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    mem_limit: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    idle_timeout_s: Mapped[int | None] = mapped_column(Integer, server_default=text("1800"), nullable=True)
    sleep_mode: Mapped[str | None] = mapped_column(String, server_default=text("'recreate'"), nullable=True)
    service_token_id: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    # v99: draft model — a draft row points back at its production app via
    # parent_app_id, is flagged is_draft so list(include_drafts=False)
    # excludes it, and records the git branch it was built from.
    parent_app_id: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    is_draft: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"), nullable=True)
    draft_branch: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    # v108: linked (externally-hosted) apps — external_url/source_ref/
    # description_override carry no server default in the migration (mirror that
    # exactly so autogenerate sees no drift); managed is NOT NULL DEFAULT FALSE.
    external_url: Mapped[str | None] = mapped_column(String, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    managed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    description_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL until the app's first (re)deploy / request — no DuckDB default either.
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_deploy_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 0113 (PG-only): 'owner' | 'viewer' — whose grants the app reads data
    # with. NOT NULL with a server default so existing rows read 'owner'.
    data_identity: Mapped[str] = mapped_column(String, server_default=text("'owner'"), nullable=False)
    # Neither the DuckDB DDL nor the alembic migration marks these NOT NULL
    # (both rely on the default) — mirror that exactly so autogenerate
    # doesn't see a constraint the applied migration never creates.
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("now()"), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("now()"), nullable=True)
