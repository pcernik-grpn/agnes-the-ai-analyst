"""Sync orchestrator — ATTACHes extract.duckdb files into master analytics.duckdb.

Remote table support
--------------------
Extractors that create views referencing external DuckDB extensions (e.g. Keboola,
BigQuery) must include a ``_remote_attach`` table in their extract.duckdb:

    CREATE TABLE _remote_attach (
        alias     VARCHAR,  -- DuckDB alias used in views, e.g. 'kbc'
        extension VARCHAR,  -- Extension name, e.g. 'keboola'
        url       VARCHAR,  -- Connection URL
        token_env VARCHAR   -- Env-var name holding the auth token (NOT the token itself).
                            -- Empty string for BigQuery — orchestrator detects
                            -- extension='bigquery' and refreshes the token from the
                            -- GCE metadata server on its own.
    );

At rebuild time the orchestrator reads ``_remote_attach``, installs/loads the
extension, then either: (a) for BigQuery, fetches a fresh access token from the
GCE metadata server and creates a session-scoped DuckDB SECRET before ATTACH;
(b) for sources with a non-empty ``token_env``, reads that env var and passes
the token inline; (c) ATTACHes without auth. Views referencing
``bq."dataset"."table"`` or ``kbc."bucket"."table"`` then resolve correctly.

Note: BQ secrets are session-scoped, so ``src.db._reattach_remote_extensions``
re-fetches the metadata token and re-creates the secret each time a read-only
analytics connection is opened.
"""

import contextlib
import hashlib
import logging
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import duckdb

from connectors.bigquery.auth import BQMetadataAuthError, get_metadata_token
from src.db import _open_duckdb
from src.orchestrator_security import (
    attach_host_allowlist_configured,
    escape_sql_string_literal,
    is_attach_host_allowed,
    is_builtin_extension,
    is_extension_allowed,
    is_token_env_allowed,
    resolve_remote_attach_token,
)
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

_rebuild_lock = threading.Lock()


@contextlib.contextmanager
def rebuild_mutex() -> Iterator[None]:
    """Combined in-process + cross-process mutual exclusion for the DuckLake
    write critical section — the exact two locks :meth:`SyncOrchestrator.rebuild`/
    :meth:`SyncOrchestrator.rebuild_source` take, in the same order (in-process
    ``_rebuild_lock`` first, then the cross-process ``src.db_pg.rebuild_lease``),
    exposed as one reusable context manager.

    Why this needs to exist as a shared helper (wave-2G Task 5 review carry-
    over, finding 1-concurrency): ``ducklake-maintenance``
    (``app.worker.kinds._run_ducklake_maintenance``) also writes the lake —
    catalog-wide ``merge_adjacent_files``/``ducklake_expire_snapshots``/
    ``ducklake_cleanup_old_files`` — via the same ``get_ducklake_write()``
    singleton a concurrent rebuild uses. ``ducklake-maintenance`` runs in the
    LIGHT lane, ``data-refresh``/``jira-refresh`` (which call
    :meth:`rebuild`/:meth:`rebuild_source`) run in the HEAVY lane, and both
    lanes run in the SAME worker process on independent OS threads
    (``asyncio.to_thread`` per lane slot — see ``app/worker/runtime.py``), so
    a long rebuild running past the maintenance job's schedule can race a
    catalog-wide expire/cleanup pass against an in-progress per-table
    ``CREATE OR REPLACE TABLE``. Taking the identical lock pair, in the
    identical order, from both call sites makes them mutually exclusive
    without risking a lock-order-inversion deadlock (which taking
    ``_rebuild_lock``/``rebuild_lease()`` in reversed order from a second
    call site would risk).

    No-op cross-process half on the DuckDB backend (``rebuild_lease()`` is a
    Postgres advisory lock, itself a no-op there) — the in-process
    ``_rebuild_lock`` still applies, matching :meth:`rebuild`'s existing
    behavior.
    """
    from src.db_pg import rebuild_lease

    with _rebuild_lock, rebuild_lease():
        yield


# Row count per Arrow RecordBatchReader batch for the DuckLake copy-ingest
# path (see `_ingest_source_ducklake`). Deliberately small relative to
# DuckDB's own default (1_000_000) so a single table's ingest never holds
# more than one batch's worth of rows in Python-process memory at a time.
_DUCKLAKE_INGEST_BATCH_SIZE = 100_000


def _capture_orchestrator_exception(exc: BaseException, **props) -> None:
    """Best-effort PostHog forward for rebuild failures. No-op when disabled."""
    try:
        from src.observability import get_posthog

        get_posthog().capture_exception(
            exc,
            distinct_id="system",
            properties={"component": "orchestrator", **props},
        )
    except Exception:
        logger.debug("PostHog capture_exception failed in orchestrator", exc_info=True)


# Identifier validation lives in src/identifier_validation.py so the
# orchestrator and the extractors share the same regex (#81 Group D).
# The local names are kept as aliases so existing call sites need no
# rename — they import from a single source of truth now.
from src.identifier_validation import (  # noqa: E402
    _SAFE_IDENTIFIER,  # noqa: F401  (re-exported for any historical caller)
)
from src.identifier_validation import (  # noqa: E402
    validate_identifier as _validate_identifier,
)


def _atomic_swap_db(tmp_path: str, target_path: str) -> None:
    """Atomically replace target DuckDB file, cleaning up WAL files."""
    import shutil

    target = Path(target_path)
    tmp = Path(tmp_path)

    # Remove old WAL file if it exists
    old_wal = Path(str(target) + ".wal")
    if old_wal.exists():
        old_wal.unlink()

    # Move temp DB into place
    if tmp.exists():
        shutil.move(str(tmp), str(target))

    # Clean up temp WAL
    tmp_wal = Path(str(tmp) + ".wal")
    if tmp_wal.exists():
        tmp_wal.unlink()


def _get_extracts_dir() -> Path:
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    return data_dir / "extracts"


def _hash_table_parts(table_dir: Path) -> list[dict] | None:
    """Per-part manifest for a partitioned table stored as a *directory* of
    parquet parts — Jira hive (``month=*/data.parquet``) and Keboola
    partitioned (``<key>.parquet``). Returns a list of
    ``{path, hash, size_bytes}`` sorted by relpath, where ``hash`` is the
    full content MD5 of the part (same contract as the single-file hash
    ``agnes pull`` re-verifies), or ``None`` when *table_dir* is not a
    directory of parquets (i.e. a single-file table).
    """
    if not table_dir.is_dir():
        return None
    parts: list[dict] = []
    for pq in sorted(
        table_dir.rglob("*.parquet"),
        key=lambda p: p.relative_to(table_dir).as_posix(),
    ):
        h = hashlib.md5()
        with open(pq, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        parts.append(
            {
                "path": pq.relative_to(table_dir).as_posix(),
                "hash": h.hexdigest(),
                "size_bytes": pq.stat().st_size,
            }
        )
    return parts or None


def _parts_rollup_hash(parts: list[dict]) -> str:
    """Deterministic whole-table hash for a partitioned table, derived from
    the path-sorted ``(path, hash)`` pairs. Lets the manifest's top-level
    ``hash`` / object-store mirror-index compare keep working unchanged for
    partitioned tables. Full 32-char MD5."""
    joined = "\n".join(f"{p['path']}:{p['hash']}" for p in sorted(parts, key=lambda p: p["path"]))
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


class SyncOrchestrator:
    """Scans /data/extracts/*, ATTACHes each extract.duckdb, creates master views."""

    def __init__(self, analytics_db_path: str | None = None):
        # analytics_db_path allows override for testing
        if analytics_db_path:
            self._db_path = analytics_db_path
        else:
            data_dir = Path(os.environ.get("DATA_DIR", "./data"))
            self._db_path = str(data_dir / "analytics" / "server.duckdb")
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

    def rebuild(self) -> dict[str, list[str]]:
        """Scan all extract directories, ATTACH each, create master views.

        Backend dispatch: ``analytics.backend: ducklake`` routes to
        :meth:`_do_rebuild_ducklake` (copy-ingest into the DuckLake
        catalog); the default ``legacy`` backend keeps the existing
        rebuild-and-swap-``server.duckdb`` path (:meth:`_do_rebuild`)
        completely unchanged.

        Returns: {source_name: [table_names]} for logging.
        """
        from src.analytics_backend import analytics_backend

        with rebuild_mutex():
            try:
                if analytics_backend() == "ducklake":
                    return self._do_rebuild_ducklake()
                return self._do_rebuild()
            except Exception as exc:
                _capture_orchestrator_exception(exc, op="rebuild")
                raise

    def rebuild_source(self, source_name: str) -> list[str]:
        """Rebuild views from a single source (e.g. after Jira webhook).

        Backend dispatch mirrors :meth:`rebuild`. On ``ducklake``, this
        is the incremental win over the legacy path: only
        ``source_name``'s own schema is re-ingested — every other
        source's DuckLake tables/snapshots are left untouched (the
        legacy path, by contrast, does a full :meth:`_do_rebuild` under
        the hood because ``server.duckdb`` is rebuilt-and-swapped whole
        each time).
        """
        from src.analytics_backend import analytics_backend

        with rebuild_mutex():
            try:
                if analytics_backend() == "ducklake":
                    return self._do_rebuild_ducklake(only_source=source_name).get(source_name, [])
                return self._do_rebuild_source(source_name)
            except Exception as exc:
                _capture_orchestrator_exception(exc, op="rebuild_source", source=source_name)
                raise

    def migrate_to_backend(self, to: str) -> dict[str, list[str]]:
        """Full rebuild into an EXPLICITLY named target backend — the
        engine behind ``agnes admin analytics migrate --to ducklake|legacy``
        (wave-2G Task 6), as distinct from :meth:`rebuild`, which dispatches
        on the *currently configured* :func:`src.analytics_backend.analytics_backend`.

        Why a separate entry point instead of temporarily monkeypatching the
        config and calling :meth:`rebuild`: ``analytics_backend()`` is
        resolved once and cached for the process lifetime (see that
        module's docstring) — config is operator-owned and read at boot,
        not hot-reloaded. The migration flow this method powers is
        therefore: (1) the admin endpoint validates prerequisites, (2) THIS
        method populates the target backend from the on-disk extracts tree
        — the extracts tree is the distribution artifact + rollback truth
        for both backends, so no re-extract is ever needed for either
        direction — and (3) the operator flips ``analytics.backend`` in
        config and restarts every role process to actually switch the live
        query-serving plane over.

        ``to="ducklake"`` runs the full copy-ingest rebuild
        (:meth:`_do_rebuild_ducklake`, all sources); ``to="legacy"`` runs
        the full rebuild-and-swap (:meth:`_do_rebuild`) — the rollback
        path, since the extracts tree never stopped being legacy's source
        of truth either. Materialized-SQL tables are NOT re-materialized
        by this call in either direction — they follow their own
        scheduler cadence and simply get copied/rebuilt from whatever
        parquet already sits under their source's ``data/`` directory.

        Takes the same :func:`rebuild_mutex` cross-process/in-process lock
        pair as :meth:`rebuild`/:meth:`rebuild_source`, so a migration
        rebuild can never race a concurrent scheduled rebuild or the
        ``ducklake-maintenance`` job.
        """
        if to not in ("ducklake", "legacy"):
            raise ValueError(f"to must be 'ducklake' or 'legacy', got {to!r}")

        with rebuild_mutex():
            try:
                if to == "ducklake":
                    return self._do_rebuild_ducklake()
                return self._do_rebuild()
            except Exception as exc:
                _capture_orchestrator_exception(exc, op="migrate_to_backend", to=to)
                raise

    def _sync_bq_remote_attach_with_overlay(self, extracts_dir: Path) -> None:
        """Detect drift in BQ extract.duckdb's ``_remote_attach.url`` and
        rewrite the extract when it disagrees with the overlay project.

        Operational hazard this closes (issue #343, observed on Foundry AI
        2026-05-19): an admin updates ``data_source.bigquery.project`` via
        ``POST /api/admin/server-config`` (overlay write), but the BQ
        ``extract.duckdb`` keeps the previously-baked ``project=<old>``
        in its ``_remote_attach`` row. The next rebuild ATTACHes the OLD
        project, queries against datasets that don't exist there, and the
        error message points at the old project — confusing operators
        who just changed the config.

        Fix: at every rebuild, read the BQ extract's ``_remote_attach.url``,
        compare against the overlay's ``data_source.bigquery.project``, and
        if they differ, call ``rebuild_from_registry`` to regenerate the
        extract. The regeneration path is the same one ``register-table``
        uses, so its semantics are well-tested.

        No-op preconditions (any one short-circuits to silent return):
          - no BQ extract directory on disk (instance never had BQ)
          - extract.duckdb missing (extracted-but-failed state)
          - overlay project unset (BQ not configured yet — first-time
            setup, not drift)
          - no ``_remote_attach`` table in the extract (legacy / non-BQ
            extract, e.g. a future "bigquery" name collision with a local
            connector)
          - existing url matches overlay (no drift)
        """
        bq_extract = extracts_dir / "bigquery" / "extract.duckdb"
        if not bq_extract.exists():
            return
        try:
            from app.instance_config import get_value
        except Exception:
            return
        overlay_project = (get_value("data_source", "bigquery", "project") or "").strip()
        if not overlay_project:
            return
        # Read-only handle, separate connection — orchestrator's rebuild
        # connection is per-call and hasn't ATTACHed extracts yet at
        # this pre-pass point, so this won't fight a file lock.
        try:
            ro = _open_duckdb(str(bq_extract), read_only=True)
        except Exception:
            return
        try:
            row = ro.execute("SELECT url FROM _remote_attach WHERE alias='bq'").fetchone()
        except Exception:
            row = None
        finally:
            try:
                ro.close()
            except Exception:
                pass
        if not row or not row[0]:
            return
        current_url = row[0]
        expected_url = f"project={overlay_project}"
        if current_url == expected_url:
            return
        logger.info(
            "BQ remote_attach drift detected: extract.duckdb has %r, "
            "overlay has %r — regenerating extract via "
            "rebuild_from_registry()",
            current_url,
            expected_url,
        )
        try:
            from connectors.bigquery.extractor import rebuild_from_registry

            result = rebuild_from_registry()
            logger.info(
                "BQ remote_attach drift sync: regenerated extract — tables_registered=%s errors=%s",
                result.get("tables_registered"),
                len(result.get("errors", [])),
            )
        except Exception as e:
            logger.warning(
                "BQ remote_attach drift sync: rebuild_from_registry() "
                "failed: %s — extract.duckdb still points at %r, queries "
                "will fail until next manual sync",
                e,
                current_url,
            )

    def _scan_meta_pairs(self, extracts_dir: Path) -> tuple:
        """Read every connector's `_meta` and return (pairs, clean) where:

        - ``pairs`` — list of (source_name, table_name) tuples successfully
          gathered from `_meta`.
        - ``clean`` — True iff every source's pre-scan succeeded. False if
          any source's `_meta` couldn't be read (transient I/O, mid-write,
          missing/corrupt extract.duckdb).

        Used by view_ownership.reconcile to release stale claims before
        the main rebuild loop tries to claim new names. The ``clean`` flag
        guards against a correctness bug: if source B's pre-scan fails
        and we naively reconcile against an incomplete `pairs` list, B's
        prior ownership is dropped, and another source could claim B's
        name in the same rebuild — a silent overwrite, exactly what
        Group C is meant to prevent. Callers MUST skip reconcile when
        ``clean`` is False; per-row claim-time collision detection still
        catches actual collisions.
        """
        pairs: list[tuple] = []
        clean = True
        for ext_dir in sorted(extracts_dir.iterdir()):
            if not ext_dir.is_dir():
                continue
            db_file = ext_dir / "extract.duckdb"
            if not db_file.exists():
                continue
            if not _validate_identifier(ext_dir.name, "source_name"):
                continue
            try:
                ro_conn = _open_duckdb(str(db_file), read_only=True)
                try:
                    rows = ro_conn.execute("SELECT table_name FROM _meta").fetchall()
                    for (table_name,) in rows:
                        if _validate_identifier(table_name, "table_name"):
                            pairs.append((ext_dir.name, table_name))
                finally:
                    ro_conn.close()
            except Exception as e:
                logger.warning(
                    "scan_meta_pairs: failed to read %s (%s) — "
                    "skipping reconcile this rebuild to avoid releasing "
                    "ownerships prematurely",
                    ext_dir.name,
                    e,
                )
                clean = False
        return pairs, clean

    def _do_rebuild(self) -> dict[str, list[str]]:
        extracts_dir = _get_extracts_dir()
        if not extracts_dir.exists():
            logger.warning("Extracts directory %s does not exist", extracts_dir)
            return {}

        # Pre-pass: detect drift between extract.duckdb _remote_attach.url
        # (where the orchestrator's ATTACH path will read the BQ project
        # from) and the overlay's data_source.bigquery.project (the
        # writable source of truth, edited via admin /server-config). If
        # they differ, regenerate the BQ extract so the new project
        # propagates into views before we run the main rebuild loop.
        # No-op when there is no BQ extract or no overlay project. See
        # issue #343 for the operational hazard this closes (admin
        # changes project in the UI, extract.duckdb stays stale, all
        # remote queries fail with "Dataset not found in <old project>").
        try:
            self._sync_bq_remote_attach_with_overlay(extracts_dir)
        except Exception as e:
            # Defensive: drift sync is a best-effort safety net. A failure
            # here must not block the rest of the rebuild — the worst
            # case is the same stale-extract failure mode the sync was
            # trying to prevent, which the operator can still resolve
            # manually via /admin/sync trigger.
            logger.warning(
                "BQ remote_attach drift sync failed: %s — continuing with "
                "existing extract.duckdb (queries may fail until next "
                "manual sync if project drifted)",
                e,
            )

        # Issue #81 Group C — load view ownership map from system DB so we
        # can detect cross-connector view-name collisions during this
        # rebuild and refuse to silently overwrite a previously-claimed
        # name. The map is kept in system.duckdb (analytics.duckdb is
        # rebuilt fresh each time and would not survive).
        # Backend-aware: view ownership lives in system state (Postgres on a
        # PG instance) — use the factory, not a raw DuckDB conn.
        from src.repositories import view_ownership_repo

        view_repo = None
        try:
            view_repo = view_ownership_repo()
            # Pre-scan every connector's _meta so we can run the reconcile
            # pass BEFORE claims are evaluated. This makes "owner stopped
            # publishing → name freed → another source can claim" work in
            # the SAME rebuild rather than requiring two consecutive runs.
            #
            # Correctness: only reconcile when EVERY source's pre-scan
            # succeeded. Otherwise a transient I/O failure on source B
            # would drop B's prior ownership and let another source steal
            # B's name — silent overwrite, exactly the bug Group C
            # prevents. Per-row claim-time collision detection still
            # catches actual collisions even without reconcile this run.
            current_pairs, pre_scan_clean = self._scan_meta_pairs(extracts_dir)
            if pre_scan_clean:
                view_repo.reconcile(current_pairs)
            else:
                logger.warning(
                    "view_ownership: skipping reconcile this rebuild — "
                    "pre-scan was incomplete; renamed tables will release "
                    "their names on the next clean rebuild instead"
                )
            existing_owners = view_repo.get_all()
        except Exception as e:
            logger.warning(
                "view_ownership pre-scan failed: %s — proceeding without collision detection",
                e,
            )
            existing_owners = {}
            view_repo = None

        # Track every (source, view) pair this rebuild successfully claims.
        claimed_pairs: list[tuple] = []

        result = {}
        # Write to temp file then rename — avoids lock conflict with query endpoint
        tmp_path = self._db_path + ".tmp"
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()
        conn = _open_duckdb(tmp_path)
        try:
            # Detach any previously attached databases (except main and temp)
            attached = [
                row[0]
                for row in conn.execute(
                    "SELECT database_name FROM duckdb_databases() "
                    "WHERE database_name NOT IN ('memory', 'system', 'temp')"
                ).fetchall()
            ]
            for db_name in attached:
                if db_name != Path(self._db_path).stem:
                    try:
                        conn.execute(f"DETACH {db_name}")
                    except Exception:
                        pass

            for ext_dir in sorted(extracts_dir.iterdir()):
                if not ext_dir.is_dir():
                    continue
                db_file = ext_dir / "extract.duckdb"
                if not db_file.exists():
                    logger.debug("Skipping %s — no extract.duckdb", ext_dir.name)
                    continue

                if not _validate_identifier(ext_dir.name, "source_name"):
                    continue

                tables = self._attach_and_create_views(
                    conn,
                    ext_dir.name,
                    str(db_file),
                    existing_owners=existing_owners,
                    claimed_pairs=claimed_pairs,
                    view_repo=view_repo,
                )
                if tables:
                    result[ext_dir.name] = tables
                    logger.info("Attached %s: %d tables", ext_dir.name, len(tables))

            # No end-of-rebuild reconcile: the pre-scan reconcile above
            # already released stale ownerships using a complete view of
            # every source's `_meta`. Reconciling again here against
            # `claimed_pairs` (which excludes refused collisions and any
            # source that failed to attach) would incorrectly drop the
            # legitimate prior owner of a name when its DB happens to be
            # transiently unreadable. See test
            # `test_pre_scan_failure_does_not_release_ownership` for the
            # contract.
        finally:
            conn.execute("CHECKPOINT")
            conn.close()

        # Atomic swap: replace analytics.duckdb with new version
        _atomic_swap_db(tmp_path, self._db_path)

        return result

    def _do_rebuild_source(self, source_name: str) -> list[str]:
        """Rebuild views for a single source by doing a full rebuild.

        A full rebuild is necessary because the analytics DB is created fresh
        each time (temp file + atomic swap). Rebuilding only one source would
        destroy views from all other sources.
        """
        extracts_dir = _get_extracts_dir()
        db_file = extracts_dir / source_name / "extract.duckdb"
        if not db_file.exists():
            logger.warning("No extract.duckdb for source %s", source_name)
            return []

        result = self._do_rebuild()
        return result.get(source_name, [])

    def _do_rebuild_ducklake(self, only_source: str | None = None) -> dict[str, list[str]]:
        """DuckLake copy-ingest rebuild — the ``analytics.backend: ducklake``
        counterpart to :meth:`_do_rebuild` / :meth:`_do_rebuild_source`.

        Unlike the legacy path (fresh temp file + atomic swap of the
        WHOLE analytics DB), the DuckLake catalog is long-lived and each
        source's data lives in its own catalog schema
        (``lake."<source>"``). When ``only_source`` is set, this method
        touches ONLY that source's schema and its own master views —
        every other source's DuckLake tables and snapshots are left
        completely alone. That is the incremental improvement over the
        legacy full-rebuild-on-webhook path (wave-2G plan, task 3): a
        Jira webhook no longer has to re-ingest Keboola/BigQuery tables
        just to refresh one Jira table.

        Runs the exact same pre-pass + view-ownership reconcile/claim
        machinery as :meth:`_do_rebuild` (BQ ``_remote_attach`` drift
        sync, ``_scan_meta_pairs`` + ``view_ownership_repo.reconcile``)
        so cross-connector view-name collision semantics are identical
        regardless of backend — the reconcile pass only touches
        system-state bookkeeping (which source owns which view name),
        never DuckLake data, so running it in full even for a per-source
        rebuild does not violate the "other sources untouched" property
        above.

        ``sync_state`` hash/manifest bookkeeping (:meth:`_update_sync_state`)
        is reused unchanged — it describes the on-disk extracts artifact,
        not the analytics backend, so it stays identical between legacy
        and ducklake.
        """
        extracts_dir = _get_extracts_dir()
        if not extracts_dir.exists():
            logger.warning("Extracts directory %s does not exist", extracts_dir)
            return {}

        # Same BQ _remote_attach drift pre-pass as the legacy rebuild —
        # see _sync_bq_remote_attach_with_overlay's docstring (issue #343).
        try:
            self._sync_bq_remote_attach_with_overlay(extracts_dir)
        except Exception as e:
            logger.warning(
                "BQ remote_attach drift sync failed: %s — continuing with "
                "existing extract.duckdb (queries may fail until next "
                "manual sync if project drifted)",
                e,
            )

        # Same view-ownership pre-scan/reconcile as the legacy rebuild
        # (issue #81 Group C) — reused as-is, same repo, same collision
        # semantics. See _scan_meta_pairs's docstring for why reconcile
        # is skipped when the pre-scan is incomplete.
        from src.repositories import view_ownership_repo

        view_repo = None
        try:
            view_repo = view_ownership_repo()
            current_pairs, pre_scan_clean = self._scan_meta_pairs(extracts_dir)
            if pre_scan_clean:
                view_repo.reconcile(current_pairs)
            else:
                logger.warning(
                    "view_ownership: skipping reconcile this rebuild — "
                    "pre-scan was incomplete; renamed tables will release "
                    "their names on the next clean rebuild instead"
                )
            existing_owners = view_repo.get_all()
        except Exception as e:
            logger.warning(
                "view_ownership pre-scan failed: %s — proceeding without collision detection",
                e,
            )
            existing_owners = {}
            view_repo = None

        claimed_pairs: list[tuple] = []
        result: dict[str, list[str]] = {}

        from src.ducklake_session import get_ducklake_write

        write_conn = get_ducklake_write()
        try:
            for ext_dir in sorted(extracts_dir.iterdir()):
                if not ext_dir.is_dir():
                    continue
                if only_source is not None and ext_dir.name != only_source:
                    continue
                db_file = ext_dir / "extract.duckdb"
                if not db_file.exists():
                    logger.debug("Skipping %s — no extract.duckdb", ext_dir.name)
                    continue
                if not _validate_identifier(ext_dir.name, "source_name"):
                    continue

                tables = self._ingest_source_ducklake(
                    write_conn,
                    ext_dir.name,
                    db_file,
                    existing_owners=existing_owners,
                    claimed_pairs=claimed_pairs,
                    view_repo=view_repo,
                )
                if tables:
                    result[ext_dir.name] = tables
                    logger.info("DuckLake ingested %s: %d tables", ext_dir.name, len(tables))

            # Remote-mode (query_mode='remote') wrapper views are owned by
            # the WRITER, created here once per rebuild — NOT on every reader
            # request. A ``CREATE OR REPLACE VIEW lake.main.<name>`` commits a
            # new DuckLake catalog snapshot on every call even when the view
            # body is unchanged (verified vs DuckDB 1.5.2: 5 identical calls →
            # +5 snapshots); doing it per-request from the api-role reader
            # would be unbounded catalog write-amplification onto the shared
            # PG catalog under concurrent load, defeating read/write plane
            # separation. See _sync_ducklake_remote_views.
            try:
                self._sync_ducklake_remote_views(
                    write_conn,
                    extracts_dir,
                    only_source=only_source,
                    claimed_pairs=claimed_pairs,
                    view_repo=view_repo,
                    local_result=result,
                )
            except Exception as e:
                logger.warning("DuckLake remote-view sync failed: %s", e)
        finally:
            # get_ducklake_write() hands back a cursor-per-caller (see
            # src/ducklake_session.py) — .close() only drops this cursor,
            # never the underlying long-lived writer connection/attach.
            write_conn.close()

        return result

    def _ingest_source_ducklake(
        self,
        write_conn: duckdb.DuckDBPyConnection,
        source_name: str,
        db_file: Path,
        existing_owners: dict[str, str] | None = None,
        claimed_pairs: list[tuple] | None = None,
        view_repo=None,
    ) -> list[str]:
        """Copy-ingest one source's LOCAL/MATERIALIZED tables into
        ``lake."<source_name>"`` and point the matching master views
        (``lake."main"."<table>"``) at them.

        Mirrors :meth:`_attach_and_create_views`'s ``_meta`` iteration
        and view-ownership claim semantics exactly, but reads each
        table via a throwaway READ-ONLY connection opened directly on
        ``db_file`` (``SELECT * FROM "<table>"`` — the identical
        expression the legacy master view uses,
        ``SELECT * FROM <source>.<table>``) rather than ATTACHing the
        extract onto the long-lived DuckLake writer connection.

        That choice is deliberate, not cosmetic:
        :func:`src.ducklake_session.get_ducklake_write` may share its
        *physical* connection with
        :func:`src.ducklake_session.get_ducklake_read` when the
        resolved catalog is a DuckDB-file target (see that module's
        "same-process file-catalog constraint" docstring), and the
        reader's own attach loop
        (:func:`src.ducklake_session._attach_remote_read_sources`, via
        :func:`src.ducklake_session._ensure_remote_extract_attach`)
        persistently ATTACHes each *remote-mode* extract source — a
        source with at least one ``query_mode='remote'`` table_registry
        row, not every extract source — under its directory-name alias
        on that shared connection; a second ATTACH of the same alias
        from here would collide with it for any source that mixes local
        and remote tables. Reading through a fully independent
        connection and streaming the result batch-by-batch through an
        Arrow ``RecordBatchReader`` (DuckDB's replacement scan can
        reference a local Python variable from either connection's
        ``execute()`` call) sidesteps that collision regardless of a
        source's local/remote mix, and is still pure copy-ingest:
        nothing is ever attached or added as a DuckLake data file
        directly from the extract's own mutable parquet paths.

        Remote-mode tables (``query_mode='remote'``) are NOT
        copy-ingested — there is no local parquet backing them, only a
        view over an externally-ATTACHed extension (BigQuery, Keboola
        direct-bucket). Their name is still claimed in
        ``view_ownership`` (so a remote table's name keeps participating
        in cross-connector collision detection exactly like every other
        table), but no ``lake`` schema object is created for it and it
        is not included in this method's returned table list — task 4's
        reader path resolves remote tables as session views built
        directly from ``table_registry`` at query time instead, keeping
        the DuckLake catalog itself free of any BigQuery/foreign-catalog
        coupling.
        """
        if existing_owners is None:
            existing_owners = {}
        tables: list[str] = []
        meta_rows: list = []

        try:
            ro = _open_duckdb(str(db_file), read_only=True)
        except Exception as e:
            logger.error("Failed to open %s for ducklake ingest: %s", db_file, e)
            return tables

        try:
            try:
                meta_rows = ro.execute("SELECT table_name, rows, size_bytes, query_mode FROM _meta").fetchall()
            except Exception as e:
                logger.error("Failed to read _meta for %s: %s", source_name, e)
                return tables

            schema_created = False

            for table_name, rows, size_bytes, query_mode in meta_rows:
                if not _validate_identifier(table_name, "table_name"):
                    continue

                if query_mode == "remote":
                    # Remote-mode tables have no local parquet — their
                    # "inner object" is the view over an externally-ATTACHed
                    # extension (BigQuery, Keboola direct-bucket), which we
                    # don't attempt to read here at all. There is nothing to
                    # probe for readability, so they claim their name
                    # unconditionally, same as the legacy path's Group C
                    # first-come-first-served claim.
                    if view_repo is not None:
                        if not view_repo.claim(table_name, source_name):
                            prior_owner = view_repo.get_owner(table_name) or existing_owners.get(
                                table_name, "<unknown>"
                            )
                            logger.error(
                                "view_ownership collision: %s already owns view %r; "
                                "%s.%s will NOT be exposed. Rename `name` in the "
                                "table_registry on one side to resolve.",
                                prior_owner,
                                table_name,
                                source_name,
                                table_name,
                            )
                            continue
                        if claimed_pairs is not None:
                            claimed_pairs.append((source_name, table_name))

                    logger.info(
                        "DuckLake ingest: %s.%s is query_mode='remote' — "
                        "skipping copy-ingest (no local parquet); the reader "
                        "resolves it from table_registry at query time",
                        source_name,
                        table_name,
                    )
                    continue

                # Determine whether this table has a readable inner object
                # BEFORE claiming ownership of its name — mirrors legacy's
                # `if table_name not in inner_objects: continue`, which runs
                # before its own `view_repo.claim()` call. A `_meta` row
                # without a backing object (e.g. keboola
                # use_extension=False) must never claim the name: doing so
                # would let it win a collision against a DIFFERENT source's
                # real table of the same name and block that source's
                # legitimate row from ever being exposed — a divergence
                # from legacy semantics caught in review.
                try:
                    # arrow_batches looks unused to static analysis, but
                    # DuckDB's replacement scan resolves the bare
                    # `arrow_batches` name in the `write_conn.execute(...)`
                    # FROM-clause below against this calling frame's locals —
                    # it IS the data transfer from the read-only extract
                    # connection to the DuckLake writer connection. See this
                    # method's docstring.
                    #
                    # `.to_arrow_reader(batch_size=...)` returns a
                    # `pyarrow.RecordBatchReader`: `write_conn` pulls it
                    # batch-by-batch (``_DUCKLAKE_INGEST_BATCH_SIZE`` rows at
                    # a time) as it executes the CTAS below, so at most one
                    # batch is ever materialized in Python-process memory at
                    # once. This is deliberate and load-bearing — the whole
                    # point of this data plane is to keep a runaway ingest
                    # from OOM-killing the process (see
                    # docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md).
                    # Do NOT change this to `.to_arrow_table()`, `.fetchall()`,
                    # or any other form that materializes the full result
                    # before handing it to `write_conn` — those buffer the
                    # entire table in memory and will OOM on large analytics
                    # tables. `tests/test_orchestrator_ducklake.py`'s
                    # bounded-memory test exists specifically to catch that
                    # regression.
                    arrow_batches = ro.sql(f"SELECT * FROM {quote_ident(table_name)}").to_arrow_reader(  # noqa: F841
                        batch_size=_DUCKLAKE_INGEST_BATCH_SIZE
                    )
                except Exception as e:
                    # Mirrors the legacy "_meta row without inner object"
                    # skip (e.g. keboola use_extension=False path) —
                    # reactive here (try/except) rather than a
                    # precomputed inner-objects set, since we're not
                    # holding an ATTACH to introspect information_schema
                    # against. Deliberately does NOT claim the name (see
                    # comment above the try block).
                    logger.info(
                        "Skipping ducklake ingest for %s.%s — no inner object (%s)",
                        source_name,
                        table_name,
                        e,
                    )
                    continue

                # Issue #81 Group C — same first-come-first-served claim as
                # the legacy path. Only reached once we've confirmed above
                # that this table actually has data to ingest.
                if view_repo is not None:
                    if not view_repo.claim(table_name, source_name):
                        prior_owner = view_repo.get_owner(table_name) or existing_owners.get(table_name, "<unknown>")
                        logger.error(
                            "view_ownership collision: %s already owns view %r; "
                            "%s.%s will NOT be exposed. Rename `name` in the "
                            "table_registry on one side to resolve.",
                            prior_owner,
                            table_name,
                            source_name,
                            table_name,
                        )
                        continue
                    if claimed_pairs is not None:
                        claimed_pairs.append((source_name, table_name))

                try:
                    if not schema_created:
                        # `source_name` is a SCHEMA name inside the `lake`
                        # catalog here, so it is quoted — unlike its other role
                        # in this module as a bare ATTACH alias (`{source_name}.`
                        # at :1006 and in _attach_and_create_views).
                        write_conn.execute(f"CREATE SCHEMA IF NOT EXISTS lake.{quote_ident(source_name)}")
                        schema_created = True
                    write_conn.execute(
                        f"CREATE OR REPLACE TABLE lake.{quote_ident(source_name)}.{quote_ident(table_name)} "
                        f"AS SELECT * FROM arrow_batches"
                    )
                    write_conn.execute(
                        f'CREATE OR REPLACE VIEW lake."main".{quote_ident(table_name)} AS '
                        f"SELECT * FROM lake.{quote_ident(source_name)}.{quote_ident(table_name)}"
                    )
                    tables.append(table_name)
                except Exception as e:
                    logger.error(
                        "DuckLake copy-ingest failed for %s.%s: %s",
                        source_name,
                        table_name,
                        e,
                    )
        finally:
            try:
                ro.close()
            except Exception:
                pass

        # sync_state describes the extracts artifact, not the analytics
        # backend — same call, same contract, as the legacy rebuild path.
        self._update_sync_state(meta_rows, source_name)
        return tables

    def _sync_ducklake_remote_views(
        self,
        write_conn: duckdb.DuckDBPyConnection,
        extracts_dir: Path,
        *,
        only_source: str | None,
        claimed_pairs: list[tuple] | None,
        view_repo=None,
        local_result: dict[str, list[str]] | None = None,
    ) -> None:
        """Create the ``lake."main"."<name>"`` wrapper view for every
        ``query_mode='remote'`` table_registry row, and reconcile away
        stale wrapper views for de-registered/renamed remote (and, on a
        full rebuild, local) tables.

        This is the WRITER-side owner of remote wrapper views. The reader
        path used to (re)create these on every request, which committed a
        fresh DuckLake catalog snapshot per call regardless of whether the
        view changed — unbounded write-amplification onto the shared
        catalog from the read plane. Creating them here is bounded (once
        per rebuild).

        Each wrapper points at the extract source's own already-correct
        inner object (``"<source>"."<name>"`` — exactly what the legacy
        master view references and what the reader wrapper used to
        reference), so all BigQuery/Keboola addressing (bq_fqn overrides,
        BASE TABLE vs VIEW ``bigquery_query()`` wrapping) stays owned by
        the extractor. DuckLake views are NOT late-bound (verified vs
        DuckDB 1.5.2 — ``CREATE VIEW`` referencing an unattached alias
        raises a Catalog Error), so the remote extract sources and their
        external extensions (BigQuery, etc.) are ATTACHed here first,
        reusing the same helpers the reader uses.

        Ownership: a remote row's name is claimed in
        :meth:`_ingest_source_ducklake` (first-come-first-served, issue #81
        Group C). Only the (source, name) pairs that WON their claim this
        rebuild (``claimed_pairs``) get a wrapper view, so a name that lost
        a cross-connector collision is not silently exposed by the losing
        source.
        """
        from src.db import _reattach_remote_extensions
        from src.ducklake_session import _ensure_remote_extract_attach, _remote_registry_rows_by_source

        remote_by_source = _remote_registry_rows_by_source()

        # Attach remote extract sources + external extensions so the view
        # bodies bind at CREATE time (views are not late-bound). Both
        # helpers are idempotent (skip already-attached) and only touch
        # sources with a remote-mode table_registry row — never a
        # live-in-place connector like Jira.
        _ensure_remote_extract_attach(write_conn, remote_by_source)
        _reattach_remote_extensions(write_conn, extracts_dir)

        owned = set(claimed_pairs or [])
        created_remote_names: set[str] = set()
        for source_name, rows in remote_by_source.items():
            if only_source is not None and source_name != only_source:
                continue
            if not _validate_identifier(source_name, "source_name"):
                continue
            for row in rows:
                name = row.get("name") or ""
                if not _validate_identifier(name, "table_name"):
                    continue
                # Respect the view-ownership claim made during ingest — only
                # the winner exposes the name. When view_repo is unavailable
                # (reconcile skipped), fall back to best-effort create.
                if view_repo is not None and (source_name, name) not in owned:
                    continue
                try:
                    write_conn.execute(
                        # `source_name` stays BARE — here it is the ATTACH alias,
                        # not a schema inside `lake`. See the CREATE SCHEMA above
                        # for the other role.
                        f'CREATE OR REPLACE VIEW lake."main".{quote_ident(name)} AS '
                        f"SELECT * FROM {source_name}.{quote_ident(name)}"
                    )
                    created_remote_names.add(name)
                except Exception as e:
                    logger.warning(
                        "DuckLake remote view create failed for %s.%s: %s",
                        source_name,
                        name,
                        e,
                    )

        # Reconcile: drop stale lake.main wrapper views whose backing
        # table_registry row is gone or renamed. Only on a FULL rebuild —
        # a per-source rebuild must leave every other source's views alone.
        if only_source is None:
            expected: set[str] = set(created_remote_names)
            for tbls in (local_result or {}).values():
                expected.update(tbls)
            self._drop_stale_ducklake_main_views(write_conn, expected)

    def _drop_stale_ducklake_main_views(self, write_conn: duckdb.DuckDBPyConnection, expected_names: set) -> None:
        """Drop any ``lake.main`` VIEW whose name is not in *expected_names*.

        The long-lived DuckLake catalog (unlike the legacy fresh-temp-file
        rebuild) accumulates wrapper views forever unless something removes
        them, so a table de-registered or renamed in table_registry leaves
        a dangling ``lake.main`` view resolving stale/erroring data. This
        reconcile — run only on a full rebuild — collects the expected set
        (all local master-view names + all remote wrapper names created
        this pass) and drops the rest.
        """
        try:
            rows = write_conn.execute(
                "SELECT table_name FROM information_schema.views WHERE table_catalog='lake' AND table_schema='main'"
            ).fetchall()
        except Exception as e:
            logger.debug("DuckLake reconcile: could not list lake.main views: %s", e)
            return
        for (view_name,) in rows:
            if view_name in expected_names or not _validate_identifier(view_name, "table_name"):
                continue
            try:
                write_conn.execute(f'DROP VIEW IF EXISTS lake."main".{quote_ident(view_name)}')
                logger.info("DuckLake reconcile: dropped stale lake.main view %s", view_name)
            except Exception as e:
                logger.warning("DuckLake reconcile: could not drop stale lake.main view %s: %s", view_name, e)

    def _attach_and_create_views(
        self,
        conn: duckdb.DuckDBPyConnection,
        source_name: str,
        db_path: str,
        existing_owners: dict[str, str] | None = None,
        claimed_pairs: list[tuple] | None = None,
        view_repo=None,
    ) -> list[str]:
        """ATTACH extract.duckdb, read _meta, create views in master.

        Issue #81 Group C — when ``existing_owners`` and ``view_repo`` are
        provided, the orchestrator checks for cross-connector view-name
        collisions and refuses to overwrite a name owned by another source.
        ``claimed_pairs`` accumulates the (source, view) tuples this
        rebuild successfully claims; the caller uses it for end-of-rebuild
        reconcile.
        """
        if existing_owners is None:
            existing_owners = {}
        tables = []
        try:
            conn.execute(f"ATTACH '{db_path}' AS {source_name} (READ_ONLY)")

            # Re-ATTACH external extensions needed by remote views
            self._attach_remote_extensions(conn, source_name)

            # Read _meta to know what's available
            meta_rows = conn.execute(
                f"SELECT table_name, rows, size_bytes, query_mode FROM {source_name}._meta"
            ).fetchall()

            # Pre-fetch the set of names that actually exist as views/tables in
            # the attached extract.duckdb. Most connectors emit a `_meta` row
            # alongside an inner view per registered name; the keboola
            # extractor with `use_extension=False` (and other connectors)
            # may insert `_meta` rows whose inner view doesn't exist yet —
            # skip those to avoid creating a master view that would resolve
            # to nothing.
            inner_objects = {
                row[0]
                for row in conn.execute(
                    f"SELECT table_name FROM information_schema.tables WHERE table_catalog='{source_name}'"
                ).fetchall()
            }

            for table_name, rows, size_bytes, query_mode in meta_rows:
                if not _validate_identifier(table_name, "table_name"):
                    continue
                if table_name not in inner_objects:
                    # `_meta` row without an inner object. Post-#160 the
                    # BigQuery extractor no longer emits these for unsupported
                    # entity types (it skips both the view AND the _meta row),
                    # so this branch fires for the keboola use_extension=False
                    # path and any future connector that splits writes across
                    # commits. Skip master-view creation; subsequent rows
                    # continue normally.
                    logger.info(
                        "Skipping master view for %s.%s — no inner object",
                        source_name,
                        table_name,
                    )
                    continue

                # Issue #81 Group C — refuse cross-connector collisions.
                # First-come-first-served: the source already in
                # view_ownership keeps the name; any other source that
                # tries to claim it gets logged + skipped until the
                # operator renames one side. Re-claim by the same source
                # is fine (idempotent rebuild).
                if view_repo is not None:
                    if not view_repo.claim(table_name, source_name):
                        prior_owner = view_repo.get_owner(table_name) or existing_owners.get(table_name, "<unknown>")
                        logger.error(
                            "view_ownership collision: %s already owns view %r; "
                            "%s.%s will NOT be exposed. Rename `name` in the "
                            "table_registry on one side to resolve.",
                            prior_owner,
                            table_name,
                            source_name,
                            table_name,
                        )
                        continue
                    if claimed_pairs is not None:
                        claimed_pairs.append((source_name, table_name))

                try:
                    conn.execute(
                        # `source_name` is the ATTACH alias and stays BARE, matching
                        # every other use of it in this module (`ATTACH … AS
                        # {source_name}`, `FROM {source_name}._meta`). Quoting it
                        # resolves identically in DuckDB, but the sweep that
                        # introduced quotes here was overreach — the alias is not
                        # an identifier this module owns.
                        f"CREATE OR REPLACE VIEW {quote_ident(table_name)} AS "
                        f"SELECT * FROM {source_name}.{quote_ident(table_name)}"
                    )
                    tables.append(table_name)
                except Exception as e:
                    # Per-row catch so one bad row doesn't drop the rest of
                    # the source's master views from the rebuild.
                    logger.error(
                        "Failed to create master view for %s.%s: %s",
                        source_name,
                        table_name,
                        e,
                    )

            # Filesystem-fallback master views (0.41.0). The 0.40.0 fix in
            # `materialize_query` tries to register the parquet in
            # `extract.duckdb`'s `_meta` + inner view, but the open-as-
            # second-write-handle from the same uvicorn process collides
            # with the existing read-only ATTACH that `rebuild()` itself
            # holds (`Unique file handle conflict: Cannot attach "extract"
            # — already attached by database "<source>"`). The 0.40.0
            # helper logs a WARNING and falls through, parquet is
            # canonical, but the master view never appears via the meta
            # path. This second pass scans `<extract_dir>/data/*.parquet`
            # directly and creates a master view via `read_parquet()` for
            # any parquet that didn't already get one through the meta
            # path. Decoupled from materialize_query's open-handle race;
            # robust against any registration drift between materialize
            # and rebuild.
            try:
                extracts_dir = _get_extracts_dir()
            except Exception:
                extracts_dir = None
            if extracts_dir is not None:
                data_dir = extracts_dir / source_name / "data"
                if data_dir.exists():
                    # Resolve the set of registry-known table_ids for this
                    # source. The fallback is a master-view recovery path
                    # for parquets that materialize_query wrote but
                    # couldn't register in `_meta`; an **orphan** parquet
                    # (registry row deleted by `DELETE /api/admin/registry`
                    # but parquet not yet cleaned up) must NOT get a
                    # master view — that would resurrect a deleted table.
                    # Pre-existing test `test_orchestrator_skips_orphan_
                    # parquet_in_extracts` pins this contract.
                    registered_ids: set | None = None
                    try:
                        # Backend-aware: read the registry through the factory
                        # (Postgres on a PG instance) — a raw DuckDB conn would
                        # see an empty registry and skip materialized parquets.
                        from src.repositories import table_registry_repo

                        rows = table_registry_repo().list_all()
                        # Match parquet stems against registry rows for
                        # THIS source where query_mode='materialized'.
                        # The parquet filename is keyed by registry
                        # `name` (per `_run_materialized_pass` /
                        # `materialize_query` convention).
                        registered_ids = {
                            str(r.get("name"))
                            for r in rows
                            if (r.get("source_type") or "") == source_name
                            and (r.get("query_mode") or "") == "materialized"
                            and r.get("name")
                        }
                    except Exception as e:
                        # No registry access (test fixture, transient DB
                        # error) — skip the fallback rather than risk
                        # exposing orphan parquets.
                        logger.warning(
                            "filesystem-fallback: registry read failed (%s); "
                            "skipping fallback scan for %s — orphan parquets "
                            "from a prior DELETE could otherwise be exposed.",
                            e,
                            source_name,
                        )
                        registered_ids = None

                    if registered_ids is not None:
                        already_created = set(tables)
                        for parquet_path in sorted(data_dir.glob("*.parquet")):
                            table_id = parquet_path.stem
                            if not _validate_identifier(table_id, "fs_fallback table_id"):
                                continue
                            if table_id in already_created:
                                continue
                            # Only register parquets that have a live
                            # materialized registry row. Orphans skip.
                            if table_id not in registered_ids:
                                logger.debug(
                                    "filesystem-fallback: skipping orphan parquet %s/%s (no registry row)",
                                    source_name,
                                    table_id,
                                )
                                continue
                            # view_repo claim — same first-come-first-served
                            # rule as the meta-path branch above.
                            if view_repo is not None:
                                if not view_repo.claim(table_id, source_name):
                                    prior_owner = view_repo.get_owner(table_id) or existing_owners.get(
                                        table_id, "<unknown>"
                                    )
                                    logger.error(
                                        "view_ownership collision: %s already owns view %r; "
                                        "%s.%s (filesystem-fallback) will NOT be exposed.",
                                        prior_owner,
                                        table_id,
                                        source_name,
                                        table_id,
                                    )
                                    continue
                                if claimed_pairs is not None:
                                    claimed_pairs.append((source_name, table_id))
                            try:
                                safe_path = str(parquet_path).replace("'", "''")
                                conn.execute(
                                    f"CREATE OR REPLACE VIEW {quote_ident(table_id)} AS "
                                    f"SELECT * FROM read_parquet('{safe_path}')"
                                )
                                tables.append(table_id)
                                logger.info(
                                    "filesystem-fallback master view created: "
                                    "%s/%s (parquet at %s) — meta row was missing",
                                    source_name,
                                    table_id,
                                    parquet_path,
                                )
                                # The fallback path publishes real, queryable
                                # data, so it must also record success in
                                # sync_state — it is the only rebuild path
                                # that otherwise skips the write, leaving any
                                # stale set_error() row (from the failed run
                                # that necessitated the fallback) shadowing a
                                # healthy table in the admin UI and manifest.
                                self._record_fallback_sync_state(conn, table_id, parquet_path)
                            except Exception as e:
                                logger.error(
                                    "filesystem-fallback master view failed for %s/%s: %s",
                                    source_name,
                                    table_id,
                                    e,
                                )

            # Update sync_state in system DB
            self._update_sync_state(meta_rows, source_name)

        except Exception as e:
            logger.error("Failed to attach %s: %s", source_name, e)

        return tables

    def _attach_remote_extensions(self, conn: duckdb.DuckDBPyConnection, source_name: str) -> None:
        """Read _remote_attach from extract.duckdb and ATTACH external sources."""
        try:
            # DuckDB attached-DB layout: ATTACH 'extract.duckdb' AS <alias>
            # exposes information_schema.tables with table_catalog=<alias>
            # and table_schema='main'. The earlier draft used
            # table_schema=<alias> here, which never matched and made
            # _attach_remote_extensions a silent no-op for every
            # connector — defeating the entire Group A hardening in
            # production. db.py:_reattach_remote_extensions already used
            # the correct column; this aligns the rebuild path.
            tables = conn.execute(
                f"SELECT table_name FROM information_schema.tables "
                f"WHERE table_catalog='{source_name}' AND table_name='_remote_attach'"
            ).fetchall()
            if not tables:
                return
        except Exception:
            return

        rows = conn.execute(f"SELECT alias, extension, url, token_env FROM {source_name}._remote_attach").fetchall()

        # Local import: the Databricks connector pulls in `requests` at module
        # scope, and the orchestrator must stay importable on an instance that
        # has no Databricks configured at all.
        from connectors.databricks.attach import UC_EXTENSION, attach_unity_catalog
        from connectors.snowflake.attach import (
            SF_EXTENSION,
            attach_snowflake,
            install_snowflake_adbc_driver,
        )

        for alias, extension, url, token_env in rows:
            # Identifier sanity (defense against weird input). The hard
            # security boundary is the allowlist a few lines down.
            if not _validate_identifier(alias, "remote_attach alias"):
                continue
            if not _validate_identifier(extension, "remote_attach extension"):
                continue

            # #81 Group A.1 — extension allowlist. The connector does NOT
            # get to pick what extensions the orchestrator loads.
            if not is_extension_allowed(extension):
                logger.error(
                    "Remote attach %s: extension %r is not in the allowlist; refusing. "
                    "Override via AGNES_REMOTE_ATTACH_EXTENSIONS if intended.",
                    alias,
                    extension,
                )
                continue

            # #81 Group A.2 — token-env hard allowlist. Refuses well-known
            # runtime secrets (JWT_SECRET_KEY, OPENAI_API_KEY, …) that a
            # malicious connector might ask us to send to its server.
            if token_env and not is_token_env_allowed(token_env):
                logger.error(
                    "Remote attach %s: token_env %r is not in the allowlist; refusing. "
                    "Override via AGNES_REMOTE_ATTACH_TOKEN_ENVS if intended.",
                    alias,
                    token_env,
                )
                continue

            token = resolve_remote_attach_token(token_env)
            if token_env and not token:
                logger.warning("Remote attach %s: token_env %s not resolvable, skipping", alias, token_env)
                continue

            try:
                # Skip if already attached (e.g. multiple sources share same extension)
                attached = {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
                if alias in attached:
                    logger.debug("Remote source %s already attached", alias)
                    continue

                # #81 Group A.1 — built-ins LOAD only; community needs INSTALL+LOAD.
                # Snowflake's community extension also needs the ADBC driver shared
                # library on disk before it will load.
                if extension == SF_EXTENSION:
                    install_snowflake_adbc_driver()
                if is_builtin_extension(extension):
                    conn.execute(f"LOAD {extension};")
                else:
                    conn.execute(f"INSTALL {extension} FROM community; LOAD {extension};")
                # #81 Group A.3 — escape URL single-quotes (mirrors src/db.py).
                safe_url = escape_sql_string_literal(url)

                # BQ-specific: refresh token from GCE metadata, create session-scoped
                # secret before ATTACH. Empty token_env (set by the BQ extractor) is
                # the contract that signals "use built-in metadata path".
                if extension == "bigquery":
                    try:
                        bq_token = get_metadata_token()
                    except BQMetadataAuthError as e:
                        logger.error(
                            "Failed to fetch BQ metadata token for %s: %s — skipping ATTACH",
                            alias,
                            e,
                        )
                        continue
                    escaped = escape_sql_string_literal(bq_token)
                    secret_name = f"bq_secret_{alias}"
                    conn.execute(f"CREATE OR REPLACE SECRET {secret_name} (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
                    from connectors.bigquery.access import apply_bq_session_settings

                    apply_bq_session_settings(conn)
                    conn.execute(f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, READ_ONLY)")
                elif extension == UC_EXTENSION:
                    # Unity Catalog needs the PAT and the workspace endpoint
                    # together, which the generic TOKEN branch below cannot
                    # express. Same credential-egress rules apply — the host
                    # comes out of `url`, so `is_attach_host_allowed` reads the
                    # real workspace host with no special casing.
                    if not is_attach_host_allowed(url):
                        logger.error(
                            "Remote attach %s: url host %r is not in %s; refusing to send credential from %s.",
                            alias,
                            url,
                            "AGNES_REMOTE_ATTACH_HOST_ALLOWLIST",
                            token_env,
                        )
                        continue
                    if not attach_host_allowlist_configured():
                        logger.warning(
                            "Remote attach %s: sending credential (%s) to connector-chosen "
                            "url %r with no AGNES_REMOTE_ATTACH_HOST_ALLOWLIST configured — "
                            "set it in production to pin allowed hosts.",
                            alias,
                            token_env,
                            url,
                        )
                    attach_unity_catalog(conn, alias=alias, url=url, token=token)
                elif extension == SF_EXTENSION:
                    # Snowflake community extension: needs CREATE SECRET with ACCOUNT,
                    # USER, PASSWORD, DATABASE and WAREHOUSE, then ATTACH '' AS <alias>.
                    # Same credential-egress host allowlist rules as Unity Catalog.
                    if not is_attach_host_allowed(url):
                        logger.error(
                            "Remote attach %s: url host %r is not in %s; refusing to send credential from %s.",
                            alias,
                            url,
                            "AGNES_REMOTE_ATTACH_HOST_ALLOWLIST",
                            token_env,
                        )
                        continue
                    if not attach_host_allowlist_configured():
                        logger.warning(
                            "Remote attach %s: sending credential (%s) to connector-chosen "
                            "url %r with no AGNES_REMOTE_ATTACH_HOST_ALLOWLIST configured — "
                            "set it in production to pin allowed hosts.",
                            alias,
                            token_env,
                            url,
                        )
                    from connectors.snowflake.settings import resolve_snowflake_passphrase_for_token

                    passphrase = resolve_snowflake_passphrase_for_token(token_env)
                    attach_snowflake(conn, alias=alias, url=url, token=token, passphrase=passphrase)
                elif token:
                    # #F10 — never ship a real credential to a connector-chosen
                    # host that the operator has not approved.
                    if not is_attach_host_allowed(url):
                        logger.error(
                            "Remote attach %s: url host %r is not in %s; refusing to send credential from %s.",
                            alias,
                            url,
                            "AGNES_REMOTE_ATTACH_HOST_ALLOWLIST",
                            token_env,
                        )
                        continue
                    if not attach_host_allowlist_configured():
                        logger.warning(
                            "Remote attach %s: sending credential (%s) to connector-chosen "
                            "url %r with no AGNES_REMOTE_ATTACH_HOST_ALLOWLIST configured — "
                            "set it in production to pin allowed hosts.",
                            alias,
                            token_env,
                            url,
                        )
                    escaped_token = escape_sql_string_literal(token)
                    conn.execute(f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')")
                    # Apply BQ session settings on every BQ-extension attach,
                    # not only the metadata-token branch above. The token-based
                    # branch previously fell through without calling
                    # apply_bq_session_settings, leaving the 90 s extension
                    # default for bq_query_timeout_ms in place.
                    if extension == "bigquery":
                        from connectors.bigquery.access import apply_bq_session_settings

                        apply_bq_session_settings(conn)
                else:
                    # No auth required (or extension handles it via env automatically)
                    conn.execute(f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, READ_ONLY)")
                    if extension == "bigquery":
                        from connectors.bigquery.access import apply_bq_session_settings

                        apply_bq_session_settings(conn)

                logger.info("Attached remote source %s via %s extension", alias, extension)
            except Exception as e:
                logger.error("Failed to attach remote source %s: %s", alias, e)

    def _update_sync_state(self, meta_rows: list, source_name: str) -> None:
        """Update sync_state table in system.duckdb from _meta entries.

        The hash stored here MUST match what `agnes pull` computes
        client-side via `cli/commands/sync.py:_md5_file` and what the
        materialized SQL path stores via `app/api/sync.py:_file_hash` —
        otherwise the CLI's post-download integrity check fails for every
        local-mode table with `hash mismatch: expected … got …`. That's
        a full content MD5 (`hashlib.md5(bytes).hexdigest()`), no
        truncation.

        Pre-fix this method computed `md5(f"{mtime_ns}:{size}")[:12]` —
        a fingerprint, not a content hash, and 12-char truncated to boot
        — which the CLI's full-32-char content MD5 could never match.
        Symptom: `agnes pull` failed with hash mismatch on every Keboola
        local-mode table because their sync_state hashes came from this
        path while their on-disk content was unrelated.
        """
        try:
            # Backend-aware: write sync_state through the factory (Postgres on
            # a PG instance) so /dashboard's factory-backed reads see it.
            from src.repositories import sync_state_repo
            from src.sync_state_key import resolve_sync_state_key

            extracts_dir = _get_extracts_dir()
            repo = sync_state_repo()
            for table_name, rows, size_bytes, query_mode in meta_rows:
                # Materialized rows own their sync_state: the materialized
                # pass writes it on success (update_sync) and failure
                # (set_error). Re-bumping last_sync here would reset the
                # schedule gate every rebuild, so a failed/killed daily run
                # is never retried until the next day.
                if query_mode == "materialized":
                    continue
                # B1: sync_state.table_id / sync_history.table_id are keyed
                # by the registry id, resolved from this table's name (see
                # src.sync_state_key) — the parquet filename below is a
                # SEPARATE, unrelated convention (still `table_name`) and
                # stays untouched.
                sync_key = resolve_sync_state_key(table_name)
                pq_path = extracts_dir / source_name / "data" / f"{table_name}.parquet"
                table_dir = extracts_dir / source_name / "data" / table_name
                file_hash = ""
                parts = None
                out_size = size_bytes or 0
                # #1339: a table can have BOTH a flat `<table>.parquet` file
                # AND a `<table>/` partition directory at once — e.g. a
                # partitioned-pull migration that wrote fresh parts but left
                # the old flat file in place. TODO(#1339): whether the flat
                # file should keep winning (as it does below, unchanged) or
                # the directory should, and whether the stale sibling should
                # be deleted, are open human decisions this fix does not
                # make — it only stops the collision from being invisible.
                both_layouts = pq_path.exists() and table_dir.is_dir()
                if both_layouts:
                    logger.error(
                        "Table %r in source %r has BOTH a flat parquet (%s) "
                        "and a partition directory (%s) — serving the flat "
                        "file (precedence unchanged), which may be stale "
                        "relative to the partitioned data. See #1339.",
                        table_name,
                        source_name,
                        pq_path,
                        table_dir,
                    )
                if pq_path.exists():
                    # TODO(#1339): the flat file winning is a precedence choice, not a
                    # verdict — when both layouts are present the partitioned
                    # data is almost certainly the fresh one. Flipping it (and
                    # the matching `app/utils.py::resolve_local_parquet_glob`,
                    # which prefers the single file the same way) plus removing
                    # the stale sibling server-side needs a decision about
                    # in-flight readers, so for now this only warns.
                    # Existence probe only — deliberately NOT `_hash_table_parts`,
                    # which full-MD5s every part just to yield a truthy value.
                    # Two reasons: the dual-layout state can persist
                    # indefinitely, and `rebuild_source` runs on every Jira
                    # webhook, so that would re-hash the whole partition dir per
                    # event while holding `rebuild_mutex()`. And this sits inside
                    # the try/except wrapping the WHOLE meta_rows loop, so a raise
                    # here would skip sync_state for every remaining table in the
                    # source — a diagnostic must never break what it diagnoses,
                    # hence the OSError guard around a mid-scan prune.
                    try:
                        both_layouts = next(table_dir.rglob("*.parquet"), None) is not None
                    except OSError:
                        both_layouts = False
                    if both_layouts:
                        # Both layouts on disk. Distribution silently freezes:
                        # the manifest advertises this as a single-file table
                        # hashed from the STALE parquet, so `agnes pull`
                        # downloads it and the md5 MATCHES — analysts keep
                        # getting pre-conversion data indefinitely while the
                        # server's own view reads the fresh partitions. No
                        # error surfaces anywhere, which is why this warns
                        # loudly. Nothing removes the sibling: a Keboola table
                        # flipped to `sync_strategy: partitioned` leaves the
                        # old `<table>.parquet` behind, and the client-side
                        # `_drop_stale_layout` has no server equivalent.
                        logger.warning(
                            "%s has BOTH a flat parquet (%s) and a partition dir (%s); "
                            "serving the flat file — the partitioned data is NOT being "
                            "distributed. Remove the stale flat parquet.",
                            table_name,
                            pq_path,
                            table_dir,
                        )
                    # Single-file table: full content MD5 (see docstring).
                    h = hashlib.md5()
                    with open(pq_path, "rb") as f:
                        for chunk in iter(lambda: f.read(8192), b""):
                            h.update(chunk)
                    file_hash = h.hexdigest()
                else:
                    # Partitioned table (no single {table}.parquet): hash each
                    # part; the rollup keeps the whole-table hash contract, and
                    # the summed part sizes replace the missing single-file size.
                    parts = _hash_table_parts(table_dir)
                    if parts:
                        file_hash = _parts_rollup_hash(parts)
                        out_size = sum(p["size_bytes"] for p in parts)

                repo.update_sync(
                    table_id=sync_key,
                    rows=rows or 0,
                    file_size_bytes=out_size,
                    hash=file_hash,
                    parts=parts,
                )
                if both_layouts:
                    # Record it on the row too — not just the log — so it
                    # stops being invisible to `GET /api/admin/registry` /
                    # `agnes admin list-tables`. Same set_error() contract
                    # every other per-table sync failure uses; it only
                    # flips status/error and leaves the rows/hash just
                    # written above untouched, so the bytes served here stay
                    # byte-for-byte identical to the flat-only case.
                    repo.set_error(
                        sync_key,
                        f"Both a flat parquet ({pq_path}) and a partition "
                        f"directory ({table_dir}) exist for this table; "
                        f"serving the flat file, which may be stale. See #1339.",
                    )
        except Exception as e:
            logger.warning("Could not update sync_state: %s", e)

    def _record_fallback_sync_state(self, conn, table_id: str, parquet_path) -> None:
        """Record a successful filesystem-fallback publish in sync_state.

        Mirrors `_update_sync_state`'s contract: full content MD5 (what
        `agnes pull` verifies against), on-disk size, and a row count taken
        from the just-created master view. Best-effort — a bookkeeping
        failure must never take down the rebuild.
        """
        try:
            from src.repositories import sync_state_repo, table_registry_repo
            from src.sync_state_key import resolve_sync_state_key_for_row

            # Materialized rows own their last_sync (see _update_sync_state):
            # the fallback fires exactly when `_meta` is missing — e.g. a
            # materialize killed between the parquet swap and the `_meta`
            # update. Record the publish (fresh rows/hash, error cleared)
            # but keep last_sync so the schedule gate stays open and the
            # next tick re-runs the materialize, healing `_meta`.
            registry_row = table_registry_repo().get_by_name(table_id)
            is_materialized = bool(registry_row and registry_row.get("query_mode") == "materialized")
            # B1: sync_state.table_id is the registry id (src.sync_state_key)
            # — reuses `registry_row`, already fetched above, rather than a
            # second lookup. `table_id` (the parameter) stays the parquet
            # filename stem used for the view lookup right below; only the
            # sync_state write key changes.
            sync_key = resolve_sync_state_key_for_row(table_id, registry_row)

            h = hashlib.md5()
            with open(parquet_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            row_count = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table_id)}").fetchone()[0]
            sync_state_repo().update_sync(
                table_id=sync_key,
                rows=int(row_count or 0),
                file_size_bytes=parquet_path.stat().st_size,
                hash=h.hexdigest(),
                bump_last_sync=not is_materialized,
            )
        except Exception as e:
            logger.warning(
                "filesystem-fallback: could not update sync_state for %s: %s",
                table_id,
                e,
            )
