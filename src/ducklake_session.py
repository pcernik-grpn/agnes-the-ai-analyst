"""DuckLake session management — reader singleton, writer singleton, lifecycle.

``src/analytics_backend.py`` (task 1) resolves *which* analytics backend is
active and *where* the DuckLake catalog/data live; this module is where the
DuckDB sessions actually get opened. Two long-lived singletons, mirroring
the singleton pattern ``get_system_db`` / ``get_analytics_db()`` use in ``src/db.py``:

- :func:`get_ducklake_read` — the api-role reader: one process-wide attach,
  cursor-per-caller, so a caller's ``.close()`` only drops the cursor
  (task 4 wires this into ``src.db.get_analytics_db_readonly()`` as the
  ``analytics.backend=ducklake`` dispatch target, documenting there why a
  long-lived attach + per-request cursor beats a per-request open for
  this backend). Local/materialized tables need no extra wiring per call
  — they live in the lake itself, so a fresh cursor already sees them via
  DuckLake's MVCC snapshot. **The reader is a strictly read-only plane: it
  issues NO ``CREATE VIEW`` / lake DDL and commits NO catalog snapshot per
  request.** Remote-mode (``query_mode='remote'``) tables have no
  lake-resident data, but their ``lake."main"."<name>"`` wrapper views are
  now owned by the WRITER (created once per rebuild in
  ``src.orchestrator.SyncOrchestrator._sync_ducklake_remote_views``) — the
  reader only queries them. At session (re)open the reader ATTACHes (only)
  the extract sources ``table_registry`` marks remote-mode plus their
  external extensions (:func:`_attach_remote_read_sources`) so those
  wrappers bind; per request it does nothing but a session-scoped BigQuery
  secret refresh (:func:`_refresh_bq_secrets`), which commits no snapshot.
  Deliberately scoped to remote-only sources: attaching every extract
  source persistently collides with a connector that rewrites its
  extract.duckdb *in place* rather than via temp-file swap (Jira) — see
  :func:`_ensure_remote_extract_attach`.
- :func:`get_ducklake_write` — the worker-role writer (task 3's copy-ingest
  rebuild lands through this). Logically a separate singleton from the
  reader — different roles in the three-plane topology.
- :func:`vacuum_ducklake_catalog` (task 5) — not a session accessor; a
  one-shot maintenance helper the ``ducklake-maintenance`` job kind
  (``app/worker/kinds.py``) calls to ``VACUUM`` the Postgres database
  backing the catalog, over its own short-lived raw connection (not the
  writer singleton above).
- :func:`validate_ducklake_migration_prerequisites` (task 6) — also not a
  session accessor; a pre-flight check (extension loadable + a real
  catalog ``ATTACH`` probe, with a ``CREATE DATABASE`` auto-repair
  attempt) the ``POST /api/admin/analytics/migrate`` endpoint
  (``app/api/admin_analytics.py``) runs before enqueueing the
  ``analytics-migrate`` job, over its own throwaway connections (again,
  not either singleton above).

Both wrap the same underlying attach mechanics (``_attach_ducklake``):
``INSTALL/LOAD ducklake`` (+``postgres`` when the catalog target is a
Postgres DSN), then ``ATTACH 'ducklake:...' AS lake (DATA_PATH '...')``.
Memory caps + thread count reuse ``src.db._apply_memory_caps`` verbatim —
same per-connection budgeting rationale as the legacy analytics
connections (see the ``_*_MEMORY_LIMIT`` block in ``src/db.py``).

**Memory-budget model (per-connection, shared across cursors).** The
``memory_limit`` is a property of the *physical DuckDB connection*, not of
a cursor. The reader singleton is one such connection; every concurrent
caller's cursor (``.cursor()``) runs under that single shared budget. This
is intended — it bounds the reader plane's *aggregate* RSS across all
in-flight analyst queries (one api-replica-wide cap), rather than letting
N concurrent cursors each claim their own ``_DUCKLAKE_READ_MEMORY_LIMIT``
and multiply the process's peak. **Corollary for the file-catalog path:**
when the resolved catalog is a DuckDB *file* target, reader and writer
share one physical connection (see the same-process constraint below),
and that shared connection is budgeted at the writer's
``_DUCKLAKE_WRITE_MEMORY_LIMIT`` (1500MB), NOT the 1GB read cap — a
file-catalog deployment is single-process ``all`` mode, so the writer's
higher cap governs both roles on the one shared connection.

**Same-process file-catalog constraint (verified directly against DuckDB
1.5.2, not documented upstream):** DuckDB refuses to ``ATTACH`` the same
DuckLake *file*-catalog target twice from two different top-level
connections in one process — ``Binder Error: ... Unique file handle
conflict: Cannot attach "__ducklake_metadata_<alias>" - the database file
"<path>" is already attached`` — regardless of the alias used. A Postgres
catalog target does **not** hit this: two independent connections each
get their own libpq connection with no conflict. **Sizing:** the attach
itself opens exactly one catalog backend (``_attach_ducklake`` disables the
postgres extension's per-thread connection cache so that this is
deterministic rather than a scheduler race — issue #2403). Every further
backend is a pooled connection borrowed by a *transaction* on the metadata
catalog: a statement that enumerates catalogs (``duckdb_tables()``,
``information_schema`` — routine on the read path) opens a second
transaction on the metadata catalog beside DuckLake's own and borrows a
second backend; statements running *concurrently* on one attach — the
reader hands a cursor to every request — each borrow one for their
duration. Pooled connections stay open once opened, until the session
closes, so a reader settles at two and a busy one grows to its peak
concurrency, capped by ``pg_pool_max_connections`` (default
``max(8, CPU count)``). Budget Postgres ``max_connections`` for that cap
per api replica, not for one. Since a file catalog is only ever valid
in single-process ``all`` mode anyway (multi-process requires an explicit
Postgres DSN — see ``app.startup_guards.validate_deployment``), the reader
and writer singletons **share one physical connection** when the
resolved catalog is a file path, and only diverge into independent
connections when it is a Postgres DSN. See :func:`_get_shared_file_catalog_conn`.

See docs/superpowers/specs/2026-07-16-three-plane-scalable-architecture.md
§3.4 for the architecture this implements, and
docs/superpowers/plans/2026-07-19-three-plane-wave2g-ducklake.md task 2.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import duckdb

from src.access_policy_udf import register_policy_udfs
from src.analytics_backend import ducklake_catalog_dsn, ducklake_data_path, is_postgres_dsn
from src.db import (
    _apply_memory_caps,
    _get_data_dir,
    _maybe_instrument,
    _reattach_remote_extensions,
    _SAFE_IDENTIFIER,
)
from src.duckdb_conn import _open_duckdb
from src.orchestrator_security import escape_sql_string_literal
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

# The ATTACHed catalog is always exposed under this fixed alias — it is
# never user/config-controlled, so it needs no runtime identifier
# validation (unlike the extract-source aliases below, which come from
# directory names on disk).
_LAKE_ALIAS = "lake"

# Per-connection memory budgets, mirroring the ``_ANALYTICS_DB_MEMORY_LIMIT``
# / ``_ANALYTICS_RO_MEMORY_LIMIT`` split in ``src/db.py``: the writer is a
# single worker-side singleton (comparable to ``get_analytics_db()``), the
# reader is the long-lived per-api-replica attach (comparable to
# ``get_analytics_db_readonly()``, except long-lived rather than per-request
# — one budgeted connection per api replica instead of one per request).
_DUCKLAKE_WRITE_MEMORY_LIMIT = "1500MB"
_DUCKLAKE_READ_MEMORY_LIMIT = "1GB"

_read_lock = threading.Lock()
_read_conn: duckdb.DuckDBPyConnection | None = None
_read_key: tuple[str, str] | None = None

_write_lock = threading.Lock()
_write_conn: duckdb.DuckDBPyConnection | None = None
_write_key: tuple[str, str] | None = None

# The single physical connection shared by reader + writer when the
# resolved catalog is a DuckDB-file target — see the module docstring's
# "same-process file-catalog constraint" note.
_shared_file_lock = threading.Lock()
_shared_file_conn: duckdb.DuckDBPyConnection | None = None
_shared_file_key: tuple[str, str] | None = None

_available_lock = threading.Lock()
_available_cache: bool | None = None


# Re-exported under the original private name: the predicate itself now
# lives in ``src.analytics_backend.is_postgres_dsn`` so
# ``app.startup_guards.validate_deployment`` shares the exact same
# postgres-DSN check as the attach path below, instead of the narrower
# ``str.startswith(("postgresql://", "postgres://"))`` it used to run,
# which silently rejected the SQLAlchemy ``+driver`` form
# (``postgresql+psycopg://...``) that a copied ``DATABASE_URL`` uses.
# Kept as a module-level alias (rather than inlining ``is_postgres_dsn``
# at each call site) so existing internal call sites and
# ``tests/test_ducklake_session.py::test_is_postgres_dsn_detects_url_forms_and_rejects_file_paths``
# keep importing ``src.ducklake_session._is_postgres_dsn`` unchanged.
_is_postgres_dsn = is_postgres_dsn


def _libpq_escape(value: str) -> str:
    """Quote+escape a single libpq keyword/value pair's value.

    libpq's keyword/value DSN syntax accepts a bare token OR a
    single-quoted one (backslash-escaping ``\\`` and ``'`` inside the
    quotes); always quoting sidesteps having to special-case empty,
    whitespace-containing, or otherwise-special values.
    """
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def pg_dsn_to_libpq(dsn: str) -> str:
    """Convert a URL-form Postgres DSN into libpq keyword form.

    ``ducklake_catalog_dsn()`` (task 1) returns an explicit Postgres
    target in URL form — the same shape ``DATABASE_URL`` uses elsewhere
    in this codebase: ``postgresql://[user[:pass]@][host][:port]/dbname[?param=value...]``,
    optionally with a SQLAlchemy ``+driver`` suffix (``postgresql+psycopg://``).
    DuckDB's ``ducklake:postgres:<dsn>`` ATTACH form expects libpq keyword
    form (``dbname=... host=... user=...``) — the POC found the URL form
    is not reliably accepted through ducklake's ATTACH path — so this is
    the converter, applied unconditionally whenever the catalog DSN is a
    Postgres URL.

    Handles the pgserver test-fixture's unix-socket URL shape, where the
    socket directory rides in the ``host`` *query* parameter rather than
    the netloc (``postgresql://user:@/db?host=/path/to/socket/dir``) —
    the ``host`` query param wins over any netloc hostname when both are
    present, since a netloc-less unix-socket URL still has to route
    somewhere. Any other query parameter (``sslmode``, ``options``, ...)
    passes through verbatim as an additional libpq keyword.

    ``urlparse().username`` / ``.password`` return the RAW (still
    percent-encoded) netloc substrings — unlike ``parse_qs``, which
    unquotes query values automatically. A password containing ``@``,
    ``:``, or a space is percent-encoded in any spec-compliant DSN
    (SQLAlchemy always renders it that way), so username/password/
    hostname are explicitly ``unquote()``'d here before libpq-escaping;
    skipping that step would hand libpq the literal percent-escapes
    instead of the real credential.
    """
    parsed = urlparse(dsn)
    scheme = parsed.scheme.split("+", 1)[0]
    if scheme not in ("postgresql", "postgres"):
        raise ValueError(f"not a postgres DSN: {dsn!r}")

    query = parse_qs(parsed.query)
    parts: list[str] = []

    dbname = unquote(parsed.path.lstrip("/"))
    if dbname:
        parts.append(f"dbname={_libpq_escape(dbname)}")
    if parsed.username:
        parts.append(f"user={_libpq_escape(unquote(parsed.username))}")
    if parsed.password:
        parts.append(f"password={_libpq_escape(unquote(parsed.password))}")

    host = (query.get("host") or [None])[0] or (unquote(parsed.hostname) if parsed.hostname else None)
    if host:
        parts.append(f"host={_libpq_escape(host)}")
    if parsed.port:
        parts.append(f"port={parsed.port}")

    for key, values in query.items():
        if key == "host":
            continue
        parts.append(f"{key}={_libpq_escape(values[0])}")

    return " ".join(parts)


def _attach_target(catalog_dsn: str) -> str:
    """Build the ``ducklake:...`` ATTACH target string for *catalog_dsn*.

    A bare filesystem path (the single-process default from
    ``ducklake_catalog_dsn()``) becomes ``ducklake:<path>``; an explicit
    Postgres DSN becomes ``ducklake:postgres:<libpq-keyword-dsn>`` via
    :func:`pg_dsn_to_libpq`.
    """
    if _is_postgres_dsn(catalog_dsn):
        return f"ducklake:postgres:{pg_dsn_to_libpq(catalog_dsn)}"
    return f"ducklake:{catalog_dsn}"


def _attach_ducklake(conn: duckdb.DuckDBPyConnection, *, catalog_dsn: str, data_path: str) -> None:
    """INSTALL/LOAD the ``ducklake`` extension (+ ``postgres`` for a PG
    catalog) and ``ATTACH`` the catalog under :data:`_LAKE_ALIAS` on *conn*.

    Shared by both the reader and writer singleton openers below —
    the attach mechanics are identical; only the memory budget and the
    extra reader-side extract/remote-extension wiring differ.
    """
    conn.execute("INSTALL ducklake")
    conn.execute("LOAD ducklake")
    if _is_postgres_dsn(catalog_dsn):
        conn.execute("INSTALL postgres")
        conn.execute("LOAD postgres")
        # The postgres extension hands the catalog's libpq connections out
        # of a pool whose per-thread cache is ON by default: a released
        # connection is parked in the *releasing* thread's slot, invisible
        # to an acquire from any other thread, which then opens a fresh
        # one. DuckLake attaches its metadata catalog through a nested
        # query on an inner connection of its own, so with ``threads=2``
        # (``_apply_memory_caps``) the catalog constructor's connection
        # and the first metadata query's transaction land on the same
        # thread only by scheduling luck — on a loaded 2-CPU host the
        # attach opened a second backend in roughly 1 attach out of 4,
        # and a closed session's cached connection outlived the DuckDB
        # instance (issue #2403). Disabling the cache returns every
        # released connection to the shared pool, so the attach itself
        # opens exactly one backend, a later transaction reuses it unless
        # one is already borrowed (the module docstring's sizing note),
        # and ``close_ducklake_sessions`` closes all of them. ``GLOBAL``,
        # not session: the pool is created, and this setting read, on
        # DuckLake's inner connection.
        conn.execute("SET GLOBAL pg_pool_enable_thread_local_cache = false")
    Path(data_path).mkdir(parents=True, exist_ok=True)
    target = escape_sql_string_literal(_attach_target(catalog_dsn))
    data_path_escaped = escape_sql_string_literal(data_path)
    conn.execute(f"ATTACH '{target}' AS {_LAKE_ALIAS} (DATA_PATH '{data_path_escaped}')")


def _get_shared_file_catalog_conn(catalog_dsn: str, data_path: str) -> duckdb.DuckDBPyConnection:
    """Return the single process-wide connection attached to a DuckDB-file
    DuckLake catalog, opening/re-opening it on first use or config change.

    See the module docstring's "same-process file-catalog constraint" —
    DuckDB refuses a second same-process ATTACH of the same catalog file
    even under a different connection/alias, so both
    :func:`get_ducklake_read` and :func:`get_ducklake_write` route through
    this single physical connection whenever the resolved catalog is a
    file path (never for a Postgres DSN, which does not hit the
    restriction). The writer's memory budget is applied since the shared
    connection carries whichever role(s) are active in this process.
    """
    global _shared_file_conn, _shared_file_key
    key = (catalog_dsn, data_path)
    with _shared_file_lock:
        if _shared_file_conn is None or _shared_file_key != key:
            if _shared_file_conn is not None:
                try:
                    _shared_file_conn.close()
                except Exception:
                    pass
            conn = _open_duckdb(":memory:")
            _apply_memory_caps(conn, _DUCKLAKE_WRITE_MEMORY_LIMIT, label="ducklake_shared_file_catalog")
            _attach_ducklake(conn, catalog_dsn=catalog_dsn, data_path=data_path)
            _shared_file_conn = conn
            _shared_file_key = key
        return _shared_file_conn


def _extracts_dir() -> Path:
    return _get_data_dir() / "extracts"


def _remote_registry_rows_by_source() -> dict[str, list[dict]]:
    """Group ``table_registry`` rows with ``query_mode='remote'`` by
    ``source_type`` (== the extract source's directory name under
    ``{DATA_DIR}/extracts`` — every connector writes its extract to
    ``extracts/<source_type>/``, one directory per connector, never
    per-instance; see ``app/api/sync.py``'s ``bq_output_dir`` /
    ``kb_output_dir`` constants).

    ``table_registry`` (system.duckdb/PG) is the task-4-decided source of
    truth for "which sources need a remote attach" — not a filesystem
    probe of each extract.duckdb's own ``_remote_attach`` table. This
    matters: it lets the reader skip touching a purely local-mode
    source's extract.duckdb *entirely*, which is the fix for the
    single-process collision described in :func:`_ensure_remote_extract_attach`.
    """
    from src.repositories import table_registry_repo

    try:
        rows = table_registry_repo().list_all()
    except Exception as e:
        logger.debug("could not list table_registry for ducklake remote-view sync: %s", e)
        return {}

    by_source: dict[str, list[dict]] = {}
    for r in rows:
        if (r.get("query_mode") or "") != "remote":
            continue
        source_type = r.get("source_type") or ""
        name = r.get("name") or ""
        if not source_type or not name:
            continue
        by_source.setdefault(source_type, []).append(r)
    return by_source


def _ensure_remote_extract_attach(conn: duckdb.DuckDBPyConnection, remote_by_source: dict[str, list[dict]]) -> None:
    """ATTACH, read-only, only the extract sources ``remote_by_source``
    names — i.e. only sources with at least one ``query_mode='remote'``
    table_registry row — and only if not already attached.

    **Why not attach every extract source (what this function did before
    task 4), and why not even a filesystem probe of every source:** a
    local-mode-only source's extract.duckdb can be rewritten *in place*
    by its connector — e.g. ``connectors/jira/extract_init.py::update_meta``
    opens ``extract.duckdb`` read-write (no temp-file swap) on every
    webhook. DuckDB refuses a read-write open while *any* other
    connection — even read-only, even same-process — already has that
    file open ("Conflicting lock is held"). The legacy per-request
    ``get_analytics_db_readonly()`` gets away with attaching every source
    because its attach is closed at the end of the same request; this
    reader's attach is process-lifetime, so holding it on a source Jira
    (or any future live-in-place connector) mutates in-place would wedge
    every subsequent webhook. The batch extractors that *do* need a
    persistent attach here (Keboola, BigQuery) never write in place —
    they always build ``extract.duckdb.tmp`` and atomically swap it in
    (see ``connectors/keboola/extractor.py::run`` /
    ``connectors/bigquery/extractor.py::_init_extract_locked``), which
    never conflicts with an existing reader holding the old inode open —
    so it is safe to hold their attach indefinitely. Restricting the
    persistent-attach set to exactly "sources with a remote-mode table"
    (via table_registry, see :func:`_remote_registry_rows_by_source`)
    happens to select exactly this safe subset, since only batch
    connectors (Keboola direct-bucket, BigQuery) ever emit
    ``query_mode='remote'`` rows.
    """
    extracts_dir = _extracts_dir()
    if not extracts_dir.exists() or not remote_by_source:
        return
    try:
        attached_dbs = {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    except Exception:
        return
    for source_name in sorted(remote_by_source):
        if not _SAFE_IDENTIFIER.match(source_name) or source_name in attached_dbs:
            continue
        db_file = extracts_dir / source_name / "extract.duckdb"
        if not db_file.exists():
            continue
        try:
            conn.execute(f"ATTACH '{db_file}' AS {source_name} (READ_ONLY)")
        except Exception as e:
            logger.debug("Could not attach remote-mode extract source %s: %s", source_name, e)


def _attach_remote_read_sources(conn: duckdb.DuckDBPyConnection) -> None:
    """ONE-TIME (per reader session open) attach of the remote-mode extract
    sources + their external extensions, so the writer-created
    ``lake.main`` wrapper views bind at query time.

    The ``lake.main`` wrapper views themselves are created by the WRITER
    (``src.orchestrator.SyncOrchestrator._sync_ducklake_remote_views``),
    NOT here — the reader never issues ``CREATE VIEW`` against the lake, so
    it never commits a DuckLake catalog snapshot. But each wrapper's body
    is ``SELECT * FROM "<source>"."<name>"``, so the reader connection must
    have the extract source (and, for a BigQuery extract, its ``bq`` alias)
    ATTACHed for the wrapper to resolve. This does that attach exactly once
    per session open — a registry scan (:func:`_remote_registry_rows_by_source`)
    is acceptable here (bounded, one-time); it must NOT run per-request.

    Newly-registered remote tables therefore appear once (a) the writer's
    next rebuild creates their wrapper view and (b) this reader session
    re-opens (picking up the new extract source attach) — a deliberate
    trade of instant per-request visibility for a read plane that issues
    no per-request registry scan and commits no per-request catalog write.
    """
    remote_by_source = _remote_registry_rows_by_source()
    if not remote_by_source:
        return
    _ensure_remote_extract_attach(conn, remote_by_source)
    _reattach_remote_extensions(conn, _extracts_dir())


def _refresh_bq_secrets(conn: duckdb.DuckDBPyConnection) -> None:
    """Per-request refresh of short-lived BigQuery ACCESS_TOKEN secrets on
    the long-lived reader connection.

    ``src.db._reattach_remote_extensions`` re-fetches the GCE metadata
    token and issues ``CREATE OR REPLACE SECRET`` for each already-attached
    BQ source (it skips re-ATTACH for anything already attached, and is a
    no-op filesystem walk when no source carries a ``_remote_attach``
    table). Crucially it does NOT scan ``table_registry`` and does NOT
    touch the DuckLake catalog — ``CREATE OR REPLACE SECRET`` is
    session-scoped and commits no snapshot (verified vs DuckDB 1.5.2) — so
    running it on every :func:`get_ducklake_read` call keeps BQ queries
    working past the ~1h token TTL without amplifying catalog writes from
    the read plane.
    """
    _reattach_remote_extensions(conn, _extracts_dir())


def get_ducklake_read() -> duckdb.DuckDBPyConnection:
    """Return a cursor on the shared, long-lived DuckLake reader singleton.

    Mirrors ``src.db.get_analytics_db()``'s singleton + cursor-per-caller
    shape: one attach per (catalog_dsn, data_path) configuration, callers
    get a ``.cursor()`` they can ``.close()`` without tearing down the
    underlying attach. Re-opens transparently when the effective catalog
    DSN or data path changes (e.g. a test fixture flips ``DATA_DIR`` or
    ``AGNES_DUCKLAKE_CATALOG_DSN`` across cases) — same contract
    ``get_analytics_db()`` has for a changed ``DATA_DIR``.

    **Read-only plane — no per-request catalog writes.** The wrapper
    views for ``query_mode='remote'`` tables (``lake."main"."<name>"``)
    are created by the WRITER once per rebuild
    (``src.orchestrator.SyncOrchestrator._sync_ducklake_remote_views``),
    NOT here. This is load-bearing: a ``CREATE OR REPLACE VIEW`` against a
    DuckLake catalog commits a fresh catalog snapshot on every call even
    when the body is unchanged (verified vs DuckDB 1.5.2 — 5 identical
    calls → +5 snapshots), so doing it on every reader request would be
    unbounded catalog write-amplification onto the shared PG catalog from
    the api plane, defeating read/write separation. The reader therefore
    issues NO ``CREATE VIEW`` / lake DDL and commits NO snapshot. Its only
    per-request work is:

    - ``USE lake`` on the returned cursor (no snapshot);
    - a BigQuery ACCESS_TOKEN secret refresh via :func:`_refresh_bq_secrets`
      — ``CREATE OR REPLACE SECRET`` is session-scoped and commits no
      DuckLake snapshot (verified), needed because the GCE metadata token
      backing the BQ ATTACH expires (~1h) while this reader's physical
      connection lives for the whole process.

    The one-time work at session (re)open attaches the remote-mode extract
    sources + their external extensions (:func:`_attach_remote_read_sources`)
    so the writer-created wrappers bind. That attach set is exactly the
    sources ``table_registry`` marks remote-mode — never a live-in-place
    connector (e.g. Jira, whose ``extract_init.py::update_meta`` rewrites
    ``extract.duckdb`` in place), so the long-lived reader never wedges a
    webhook write with a "Conflicting lock". Remote-mode sources (Keboola
    direct-bucket, BigQuery) always temp-file-swap, so holding their attach
    is safe.

    Every returned cursor also runs ``USE lake`` so an unqualified
    ``SELECT ... FROM "<table>"`` resolves against ``lake.main`` — the
    schema task 3's writer creates master views in — the same way a
    query against the legacy ``server.duckdb`` file resolves unqualified
    names against its own default catalog/schema.

    When the resolved catalog is a DuckDB-file target, the underlying
    physical connection is shared with :func:`get_ducklake_write` (see the
    module docstring) — closing *this* cursor never affects that shared
    connection; only :func:`close_ducklake_sessions` tears it down.
    """
    global _read_conn, _read_key
    catalog_dsn = ducklake_catalog_dsn()
    data_path = ducklake_data_path()
    key = (catalog_dsn, data_path)

    with _read_lock:
        if _read_conn is None or _read_key != key:
            old = _read_conn
            if old is not None and old is not _shared_file_conn:
                try:
                    old.close()
                except Exception:
                    pass
            if _is_postgres_dsn(catalog_dsn):
                conn = _open_duckdb(":memory:")
                _apply_memory_caps(conn, _DUCKLAKE_READ_MEMORY_LIMIT, label="get_ducklake_read")
                _attach_ducklake(conn, catalog_dsn=catalog_dsn, data_path=data_path)
            else:
                conn = _get_shared_file_catalog_conn(catalog_dsn, data_path)
            _read_conn = conn
            _read_key = key
            # ONE-TIME (per session open, not per request): ATTACH the
            # remote-mode extract sources + their external extensions so
            # the writer-created ``lake.main`` wrapper views bind at query
            # time. A registry scan is fine here (once per open); it must
            # NOT happen per-request (see below).
            _attach_remote_read_sources(_read_conn)
            # ONE-TIME as well: the access-policy scalar UDFs (``agnes_hmac``)
            # are catalog-scoped on this shared physical connection, so they
            # are registered here, under ``_read_lock`` at session open --
            # never per request, where two concurrent first callers could
            # race the same ``CREATE FUNCTION`` on one catalog.
            register_policy_udfs(_read_conn)
        # Per-request: refresh ONLY the BQ ACCESS_TOKEN secret. The reader
        # issues NO ``CREATE VIEW`` / lake DDL and commits NO catalog
        # snapshot — the writer owns the wrapper views now. ``CREATE OR
        # REPLACE SECRET`` is session-scoped and does NOT commit a DuckLake
        # snapshot (verified vs DuckDB 1.5.2), so this cannot amplify
        # catalog writes from the read plane. Kept under ``_read_lock``
        # because it mutates the shared physical connection.
        _refresh_bq_secrets(_read_conn)
        cursor = _read_conn.cursor()
        cursor.execute(f"USE {_LAKE_ALIAS}")
        return _maybe_instrument(cursor, "ducklake_read")


def get_ducklake_write() -> duckdb.DuckDBPyConnection:
    """Return a cursor on the shared, long-lived DuckLake writer singleton.

    Separate singleton from :func:`get_ducklake_read` — the worker's
    copy-ingest rebuild path (task 3) is the only writer, distinct from
    the api-role reader(s). No extract-source attach or remote-extension
    re-attach here: the writer's job is
    ``CREATE OR REPLACE TABLE lake."<source>"."<table>" AS SELECT * FROM
    read_parquet(...)`` against the source's own extract parquet files
    (resolved directly by the caller), not multi-source catalog reads.

    Same re-open-on-config-change contract as :func:`get_ducklake_read`.
    When the resolved catalog is a DuckDB-file target, this shares its
    underlying physical connection with :func:`get_ducklake_read` — see
    the module docstring's "same-process file-catalog constraint".
    """
    global _write_conn, _write_key
    catalog_dsn = ducklake_catalog_dsn()
    data_path = ducklake_data_path()
    key = (catalog_dsn, data_path)

    with _write_lock:
        if _write_conn is None or _write_key != key:
            old = _write_conn
            if old is not None and old is not _shared_file_conn:
                try:
                    old.close()
                except Exception:
                    pass
            if _is_postgres_dsn(catalog_dsn):
                conn = _open_duckdb(":memory:")
                _apply_memory_caps(conn, _DUCKLAKE_WRITE_MEMORY_LIMIT, label="get_ducklake_write")
                _attach_ducklake(conn, catalog_dsn=catalog_dsn, data_path=data_path)
            else:
                conn = _get_shared_file_catalog_conn(catalog_dsn, data_path)
            _write_conn = conn
            _write_key = key
        return _maybe_instrument(_write_conn.cursor(), "ducklake_write")


# The wave-2G plan text refers to these as ``open_ducklake_read()`` /
# ``open_ducklake_write()`` (the "open a session" verb); ``get_*`` matches
# the naming convention every other singleton accessor in ``src/db.py``
# uses (``get_system_db``, ``get_analytics_db``, ...). Both names are kept
# as the public surface — plain aliases, not separate implementations —
# so either wave-2G task or callsite that reaches for either spelling works.
open_ducklake_read = get_ducklake_read
open_ducklake_write = get_ducklake_write


def close_ducklake_sessions() -> None:
    """Close both DuckLake singletons (reader + writer).

    Mirrors ``src.db.close_singleton_connections()`` — used for lifecycle
    handoff (e.g. before a subprocess needs the catalog attach fresh, or a
    role/process shutdown) and by tests that need a clean re-open under a
    changed config. Idempotent; the next :func:`get_ducklake_read` /
    :func:`get_ducklake_write` call lazily re-opens.

    Reader and writer may (file-catalog case) point at the *same*
    physical connection object — collected by identity into a dict first
    so that connection is closed exactly once instead of twice.
    """
    global _read_conn, _read_key, _write_conn, _write_key, _shared_file_conn, _shared_file_key
    to_close: dict[int, duckdb.DuckDBPyConnection] = {}

    with _read_lock:
        if _read_conn is not None:
            to_close[id(_read_conn)] = _read_conn
        _read_conn = None
        _read_key = None
    with _write_lock:
        if _write_conn is not None:
            to_close[id(_write_conn)] = _write_conn
        _write_conn = None
        _write_key = None
    with _shared_file_lock:
        if _shared_file_conn is not None:
            to_close[id(_shared_file_conn)] = _shared_file_conn
        _shared_file_conn = None
        _shared_file_key = None

    for conn in to_close.values():
        try:
            conn.close()
        except Exception:
            pass


def ducklake_available() -> bool:
    """Probe whether the ``ducklake`` DuckDB extension can be installed
    and loaded in this environment.

    A throwaway in-memory connection tries ``INSTALL ducklake; LOAD
    ducklake;``. Used by startup guards / the migration command (later
    wave-2G tasks) to fail loud, before an operator flips
    ``analytics.backend: ducklake``, in an offline/air-gapped deployment
    where the extension can't be fetched.

    A successful probe is cached for the rest of the process lifetime —
    extension availability is a property of the DuckDB build/environment,
    not something that flips mid-process. A *failed* probe is deliberately
    NOT cached, so a transient failure (extension repo unreachable at
    startup) doesn't wedge an operator retry after connectivity returns;
    see :func:`reset_ducklake_available_cache` for tests that need to
    force a re-probe regardless.
    """
    global _available_cache
    with _available_lock:
        if _available_cache:
            return True
        try:
            # Route through _open_duckdb (not a bare duckdb.connect) so this
            # production call site honors the UTC-timezone pin like every
            # other — enforced by tests/test_duckdb_session_tz.py. The probe
            # writes nothing tz-sensitive, but the guard is blanket by design.
            probe = _open_duckdb(":memory:")
            try:
                probe.execute("INSTALL ducklake")
                probe.execute("LOAD ducklake")
            finally:
                probe.close()
        except Exception as e:
            logger.warning("ducklake extension probe failed: %s", e)
            return False
        _available_cache = True
        return True


def reset_ducklake_available_cache() -> None:
    """Drop the cached :func:`ducklake_available` probe result. Test-only."""
    global _available_cache
    with _available_lock:
        _available_cache = None


def vacuum_ducklake_catalog() -> bool:
    """``VACUUM`` the Postgres database backing the DuckLake catalog, when
    the resolved catalog target is Postgres. Returns ``True`` when it ran,
    ``False`` when skipped (file-catalog target).

    Used by the ``ducklake-maintenance`` job kind (``app/worker/kinds.py``)
    as the fourth step of the POC-verified maintenance sequence
    (``merge_adjacent_files`` → ``ducklake_expire_snapshots`` →
    ``ducklake_cleanup_old_files`` → catalog VACUUM).

    **Why a separate raw connection instead of running ``VACUUM`` on the
    ``lake``-ATTACHed writer cursor:** the DuckLake catalog, when Postgres-
    backed, is exposed through the ``lake`` alias as the *data* catalog
    (schemas/tables the ingest path writes) — DuckDB's ``ducklake``/
    ``postgres`` extensions do not surface a passthrough for administrative
    commands like ``VACUUM`` against the underlying Postgres database
    through that ATTACH. Verified directly (real pgserver instance, DuckDB
    1.5.2): DuckLake's Postgres-catalog implementation stores its
    ``ducklake_*`` metadata tables as plain tables directly in the
    database's ``public`` schema (see
    ``tests/db_pg/test_ducklake_pg_catalog.py``'s module docstring) — so a
    single unqualified ``VACUUM`` issued over a *direct* connection to that
    database vacuums the whole thing (catalog metadata tables included),
    exactly like any other Postgres database maintenance pass. This reuses
    :func:`pg_dsn_to_libpq` (the same DSN-conversion this module already
    applies for the ``ducklake:postgres:...`` ATTACH form) to build a
    conninfo string ``psycopg`` accepts directly — including the
    pgserver-style unix-socket DSN shape (``host`` riding in the query
    string).

    ``autocommit=True`` is required: Postgres refuses ``VACUUM`` inside an
    explicit transaction block (``VACUUM cannot run inside a transaction
    block``), and psycopg opens every connection in an implicit transaction
    by default.

    A no-op (returns ``False``) for a DuckDB-file catalog target — DuckDB
    has no equivalent storage-compaction ``VACUUM`` (its ``VACUUM``/
    ``VACUUM ANALYZE`` statements only refresh planner statistics, and
    ``merge_adjacent_files``/``ducklake_cleanup_old_files`` above already
    do the file-level compaction DuckLake itself needs); running one there
    would be a costly no-op rather than a meaningful maintenance step.
    """
    catalog_dsn = ducklake_catalog_dsn()
    if not _is_postgres_dsn(catalog_dsn):
        logger.debug(
            "vacuum_ducklake_catalog: catalog target is a DuckDB file, not Postgres — "
            "skipping (no storage-compaction VACUUM equivalent for a file catalog)"
        )
        return False

    import psycopg

    libpq = pg_dsn_to_libpq(catalog_dsn)
    conn = psycopg.connect(libpq, autocommit=True)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    return True


def _dsn_with_database(dsn: str, database: str) -> str:
    """Return the Postgres URL *dsn* with its path (database name) replaced
    by *database* — netloc and query string untouched.

    Used to derive an "administrative" connection target (the always-
    present ``postgres`` database on the same server) from an operator-
    supplied ``ducklake.catalog_dsn`` whose named database doesn't exist
    yet — see :func:`_probe_or_repair_ducklake_pg_catalog`'s auto-create
    path. Works for the pgserver-style unix-socket DSN shape too (the
    socket directory rides in the query string, which this leaves alone).
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(dsn)
    return urlunparse(parsed._replace(path=f"/{database}"))


def _ducklake_target_database_name(catalog_dsn: str) -> str:
    """Extract the bare database name a Postgres ``catalog_dsn`` targets."""
    from urllib.parse import unquote, urlparse

    return unquote(urlparse(catalog_dsn).path.lstrip("/"))


def _probe_or_repair_ducklake_pg_catalog(catalog_dsn: str, data_path: str) -> list[str]:
    """Postgres-catalog half of :func:`validate_ducklake_migration_prerequisites`.

    Attempts a real DuckLake ``ATTACH`` against *catalog_dsn* on a
    throwaway in-memory connection. On success, returns ``[]``. On a
    "database does not exist" failure — the expected shape on an EXISTING
    Postgres volume that pre-dates DuckLake, since
    ``deploy/postgres/init-ducklake-db.sql`` only ever runs against a
    brand-new empty volume (wave-2G Task 5's known m-tier gap) — attempts
    ``CREATE DATABASE`` against the same server's ``postgres`` database
    and retries the attach once. Any other failure (auth, missing schema
    privileges, unwritable ``data_path``, an unreachable server, a
    ``CREATE DATABASE`` permission failure, ...) is reported verbatim,
    with the exact manual ``CREATE DATABASE`` command included whenever
    the missing-database branch is the one that failed to self-heal —
    this instance's credentials may simply lack ``CREATEDB``.

    Returns a list of human-readable problem strings (empty on success).
    Broken out from :func:`validate_ducklake_migration_prerequisites` so
    the "database missing -> try to create it -> retry" branch reads as
    one linear sequence instead of nesting inside the caller.
    """
    import psycopg

    dbname = _ducklake_target_database_name(catalog_dsn)

    def _try_attach() -> Exception | None:
        try:
            probe = _open_duckdb(":memory:")
            try:
                _attach_ducklake(probe, catalog_dsn=catalog_dsn, data_path=data_path)
            finally:
                probe.close()
            return None
        except Exception as e:  # noqa: BLE001 - classify+report, never crash the caller
            return e

    exc = _try_attach()
    if exc is None:
        return []

    message = str(exc)
    missing_db = bool(dbname) and f'database "{dbname}" does not exist' in message
    if not missing_db:
        return [f"could not reach the DuckLake Postgres catalog at the configured DSN: {message}"]

    create_cmd = f"CREATE DATABASE {quote_ident(dbname)};"
    admin_dsn = _dsn_with_database(catalog_dsn, "postgres")
    try:
        admin_conn = psycopg.connect(pg_dsn_to_libpq(admin_dsn), autocommit=True)
        try:
            admin_conn.execute(f"CREATE DATABASE {quote_ident(dbname)}")
        finally:
            admin_conn.close()
    except Exception as create_exc:
        return [
            f"database {dbname!r} does not exist on the configured DuckLake Postgres "
            f"catalog server, and this instance could not create it automatically "
            f"({create_exc}). This is expected on a Postgres volume that pre-dates "
            f"DuckLake (deploy/postgres/init-ducklake-db.sql only runs on a brand-new "
            f"volume) — run this manually against the target Postgres server, then "
            f"retry the migration: {create_cmd}"
        ]

    logger.info(
        "validate_ducklake_migration_prerequisites: created missing catalog database %r",
        dbname,
    )

    retry_exc = _try_attach()
    if retry_exc is not None:
        return [f"created database {dbname!r} but the DuckLake ATTACH still failed: {retry_exc}"]
    return []


def validate_ducklake_migration_prerequisites() -> list[str]:
    """Validate that ``analytics.backend: ducklake`` can actually be
    populated/queried in this environment, WITHOUT flipping any config —
    the engine behind ``agnes admin analytics migrate --to ducklake``
    (wave-2G Task 6). Returns a list of human-readable problem strings;
    an empty list means every prerequisite is satisfied and the migration
    rebuild is safe to trigger.

    Every applicable check runs (not short-circuited on the first
    failure), so an operator sees every problem in one pass:

    1. :func:`ducklake_available` — the ``ducklake`` DuckDB extension must
       be installable/loadable in this environment (offline/air-gapped
       deployments without vendored extensions fail here).
    2. A multi-process topology (``app.startup_guards.is_multi_process``)
       requires an explicit Postgres ``ducklake.catalog_dsn`` — a DuckDB-
       file catalog is hard single-process. ``app.startup_guards
       .validate_deployment`` enforces the same rule at boot; re-checking
       it here fails the migrate command BEFORE triggering a (potentially
       large) rebuild instead of only at the next restart.
    3. When the resolved catalog is a Postgres DSN: a real ``ATTACH``
       reachability probe, with an automatic ``CREATE DATABASE`` repair
       attempt (or the exact manual command) when the target database
       itself doesn't exist yet — see
       :func:`_probe_or_repair_ducklake_pg_catalog`.

    A single-process DuckDB-file catalog target gets no live reachability
    check beyond the extension probe — ``_attach_ducklake`` creates the
    file/directory on first write if it doesn't exist yet, so there is
    nothing meaningful to pre-flight there.
    """
    problems: list[str] = []

    if not ducklake_available():
        problems.append(
            "the 'ducklake' DuckDB extension could not be installed/loaded in this "
            "environment — check network egress to the DuckDB extension repository "
            "(or vendor the extension locally for an air-gapped deploy)"
        )

    catalog_dsn = ducklake_catalog_dsn()
    data_path = ducklake_data_path()

    if is_postgres_dsn(catalog_dsn):
        problems.extend(_probe_or_repair_ducklake_pg_catalog(catalog_dsn, data_path))
    else:
        from app.startup_guards import is_multi_process

        if is_multi_process():
            problems.append(
                "this is a multi-process deployment (role split or UVICORN_WORKERS>1) "
                "but ducklake.catalog_dsn (or AGNES_DUCKLAKE_CATALOG_DSN) resolves to a "
                "DuckDB-file path — a file catalog is hard single-process; set an "
                "explicit Postgres DSN before migrating"
            )

    return problems
