"""DuckDB-backed repository for ``corpus_files`` (v82).

One row per uploaded file associated with a ``file_corpora`` corpus.
Tracks the processing lifecycle: pending → processing → indexed | needs_review | rejected.
``processing_detail`` is a JSON dict stored as VARCHAR text.

Template: src/repositories/data_packages.py.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

import duckdb


class CorpusFilesRepository:
    """DuckDB twin for the ``corpus_files`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    _COLS = [
        "id",
        "corpus_id",
        "filename",
        "sha256",
        "file_type",
        "size_bytes",
        "storage_path",
        "parent_file_id",
        "path",
        "processing_status",
        "processing_detail",
        "created_at",
        "updated_at",
    ]
    _SELECT = ", ".join(_COLS)

    @staticmethod
    def _decode_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Decode ``processing_detail`` from JSON text to dict (or keep None)."""
        v = row_dict.get("processing_detail")
        if v is None or v == "":
            row_dict["processing_detail"] = None
        elif isinstance(v, str):
            try:
                row_dict["processing_detail"] = json.loads(v)
            except (ValueError, TypeError):
                row_dict["processing_detail"] = None
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
        self.conn.execute(
            "INSERT INTO corpus_files "
            "(id, corpus_id, filename, sha256, file_type, size_bytes, storage_path, parent_file_id, path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [file_id, corpus_id, filename, sha256, file_type, size_bytes, storage_path, parent_file_id, path],
        )
        return file_id

    def get(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one file row by id. Returns ``None`` if not found."""
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM corpus_files WHERE id = ?",
            [file_id],
        ).fetchone()
        if not row:
            return None
        return self._decode_row(dict(zip(self._COLS, row)))

    def get_by_path(self, corpus_id: str, path: str) -> Optional[Dict[str, Any]]:
        """Fetch one file row by its ``(corpus_id, path)`` logical identity.

        Used for upsert-on-upload. Returns ``None`` when no row carries that
        path. ``path=None`` never matches (plain-insert files stay distinct).
        """
        if path is None:
            return None
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM corpus_files WHERE corpus_id = ? AND path = ? ORDER BY created_at LIMIT 1",
            [corpus_id, path],
        ).fetchone()
        if not row:
            return None
        return self._decode_row(dict(zip(self._COLS, row)))

    def list_for_corpus(self, corpus_id: str) -> List[Dict[str, Any]]:
        """All files for a given corpus, ordered by created_at."""
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM corpus_files WHERE corpus_id = ? ORDER BY created_at",
            [corpus_id],
        ).fetchall()
        return [self._decode_row(dict(zip(self._COLS, r))) for r in rows]

    def count_by_storage_path(self, corpus_id: str, storage_path: str) -> int:
        """How many rows in this corpus reference ``storage_path``.

        Content-addressed blobs are shared (not refcounted): callers use this
        before unlinking a blob so they never wipe one another row still
        points at. ``None``/empty path counts as 0.
        """
        if not storage_path:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) FROM corpus_files WHERE corpus_id = ? AND storage_path = ?",
            [corpus_id, storage_path],
        ).fetchone()
        return int(row[0]) if row else 0

    def list_children(self, parent_file_id: str) -> List[Dict[str, Any]]:
        """All child rows extracted from the given archive file, by created_at."""
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM corpus_files WHERE parent_file_id = ? ORDER BY created_at",
            [parent_file_id],
        ).fetchall()
        return [self._decode_row(dict(zip(self._COLS, r))) for r in rows]

    def set_status(
        self,
        file_id: str,
        *,
        status: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update processing_status (and optionally processing_detail).

        ``detail`` is serialised to JSON text before writing.
        """
        detail_json = json.dumps(detail) if detail is not None else None
        self.conn.execute(
            "UPDATE corpus_files "
            "SET processing_status = ?, processing_detail = ?, "
            "    updated_at = current_timestamp "
            "WHERE id = ?",
            [status, detail_json, file_id],
        )

    def move_to_corpus(self, file_id: str, target_corpus_id: str) -> bool:
        """Reparent a file into another corpus (the Library's drag-and-drop).

        Returns False if the file doesn't exist. ``path`` is cleared: it is
        unique per ``(corpus_id, path)`` and describes a location inside the
        OLD corpus, so carrying it over could collide with an existing file in
        the target and would misdescribe the file either way.
        """
        row = self.get(file_id)
        if row is None:
            return False
        self.conn.execute(
            "UPDATE corpus_files SET corpus_id = ?, path = NULL, updated_at = current_timestamp WHERE id = ?",
            [target_corpus_id, file_id],
        )
        return True

    def delete(self, file_id: str) -> None:
        """Hard-delete a file row (individual files are not soft-deleted)."""
        self.conn.execute("DELETE FROM corpus_files WHERE id = ?", [file_id])

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
        """Upsert-in-place: refresh identity/content fields on an EXISTING
        row, preserving its id (fact-graph-over-Collections §6 prerequisite —
        see ``app/api/collections.py::_upsert_corpus_file``).

        Used when an upload matches an existing row via ``source_stable_id``
        or ``path``: a rename/move refreshes ``filename``/``path``/
        ``storage_path`` without disturbing ``processing_status``; the caller
        resets that separately (via ``set_status``) only when content
        actually changed, so an unchanged-content match can skip
        re-chunking entirely.
        """
        self.conn.execute(
            "UPDATE corpus_files "
            "SET filename = ?, sha256 = ?, file_type = ?, size_bytes = ?, "
            "    storage_path = ?, path = ?, updated_at = current_timestamp "
            "WHERE id = ?",
            [filename, sha256, file_type, size_bytes, storage_path, path, file_id],
        )
