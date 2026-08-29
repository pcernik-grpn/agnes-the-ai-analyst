"""Postgres-only repository for ``facts_ingest_runs`` — persisted run reports
for ``POST /api/facts/ingest`` (spec §7.2/§13.2).

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.facts_ingest_runs_repo()``; on a DuckDB-backed instance
that factory call raises ``RequiresPostgresBackend`` (translated to a
``501`` by the app-wide handler).

Deliberately a SEPARATE repository from ``src/repositories/facts_pg.py``
(rather than one more method on ``FactsPgRepository``): a run-report write
must never be part of the ingest transaction, so it wants a name and a call
site that make "this is a best-effort side record, not part of ingest" hard
to miss — see ``app/api/facts.py::facts_ingest``'s log-and-continue wrapper.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


_LIST_JSON_FIELDS = ("corpus_ids", "claims_rejected", "source_urls_rejected", "deferred", "review_items")


def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    for key in _LIST_JSON_FIELDS:
        value = row.get(key)
        if isinstance(value, str):
            try:
                row[key] = json.loads(value)
            except (ValueError, TypeError):
                row[key] = []
    # `anonymization` is a dict, not a list — decoded separately so a bad
    # payload falls back to `{}` (never `[]`, which every OTHER field above
    # correctly falls back to).
    anonymization = row.get("anonymization")
    if isinstance(anonymization, str):
        try:
            row["anonymization"] = json.loads(anonymization)
        except (ValueError, TypeError):
            row["anonymization"] = {}
    elif anonymization is None:
        row["anonymization"] = {}
    if row.get("created_at") is not None:
        row["created_at"] = row["created_at"].isoformat()
    return row


class FactsIngestRunsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self,
        *,
        corpus_ids: List[str],
        caller: str,
        documents_seen: int,
        claims_written: int,
        claims_rejected: List[Dict[str, Any]],
        deferred: List[Dict[str, Any]],
        subjects_created: int,
        subjects_deleted: int,
        review_items: List[Dict[str, Any]],
        anonymization: Optional[Dict[str, Any]] = None,
        source_urls_rejected: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Persist one ingest batch's run report. Returns the generated id.

        Called AFTER ``FactsPgRepository.ingest_batch()`` has already
        returned — this insert is intentionally its own statement, its own
        connection, and its own failure domain (see the module docstring
        and ``app/api/facts.py::facts_ingest``): a report-write failure must
        never look like an ingest failure to the caller.

        ``anonymization`` (spec §9.2) is the producer's OPTIONAL declaration
        that (some of) this batch went through the anonymize-in-front
        pipeline before ingestion — ``None`` (the default, a producer that
        never anonymizes) stores ``{}``, never ``NULL``, so every reader can
        treat the column as always-present.

        ``source_urls_rejected`` (O7 follow-up) is the itemized
        ``{doc_id, reason}`` list of ``documents[].source_url`` values
        ``ingest_batch`` dropped as invalid (never-https, no host, too
        long, ...) — the claim itself still wrote, only its citation link
        is missing. ``None``/omitted (the default, no drops this batch)
        stores ``[]``, same never-``NULL`` contract as every other list
        field here; ``source_urls_rejected_count`` is derived the same way
        ``claims_rejected_count`` is, never trusted from the caller.
        """
        run_id = "ir_" + secrets.token_hex(8)
        source_urls_rejected = source_urls_rejected or []
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO facts_ingest_runs "
                    "(id, corpus_ids, caller, documents_seen, claims_written, "
                    " claims_rejected_count, claims_rejected, "
                    " source_urls_rejected_count, source_urls_rejected, deferred, "
                    " subjects_created, subjects_deleted, review_items, anonymization) "
                    "VALUES (:id, :corpus_ids, :caller, :documents_seen, :claims_written, "
                    "        :claims_rejected_count, :claims_rejected, "
                    "        :source_urls_rejected_count, :source_urls_rejected, :deferred, "
                    "        :subjects_created, :subjects_deleted, :review_items, :anonymization)"
                ),
                {
                    "id": run_id,
                    "corpus_ids": json.dumps(sorted(set(corpus_ids))),
                    "caller": caller,
                    "documents_seen": documents_seen,
                    "claims_written": claims_written,
                    "claims_rejected_count": len(claims_rejected),
                    "claims_rejected": json.dumps(claims_rejected),
                    "source_urls_rejected_count": len(source_urls_rejected),
                    "source_urls_rejected": json.dumps(source_urls_rejected),
                    "deferred": json.dumps(deferred),
                    "subjects_created": subjects_created,
                    "subjects_deleted": subjects_deleted,
                    "review_items": json.dumps(review_items),
                    "anonymization": json.dumps(anonymization or {}),
                },
            )
        return run_id

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(sa.text("SELECT * FROM facts_ingest_runs WHERE id = :id"), {"id": run_id})
                .mappings()
                .first()
            )
        return _decode_row(dict(row)) if row else None

    def list_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Most recent runs first — the source card's history (spec §13.2);
        ``[0]`` is "the LAST run report" the error badges are drawn from."""
        limit = max(1, min(limit, 200))
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM facts_ingest_runs ORDER BY created_at DESC LIMIT :limit"),
                    {"limit": limit},
                )
                .mappings()
                .all()
            )
        return [_decode_row(dict(r)) for r in rows]

    def distinct_corpus_ids(self) -> List[str]:
        """Every collection id that has ever appeared in a run report.

        Interim heuristic for "this file source's scope collections" (spec
        §13.2's source card) until a real connection-to-collection scope
        mapping exists (the connect wizard's step 2, a sibling effort) —
        only a file-source producer ever calls ``POST /api/facts/ingest``,
        so the set of collections it has ever ingested into is, today, the
        best available proxy for its scope. Two sharepoint connections
        would not be distinguishable by this alone; that limitation is
        acceptable for a single-connection instance and named here so it is
        not rediscovered as a surprise.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT DISTINCT jsonb_array_elements_text(corpus_ids) AS cid FROM facts_ingest_runs")
            ).fetchall()
        return sorted({r[0] for r in rows if r[0]})
