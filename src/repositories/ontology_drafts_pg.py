"""Postgres-only repository for ``ontology_drafts``.

The ontology builder's working state (fact-graph-over-Collections §13.2,
"Ontology builder"): every method here mutates ONLY this table — no method
on this repository ever touches ``semantic_models``. That write happens
exactly once, in ``app/api/ontology.py``'s save handler, via
``semantic_model_repo()`` — never through this class. Keeping the two
concerns in separate repositories is what makes "editing sections never
writes until Save" a structural property rather than a discipline someone
has to remember.

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.ontology_drafts_repo()``; on a DuckDB-backed instance
that factory call raises ``RequiresPostgresBackend`` (translated to a
``501`` by the app-wide handler in ``app/main.py``).
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """JSONB columns already decode to Python objects via psycopg — this
    only guards the (rare) case a driver returns them as text."""
    for key in ("node_types", "edge_types", "document_sample", "dry_run_results"):
        v = row.get(key)
        if isinstance(v, str):
            try:
                row[key] = json.loads(v)
            except (ValueError, TypeError):
                pass
    return row


class OntologyDraftsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(self, *, name: str, created_by: Optional[str] = None) -> Dict[str, Any]:
        """Insert a new, empty draft. Returns the created row."""
        draft_id = "ontd_" + secrets.token_hex(8)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("INSERT INTO ontology_drafts (id, name, created_by) VALUES (:id, :name, :created_by)"),
                {"id": draft_id, "name": name, "created_by": created_by},
            )
        return self.get(draft_id)  # type: ignore[return-value]

    def get(self, draft_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(sa.text("SELECT * FROM ontology_drafts WHERE id = :id"), {"id": draft_id})
                .mappings()
                .first()
            )
        return _decode_row(dict(row)) if row else None

    def list(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT * FROM ontology_drafts ORDER BY created_at DESC")).mappings().all()
        return [_decode_row(dict(r)) for r in rows]

    def update(
        self,
        draft_id: str,
        *,
        name: Optional[str] = None,
        node_types: Optional[dict] = None,
        edge_types: Optional[dict] = None,
        document_sample: Optional[list] = None,
        leftover_report: Optional[str] = None,
        dry_run_results: Optional[dict] = None,
    ) -> Optional[Dict[str, Any]]:
        """Patch the provided (non-``None``) fields. A pure draft mutation —
        never touches ``semantic_models``. Returns the updated row, or
        ``None`` if ``draft_id`` doesn't exist."""
        sets: List[str] = []
        params: Dict[str, Any] = {"id": draft_id}
        if name is not None:
            sets.append("name = :name")
            params["name"] = name
        if node_types is not None:
            sets.append("node_types = :node_types")
            params["node_types"] = json.dumps(node_types)
        if edge_types is not None:
            sets.append("edge_types = :edge_types")
            params["edge_types"] = json.dumps(edge_types)
        if document_sample is not None:
            sets.append("document_sample = :document_sample")
            params["document_sample"] = json.dumps(document_sample)
        if leftover_report is not None:
            sets.append("leftover_report = :leftover_report")
            params["leftover_report"] = leftover_report
        if dry_run_results is not None:
            sets.append("dry_run_results = :dry_run_results")
            params["dry_run_results"] = json.dumps(dry_run_results)
        if not sets:
            return self.get(draft_id)
        sets.append("updated_at = now()")
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(f"UPDATE ontology_drafts SET {', '.join(sets)} WHERE id = :id"),
                params,
            )
            if result.rowcount == 0:
                return None
        return self.get(draft_id)

    def mark_saved(self, draft_id: str, *, saved_model_slug: str) -> None:
        """Record the semantic-model slug Save just created — a bookkeeping
        write on the draft's OWN row, distinct from the save action's write
        to ``semantic_models`` (which happens before this is called)."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE ontology_drafts SET saved_model_slug = :slug, updated_at = now() WHERE id = :id"),
                {"slug": saved_model_slug, "id": draft_id},
            )

    def delete(self, draft_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM ontology_drafts WHERE id = :id"), {"id": draft_id})
