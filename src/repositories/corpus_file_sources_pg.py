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
