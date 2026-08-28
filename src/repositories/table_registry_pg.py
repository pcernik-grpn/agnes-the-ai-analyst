"""Postgres-backed table registry repository.

Mirrors ``src/repositories/table_registry.py``. The ``primary_key`` /
``where_filters`` encode/decode helpers are re-exported from the original
module so behaviour stays bit-identical across backends.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.repositories.table_registry import (
    _decode_primary_key,
    _decode_where_filters,
    _encode_primary_key,
    _encode_where_filters,
)


class TableRegistryPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def register(
        self,
        id: str,
        name: str,
        folder: Optional[str] = None,
        sync_strategy: Optional[str] = None,
        primary_key: Union[None, str, List[str]] = None,
        description: Optional[str] = None,
        registered_by: Optional[str] = None,
        source_type: Optional[str] = None,
        bucket: Optional[str] = None,
        source_table: Optional[str] = None,
        source_query: Optional[str] = None,
        query_mode: str = "local",
        sync_schedule: Optional[str] = None,
        profile_after_sync: bool = True,
        registered_at: Optional[datetime] = None,
        incremental_window_days: Optional[int] = None,
        max_history_days: Optional[int] = None,
        incremental_column: Optional[str] = None,
        where_filters: Union[None, str, List[Dict[str, Any]]] = None,
        partition_by: Optional[str] = None,
        partition_granularity: Optional[str] = None,
        initial_load_chunk_days: Optional[int] = None,
        bq_fqn: Optional[str] = None,
        # v74 (#607) — distribution flag decoupled from query_mode.
        server_only: bool = False,
        # v79 — nullable FK to source_connections.id (spec 2026-06-12).
        connection_id: Optional[str] = None,
    ) -> None:
        ts = registered_at or datetime.now(timezone.utc)
        encoded_pk = _encode_primary_key(primary_key)
        encoded_filters = _encode_where_filters(where_filters)
        effective_strategy = sync_strategy or "full_refresh"
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO table_registry (id, name, folder, sync_strategy,
                          primary_key, description, registered_by, registered_at,
                          source_type, bucket, source_table, source_query, query_mode,
                          sync_schedule, profile_after_sync,
                          incremental_window_days, max_history_days, incremental_column,
                          where_filters, partition_by, partition_granularity,
                          initial_load_chunk_days, bq_fqn, server_only, connection_id)
                       VALUES (:id, :name, :folder, :strategy, :pk, :description,
                               :registered_by, :registered_at,
                               :source_type, :bucket, :source_table, :source_query, :query_mode,
                               :sync_schedule, :profile_after_sync,
                               :iwd, :mhd, :icol, :wf, :pby, :pgr, :ilcd, :bq_fqn, :server_only,
                               :connection_id)
                       ON CONFLICT (id) DO UPDATE SET
                         name = EXCLUDED.name,
                         folder = EXCLUDED.folder,
                         sync_strategy = EXCLUDED.sync_strategy,
                         primary_key = EXCLUDED.primary_key,
                         description = EXCLUDED.description,
                         registered_at = EXCLUDED.registered_at,
                         source_type = EXCLUDED.source_type,
                         bucket = EXCLUDED.bucket,
                         source_table = EXCLUDED.source_table,
                         source_query = EXCLUDED.source_query,
                         query_mode = EXCLUDED.query_mode,
                         sync_schedule = EXCLUDED.sync_schedule,
                         profile_after_sync = EXCLUDED.profile_after_sync,
                         incremental_window_days = EXCLUDED.incremental_window_days,
                         max_history_days = EXCLUDED.max_history_days,
                         incremental_column = EXCLUDED.incremental_column,
                         where_filters = EXCLUDED.where_filters,
                         partition_by = EXCLUDED.partition_by,
                         partition_granularity = EXCLUDED.partition_granularity,
                         initial_load_chunk_days = EXCLUDED.initial_load_chunk_days,
                         bq_fqn = EXCLUDED.bq_fqn,
                         server_only = EXCLUDED.server_only,
                         connection_id = EXCLUDED.connection_id"""
                ),
                {
                    "id": id,
                    "name": name,
                    "folder": folder,
                    "strategy": effective_strategy,
                    "pk": encoded_pk,
                    "description": description,
                    "registered_by": registered_by,
                    "registered_at": ts,
                    "source_type": source_type,
                    "bucket": bucket,
                    "source_table": source_table,
                    "source_query": source_query,
                    "query_mode": query_mode,
                    "sync_schedule": sync_schedule,
                    "profile_after_sync": profile_after_sync,
                    "iwd": incremental_window_days,
                    "mhd": max_history_days,
                    "icol": incremental_column,
                    "wf": encoded_filters,
                    "pby": partition_by,
                    "pgr": partition_granularity,
                    "ilcd": initial_load_chunk_days,
                    "bq_fqn": bq_fqn,
                    "server_only": bool(server_only),
                    "connection_id": connection_id,
                },
            )

    @staticmethod
    def _decode_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        if "primary_key" in row_dict:
            row_dict["primary_key"] = _decode_primary_key(row_dict["primary_key"])
        if "where_filters" in row_dict:
            row_dict["where_filters"] = _decode_where_filters(row_dict["where_filters"])
        # ``platforms`` / ``gotchas`` are TEXT columns holding a json.dumps()'d
        # value (unlike the JSONB ``sample_questions`` / ``pairs_well_with`` which
        # the driver returns already-decoded). Without decoding, reads return the
        # raw JSON string and downstream consumers iterate it character-by-character
        # (e.g. the catalog table-page UI).
        #
        # Mirrors DuckDB ``TableRegistryRepository._decode_row``: every list-shaped
        # docs field is normalized to ``[]`` for NULL / empty-string / parse-failure
        # / non-list-parsed-value so behaviour matches across backends. Current
        # consumers all guard with ``or []``, so this only closes a latent gap —
        # but the gap was real (Devin Review on #582, ANALYSIS_0001).
        for k in ("sample_questions", "pairs_well_with", "platforms", "gotchas"):
            if k not in row_dict:
                continue
            v = row_dict[k]
            if v is None or v == "":
                row_dict[k] = []
                continue
            if isinstance(v, list):
                continue
            try:
                parsed = json.loads(v) if isinstance(v, str) else v
                row_dict[k] = parsed if isinstance(parsed, list) else []
            except (ValueError, TypeError):
                row_dict[k] = []
        return row_dict

    def update_docs(
        self,
        table_id: str,
        *,
        # v52 docs surface
        sample_questions: Optional[List[str]] = None,
        things_to_know: Optional[str] = None,
        pairs_well_with: Optional[List[str]] = None,
        clear_sample_questions: bool = False,
        clear_things_to_know: bool = False,
        clear_pairs_well_with: bool = False,
        # v56 structured docs
        grain: Optional[str] = None,
        platforms: Optional[List[str]] = None,
        partition_col: Optional[str] = None,
        history: Optional[str] = None,
        gotchas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Parity with the DuckDB repo: partial write of per-table docs fields
        shown on /catalog/t/<id> and the package-detail page. Optional-is-no-op;
        pass an explicit ``clear_*`` flag to actively NULL a v52 field.

        Column types in the PG model: ``sample_questions`` / ``pairs_well_with``
        are JSONB; ``platforms`` / ``gotchas`` are text columns that store the
        JSON-serialized value (mirrors the DuckDB repo); the rest are plain text.
        """
        fields: List[str] = []
        params: Dict[str, Any] = {"id": table_id}
        if clear_sample_questions:
            fields.append("sample_questions = NULL")
        elif sample_questions is not None:
            fields.append("sample_questions = CAST(:sq AS JSONB)")
            params["sq"] = json.dumps(sample_questions)
        if clear_things_to_know:
            fields.append("things_to_know = NULL")
        elif things_to_know is not None:
            fields.append("things_to_know = :ttk")
            params["ttk"] = things_to_know
        if clear_pairs_well_with:
            fields.append("pairs_well_with = NULL")
        elif pairs_well_with is not None:
            fields.append("pairs_well_with = CAST(:pww AS JSONB)")
            params["pww"] = json.dumps(pairs_well_with)
        if grain is not None:
            fields.append("grain = :grain")
            params["grain"] = grain
        if platforms is not None:
            fields.append("platforms = :plat")
            params["plat"] = json.dumps(platforms)
        if partition_col is not None:
            fields.append("partition_col = :pcol")
            params["pcol"] = partition_col
        if history is not None:
            fields.append("history = :hist")
            params["hist"] = history
        if gotchas is not None:
            fields.append("gotchas = :got")
            params["got"] = json.dumps(gotchas)
        if not fields:
            return
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE table_registry SET {', '.join(fields)} WHERE id = :id"),
                params,
            )

    def unregister(self, table_id: str) -> None:
        """Postgres mirror of ``TableRegistryRepository.unregister`` —
        see that docstring for why the dependants are cleared explicitly.

        ``resource_grants`` would go anyway (its ``resource_id_table`` FK
        is ``ON DELETE CASCADE``); ``data_package_tables`` would NOT — that
        column carries no FK here, so without this the junction row simply
        outlived the table. Both statements are spelled out so the two
        backends read alike and neither can drift.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM data_package_tables WHERE table_id = :id"),
                {"id": table_id},
            )
            conn.execute(
                sa.text("DELETE FROM resource_grants WHERE resource_type = 'table' AND resource_id = :id"),
                {"id": table_id},
            )
            conn.execute(
                sa.text("DELETE FROM table_registry WHERE id = :id"),
                {"id": table_id},
            )

    def delete_internal_except(self, keep_ids: List[str]) -> int:
        """Postgres mirror of
        ``TableRegistryRepository.delete_internal_except``."""
        keep_ids = list(keep_ids)
        with self._engine.begin() as conn:
            if keep_ids:
                rows = conn.execute(
                    sa.text(
                        """DELETE FROM table_registry
                            WHERE source_type = 'internal' AND id NOT IN :keep_ids
                            RETURNING 1"""
                    ).bindparams(sa.bindparam("keep_ids", expanding=True)),
                    {"keep_ids": keep_ids},
                ).fetchall()
            else:
                rows = conn.execute(
                    sa.text(
                        """DELETE FROM table_registry
                            WHERE source_type = 'internal' AND id NOT IN ('')
                            RETURNING 1"""
                    )
                ).fetchall()
        return len(rows)

    def delete_for_corpus(self, corpus_id: str) -> List[str]:
        """Delete all table_registry rows belonging to a file-corpus collection.

        Rows are identified by ``source_type='collection'`` and
        ``bucket=corpus_id``.  Returns the list of deleted table ids so the
        caller can clean up derived artefacts (parquet files, extract.duckdb
        views) before calling ``orchestrator.rebuild_source``.
        """
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text("SELECT id FROM table_registry WHERE source_type = 'collection' AND bucket = :corpus_id"),
                {"corpus_id": corpus_id},
            ).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                conn.execute(
                    sa.text("DELETE FROM table_registry WHERE source_type = 'collection' AND bucket = :corpus_id"),
                    {"corpus_id": corpus_id},
                )
        return ids

    def set_description(self, table_id: str, description: str) -> None:
        """Set only the ``description`` column, leaving every other field
        untouched (unlike ``register()``'s full upsert). Used by the LLM
        auto-doc tool (#399) to backfill descriptions without disturbing
        sync-strategy / partition / docs columns."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE table_registry SET description = :d WHERE id = :id"),
                {"d": description, "id": table_id},
            )

    def set_access_policy(
        self,
        table_id: str,
        sql: Optional[str],
        note: Optional[str],
        updated_by: str,
    ) -> None:
        """Postgres mirror of ``TableRegistryRepository.set_access_policy``.

        ``sql=None`` clears all four ``access_policy_*`` columns; setting a
        policy re-stamps ``access_policy_updated_at`` to "now".
        """
        with self._engine.begin() as conn:
            if sql is None:
                conn.execute(
                    sa.text(
                        """UPDATE table_registry
                           SET access_policy_sql = NULL,
                               access_policy_note = NULL,
                               access_policy_updated_at = NULL,
                               access_policy_updated_by = NULL
                           WHERE id = :id"""
                    ),
                    {"id": table_id},
                )
                return
            conn.execute(
                sa.text(
                    """UPDATE table_registry
                       SET access_policy_sql = :sql,
                           access_policy_note = :note,
                           access_policy_updated_at = :updated_at,
                           access_policy_updated_by = :updated_by
                       WHERE id = :id"""
                ),
                {
                    "sql": sql,
                    "note": note,
                    "updated_at": datetime.now(timezone.utc),
                    "updated_by": updated_by,
                    "id": table_id,
                },
            )

    def mark_semantic_draft_pending(self, table_id: str) -> None:
        """Stamp ``semantic_draft_pending_at`` to now.

        Postgres-only (A3 PG-first ratchet): ``semantic_draft_pending_at``
        is a PG-only column
        (``migrations/versions/0076_semantic_draft_pending.py``), so this
        method has no DuckDB sibling — the DuckDB
        app-state backend simply does not gain this capability. Set BEFORE a
        headless auto-draft session is invoked for this table
        (semantic-phase5 wave 2's auto-draft sweep) — never after — so a
        concurrent or overlapping sweep tick's own coverage read (which
        already excludes rows with this flag set) can never pick the same
        table twice.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE table_registry SET semantic_draft_pending_at = :ts WHERE id = :id"),
                {"ts": datetime.now(timezone.utc), "id": table_id},
            )

    def clear_semantic_draft_pending(self, table_id: str) -> None:
        """Clear the dedup flag ``mark_semantic_draft_pending`` set.

        Postgres-only (A3 PG-first ratchet) — see
        ``mark_semantic_draft_pending`` above. Called once the
        ``authoring_suggestions`` row covering this table's draft is
        resolved — approved or rejected — so a later sweep can draft again
        if the table is still uncovered.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE table_registry SET semantic_draft_pending_at = NULL WHERE id = :id"),
                {"id": table_id},
            )

    def set_policy_mapping(self, table_id: str, value: bool) -> None:
        """Postgres mirror of ``TableRegistryRepository.set_policy_mapping``."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE table_registry SET policy_mapping = :v WHERE id = :id"),
                {"v": bool(value), "id": table_id},
            )

    def get(self, table_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM table_registry WHERE id = :id"),
                    {"id": table_id},
                )
                .mappings()
                .first()
            )
        return self._decode_row(dict(row)) if row else None

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM table_registry WHERE name = :name"),
                    {"name": name},
                )
                .mappings()
                .first()
            )
        return self._decode_row(dict(row)) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT * FROM table_registry ORDER BY name")).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def count_non_internal(self) -> int:
        """Count registered business tables, excluding internal source rows.

        ``source_type='internal'`` rows (agnes_* tables) live in their own
        card on /catalog and are excluded from the headline counter on the
        dashboard + the catalog empty-state hint. NULL ``source_type`` is
        treated as non-internal (COALESCE to '').
        """
        with self._engine.connect() as conn:
            result = conn.execute(
                sa.text("SELECT COUNT(*) FROM table_registry WHERE COALESCE(source_type, '') != 'internal'")
            ).scalar()
        return int(result) if result is not None else 0

    def list_by_source(self, source_type: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM table_registry WHERE source_type = :st ORDER BY name"),
                    {"st": source_type},
                )
                .mappings()
                .all()
            )
        return [self._decode_row(dict(r)) for r in rows]

    def find_by_bq_path(
        self,
        bucket: str,
        source_table: str,
    ) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        """SELECT * FROM table_registry
                       WHERE source_type = 'bigquery'
                         AND bucket IS NOT NULL
                         AND source_table IS NOT NULL
                         AND lower(bucket) = lower(:bucket)
                         AND lower(source_table) = lower(:source_table)
                       ORDER BY registered_at ASC
                       LIMIT 1"""
                    ),
                    {"bucket": bucket, "source_table": source_table},
                )
                .mappings()
                .first()
            )
        return self._decode_row(dict(row)) if row else None

    def list_local(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            if source_type:
                rows = (
                    conn.execute(
                        sa.text(
                            """SELECT * FROM table_registry
                           WHERE query_mode = 'local' AND source_type = :st
                           ORDER BY name"""
                        ),
                        {"st": source_type},
                    )
                    .mappings()
                    .all()
                )
            else:
                rows = (
                    conn.execute(
                        sa.text("SELECT * FROM table_registry WHERE query_mode = 'local' ORDER BY name"),
                    )
                    .mappings()
                    .all()
                )
        return [self._decode_row(dict(r)) for r in rows]
