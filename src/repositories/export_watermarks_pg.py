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

    def get(self, name: str) -> datetime | None:
        """The current watermark for ``name``, or ``None`` when no row
        exists yet (a sink that has never delivered a batch) — the caller
        treats ``None`` as "export everything since the beginning"."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT watermark FROM export_watermarks WHERE name = :name"), {"name": name}
            ).first()
        return row[0] if row is not None else None

    def advance(self, name: str, watermark: datetime) -> dict[str, Any]:
        """Upsert ``name``'s watermark to ``watermark``.

        The caller (the ``conversation-export`` worker job) only ever calls
        this with a value strictly later than what :meth:`get` last
        returned — after a batch's destination endpoint answered 2xx, to
        the latest ``conversation_end`` in that batch — so this method
        trusts the caller rather than re-validating monotonicity itself.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO export_watermarks (name, watermark, updated_at)
                    VALUES (:name, :watermark, current_timestamp)
                    ON CONFLICT (name) DO UPDATE
                      SET watermark = EXCLUDED.watermark,
                          updated_at = current_timestamp
                    """
                ),
                {"name": name, "watermark": watermark},
            )
        return {"name": name, "watermark": watermark}
