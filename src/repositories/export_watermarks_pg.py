"""Postgres repository for ``export_watermarks`` — the durable cursor a
scheduled push export advances only after a successful delivery (design
2026-09-08, §3.12, Task 11).

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately no ``src/repositories/export_watermarks.py`` DuckDB
sibling: the table landed after the DuckDB app-state backend was frozen, is
registered ``PG``-only in :data:`src.repositories._REGISTRY`, and resolving
it on a DuckDB-backed instance raises
:class:`src.repositories.RequiresPostgresBackend`. In practice that never
happens for the ``conversation-export`` worker job: it resolves
``llm_calls_repo()`` (itself PG-only) FIRST and swallows the resulting
error before ever reaching this repo — see
``app/worker/kinds_conversation_export.py``. Reach this repo only through
``src.repositories.export_watermarks_repo()``, never by instantiating this
class.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class ExportWatermarksPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, name: str) -> tuple[datetime, str] | None:
        """The current ``(watermark, cursor_id)`` for ``name``, or ``None``
        when no row exists yet, or the row's ``watermark`` is still
        ``NULL`` (a sink that has never delivered a batch) — the caller
        treats ``None`` as "export everything since the beginning". Always
        returns BOTH fields together: a caller resuming a keyset walk
        needs the id alongside the timestamp (design 2026-09-08 §3.12,
        push-sink defect fix) — a bare timestamp is not a safe resume
        point on its own.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT watermark, cursor_id FROM export_watermarks WHERE name = :name"), {"name": name}
            ).first()
        if row is None or row[0] is None:
            return None
        return (row[0], row[1])

    def set(self, name: str, watermark: datetime, cursor_id: str) -> dict[str, Any]:
        """Upsert ``name``'s ``(watermark, cursor_id)``.

        The caller (the ``conversation-export`` worker job) only ever calls
        this with a keyset position strictly later than what :meth:`get`
        last returned — after a batch's destination endpoint answered 2xx,
        to the last DELIVERED row's own ``(last_message_at, id)`` — so this
        method trusts the caller rather than re-validating monotonicity
        itself.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO export_watermarks (name, watermark, cursor_id, updated_at)
                    VALUES (:name, :watermark, :cursor_id, current_timestamp)
                    ON CONFLICT (name) DO UPDATE
                      SET watermark = EXCLUDED.watermark,
                          cursor_id = EXCLUDED.cursor_id,
                          updated_at = current_timestamp
                    """
                ),
                {"name": name, "watermark": watermark, "cursor_id": cursor_id},
            )
        return {"name": name, "watermark": watermark, "cursor_id": cursor_id}
