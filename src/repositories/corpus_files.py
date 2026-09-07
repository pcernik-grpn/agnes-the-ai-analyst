"""DuckDB-backed repository for ``corpus_files`` (v82).

One row per uploaded file associated with a ``file_corpora`` corpus.
Tracks the processing lifecycle: pending → processing → indexed | needs_review | rejected.
``processing_detail`` is a JSON dict stored as VARCHAR text.

Template: src/repositories/data_packages.py.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

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
    def _decode_row(row_dict: dict[str, Any]) -> dict[str, Any]:
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
        file_type: str | None,
        size_bytes: int | None,
        storage_path: str | None,
        parent_file_id: str | None = None,
        path: str | None = None,
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

    def get(self, file_id: str) -> dict[str, Any] | None:
        """Fetch one file row by id. Returns ``None`` if not found."""
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM corpus_files WHERE id = ?",
            [file_id],
        ).fetchone()
        if not row:
            return None
        return self._decode_row(dict(zip(self._COLS, row)))

    def get_by_path(self, corpus_id: str, path: str) -> dict[str, Any] | None:
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

    def _filter_clause(self, corpus_id: str, q: str | None, status: str | None) -> tuple[str, list[Any]]:
        """Shared WHERE-clause builder for ``list_for_corpus``/``count_for_corpus``.

        Blank (``None``/empty/whitespace-only) ``q``/``status`` means "no
        filter" — never "match nothing".
        """
        where = ["corpus_id = ?"]
        params: list[Any] = [corpus_id]
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
        limit: int | None = None,
        offset: int = 0,
        q: str | None = None,
        status: str | None = None,
        order: str = "oldest",
    ) -> list[dict[str, Any]]:
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
        q: str | None = None,
        status: str | None = None,
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

    def count_by_corpus(self) -> dict[str, int]:
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

    def search_across_corpora(self, q: str, *, limit: int = 50) -> list[dict[str, Any]]:
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

    def match_filenames(
        self,
        corpus_ids: list[str] | None,
        needles: list[str],
        *,
        extra_file_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Files whose ``filename`` or ``path`` contains any of ``needles``
        (case-insensitive substring), scoped to ``corpus_ids`` plus,
        individually, ``extra_file_ids``.

        The RBAC-scoped counterpart to :meth:`search_across_corpora`: that
        method deliberately searches every corpus (an admin-only picker
        behind its own access gate), while this one exists for a caller that
        must never see a match outside corpora it can already reach — a
        `document:` source-claim link (``app/chat/document_links.py``)
        resolves a citation's filename to a real file, and the citation ref
        is written by the model, not chosen from a list, so the scope has to
        be enforced in the query rather than trusted from the caller.

        ``extra_file_ids`` covers a file granted individually — reachable at
        ``/library/{slug}/f/{file_id}`` (``library_file_detail``'s own
        ``can_parent OR file_granted`` check) even when its PARENT collection
        is not itself in ``corpus_ids``. Without this, a citation naming a
        per-file-shared document could never resolve: the collection-only
        scope this method started with was narrower than the page it links
        to, so a legitimate link was silently suppressed rather than shown.

        ``corpus_ids=None`` means "no restriction" (an admin, who already
        sees every collection — mirrors ``accessible_collection_ids``'s own
        ``None`` convention) — ``extra_file_ids`` is redundant there and
        ignored. ``corpus_ids=[]`` with no ``extra_file_ids`` returns ``[]``
        outright rather than running a query that, on some engines, could
        vacuously match every row with no ``corpus_id`` to compare against.

        Returns candidate ROWS, not a decision — a substring hit is not the
        same claim as an equality match. The caller (``resolve_document_url``)
        re-normalizes ``filename``/``path`` the same way :func:`app.chat.
        sources.verify` does and treats more than one distinct file id as no
        match at all, rather than guessing.
        """
        if not needles:
            return []
        if corpus_ids is not None and not corpus_ids and not extra_file_ids:
            return []
        where: list[str] = []
        params: list[Any] = []
        if corpus_ids is not None:
            scope_clauses = []
            placeholders = ", ".join("?" for _ in corpus_ids)
            if corpus_ids:
                scope_clauses.append(f"corpus_id IN ({placeholders})")
                params.extend(corpus_ids)
            if extra_file_ids:
                id_placeholders = ", ".join("?" for _ in extra_file_ids)
                scope_clauses.append(f"id IN ({id_placeholders})")
                params.extend(extra_file_ids)
            where.append("(" + " OR ".join(scope_clauses) + ")")
        like_clauses = []
        for n in needles:
            like_clauses.append("(LOWER(filename) LIKE LOWER(?) ESCAPE '\\' OR LOWER(path) LIKE LOWER(?) ESCAPE '\\')")
            pattern = f"%{self._escape_like(n)}%"
            params.extend([pattern, pattern])
        where.append("(" + " OR ".join(like_clauses) + ")")
        sql = (
            f"SELECT {self._SELECT} FROM corpus_files WHERE {' AND '.join(where)} "
            "ORDER BY LOWER(filename) ASC, id ASC LIMIT ?"
        )
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._decode_row(dict(zip(self._COLS, r))) for r in rows]

    def filenames_for_ids(self, file_ids: list[str]) -> dict[str, str | None]:
        """``{file_id: filename}`` for exactly the given ids, in ONE query.

        The bulk-by-ids counterpart to :meth:`get`, for a caller that needs
        many filenames at once (retrieval's citation resolution — see
        ``src.ingest.retrieval.search_with_meta``'s filename-fallback
        prepare pass): before this existed, resolving a handful of citation
        filenames meant ``list_for_corpus``-ing every file row of every
        corpus in scope, an O(files in the collection) cost that on a
        collection with hundreds of thousands of files paid several seconds
        on EVERY query whose best-matching passage did not cover the whole
        question — the common case for any query with more than one content
        word. This is O(candidates) instead: exactly the ids the caller
        already has in hand. A requested id with no matching row is simply
        absent from the result — same "requested but not found is just
        absent" contract as :meth:`status_counts_for_corpora`.
        """
        if not file_ids:
            return {}
        placeholders = ", ".join("?" for _ in file_ids)
        rows = self.conn.execute(
            f"SELECT id, filename FROM corpus_files WHERE id IN ({placeholders})",
            list(file_ids),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def status_counts_for_corpora(self, corpus_ids: list[str]) -> dict[str, dict[str, int]]:
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
        out: dict[str, dict[str, int]] = {}
        for corpus_id, status, n in rows:
            out.setdefault(corpus_id, {})[status or "pending"] = int(n)
        return out

    def top_folder_status_counts(self, corpus_id: str) -> dict[str, dict[str, int]]:
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
        out: dict[str, dict[str, int]] = {}
        for top_folder, status, n in rows:
            out.setdefault(top_folder, {})[status or "pending"] = int(n)
        return out

    def extension_status_counts(self, corpus_ids: list[str]) -> dict[str, dict[str, dict[str, int]]]:
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
        out: dict[str, dict[str, dict[str, int]]] = {}
        for extension, status, n, size_bytes in rows:
            out.setdefault(extension, {})[status or "pending"] = {"count": int(n), "bytes": int(size_bytes or 0)}
        return out

    def list_children(self, parent_file_id: str) -> list[dict[str, Any]]:
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
        detail: dict[str, Any] | None = None,
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
        file_type: str | None,
        size_bytes: int | None,
        storage_path: str | None,
        path: str | None,
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

    def update_path(self, file_id: str, *, path: str | None, filename: str) -> None:
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
