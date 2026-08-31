"""Postgres repository for ``resource_source_tags``.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately no ``src/repositories/resource_source_tags.py`` DuckDB
sibling: the table landed after the DuckDB app-state backend was frozen, is
registered ``PG``-only in :data:`src.repositories._REGISTRY`, and resolving it
on a DuckDB-backed instance raises
:class:`src.repositories.RequiresPostgresBackend` (translated to a typed
``501`` by ``app/main.py``). See ``docs/migrations.md`` -> "Adding a PG-only
feature (post-A3)".

A tag answers "which data source is this skill / agent / knowledge domain
about", which the cross-domain coverage report
(``src/semantic/coverage.py``) needs and no other table records. It is NOT a
``resource_grants`` row — that answers "which group may reach it".
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class ResourceSourceTagsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def create(
        self,
        *,
        resource_type: str,
        resource_id: str,
        source_id: str,
        tagged_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Tag one resource to one source; returns the stored row.

        Raises ``sqlalchemy.exc.IntegrityError`` when the
        ``(resource_type, resource_id, source_id)`` triple already exists —
        the caller turns that into a ``409`` rather than the repository
        swallowing it, because "already tagged" is information the admin
        asked for, not a silent no-op.
        """
        tag_id = f"rst_{uuid4().hex[:16]}"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO resource_source_tags
                      (id, resource_type, resource_id, source_id, tagged_by, tagged_at)
                    VALUES
                      (:id, :resource_type, :resource_id, :source_id, :tagged_by, current_timestamp)
                    """
                ),
                {
                    "id": tag_id,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "source_id": source_id,
                    "tagged_by": tagged_by,
                },
            )
        row = self.get(tag_id)
        assert row is not None  # just inserted in the same engine
        return row

    def get(self, tag_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM resource_source_tags WHERE id = :id"),
                    {"id": tag_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list_for_source(self, source_id: str) -> List[Dict[str, Any]]:
        """Every tag pointing at ``source_id`` — the coverage report's read."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM resource_source_tags WHERE source_id = :source_id "
                        "ORDER BY resource_type, resource_id"
                    ),
                    {"source_id": source_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_for_resource(self, resource_type: str, resource_id: str) -> List[Dict[str, Any]]:
        """Every source one resource is tagged to — a skill may legitimately
        be about two projects, so this is a list, not an optional row."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM resource_source_tags "
                        "WHERE resource_type = :resource_type AND resource_id = :resource_id "
                        "ORDER BY source_id"
                    ),
                    {"resource_type": resource_type, "resource_id": resource_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def delete(self, tag_id: str) -> bool:
        """Remove one tag. ``False`` when nothing matched, so the endpoint can
        answer 404 instead of pretending it deleted something."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM resource_source_tags WHERE id = :id"),
                {"id": tag_id},
            )
        return bool(result.rowcount)
