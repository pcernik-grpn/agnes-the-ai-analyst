"""Postgres-only repository for ``facts_llm_cache`` — a content-hash LLM
response cache for the SharePoint fact-extraction stage (cost-levers spec
2026-09-02, lever B).

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.facts_llm_cache_repo()``; on a DuckDB-backed instance
that factory call raises ``RequiresPostgresBackend``.

Every caller goes through ``connectors.sharepoint.facts_extraction``'s own
``_resolve_llm_cache``, which catches that (and any other resolution
failure) and degrades to running the pass with no cache — one log line at
the start of the pass, never a document-by-document failure.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

#: JSONB columns — decoded back to a dict/None on read so a caller never has
#: to care whether the driver handed back a string or a mapping.
_JSON_FIELDS = ("response", "usage")


def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    for key in _JSON_FIELDS:
        value = row.get(key)
        if isinstance(value, str):
            try:
                row[key] = json.loads(value)
            except (ValueError, TypeError):
                row[key] = None if key == "usage" else {}
    return row


class FactsLlmCachePgRepository:
    """Content-hash cache: one row per ``(document sha256, model,
    prompt/ontology fingerprint, call-kind)`` combination — see
    ``connectors.sharepoint.facts_extraction._facts_cache_key``, the single
    place that derives ``cache_key`` from those four inputs. Deliberately
    NOT scoped by connection or collection: a byte-identical document
    copied into several SharePoint folders (or several connections) shares
    one cached response, which is the whole point of the lever.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(sa.text("SELECT * FROM facts_llm_cache WHERE cache_key = :k"), {"k": cache_key})
                .mappings()
                .first()
            )
        return _decode_row(dict(row)) if row else None

    def put(
        self,
        cache_key: str,
        *,
        sha256: str,
        model: str,
        fingerprint: str,
        response: Dict[str, Any],
        usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Upsert: a re-extraction of the SAME document/model/fingerprint
        (e.g. a retried batch after a transient ingest refusal) simply
        overwrites its own row rather than erroring on the primary key."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO facts_llm_cache "
                    "(cache_key, sha256, model, fingerprint, response, usage, created_at) "
                    "VALUES (:cache_key, :sha256, :model, :fingerprint, :response, :usage, :created_at) "
                    "ON CONFLICT (cache_key) DO UPDATE SET "
                    "sha256 = EXCLUDED.sha256, model = EXCLUDED.model, fingerprint = EXCLUDED.fingerprint, "
                    "response = EXCLUDED.response, usage = EXCLUDED.usage, created_at = EXCLUDED.created_at"
                ),
                {
                    "cache_key": cache_key,
                    "sha256": sha256,
                    "model": model,
                    "fingerprint": fingerprint,
                    "response": json.dumps(response),
                    "usage": json.dumps(usage) if usage is not None else None,
                    "created_at": datetime.now(timezone.utc),
                },
            )

    def stats(self) -> Dict[str, Any]:
        """Row count and distinct-document count — ``agnes admin sharepoint
        facts-cache stats``'s whole payload. Distinct-document count is what
        makes the "consultancies keep several copies of the same file"
        saving visible: a big gap between the two numbers IS the dedup win."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) AS rows, COUNT(DISTINCT sha256) AS distinct_documents FROM facts_llm_cache")
            ).one()
        return {"rows": int(row.rows or 0), "distinct_documents": int(row.distinct_documents or 0)}

    def clear(self) -> int:
        """Delete every cached response; returns how many rows were
        removed. Global, not connection-scoped — the cache key carries no
        connection id (see the class docstring), so there is nothing
        narrower to delete."""
        with self._engine.begin() as conn:
            result = conn.execute(sa.text("DELETE FROM facts_llm_cache"))
            return int(result.rowcount or 0)
