"""Keboola extractor — produces extract.duckdb + data/*.parquet using DuckDB Keboola extension."""

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from src.duckdb_conn import _open_duckdb
from src.identifier_validation import (
    is_safe_quoted_identifier,
    validate_identifier,
    validate_quoted_identifier,
)
from src.parquet_publish import atomic_publish, atomic_publish_finalize, atomic_publish_temp_path
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)


# Cap DuckDB memory_limit + thread count on short-lived consolidation
# connections inside ``materialize_query``. DuckDB's default
# ``memory_limit`` is 80% of system RAM, which on a 4 GiB cgroup
# container resolves to ~3.2 GiB of process-resident buffer pool. With
# Python objects + a few hundred MiB for orchestrator state + caddy /
# scheduler sidecars, that exceeds the cgroup cap and triggers OOM-kill
# during slice consolidation against any non-trivial table (observed
# on a 4 GiB dev container against a multi-GiB Keboola table: anon RSS
# climbed from ~350 MiB to ~3.5 GiB in minutes, then SIGKILL).
#
# 2 GiB strikes the balance: the parquet path's streaming row-group
# COPY rarely needs more than ~100 MiB, but the legacy CSV path's
# ``read_csv(max_line_size=64MB)`` pre-allocates a multi-thread
# sliding window buffer that DuckDB internally treats as a single
# ~1 GiB allocation unit (verified empirically — a 1 GiB cap raised
# `OutOfMemoryException` on a 2-row CSV fixture). Combined with
# ``preserve_insertion_order=false`` the peak stays well under 2 GiB.
# On a 4 GiB cgroup container that leaves ~1.5 GiB for Python objects
# + sidecars; on the 8 GiB target sizing the headroom is generous.
_CONSOLIDATION_MEMORY_LIMIT = "2GB"
_CONSOLIDATION_THREADS = 2

# Cap on the DuckDB spill directory (`temp_directory` below). Mirrors
# `_DUCKDB_MAX_TEMP_DIR_SIZE` in `src/db.py`; DuckDB's own default is
# "90% of available disk space", which lets one runaway consolidation
# fill the whole volume before it fails.
_CONSOLIDATION_MAX_TEMP_DIR_SIZE = "10GB"

# Bound the parquet *writer's* row-group buffer.
#
# DuckDB buffers an entire row group in memory before flushing it, and the
# default (122,880 rows) is sized for narrow rows. A table whose cells hold
# whole documents — e.g. a conversation-log export at ~16 KiB/row — needs
# well past 2 GiB for a single group, so the COPY raises
# `OutOfMemoryException` mid-write no matter how well the *scan* side
# streams. This is what the `_CONSOLIDATION_MEMORY_LIMIT` comment above did
# not account for: the cap bounds the buffer pool, not the writer's
# pre-flush accumulation, so raising the cap only moves the cliff.
#
# Reproduced on a 16-slice / 400k-row / 16 KiB-per-cell fixture: the default
# row group raises `failed to allocate data of size 416.0 MiB (1.4 GiB/1.8
# GiB used)` after ~4 s, while a 128 MiB bound completes the same COPY in
# ~1.2 s inside the same cap. Sliced-parquet consolidation, the CSV→parquet
# path, and the typed retype all write through this same writer.
#
# Two knobs exist and both are needed, because they are not interchangeable:
#   * `ROW_GROUP_SIZE_BYTES` — width-adaptive and the better default, but
#     DuckDB rejects it outright while `preserve_insertion_order=true`
#     ("ROW_GROUP_SIZE_BYTES does not work while preserving insertion
#     order"). Used by the two order-agnostic consolidation COPYs.
#   * `ROW_GROUP_SIZE` (row count) — valid in both modes. The retype COPY
#     keeps insertion order for MD5 stability, so it derives an equivalent
#     row count from the source footer via `_row_group_rows_for`.
_ROW_GROUP_TARGET_BYTES = 128 * 1024 * 1024
_ROW_GROUP_TARGET_BYTES_SQL = "128MB"
# Floor: DuckDB flushes on data-chunk boundaries (STANDARD_VECTOR_SIZE, 2048
# rows), so a smaller row count is silently ignored rather than honoured —
# verified, a derived 15 left a 400-row file as a single row group. A table
# whose individual cells are so large that even 2048 rows overshoot the target
# cannot be bounded by this knob at all; it is the memory_limit's problem.
# Ceiling: DuckDB's own default — widening it would regress scan efficiency
# for the narrow tables this must stay neutral for.
_ROW_GROUP_MIN_ROWS = 2_048
_ROW_GROUP_MAX_ROWS = 122_880


def _row_group_rows_for(parquet_path) -> int:
    """Rows per row group so one group holds ~``_ROW_GROUP_TARGET_BYTES`` of
    *uncompressed* data, derived from ``parquet_path``'s footer statistics.

    For the order-preserving retype COPY, which cannot use the byte-denominated
    `ROW_GROUP_SIZE_BYTES`. Reads only the parquet footer, never row data.

    Degrades to DuckDB's default row count whenever the footer can't answer
    (unreadable file, zero rows, missing size statistics) — the caller is a
    memory guardrail, not a correctness step, so a bad estimate must not fail
    the retype.
    """
    try:
        import pyarrow.parquet as pq

        md = pq.read_metadata(str(parquet_path))
        rows = md.num_rows
        if not rows:
            return _ROW_GROUP_MAX_ROWS
        uncompressed = 0
        for g in range(md.num_row_groups):
            group = md.row_group(g)
            for c in range(group.num_columns):
                uncompressed += group.column(c).total_uncompressed_size
        if uncompressed <= 0:
            return _ROW_GROUP_MAX_ROWS
        avg_row_bytes = max(1, uncompressed // rows)
        target = _ROW_GROUP_TARGET_BYTES // avg_row_bytes
        return max(_ROW_GROUP_MIN_ROWS, min(_ROW_GROUP_MAX_ROWS, int(target)))
    except Exception:  # pragma: no cover - guardrail must never fail the copy
        logger.debug("row-group sizing fell back to default", exc_info=True)
        return _ROW_GROUP_MAX_ROWS


def _open_consolidation_conn(db_path: Optional[str] = None):
    """Return a DuckDB connection with memory/thread caps applied.

    Use for short-lived ``COPY (SELECT … FROM read_parquet/read_csv)``
    consolidation queries inside the materialize path. ``db_path``
    defaults to in-memory; pass a path for on-disk consolidation.

    ``preserve_insertion_order=false`` is set alongside the memory cap
    on DuckDB's recommendation (the OOM error message itself proposes
    it) — for the CSV→parquet path, the default
    insertion-order-preserving execution holds row batches in memory
    until the parent operator can emit them in order, which collides
    badly with a 2 GiB cap when ``read_csv(max_line_size=64MB)``
    pre-allocates large sliding-window buffers. The materialize output
    is a single parquet that downstream consumers re-sort however they
    like — preserving the read-order from the input file isn't
    something any caller depends on.

    Spill (`temp_directory`) is pinned to the same root the Storage API
    slice downloads already use (``AGNES_TEMP_DIR`` via ``get_temp_root``).
    Left unset, DuckDB spills to ``.tmp`` *relative to the process cwd* —
    ``/app`` in the shipped image, i.e. the container's overlay on the boot
    disk. Measured on a live instance: one consolidation transiently held
    7.6 GB of spill there (boot disk 26% → 53% and back), on a volume no
    watchdog tracks. Best-effort: a failing PRAGMA leaves the connection
    usable on DuckDB's defaults rather than failing the sync.

    The spill dir is **private to this connection** —
    ``{temp_root}/kbc-spill-{pid}-{uuid}``, never a shared one — because
    DuckDB's spill filenames carry no process identity
    (``duckdb_temp_storage_DEFAULT-0.tmp``: size class + index) and a closing
    instance deletes every ``duckdb_temp_storage_*`` in its temp dir, not just
    its own. Two instances sharing a directory therefore clobber each other,
    and consolidation connections do run concurrently (api / worker /
    sync-subprocess roles share the data volume). For the same reason the
    spill must not be pointed at ``{STATE_DIR}/duckdb-tmp``, which the
    app's own DuckDB connections use (``src/db.py:_apply_memory_caps``).
    Left uncreated on purpose: DuckDB creates the directory on the first
    spill and removes it — spill files included — when the connection closes,
    so a connection that never spills leaves nothing behind, and the only
    survivors are hard-kill orphans, which
    ``storage_api.sweep_orphaned_scratch`` reclaims via the shared
    ``kbc-`` scratch prefixes.
    """
    conn = _open_duckdb(db_path) if db_path else _open_duckdb(":memory:")
    conn.execute(f"SET memory_limit='{_CONSOLIDATION_MEMORY_LIMIT}'")
    conn.execute(f"SET threads={_CONSOLIDATION_THREADS}")
    conn.execute("SET preserve_insertion_order=false")
    try:
        from connectors.keboola.storage_api import SPILL_DIR_PREFIX, get_temp_root

        temp_root = get_temp_root()
        if temp_root:
            spill = Path(temp_root) / f"{SPILL_DIR_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:12]}"
            conn.execute(f"SET temp_directory='{str(spill).replace(chr(39), chr(39) * 2)}'")
        conn.execute(f"SET max_temp_directory_size='{_CONSOLIDATION_MAX_TEMP_DIR_SIZE}'")
    except Exception as e:
        logger.debug("consolidation temp_directory spill setup failed (%s)", e)
    return conn


def _duckdb_type_for(pa_type) -> Optional[str]:
    """Map a pyarrow target type to the DuckDB type name used in the
    streaming retype cast, or None when no cast is needed/possible
    (string targets keep Storage API's native VARCHAR; unmapped types
    keep the native column untouched)."""
    import pyarrow as pa

    if pa.types.is_string(pa_type) or pa.types.is_large_string(pa_type):
        return None
    if pa.types.is_int8(pa_type):
        return "TINYINT"
    if pa.types.is_int16(pa_type):
        return "SMALLINT"
    if pa.types.is_int32(pa_type):
        return "INTEGER"
    if pa.types.is_integer(pa_type):
        return "BIGINT"
    if pa.types.is_float32(pa_type):
        return "FLOAT"
    if pa.types.is_floating(pa_type):
        return "DOUBLE"
    if pa.types.is_boolean(pa_type):
        return "BOOLEAN"
    if pa.types.is_date(pa_type):
        return "DATE"
    if pa.types.is_timestamp(pa_type):
        return "TIMESTAMPTZ" if pa_type.tz else "TIMESTAMP"
    return None


def _warn_on_coerced_nulls(src_parquet: Path, typed_parquet: str, cast_columns) -> None:
    """Log a warning per retyped column whose NULL count grew — those are
    values TRY_CAST coerced to NULL (mirrors the old pandas-fallback
    ``errors='coerce'`` warnings). Reads only parquet footers, never data;
    best-effort — footer stats may be absent, and logging must never fail
    the retype."""
    try:
        import pyarrow.parquet as pq

        def null_counts(path: str) -> Dict[str, Optional[int]]:
            md = pq.read_metadata(path)
            counts: Dict[str, Optional[int]] = {}
            for rg in range(md.num_row_groups):
                group = md.row_group(rg)
                for ci in range(group.num_columns):
                    col = group.column(ci)
                    stats = col.statistics
                    name = col.path_in_schema
                    if stats is None or not stats.has_null_count or counts.get(name, 0) is None:
                        counts[name] = None
                    else:
                        counts[name] = counts.get(name, 0) + stats.null_count
            return counts

        before = null_counts(str(src_parquet))
        after = null_counts(typed_parquet)
        for name in cast_columns:
            if before.get(name) is None or after.get(name) is None:
                continue
            coerced = after[name] - before[name]
            if coerced > 0:
                logger.warning(
                    "Column %r: %d value(s) not castable to %s → NULL in typed parquet",
                    name,
                    coerced,
                    cast_columns[name],
                )
    except Exception:  # pragma: no cover - stats logging is best-effort
        logger.debug("Coerced-NULL stats logging skipped", exc_info=True)


def _retype_parquet_streaming(tmp_parquet: Path, target_schema) -> None:
    """Retype ``tmp_parquet`` in place to ``target_schema`` by streaming it
    through a memory-capped DuckDB ``COPY`` — peak memory is bounded by
    ``_CONSOLIDATION_MEMORY_LIMIT`` regardless of table size, unlike the
    previous ``pq.read_table`` + ``apply_schema_to_table`` retype whose
    peak scaled 2-3× with the materialized result and OOM-killed syncs of
    large tables.

    Cast semantics match the old pyarrow/pandas mechanism where it
    matters: uncastable values (including empty strings) coerce to NULL
    (``TRY_CAST``, the pandas ``errors='coerce'`` equivalent, with a
    footer-stats warning per affected column), columns absent from
    ``target_schema`` and columns already matching keep their native
    type. Known divergences, both strictly better: DATE targets now cast
    (the pandas fallback only handled timestamp/numeric, so one bad value
    used to leave the whole column VARCHAR), and a wholesale-uncastable
    column degrades per-value instead of wholesale.

    Raises on failure — the caller degrades to the native (untyped)
    parquet. Published atomically (#1359) via `atomic_publish` — note *dest*
    here is `tmp_parquet` itself (not yet the served `parquet_path`; the
    caller commits that separately), which is a perfectly ordinary use of
    the primitive: it guarantees no reader of *dest* ever sees a torn write,
    regardless of whether *dest* happens to be a "final" path or, as here,
    the caller's own intermediate one.
    """
    import pyarrow.parquet as pq  # footer-only read, never data

    source_schema = pq.read_schema(str(tmp_parquet))
    target_types = {f.name: f.type for f in target_schema}

    casts: Dict[str, str] = {}
    for field in source_schema:
        target = target_types.get(field.name)
        if target is None or field.type == target:
            continue
        duck_type = _duckdb_type_for(target)
        if duck_type is not None:
            casts[field.name] = duck_type
    if not casts:
        return  # nothing to retype — skip the rewrite entirely

    def quoted(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    select_parts = []
    for field in source_schema:
        q = quoted(field.name)
        if field.name in casts:
            select_parts.append(f"TRY_CAST({q} AS {casts[field.name]}) AS {q}")
        else:
            select_parts.append(q)

    safe_src = str(tmp_parquet).replace("'", "''")
    with atomic_publish(tmp_parquet) as typed_tmp:
        conn = _open_consolidation_conn()
        try:
            # Override the consolidation default: the materialized parquet's
            # MD5 is the change-detection key for `agnes pull`, so the retype
            # must be byte-stable for identical input — with
            # preserve_insertion_order=false DuckDB reorders rows across row
            # groups (empirically, ~10 groups suffice). Unlike the CSV
            # consolidation path that motivated the false default, this
            # parquet→parquet projection streams fine with order preserved.
            conn.execute("SET preserve_insertion_order=true")
            try:
                # Naive strings cast to TIMESTAMPTZ should be read as UTC
                # (the old pandas path's `utc=True`). Best-effort: named-zone
                # support may need the icu extension; without it the session
                # default applies.
                conn.execute("SET TimeZone='UTC'")
            except Exception:
                pass
            # `ROW_GROUP_SIZE_BYTES` is unavailable here — DuckDB rejects it
            # whenever insertion order is preserved, which the line above
            # deliberately re-enables — so bound the writer by an equivalent
            # row count derived from the source footer instead. Without a
            # bound this COPY OOMs on exactly the tables the consolidation
            # bound above just rescued, one step later.
            row_group_rows = _row_group_rows_for(tmp_parquet)
            safe_dst = str(typed_tmp).replace("'", "''")
            conn.execute(
                f"COPY (SELECT {', '.join(select_parts)} FROM read_parquet('{safe_src}')) "
                f"TO '{safe_dst}' (FORMAT PARQUET, COMPRESSION SNAPPY, "
                f"ROW_GROUP_SIZE {row_group_rows})"
            )
        finally:
            conn.close()
        _warn_on_coerced_nulls(tmp_parquet, str(typed_tmp), casts)


def materialize_query(
    table_id: str,
    *,
    bucket: str,
    source_table: str,
    source_query: Optional[str] = None,
    storage_client=None,  # KeboolaStorageClient (avoid circular import)
    keboola_url: Optional[str] = None,
    keboola_token: Optional[str] = None,
    output_dir: Path,
) -> dict:
    """Materialize a Keboola Storage table to a local parquet via Storage API.

    Replaces the previous DuckDB-extension path. The extension's QueryService
    scan is unreliable on linked-bucket projects (keboola/duckdb-extension#17;
    fix shipped upstream as v0.1.6 but not yet in the community CDN, and on
    flag-restricted projects the pre-fix workspace role wouldn't have GRANTs
    on the bucket schema anyway). The Storage API export-async path always
    works regardless of project flags.

    Parallel of `connectors/bigquery/extractor.py:materialize_query` in
    surface — same return shape, same atomic write, same MD5 contract — but
    the inputs differ because Keboola's structured filter spec replaces
    BQ's free-form SQL.

    Args:
        table_id: parquet filename + sync_state key (must be a safe ident).
        bucket: Keboola bucket id, e.g. ``in.c-crm``.
        source_table: table id within the bucket, e.g. ``orders``.
        source_query: optional JSON string with a Storage API filter spec
            (see `storage_api.ExportFilter`). Empty / NULL = full table.
        storage_client: pre-built `KeboolaStorageClient` (preferred — lets
            sync.py share one across rows). When omitted, ``keboola_url``
            and ``keboola_token`` are used to construct a one-shot client.
        keboola_url, keboola_token: alternative to ``storage_client`` for
            single-call usage (tests, ad-hoc).
        output_dir: directory to write `<table_id>.parquet`.

    Returns:
        ``{"table_id", "path", "rows", "bytes", "md5"}`` — same shape the
        BQ branch returns, so ``app/api/sync.py:_run_materialized_pass``
        downstream code stays uniform.
    """
    import hashlib
    import json
    import re

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_id):
        raise ValueError(f"unsafe table_id for materialize: {table_id!r}")

    # Lazy import to avoid pulling `requests` at module import time when only
    # the sync trigger imports `extractor` for `run()`.
    from connectors.keboola.storage_api import (
        FILE_TYPE_PARQUET,
        ExportFilter,
        KeboolaStorageClient,
        normalize_source_table,
    )

    if storage_client is None:
        if not (keboola_url and keboola_token):
            raise ValueError("materialize_query requires either storage_client or (keboola_url + keboola_token)")
        storage_client = KeboolaStorageClient(url=keboola_url, token=keboola_token)

    # Heal rows whose source_table carries the bucket prefix (written by the
    # pre-fix Data-sources wizard) — composing the export id below would
    # otherwise double the bucket and 404 on a nonexistent table id.
    bare_source_table = normalize_source_table(bucket, source_table)
    if bare_source_table != source_table:
        logger.info(
            "materialize %s: source_table %r carried the bucket prefix; using %r",
            table_id,
            source_table,
            bare_source_table,
        )
        source_table = bare_source_table

    # Filter spec is optional. Admin can register a row with no
    # source_query at all (= full-table export), or with a JSON object
    # describing whereFilters / columns / changedSince / file_type.
    payload: dict = {}
    if source_query:
        _sq = source_query.strip()
        if _sq.upper().startswith(("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")):
            raise ValueError(
                f"source_query for {table_id} contains SQL, but Keboola "
                f"materialized tables use a JSON filter spec (or null for "
                f"full-table export). Set query_mode='local' for DuckDB-SQL "
                f"Keboola pulls, or clear source_query for a full-table export."
            )
        try:
            payload = json.loads(_sq)
        except json.JSONDecodeError as e:
            raise ValueError(f"source_query for {table_id} is not valid JSON: {e}") from e
    export_filter = ExportFilter.from_dict(payload)

    # Resolve date placeholders ({{last_6_months}}, {{today}}, …) in the
    # where_filters values, mirroring the local/legacy extract path
    # (`run()` calls `resolve_placeholders(parse_filters(...))`). The Storage
    # API receives literal dates; an unresolved `{{last_6_months}}` would be
    # compared verbatim and silently return 0 rows. Materialized rows
    # previously skipped this step, so placeholders only worked on
    # `query_mode='local'` rows — this aligns the materialized path.
    if export_filter.where_filters:
        from connectors.keboola.where_filters import (
            InvalidFilterError,
            parse_filters,
            resolve_placeholders,
        )

        try:
            export_filter.where_filters = resolve_placeholders(
                parse_filters(export_filter.where_filters),
                datetime.now(timezone.utc),
            )
        except InvalidFilterError as e:
            raise ValueError(f"source_query for {table_id} has an invalid where_filters placeholder: {e}") from e

    # Default the materialized path to parquet — Storage API serves it
    # via native Snowflake UNLOAD, the extractor renames it into place,
    # no CSV intermediate, no DuckDB COPY, no peak-memory load. Admin
    # can pin `{"file_type":"csv"}` in source_query to fall back (legacy
    # debugging, or projects whose backend can't UNLOAD parquet — none
    # known today, but the escape hatch costs nothing). Only override
    # when the admin spec didn't *explicitly* set a file_type.
    if "file_type" not in payload and "fileType" not in payload:
        export_filter.file_type = FILE_TYPE_PARQUET

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = output_dir / f"{table_id}.parquet"
    # Published atomically (#1359) via `atomic_publish_temp_path` +
    # `atomic_publish_finalize` — the manual two-step form, since this
    # function's several branches (slice consolidation, empty placeholder,
    # CSV path, one retry) all write into the same temp before the single
    # commit at the end, too spread out to nest inside `atomic_publish`'s
    # `with` block. Per-process naming replaces the previous shared
    # `<id>.parquet.tmp` name, which raced two writers (#1274); the final
    # commit chmods 0644, which a restrictive umask needs (#203).
    tmp_parquet = atomic_publish_temp_path(parquet_path)

    # Per-call temp dir for the intermediate file (CSV or parquet) —
    # separates concurrent exports cleanly without the os.chdir() race
    # the kbcstorage SDK has. ``ignore_cleanup_errors=True`` keeps
    # disk-full / permission errors from masking the original
    # exception, and prevents a half-cleaned dir from sitting around
    # forever (a 12 GiB stale slice tree was seen after a worker died
    # mid-write on a saturated boot disk). ``dir=get_temp_root()``
    # routes to ``AGNES_TEMP_DIR`` when the operator has steered
    # tempfiles off the overlayfs (e.g. onto the data disk) — see
    # storage_api.get_temp_root for the rationale.
    import tempfile

    from connectors.keboola.storage_api import get_temp_root, warn_if_scratch_survived

    _tmp_ctx = tempfile.TemporaryDirectory(
        prefix=f"kbc-export-{table_id}-",
        dir=get_temp_root(),
        ignore_cleanup_errors=True,
    )
    try:
        with _tmp_ctx as tmpdir:
            full_table_id = f"{bucket}.{source_table}"

            if export_filter.file_type == FILE_TYPE_PARQUET:
                # Native parquet path. Storage API serves Snowflake UNLOAD
                # output directly. Two shapes to handle:
                #
                # 1. **Single file** (small exports): file_info.url points at
                #    one signed URL; download to tmp_parquet and we're done.
                # 2. **Sliced** (large exports — Snowflake UNLOAD respects
                #    MAX_FILE_SIZE, default 16 MiB, so anything past that
                #    arrives as a manifest of N parquet slices). Each slice
                #    is itself a complete parquet file with its own footer;
                #    naively concatenating them like CSV would be invalid.
                #    We download all slices into the per-call tempdir, then
                #    DuckDB-COPY across `read_parquet([slice1, slice2, ...])`
                #    into one consolidated tmp_parquet. The scan side streams
                #    row groups, but the *writer* accumulates a whole output
                #    row group before flushing, and "one row group" is only
                #    ~1 MiB for narrow rows — on a document-shaped table it is
                #    gigabytes, which is what used to OOM this COPY. Hence the
                #    explicit `ROW_GROUP_SIZE_BYTES` bound below; see
                #    `_ROW_GROUP_TARGET_BYTES`.
                stats = storage_client.prepare_export(
                    full_table_id,
                    export_filter=export_filter,
                )
                file_info = stats["file_info"]
                if file_info.get("isSliced"):
                    slice_dir = Path(tmpdir) / "slices"
                    slice_paths = storage_client.download_file_slices(file_info, slice_dir)
                    if not slice_paths:
                        raise RuntimeError(f"sliced parquet export for {full_table_id} yielded no slices")
                    quoted = ", ".join("'" + str(p).replace("'", "''") + "'" for p in slice_paths)
                    safe_tmp = str(tmp_parquet).replace("'", "''")
                    conv = _open_consolidation_conn()
                    try:
                        conv.execute(
                            f"COPY (SELECT * FROM read_parquet([{quoted}])) TO '{safe_tmp}' "
                            f"(FORMAT PARQUET, ROW_GROUP_SIZE_BYTES '{_ROW_GROUP_TARGET_BYTES_SQL}')"
                        )
                    finally:
                        conv.close()
                else:
                    storage_client.download_file(file_info, tmp_parquet)
                    stats["bytes"] = tmp_parquet.stat().st_size if tmp_parquet.exists() else 0

                if not tmp_parquet.exists() or tmp_parquet.stat().st_size == 0:
                    logger.warning(
                        "Storage API parquet export for %s returned no data (filter may be too restrictive)",
                        full_table_id,
                    )
                    # Empty placeholder parquet so the orchestrator doesn't
                    # choke on a missing file.
                    _open_consolidation_conn().execute(
                        f"COPY (SELECT 1 AS _empty WHERE FALSE) TO '{tmp_parquet}' (FORMAT PARQUET)"
                    ).close()
                else:
                    # Typed-parquet fix for the native-parquet path (verified
                    # live, 2026-07-15): Storage API's Snowflake UNLOAD serves
                    # every column as VARCHAR regardless of the source's real
                    # type — a genuinely numeric column (e.g. a revenue metric
                    # aggregated over it) came back as a DuckDB VARCHAR,
                    # requiring callers to TRY_CAST before aggregating. The
                    # legacy CSV extraction path (_extract_via_legacy) already
                    # fixes the equivalent problem via
                    # KeboolaClient.get_pyarrow_schema() + apply_schema_to_table
                    # (parquet_io.py, the "v27 typed-parquet fix") — that
                    # mechanism operates on a generic pyarrow.Table with no
                    # CSV-specific coupling, so it's reused here rather than
                    # reimplemented. `storage_client.base` always has the form
                    # `<url>/v2/storage` (see KeboolaStorageClient.__init__),
                    # so stripping that known suffix recovers the plain
                    # Keboola URL without depending on the keboola_url/token
                    # kwargs being set — sync.py's preferred call shape passes
                    # a pre-built storage_client and may leave those unset.
                    #
                    # `isinstance(..., str)` guards against a test double /
                    # unusual caller whose `.token`/`.base` aren't real strings
                    # (e.g. a MagicMock, which is truthy but not a str) — skip
                    # typing rather than attempt a KeboolaClient call with
                    # garbage credentials. Real `KeboolaStorageClient` instances
                    # always have string `.token`/`.base`, so this is a no-op
                    # in production.
                    pyarrow_schema = None
                    if isinstance(getattr(storage_client, "token", None), str) and isinstance(
                        getattr(storage_client, "base", None), str
                    ):
                        from connectors.keboola.client import KeboolaClient

                        try:
                            metadata_client = KeboolaClient(
                                token=storage_client.token,
                                url=storage_client.base.removesuffix("/v2/storage"),
                            )
                            pyarrow_schema = metadata_client.get_pyarrow_schema(full_table_id)
                        except Exception as e:
                            logger.warning(
                                "Keboola schema unavailable for %s (%s); materialized parquet "
                                "keeps Storage API's native (often all-VARCHAR) types",
                                full_table_id,
                                e,
                            )
                            pyarrow_schema = None

                    if pyarrow_schema is not None:
                        # Streaming retype (sibling temp + atomic replace inside
                        # the helper): peak memory is bounded by the consolidation
                        # cap regardless of table size. Degrade gracefully — a
                        # retype failure must not turn an otherwise-successful
                        # materialize into a hard failure; keep the native
                        # (untyped) parquet, matching the schema-fetch fallback
                        # above.
                        try:
                            _retype_parquet_streaming(tmp_parquet, pyarrow_schema)
                        except Exception as e:
                            logger.warning(
                                "Keboola typed-parquet retype failed for %s (%s); keeping native (untyped) parquet",
                                full_table_id,
                                e,
                            )
            else:
                # Legacy CSV path. Kept for the explicit `{"file_type":"csv"}`
                # opt-in. Slower (CSV parse + parquet rewrite) and
                # memory-heavier (DuckDB pulls the CSV into a buffer with
                # max_line_size headroom), but doesn't depend on Storage
                # API parquet support if a future project backend lacks it.
                csv_path = Path(tmpdir) / f"{table_id}.csv"
                stats = storage_client.export_table(
                    full_table_id,
                    csv_path,
                    export_filter=export_filter,
                )
                if not csv_path.exists() or csv_path.stat().st_size == 0:
                    logger.warning(
                        "Storage API CSV export for %s returned no data (filter may be too restrictive)",
                        full_table_id,
                    )
                    _open_consolidation_conn().execute(
                        f"COPY (SELECT 1 AS _empty WHERE FALSE) TO '{tmp_parquet}' (FORMAT PARQUET)"
                    ).close()
                else:
                    # CSV → parquet via DuckDB. `all_varchar=True` matches the
                    # legacy client's behavior — preserves the source's exact
                    # character data without DuckDB's type inference rewriting
                    # numeric-looking strings (e.g. "Non-Manager") as NULL.
                    #
                    # `max_line_size=64MB` overrides DuckDB's default 2 MB cap
                    # on any single CSV line. Keboola tables that store
                    # embedded JSON / SQL transformation bodies routinely
                    # have multi-MB cells (e.g. `kbc_component_configuration`
                    # rows ship full Snowflake transformation SQL inline as
                    # a JSON column value); the default 2 MB ceiling rejects
                    # them with `Maximum line size of 2000000 bytes
                    # exceeded`. 64 MB is generous enough to absorb any
                    # reasonable embedded blob; DuckDB allocates a single
                    # buffer of this size per worker thread.
                    #
                    # `quote='"', escape='"'` pin the RFC-4180 dialect Keboola
                    # Storage API exports use (delimiter ',', quote '"', embedded
                    # quotes doubled). Without this, DuckDB's dialect sniffer can
                    # misdetect the escape char on cells holding embedded
                    # JSON/SQL text with their own quoting, producing a false
                    # `CSV Error on Line: N` — pinning removes the guesswork.
                    safe_csv = str(csv_path).replace("'", "''")
                    safe_tmp = str(tmp_parquet).replace("'", "''")
                    conv = _open_consolidation_conn()
                    try:
                        conv.execute(
                            f"COPY (SELECT * FROM read_csv('{safe_csv}', "
                            f"all_varchar=true, max_line_size=67108864, "
                            f"quote='\"', escape='\"')) "
                            f"TO '{safe_tmp}' (FORMAT PARQUET, "
                            f"ROW_GROUP_SIZE_BYTES '{_ROW_GROUP_TARGET_BYTES_SQL}')"
                        )
                    finally:
                        conv.close()
    except BaseException:
        # Cleanup belongs on the failure path only — see
        # `src.parquet_publish`'s module docstring for why (not a `finally`,
        # `BaseException` not `Exception`). Every branch above writes only
        # into `tmp_parquet`, never `parquet_path` directly, so this is the
        # one cleanup point every failure path needs, replacing the
        # per-branch `except Exception: tmp_parquet.unlink(); raise` this
        # used to duplicate.
        tmp_parquet.unlink(missing_ok=True)
        raise
    finally:
        warn_if_scratch_survived(_tmp_ctx.name)

    # Row count from the parquet, not from `stats["rows"]` — Storage API
    # sometimes omits totalRowsCount on small results, and the parquet is
    # the authoritative count we'll be serving downstream anyway.
    safe_tmp = str(tmp_parquet).replace("'", "''")
    cnt_conn = _open_consolidation_conn()
    try:
        row_count = cnt_conn.execute(f"SELECT COUNT(*) FROM read_parquet('{safe_tmp}')").fetchone()[0]
    finally:
        cnt_conn.close()

    # Streaming MD5 — bounded memory regardless of parquet size.
    h = hashlib.md5()
    with open(tmp_parquet, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    md5 = h.hexdigest()
    size = tmp_parquet.stat().st_size

    atomic_publish_finalize(tmp_parquet, parquet_path)

    if row_count == 0:
        logger.warning(
            "Materialized Keboola export for %s wrote 0 rows — verify the filter and that the source bucket has data.",
            table_id,
        )

    # Make the parquet visible to the orchestrator's master-view rebuild. Runs
    # after the atomic publish so the view never points at a half-written file,
    # and after the extractor pass (`_run_sync` order: extractor subprocess →
    # materialized pass → rebuild), whose `_create_meta_table` DROP would
    # otherwise wipe the row we just wrote.
    _persist_materialized_inner_view(
        extract_db_path=output_dir.parent / "extract.duckdb",
        table_id=table_id,
        parquet_path=parquet_path,
        rows=row_count,
        size_bytes=size,
    )

    return {
        "table_id": table_id,
        "path": str(parquet_path),
        "rows": row_count,
        "bytes": size,
        "md5": md5,
    }


def _read_last_sync_for_tc(tc: Dict[str, Any]):
    """Resolve last_sync for an incremental/partitioned table_config.

    Two paths, in order of preference:
    1. `__last_sync__` injected by the parent server before subprocess
       spawn (see app/api/sync.py:_run_sync) — this is the canonical
       path because the subprocess holds no DuckDB lock.
    2. Direct sync_state read — only safe in same-process callers
       (tests, in-process sync). The subprocess path errors out with
       "Conflicting lock is held" because the web server keeps an
       open write handle on system.duckdb.

    Returns None when no prior sync (treat error state as never-synced
    so the next attempt redownloads from max_history_days, not a stale
    watermark from a half-finished run).

    Tests stub this directly (`monkeypatch.setattr(extractor, "_read_last_sync_for_tc", ...)`).
    Pre-v26 fallback name `_read_last_sync` retained for monkeypatching tests.
    """
    injected = tc.get("__last_sync__")
    if injected is not None:
        if isinstance(injected, str):
            from datetime import datetime

            try:
                return datetime.fromisoformat(injected)
            except ValueError:
                return None
        return injected

    # Direct DB read — same-process fallback only. Routed through the
    # backend-aware factory (src.repositories.sync_state_repo) so a
    # Postgres-backed instance reads the real sync_state row instead of an
    # always-empty DuckDB one (get_system_db() is DuckDB-only).
    try:
        from src.repositories import sync_state_repo

        repo = sync_state_repo()
        state = repo.get_table_state(tc.get("id") or tc.get("name"))
        if not state or state.get("status") == "error":
            return None
        return state.get("last_sync")
    except Exception as e:
        logger.warning(
            "_read_last_sync_for_tc fallback failed for %s (%s); treating as first sync",
            tc.get("id") or tc.get("name"),
            e,
        )
        return None


# Back-compat alias for tests written against the pre-v26 name.
def _read_last_sync(table_id: str):
    return _read_last_sync_for_tc({"id": table_id})


def _ensure_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotent variant of :func:`_create_meta_table` — creates ``_meta`` if
    absent and leaves existing rows alone.

    :func:`_create_meta_table` DROPs first, which is right for the extractor
    pass (it rewrites the whole extract) but wrong for the materialize path,
    which touches exactly one table's row in an extract other rows may already
    own.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS _meta (
        table_name VARCHAR NOT NULL,
        description VARCHAR,
        rows BIGINT,
        size_bytes BIGINT,
        extracted_at TIMESTAMP,
        query_mode VARCHAR DEFAULT 'local'
    )""")


def _persist_materialized_inner_view(
    extract_db_path: Path,
    table_id: str,
    parquet_path: Path,
    rows: int,
    size_bytes: int,
) -> None:
    """Register a materialized parquet in ``extract.duckdb`` as a ``_meta`` row
    plus an inner view, so ``SyncOrchestrator.rebuild()`` creates the master
    view for it.

    Without this the parquet lands on disk and ``sync_state`` reports ``ok``
    with a row count, but the orchestrator — which only ever walks ``_meta`` —
    never creates a view, so every read 400s with "registered as
    query_mode='materialized' but is not yet materialized in this instance's
    analytics views". On an instance whose Keboola rows are ALL materialized
    there was no ``extract.duckdb`` at all, so the orchestrator skipped the
    whole source with a debug-level "no extract.duckdb" and nothing surfaced
    in the operator's log.

    Parallel of ``connectors/bigquery/extractor.py::_persist_materialized_inner_view``
    and the Snowflake/Databricks equivalents, with one deliberate difference:
    it CREATES ``extract.duckdb`` when absent instead of skipping. BigQuery can
    assume the extractor subprocess made the file (a BQ instance always has
    remote rows to write); a materialized-only Keboola source has nothing else
    that would ever create it.

    Idempotent: the table's own ``_meta`` row is replaced (the table carries no
    UNIQUE on ``table_name``) and its inner view recreated; other rows are left
    untouched. Fail-soft — the parquet is the canonical artifact, so a
    registration failure (lock contention, schema drift) is logged and the next
    pass gets another chance.

    How long the registration lives depends on whether the source also has
    ``query_mode='local'`` rows, and it is worth being precise about it:

    - **Materialized-only source** (the case this fixes): nothing else ever
      writes this file, so the row and view persist until the next materialize
      replaces them.
    - **Mixed local + materialized source**: :func:`run` rebuilds
      ``extract.duckdb`` from scratch on every extractor pass — it writes a
      fresh ``extract.duckdb.tmp`` and ``shutil.move``s it over the old file —
      so a materialized row registered on an earlier tick is wiped by any later
      pass on which that table is not itself due. In the same tick it is
      harmless (the materialized pass runs after the subprocess and re-registers
      what it just published); across ticks the master view is carried by the
      orchestrator's pre-existing filesystem-fallback pass, which recreates it
      from ``data/*.parquet`` when a registered materialized row has no ``_meta``
      entry, until the next materialize restores this one. Making the
      registration survive that rebuild means teaching :func:`run` to preserve
      foreign ``_meta`` rows, which is a change to the whole-file swap and not
      this fix's scope.
    """
    safe_path = str(parquet_path).replace("'", "''")
    try:
        extract_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open_duckdb(str(extract_db_path), read_only=False)
        try:
            _ensure_meta_table(conn)
            # Wrapped so a concurrent reader of `_meta` sees either the old row
            # or the new one, never both / neither.
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM _meta WHERE table_name = ?", [table_id])
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, 'materialized')",
                    [table_id, "", rows, size_bytes],
                )
                conn.execute(
                    f"CREATE OR REPLACE VIEW {quote_ident(table_id)} AS SELECT * FROM read_parquet('{safe_path}')"
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(
            "materialize %s: could not register _meta/inner view in %s (%s) — the parquet is "
            "published, but the master view will be missing until the next pass",
            table_id,
            extract_db_path,
            exc,
        )


def _create_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the _meta table required by the extract.duckdb contract."""
    conn.execute("DROP TABLE IF EXISTS _meta")
    conn.execute("""CREATE TABLE _meta (
        table_name VARCHAR NOT NULL,
        description VARCHAR,
        rows BIGINT,
        size_bytes BIGINT,
        extracted_at TIMESTAMP,
        query_mode VARCHAR DEFAULT 'local'
    )""")


def _create_remote_attach_table(conn: duckdb.DuckDBPyConnection, keboola_url: str) -> None:
    """Write _remote_attach so orchestrator can re-ATTACH the Keboola extension."""
    conn.execute("DROP TABLE IF EXISTS _remote_attach")
    conn.execute("""CREATE TABLE _remote_attach (
        alias VARCHAR,
        extension VARCHAR,
        url VARCHAR,
        token_env VARCHAR
    )""")
    conn.execute(
        "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
        ["kbc", "keboola", keboola_url, "KEBOOLA_STORAGE_TOKEN"],
    )


def _ensure_remote_attach_row(conn: duckdb.DuckDBPyConnection, keboola_url: str) -> None:
    """Merge-mode variant of :func:`_create_remote_attach_table` — creates the
    table if absent and inserts the ``kbc`` alias row only when no row already
    claims that alias.

    First writer wins: ``_run_sync`` dispatches the global (``connection_id
    IS NULL``) credential group first, so when both the global project and a
    named connection carry ``remote`` rows, the alias keeps pointing at the
    global stack — matching how the orchestrator resolves the re-ATTACH token
    (``token_env='KEBOOLA_STORAGE_TOKEN'``, the global env credential). A
    DROP-and-recreate here would silently repoint every remote view of the
    pass at the LAST group's stack URL.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS _remote_attach (
        alias VARCHAR,
        extension VARCHAR,
        url VARCHAR,
        token_env VARCHAR
    )""")
    existing = conn.execute("SELECT count(*) FROM _remote_attach WHERE alias = 'kbc'").fetchone()[0]
    if not existing:
        conn.execute(
            "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
            ["kbc", "keboola", keboola_url, "KEBOOLA_STORAGE_TOKEN"],
        )


def _try_attach_extension(conn: duckdb.DuckDBPyConnection, keboola_url: str, keboola_token: str) -> bool:
    """Try to install and attach the Keboola DuckDB extension. Returns True on success."""
    try:
        conn.execute("INSTALL keboola FROM community; LOAD keboola;")
        escaped_token = keboola_token.replace("'", "''")
        # Strip trailing slash — the Keboola DuckDB extension's ATTACH fails
        # with a network error when the URL ends in `/` (e.g. the canonical
        # `https://connection.us-east4.gcp.keboola.com/` form). Bare host
        # works.
        attach_url = keboola_url.rstrip("/")
        conn.execute(f"ATTACH '{attach_url}' AS kbc (TYPE keboola, TOKEN '{escaped_token}')")
        logger.info("Using DuckDB Keboola extension")
        return True
    except Exception as e:
        logger.warning("Keboola extension unavailable (%s), falling back to legacy client", e)
        return False


def run(
    output_dir: str,
    table_configs: List[Dict[str, Any]],
    keboola_url: str,
    keboola_token: str,
    merge: bool = False,
) -> Dict[str, Any]:
    """Extract tables from Keboola into output_dir using DuckDB extension.

    Args:
        output_dir: Path to write extract.duckdb + data/
        table_configs: List of table config dicts from table_registry
        keboola_url: Keboola stack URL
        keboola_token: Keboola Storage API token
        merge: When False (default), the produced ``extract.duckdb`` is
            rebuilt from scratch and contains ONLY this call's tables —
            the historical whole-pass semantics, whose implicit prune is
            how deleted/renamed registry rows disappear. When True, the
            temp build is seeded from the CURRENT ``extract.duckdb`` so
            this call only replaces its own tables' ``_meta`` rows and
            views, preserving every other table already in the extract.
            ``app.api.sync._run_sync`` dispatches one ``run()`` per
            ``connection_id`` credential group (#B2); the first group of
            a pass runs ``merge=False`` and every later group
            ``merge=True`` — without this, each group's atomic
            tmp-then-move swap clobbered the previous group's extract and
            only the LAST connection's tables survived the pass.

    Returns:
        Dict with extraction stats: {tables_extracted: int, tables_failed: int, errors: list}
    """
    import shutil

    output_path = Path(output_dir)
    data_dir = output_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Write to temp file then rename — avoids lock conflict with orchestrator
    # which may hold a read lock on the existing extract.duckdb
    db_path = output_path / "extract.duckdb"
    tmp_db_path = output_path / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()
    if merge and db_path.exists():
        # Seed the temp build with the extract as it stands (a previous
        # credential group's output in the same sync pass) so the swap
        # below replaces rather than discards it. Copy, never open the
        # live file for write — the orchestrator may hold a read ATTACH.
        shutil.copy2(str(db_path), str(tmp_db_path))
    conn = _open_duckdb(str(tmp_db_path))

    stats = {"tables_extracted": 0, "tables_failed": 0, "errors": []}
    now = datetime.now(timezone.utc)

    # Per-table workitems whose extension scan failed and need the legacy
    # Storage-API fallback. Drained in a parallel pool below the per-table
    # serial loop. Items are `(tc, pq_path)` tuples.
    legacy_queue: List[tuple] = []

    try:
        # Try DuckDB Keboola extension
        use_extension = _try_attach_extension(conn, keboola_url, keboola_token)

        if merge:
            # Merge mode: keep foreign `_meta` rows (previous credential
            # groups of this pass); replace only this call's own rows so a
            # re-extracted table never duplicates. Views use CREATE OR
            # REPLACE below, so no separate view cleanup is needed.
            _ensure_meta_table(conn)
            for tc in table_configs:
                conn.execute("DELETE FROM _meta WHERE table_name = ?", [tc["name"]])
        else:
            _create_meta_table(conn)

        has_remote = any(tc.get("query_mode") == "remote" for tc in table_configs)
        if has_remote and use_extension:
            if merge:
                _ensure_remote_attach_row(conn, keboola_url)
            else:
                _create_remote_attach_table(conn, keboola_url)

        for tc in table_configs:
            table_name = tc["name"]
            query_mode = tc.get("query_mode", "local")

            # Materialized rows are written by the sync trigger pass via
            # `materialize_query()` — they live as parquets in
            # /data/extracts/keboola/data/, picked up by the orchestrator's
            # standard local-parquet discovery. Don't extract here (would
            # double-write data via the source bucket reference and confuse
            # sync_state bookkeeping). Mirror of the BQ extractor's skip at
            # connectors/bigquery/extractor.py:190.
            if query_mode == "materialized":
                logger.info(
                    "Skipping legacy extract for %s — query_mode='materialized', "
                    "handled by _run_materialized_pass instead",
                    tc.get("id") or tc.get("name"),
                )
                continue

            # #81 Group D — refuse rows whose identifiers don't pass the
            # whitelist. The registry is admin-controlled but anyone with
            # write access can otherwise inject SQL via the CREATE VIEW /
            # COPY / SELECT interpolation below. Skip-and-continue rather
            # than crashing the whole extraction; valid rows still process.
            #
            # `table_name` is the DuckDB view name in the master
            # analytics DB. The orchestrator uses the STRICT validator
            # (`^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`) when re-creating views,
            # so any name with `-` or `.` would pass extraction here
            # but be silently dropped at orchestrator-rebuild time.
            # Use the strict validator here too so the failure is
            # caught early and visible in tables_failed.
            if not validate_identifier(table_name, "Keboola table_name"):
                stats["tables_failed"] += 1
                stats["errors"].append({"table": table_name, "error": "unsafe identifier"})
                continue

            if query_mode == "remote":
                # Create view pointing to kbc extension (requires re-ATTACH at query time)
                from connectors.keboola.storage_api import normalize_source_table

                bucket = tc.get("bucket", "")
                # A row whose source_table carries the bucket prefix (pre-fix
                # wizard registration) would build kbc."in.c-x"."in.c-x.tbl".
                # validate_quoted_identifier accepts it — dots are legal in
                # Keboola's `in.c-foo` convention — so nothing else catches it.
                source_table = normalize_source_table(bucket, tc.get("source_table", table_name))
                if not (
                    validate_quoted_identifier(bucket, "Keboola bucket")
                    and validate_quoted_identifier(source_table, "Keboola source_table")
                ):
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": table_name, "error": "unsafe bucket/source_table"})
                    continue
                if use_extension and bucket:
                    # The extension validates the referenced table eagerly at
                    # CREATE VIEW. Isolate the failure per row: unguarded, one
                    # bad row raised out of run(), skipped the atomic
                    # extract.duckdb rename, and took every other table of this
                    # source down with it — every sibling branch degrades to
                    # tables_failed and continues.
                    try:
                        conn.execute(
                            f"CREATE OR REPLACE VIEW {quote_ident(table_name)} AS SELECT * FROM kbc.{quote_ident(bucket)}.{quote_ident(source_table)}"
                        )
                    except Exception as view_err:
                        logger.warning(
                            "Remote view creation failed for %s (%s.%s): %s",
                            table_name,
                            bucket,
                            source_table,
                            view_err,
                        )
                        stats["tables_failed"] += 1
                        stats["errors"].append({"table": table_name, "error": f"remote view failed: {view_err}"})
                        continue
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, 0, 0, ?, 'remote')",
                    [table_name, tc.get("description", ""), now],
                )
                stats["tables_extracted"] += 1
                continue

            # v26 dispatcher: route per-table by sync_strategy.
            # API-layer validators reject conflicting combinations
            # (incremental + where_filters, partitioned + remote) before
            # rows reach this point — here we trust tc and dispatch.
            sync_strategy = tc.get("sync_strategy") or "full_refresh"

            # Resolve where_filters once for any strategy that supports them.
            # Storage API extension does not expose whereFilters, so any
            # filter forces the SDK path. Resolution happens here so a
            # placeholder typo surfaces as a per-table error, not silent.
            resolved_filters = None
            raw_filters = tc.get("where_filters")
            if raw_filters:
                from connectors.keboola.where_filters import (
                    InvalidFilterError,
                    parse_filters,
                    resolve_placeholders,
                )

                try:
                    resolved_filters = resolve_placeholders(
                        parse_filters(raw_filters),
                        datetime.now(timezone.utc),
                    )
                except InvalidFilterError as e:
                    logger.error("where_filters invalid for %s: %s", table_name, e)
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": table_name, "error": f"where_filters: {e}"})
                    continue

            if sync_strategy == "incremental":
                try:
                    pq_path = data_dir / f"{table_name}.parquet"
                    last_sync = _read_last_sync_for_tc(tc)
                    from connectors.keboola.incremental import extract_incremental

                    incr_result = extract_incremental(
                        table_config=tc,
                        parquet_path=pq_path,
                        last_sync=last_sync,
                        keboola_url=keboola_url,
                        keboola_token=keboola_token,
                    )
                    safe_pq_lit = str(pq_path).replace("'", "''")
                    rows = incr_result["rows"]
                    size = pq_path.stat().st_size if pq_path.exists() else 0
                    conn.execute(
                        f"CREATE OR REPLACE VIEW {quote_ident(table_name)} AS SELECT * FROM read_parquet('{safe_pq_lit}')"
                    )
                    conn.execute(
                        "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
                        [table_name, tc.get("description", ""), rows, size, now],
                    )
                    stats["tables_extracted"] += 1
                    logger.info(
                        "Incremental %s: %d rows (%d delta), changedSince=%s",
                        table_name,
                        rows,
                        incr_result["delta_rows"],
                        incr_result["changed_since_used"],
                    )
                except Exception as e:
                    logger.error("Incremental extract failed for %s: %s", table_name, e)
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": table_name, "error": str(e)})
                continue

            if sync_strategy == "partitioned":
                try:
                    partition_dir = data_dir / table_name
                    partition_dir.mkdir(exist_ok=True)
                    last_sync = _read_last_sync_for_tc(tc)
                    from connectors.keboola.partitioned import extract_partitioned

                    part_result = extract_partitioned(
                        table_config=tc,
                        output_dir=partition_dir,
                        last_sync=last_sync,
                        keboola_url=keboola_url,
                        keboola_token=keboola_token,
                    )
                    glob_lit = str(partition_dir / "*.parquet").replace("'", "''")
                    rows = part_result["rows"]
                    size = sum(p.stat().st_size for p in partition_dir.glob("*.parquet"))
                    conn.execute(
                        f"CREATE OR REPLACE VIEW {quote_ident(table_name)} AS SELECT * FROM read_parquet('{glob_lit}')"
                    )
                    conn.execute(
                        "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
                        [table_name, tc.get("description", ""), rows, size, now],
                    )
                    stats["tables_extracted"] += 1
                    logger.info(
                        "Partitioned %s: %d rows across %d partition file(s)",
                        table_name,
                        rows,
                        part_result.get("partitions_touched", part_result.get("partitions_written", 0)),
                    )
                except Exception as e:
                    logger.error("Partitioned extract failed for %s: %s", table_name, e)
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": table_name, "error": str(e)})
                continue

            # full_refresh fall-through: existing extension/legacy logic.
            # When where_filters are set we MUST force the legacy path
            # (extension lacks whereFilters support).
            try:
                pq_path = str(data_dir / f"{table_name}.parquet")

                if resolved_filters:
                    _extract_via_legacy(
                        tc,
                        pq_path,
                        keboola_url,
                        keboola_token,
                        where_filters=resolved_filters,
                    )
                elif use_extension:
                    try:
                        _extract_via_extension(conn, tc, pq_path)
                    except Exception as ext_err:
                        # ATTACH succeeded but the per-table COPY failed —
                        # most commonly a Keboola QueryService permission error
                        # (`Schema '..."in.c-..."' does not exist or not
                        # authorized`, see keboola/duckdb-extension#17). The
                        # legacy Storage-API client doesn't go through
                        # QueryService at all, so queue for the parallel
                        # legacy fallback below.
                        logger.warning(
                            "Keboola extension scan failed for %s (%s); queued for legacy Storage-API fallback",
                            table_name,
                            ext_err,
                        )
                        legacy_queue.append((tc, pq_path))
                        continue
                else:
                    legacy_queue.append((tc, pq_path))
                    continue

                # Extension path succeeded — register _meta synchronously.
                _register_local_meta(conn, tc, pq_path, now)
                stats["tables_extracted"] += 1
                rows_log = conn.execute(
                    f"SELECT count(*) FROM read_parquet('{pq_path.replace(chr(39), chr(39) * 2)}')"
                ).fetchone()[0]
                logger.info("Extracted %s via extension: %d rows", table_name, rows_log)

            except Exception as e:
                logger.error("Failed to extract %s: %s", table_name, e)
                stats["tables_failed"] += 1
                stats["errors"].append({"table": table_name, "error": str(e)})

        # Detach Keboola if extension was used
        if use_extension:
            try:
                conn.execute("DETACH kbc")
            except Exception:
                pass

        # Phase 2: legacy fallback in parallel. Keboola Storage API export
        # jobs are independent per table — a worker pool of N workers fans
        # out the per-table HTTP roundtrips (export job submit + poll +
        # CSV download) instead of stacking them sequentially. Project-level
        # concurrency is bounded by the storage.jobsParallelism limit
        # (typically 10); default to 4 to leave headroom for other clients.
        # Override via AGNES_KEBOOLA_PARALLELISM env var.
        #
        # Workers are PROCESSES, not threads — `connectors/keboola/client.py:
        # export_table` does `os.chdir(temp_dir)` to redirect kbcstorage's
        # slice-file downloads into a per-call temp directory, and `os.chdir`
        # is process-global. With threads, two parallel exports race on CWD
        # and slice files end up in the wrong directory; the merge step then
        # fails with `[Errno 2] No such file or directory:
        # '<job_id>.csv_X_Y_Z.csv'`. ProcessPoolExecutor gives each worker
        # its own process and therefore its own CWD.
        if legacy_queue:
            parallelism = max(1, int(os.environ.get("AGNES_KEBOOLA_PARALLELISM", "8")))
            workers = min(parallelism, len(legacy_queue))
            logger.info(
                "Running legacy Storage-API fallback for %d tables across %d worker processes",
                len(legacy_queue),
                workers,
            )

            if workers == 1:
                legacy_results = [_legacy_worker(item, keboola_url, keboola_token) for item in legacy_queue]
            else:
                from concurrent.futures import ProcessPoolExecutor

                with ProcessPoolExecutor(max_workers=workers) as ex:
                    futures = [ex.submit(_legacy_worker, item, keboola_url, keboola_token) for item in legacy_queue]
                    legacy_results = [f.result() for f in futures]

            # Phase 3: serial _meta insert for legacy results. DuckDB conn
            # isn't thread-safe, so we collect parallel work and only touch
            # `conn` (and `stats`) here on the main thread.
            for tc_, pq_, err in legacy_results:
                tn = tc_["name"]
                if err is not None:
                    logger.error("Failed to extract %s via legacy: %s", tn, err)
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": tn, "error": err})
                    continue
                try:
                    _register_local_meta(conn, tc_, pq_, now)
                    stats["tables_extracted"] += 1
                    rows_log = conn.execute(
                        f"SELECT count(*) FROM read_parquet('{pq_.replace(chr(39), chr(39) * 2)}')"
                    ).fetchone()[0]
                    logger.info("Extracted %s via legacy: %d rows", tn, rows_log)
                except Exception as e:
                    logger.error("Failed to register _meta for %s: %s", tn, e)
                    stats["tables_failed"] += 1
                    stats["errors"].append({"table": tn, "error": str(e)})

    finally:
        conn.execute("CHECKPOINT")
        conn.close()

    # Atomic replace: swap temp DB into place, cleaning up any WAL files
    old_wal = Path(str(db_path) + ".wal")
    if old_wal.exists():
        old_wal.unlink()

    if tmp_db_path.exists():
        shutil.move(str(tmp_db_path), str(db_path))

    tmp_wal = Path(str(tmp_db_path) + ".wal")
    if tmp_wal.exists():
        tmp_wal.unlink()

    return stats


def _register_local_meta(
    conn: duckdb.DuckDBPyConnection,
    tc: Dict[str, Any],
    pq_path: str,
    extracted_at: datetime,
) -> None:
    """After a parquet has been written for a local-mode table, create the
    DuckDB view and register the row in `_meta`. Hoisted out of the run()
    body so both the serial extension-success path and the parallel
    legacy-result path share one implementation."""
    table_name = tc["name"]
    safe_pq_lit = pq_path.replace("'", "''")
    rows = conn.execute(f"SELECT count(*) FROM read_parquet('{safe_pq_lit}')").fetchone()[0]
    size = os.path.getsize(pq_path)
    conn.execute(f"CREATE OR REPLACE VIEW {quote_ident(table_name)} AS SELECT * FROM read_parquet('{safe_pq_lit}')")
    conn.execute(
        "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
        [table_name, tc.get("description", ""), rows, size, extracted_at],
    )


def _extract_via_extension(conn: duckdb.DuckDBPyConnection, tc: Dict[str, Any], pq_path: str) -> None:
    """Extract a table using the DuckDB Keboola extension.

    Backs ``sync_strategy='full_refresh'``, the primary Keboola sync path —
    published atomically (#1359) via `src.parquet_publish.atomic_publish` so
    a reader (the orchestrator's hasher, the master views, `agnes pull`) can
    never observe a half-written ``pq_path``; a direct ``COPY ... TO
    '<pq_path>'`` here used to leave exactly that window open, on the most
    central connector, for however long the COPY takes.
    """
    from connectors.keboola.storage_api import normalize_source_table

    bucket = tc.get("bucket", "")
    # Strip a legacy bucket prefix (pre-fix wizard rows) before it becomes
    # kbc."in.c-x"."in.c-x.tbl" — same healing the legacy/materialize paths do.
    source_table = normalize_source_table(bucket, tc.get("source_table", tc["name"]))
    # #81 Group D — defense-in-depth. The caller already validates these;
    # refuse here too in case a future caller forgets. Use the relaxed
    # quoted-identifier check that accepts Keboola's `in.c-foo` form.
    if not (is_safe_quoted_identifier(bucket) and is_safe_quoted_identifier(source_table)):
        raise ValueError(f"unsafe bucket/source_table: {bucket!r}/{source_table!r}")
    # `kbc` is the ATTACH alias and stays bare; bucket/source_table are identifiers.
    with atomic_publish(Path(pq_path)) as tmp_dest:
        safe_tmp_lit = str(tmp_dest).replace("'", "''")
        conn.execute(
            f"COPY (SELECT * FROM kbc.{quote_ident(bucket)}.{quote_ident(source_table)}) "
            f"TO '{safe_tmp_lit}' (FORMAT PARQUET)"
        )


def _legacy_worker(tc_pq, keboola_url: str, keboola_token: str):
    """Module-level wrapper for ProcessPoolExecutor — must be picklable.

    Returns `(tc, pq_path, error_str_or_None)` so the main process can
    aggregate results and update _meta serially on its DuckDB connection.
    """
    tc_, pq_ = tc_pq
    try:
        _extract_via_legacy(tc_, pq_, keboola_url, keboola_token)
        return (tc_, pq_, None)
    except Exception as exc:
        return (tc_, pq_, str(exc))


def _extract_via_legacy(
    tc: Dict[str, Any],
    pq_path: str,
    keboola_url: str,
    keboola_token: str,
    where_filters: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Per-table extract via the Storage API export-async path with typed parquet.

    Despite the name (kept for caller compatibility with `_legacy_worker`),
    this no longer goes through the `kbcstorage` SDK — it talks to the
    Storage API directly via `connectors/keboola/storage_api.py`. The old
    SDK path had a thread-unsafe `os.chdir(temp_dir)` that broke parallel
    execution; the direct path uses per-call temp directories and signed-URL
    downloads, so threads / processes don't trip on each other.

    `where_filters` (v27) — when present, builds an `ExportFilter` so the
    Storage API applies the row filter server-side before signing the
    parquet/CSV file. Caller has already resolved any date placeholders
    (see `connectors/keboola/where_filters.py:resolve_placeholders`). The
    DuckDB Keboola extension does not expose whereFilters, so any v27
    filter row forces this path.

    The CSV → parquet conversion uses `connectors/keboola/parquet_io.csv_to_parquet`
    with the PyArrow schema + pandas dtypes pulled from Keboola column metadata
    (provider cascade `user > ai-metadata-enrichment > keboola.snowflake-transformation > storage`).
    Falls back to string-typed parquet only when the metadata API is unreachable.
    Pre-v27 this path used `read_csv(all_varchar=true)` which flattened every
    column to VARCHAR.
    """
    import tempfile

    from connectors.keboola.client import KeboolaClient
    from connectors.keboola.parquet_io import csv_to_parquet
    from connectors.keboola.storage_api import (
        ExportFilter,
        KeboolaStorageClient,
        get_temp_root,
        normalize_source_table,
        warn_if_scratch_survived,
    )

    bucket = tc.get("bucket", "")
    # normalize_source_table: a row whose source_table carries the bucket
    # prefix (pre-fix wizard registration) would double the bucket in the
    # export id below.
    source_table = normalize_source_table(bucket, tc.get("source_table", tc["name"]))
    table_id = f"{bucket}.{source_table}" if bucket else tc.get("id", tc["name"])

    # Pull column-level metadata for typed parquet (v27 typed-parquet fix).
    # `KeboolaClient` (kbcstorage SDK) is the only source of provider-cascade
    # PyArrow schema; storage_api.KeboolaStorageClient handles the export
    # path. Both are kept side by side intentionally.
    metadata_client = KeboolaClient(token=keboola_token, url=keboola_url)
    try:
        pyarrow_schema = metadata_client.get_pyarrow_schema(table_id)
    except Exception as e:
        logger.warning(
            "Keboola schema unavailable for %s (%s); writing string-typed parquet",
            table_id,
            e,
        )
        pyarrow_schema = None
    try:
        dtypes = metadata_client.get_pandas_dtypes(table_id) if pyarrow_schema else {}
    except Exception:
        dtypes = {}
    try:
        date_columns = metadata_client.get_date_columns(table_id) if pyarrow_schema else []
    except Exception:
        date_columns = []

    export_filter = None
    if where_filters:
        # ExportFilter validates shape (column/operator/values keys) on
        # to_export_params(); raises ValueError on malformed entries which
        # the worker wrapper turns into a per-table error.
        export_filter = ExportFilter(where_filters=list(where_filters))

    _tmp_ctx = tempfile.TemporaryDirectory(
        prefix=f"kbc-export-{tc['name']}-",
        dir=get_temp_root(),
        ignore_cleanup_errors=True,
    )
    try:
        with _tmp_ctx as tmpdir:
            csv_path = Path(tmpdir) / f"{tc['name']}.csv"
            client = KeboolaStorageClient(url=keboola_url, token=keboola_token)
            client.export_table_to_csv(table_id, csv_path, export_filter=export_filter)

            if not csv_path.exists() or csv_path.stat().st_size == 0:
                # Storage API succeeded but produced no rows. Emit an empty
                # parquet rather than crashing — same defensive behavior as
                # `materialize_query`. Published atomically (#1359) — this used
                # to COPY straight onto the live `pq_path`.
                with atomic_publish(Path(pq_path)) as tmp_dest:
                    safe_tmp_lit = str(tmp_dest).replace("'", "''")
                    _open_consolidation_conn().execute(
                        f"COPY (SELECT 1 AS _empty WHERE FALSE) TO '{safe_tmp_lit}' (FORMAT PARQUET)"
                    ).close()
                return

            # v27 typed-parquet path: use csv_to_parquet with PyArrow schema
            # from Keboola metadata. Falls through to string typing when
            # pyarrow_schema is None (metadata API unreachable).
            csv_to_parquet(
                csv_path=csv_path,
                parquet_path=Path(pq_path),
                dtypes=dtypes,
                date_columns=date_columns,
                pyarrow_schema=pyarrow_schema,
                table_id=table_id,
            )
    finally:
        warn_if_scratch_survived(_tmp_ctx.name)


def compute_exit_code(stats: Dict[str, Any], total: int) -> int:
    """Map an extraction `stats` dict to a process exit code.

    Issue #81 Group B: distinguish full success from partial failure so
    the sync API and CLI consumers can alert on partial vs. full failure
    rather than treating any non-zero as one bucket.

    - ``0`` — every table succeeded (or no tables registered).
    - ``1`` — every table failed (full failure).
    - ``2`` — at least one succeeded and at least one failed (partial).

    `total` is the count of tables the extractor was asked to process.
    `stats["tables_failed"]` is the count it actually failed.
    """
    failed = stats.get("tables_failed", 0)
    if total == 0:
        return 0
    if failed == 0:
        return 0
    if failed >= total:
        return 1
    return 2


def _registered_keboola_tables() -> List[Dict[str, Any]]:
    """Read Keboola rows from ``table_registry`` via the backend-aware factory.

    Standalone-entrypoint helper for the ``__main__`` block below — reachable
    from the ``extract`` one-shot service in ``docker-compose.yml``, which
    runs ``python -m connectors.keboola.extractor`` directly (outside the
    app's request-scoped sync path). Routes through ``table_registry_repo()``
    so a Postgres-backed instance reads the real registry instead of an
    always-empty DuckDB one (``get_system_db()`` is DuckDB-only).
    """
    from src.repositories import table_registry_repo

    return table_registry_repo().list_by_source("keboola")


if __name__ == "__main__":
    """Standalone: reads config from env + table_registry, runs extraction.

    Used by sync trigger subprocess. Reads KEBOOLA_STORAGE_TOKEN and
    KEBOOLA_STACK_URL from environment, table list from DuckDB registry.
    """
    from app.logging_config import setup_logging

    setup_logging(__name__)

    # Read Keboola credentials — env > vault > instance.yaml fallback
    url = os.environ.get("KEBOOLA_STACK_URL", "")
    from app.datasource_secrets import datasource_secret as _datasource_secret

    try:
        token = _datasource_secret("KEBOOLA_STORAGE_TOKEN") or ""
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "datasource_secret unavailable during extractor startup; falling back to env"
        )
        token = ""

    if not url or not token:
        try:
            from config.loader import load_instance_config

            config = load_instance_config()
            kbc_config = config.get("keboola", {})
            url = url or kbc_config.get("url", "")
            token_env = kbc_config.get("token_env", "KEBOOLA_STORAGE_TOKEN")
            token = token or os.environ.get(token_env, "")
        except Exception:
            pass

    if not url or not token:
        logger.error("Missing KEBOOLA_STACK_URL or KEBOOLA_STORAGE_TOKEN")
        exit(1)

    # Read table list from registry
    tables = _registered_keboola_tables()

    if not tables:
        logger.warning("No Keboola tables registered in table_registry")
        exit(0)

    logger.info("Extracting %d tables from %s", len(tables), url)
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    result = run(str(data_dir / "extracts" / "keboola"), tables, url, token)
    logger.info("Extraction complete: %s", result)

    code = compute_exit_code(result, len(tables))
    if code == 2:
        logger.error("Partial failure: %d of %d tables failed", result.get("tables_failed", 0), len(tables))
    elif code == 1:
        logger.error("All %d tables failed", len(tables))
    exit(code)
