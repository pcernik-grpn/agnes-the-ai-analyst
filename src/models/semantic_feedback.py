"""SQLAlchemy model behind the semantic-layer feedback channel.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline"):
this table landed after the DuckDB app-state backend was frozen, so there is
no ``src/db.py`` ladder step and no DuckDB repository sibling. See
``docs/migrations.md`` -> "Adding a PG-only feature (post-A3)".

Its own module rather than a row in ``src/models/semantic.py``: the tables
there (``semantic_models``, ``semantic_sources``) are pre-A3 and still have a
DuckDB half, and mixing a PG-only table into that file would make the next
author guess which half of it the freeze applies to.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base

#: The report's lifecycle vocabulary. ``acknowledged`` is the "seen, not
#: fixed yet" middle state — the queue can filter on it, and it exists so an
#: admin who triages before fixing does not have to choose between lying
#: ("resolved") and losing the triage (leaving it "open"). Only ``resolve()``
#: writes today; nothing silently invents a status outside this set.
FEEDBACK_STATUSES: tuple[str, ...] = ("open", "acknowledged", "resolved")


class SemanticFeedback(Base):
    """ "This answer looked wrong" — one report, from a person or an agent.

    The channel the semantic layer otherwise lacks: coverage says what is
    undocumented and health says what is broken, but neither can see the case
    where everything looks fine and the ANSWER was still wrong — an
    unsupported number, a metric that means something else, a concept nobody
    ever defined. Only whoever read the answer knows that, so anyone signed in
    may file one (``POST /api/semantic-feedback``) and an admin works the
    queue.

    ``model_content_hash`` pins WHICH version of the semantic model produced
    the answer: a report filed against a document that has since been rewritten
    is a different fact from one filed against the current text, and without the
    hash an admin cannot tell those apart.
    """

    __tablename__ = "semantic_feedback"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # The question as asked, in the reporter's own words — never normalized:
    # the phrasing is the evidence for what the semantic layer failed to say.
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # The SQL that produced the suspect answer, when there was any. Text, not
    # a parsed structure: this is a report, not something Agnes will execute.
    sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    metric_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model_content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'open'"))
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The queue's hot read is "what is still open", on every page view of
        # the admin Feedback tab.
        Index("idx_semantic_feedback_status", "status"),
    )
