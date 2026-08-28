"""Postgres-backed repository for ``corpus_files`` (v82).

Mirrors ``src/repositories/corpus_files.py`` (the DuckDB impl) on the
``CorpusFilesRepository`` public surface. Cross-engine parity is covered
by ``tests/db_pg/test_corpus_files_contract.py``.

Tracks the processing lifecycle: pending → processing → indexed | needs_review | rejected.

Implementation notes vs DuckDB:
- ``processing_detail`` is stored as VARCHAR text on both sides (not JSONB)
  so that the DuckDB↔PG behaviour is symmetric: writes go through
  ``json.dumps``, reads come back as text and are decoded to dict by
  ``_decode_row`` on both sides. This avoids the need for JSONB casts
  and keeps the parity contract simple.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class CorpusFilesPgRepository:
    """Postgres twin of ``CorpusFilesRepository``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _decode_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Decode ``processing_detail`` from JSON text to dict (or keep None).

        PG stores the column as VARCHAR (not JSONB), so psycopg returns
        a plain str — we json.loads it here just like the DuckDB side.
        """
        v = row_dict.get("processing_detail")
        if v is None or v == "":
            row_dict["processing_detail"] = None
        elif isinstance(v, str):
            try:
                row_dict["processing_detail"] = json.loads(v)
            except (ValueError, TypeError):
                row_dict["processing_detail"] = None
        # If psycopg already deserialised it (e.g. a future JSONB migration),
        # leave it as-is.
        return row_dict

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(
        self,
        *,
        corpus_id: str,
        filename: str,
        sha256: str,
        file_type: Optional[str],
        size_bytes: Optional[int],
        storage_path: Optional[str],
        parent_file_id: Optional[str] = None,
        path: Optional[str] = None,
    ) -> str:
        """Insert a new file row with default status 'pending'.

        ``parent_file_id`` links a bundle-extracted child to its archive row.
        ``path`` is an optional caller-supplied logical identity used for
        upsert-on-upload (see ``get_by_path``); NULL keeps plain-insert
        behavior. Returns the generated ``cf_*`` id.
        """
        file_id = "cf_" + secrets.token_hex(8)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO corpus_files "
                    "(id, corpus_id, filename, sha256, file_type, size_bytes, storage_path, parent_file_id, path) "
                    "VALUES (:id, :corpus_id, :filename, :sha256, "
                    "        :file_type, :size_bytes, :storage_path, :parent_file_id, :path)"
                ),
                {
                    "id": file_id,
                    "corpus_id": corpus_id,
                    "filename": filename,
                    "sha256": sha256,
                    "file_type": file_type,
                    "size_bytes": size_bytes,
                    "storage_path": storage_path,
                    "parent_file_id": parent_file_id,
                    "path": path,
                },
            )
        return file_id

    def get(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one file row by id. Returns ``None`` if not found."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM corpus_files WHERE id = :id"),
                    {"id": file_id},
                )
                .mappings()
                .first()
            )
        return self._decode_row(dict(row)) if row else None

    def get_by_path(self, corpus_id: str, path: str) -> Optional[Dict[str, Any]]:
        """Fetch one file row by its ``(corpus_id, path)`` logical identity.

        Used for upsert-on-upload. Returns ``None`` when no row carries that
        path. ``path=None`` never matches (plain-insert files stay distinct).
        """
        if path is None:
            return None
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM corpus_files "
                        "WHERE corpus_id = :corpus_id AND path = :path "
                        "ORDER BY created_at LIMIT 1"
                    ),
                    {"corpus_id": corpus_id, "path": path},
                )
                .mappings()
                .first()
            )
        return self._decode_row(dict(row)) if row else None

    def list_for_corpus(self, corpus_id: str) -> List[Dict[str, Any]]:
        """All files for a given corpus, ordered by created_at."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM corpus_files WHERE corpus_id = :corpus_id ORDER BY created_at"),
                    {"corpus_id": corpus_id},
                )
                .mappings()
                .all()
            )
        return [self._decode_row(dict(r)) for r in rows]

    def count_by_storage_path(self, corpus_id: str, storage_path: str) -> int:
        """How many rows in this corpus reference ``storage_path``.

        Content-addressed blobs are shared (not refcounted): callers use this
        before unlinking a blob so they never wipe one another row still
        points at. ``None``/empty path counts as 0.
        """
        if not storage_path:
            return 0
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT COUNT(*) AS n FROM corpus_files WHERE corpus_id = :corpus_id AND storage_path = :sp"
                    ),
                    {"corpus_id": corpus_id, "sp": storage_path},
                )
                .mappings()
                .first()
            )
        return int(row["n"]) if row else 0

    def list_children(self, parent_file_id: str) -> List[Dict[str, Any]]:
        """All child rows extracted from the given archive file, by created_at."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM corpus_files WHERE parent_file_id = :pid ORDER BY created_at"),
                    {"pid": parent_file_id},
                )
                .mappings()
                .all()
            )
        return [self._decode_row(dict(r)) for r in rows]

    def set_status(
        self,
        file_id: str,
        *,
        status: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update processing_status (and optionally processing_detail).

        ``detail`` is serialised to JSON text before writing — matches
        the DuckDB side's VARCHAR storage.
        """
        detail_json = json.dumps(detail) if detail is not None else None
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE corpus_files "
                    "SET processing_status = :status, "
                    "    processing_detail = :detail, "
                    "    updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = :id"
                ),
                {"status": status, "detail": detail_json, "id": file_id},
            )

    def move_to_corpus(self, file_id: str, target_corpus_id: str) -> bool:
        """Reparent a file into another corpus (the Library's drag-and-drop).

        Returns False if the file doesn't exist. ``path`` is cleared — see the
        DuckDB twin for why.
        """
        row = self.get(file_id)
        if row is None:
            return False
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE corpus_files "
                    "SET corpus_id = :cid, path = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = :id"
                ),
                {"cid": target_corpus_id, "id": file_id},
            )
        return True

    def delete(self, file_id: str) -> None:
        """Hard-delete a file row (individual files are not soft-deleted)."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM corpus_files WHERE id = :id"),
                {"id": file_id},
            )

    def update_in_place(
        self,
        file_id: str,
        *,
        filename: str,
        sha256: str,
        file_type: Optional[str],
        size_bytes: Optional[int],
        storage_path: Optional[str],
        path: Optional[str],
    ) -> None:
        """Postgres twin of the DuckDB ``update_in_place`` — see its
        docstring (fact-graph-over-Collections §6 prerequisite)."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE corpus_files "
                    "SET filename = :filename, sha256 = :sha256, file_type = :file_type, "
                    "    size_bytes = :size_bytes, storage_path = :storage_path, path = :path, "
                    "    updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = :id"
                ),
                {
                    "filename": filename,
                    "sha256": sha256,
                    "file_type": file_type,
                    "size_bytes": size_bytes,
                    "storage_path": storage_path,
                    "path": path,
                    "id": file_id,
                },
            )
