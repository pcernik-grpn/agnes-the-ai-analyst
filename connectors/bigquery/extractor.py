"""BigQuery extractor — produces extract.duckdb with remote views via DuckDB BigQuery extension.

No data is downloaded. All queries go directly to BigQuery via DuckDB extension ATTACH.
"""

import fcntl
import hashlib
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import duckdb

from connectors.bigquery.auth import get_metadata_token, BQMetadataAuthError


def _pin_session_utc(conn):
    """Inline pin — see `src.duckdb_conn._open_duckdb` for the full
    rationale. The BQ extractor opens its connections via
    `duckdb.connect(...)` directly (not the helper) because every test
    in `tests/test_bigquery_extractor*.py` patches
    `connectors.bigquery.extractor.duckdb` to intercept calls; routing
    through a different module would bypass the patch surface.
    """
    try:
        conn.execute("SET GLOBAL TimeZone='UTC'")
    except duckdb.Error:
        pass
    return conn
from src.sql_safe import (
    validate_identifier as _validate_identifier,
    validate_project_id as _validate_project_id,
)
from src.identifier_validation import validate_identifier, validate_quoted_identifier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ``bq_metadata_cache`` stores the BQ INFORMATION_SCHEMA ``table_type`` value
# refreshed every 4 h by the scheduler job. The cache uses the BQ-canonical
# spelling ``MATERIALIZED VIEW`` (with a space), while the extractor loop
# branches on ``MATERIALIZED_VIEW`` (underscore). We normalise here so the
# existing branch logic does not change.
_MATERIALIZED_VIEW_SPACE = "MATERIALIZED VIEW"
_MATERIALIZED_VIEW_UNDER = "MATERIALIZED_VIEW"


def _normalize_entity_type(raw: str | None) -> str | None:
    """Normalise a cached entity_type value for use in the extractor loop.

    BigQuery INFORMATION_SCHEMA returns ``MATERIALIZED VIEW`` (space); the
    extractor loop branches on ``MATERIALIZED_VIEW`` (underscore). This
    function converts the cached form to the extractor's expected form so
    the VIEW branch still fires correctly. All other values pass through
    unchanged.
    """
    if raw == _MATERIALIZED_VIEW_SPACE:
        return _MATERIALIZED_VIEW_UNDER
    return raw


def bq_metadata_cache_repo():
    """Thin shim so tests can monkeypatch ``connectors.bigquery.extractor.bq_metadata_cache_repo``.

    Deferred import keeps the module importable in standalone / offline contexts
    (no app layer, no system DB) — the import error only occurs when the
    function is actually called.
    """
    from src.repositories import bq_metadata_cache_repo as _factory  # noqa: PLC0415

    return _factory()


def _get_cached_entity_type(table_id: str | None) -> str | None:
    """Return a normalised entity_type from the metadata cache, or ``None``.

    ``None`` means "cache miss or unavailable" — caller must fall back to
    the live ``_detect_table_type`` BQ round-trip.  Any error (no system DB,
    missing table, import failure) is absorbed and logged at DEBUG so the
    standalone extractor path continues to work without app-layer imports.

    The returned value is normalised via ``_normalize_entity_type``:
    ``MATERIALIZED VIEW`` → ``MATERIALIZED_VIEW``.
    """
    if not table_id:
        return None
    try:
        row = bq_metadata_cache_repo().get(table_id)
    except Exception as exc:
        logger.debug(
            "bq_metadata_cache lookup skipped for %r (repo unavailable: %s) — "
            "falling back to live entity-type detection",
            table_id,
            exc,
        )
        return None
    if row is None:
        return None
    raw = row.get("entity_type")
    if not raw:
        return None
    return _normalize_entity_type(raw)


def parse_bq_fqn(value: Optional[str]) -> Optional[tuple]:
    """Parse a ``project.dataset.table`` fully-qualified BigQuery name.

    Returns ``(project, dataset, table)`` on success, ``None`` if ``value``
    is empty / None, or raises ``ValueError`` with a descriptive message
    if the string is non-empty but malformed (wrong segment count, empty
    segments, or any segment that fails the BQ identifier validators).

    Distinguishes "not set" (None / empty -> return None, caller falls
    back to legacy bucket+source_table path) from "set but invalid" (raise
    -> caller surfaces a registration error). Silently treating malformed
    values as missing would let an admin typo land in the registry and
    then rebuild against the legacy path, hiding the typo until query
    time.
    """
    if not value:
        return None
    parts = value.split(".")
    if len(parts) != 3 or not all(p for p in parts):
        raise ValueError(
            f"malformed bq_fqn {value!r}: expected exactly three non-empty "
            f"segments 'project.dataset.table'"
        )
    project, dataset, table = parts
    if not _validate_project_id(project):
        raise ValueError(
            f"malformed bq_fqn {value!r}: project {project!r} fails BQ "
            f"project-id grammar"
        )
    if not validate_quoted_identifier(dataset, "BQ dataset"):
        raise ValueError(
            f"malformed bq_fqn {value!r}: dataset {dataset!r} fails BQ "
            f"identifier grammar"
        )
    if not validate_quoted_identifier(table, "BQ table"):
        raise ValueError(
            f"malformed bq_fqn {value!r}: table {table!r} fails BQ "
            f"identifier grammar"
        )
    return (project, dataset, table)


# Serializes the body of `init_extract` across threads so two concurrent
# materialize calls (e.g. the synchronous timeout-fallback BackgroundTask
# kicking in while the original daemon thread is still running) can't both
# reach the `shutil.move(tmp, db_path)` swap and corrupt the extract file.
# `SyncOrchestrator._rebuild_lock` only protects the master-view rebuild,
# not the per-source extract-file write, so we need a dedicated lock here.
_INIT_EXTRACT_LOCK = threading.Lock()

_LOCK_TTL_DEFAULT_SECONDS: int = 86400  # 24h — overridable via materialize.lock_ttl_seconds


class MaterializeInFlightError(Exception):
    """Raised when a per-table_id materialize is already running.

    Caller (`_run_materialized_pass`) should treat this as a 'skipped,
    in-flight' outcome — the in-flight worker will finish and write
    sync_state on its own. Critically, this is NOT an error condition;
    `state.set_error` MUST NOT be called for this exception or the
    registry would surface a false-positive failure to the operator
    every overlap."""

    def __init__(self, table_id: str, layer: str = "process"):
        self.table_id = table_id
        self.layer = layer
        super().__init__(
            f"materialize for {table_id!r} already in flight ({layer} lock held)"
        )


# Unbounded by design — each registered table_id gets one Lock for the
# process lifetime. Per-Lock cost is ~56 bytes; a deployment with even
# 10k registered tables holds <1 MB. No cleanup logic — clean would
# need ref-counting and risks freeing a Lock currently held by a worker.
_table_locks: dict[str, threading.Lock] = {}
_table_locks_registry: threading.Lock = threading.Lock()


def _get_table_lock(table_id: str) -> threading.Lock:
    """Return the process-wide mutex for a given table_id, creating it
    on first reference. The registry mutex serializes the dict mutation
    only — once the per-id Lock is returned, contention between callers
    happens on that lock alone."""
    with _table_locks_registry:
        lock = _table_locks.get(table_id)
        if lock is None:
            lock = threading.Lock()
            _table_locks[table_id] = lock
        return lock


def _get_lock_ttl_seconds() -> int:
    """Read the configured stale-lock TTL with fallback to the default.

    Operator override lives at instance.yaml `materialize.lock_ttl_seconds`
    (also editable via /admin/server-config). Default 86400 s = 24 h
    matches the upper bound of any healthy BQ COPY in practice — anything
    longer is a stuck process or a hung BQ session, both of which warrant
    reclaim on next attempt."""
    try:
        # Deferred import: keeps the connectors module importable in
        # contexts where the app layer isn't bootstrapped (e.g. unit tests
        # that exercise extractor helpers without the FastAPI app).
        from app.instance_config import get_value
        v = get_value(
            "materialize", "lock_ttl_seconds",
            default=_LOCK_TTL_DEFAULT_SECONDS,
        )
        n = int(v) if v is not None else _LOCK_TTL_DEFAULT_SECONDS
        return n if n > 0 else _LOCK_TTL_DEFAULT_SECONDS
    except Exception:
        return _LOCK_TTL_DEFAULT_SECONDS


def sweep_stale_parquet_locks(data_root: Path | str) -> int:
    """Walk every ``*.parquet.lock`` under ``data_root`` and unlink the
    ones older than the configured TTL. Returns the count reclaimed.

    Called once at app startup so the dev VM doesn't accumulate 0-byte
    zombie lock files (issue #260 — observed lock from a SIGKILL'd
    materialize run sitting for 6 days). The acquire path already
    reclaims stale locks lazily on the next attempt, but startup sweep
    keeps `/data/extracts/*/data/` tidy regardless of whether the next
    materialize is scheduled soon.

    Failures (permission, vanished mid-stat, weird FS) are logged at
    WARNING and counted toward "errors"; the sweep doesn't raise.
    """
    root = Path(data_root)
    if not root.exists():
        return 0
    ttl = _get_lock_ttl_seconds()
    now = time.time()
    reclaimed = 0
    errors = 0
    # Search both ``<root>/data/*.lock`` (Keboola/BQ layout) and
    # ``<root>/*/data/*.lock`` (multi-source layout under /data/extracts).
    candidates: list[Path] = []
    try:
        candidates.extend(root.rglob("*.parquet.lock"))
    except Exception as e:
        logger.warning("sweep_stale_parquet_locks: rglob failed at %s: %s", root, e)
        return 0
    for lock_path in candidates:
        try:
            age = now - lock_path.stat().st_mtime
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning("sweep_stale_parquet_locks: stat failed on %s: %s", lock_path, e)
            errors += 1
            continue
        if age <= ttl:
            continue
        try:
            lock_path.unlink()
            logger.info(
                "Swept stale materialize lock at %s (age %.0fs > TTL %ds)",
                lock_path, age, ttl,
            )
            reclaimed += 1
        except FileNotFoundError:
            # Concurrent reclaim won — fine.
            continue
        except Exception as e:
            logger.warning("sweep_stale_parquet_locks: unlink failed on %s: %s", lock_path, e)
            errors += 1
    if reclaimed or errors:
        logger.info(
            "sweep_stale_parquet_locks: reclaimed=%d errors=%d under %s",
            reclaimed, errors, root,
        )
    return reclaimed


def _try_acquire_file_lock(lock_path: Path):
    """Try to acquire an advisory exclusive flock on `lock_path`. Returns
    the open file object on success (caller must close to release); None
    on conflict.

    Stale-lock reclaim: if the lock_path exists and its mtime is older
    than the configured TTL, log a warning and unlink BEFORE attempting
    to open + flock. The pre-stat-then-decide order is load-bearing: a
    naive "open then check mtime" path opens with `mode="w"` which
    truncates the file and refreshes its mtime to *now* on every
    invocation, including failed flock attempts. That makes the age
    check at the next pass always see ~0 — the TTL reclaim branch
    becomes unreachable and `materialize.lock_ttl_seconds` config knob
    is silently dead code (Devin Review on extractor.py:166). Stat-first
    sidesteps the self-refresh: the reclaim decision is made against the
    REAL mtime the file had before any of this call's I/O.

    Caveat: ``lock_path.unlink()`` + the subsequent ``open()`` creates a
    NEW inode — fcntl.flock keys on inode, so a still-running holder's
    lock on the (now-orphan) old inode does NOT block the new acquisition.
    A genuine overrunning materialize past TTL therefore CAN race a
    fresh attempt and both can write to ``<id>.parquet.tmp``. The
    in-process ``threading.Lock`` keyed on ``table_id`` blocks that race
    within one scheduler process; cross-process protection (two schedulers
    on the same workspace) relies on operators not running multiple
    concurrent schedulers AND on the TTL being well above the longest
    plausible COPY (24 h default). If real corruption surfaces in
    production, the next iteration should attach a pid to the lock file
    and skip reclaim while the holder pid is alive."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Stat BEFORE any open(). This is the pre-probe mtime — what the
    # file looked like before our call ran any I/O. If the file doesn't
    # exist yet, there's nothing stale to reclaim and we fall through to
    # the open + flock path.
    try:
        pre_probe_mtime: float | None = lock_path.stat().st_mtime
    except FileNotFoundError:
        pre_probe_mtime = None

    if pre_probe_mtime is not None:
        age = time.time() - pre_probe_mtime
        if age > _get_lock_ttl_seconds():
            logger.warning(
                "Reclaiming stale materialize lock at %s (age %.1fs > TTL)",
                lock_path, age,
            )
            try:
                lock_path.unlink()
            except FileNotFoundError:
                # Race: someone else reclaimed between our stat and unlink.
                # Fall through to the open + flock attempt — they must have
                # already acquired and we'll legitimately conflict.
                pass

    # Open in 'w' mode (creates if missing, truncates if present). The
    # mtime refresh on success is intentional — that's the signal the
    # NEXT caller's pre_probe_mtime stat reads to decide whether the
    # lock has gone stale. Failed flock on a non-stale lock simply
    # bumps mtime to now (the last-attempted-acquire timestamp); that's
    # a harmless lie about who's holding it, since the active holder
    # would refresh it anyway on a successful acquire.
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        # Another holder owns the lock — return None so the caller can
        # propagate MaterializeInFlightError.
        f.close()
        return None
    except OSError:
        # Anything else (read-only fs, unsupported, fd exhaustion) is a
        # platform / config error, not a contention signal. Close the fd
        # and re-raise so the caller (and operator) sees the real failure
        # instead of a silent leak.
        f.close()
        raise


def _detect_table_type(
    conn: duckdb.DuckDBPyConnection,
    project: str,
    dataset: str,
    table: str,
    billing_project: str | None = None,
) -> str | None:
    """Return BQ entity type for `project.dataset.table`.

    Uses `bigquery_query()` table function which routes through the BQ jobs
    API — works on tables, views, and materialized views alike. Returns the
    value of INFORMATION_SCHEMA.TABLES.table_type ('BASE TABLE', 'VIEW',
    'MATERIALIZED_VIEW') or None if not found.

    Args:
        project: Data project (where the entity lives — appears in the
            INFORMATION_SCHEMA FROM clause).
        dataset, table: Identify the BQ entity.
        billing_project: Project whose SA quota pays for the lookup job and
            against which `serviceusage.services.use` is checked. When ``None``
            (default), bills against ``project`` — fine for same-project
            lookups. Cross-project callers MUST pass ``billing_project`` to
            the extractor's billing project explicitly; the data-side SA
            typically has only ``bigquery.dataViewer`` on ``project`` and lacks
            ``serviceusage.services.use`` there, so reusing ``project`` for
            billing 403s and the caller's broad ``except Exception`` silently
            drops the row.
    """
    if billing_project is None:
        billing_project = project
    bq_sql = (
        f"SELECT table_type FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES` "
        f"WHERE table_name = ? LIMIT 1"
    )
    # Parameter-bind billing_project (1st arg of bigquery_query), the inner BQ
    # SQL (2nd arg), and the table-name predicate. This avoids the nested-quote
    # bug where inline `'{table}'` would close the outer `bigquery_query('...')`
    # string. Note: bigquery_query forwards extra positional args as BQ query
    # parameters, bound positionally to the `?` placeholders inside `bq_sql`.
    duck_sql = "SELECT * FROM bigquery_query(?, ?, ?)"
    row = conn.execute(duck_sql, [billing_project, bq_sql, table]).fetchone()
    return row[0] if row else None


_BILLING_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def _escape_sql_string_literal(s: str) -> str:
    """Double every single quote so the result is safe to embed inside a
    single-quoted SQL string literal. DuckDB and BigQuery both honor the
    SQL standard `''` escape inside `'...'`. Used to wrap admin
    source_query into bigquery_query()'s second arg without breaking
    the literal envelope."""
    return s.replace("'", "''")


def _wrap_admin_sql_for_jobs_api(billing_project: str, inner_sql: str) -> str:
    """Build the COPY-source SQL that runs admin's `inner_sql` through
    the BigQuery jobs API via the DuckDB BQ extension's
    ``bigquery_query()`` table function.

    Why: the default `bq."ds"."t"` reference path uses the BQ Storage
    Read API which rejects non-base entities (views, materialized views).
    Routing through `bigquery_query()` uses the jobs API which accepts
    every entity type uniformly.

    Args:
        billing_project: GCP project ID that bills the BQ job. Must
            match the GCP project_id grammar — anything else is rejected
            as a defense-in-depth check (admin is trusted, but a typo
            should fail closed not silently lose budget to the wrong
            project).
        inner_sql: BigQuery-flavor SQL the admin registered as
            ``source_query``. Should be BigQuery-native; DuckDB-flavor
            `bq."ds"."t"` references are not enforced here but will fail at
            COPY time inside the BQ jobs API. Existing rows are converted by
            the v24 schema migration; new rows are validated upstream at
            register/PUT.

    Returns:
        A DuckDB-parseable SQL fragment suitable as the operand of
        ``COPY (...) TO 'path' (FORMAT PARQUET)``.
    """
    if not _BILLING_PROJECT_RE.match(billing_project):
        raise ValueError(
            f"billing_project {billing_project!r} is not a valid GCP project_id "
            "(grammar: ^[a-z][a-z0-9-]{4,28}[a-z0-9]$)"
        )
    return (
        f"SELECT * FROM bigquery_query('{billing_project}', "
        f"'{_escape_sql_string_literal(inner_sql)}')"
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
        query_mode VARCHAR DEFAULT 'remote'
    )""")


def _ensure_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotent variant of `_create_meta_table` — creates the table if
    missing, leaves existing rows untouched. Used by `materialize_query`
    to register the materialized parquet without wiping the
    extractor-subprocess-written remote rows that share the same
    extract.duckdb."""
    conn.execute("""CREATE TABLE IF NOT EXISTS _meta (
        table_name VARCHAR NOT NULL,
        description VARCHAR,
        rows BIGINT,
        size_bytes BIGINT,
        extracted_at TIMESTAMP,
        query_mode VARCHAR DEFAULT 'remote'
    )""")


def _persist_materialized_inner_view(
    extract_db_path: Path,
    table_id: str,
    parquet_path: Path,
    rows: int,
    size_bytes: int,
) -> None:
    """Write the materialized parquet's inner view + ``_meta`` row into
    ``extract.duckdb`` so the orchestrator's master-view rebuild picks it
    up uniformly with remote-mode rows. Without this, ``materialize_query``
    leaves the parquet on disk but no record of it in ``_meta``, and the
    orchestrator's ``rebuild()`` scan never creates the master view —
    ``agnes query`` then 400s with "registered as query_mode='materialized'
    but is not yet materialized" even though the parquet exists.

    Idempotent: existing ``_meta`` row for the same ``table_name`` is
    replaced, existing inner view is recreated. Fail-soft — the parquet
    is the canonical artifact; if extract.duckdb registration fails (lock
    contention, missing file, schema drift), log and continue. The
    caller's ``rebuild_from_registry`` rebuild will get a chance to fix
    it next pass.
    """
    if not extract_db_path.exists():
        # Fresh BQ-only deployment hasn't run the extractor subprocess
        # yet, so extract.duckdb doesn't exist. Nothing to update — the
        # next extractor pass + materialize cycle will populate it.
        logger.info(
            "materialize: extract.duckdb at %s does not exist yet; "
            "skipping inner-view registration. Next extractor pass will "
            "create it and the master view will appear on the rebuild "
            "after that.",
            extract_db_path,
        )
        return

    safe_table = _escape_sql_string_literal(table_id)
    safe_path = _escape_sql_string_literal(str(parquet_path))
    try:
        with _pin_session_utc(duckdb.connect(str(extract_db_path), read_only=False)) as ext_conn:
            _ensure_meta_table(ext_conn)
            # `_meta` has no UNIQUE on table_name (legacy schema), so we
            # do a manual delete-then-insert. Wrap in a transaction so
            # concurrent reads of `_meta` either see the old row or the
            # new one, never both / neither.
            ext_conn.execute("BEGIN")
            try:
                ext_conn.execute(
                    "DELETE FROM _meta WHERE table_name = ?", [table_id]
                )
                ext_conn.execute(
                    "INSERT INTO _meta VALUES (?, '', ?, ?, CURRENT_TIMESTAMP, 'materialized')",
                    [table_id, rows, size_bytes],
                )
                # Inner view backing the master view. Orchestrator scans
                # information_schema.tables for the attached extract.duckdb
                # and only creates a master view when an inner object
                # exists by the same name. read_parquet() is hot per-call,
                # so the master view path goes through the same disk.
                ext_conn.execute(
                    f"CREATE OR REPLACE VIEW \"{table_id}\" AS "
                    f"SELECT * FROM read_parquet('{safe_path}')"
                )
                ext_conn.execute("COMMIT")
            except Exception:
                try:
                    ext_conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
    except Exception as e:
        # Fail-soft: parquet is on disk, registry stays consistent, the
        # next extractor + orchestrator pass will recover. Loud log so
        # operators can spot persistent breakage.
        logger.warning(
            "materialize: failed to register %s in extract.duckdb (%s) — "
            "parquet at %s is fine, master view will appear after the "
            "next sync cycle. Error: %s",
            table_id, extract_db_path, parquet_path, e,
        )


def _create_remote_attach_table(
    conn: duckdb.DuckDBPyConnection, project_id: str
) -> None:
    """Write _remote_attach. token_env is empty for BQ — orchestrator
    detects extension='bigquery' and refreshes the token from GCE metadata
    on its own."""
    conn.execute("DROP TABLE IF EXISTS _remote_attach")
    conn.execute("""CREATE TABLE _remote_attach (
        alias VARCHAR,
        extension VARCHAR,
        url VARCHAR,
        token_env VARCHAR
    )""")
    conn.execute(
        "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
        ["bq", "bigquery", f"project={project_id}", ""],
    )


def init_extract(
    output_dir: str,
    project_id: str,
    table_configs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Create extract.duckdb with remote views into BigQuery.

    Authenticates via the GCE metadata server. For each registered table,
    detects whether the BQ entity is a BASE TABLE or VIEW, and emits a
    DuckDB view that uses the appropriate path:
      - BASE TABLE → direct ATTACH ref (Storage Read API, fast for full scans)
      - VIEW       → bigquery_query() table function (jobs API, supports views)

    Args:
        output_dir: Path to write extract.duckdb
        project_id: GCP project ID for billing/job execution
        table_configs: List of table config dicts from table_registry

    Returns:
        Dict with stats: {tables_registered: int, errors: list}
    """
    # Serialize concurrent calls to avoid a torn `shutil.move` swap when the
    # admin route's timeout-fallback BackgroundTask runs alongside the still-
    # alive daemon thread that exceeded the 5s budget.
    with _INIT_EXTRACT_LOCK:
        return _init_extract_locked(output_dir, project_id, table_configs)


def _init_extract_locked(
    output_dir: str,
    project_id: str,
    table_configs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Inner body of init_extract executed under _INIT_EXTRACT_LOCK."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    db_path = output_path / "extract.duckdb"
    tmp_db_path = output_path / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()

    stats: Dict[str, Any] = {"tables_registered": 0, "errors": []}
    now = datetime.now(timezone.utc)

    # Validate project_id before any work — no point opening DB or fetching a
    # token if the value is structurally bogus and would only break SQL later.
    if not _validate_project_id(project_id):
        msg = f"unsafe BQ project_id: {project_id!r}"
        logger.error(msg)
        stats["errors"].append({"table": "<config>", "error": msg})
        return stats

    # Fetch token before opening DB so failure aborts cleanly without partial file
    try:
        token = get_metadata_token()
    except BQMetadataAuthError as e:
        logger.error("BQ metadata auth failed: %s", e)
        stats["errors"].append({"table": "<auth>", "error": str(e)})
        return stats

    conn = _pin_session_utc(duckdb.connect(str(tmp_db_path)))
    try:
        # Install and load BigQuery extension
        try:
            # BQ jobs issued via the DuckDB bigquery extension cannot
            # carry job labels (DuckDB owns the job config). See CHANGELOG.
            conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
            # session-scoped DuckDB secret with the metadata token
            escaped_token = token.replace("'", "''")
            conn.execute(
                f"CREATE SECRET bq_session (TYPE bigquery, ACCESS_TOKEN '{escaped_token}')"
            )
            from connectors.bigquery.access import apply_bq_session_settings
            apply_bq_session_settings(conn)
            conn.execute(
                f"ATTACH 'project={project_id}' AS bq (TYPE bigquery, READ_ONLY)"
            )
            logger.info("Attached BigQuery project: %s", project_id)
        except Exception as attach_err:
            logger.error("Failed to attach BigQuery project %s: %s", project_id, attach_err)
            stats["errors"].append(
                {"table": "*", "error": f"BigQuery ATTACH failed: {attach_err}"}
            )
            # No tables can be registered without a working connection
            for tc in table_configs:
                stats["errors"].append(
                    {"table": tc["name"], "error": "skipped: BigQuery ATTACH failed"}
                )
            return stats

        _create_meta_table(conn)
        _create_remote_attach_table(conn, project_id)

        for tc in table_configs:
            # Materialized rows are written by the sync trigger pass via
            # `materialize_query()` — they live as parquets in
            # /data/extracts/bigquery/data/, picked up by the orchestrator's
            # standard local-parquet discovery. Don't create a remote view
            # here (would shadow the parquet via name collision).
            if tc.get("query_mode") == "materialized":
                continue

            table_name = tc["name"]
            # v51: if ``bq_fqn`` is set on the registry row, it overrides the
            # legacy bucket+source_table+project_id triplet. ``bq_fqn``
            # decouples the UX/RBAC ``bucket`` label from the physical BQ
            # dataset name (issue #343). Missing/empty bq_fqn falls back to
            # the legacy path — backwards compat for pre-v51 registrations.
            raw_fqn = tc.get("bq_fqn")
            try:
                parsed_fqn = parse_bq_fqn(raw_fqn)
            except ValueError as e:
                stats["errors"].append({"table": table_name, "error": str(e)})
                continue
            if parsed_fqn is not None:
                fqn_project, dataset, source_table = parsed_fqn
                # Cross-project bq_fqn: extractor ATTACHed `bq` against
                # `project_id` (the overlay's data project). When
                # bq_fqn.project differs, the BASE TABLE path via the bq
                # alias would silently route to the wrong project. The
                # VIEW path goes through ``bigquery_query(billing, …)``
                # which takes its own billing arg, so cross-project works
                # there — but BASE TABLE we skip with a clear warning
                # rather than serve wrong-source data. Multi-ATTACH per
                # distinct project is the proper fix (follow-up; see PR
                # description).
                cross_project = fqn_project != project_id
            else:
                dataset = tc.get("bucket", "")
                source_table = tc.get("source_table", table_name)
                fqn_project = project_id
                cross_project = False

            # #81 Group D — refuse rows with unsafe identifiers. Same
            # rationale as the keboola extractor: registry is admin-controlled
            # but anyone with write access can otherwise inject SQL via the
            # CREATE VIEW interpolation below. Skip-and-continue.
            # `table_name` is the DuckDB view name in the master
            # analytics DB and the orchestrator uses the STRICT
            # validator there — accept the same constraint upstream
            # so a name with `-` or `.` fails fast in extraction
            # rather than getting silently dropped at rebuild time.
            # `dataset` and `source_table` are upstream-typed (BQ
            # naming) so use the relaxed validator for those.
            if not validate_identifier(table_name, "BigQuery table_name"):
                stats["errors"].append({"table": table_name, "error": f"unsafe table_name: {table_name!r}"})
                continue
            if not validate_quoted_identifier(dataset, "BigQuery dataset"):
                stats["errors"].append({"table": table_name, "error": f"unsafe dataset: {dataset!r}"})
                continue
            if not validate_quoted_identifier(source_table, "BigQuery source_table"):
                stats["errors"].append({"table": table_name, "error": f"unsafe source_table: {source_table!r}"})
                continue

            try:
                # Cross-project rows MUST bill against ``project_id`` (the
                # extractor's billing project where the SA has
                # ``serviceusage.services.use``). Passing ``fqn_project`` for
                # both data + billing 403s on cross-project setups, the
                # broad ``except Exception`` below silently drops the row,
                # and the cross-project VIEW path at line ~696 never
                # executes. (Same-project rows: ``fqn_project == project_id``
                # so this is a no-op.)
                table_id = tc.get("id")
                entity_type = _get_cached_entity_type(table_id)
                if entity_type is None:
                    # _detect_table_type returns the raw BQ INFORMATION_SCHEMA
                    # value ("MATERIALIZED VIEW" with a space); normalize it to
                    # the underscore form the branch below expects, same as the
                    # cached path.
                    entity_type = _normalize_entity_type(
                        _detect_table_type(
                            conn, fqn_project, dataset, source_table,
                            billing_project=project_id,
                        )
                    )
                if entity_type is None:
                    raise RuntimeError(
                        f"BQ entity {fqn_project}.{dataset}.{source_table} not found"
                    )

                # Issue #160: always create a master view for query_mode='remote'
                # rows we have proven runtime support for.
                #   BASE TABLE → catalog path (Storage Read API, predicate pushdown)
                #   VIEW / MATERIALIZED_VIEW → bigquery_query() (jobs API, no
                #   pushdown — cost is bounded by the /api/query guardrail)
                # Other entity types (EXTERNAL, SNAPSHOT, CLONE, future) are
                # logged + skipped, with NO _meta row, since orchestrator-side
                # master-view creation requires a corresponding inner view.
                if entity_type == "BASE TABLE":
                    if cross_project:
                        # bq_fqn points to a different project than the
                        # ATTACH alias — see comment above. Skip with a
                        # diagnostic instead of serving wrong-source data.
                        logger.warning(
                            "bq_fqn project mismatch for BASE TABLE %s: "
                            "bq_fqn=%s, extractor ATTACHed to %s. Master "
                            "view skipped — multi-ATTACH follow-up needed "
                            "or register a same-project proxy view.",
                            table_name, fqn_project, project_id,
                        )
                        continue
                    view_sql = (
                        f'CREATE OR REPLACE VIEW "{table_name}" AS '
                        f'SELECT * FROM bq."{dataset}"."{source_table}"'
                    )
                    conn.execute(view_sql)
                elif entity_type in ("VIEW", "MATERIALIZED_VIEW"):
                    # `dataset` and `source_table` are validated above by
                    # validate_quoted_identifier; ``fqn_project`` is either
                    # the entry-validated ``project_id`` or comes from
                    # ``parse_bq_fqn`` which re-validates the project
                    # segment via ``_validate_project_id``.
                    # The .replace("'", "''") is defense-in-depth on the
                    # inline literal.
                    bq_inner = f"SELECT * FROM `{fqn_project}.{dataset}.{source_table}`"
                    bq_inner_escaped = bq_inner.replace("'", "''")
                    # Billing project stays ``project_id`` (the extractor's
                    # ATTACH project) — that's the project whose SA quota
                    # pays for the job. ``fqn_project`` is the data project
                    # (where the table lives); ``bigquery_query`` reads
                    # cross-project just fine when the SA on ``project_id``
                    # has BQ Data Viewer on ``fqn_project``.
                    view_sql = (
                        f'CREATE OR REPLACE VIEW "{table_name}" AS '
                        f"SELECT * FROM bigquery_query('{project_id}', '{bq_inner_escaped}')"
                    )
                    conn.execute(view_sql)
                else:
                    # Unverified entity type. Skip both the wrap view and
                    # the _meta row. The registry row remains; /api/v2/scan
                    # can still operate from it (builds BQ SQL from
                    # bucket+source_table), and `agnes snapshot create` works.
                    logger.warning(
                        "Unverified BQ entity_type %r for %s.%s.%s — master view skipped. "
                        "Use `agnes snapshot create` for this row, or file an issue with "
                        "a repro to request native support.",
                        entity_type, fqn_project, dataset, source_table,
                    )
                    continue  # Do NOT insert _meta — no inner view to point at.

                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, 0, 0, ?, 'remote')",
                    [table_name, tc.get("description", ""), now],
                )
                stats["tables_registered"] += 1
                logger.info(
                    "Registered remote view: %s -> %s.%s.%s (%s)",
                    table_name, fqn_project, dataset, source_table, entity_type,
                )
            except Exception as e:
                logger.error("Failed to register %s: %s", table_name, e)
                stats["errors"].append({"table": table_name, "error": str(e)})

        conn.execute("DETACH bq")
    finally:
        conn.close()

    # Atomic swap (preserve from existing implementation)
    old_wal = Path(str(db_path) + ".wal")
    if old_wal.exists():
        old_wal.unlink()
    if tmp_db_path.exists():
        shutil.move(str(tmp_db_path), str(db_path))
    tmp_wal = Path(str(tmp_db_path) + ".wal")
    if tmp_wal.exists():
        tmp_wal.unlink()

    return stats


class MaterializeBudgetError(RuntimeError):
    """Raised when a `materialize_query` BQ dry-run estimate exceeds the
    configured `data_source.bigquery.max_bytes_per_materialize` cap.

    The materialize trigger pass logs this and skips the row; the next
    scheduled tick re-tries (in case the underlying table size dropped
    or the operator raised the cap). Shape mirrors `BqAccessError` —
    `current` and `limit` for operator triage.
    """

    def __init__(self, message: str, *, table_id: str, current: int, limit: int):
        self.table_id = table_id
        self.current = current
        self.limit = limit
        super().__init__(message)


def materialize_query(
    table_id: str,
    sql: str,
    *,
    bq,  # connectors.bigquery.access.BqAccess (untyped here to avoid circular import at type-check)
    output_dir: str,
    max_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Run `sql` through the DuckDB BigQuery extension and write the result
    to `<output_dir>/data/<table_id>.parquet` atomically.

    Designed for `query_mode='materialized'` table_registry rows. The SQL
    is admin-registered BQ-native SQL (DuckDB-flavor `bq."ds"."t"` refs are
    validated upstream). The SQL is wrapped in `bigquery_query('<billing>',
    '<inner>')` before the COPY so the BQ extension routes through the BQ
    jobs API — the default Storage Read API path rejects non-base entities
    (views, materialized views) with "non-table entities cannot be read with
    the storage API". Routing through `bigquery_query()` works uniformly for
    base tables and views alike.

    Cost guardrail: when `max_bytes` is a positive int, run a BQ dry-run
    via `bq.client()` first; raise `MaterializeBudgetError` if the
    estimate exceeds the cap. `max_bytes=None` or `max_bytes <= 0`
    disables the guardrail (config sentinel, see
    `data_source.bigquery.max_bytes_per_materialize`). The dry-run operates
    on the inner `sql` (BQ-native), not the wrapped form.

    Dry-run is best-effort and fail-open: if the dry-run errors (transient
    upstream failure, missing google lib), we log a warning and proceed
    with the wrapped COPY.

    Atomic write: result lands in `<id>.parquet.tmp` first, then
    `os.replace` swaps it in. A failed COPY leaves no partial file behind.

    Concurrency: per-``table_id`` in-process mutex + advisory file lock
    on ``<table_id>.parquet.lock``. Overlapping calls for the same id
    raise ``MaterializeInFlightError`` immediately so the caller can
    skip cleanly without consuming the COPY budget twice. Stale file
    locks (mtime > ``materialize.lock_ttl_seconds``, default 24 h) are
    reclaimed automatically.

    Args:
        table_id: Logical id from table_registry; becomes the parquet
            filename. Must pass `validate_identifier()` so it can't
            inject path traversal.
        sql: BQ-native SELECT statement, no trailing semicolon. Wrapped
            in `bigquery_query()` before the COPY — must not itself
            contain a `bigquery_query()` call.
        bq: A `BqAccess` instance — provides `duckdb_session()` for the
            COPY and `client()` for the dry-run.
        output_dir: Connector root, e.g. `/data/extracts/bigquery`.
            Parquet lands in `<output_dir>/data/<table_id>.parquet`.
        max_bytes: Optional cap on BQ bytes scanned. None or <= 0 disables.

    Returns:
        {"rows": int, "size_bytes": int, "query_mode": "materialized"}

    Raises:
        ValueError: if `table_id` is unsafe or `bq.projects.billing` fails
            the GCP project_id grammar check.
        MaterializeInFlightError: if a concurrent call for the same table_id
            is already in progress (in-process or cross-process).
        MaterializeBudgetError: if `max_bytes > 0` and dry-run estimate exceeds it.
        BqAccessError: from `bq.duckdb_session()` (auth_failed / bq_lib_missing /
            not_configured) — caller catches and aggregates into the trigger
            pass summary.
        duckdb.Error: if the COPY itself fails (e.g. bad SQL, missing table).
    """
    if not validate_identifier(table_id, "materialize table_id"):
        raise ValueError(f"unsafe table_id: {table_id!r}")

    out_path = Path(output_dir)
    data_dir = out_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    parquet_path = data_dir / f"{table_id}.parquet"
    tmp_path = data_dir / f"{table_id}.parquet.tmp"
    lock_path = data_dir / f"{table_id}.parquet.lock"

    proc_lock = _get_table_lock(table_id)
    if not proc_lock.acquire(blocking=False):
        raise MaterializeInFlightError(table_id, layer="process")
    try:
        file_lock = _try_acquire_file_lock(lock_path)
        if file_lock is None:
            raise MaterializeInFlightError(table_id, layer="file")
        try:
            if tmp_path.exists():
                tmp_path.unlink()

            # Build the wrapped SQL once — both the cost guardrail dry-run and
            # the COPY operate on `sql` (the inner BQ SQL); only the COPY needs
            # the DuckDB-side bigquery_query() envelope.
            billing_project = bq.projects.billing
            wrapped_sql = _wrap_admin_sql_for_jobs_api(billing_project, sql)

            if max_bytes is not None and max_bytes > 0:
                try:
                    from app.api.v2_scan import _bq_dry_run_bytes  # reuse main's impl
                    estimated = _bq_dry_run_bytes(bq, sql)  # NB: pass inner SQL (BQ-native)
                except Exception as e:
                    logger.warning(
                        "BQ dry-run failed for materialize cost guardrail (fail-open): %s. "
                        "Proceeding with COPY against `bigquery_query()` wrapping.",
                        e,
                    )
                    estimated = 0
                if estimated > max_bytes:
                    raise MaterializeBudgetError(
                        f"dry-run estimate {estimated:,} bytes exceeds cap "
                        f"{max_bytes:,} for table {table_id!r}",
                        table_id=table_id,
                        current=estimated,
                        limit=max_bytes,
                    )

            # COPY through a BqAccess-managed session. The session has the BQ
            # extension loaded with a SECRET token; bigquery_query() reuses that
            # auth path against the billing_project for the jobs API call.
            with bq.duckdb_session() as conn:
                # Memory caps (memory_limit=2GB, threads=2,
                # preserve_insertion_order=false) are applied uniformly
                # on every pool acquire by ``apply_bq_session_settings``
                # — see that function in ``connectors/bigquery/access.py``
                # for the rationale and the empirical 2 GiB trace.
                # Previously the SETs lived inline here, which only
                # mutated whichever pool entry was acquired for this
                # materialize call — leaving the other ~3 pool entries
                # at the 80%-of-host default and re-opening the OOM
                # window for any analyst query that landed on them.
                attached = {
                    r[0] for r in conn.execute(
                        "SELECT database_name FROM duckdb_databases()"
                    ).fetchall()
                }
                if "bq" not in attached:
                    conn.execute(
                        f"ATTACH 'project={bq.projects.data}' AS bq (TYPE bigquery, READ_ONLY)"
                    )

                try:
                    safe_path = _escape_sql_string_literal(str(tmp_path))
                    conn.execute(
                        f"COPY ({wrapped_sql}) TO '{safe_path}' (FORMAT PARQUET)"
                    )
                    rows = conn.execute(
                        f"SELECT count(*) FROM read_parquet('{safe_path}')"
                    ).fetchone()[0]
                except Exception:
                    if tmp_path.exists():
                        tmp_path.unlink()
                    raise

            # Compute the parquet hash inline before the atomic swap. The caller used
            # to re-read the file in `_run_materialized_pass` to hash it via
            # `_file_hash`, but that's a synchronous full-read on the FastAPI worker
            # thread — a 10 GiB parquet means 50+ seconds of disk I/O blocking other
            # requests. Hashing here keeps the open-file handle hot from the COPY
            # round and removes the second read. Devil's-advocate review item.
            h = hashlib.md5()
            with open(tmp_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            parquet_hash = h.hexdigest()

            size_bytes = tmp_path.stat().st_size
            os.replace(tmp_path, parquet_path)

            rows = int(rows)

            # Register the parquet in extract.duckdb so the orchestrator's
            # master-view rebuild can pick it up uniformly with remote-mode
            # rows. Without this, the parquet sits on disk but the master
            # view is never created — `agnes query "SELECT … FROM <id>"`
            # 400s with "not yet materialized in this instance's analytics
            # views". Fail-soft — the parquet is canonical, the next
            # extractor + orchestrator pass will recover any registration
            # drift.
            _persist_materialized_inner_view(
                extract_db_path=out_path / "extract.duckdb",
                table_id=table_id,
                parquet_path=parquet_path,
                rows=rows,
                size_bytes=size_bytes,
            )

            if rows == 0:
                # 0 rows is indistinguishable from "the SQL is wrong and nobody
                # noticed" — surface it loudly so operators see it in the scheduler
                # log line and the per-row error aggregation. Caller decides whether
                # to alert.
                logger.warning(
                    "Materialized %s produced 0 rows — verify the SQL filter is "
                    "intentional. Parquet written: %s",
                    table_id, parquet_path,
                )

            return {
                "rows": rows,
                "size_bytes": size_bytes,
                "query_mode": "materialized",
                "hash": parquet_hash,
            }
        finally:
            try:
                file_lock.close()  # releases flock
            except Exception:
                pass
            # Don't unlink lock_path — its mtime is the TTL signal for
            # the next reclaim. Leaving it in place is intentional.
    finally:
        proc_lock.release()


def _resolve_bq_project_id() -> str:
    """Resolve ``data_source.bigquery.project`` honoring the overlay.

    Tries ``app.instance_config.get_value`` first (deep-merge of the static
    ``CONFIG_DIR/instance.yaml`` and the writable
    ``DATA_DIR/state/instance.yaml``); the writable overlay is what the
    admin UI / API writes to. Falls back to a direct read of the static
    config so the standalone ``__main__`` entry point still works in
    environments where the FastAPI app isn't importable (e.g. a one-shot
    scheduler container that only ships connector code).
    """
    try:
        from app.instance_config import get_value as _get_value  # noqa: PLC0415
        project_id = _get_value("data_source", "bigquery", "project", default="") or ""
        if project_id:
            return project_id
    except Exception:
        # The fallback below covers this path — keep going.
        pass
    try:
        from config.loader import load_instance_config as _load  # noqa: PLC0415
        cfg = _load() or {}
        return ((cfg.get("data_source") or {}).get("bigquery") or {}).get("project", "") or ""
    except Exception:
        return ""


def rebuild_from_registry(
    conn: duckdb.DuckDBPyConnection | None = None,
    output_dir: str | None = None,
) -> Dict[str, Any]:
    """Re-materialize the BigQuery extract.duckdb from the current registry.

    Reads ``data_source.bigquery.project`` from ``instance.yaml`` and the
    BigQuery rows from ``table_registry``, then calls ``init_extract`` to
    write ``extract.duckdb`` containing one DuckDB view per registered BQ
    table. Used by the admin API immediately after a register / update /
    unregister of a BigQuery row so the master view appears (or disappears)
    in seconds without waiting for the next scheduled sync.

    Args:
        conn: System DuckDB connection (already open). If None, a new one
            is opened and closed inside this call — convenient for the
            standalone __main__ entrypoint, but the API path always passes
            its request-scoped connection so we don't open a second handle
            on the same file.
        output_dir: Override for the extract directory. Defaults to
            ``${DATA_DIR}/extracts/bigquery`` to match the orchestrator's
            scan path.

    Returns:
        Dict with ``project_id``, ``tables_registered``, ``errors``, and
        ``skipped`` (set to True when there are no BQ rows in the registry,
        in which case the extract is left untouched).

    Project resolution: reads ``data_source.bigquery.project`` via
    ``app.instance_config.get_value`` so the writable overlay
    (``DATA_DIR/state/instance.yaml``, populated by ``POST /api/admin/
    configure`` and ``/server-config``) is honored. Pre-2026-04-28 this
    used ``config.loader.load_instance_config`` directly, which only sees
    the static ``CONFIG_DIR/instance.yaml`` — operators who configured BQ
    through the admin UI got a silent rebuild failure ("project missing")
    while validation passed (the validator already used the merged view).
    See review BLOCKER 2 in PR #119.
    """
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    project_id = _resolve_bq_project_id()

    if not project_id:
        msg = "data_source.bigquery.project missing from instance.yaml"
        logger.error(msg)
        return {
            "project_id": "",
            "tables_registered": 0,
            "errors": [{"table": "<config>", "error": msg}],
            "skipped": False,
        }

    owns_conn = conn is None
    sys_conn = conn if conn is not None else get_system_db()
    try:
        repo = TableRegistryRepository(sys_conn)
        tables = repo.list_by_source("bigquery")
    finally:
        if owns_conn:
            try:
                sys_conn.close()
            except Exception:
                pass

    if not tables:
        logger.warning("No BigQuery tables registered in table_registry")
        return {
            "project_id": project_id,
            "tables_registered": 0,
            "errors": [],
            "skipped": True,
        }

    if output_dir is None:
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        output_dir = str(data_dir / "extracts" / "bigquery")

    # Resolve init_extract via this module so tests that monkey-patch it
    # (e.g. tests/test_admin_bq_register.py) see the patched callable.
    import connectors.bigquery.extractor as _self
    result = _self.init_extract(output_dir, project_id, tables)
    out = dict(result)
    out["project_id"] = project_id
    out["skipped"] = False
    return out


if __name__ == "__main__":
    """Standalone: reads config from instance.yaml + table_registry, creates extract."""
    result = rebuild_from_registry()
    if result.get("skipped"):
        # No BQ rows registered — nothing to do, exit cleanly.
        raise SystemExit(0)
    if not result.get("project_id"):
        # Missing project → already logged inside rebuild_from_registry.
        raise SystemExit(2)
    logger.info("BigQuery extract init complete: %s", result)
