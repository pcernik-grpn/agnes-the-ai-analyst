"""SQLAlchemy model behind ``extraction_conditions`` — fleet-level provider
refusals the built-in facts-extraction stage cannot retry its way past
(TCRD-296 synthesis F.25, gaps #25/#48).

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline"):
this table landed after the DuckDB app-state backend was frozen, so there is
no ``src/db.py`` ladder step and no DuckDB repository sibling. See
``docs/migrations.md`` -> "Adding a PG-only feature (post-A3)".

A row here answers "is the provider currently refusing every call for a
reason no amount of retrying fixes" — a workspace/account usage-limit
exhaustion, a Vertex region×model quota bucket with zero allocation, or
billing disabled. Deliberately NOT keyed on ``connection_id``: the
underlying refusal is account/workspace/region-scoped, never tied to one
SharePoint connection, so a per-connection table would need N identical rows
for N connections sharing one provider account and N independent "did this
clear yet" checks. See ``connectors/sharepoint/facts_extraction.py``'s
provider-limit classification section for the write side and
``crawler.py``'s ``_enqueue_streamed_facts_pass`` for the read side that
suppresses further enqueues while a condition is active.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class ExtractionCondition(Base):
    """One active (or formerly active) provider-refusal condition.

    ``model``/``region`` default to ``""`` rather than ``NULL`` (a workspace-
    level refusal like ``workspace_limit`` is not scoped to either) so
    equality comparisons in the repository's find/record path never have to
    special-case ``NULL``. ``cleared_at`` NULL means still active; set once a
    pass for this ``provider`` completes without hitting a condition again
    (:func:`connectors.sharepoint.facts_extraction.clear_provider_limit_conditions`).
    """

    __tablename__ = "extraction_conditions"

    id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    # The only kind this table carries today — kept as a column (not
    # hardcoded in every query) so a future distinct fleet-level condition
    # can share the table without a schema change.
    kind: Mapped[str] = mapped_column(sa.String, nullable=False, server_default="provider_limit")
    # One of PROVIDER_LIMIT_REASONS ("workspace_limit"/"quota_exceeded"/
    # "billing_disabled") — see facts_extraction.py's classification.
    reason: Mapped[str] = mapped_column(sa.String, nullable=False)
    provider: Mapped[str] = mapped_column(sa.String, nullable=False)
    model: Mapped[str] = mapped_column(sa.String, nullable=False, server_default="")
    region: Mapped[str] = mapped_column(sa.String, nullable=False, server_default="")
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retry_after_s: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
    )
    last_seen: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
    )
    cleared_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The hot read is "every active condition" (the fleet banner, and
        # the streamed-enqueue suppression check on every crawl page).
        sa.Index("idx_extraction_conditions_active", "provider", "cleared_at"),
    )
