"""Postgres-only repository for ``corpus_file_sources`` (v125-equivalent).

Crawler-anchor mapping for the fact-graph-over-Collections design
(docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md
§6, "Prerequisite change to Collections") — maps a producer's
``(corpus_id, source_stable_id)`` delta key to the ``corpus_files`` row it
currently resolves to.

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.corpus_file_sources_repo()``; on a DuckDB-backed instance
that factory call raises ``RequiresPostgresBackend`` (translated to a
``501`` by the app-wide handler).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class CorpusFileSourcesPgRepository:
    """Maps ``(corpus_id, source_stable_id)`` -> ``corpus_file_id``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def resolve(self, corpus_id: str, source_stable_id: str) -> Optional[str]:
        """Return the ``corpus_file_id`` anchored to this stable id, or None."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT corpus_file_id FROM corpus_file_sources "
                        "WHERE corpus_id = :corpus_id AND source_stable_id = :sid"
                    ),
                    {"corpus_id": corpus_id, "sid": source_stable_id},
                )
                .mappings()
                .first()
            )
        return row["corpus_file_id"] if row else None

    def get(self, corpus_file_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the mapping row for a given ``corpus_files.id``, or None."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM corpus_file_sources WHERE corpus_file_id = :id"),
                    {"id": corpus_file_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def get_by_source_doc_id(self, source_doc_id: str) -> Optional[Dict[str, Any]]:
        """Look up the mapping row for a producer's ``source_doc_id`` (the
        crawler's ``sha256[:16]`` citation key), regardless of which
        collection it currently maps into.

        Read-only display helper for the source card's "Last run" drawer
        (TCRD-240/241 live-use feedback: a bare sha16 "told nobody
        anything") — mirrors the arbitrary ``LIMIT 1`` semantics the ingest
        write path itself already resolves through (``facts_pg.py``'s
        ``_resolve_doc``): a ``source_doc_id`` is not schema-guaranteed
        unique across collections, only ``(corpus_id, source_stable_id)``
        is, so a doc_id that somehow landed in two collections resolves to
        whichever row ``LIMIT 1`` picks. Never used for a write decision.
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT corpus_file_id, corpus_id FROM corpus_file_sources "
                        "WHERE source_doc_id = :doc_id LIMIT 1"
                    ),
                    {"doc_id": source_doc_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def resolve_doc_labels(self, source_doc_ids: list[str]) -> Dict[str, Dict[str, Any]]:
        """``source_doc_id -> {"name": filename, "collection": collection_name
        | None}`` for every resolvable id, in ONE query.

        The batched sibling of :meth:`get_by_source_doc_id` + a
        ``corpus_files``/``file_corpora`` lookup per id — the card's "Last
        run" drawer (``app.web.router._resolve_sharepoint_rejection_doc_labels``)
        used to spend 3 round trips PER unique rejected/deferred doc_id in a
        run report, which on a run with hundreds of rejections dominated the
        page's query count. An id this instance has never seen is simply
        absent (same "None" contract as the single-id lookup); when a
        ``source_doc_id`` maps to more than one row (schema allows it, see
        :meth:`get_by_source_doc_id`'s docstring) an arbitrary one wins, same
        as the single-id lookup's unordered ``LIMIT 1``.
        """
        if not source_doc_ids:
            return {}
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT cfs.source_doc_id AS doc_id, cf.filename AS name, fc.name AS collection_name "
                        "FROM corpus_file_sources cfs "
                        "JOIN corpus_files cf ON cf.id = cfs.corpus_file_id "
                        "LEFT JOIN file_corpora fc ON fc.id = cf.corpus_id "
                        "WHERE cfs.source_doc_id = ANY(:ids)"
                    ),
                    {"ids": list(source_doc_ids)},
                )
                .mappings()
                .all()
            )
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            out.setdefault(r["doc_id"], {"name": r["name"], "collection": r["collection_name"]})
        return out

    def files_for_doc(self, corpus_id: str, source_doc_id: str) -> list[str]:
        """Every ``corpus_file_id`` anchored to ``(corpus_id, source_doc_id)``
        — more than one row can legally match (TCRD-241: a byte-identical
        copy shares its sha-derived doc_id with every other copy). Mirrors
        the scope of ``FactsPgRepository.ingest_batch``'s own internal
        ``_copies_for`` (that closure resolves the ingest-time WRITE
        winner; this is the public, read-only "who are the copies"
        equivalent for a caller outside the ingest path — e.g. the facts
        ledger's reset-no-claims recovery, TCRD-296 gap #62)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT corpus_file_id FROM corpus_file_sources "
                    "WHERE corpus_id = :corpus_id AND source_doc_id = :doc_id"
                ),
                {"corpus_id": corpus_id, "doc_id": source_doc_id},
            ).all()
        return [r[0] for r in rows]

    def pending_extraction_candidates(self, corpus_ids: list[str]) -> list[Dict[str, Any]]:
        """Every indexed, source-anchored file across ``corpus_ids``,
        projected to ``{file_id, sha256}`` — ONE query, regardless of how
        many files the corpora hold (TCRD-296 gap #72).

        This is the candidate set ``connectors.sharepoint.facts_extraction
        .count_pending_documents`` used to assemble with a ``list_for_corpus``
        call per collection PLUS a ``get(file_id)`` call PER FILE (282k round
        trips on the connection that surfaced this) — replaced by the same
        ``indexed`` + ``source_doc_id IS NOT NULL`` pre-filter that loop
        applied, pushed into the join instead of Python. The caller still
        finishes the per-file decision itself (the ledger's ``docs_state``
        is a JSON blob, not a joinable table), but over this single,
        already-narrow result set rather than one query per row.
        """
        if not corpus_ids:
            return []
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT cf.id AS file_id, cf.sha256 AS sha256 "
                        "FROM corpus_files cf "
                        "JOIN corpus_file_sources cfs ON cfs.corpus_file_id = cf.id "
                        "WHERE cf.corpus_id = ANY(:corpus_ids) "
                        "AND cf.processing_status = 'indexed' "
                        "AND cfs.source_doc_id IS NOT NULL"
                    ),
                    {"corpus_ids": list(corpus_ids)},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def upsert(
        self,
        *,
        corpus_file_id: str,
        corpus_id: str,
        source_stable_id: str,
        source_doc_id: Optional[str] = None,
        source_sha256: Optional[str] = None,
        source_url: Optional[str] = None,
    ) -> None:
        """Insert or refresh the mapping keyed on ``corpus_file_id`` (its PK).

        The caller has already resolved (or just minted) the target row's id
        before calling this — ``(corpus_id, source_stable_id)`` uniqueness is
        enforced by the table constraint, not by this upsert's conflict
        target. ``source_doc_id`` is rewritten here when a provisional id is
        replaced by the real one on first content crawl (spec §6).

        The three optional columns are ``COALESCE``d against the stored row,
        so **omitting** one leaves it alone and only a supplied value
        overwrites. These fields are independently optional on the upload
        endpoint, so a re-sync that carries `source_stable_ids` but not
        `source_doc_ids` (a rename-only delta) otherwise reset a real
        `source_doc_id` back to NULL — which would break exactly the lookup
        the `source_doc_id` index exists for. The wire format has no way to
        express "clear this field", so there is no meaning being lost.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO corpus_file_sources "
                    "(corpus_file_id, corpus_id, source_stable_id, source_doc_id, source_sha256, source_url) "
                    "VALUES (:corpus_file_id, :corpus_id, :source_stable_id, "
                    "        :source_doc_id, :source_sha256, :source_url) "
                    "ON CONFLICT (corpus_file_id) DO UPDATE SET "
                    "corpus_id = EXCLUDED.corpus_id, "
                    "source_stable_id = EXCLUDED.source_stable_id, "
                    "source_doc_id = COALESCE(EXCLUDED.source_doc_id, corpus_file_sources.source_doc_id), "
                    "source_sha256 = COALESCE(EXCLUDED.source_sha256, corpus_file_sources.source_sha256), "
                    "source_url = COALESCE(EXCLUDED.source_url, corpus_file_sources.source_url)"
                ),
                {
                    "corpus_file_id": corpus_file_id,
                    "corpus_id": corpus_id,
                    "source_stable_id": source_stable_id,
                    "source_doc_id": source_doc_id,
                    "source_sha256": source_sha256,
                    "source_url": source_url,
                },
            )
