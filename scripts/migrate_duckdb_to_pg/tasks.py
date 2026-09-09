"""Migration task implementations for DuckDB → Postgres.

The generic copy loop in ``__init__.py`` handles every table in
``Base.metadata.sorted_tables`` via :class:`GenericCopyTask`, which does:

  SELECT * FROM <source>  →  INSERT … ON CONFLICT DO NOTHING  →  subset validate

:data:`EXPLICIT_TASKS` overrides the generic path for tables that need
per-row work (type coercion, NULL backfill, FTS rebuild, etc.).  Currently
all tables are handled generically — JSON/JSONB coercion is covered by the
:data:`_JSON_COLUMNS` allowlist used inside ``_build_insert``.  If a future
table genuinely requires a custom step, add an override class here and
register it in :data:`EXPLICIT_TASKS`.

Adding a new table that does NOT need per-row work: register the model in
``src/models/`` — the generic loop picks it up automatically through
``Base.metadata.sorted_tables``.  The test
``tests/db_pg/test_data_migration.py::test_every_pg_model_has_a_migration_task``
ensures no model goes uncovered.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

import duckdb
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.sql_ident import quote_ident

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON/JSONB column registry
# ---------------------------------------------------------------------------


def _build_json_columns() -> frozenset[tuple[str, str]]:
    """Derive the set of ``(table, column)`` JSONB pairs from the PG
    Base.metadata. H6-NEW: pre-fix the set was hand-maintained and
    drifted (``data_packages.tags`` declared JSONB in
    ``src/models/data_packages.py:55`` but absent here, along with
    ``data_packages.when_to_use``, ``when_not_to_use``,
    ``example_questions``, ``recipes.related_table_ids``,
    ``table_registry.sample_questions``, and
    ``table_registry.pairs_well_with``). Deriving dynamically guarantees
    every model-declared JSONB column gets the ``CAST(:col AS JSONB)``
    treatment in ``_build_insert`` + the ``json.dumps`` wrapper in the
    copy loop.
    """
    from sqlalchemy.dialects.postgresql import JSONB

    import src.models  # noqa: F401 — registers all models on Base
    from src.db_pg import Base

    out: set[tuple[str, str]] = set()
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                out.add((table.name, col.name))
    return frozenset(out)


_JSON_COLUMNS: frozenset[tuple[str, str]] = _build_json_columns()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolved_columns(table_name: str, duck_conn: duckdb.DuckDBPyConnection) -> List[str]:
    """Return ordered column list for *table_name* from DuckDB information_schema.

    Returns ``[]`` when the table does not exist in DuckDB — this is a valid
    state when PG has newer tables (added by alembic migrations) that the
    source DuckDB instance has never had. The caller treats ``[]`` as an
    empty source (0 rows to copy).
    """
    rows = duck_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_name = '{table_name}' AND table_schema = 'main' "
        "ORDER BY ordinal_position"
    ).fetchall()
    if rows:
        return [r[0] for r in rows]
    # Fallback: PRAGMA (DuckDB extension tables may not appear in
    # information_schema on all versions). Catch CatalogException
    # so a table that exists in PG but not in DuckDB returns [] rather
    # than raising — callers treat [] as "empty source, nothing to copy".
    try:
        pragma = duck_conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    except Exception:
        return []
    return [r[1] for r in pragma]


def _build_insert(
    target_table: str,
    columns: Sequence[str],
    pk_columns: Sequence[str],
) -> str:
    """Build the parametrised INSERT used by GenericCopyTask.run.

    Columns listed in :data:`_JSON_COLUMNS` are wrapped in
    ``CAST(:col AS JSONB)`` so DuckDB-native dict/list values are coerced
    correctly in Postgres.

    NEW-X: the ON CONFLICT target is intentionally OMITTED — bare
    ``ON CONFLICT DO NOTHING`` matches every UNIQUE constraint on the
    table, not just the PK. Pre-fix the form was
    ``ON CONFLICT ({pk}) DO NOTHING`` which let an INSERT collide on
    a non-PK UNIQUE (e.g. ``users.email``) raise UniqueViolation
    mid-batch — psycopg's executemany then left a partial commit
    in PG and aborted with secondary rows uninserted.

    Side note: ``pk_columns`` is still part of the signature because
    the validator + the row-hash code use it; the parameter is unused
    here on purpose.
    """
    placeholders: List[str] = []
    for c in columns:
        if (target_table, c) in _JSON_COLUMNS:
            placeholders.append(f"CAST(:{c} AS JSONB)")
        else:
            placeholders.append(f":{c}")
    col_list = ", ".join(columns)
    val_list = ", ".join(placeholders)
    return f"INSERT INTO {target_table} ({col_list}) VALUES ({val_list}) ON CONFLICT DO NOTHING"


def _not_null_columns_with_default(table_name: str) -> dict[str, Any]:
    """Return ``{column_name: server_default}`` for NOT NULL columns whose
    PG schema has a server_default.

    Migrator collateral: DuckDB rows sometimes carry ``None`` in columns
    that are NOT NULL on the PG side with a ``server_default`` (most
    commonly ``created_at`` / ``updated_at`` with
    ``server_default=CURRENT_TIMESTAMP``). SQLAlchemy treats an explicit
    ``None`` in the bind parameters as "literal NULL" and PG raises
    ``NotNullViolation`` even though the column has a default — defaults
    fire only when the column is absent from the INSERT, not when bound
    to NULL. Substituting the default value at copy time keeps the
    INSERT uniform across rows while honouring the schema.
    """
    import src.models  # noqa: F401
    from src.db_pg import Base

    table = Base.metadata.tables.get(table_name)
    if table is None:
        return {}
    out: dict[str, Any] = {}
    for c in table.columns:
        if c.nullable:
            continue
        if c.server_default is None:
            continue
        out[c.name] = c.server_default
    return out


def _substitute_default(value: Any, default: Any, *, column_name: str = "") -> Any:
    """Materialise a server_default ONLY when the row's value is None.

    Honour the existing value in every other case — never overwrite an
    operator-supplied timestamp or any other typed value. Returning
    ``value`` unchanged for non-None inputs is the audit-integrity
    contract; the only legitimate use is to fill genuine NULLs in
    NOT-NULL columns where the source carries no value.

    Returns None when no usable default is found (caller decides what
    to do — typically let the INSERT raise NotNullViolation so the
    operator sees the column needs attention).

    ``CURRENT_TIMESTAMP`` / ``CURRENT_DATE`` text defaults materialise to a
    timezone-aware Python ``datetime`` / ``date`` so the migrator can ship
    a binding psycopg understands.
    """
    if value is not None:
        return value
    from datetime import date, datetime, timezone

    from sqlalchemy.schema import DefaultClause

    if isinstance(default, DefaultClause):
        sql = str(default.arg).upper()
    else:
        sql = str(default).upper()
    if "CURRENT_TIMESTAMP" in sql or "NOW()" in sql:
        return datetime.now(timezone.utc)
    if "CURRENT_DATE" in sql:
        return date.today()
    return value


def _array_columns_for(table_name: str) -> set[str]:
    """Return the names of PG ``ARRAY`` columns on *table_name*.

    Introspected from ``Base.metadata`` at first use and cached. PG arrays
    are NOT the same wire format as JSONB — psycopg expects a Python list
    for an array column and serialises it as a ``{a, b, c}`` literal,
    while a JSON string starting with ``[`` would be misread as an
    ill-formed PG array literal (``"[" must introduce explicitly-specified
    array dimensions``, surfaced live on agnes-dev migration).

    DuckDB returns these columns as JSON strings, so we json.loads them
    before passing to psycopg. Detecting from metadata avoids the
    duplicate maintenance burden of a hand-curated registry like
    ``_JSON_COLUMNS``.
    """
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY

    import src.models  # noqa: F401 — ensures every model is imported
    from src.db_pg import Base

    table = Base.metadata.tables.get(table_name)
    if table is None:
        return set()
    return {c.name for c in table.columns if isinstance(c.type, PG_ARRAY)}


def _normalize_for_pg(value: Any) -> Any:
    """Serialise DuckDB-native dict/list to JSON text for PG CAST."""
    import json as _json

    if isinstance(value, (dict, list)):
        return _json.dumps(value)
    return value


def _coerce_array_value(value: Any) -> Any:
    """Coerce a DuckDB-returned ARRAY value into a Python list for psycopg.

    DuckDB returns ``ARRAY``-typed columns either as a Python list already
    (when the source type is ``LIST``/``ARRAY``) or as a JSON-encoded text
    string (when the source type is ``JSON``/``VARCHAR`` and the producer
    serialised manually — what ``metric_definitions.dimensions`` does on
    agnes-dev). psycopg's PG-array adapter wants the list form; if it
    receives the string form it forwards it to PG as text and PG raises
    ``InvalidTextRepresentation: malformed array literal``.
    """
    import json as _json

    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = _json.loads(stripped)
        except _json.JSONDecodeError:
            return value
        if isinstance(parsed, list):
            return parsed
        return value
    return value


def _checksum(values: Sequence[Sequence[Any]]) -> str:
    """Stable SHA-256 digest over a sorted list of PK tuples."""
    h = hashlib.sha256()
    for v in sorted(tuple(str(x) for x in row) for row in values):
        for item in v:
            h.update(item.encode("utf-8"))
            h.update(b"|")
        h.update(b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# GenericCopyTask
# ---------------------------------------------------------------------------


@dataclass
class GenericCopyTask:
    """SELECT * → INSERT … ON CONFLICT DO NOTHING + subset validate.

    Default handler for any table that does not require per-row work.
    Reads every column from DuckDB, batch-inserts into PG with bare
    ``ON CONFLICT DO NOTHING`` (matches every UNIQUE constraint, not
    just the PK — see :func:`_build_insert` for the NEW-X rationale),
    and records PK-set + row-count for post-copy validation.

    Attributes:
        table_name:  DuckDB source table name (identical to PG target).
        pk_columns:  PK columns used by :func:`validate_task`.  When
                     empty, validation falls back to the ``id`` column.
        batch_size:  Rows per INSERT batch.
        fk_parents:  Optional ``{child_column: (parent_table, parent_pk)}``
                     map.  When set, :meth:`run` drops source rows whose
                     non-null FK value has no matching parent row *in the
                     DuckDB source* (a dangling grant / orphan) and logs a
                     warning, instead of letting the PG INSERT abort the
                     whole task with a ``ForeignKeyViolation``.  Orphans
                     arise when a parent row was deleted without cascading
                     to its children (e.g. a table unregistered while an
                     RBAC ``resource_grants`` row still points at it).
    """

    table_name: str
    pk_columns: List[str] = field(default_factory=lambda: ["id"])
    batch_size: int = 500
    fk_parents: Dict[str, Tuple[str, str]] = field(default_factory=dict)

    # Source == target for all current tables.
    @property
    def source_table(self) -> str:  # noqa: D401
        return self.table_name

    @property
    def target_table(self) -> str:  # noqa: D401
        return self.table_name

    def run(
        self,
        duck_conn: duckdb.DuckDBPyConnection,
        pg_engine: Engine,
        *,
        dry_run: bool = False,
    ) -> int:
        """Copy rows from DuckDB to PG.  Returns number of rows considered."""
        columns = _resolved_columns(self.table_name, duck_conn)
        if not columns:
            # Table does not exist in DuckDB — PG-only table added by a
            # later alembic migration. Nothing to copy; this is not an error.
            log.info("migrate %s: table absent in DuckDB, skipping (0 rows)", self.table_name)
            return 0

        # Probe for DuckDB-only columns that hold data. Silent column
        # drop is data loss; force the operator to either land the
        # alembic migration (so PG has the column) or explicitly
        # accept the loss by removing the column from the DuckDB
        # source first.
        import src.models as _m  # noqa: F401 — ensure models registered
        from src.db_pg import Base as _Base

        _pg_table = _Base.metadata.tables.get(self.target_table)
        _pg_cols = {c.name for c in _pg_table.columns} if _pg_table is not None else set()
        _duck_only = [c for c in columns if c not in _pg_cols]
        for _col in _duck_only:
            try:
                _non_null = duck_conn.execute(
                    f"SELECT COUNT(*) FROM {quote_ident(self.table_name)} WHERE {quote_ident(_col)} IS NOT NULL"
                ).fetchone()[0]
            except Exception:
                _non_null = 0
            if _non_null > 0:
                raise RuntimeError(
                    f"Column '{self.table_name}.{_col}' exists in DuckDB with "
                    f"{_non_null} non-null row(s) but is missing from the PG "
                    f"schema — data will be lost. Land the alembic migration "
                    f"that adds the column, or drop the column from DuckDB "
                    f"before re-running."
                )
            log.warning(
                "DuckDB-only column %s.%s is empty; skipping from PG INSERT",
                self.table_name,
                _col,
            )
        # Restrict `columns` to the PG-side set so the INSERT is well-formed.
        columns = [c for c in columns if c in _pg_cols]

        log.info("migrate %s (%d cols, dry_run=%s)", self.table_name, len(columns), dry_run)

        select_sql = f"SELECT {', '.join(columns)} FROM {self.source_table}"
        rows = duck_conn.execute(select_sql).fetchall()
        if not rows:
            log.info("  empty source; nothing to do")
            return 0

        if self.fk_parents:
            rows = self._drop_fk_orphans(rows, columns, duck_conn)
            if not rows:
                log.info("  all rows dropped as FK orphans; nothing to do")
                return 0

        insert_sql = _build_insert(self.target_table, columns, self.pk_columns)
        array_cols = _array_columns_for(self.target_table)
        default_cols = _not_null_columns_with_default(self.target_table)
        considered = 0
        batch: List[Dict[str, Any]] = []
        for r in rows:
            d: Dict[str, Any] = {}
            for k, v in zip(columns, r):
                if k in array_cols:
                    d[k] = _coerce_array_value(v)
                else:
                    d[k] = _normalize_for_pg(v)
                if k in default_cols:
                    d[k] = _substitute_default(d[k], default_cols[k])
            batch.append(d)
            considered += 1
            if len(batch) >= self.batch_size and not dry_run:
                with pg_engine.begin() as conn:
                    conn.execute(sa.text(insert_sql), batch)
                batch.clear()
        if batch and not dry_run:
            with pg_engine.begin() as conn:
                conn.execute(sa.text(insert_sql), batch)

        log.info("  considered %d rows%s", considered, " (dry-run)" if dry_run else "")
        return considered

    def _drop_fk_orphans(
        self,
        rows: List[tuple],
        columns: List[str],
        duck_conn: duckdb.DuckDBPyConnection,
    ) -> List[tuple]:
        """Drop rows whose non-null FK value has no parent in the DuckDB source.

        A parent that is absent from the source will also be absent from PG
        (the migration copies source → target), so such a child row would
        abort the whole task with a ``ForeignKeyViolation``. These are
        genuine orphans (a grant pointing at a deleted resource); dropping
        them loses nothing and is logged loudly per-column.
        """
        col_index = {c: i for i, c in enumerate(columns)}
        keep = list(rows)
        for child_col, (parent_table, parent_pk) in self.fk_parents.items():
            idx = col_index.get(child_col)
            if idx is None:
                # FK column not part of the copied column set — nothing to check.
                continue
            try:
                parent_ids = {r[0] for r in duck_conn.execute(f"SELECT {quote_ident(parent_pk)} FROM {quote_ident(parent_table)}").fetchall()}
            except Exception:
                # Parent table absent in the DuckDB source — can't determine
                # orphans here; leave the rows and let PG's FK be the backstop.
                log.debug(
                    "fk-orphan check for %s.%s skipped: parent %s absent in source",
                    self.table_name,
                    child_col,
                    parent_table,
                )
                continue
            survivors: List[tuple] = []
            dropped = 0
            for row in keep:
                val = row[idx]
                if val is None or val in parent_ids:
                    survivors.append(row)
                else:
                    dropped += 1
                    if dropped <= 20:
                        log.warning(
                            "DROP orphan %s row (id=%r): %s=%r has no parent in %s",
                            self.table_name,
                            row[col_index["id"]] if "id" in col_index else "?",
                            child_col,
                            val,
                            parent_table,
                        )
            if dropped:
                log.warning(
                    "migrate %s: dropped %d orphaned row(s) on %s → %s",
                    self.table_name,
                    dropped,
                    child_col,
                    parent_table,
                )
            keep = survivors
        return keep

    def validate(
        self,
        duck_conn: duckdb.DuckDBPyConnection,
        pg_engine: Engine,
    ) -> Dict[str, Any]:
        """Verify every DuckDB source PK made it into PG (source ⊆ target).

        Containment, not exact equality: ``checksum_match`` is True when the
        source PK-set is a subset of the target PK-set. A target *superset*
        is legitimate and expected — after cutover the app writes new rows to
        PG, and the compose ``data-migrate`` one-shot re-runs on every deploy;
        requiring exact equality there would fail (PG has grown) and, because
        ``app`` gates on ``data-migrate`` exiting 0, take the instance down.
        Alembic-seeded rows (system groups, etc.) present in PG but not the
        source are tolerated for the same reason. A genuine copy failure —
        a source row missing from the target — still fails (it is in
        ``missing_count``).

        When the table is absent in DuckDB (PG-only table from a later
        alembic migration), the source is empty → the subset is trivially
        satisfied.  Orphaned source rows dropped by :meth:`_drop_fk_orphans`
        are excluded from the source set so they are not counted as missing.
        """
        pk_select = ", ".join(self.pk_columns)
        try:
            duck_rows = duck_conn.execute(f"SELECT {pk_select} FROM {self.source_table}").fetchall()
        except Exception:
            # Table absent in DuckDB — treat as empty source.
            duck_rows = []
        duck_count = len(duck_rows)

        with pg_engine.connect() as conn:
            pg_rows = conn.execute(sa.text(f"SELECT {pk_select} FROM {self.target_table}")).all()
        pg_count = len(pg_rows)

        duck_set = {tuple(r) for r in duck_rows}
        pg_set = {tuple(r) for r in pg_rows}
        missing = duck_set - pg_set
        # FK orphans intentionally dropped by run() are not real copy
        # failures; exclude them from the "missing" set.
        if self.fk_parents and missing:
            missing = {pk for pk in missing if not self._is_dropped_orphan(pk, duck_conn)}

        return {
            "table": self.target_table,
            "duckdb_rows": duck_count,
            "pg_rows": pg_count,
            "missing_count": len(missing),
            "checksum_match": len(missing) == 0,
        }

    def _is_dropped_orphan(
        self,
        pk: tuple,
        duck_conn: duckdb.DuckDBPyConnection,
    ) -> bool:
        """True when the source row with this PK was an FK orphan (so run()
        legitimately dropped it and it is expected to be absent from PG)."""
        if self.pk_columns != ["id"]:
            # Orphan-exclusion is only wired for single ``id`` PK tables
            # (the only ones that declare fk_parents today).
            return False
        (pk_val,) = pk
        for child_col, (parent_table, parent_pk) in self.fk_parents.items():
            try:
                row = duck_conn.execute(
                    f'SELECT {quote_ident(child_col)} FROM {quote_ident(self.source_table)} WHERE "id" = ?',
                    [pk_val],
                ).fetchone()
            except Exception:
                continue
            if not row:
                continue
            val = row[0]
            if val is None:
                continue
            try:
                exists = duck_conn.execute(
                    f"SELECT 1 FROM {quote_ident(parent_table)} WHERE {quote_ident(parent_pk)} = ?",
                    [val],
                ).fetchone()
            except Exception:
                continue
            if not exists:
                return True
        return False


# ---------------------------------------------------------------------------
# Explicit per-table overrides
# ---------------------------------------------------------------------------
#
# All 39 tables are currently handled generically.  JSON/JSONB coercion is
# covered by _JSON_COLUMNS above; there are no enum casts, NULL backfills,
# or FTS rebuilds required for the DuckDB → PG migration path:
#
#   - audit_log: _audit_log_transform was a no-op; JSONB is in _JSON_COLUMNS.
#   - usage_events: is_error has DEFAULT FALSE in DuckDB → no NULLs in prod.
#   - personal_access_tokens: token_hash is plain VARCHAR in both stores.
#   - knowledge_items: PG schema has no tsvector column (FTS is DuckDB-only).
#   - store_submissions: status is String, not a PG enum.
#
# If a future table genuinely requires a custom step, add a dataclass here
# (implementing .run() and .validate()) and register it below.
#
#   - resource_grants: registered below only to declare its FK parents so
#     dangling grants (a table/package/etc. deleted without cascading to its
#     grants) are dropped-with-warning instead of aborting the copy with a
#     ForeignKeyViolation. Still a plain GenericCopyTask otherwise.
#
#   - corpus_chunks: a plain copy PLUS the stored-tsvector backfill. The
#     cutover runs ``alembic upgrade head`` on an EMPTY target first, so
#     ``0114_corpus_chunks_tsv``'s row-count gate sees nothing to backfill
#     and warns about nothing — and the copy that follows carries only the
#     DuckDB columns, so every migrated chunk would land with ``tsv`` NULL:
#     correct (the ranking falls back per row) but paying the pre-0114
#     re-tokenizing cost with no operator signal. ``CorpusChunksCopyTask``
#     runs the same bounded, batched backfill the operator script uses,
#     right after the copy.


@dataclass
class CorpusChunksCopyTask(GenericCopyTask):
    """``corpus_chunks`` copy followed by the ``tsv`` backfill (0114).

    The backfill is ``scripts.backfill_corpus_chunks_tsv.backfill`` — keyset
    batches of ``batch_size`` rows, one short transaction each, idempotent —
    so a cutover never opens one unbounded transaction over the whole
    table, and a retried cutover only fills what is still NULL. Skipped on
    ``dry_run`` like the copy itself.
    """

    def run(
        self,
        duck_conn: duckdb.DuckDBPyConnection,
        pg_engine: Engine,
        *,
        dry_run: bool = False,
    ) -> int:
        considered = super().run(duck_conn, pg_engine, dry_run=dry_run)
        if dry_run:
            return considered
        from scripts.backfill_corpus_chunks_tsv import backfill

        updated = backfill(pg_engine, batch_size=max(self.batch_size, 1))
        log.info("  corpus_chunks.tsv backfilled for %d migrated row(s)", updated)
        return considered


EXPLICIT_TASKS: Dict[str, GenericCopyTask] = {
    "corpus_chunks": CorpusChunksCopyTask(table_name="corpus_chunks", pk_columns=["id"]),
    "resource_grants": GenericCopyTask(
        table_name="resource_grants",
        pk_columns=["id"],
        fk_parents={
            "group_id": ("user_groups", "id"),
            "resource_id_table": ("table_registry", "id"),
            "resource_id_data_package": ("data_packages", "id"),
            "resource_id_memory_domain": ("memory_domains", "id"),
            "resource_id_memory_item": ("knowledge_items", "id"),
            "resource_id_recipe": ("recipes", "id"),
        },
    ),
}
