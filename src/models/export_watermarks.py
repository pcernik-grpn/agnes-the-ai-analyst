"""``export_watermarks`` — the durable cursor for a scheduled push export
(design 2026-09-08, §3.12, Task 11).

One row per named sink. The ``conversation-export`` worker job kind
(``app/worker/kinds_conversation_export.py``) reads the row named
``"conversation_export"`` to know which conversations it has already
delivered, and advances it only after the destination endpoint answers
2xx — so a failed delivery is retried, never silently skipped, and a
successful one is never resent from scratch on the next tick.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
This table lands after the DuckDB app-state backend was frozen: no
``src/db.py`` step, no DuckDB repository sibling, registered ``PG``-only in
``src.repositories._REGISTRY``. A DuckDB-backed instance never reaches this
table at all — the worker job resolves ``llm_calls_repo()`` first (itself
PG-only) and swallows the resulting ``RequiresPostgresBackend`` before ever
touching a watermark.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class ExportWatermark(Base):
    """One row per named export sink — how far it has successfully
    delivered. ``watermark`` is ``NULL`` until the sink's first successful
    batch; the worker job treats a missing row/``NULL`` watermark as "export
    everything from the beginning of the transcript"."""

    __tablename__ = "export_watermarks"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
