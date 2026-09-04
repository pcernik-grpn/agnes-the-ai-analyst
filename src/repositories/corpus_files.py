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

    # ``order`` is mapped through this literal dict — never interpolated as a
    # raw caller string — so an unrecognised value simply falls back to
    # "oldest" instead of raising or reaching SQL as text. Every fragment ends
    # in ``, id ASC``: files uploaded in one batch share a ``created_at``, and
    # without that tie-break a page 2 lookup can repeat or skip rows.
    _ORDER_SQL = {
        "oldest": "created_at ASC, id ASC",
        "newest": "created_at DESC, id ASC",
        "name": "LOWER(filename) ASC, id ASC",
        "size": "size_bytes DESC NULLS LAST, id ASC",
    }

    @staticmethod
    def _escape_like(value: str) -> str:
        """Escape LIKE metacharacters so untrusted search text matches
        literally (see ``users.py::get_by_email_prefix`` for the same idiom)."""
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _filter_clause(self, corpus_id: str, q: Optional[str], status: Optional[str]) -> tuple[str, List[Any]]:
        """Shared WHERE-clause builder for ``list_for_corpus``/``count_for_corpus``.

        Blank (``None``/empty/whitespace-only) ``q``/``status`` means "no
        filter" — never "match nothing".
        """
        where = ["corpus_id = ?"]
        params: List[Any] = [corpus_id]
        q_norm = q.strip() if q else ""
        if q_norm:
            pattern = f"%{self._escape_like(q_norm)}%"
            where.append("(LOWER(filename) LIKE LOWER(?) ESCAPE '\\' OR LOWER(path) LIKE LOWER(?) ESCAPE '\\')")
            params.extend([pattern, pattern])
        status_norm = status.strip() if status else ""
        if status_norm:
            where.append("processing_status = ?")
            params.append(status_norm)
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
        sql = f"SELECT {self._SELECT} FROM corpus_files WHERE {where_sql} ORDER BY {order_sql}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(offset)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._decode_row(dict(zip(self._COLS, r))) for r in rows]

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
        row = self.conn.execute(f"SELECT COUNT(*) FROM corpus_files WHERE {where_sql}", params).fetchone()
        return int(row[0]) if row else 0

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

    def count_by_corpus(self) -> Dict[str, int]:
        """``corpus_id -> file count`` for every corpus that has files, in ONE
        query.

        For listings that need only the number: the admin /access projection
        shows a file count per collection, and doing that with
        ``list_for_corpus`` per collection made the page's query count grow
        with the number of collections — which on an instance where every chat
        file-drop is its own one-file collection is the common case, not the
        pathological one. A corpus with no files is simply absent (the caller
        renders 0), so nothing here has to know which corpora exist.
        """
        rows = self.conn.execute("SELECT corpus_id, COUNT(*) FROM corpus_files GROUP BY corpus_id").fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def search_across_corpora(self, q: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Files across EVERY corpus whose filename or path matches ``q``.

        The bounded, on-demand counterpart to the admin ``/access`` overview
        projection (``app.resource_types._corpus_file_blocks``), which caps
        how many files of one collection it lists — on an instance with
        hundreds of thousands of files, listing them all made that payload
        tens of megabytes. This is what the per-file grant picker calls
        instead: a query, not a preloaded scan.

        A blank/whitespace-only ``q`` matches nothing (never "everything") —
        same convention as ``list_for_corpus``'s ``q`` filter, just without a
        ``corpus_id`` to scope it.
        """
        q_norm = (q or "").strip()
        if not q_norm:
            return []
        pattern = f"%{self._escape_like(q_norm)}%"
        rows = self.conn.execute(
            f"SELECT {self._SELECT} FROM corpus_files "
            "WHERE LOWER(filename) LIKE LOWER(?) ESCAPE '\\' OR LOWER(path) LIKE LOWER(?) ESCAPE '\\' "
            "ORDER BY LOWER(filename) ASC, id ASC LIMIT ?",
            [pattern, pattern, limit],
        ).fetchall()
        return [self._decode_row(dict(zip(self._COLS, r))) for r in rows]

    def status_counts_for_corpora(self, corpus_ids: List[str]) -> Dict[str, Dict[str, int]]:
        """``{corpus_id: {processing_status: count}}`` for exactly the given
        corpus ids, in ONE query.

        The batched sibling of ``count_by_corpus`` (same rationale, scoped
        rather than global, and broken down by status): a caller that used to
        call ``list_for_corpus(scope_id)`` once per scope to bucket files by
        status had its query count grow with the number of scopes — up to
        ~180 on a real SharePoint connection — instead of staying flat
        (`app.web.router._sharepoint_pipeline_cell`). A corpus id with no
        files, or not in ``corpus_ids`` at all, is simply absent.
        """
        if not corpus_ids:
            return {}
        placeholders = ", ".join("?" for _ in corpus_ids)
        rows = self.conn.execute(
            f"SELECT corpus_id, processing_status, COUNT(*) FROM corpus_files "
            f"WHERE corpus_id IN ({placeholders}) GROUP BY corpus_id, processing_status",
            list(corpus_ids),
        ).fetchall()
        out: Dict[str, Dict[str, int]] = {}
        for corpus_id, status, n in rows:
            out.setdefault(corpus_id, {})[status or "pending"] = int(n)
        return out

    def top_folder_status_counts(self, corpus_id: str) -> Dict[str, Dict[str, int]]:
        """``{top_folder: {processing_status: count}}`` for one corpus, in ONE
        grouped query — ``top_folder`` is the first ``/``-delimited segment
        of ``path`` (``""`` for a file with no ``/`` in its path, i.e. one
        sitting directly at the corpus root — mirrors the SharePoint
        site-split planner's ``loose_root_files``; a NULL/blank ``path``
        buckets under ``""`` too, never dropped).

        The per-folder sibling of ``status_counts_for_corpora``: the
        SharePoint completeness check (``app.api.admin_extraction``'s
        ``…/extraction/completeness``) needs an ``indexed``/``rejected``
        breakdown per top-level folder for a single-scope, drive-root
        connection, and doing that with ``count_for_corpus`` once per folder
        made the query count grow with the folder count.
        """
        rows = self.conn.execute(
            "SELECT CASE WHEN path IS NOT NULL AND strpos(path, '/') > 0 "
            "THEN split_part(path, '/', 1) ELSE '' END AS top_folder, "
            "processing_status, COUNT(*) FROM corpus_files WHERE corpus_id = ? "
            "GROUP BY top_folder, processing_status",
            [corpus_id],
        ).fetchall()
        out: Dict[str, Dict[str, int]] = {}
        for top_folder, status, n in rows:
            out.setdefault(top_folder, {})[status or "pending"] = int(n)
        return out

    def extension_status_counts(self, corpus_ids: List[str]) -> Dict[str, Dict[str, Dict[str, int]]]:
        """``{extension: {processing_status: {count, bytes}}}`` across every
        given corpus id, in ONE query — the extraction breakdown surface's
        "indexed / failed / rejected / skipped, by file type" table
        (``GET .../extraction/breakdown``).

        ``extension`` is parsed from ``path`` (the last ``/``-delimited
        segment's suffix after its last ``.``, lowercased; ``""`` when the
        basename has no ``.`` at all, or ``path`` is NULL/blank) — NEVER
        ``filename``/``file_type``. Both of those name the STORED artifact,
        which for a converted document is always the markdown Agnes wrote,
        not the source file a reader actually cares about — every row in a
        SharePoint corpus reports ``file_type = "md"`` regardless of
        whether the original was a ``.pdf`` or a ``.pptx``, which is
        useless for "how many PDFs failed".

        Uses the ``reverse(split_part(reverse(x), delim, 1))`` idiom twice
        (basename off ``path``, then extension off the basename) rather
        than a negative ``split_part`` index — DuckDB and Postgres both
        support it identically, so this method's SQL needs no per-backend
        branch, same as :meth:`top_folder_status_counts`.
        """
        if not corpus_ids:
            return {}
        placeholders = ", ".join("?" for _ in corpus_ids)
        rows = self.conn.execute(
            "SELECT extension, processing_status, COUNT(*), COALESCE(SUM(size_bytes), 0) FROM ( "
            "  SELECT processing_status, size_bytes, "
            "    CASE WHEN strpos(basename, '.') > 0 "
            "         THEN lower(reverse(split_part(reverse(basename), '.', 1))) "
            "         ELSE '' END AS extension "
            "  FROM ( "
            "    SELECT processing_status, size_bytes, "
            "      reverse(split_part(reverse(COALESCE(path, '')), '/', 1)) AS basename "
            f"    FROM corpus_files WHERE corpus_id IN ({placeholders}) "
            "  ) basenames "
            ") extensions "
            "GROUP BY extension, processing_status",
            list(corpus_ids),
        ).fetchall()
        out: Dict[str, Dict[str, Dict[str, int]]] = {}
        for extension, status, n, size_bytes in rows:
            out.setdefault(extension, {})[status or "pending"] = {"count": int(n), "bytes": int(size_bytes or 0)}
        return out

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

    def update_path(self, file_id: str, *, path: Optional[str], filename: str) -> None:
        """Narrower sibling of :meth:`update_in_place`: a rename/move whose
        CONTENT is unchanged (the caller already proved that — e.g. a
        SharePoint crawl item whose cTag still matches, see
        ``connectors.sharepoint.crawler._Ingestor.rename``), so only the
        LOCATION fields move. No ``sha256``/``storage_path``/``size_bytes``
        write, no read of the row's current values first — the whole point
        is to cost one indexed lookup plus one targeted UPDATE, never a
        re-download/re-convert/re-ingest. Leaves ``processing_status``
        untouched, same discipline as ``update_in_place``.
        """
        self.conn.execute(
            "UPDATE corpus_files SET filename = ?, path = ?, updated_at = current_timestamp WHERE id = ?",
            [filename, path, file_id],
        )
