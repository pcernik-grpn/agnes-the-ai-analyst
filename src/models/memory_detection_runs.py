"""SQLAlchemy model behind ``memory_detection_runs`` — one row per
corporate-memory detection run (issue #1971 Part 3).

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline"):
this table landed after the DuckDB app-state backend was frozen, so there is
no ``src/db.py`` ladder step and no DuckDB repository sibling. See
``docs/migrations.md`` -> "Adding a PG-only feature (post-A3)".

Its own module rather than a row in ``src/models/knowledge.py``, for the
same reason ``semantic_health_mutes.py`` is its own module: the tables in
``knowledge.py`` (``knowledge_items``, ``memory_domains``, …) are pre-A3 and
still have a DuckDB half, and mixing a PG-only table into that file would
make the next author guess which half of it the freeze applies to.

This is the "missing observability" the two corporate-memory extractors
never had: before this, ``app/worker/kinds.py``'s scheduled collector wrapper
discarded ``collect_all()``'s return value entirely, and the session-
transcript detector recorded nothing about its own runs beyond scattered log
lines. Every write here is best-effort — see
``src/memory_detection_logging.py::record_detection_run`` — a failure to log
a run must never fail the detection run itself, and MUST NOT raise on a
DuckDB-backed instance (this table simply isn't there; the helper degrades
to one warning log line).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base

#: The two extractor paths that write into `knowledge_items` — kept as a
#: soft, documented vocabulary rather than a DB-level CHECK constraint: a
#: third source landing later should not require a migration just to be
#: nameable here.
SOURCE_SESSION_TRANSCRIPTS = "session_transcripts"
SOURCE_CLAUDE_LOCAL_MD = "claude_local_md"


class MemoryDetectionRun(Base):
    """One completed (or dry-run) pass of a corporate-memory detector.

    ``sessions_scanned`` is named for the session-transcript path's unit of
    work; the CLAUDE.local.md collector's run records the number of user
    files it scanned in this same column — there is no per-source unit
    flexible enough to need two columns, and the collector's own stats dict
    calls that number ``users_scanned`` for the same reason. See
    ``src/memory_detection_logging.py`` for the exact mapping per source.

    ``policy_fingerprint`` is a sha256 hex digest of the detection policy
    text this run actually used, so an admin can correlate a change in
    behavior with a policy edit without diffing prose by eye. ``NULL`` for a
    source that consults no editable policy (the collector, today — see
    issue #1971 Part 2's decision to leave its structurally different prompt
    on the built-in default).
    """

    __tablename__ = "memory_detection_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sessions_scanned: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    items_proposed: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    items_filtered: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    items_inserted: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    items_routed_side_domain: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    # {} means "no token usage recorded for this run" (source doesn't report
    # it, or the LLM was never called) — a different claim from "$0.00" and
    # the two must stay tellable apart, same convention as `extraction_runs.usage`.
    token_usage = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    policy_fingerprint: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (
        # The hot read is "the last ~20 runs, newest first" — the admin
        # panel's observability list.
        Index("idx_memory_detection_runs_started_at", "started_at"),
    )
