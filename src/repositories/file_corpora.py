"""DuckDB-backed repository for ``file_corpora`` (v82).

A file corpus is a self-service Collection container — an admin or user
creates one, uploads files into it, and the ingestion pipeline turns those
files into ``corpus_files`` rows (and eventually ``corpus_chunks``).

Template: src/repositories/data_packages.py.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, List, Optional

import duckdb


class FileCorporaRepository:
    """DuckDB twin for the ``file_corpora`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    _COLS = [
        "id",
        "slug",
        "name",
        "description",
        "created_by",
        "origin",
        "created_at",
        "updated_at",
        "deleted_at",
    ]
    _SELECT = ", ".join(_COLS)

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

        ``origin`` records provenance — ``'uploaded'`` (a user brought the
        file in, the default) or ``'generated'`` (an agent authored it).

        Raises ``duckdb.ConstraintException`` if ``slug`` collides.
        """
        corpus_id = "col_" + secrets.token_hex(8)
        self.conn.execute(
            "INSERT INTO file_corpora (id, slug, name, description, created_by, origin) VALUES (?, ?, ?, ?, ?, ?)",
            [corpus_id, slug, name, description, created_by, origin],
        )
        return corpus_id

    def get(self, corpus_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Fetch one corpus by id. Returns ``None`` if not found.

        Soft-deleted rows are hidden by default; pass
        ``include_deleted=True`` for the restore path.
        """
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM file_corpora WHERE id = ?{guard}",
            [corpus_id],
        ).fetchone()
        if not row:
            return None
        return dict(zip(self._COLS, row))

    def get_by_slug(self, slug: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Fetch one corpus by slug. Returns ``None`` if not found or soft-deleted."""
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        row = self.conn.execute(
            f"SELECT id FROM file_corpora WHERE slug = ?{guard}",
            [slug],
        ).fetchone()
        return self.get(row[0], include_deleted=include_deleted) if row else None

    def list(
        self,
        *,
        search: Optional[str] = None,
        created_by: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List live (non-soft-deleted) corpora, name-ordered.

        ``created_by`` filters to one creator in SQL. The agent-scope
        intersection needs exactly that set and runs once per brokered
        request; without the predicate it read the whole table under a
        100k cap and filtered in Python, on the authorization path.
        """
        query = f"SELECT {self._SELECT} FROM file_corpora WHERE deleted_at IS NULL"
        params: List[Any] = []
        if search:
            query += " AND name ILIKE ?"
            params.append(f"%{search}%")
        if created_by:
            query += " AND created_by = ?"
            params.append(created_by)
        query += " ORDER BY name LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def list_all(self) -> List[Dict[str, Any]]:
        """Every live corpus, name-ordered — no cap.

        The unbounded twin of :meth:`list`, whose ``limit`` defaults to 200.
        Callers that need the whole set rather than a page — an authorization
        input, a full listing, an id→name map — must use this: a silent
        truncation inside an access decision fails *closed*, which surfaces as
        "the grant is broken" rather than "the list was cut off".
        """
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM file_corpora WHERE deleted_at IS NULL ORDER BY name"
        ).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def soft_delete(self, corpus_id: str) -> None:
        """Set ``deleted_at`` to now (also bumps ``updated_at``). Idempotent."""
        self.conn.execute(
            "UPDATE file_corpora SET deleted_at = current_timestamp, updated_at = current_timestamp WHERE id = ?",
            [corpus_id],
        )
