"""Postgres-backed repository for ``file_corpora`` (v82).

Mirrors ``src/repositories/file_corpora.py`` (the DuckDB impl) on the
``FileCorporaRepository`` public surface. Cross-engine parity is covered
by ``tests/db_pg/test_file_corpora_contract.py``.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class FileCorporaPgRepository:
    """Postgres twin of ``FileCorporaRepository``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        slug: str,
        description: Optional[str],
        created_by: str,
        origin: str = "uploaded",
    ) -> str:
        """Insert a new corpus; returns the generated ``col_*`` id.

        ``origin`` records provenance — ``'uploaded'`` (default) or
        ``'generated'`` (an agent authored it).

        Raises ``IntegrityError`` on slug collision.
        """
        corpus_id = "col_" + secrets.token_hex(8)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO file_corpora "
                    "(id, slug, name, description, created_by, origin) "
                    "VALUES (:id, :slug, :name, :description, :created_by, :origin)"
                ),
                {
                    "id": corpus_id,
                    "slug": slug,
                    "name": name,
                    "description": description,
                    "created_by": created_by,
                    "origin": origin,
                },
            )
        return corpus_id

    def get(self, corpus_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Fetch one corpus by id. Returns ``None`` if not found."""
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(f"SELECT * FROM file_corpora WHERE id = :id{guard}"),
                    {"id": corpus_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def get_by_slug(self, slug: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Fetch one corpus by slug. Returns ``None`` if not found or soft-deleted."""
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(f"SELECT id FROM file_corpora WHERE slug = :slug{guard}"),
                {"slug": slug},
            ).first()
        return self.get(row[0], include_deleted=include_deleted) if row else None

    def list(
        self,
        *,
        search: Optional[str] = None,
        created_by: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List live (non-soft-deleted) corpora, name-ordered.

        ``created_by`` filters to one creator in SQL — see the DuckDB
        sibling for why the agent-scope intersection needs the predicate
        rather than a full read.
        """
        query = "SELECT * FROM file_corpora WHERE deleted_at IS NULL"
        params: Dict[str, Any] = {}
        if search:
            query += " AND name ILIKE :search"
            params["search"] = f"%{search}%"
        if created_by:
            query += " AND created_by = :created_by"
            params["created_by"] = created_by
        query += " ORDER BY name LIMIT :limit"
        params["limit"] = limit
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(query), params).mappings().all()
        return [dict(r) for r in rows]

    def soft_delete(self, corpus_id: str) -> None:
        """Set ``deleted_at`` to now (also bumps ``updated_at``). Idempotent."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE file_corpora SET deleted_at = CURRENT_TIMESTAMP, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = :id"
                ),
                {"id": corpus_id},
            )
