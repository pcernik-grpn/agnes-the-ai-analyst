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

    # Mirrors the DuckDB sibling's ``_ORDER_SQL`` — see its docstring for why
    # ``order`` is mapped through a literal dict and every fragment ends in
    # ``, id ASC``.
    _ORDER_SQL = {
        "oldest": "created_at ASC, id ASC",
        "newest": "created_at DESC, id ASC",
        "name": "LOWER(filename) ASC, id ASC",
        "size": "size_bytes DESC NULLS LAST, id ASC",
    }

    @staticmethod
    def _escape_like(value: str) -> str:
        """Escape LIKE metacharacters so untrusted search text matches
        literally (see ``users_pg.py::get_by_email_prefix`` for the same idiom)."""
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _filter_clause(self, corpus_id: str, q: Optional[str], status: Optional[str]) -> tuple[str, Dict[str, Any]]:
        """Shared WHERE-clause builder for ``list_for_corpus``/``count_for_corpus``.

        Blank (``None``/empty/whitespace-only) ``q``/``status`` means "no
        filter" — never "match nothing".
        """
        where = ["corpus_id = :corpus_id"]
        params: Dict[str, Any] = {"corpus_id": corpus_id}
        q_norm = q.strip() if q else ""
        if q_norm:
            pattern = f"%{self._escape_like(q_norm)}%"
            where.append("(LOWER(filename) LIKE LOWER(:q) ESCAPE '\\' OR LOWER(path) LIKE LOWER(:q) ESCAPE '\\')")
            params["q"] = pattern
        status_norm = status.strip() if status else ""
        if status_norm:
            where.append("processing_status = :status")
            params["status"] = status_norm
        return " AND ".join(where), params

    def list_for_corpus(
        self,
        corpus_id: str,
        *,
        limit: Optional[int] = None,
        offset: int = 0,
        q: Optional[str] = None,
        status: Optional[str] = None,
        order: str = "oldest",
    ) -> List[Dict[str, Any]]:
        """Files for a given corpus, paginated/filtered/ordered.

        Backwards compatible: a bare ``list_for_corpus(corpus_id)`` call
        keeps returning every row ordered by ``created_at`` ascending, as it
        always has — 14 existing callers depend on this.
        """
        where_sql, params = self._filter_clause(corpus_id, q, status)
        order_sql = self._ORDER_SQL.get(order, self._ORDER_SQL["oldest"])
        sql = f"SELECT * FROM corpus_files WHERE {where_sql} ORDER BY {order_sql}"
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = limit
        if offset:
            sql += " OFFSET :offset"
            params["offset"] = offset
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def count_for_corpus(
        self,
        corpus_id: str,
        *,
        q: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        """Row count for ``list_for_corpus`` under the same ``q``/``status``
        filters (and nothing else — no limit/offset applies to a count)."""
        where_sql, params = self._filter_clause(corpus_id, q, status)
        with self._engine.connect() as conn:
            row = (
                conn.execute(sa.text(f"SELECT COUNT(*) AS n FROM corpus_files WHERE {where_sql}"), params)
                .mappings()
                .first()
            )
        return int(row["n"]) if row else 0

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

    def count_by_corpus(self) -> Dict[str, int]:
        """``corpus_id -> file count`` for every corpus that has files, in one
        query. See the DuckDB sibling for why this exists."""
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT corpus_id, COUNT(*) AS n FROM corpus_files GROUP BY corpus_id")).all()
        return {r[0]: int(r[1]) for r in rows}

    def search_across_corpora(self, q: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Mirrors the DuckDB sibling — see its docstring."""
        q_norm = (q or "").strip()
        if not q_norm:
            return []
        pattern = f"%{self._escape_like(q_norm)}%"
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM corpus_files "
                        "WHERE LOWER(filename) LIKE LOWER(:q) ESCAPE '\\' OR LOWER(path) LIKE LOWER(:q) ESCAPE '\\' "
                        "ORDER BY LOWER(filename) ASC, id ASC LIMIT :limit"
                    ),
                    {"q": pattern, "limit": limit},
                )
                .mappings()
                .all()
            )
        return [self._decode_row(dict(r)) for r in rows]

    def filenames_for_ids(self, file_ids: List[str]) -> Dict[str, Optional[str]]:
        """Mirrors the DuckDB sibling — see its docstring for why this
        exists (the whole-corpus-listing cost it replaces)."""
        if not file_ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT id, filename FROM corpus_files WHERE id = ANY(:ids)"),
                {"ids": list(file_ids)},
            ).all()
        return {r[0]: r[1] for r in rows}

    def status_counts_for_corpora(self, corpus_ids: List[str]) -> Dict[str, Dict[str, int]]:
        """``{corpus_id: {processing_status: count}}`` for exactly the given
        corpus ids, in ONE query. Mirrors the DuckDB sibling — see its
        docstring for why this exists (the per-scope N+1 it replaces)."""
        if not corpus_ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT corpus_id, processing_status, COUNT(*) AS n FROM corpus_files "
                    "WHERE corpus_id = ANY(:ids) GROUP BY corpus_id, processing_status"
                ),
                {"ids": list(corpus_ids)},
            ).all()
        out: Dict[str, Dict[str, int]] = {}
        for corpus_id, status, n in rows:
            out.setdefault(corpus_id, {})[status or "pending"] = int(n)
        return out

    def top_folder_status_counts(self, corpus_id: str) -> Dict[str, Dict[str, int]]:
        """Mirrors the DuckDB sibling — see its docstring."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT CASE WHEN path IS NOT NULL AND strpos(path, '/') > 0 "
                    "THEN split_part(path, '/', 1) ELSE '' END AS top_folder, "
                    "processing_status, COUNT(*) AS n FROM corpus_files "
                    "WHERE corpus_id = :id GROUP BY top_folder, processing_status"
                ),
                {"id": corpus_id},
            ).all()
        out: Dict[str, Dict[str, int]] = {}
        for top_folder, status, n in rows:
            out.setdefault(top_folder, {})[status or "pending"] = int(n)
        return out

    def extension_status_counts(self, corpus_ids: List[str]) -> Dict[str, Dict[str, Dict[str, int]]]:
        """Mirrors the DuckDB sibling — see its docstring."""
        if not corpus_ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT extension, processing_status, COUNT(*) AS n, "
                    "  COALESCE(SUM(size_bytes), 0) AS bytes FROM ( "
                    "  SELECT processing_status, size_bytes, "
                    "    CASE WHEN strpos(basename, '.') > 0 "
                    "         THEN lower(reverse(split_part(reverse(basename), '.', 1))) "
                    "         ELSE '' END AS extension "
                    "  FROM ( "
                    "    SELECT processing_status, size_bytes, "
                    "      reverse(split_part(reverse(COALESCE(path, '')), '/', 1)) AS basename "
                    "    FROM corpus_files WHERE corpus_id = ANY(:ids) "
                    "  ) basenames "
                    ") extensions "
                    "GROUP BY extension, processing_status"
                ),
                {"ids": list(corpus_ids)},
            ).all()
        out: Dict[str, Dict[str, Dict[str, int]]] = {}
        for extension, status, n, size_bytes in rows:
            out.setdefault(extension, {})[status or "pending"] = {"count": int(n), "bytes": int(size_bytes or 0)}
        return out

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

    def update_path(self, file_id: str, *, path: Optional[str], filename: str) -> None:
        """Postgres twin of the DuckDB ``update_path`` — see its docstring."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE corpus_files SET filename = :filename, path = :path, "
                    "    updated_at = CURRENT_TIMESTAMP WHERE id = :id"
                ),
                {"filename": filename, "path": path, "id": file_id},
            )
