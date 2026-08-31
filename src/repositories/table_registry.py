"""Repository for table registry."""

import json
from datetime import datetime, timezone
from typing import Any, Optional, List, Dict, Union

import duckdb


def _encode_primary_key(pk: Union[None, str, List[str]]) -> Optional[str]:
    """Serialize primary_key (list-or-string) to a canonical VARCHAR form.

    Frontend + API send lists (composite PKs are real — session-grain MSA
    tables key on `(session_id, event_date)` etc.). The schema column is
    VARCHAR for backwards compat, so we JSON-encode the list on write.
    Accepts a string for legacy CLI callers.
    """
    if pk is None or pk == "":
        return None
    if isinstance(pk, list):
        return json.dumps(pk) if pk else None
    if isinstance(pk, str):
        return json.dumps([pk])
    return json.dumps([str(pk)])


def _encode_where_filters(filters: Union[None, str, List[Dict[str, Any]]]) -> Optional[str]:
    """Serialize where_filters to canonical JSON for storage.

    Accepts None / empty / list / pre-serialized JSON string. Stores as
    canonical JSON so a round-trip is stable. Validation is the API
    layer's job (`connectors.keboola.where_filters.parse_filters`); this
    function only handles encoding.
    """
    if filters is None or filters == "" or filters == []:
        return None
    if isinstance(filters, str):
        try:
            parsed = json.loads(filters)
        except json.JSONDecodeError:
            # Surface malformed payload on subsequent reads rather than dropping;
            # admin tooling validates separately before reaching the repo.
            return filters
        return json.dumps(parsed)
    return json.dumps(filters)


def _decode_where_filters(stored: Any) -> Optional[List[Dict[str, Any]]]:
    if stored is None or stored == "":
        return None
    if isinstance(stored, list):
        return stored
    if isinstance(stored, str):
        try:
            parsed = json.loads(stored)
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            return None
    return None


def _decode_primary_key(stored: Any) -> Optional[List[str]]:
    """Decode a registry-stored primary_key into the API-canonical list-of-str
    form. Tolerates four legacy representations:

    - None / empty string → None
    - JSON-array string `'["a","b"]'` (current canonical)
    - Comma-separated string `'a,b'` (legacy CLI input)
    - Python repr literal `"['a', 'b']"` (legacy bug — see #111)
    - Plain string `'a'` (legacy single-PK CLI input)
    """
    if stored is None or stored == "":
        return None
    if isinstance(stored, list):
        return [str(x) for x in stored if x]
    if not isinstance(stored, str):
        return [str(stored)]
    s = stored.strip()
    if not s:
        return None
    if s.startswith("[") and s.endswith("]"):
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return [str(x) for x in v if x]
        except json.JSONDecodeError:
            # Python repr legacy: `"['a', 'b']"` (single-quoted)
            try:
                import ast

                v = ast.literal_eval(s)
                if isinstance(v, list):
                    return [str(x) for x in v if x]
            except Exception:
                pass
    if "," in s:
        return [p.strip() for p in s.split(",") if p.strip()]
    return [s]


class TableRegistryRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

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
        # v26 — Keboola sync-strategy support fields. All optional; meaningful
        # only when paired with the matching sync_strategy. API-layer
        # validators enforce per-strategy required-field rules and reject
        # conflicting combinations (see app/api/admin.py).
        incremental_window_days: Optional[int] = None,
        max_history_days: Optional[int] = None,
        incremental_column: Optional[str] = None,
        where_filters: Union[None, str, List[Dict[str, Any]]] = None,
        partition_by: Optional[str] = None,
        partition_granularity: Optional[str] = None,
        initial_load_chunk_days: Optional[int] = None,
        # v51 — fully-qualified BigQuery path (``project.dataset.table``).
        # When set, the orchestrator uses this in place of constructing the
        # path from ``_remote_attach.url.project`` + ``bucket`` +
        # ``source_table`` at rebuild. Decouples the UX/RBAC ``bucket``
        # label from the physical BQ dataset name (issue #343).
        bq_fqn: Optional[str] = None,
        # v74 (#607) — distribution flag decoupled from query_mode. When True
        # the row is kept server-side & queryable via `agnes query --remote`,
        # but `agnes pull` skips its parquet. API-layer validator rejects
        # True paired with query_mode='remote'.
        server_only: bool = False,
        # v79 — nullable FK to source_connections.id. NULL = use the default
        # connection for the row's source_type (spec 2026-06-12).
        connection_id: Optional[str] = None,
    ) -> None:
        # `registered_at` defaults to "now" for fresh inserts. Updaters that
        # want to preserve the original registration time across edits pass
        # the existing value explicitly — otherwise PUT /api/admin/registry/{id}
        # would silently reset the timestamp on every edit (issue #130).
        ts = registered_at or datetime.now(timezone.utc)
        encoded_pk = _encode_primary_key(primary_key)
        encoded_filters = _encode_where_filters(where_filters)
        # Mirror the column DEFAULT — explicit None in the INSERT would
        # override the schema default, leaving NULL in the column. Callers
        # that don't pass a strategy expect 'full_refresh' semantics.
        effective_strategy = sync_strategy or "full_refresh"
        self.conn.execute(
            """INSERT INTO table_registry (id, name, folder, sync_strategy,
                primary_key, description, registered_by, registered_at,
                source_type, bucket, source_table, source_query, query_mode,
                sync_schedule, profile_after_sync,
                incremental_window_days, max_history_days, incremental_column,
                where_filters, partition_by, partition_granularity,
                initial_load_chunk_days, bq_fqn, server_only, connection_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name, folder = excluded.folder,
                sync_strategy = excluded.sync_strategy, primary_key = excluded.primary_key,
                description = excluded.description, registered_at = excluded.registered_at,
                source_type = excluded.source_type, bucket = excluded.bucket,
                source_table = excluded.source_table, source_query = excluded.source_query,
                query_mode = excluded.query_mode,
                sync_schedule = excluded.sync_schedule,
                profile_after_sync = excluded.profile_after_sync,
                incremental_window_days = excluded.incremental_window_days,
                max_history_days = excluded.max_history_days,
                incremental_column = excluded.incremental_column,
                where_filters = excluded.where_filters,
                partition_by = excluded.partition_by,
                partition_granularity = excluded.partition_granularity,
                initial_load_chunk_days = excluded.initial_load_chunk_days,
                bq_fqn = excluded.bq_fqn,
                server_only = excluded.server_only,
                connection_id = excluded.connection_id""",
            [
                id,
                name,
                folder,
                effective_strategy,
                encoded_pk,
                description,
                registered_by,
                ts,
                source_type,
                bucket,
                source_table,
                source_query,
                query_mode,
                sync_schedule,
                profile_after_sync,
                incremental_window_days,
                max_history_days,
                incremental_column,
                encoded_filters,
                partition_by,
                partition_granularity,
                initial_load_chunk_days,
                bq_fqn,
                bool(server_only),
                connection_id,
            ],
        )

    @staticmethod
    def _decode_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Apply JSON-decoding to fields stored as canonical VARCHAR."""
        if "primary_key" in row_dict:
            row_dict["primary_key"] = _decode_primary_key(row_dict["primary_key"])
        if "where_filters" in row_dict:
            row_dict["where_filters"] = _decode_where_filters(row_dict["where_filters"])
        # v52 + v56: per-table docs surface for /catalog/t/<id> + the
        # package-detail-page extended sections. DuckDB's JSON column
        # round-trips as a Python str on read; decode to list/dict.
        # ``platforms`` + ``gotchas`` are v56 additions stored as VARCHAR
        # (not JSON column) so they go through the same str→list path.
        # NULL / empty → [].
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
            except Exception:
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
        # v56 structured docs for the package-detail rewrite. Same
        # Optional-is-no-op contract as the v52 fields.
        grain: Optional[str] = None,
        platforms: Optional[List[str]] = None,
        partition_col: Optional[str] = None,
        history: Optional[str] = None,
        gotchas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """v52 + v56: write the per-table docs fields shown on
        /catalog/t/<id> + the per-table extended section on
        /catalog/p/<slug>.

        Optional-is-no-op contract; pass an explicit ``clear_*`` flag to
        actively NULL a v52 field instead of leaving it untouched (mirrors
        the cover_image_url pattern in data_packages.update). v56 fields
        don't have ``clear_*`` flags yet — pass an empty list/empty string
        if you want to actively clear them (rare in practice)."""
        fields: List[str] = []
        params: List[Any] = []
        if clear_sample_questions:
            fields.append("sample_questions = NULL")
        elif sample_questions is not None:
            fields.append("sample_questions = ?")
            params.append(json.dumps(sample_questions))
        if clear_things_to_know:
            fields.append("things_to_know = NULL")
        elif things_to_know is not None:
            fields.append("things_to_know = ?")
            params.append(things_to_know)
        if clear_pairs_well_with:
            fields.append("pairs_well_with = NULL")
        elif pairs_well_with is not None:
            fields.append("pairs_well_with = ?")
            params.append(json.dumps(pairs_well_with))
        # v56 fields
        if grain is not None:
            fields.append("grain = ?")
            params.append(grain)
        if platforms is not None:
            fields.append("platforms = ?")
            params.append(json.dumps(platforms))
        if partition_col is not None:
            fields.append("partition_col = ?")
            params.append(partition_col)
        if history is not None:
            fields.append("history = ?")
            params.append(history)
        if gotchas is not None:
            fields.append("gotchas = ?")
            params.append(json.dumps(gotchas))
        if not fields:
            return
        params.append(table_id)
        self.conn.execute(
            f"UPDATE table_registry SET {', '.join(fields)} WHERE id = ?",
            params,
        )

    # Every dependant a ``table_registry`` row owns, cleared by all three
    # delete paths below (``unregister``, ``delete_internal_except``,
    # ``delete_for_corpus``) so a DELETE means the same thing whichever door
    # it came through and on either backend:
    #
    # - ``data_package_tables`` — the DuckDB DDL declares ``REFERENCES
    #   table_registry(id)`` with no ``ON DELETE`` clause, so deleting a
    #   table that belongs to a data package raised a constraint violation
    #   (a raw 500 out of the calling endpoint). Postgres declares no FK on
    #   that column at all, so the same call succeeded there and left an
    #   orphan junction row — the package kept "containing" a table that no
    #   longer exists.
    # - ``resource_grants`` — Postgres's ``resource_id_table`` FK (migration
    #   0013) IS ``ON DELETE CASCADE``, while DuckDB enforces no FK for it,
    #   so a per-table grant outlived the table it named and would silently
    #   re-apply if that id were ever registered again. Deleting on the
    #   polymorphic ``(resource_type, resource_id)`` pair also reaches
    #   legacy rows written before the per-type column existed.
    #
    # The Postgres sibling runs the same statements (the grants one
    # redundant with its own cascade, kept so the two implementations read
    # alike). A3 PG-first ratchet: this is a code-path fix, not a schema
    # change — the frozen DuckDB DDL is untouched.
    #
    # WHY THESE THREE PATHS DO NOT RUN IN AN EXPLICIT TRANSACTION, where
    # the Postgres sibling wraps each in one ``engine.begin()`` (the obvious
    # parity hardening, and DuckDB refuses it — measured on duckdb 1.5.2,
    # 2026-08-28):
    #
    #     con.execute("BEGIN")
    #     con.execute("DELETE FROM child  WHERE pid = 'b'")
    #     con.execute("DELETE FROM parent WHERE id  = 'b'")
    #     -> ConstraintException: Violates foreign key constraint because
    #        key "pid: b" is still referenced by a foreign key in a
    #        different table.
    #
    # The identical sequence in autocommit succeeds, and swapping the two
    # DELETEs inside the transaction fails the same way — this is DuckDB's
    # documented foreign-key limitation (a referenced key deleted in the
    # same transaction as its referencing rows), not a statement-ordering
    # bug. Wrapping these methods in BEGIN/COMMIT to buy PG-equivalent
    # atomicity would therefore reintroduce, unconditionally, the exact
    # constraint violation the dependant cleanup exists to prevent.
    #
    # Residual risk, accepted knowingly: a failure between the dependant
    # deletes and the registry delete leaves a table stripped of its
    # memberships/grants but still registered — a half-state the Postgres
    # sibling (one ``engine.begin()``) cannot reach. It fails CLOSED (access
    # removed, not granted), it is repairable by re-running the delete, and
    # the only alternative on this backend is a delete that always fails.
    # ``tests/db_pg/test_table_registry_contract.py::TestUnregisterCascade``
    # is the guard: it fails on the DuckDB parameter the moment anyone
    # re-adds the transaction.

    def _delete_dependants(self, table_ids: List[str]) -> Dict[str, int]:
        """Clear both dependants for ``table_ids``; return what was removed.

        Autocommit, deliberately — see the note above this method.
        """
        if not table_ids:
            return {"package_memberships_removed": 0, "grants_revoked": 0}
        placeholders = ",".join("?" for _ in table_ids)
        memberships = self.conn.execute(
            f"DELETE FROM data_package_tables WHERE table_id IN ({placeholders}) RETURNING 1",
            list(table_ids),
        ).fetchall()
        grants = self.conn.execute(
            f"DELETE FROM resource_grants WHERE resource_type = 'table' "
            f"AND resource_id IN ({placeholders}) RETURNING 1",
            list(table_ids),
        ).fetchall()
        return {
            "package_memberships_removed": len(memberships),
            "grants_revoked": len(grants),
        }

    def unregister(self, table_id: str) -> Dict[str, int]:
        """Delete the registry row, and the rows that depend on it.

        Returns ``{"package_memberships_removed": N, "grants_revoked": M}``
        — the cascade revokes access grants, and an audit row for the
        deletion that does not say how many is missing the security-relevant
        half of what happened (precedent:
        ``app/api/marketplaces.py``'s ``revoked_grants``). Both keys are
        always present, zero included, so an unregister that found no
        dependants is distinguishable from one that was never asked to look.

        The statements run in autocommit rather than one transaction — see
        the note above ``_delete_dependants`` for the measured reason
        (DuckDB refuses to delete a referenced key in the same transaction
        as its referencing rows).
        """
        removed = self._delete_dependants([table_id])
        self.conn.execute("DELETE FROM table_registry WHERE id = ?", [table_id])
        return removed

    def delete_internal_except(self, keep_ids: List[str]) -> int:
        """Delete every ``source_type='internal'`` row whose id is NOT in
        ``keep_ids``. Returns the number of rows removed.

        Backs ``connectors.internal.registry.ensure_internal_tables_registered``'s
        stale-row eviction — used when an internal table is renamed (e.g.
        agnes_usage → agnes_telemetry) so the old id doesn't linger in
        /catalog forever. ``keep_ids`` is normally the current canonical id
        set from ``INTERNAL_TABLES``.

        Clears each dropped row's dependants first (see
        ``_delete_dependants``): an internal table that had been added to a
        data package hit the same DuckDB foreign key ``unregister`` does,
        and left the same orphan junction row on Postgres. The ids are read
        before the delete rather than taken from ``RETURNING`` so both
        backends run the identical sequence.
        """
        keep_ids = list(keep_ids)
        placeholders = ",".join("?" for _ in keep_ids) if keep_ids else "''"
        doomed = [
            r[0]
            for r in self.conn.execute(
                f"SELECT id FROM table_registry WHERE source_type = 'internal' AND id NOT IN ({placeholders})",
                keep_ids,
            ).fetchall()
        ]
        self._delete_dependants(doomed)
        rows = self.conn.execute(
            f"""DELETE FROM table_registry
                WHERE source_type = 'internal' AND id NOT IN ({placeholders})
                RETURNING 1""",
            keep_ids,
        ).fetchall()
        return len(rows)

    def delete_for_corpus(self, corpus_id: str) -> List[str]:
        """Delete all table_registry rows belonging to a file-corpus collection.

        Rows are identified by ``source_type='collection'`` and
        ``bucket=corpus_id``.  Returns the list of deleted table ids so the
        caller can clean up derived artefacts (parquet files, extract.duckdb
        views) before calling ``orchestrator.rebuild_source``.

        Clears each dropped row's dependants first (see
        ``_delete_dependants``): a collection table that had been added to a
        data package hit the same DuckDB foreign key ``unregister`` does —
        and this path is called from ``DELETE /api/collections/{id}``, which
        unlinks the parquet files right after, so the 500 landed with the
        durable artefacts already half gone.
        """
        rows = self.conn.execute(
            "SELECT id FROM table_registry WHERE source_type = 'collection' AND bucket = ?",
            [corpus_id],
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            self._delete_dependants(ids)
            self.conn.execute(
                "DELETE FROM table_registry WHERE source_type = 'collection' AND bucket = ?",
                [corpus_id],
            )
        return ids

    def set_description(self, table_id: str, description: str) -> None:
        """Set only the ``description`` column, leaving every other field
        untouched (unlike ``register()``'s full upsert). Used by the LLM
        auto-doc tool (#399) to backfill descriptions without disturbing
        sync-strategy / partition / docs columns."""
        self.conn.execute(
            "UPDATE table_registry SET description = ? WHERE id = ?",
            [description, table_id],
        )

    def set_access_policy(
        self,
        table_id: str,
        sql: Optional[str],
        note: Optional[str],
        updated_by: str,
    ) -> None:
        """Set (or clear) the SQL access policy attached to a table
        (table access policies design doc, §4/§13).

        ``sql=None`` clears all four ``access_policy_*`` columns —
        ``access_policy_updated_at``/``_by`` included — so a cleared table
        reads back exactly as one that never had a policy. Setting a policy
        always re-stamps ``access_policy_updated_at`` to "now"; ``audit_log``
        remains the authoritative history, this column is UI convenience.
        """
        if sql is None:
            self.conn.execute(
                """UPDATE table_registry
                   SET access_policy_sql = NULL,
                       access_policy_note = NULL,
                       access_policy_updated_at = NULL,
                       access_policy_updated_by = NULL
                   WHERE id = ?""",
                [table_id],
            )
            return
        self.conn.execute(
            """UPDATE table_registry
               SET access_policy_sql = ?,
                   access_policy_note = ?,
                   access_policy_updated_at = ?,
                   access_policy_updated_by = ?
               WHERE id = ?""",
            [sql, note, datetime.now(timezone.utc), updated_by, table_id],
        )

    # NOTE (A3 PG-first ratchet): mark_semantic_draft_pending /
    # clear_semantic_draft_pending do NOT exist on this DuckDB repo.
    # table_registry.semantic_draft_pending_at is a Postgres-only column
    # (migrations/versions/0086_semantic_draft_pending.py) — the
    # DuckDB app-state ladder is frozen at v124 and does not gain this
    # capability. See src/repositories/table_registry_pg.py for the
    # PG-only pair and CLAUDE.md -> "Dual-backend discipline".

    def set_policy_mapping(self, table_id: str, value: bool) -> None:
        """Mark (or unmark) a table as referenceable from another table's
        access-policy body (a "mapping table", e.g. a user->cost-center
        lookup — table access policies design doc §15)."""
        self.conn.execute(
            "UPDATE table_registry SET policy_mapping = ? WHERE id = ?",
            [bool(value), table_id],
        )

    def get(self, table_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM table_registry WHERE id = ?", [table_id]).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return self._decode_row(dict(zip(columns, result)))

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM table_registry WHERE name = ?", [name]).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return self._decode_row(dict(zip(columns, result)))

    def list_all(self) -> List[Dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM table_registry ORDER BY name").fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [self._decode_row(dict(zip(columns, row))) for row in results]

    def count_non_internal(self) -> int:
        """Count registered business tables, excluding internal source rows.

        ``source_type='internal'`` rows (agnes_* tables) live in their own
        card on /catalog and are excluded from the headline counter on the
        dashboard + the catalog empty-state hint. NULL ``source_type`` is
        treated as non-internal (COALESCE to '').
        """
        result = self.conn.execute(
            "SELECT COUNT(*) FROM table_registry WHERE COALESCE(source_type, '') != 'internal'"
        ).fetchone()
        return int(result[0]) if result else 0

    def list_by_source(self, source_type: str) -> List[Dict[str, Any]]:
        """List tables for a given source type (keboola, bigquery, jira, etc.)."""
        results = self.conn.execute(
            "SELECT * FROM table_registry WHERE source_type = ? ORDER BY name",
            [source_type],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [self._decode_row(dict(zip(columns, row))) for row in results]

    def find_by_bq_path(
        self,
        bucket: str,
        source_table: str,
    ) -> Optional[Dict[str, Any]]:
        """Look up a BigQuery row by `(bucket, source_table)`.

        Used by /api/query's RBAC patch to decide whether a direct
        `bq."<dataset>"."<source_table>"` reference in user SQL points at a
        registered row. If no row matches, the caller has bypassed the
        registry — the request is rejected before execute.

        Match is case-insensitive on `bucket` and `source_table`. NULL values
        in either column are excluded so a legacy NULL-bucket row never
        masks a legitimate non-NULL lookup.

        When 2+ rows match (no UNIQUE constraint on the
        (source_type, bucket, source_table) triple — admins can register a
        BQ table twice with different ids/names), return the oldest by
        `registered_at` so callers see deterministic resolution.
        """
        result = self.conn.execute(
            """SELECT * FROM table_registry
            WHERE source_type = 'bigquery'
              AND bucket IS NOT NULL
              AND source_table IS NOT NULL
              AND lower(bucket) = lower(?)
              AND lower(source_table) = lower(?)
            ORDER BY registered_at ASC
            LIMIT 1""",
            [bucket, source_table],
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return self._decode_row(dict(zip(columns, result)))

    def list_local(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """List tables with query_mode='local' (data downloaded to parquet)."""
        if source_type:
            results = self.conn.execute(
                "SELECT * FROM table_registry WHERE query_mode = 'local' AND source_type = ? ORDER BY name",
                [source_type],
            ).fetchall()
        else:
            results = self.conn.execute(
                "SELECT * FROM table_registry WHERE query_mode = 'local' ORDER BY name",
            ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [self._decode_row(dict(zip(columns, row))) for row in results]
