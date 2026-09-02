"""Postgres-only repository backing SharePoint collection consolidation
(``POST /api/admin/sharepoint/connections/{id}/collections/consolidate``).

A large site split across many bulk-added scopes (``app/api/
admin_sharepoint.py::bulk_add_scopes``) can leave the site's content spread
across many ``file_corpora`` collections — one per scope, when the admin did
not (or could not yet) use the shared-collection option. This repository
folds several SOURCE collections into one TARGET, entirely a data operation
(no schema change): every row carrying a ``corpus_id`` for a source
collection is re-pointed to the target, in ONE transaction.

PG-first ratchet (A3): brand-new app-state surface, Postgres-only by
construction — the tables this touches beyond the frozen ``file_corpora`` /
``corpus_files`` / ``corpus_chunks`` pair (``corpus_file_sources``,
``corpus_file_events``, ``claims``, ``fact_alias_sources``) are themselves
PG-only (A3 ratchet, no DuckDB sibling), so the operation as a whole can
never run against a DuckDB-backed instance. No matching DuckDB module is
possible or intended — a DuckDB-backed instance never resolves this repo key
at all (``RequiresPostgresBackend`` — see ``src/repositories/__init__.py``),
translated to a typed ``501`` by ``app/main.py``.

Collision handling: a straight ``UPDATE ... SET corpus_id = target`` is safe
for ``corpus_files``/``corpus_chunks``/``corpus_file_events``/``claims`` (no
unique constraint keyed on ``corpus_id`` alone survives the merge — see
:meth:`consolidate`'s docstring for the one exception, ``corpus_files.path``,
which IS pre-checked). ``corpus_file_sources`` (unique on ``(corpus_id,
source_stable_id)``) and ``fact_alias_sources`` (PK ``(type, natural_key,
corpus_id)``) can genuinely collide once merged — the former is pre-checked
and refused (the same document identity present in two source scopes is a
real, if rare, cross-scope duplicate an admin should investigate, not merge
silently); the latter dedups itself via an upsert-then-delete, since a
DIFFERENT-corpus provenance row for the SAME alias is exactly what the table
is *for* (see ``migrations/versions/0084_fact_alias_sources.py``).
"""

from __future__ import annotations

from typing import Any, Dict, List
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class ConsolidationConflict(RuntimeError):
    """Refused: merging ``source_ids`` into ``target_id`` would collide on
    ``kind`` (a table.column description) for the (bounded, first-20) list
    of conflicting keys. The transaction this raised inside is rolled back
    in full — never a partial merge."""

    def __init__(self, kind: str, keys: List[str]) -> None:
        self.kind = kind
        self.keys = keys
        super().__init__(f"{kind}: {len(keys)} conflicting value(s), e.g. {keys[:5]!r}")


class SharePointCollectionConsolidationPgRepository:
    """Postgres-only — see module docstring."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def preview(self, collection_ids: List[str]) -> List[Dict[str, Any]]:
        """One row per LIVE collection in ``collection_ids``: ``{id, name,
        slug, file_count}`` — the dry-run listing. A soft-deleted or unknown
        id is simply absent from the result (the caller already knows which
        ids it asked for)."""
        if not collection_ids:
            return []
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT fc.id, fc.name, fc.slug, "
                        "COALESCE(cnt.file_count, 0) AS file_count "
                        "FROM file_corpora fc "
                        "LEFT JOIN ("
                        "  SELECT corpus_id, COUNT(*) AS file_count FROM corpus_files "
                        "  WHERE corpus_id = ANY(:ids) GROUP BY corpus_id"
                        ") cnt ON cnt.corpus_id = fc.id "
                        "WHERE fc.id = ANY(:ids) AND fc.deleted_at IS NULL "
                        "ORDER BY fc.name"
                    ),
                    {"ids": collection_ids},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def consolidate(self, *, source_ids: List[str], target_id: str) -> Dict[str, int]:
        """Re-point every row carrying a ``corpus_id`` for ``source_ids``
        onto ``target_id``, union ``resource_grants`` (by group, keeping the
        target's own grant when both sides already have one), and
        soft-delete the emptied sources — all in ONE transaction.

        Raises :class:`ConsolidationConflict` (transaction rolled back in
        full, nothing applied) when the merge would collide on
        ``corpus_files.path`` or ``corpus_file_sources.(corpus_id,
        source_stable_id)`` — checked BEFORE any row is touched.

        Returns a summary dict of how many rows moved per table (informational
        only — the route's response, not a dry run; call :meth:`preview` for
        that).
        """
        all_ids = [*source_ids, target_id]
        with self._engine.begin() as conn:
            path_conflicts = (
                conn.execute(
                    sa.text(
                        "SELECT path FROM corpus_files "
                        "WHERE corpus_id = ANY(:ids) AND path IS NOT NULL "
                        "GROUP BY path HAVING COUNT(*) > 1 "
                        "ORDER BY path LIMIT 20"
                    ),
                    {"ids": all_ids},
                )
                .scalars()
                .all()
            )
            if path_conflicts:
                raise ConsolidationConflict("corpus_files.path", list(path_conflicts))

            stable_id_conflicts = (
                conn.execute(
                    sa.text(
                        "SELECT source_stable_id FROM corpus_file_sources "
                        "WHERE corpus_id = ANY(:ids) "
                        "GROUP BY source_stable_id HAVING COUNT(*) > 1 "
                        "ORDER BY source_stable_id LIMIT 20"
                    ),
                    {"ids": all_ids},
                )
                .scalars()
                .all()
            )
            if stable_id_conflicts:
                raise ConsolidationConflict("corpus_file_sources.source_stable_id", list(stable_id_conflicts))

            files_moved = (
                conn.execute(
                    sa.text("UPDATE corpus_files SET corpus_id = :target WHERE corpus_id = ANY(:sources)"),
                    {"target": target_id, "sources": source_ids},
                ).rowcount
                or 0
            )
            chunks_moved = (
                conn.execute(
                    sa.text("UPDATE corpus_chunks SET corpus_id = :target WHERE corpus_id = ANY(:sources)"),
                    {"target": target_id, "sources": source_ids},
                ).rowcount
                or 0
            )
            sources_moved = (
                conn.execute(
                    sa.text("UPDATE corpus_file_sources SET corpus_id = :target WHERE corpus_id = ANY(:sources)"),
                    {"target": target_id, "sources": source_ids},
                ).rowcount
                or 0
            )
            events_moved = (
                conn.execute(
                    sa.text("UPDATE corpus_file_events SET corpus_id = :target WHERE corpus_id = ANY(:sources)"),
                    {"target": target_id, "sources": source_ids},
                ).rowcount
                or 0
            )
            claims_moved = (
                conn.execute(
                    sa.text("UPDATE claims SET corpus_id = :target WHERE corpus_id = ANY(:sources)"),
                    {"target": target_id, "sources": source_ids},
                ).rowcount
                or 0
            )

            # fact_alias_sources: PK (type, natural_key, corpus_id) — a
            # straight UPDATE can collide when a source and the target
            # already independently derived the SAME alias from their own
            # evidence (exactly what this table is designed to hold several
            # rows for). Upsert onto the target, then drop the source rows,
            # so the merge dedups instead of failing.
            conn.execute(
                sa.text(
                    "INSERT INTO fact_alias_sources (type, natural_key, corpus_id) "
                    "SELECT type, natural_key, :target FROM fact_alias_sources "
                    "WHERE corpus_id = ANY(:sources) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"target": target_id, "sources": source_ids},
            )
            conn.execute(
                sa.text("DELETE FROM fact_alias_sources WHERE corpus_id = ANY(:sources)"),
                {"sources": source_ids},
            )

            # resource_grants: union of groups onto the target. A group
            # already granted on the target (directly, or via an earlier
            # source in this same call) is left as-is — its existing
            # `assigned_by`/`requirement` wins over the source's.
            existing_target_groups = set(
                conn.execute(
                    sa.text(
                        "SELECT group_id FROM resource_grants "
                        "WHERE resource_type = 'collection' AND resource_id = :target"
                    ),
                    {"target": target_id},
                )
                .scalars()
                .all()
            )
            source_grants = (
                conn.execute(
                    sa.text(
                        "SELECT DISTINCT ON (group_id) group_id, assigned_by, requirement "
                        "FROM resource_grants "
                        "WHERE resource_type = 'collection' AND resource_id = ANY(:sources) "
                        "ORDER BY group_id, assigned_at"
                    ),
                    {"sources": source_ids},
                )
                .mappings()
                .all()
            )
            grants_merged = 0
            for grant in source_grants:
                group_id = grant["group_id"]
                if group_id in existing_target_groups:
                    continue
                conn.execute(
                    sa.text(
                        "INSERT INTO resource_grants "
                        "(id, group_id, resource_type, resource_id, assigned_by, requirement) "
                        "VALUES (:id, :group_id, 'collection', :target, :assigned_by, :requirement) "
                        "ON CONFLICT (group_id, resource_type, resource_id) DO NOTHING"
                    ),
                    {
                        "id": str(uuid4()),
                        "group_id": group_id,
                        "target": target_id,
                        "assigned_by": grant["assigned_by"],
                        "requirement": grant["requirement"],
                    },
                )
                existing_target_groups.add(group_id)
                grants_merged += 1
            conn.execute(
                sa.text(
                    "DELETE FROM resource_grants WHERE resource_type = 'collection' AND resource_id = ANY(:sources)"
                ),
                {"sources": source_ids},
            )

            conn.execute(
                sa.text(
                    "UPDATE file_corpora SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ANY(:sources)"
                ),
                {"sources": source_ids},
            )

        return {
            "files_moved": files_moved,
            "chunks_moved": chunks_moved,
            "sources_moved": sources_moved,
            "events_moved": events_moved,
            "claims_moved": claims_moved,
            "grants_merged": grants_merged,
        }
