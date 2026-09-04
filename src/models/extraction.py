"""SQLAlchemy model for ``extraction_runs`` (PG-only, A3 ratchet — no
DuckDB sibling).

One row per built-in extraction run (2026-08-31 extraction-observability-ui
design §7.1): what phase it is in, how far it got, what it finally reported,
and what it refused to index. Written by the crawl at the checkpoint cadence
it already has (``connectors/sharepoint/crawler.py``) and read by the
SharePoint source card's live crawl cell + run-history drawer.

Two properties the columns exist to preserve:

* **An interrupted run still reports.** ``status='interrupted'`` is its own
  outcome, not a flavour of failure — the run ingested what it ingested, and
  the next run resumes from the persisted cTags.
* **Every number names its freshness.** ``checkpoint_at`` is what the card's
  "as of" caption prints; nothing here is re-timestamped at read time.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"
    __table_args__ = (
        sa.Index("idx_extraction_runs_connection_started", "connection_id", "started_at"),
        sa.Index("idx_extraction_runs_parent", "parent_run_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String, nullable=False)
    #: The ``jobs`` row this run belongs to, when the caller knows it — the
    #: builtin crawl receives the job's payload, not its id, so this is
    #: nullable rather than a foreign key.
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: running | done | interrupted | failed
    status: Mapped[str] = mapped_column(String, server_default="running", nullable=False)
    #: crawl | convert | anonymize | ingest — a label of the LAST OBSERVED
    #: phase at ``checkpoint_at``, never a claim about this instant.
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    files_seen: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    files_done: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    #: False while the delta enumeration can still raise ``files_seen``.
    #: Never turned into a fraction: the crawl enumerates and processes in
    #: lockstep per delta page, so seen and done are equal at every
    #: checkpoint — absolute counters only.
    enumeration_done: Mapped[bool] = mapped_column(Boolean, server_default=sa.text("false"), nullable=False)
    #: The final ``CrawlStats.report()``; ``{}`` while the run is live.
    report: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False)
    #: Live counters at the last checkpoint.
    progress: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False)
    #: LLM token usage when a detector spends any; ``{}`` = none spent,
    #: which is a different claim from "$0.00" (design §7.2).
    usage: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False)
    #: ``{items: [{path, reason, detail}], total: int}`` — capped, so a
    #: truncated list is visibly truncated.
    skips: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- shard-crawl columns (2026-09-03 auto-parallel-crawl design §4.2) ---
    #: The PLANNER run this row was enqueued from — ``NULL`` for a run that
    #: is not a shard (every run before this feature, and the inline path
    #: forever). A shard child never sets its own row's ``shards_total``/
    #: ``shards_done`` — those belong to the PARENT (this column's target).
    parent_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    #: This shard's own delta-unit key (``DriveTarget.state_key`` — a drive
    #: id, or ``"<drive_id>:<item_id>"`` for a folder shard), or ``NULL`` on
    #: a non-shard run. Joins this row to its own
    #: ``sharepoint_connection_state`` row (``kind='crawl:<shard_key>'``).
    shard_key: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The human-facing folder path for this shard — what the fleet view's
    #: shard disclosure renders instead of the opaque key.
    shard_label: Mapped[str | None] = mapped_column(String, nullable=True)
    #: PARENT-only: how many children the plan enqueued. ``NULL`` on a
    #: non-parent run.
    shards_total: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    #: PARENT-only: how many children have finished (success or failure) —
    #: advanced atomically by ``finish_shard`` (Task 4). ``0`` on every row,
    #: including a non-parent one, where it is simply unused.
    shards_done: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
