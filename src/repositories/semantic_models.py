"""Repository for ``semantic_models`` + ``data_package_semantic_models`` (v116).

The canonical form of a semantic layer is the Ossie document in ``document``.
Every flat projection (metric_definitions, glossary_terms, column_metadata) is
derived from it and can be regenerated; this table is the owner.

``delete_missing`` is the prune primitive and is deliberately keyed on
(source, source_ref): a sync run can only ever delete rows it could have
written. Two sources syncing into one instance cannot collide.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import duckdb

_JSON_COLS = ("document_json", "validation_errors")


class SemanticModelsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    _COLS = [
        "id",
        "slug",
        "name",
        "description",
        "document",
        "document_json",
        "spec_version",
        "content_hash",
        "source",
        "source_ref",
        "status",
        "validation_errors",
        "validated_at",
        "created_at",
        "updated_at",
    ]
    _SELECT = ", ".join(_COLS)

    def _decode(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        out = dict(zip(self._COLS, row))
        for col in _JSON_COLS:
            val = out.get(col)
            if isinstance(val, str):
                out[col] = json.loads(val) if val else None
        return out

    def upsert(
        self,
        *,
        id,
        slug,
        name,
        description,
        document,
        document_json,
        spec_version,
        content_hash,
        source,
        source_ref,
        status,
        validation_errors,
        validated_at,
    ) -> Dict[str, Any]:
        self.conn.execute(
            "DELETE FROM semantic_models WHERE source = ? AND source_ref IS NOT DISTINCT FROM ? AND slug = ?",
            [source, source_ref, slug],
        )
        self.conn.execute(
            f"INSERT INTO semantic_models ({self._SELECT}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp,current_timestamp)",
            [
                id,
                slug,
                name,
                description,
                document,
                json.dumps(document_json) if document_json is not None else None,
                spec_version,
                content_hash,
                source,
                source_ref,
                status,
                json.dumps(validation_errors) if validation_errors is not None else None,
                validated_at,
            ],
        )
        return self.get(id)  # type: ignore[return-value]

    def update_document(
        self,
        model_id: str,
        *,
        name: str,
        description,
        document: str,
        document_json,
        spec_version: str,
        content_hash: str,
        status: str = "valid",
        validation_errors=None,
        validated_at,
    ) -> Dict[str, Any]:
        """Rewrite an existing row's mutable fields in place — a plain
        UPDATE, not ``upsert``'s DELETE+INSERT. Preserves everything
        ``upsert`` has no parameter for: ``id``/``source``/``source_ref``
        (provenance) and, on Postgres, ``sync_mode``/detach-tracking. Used
        by ``apply_manual_model`` and ``update_semantic_model`` when editing
        an already-detached model (F3), so the edit doesn't silently reset
        it to ``sync_mode='synced'``."""
        self.conn.execute(
            "UPDATE semantic_models SET name = ?, description = ?, document = ?, document_json = ?, "
            "spec_version = ?, content_hash = ?, status = ?, validation_errors = ?, validated_at = ?, "
            "updated_at = current_timestamp WHERE id = ?",
            [
                name,
                description,
                document,
                json.dumps(document_json) if document_json is not None else None,
                spec_version,
                content_hash,
                status,
                json.dumps(validation_errors) if validation_errors is not None else None,
                validated_at,
                model_id,
            ],
        )
        return self.get(model_id)  # type: ignore[return-value]

    def get(self, model_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(f"SELECT {self._SELECT} FROM semantic_models WHERE id = ?", [model_id]).fetchone()
        return self._decode(row)

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM semantic_models WHERE slug = ? ORDER BY updated_at DESC",
            [slug],
        ).fetchone()
        return self._decode(row)

    def list_all(self, *, source: Optional[str] = None, source_ref: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = f"SELECT {self._SELECT} FROM semantic_models"
        clauses, params = [], []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if source_ref is not None:
            clauses.append("source_ref IS NOT DISTINCT FROM ?")
            params.append(source_ref)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name"
        return [self._decode(r) for r in self.conn.execute(sql, params).fetchall()]

    def delete(self, model_id: str) -> bool:
        existed = self.get(model_id) is not None
        self.conn.execute("DELETE FROM data_package_semantic_models WHERE model_id = ?", [model_id])
        self.conn.execute("DELETE FROM semantic_models WHERE id = ?", [model_id])
        return existed

    def delete_missing(self, *, source: str, source_ref: Optional[str], keep_slugs: List[str]) -> List[str]:
        rows = self.conn.execute(
            "SELECT id FROM semantic_models "
            "WHERE source = ? AND source_ref IS NOT DISTINCT FROM ? "
            "  AND slug NOT IN (SELECT UNNEST(?::VARCHAR[])) ORDER BY id",
            [source, source_ref, keep_slugs],
        ).fetchall()
        ids = [r[0] for r in rows]
        for model_id in ids:
            self.delete(model_id)
        return ids

    def detach(self, model_id: str, *, by: str, base_hash: str) -> Dict[str, Any]:
        """F3 detach is Postgres-only (A3 ratchet — migrations/versions/
        0090_semantic_models_detach.py has no DuckDB counterpart); this
        DuckDB repo gains no capability that depends on it."""
        from src.repositories import RequiresPostgresBackend

        raise RequiresPostgresBackend("semantic_model_detach")

    def reattach(self, model_id: str) -> Dict[str, Any]:
        """Postgres-only sibling of :meth:`detach` — see that docstring."""
        from src.repositories import RequiresPostgresBackend

        raise RequiresPostgresBackend("semantic_model_detach")

    def update_source_content_hash(self, model_id: str, content_hash: str) -> None:
        """Postgres-only sibling of :meth:`detach` — see that docstring."""
        from src.repositories import RequiresPostgresBackend

        raise RequiresPostgresBackend("semantic_model_detach")

    def mark_source_missing(self, model_id: str) -> None:
        """Postgres-only sibling of :meth:`detach` — see that docstring."""
        from src.repositories import RequiresPostgresBackend

        raise RequiresPostgresBackend("semantic_model_detach")

    def clear_source_missing(self, model_id: str) -> None:
        """Postgres-only sibling of :meth:`detach` — see that docstring."""
        from src.repositories import RequiresPostgresBackend

        raise RequiresPostgresBackend("semantic_model_detach")

    def list_detached_with_health_state(self) -> List[Dict[str, Any]]:
        """Postgres-only sibling of :meth:`detach` — see that docstring.

        The query it mirrors reads ``sync_mode``/``source_missing_since``/
        ``source_content_hash``, none of which exist on this backend, so an
        empty list would be a lie rather than a fail-clean answer."""
        from src.repositories import RequiresPostgresBackend

        raise RequiresPostgresBackend("semantic_model_detach")

    def link_package(self, package_id: str, model_id: str) -> None:
        self.conn.execute(
            "DELETE FROM data_package_semantic_models WHERE package_id = ? AND model_id = ?",
            [package_id, model_id],
        )
        self.conn.execute(
            "INSERT INTO data_package_semantic_models (package_id, model_id) VALUES (?, ?)",
            [package_id, model_id],
        )

    def unlink_package(self, package_id: str, model_id: str) -> None:
        self.conn.execute(
            "DELETE FROM data_package_semantic_models WHERE package_id = ? AND model_id = ?",
            [package_id, model_id],
        )

    def list_for_package(self, package_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {', '.join('m.' + c for c in self._COLS)} FROM semantic_models m "
            "JOIN data_package_semantic_models j ON j.model_id = m.id "
            "WHERE j.package_id = ? ORDER BY m.name",
            [package_id],
        ).fetchall()
        return [self._decode(r) for r in rows]

    def list_packages_for_model(self, model_id: str) -> List[str]:
        """Data package ids ``model_id`` is linked to — the reverse of
        ``list_for_package``. Not part of the original Task 3 interface: added
        for the export/search RBAC gate (Task 10), which must answer "which
        packages grant access to this model", not "which models does this
        package grant" — the direction ``list_for_package`` already covers.
        """
        rows = self.conn.execute(
            "SELECT package_id FROM data_package_semantic_models WHERE model_id = ? ORDER BY package_id",
            [model_id],
        ).fetchall()
        return [r[0] for r in rows]
