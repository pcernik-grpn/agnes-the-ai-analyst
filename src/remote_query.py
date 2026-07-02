"""RemoteQueryEngine — two-phase BQ registration + DuckDB execution.

Phase 1 (register_bq): validate SQL, COUNT(*) pre-check against BigQuery,
fetch Arrow table, check memory, register as DuckDB view.

Phase 2 (execute): validate SQL, execute against DuckDB (which may reference
registered BQ views), serialize and return results.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import duckdb

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

_RESERVED_ALIASES = {
    "information_schema", "duckdb_tables", "duckdb_columns",
    "duckdb_databases", "duckdb_settings", "duckdb_functions",
    "duckdb_views", "duckdb_indexes", "duckdb_schemas",
    "main", "memory", "system", "temp",
}

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL blocklist — based on app/api/query.py, extended with additional DuckDB metadata tables
# ---------------------------------------------------------------------------

_BLOCKED_KEYWORDS: List[str] = [
    "drop ",
    "delete ",
    "insert ",
    "update ",
    "alter ",
    "create ",
    "copy ",
    "attach ",
    "detach ",
    "load ",
    "install ",
    "export ",
    "import ",
    "pragma ",
    "call ",
    # File access functions
    "read_csv",
    "read_json",
    "read_parquet",
    "read_text",
    "write_csv",
    "write_parquet",
    "read_blob",
    "read_ndjson",
    "parquet_scan",
    "parquet_metadata",
    "parquet_schema",
    "json_scan",
    "csv_scan",
    "query_table",
    "iceberg_scan",
    "delta_scan",
    "glob(",
    "list_files",
    "'/",
    '\"/',
    "http://",
    "https://",
    "s3://",
    "gcs://",
    # DuckDB metadata (leaks schema info regardless of RBAC)
    "information_schema",
    "duckdb_tables",
    "duckdb_columns",
    "duckdb_databases",
    "duckdb_settings",
    "duckdb_functions",
    "duckdb_views",
    "duckdb_indexes",
    "duckdb_schemas",
    "pragma_table_info",
    "pragma_storage_info",
    # Relative path traversal
    "'../",
    '"../',
    # Multiple statements
    ";",
]


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class RemoteQueryError(Exception):
    """Raised by RemoteQueryEngine for all controlled error conditions.

    Attributes:
        error_type: One of "row_limit", "memory_limit", "bq_error",
                    "query_error", "timeout".
        details: Optional dict with additional context.
    """

    def __init__(
        self,
        message: str,
        error_type: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.details = details or {}


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _strip_leading_sql_comments(sql: str) -> str:
    """Return *sql* with leading SQL comments removed so a ``^(select|with)``
    check sees the first real statement keyword.

    Strips any stack of leading ``--`` line comments and ``/* … */`` block
    comments (plus surrounding whitespace). DuckDB and BigQuery both tolerate
    leading comments, so this lets the SELECT/WITH guard agree with them and
    with the local ``agnes query`` path. Used ONLY for the starts-with-a-keyword
    check — callers keep scanning the original SQL with the blocklist, so a
    comment can never be used to smuggle a blocked keyword through.
    """
    s = sql
    while True:
        stripped = s.lstrip()
        if stripped.startswith("--"):
            # Line comment runs to the next newline (or end of string).
            newline = stripped.find("\n")
            s = "" if newline == -1 else stripped[newline + 1:]
            continue
        if stripped.startswith("/*"):
            # Search for the closing */ AFTER the two-char opener, so a comment
            # like `/*/ …` doesn't match the opener's own `*` + the next `/` as
            # a spurious close (which would leave the real statement's guard
            # bypassed while DuckDB parses on to the true close).
            end = stripped.find("*/", 2)
            if end == -1:
                # Unterminated block comment — leave it so the guard rejects.
                return stripped
            s = stripped[end + 2:]
            continue
        return stripped


def _validate_sql(sql: str) -> None:
    """Raise RemoteQueryError if *sql* contains blocked patterns.

    Raises:
        RemoteQueryError: with error_type="query_error" if validation fails.
    """
    sql_lower = sql.strip().lower()

    for keyword in _BLOCKED_KEYWORDS:
        if keyword in sql_lower:
            raise RemoteQueryError(
                f"Blocked SQL pattern: {keyword!r}",
                error_type="query_error",
                details={"blocked_keyword": keyword},
            )

    import re as _re
    if not _re.match(r"^(select|with)\s", _strip_leading_sql_comments(sql_lower)):
        raise RemoteQueryError(
            "Query must start with SELECT or WITH",
            error_type="query_error",
        )


# BQ SQL blocklist — only blocks write/mutation operations
_BQ_BLOCKED_KEYWORDS = [
    "drop ",
    "delete ",
    "insert ",
    "update ",
    "alter ",
    "create ",
    "truncate ",
    "merge ",
    ";",  # prevent multi-statement
]


def _validate_bq_sql(sql: str) -> None:
    """Validate BQ SQL — narrower than DuckDB blocklist, only blocks writes."""
    sql_lower = sql.strip().lower()
    for keyword in _BQ_BLOCKED_KEYWORDS:
        if keyword in sql_lower:
            raise RemoteQueryError(
                f"Blocked BQ SQL keyword: {keyword.strip()}",
                error_type="query_error",
            )
    import re as _re
    if not _re.match(r"^(select|with)\s", _strip_leading_sql_comments(sql_lower)):
        raise RemoteQueryError(
            "BQ query must start with SELECT or WITH",
            error_type="query_error",
        )


def load_config() -> Dict[str, Any]:
    """Load the ``remote_query:`` section from instance.yaml.

    Returns an empty dict if the section is missing or config cannot be loaded.
    """
    try:
        from app.instance_config import get_value

        return get_value("remote_query", default={}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class RemoteQueryEngine:
    """Two-phase query engine: BQ registration (Phase 1) + DuckDB execution (Phase 2).

    Args:
        conn: Open DuckDB connection used for both view registration and querying.
        bq_access: Optional ``BqAccess`` instance for BigQuery access. When ``None``
            (the default), it is resolved lazily on first BQ call via
            ``connectors.bigquery.access.get_bq_access()``. Pure-DuckDB callers that
            never invoke ``register_bq`` can leave it ``None`` even in environments
            without BQ config.
        max_bq_registration_rows: Maximum rows allowed in a single BQ registration.
        max_memory_mb: Maximum in-memory Arrow table size (MiB).
        max_result_rows: Maximum rows returned by ``execute()``.
        timeout_seconds: Query timeout (reserved for future use).
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        *,
        bq_access: "BqAccess | None" = None,
        max_bq_registration_rows: int = 500_000,
        max_memory_mb: float = 2048.0,
        max_result_rows: int = 100_000,
        timeout_seconds: int = 300,
    ) -> None:
        self._conn = conn
        self._bq = bq_access
        self.max_bq_registration_rows = max_bq_registration_rows
        self.max_memory_mb = max_memory_mb
        self.max_result_rows = max_result_rows
        self.timeout_seconds = timeout_seconds

        # Track which aliases have been registered in this session
        self._registered: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Phase 1
    # ------------------------------------------------------------------

    def register_bq(
        self, alias: str, bq_sql: str, *, job_labels: dict[str, str] | None = None
    ) -> Dict[str, Any]:
        """Register a BigQuery query result as a DuckDB view.

        Steps:
        1. Validate *bq_sql* against the SQL blocklist.
        2. COUNT(*) pre-check via BQ client.
        3. Execute the actual BQ query and fetch as Arrow table.
        4. Check in-memory size against *max_memory_mb*.
        5. Register Arrow table in DuckDB under *alias*.

        Args:
            alias: DuckDB view name to register (e.g. ``"bq_orders"``).
            bq_sql: SQL query to execute on BigQuery.
            job_labels: Optional BQ job labels for cost attribution.

        Returns:
            ``{alias, rows, columns, memory_mb}``

        Raises:
            RemoteQueryError: For row/memory limits or BQ errors.
            ImportError: If google-cloud-bigquery is not installed.
        """
        if not _SAFE_IDENTIFIER.match(alias or ""):
            raise RemoteQueryError(
                f"Invalid alias {alias!r}: must be a valid SQL identifier",
                error_type="query_error",
            )
        if alias.lower() in _RESERVED_ALIASES:
            raise RemoteQueryError(
                f"Reserved alias {alias!r}: cannot shadow system objects",
                error_type="query_error",
            )

        _validate_bq_sql(bq_sql)

        client = self._get_bq_client()

        # FAI-105: tag the BQ jobs for per-user/workload cost attribution.
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(labels=job_labels) if job_labels else None

        # --- Phase 1a: COUNT(*) pre-check ---
        count_sql = f"SELECT COUNT(*) FROM ({bq_sql}) AS _cnt"
        try:
            count_job = client.query(count_sql, job_config=job_config)
            count_arrow = count_job.to_arrow()
            count_value = int(count_arrow.column(0)[0].as_py())
        except RemoteQueryError:
            raise
        except Exception as exc:
            raise RemoteQueryError(
                f"BQ COUNT pre-check failed: {exc}",
                error_type="bq_error",
                details={"original_error": str(exc)},
            ) from exc

        if count_value > self.max_bq_registration_rows:
            raise RemoteQueryError(
                f"BQ result has {count_value:,} rows, exceeding the "
                f"limit of {self.max_bq_registration_rows:,}.",
                error_type="row_limit",
                details={
                    "count": count_value,
                    "max": self.max_bq_registration_rows,
                },
            )

        # --- Phase 1b: Fetch actual data ---
        try:
            data_job = client.query(bq_sql, job_config=job_config)
            try:
                arrow_table = data_job.to_arrow()
            except Exception as storage_exc:
                if "readsessions" in str(storage_exc) or "PERMISSION_DENIED" in str(storage_exc):
                    logger.warning("BQ Storage API unavailable, falling back to REST")
                    arrow_table = data_job.to_arrow(create_bqstorage_client=False)
                else:
                    raise
        except RemoteQueryError:
            raise
        except Exception as exc:
            raise RemoteQueryError(
                f"BQ query failed: {exc}",
                error_type="bq_error",
                details={"original_error": str(exc)},
            ) from exc

        # --- Phase 1c: Memory check (accurate, post-fetch) ---
        memory_mb = arrow_table.nbytes / (1024 * 1024)
        if memory_mb > self.max_memory_mb:
            raise RemoteQueryError(
                f"Arrow table uses {memory_mb:.1f} MiB, exceeding the "
                f"limit of {self.max_memory_mb:.1f} MiB.",
                error_type="memory_limit",
                details={"memory_mb": memory_mb, "max_memory_mb": self.max_memory_mb},
            )

        # --- Phase 1d: Register in DuckDB ---
        self._conn.register(alias, arrow_table)

        info: Dict[str, Any] = {
            "alias": alias,
            "rows": arrow_table.num_rows,
            "columns": arrow_table.schema.names,
            "memory_mb": memory_mb,
        }
        self._registered[alias] = info
        logger.info(
            "Registered BQ alias %r: %d rows, %.2f MiB",
            alias,
            arrow_table.num_rows,
            memory_mb,
        )
        return info

    # ------------------------------------------------------------------
    # Phase 2
    # ------------------------------------------------------------------

    def execute(self, sql: str) -> Dict[str, Any]:
        """Execute SQL against DuckDB (which may reference registered BQ views).

        Args:
            sql: SQL query to execute. Must pass the SQL blocklist.

        Returns:
            ``{columns, rows, row_count, truncated, bq_stats}``

        Raises:
            RemoteQueryError: If SQL is blocked or a DuckDB error occurs.
        """
        _validate_sql(sql)

        try:
            result = self._conn.execute(sql).fetchmany(self.max_result_rows + 1)
            columns = (
                [desc[0] for desc in self._conn.description]
                if self._conn.description
                else []
            )
        except RemoteQueryError:
            raise
        except Exception as exc:
            raise RemoteQueryError(
                f"Query error: {exc}",
                error_type="query_error",
                details={"original_error": str(exc)},
            ) from exc

        truncated = len(result) > self.max_result_rows
        rows = result[: self.max_result_rows]

        # Serialize non-standard types (mirrors app/api/query.py lines 92-96)
        serializable_rows = []
        for row in rows:
            serializable_rows.append(
                [
                    str(v) if v is not None and not isinstance(v, (int, float, bool, str)) else v
                    for v in row
                ]
            )

        return {
            "columns": columns,
            "rows": serializable_rows,
            "row_count": len(serializable_rows),
            "truncated": truncated,
            "bq_stats": {
                "registered_aliases": list(self._registered.keys()),
                "alias_count": len(self._registered),
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_bq_client(self):
        """Lazy-resolve BqAccess on first use. Many tests construct RemoteQueryEngine
        for DuckDB-only paths and never touch BQ — those must not fail with not_configured.

        Translates ``BqAccessError`` (e.g. ``bq_lib_missing``, ``not_configured``) into
        ``RemoteQueryError(error_type="bq_error")`` to preserve the engine's existing
        contract with its callers.
        """
        if self._bq is None:
            from connectors.bigquery.access import get_bq_access, BqAccessError
            try:
                self._bq = get_bq_access()
            except BqAccessError as exc:
                raise RemoteQueryError(
                    f"BigQuery access unavailable: {exc.message}",
                    error_type="bq_error",
                    details={"kind": exc.kind, **exc.details},
                ) from exc
        try:
            return self._bq.client()
        except Exception as exc:
            from connectors.bigquery.access import BqAccessError
            if isinstance(exc, BqAccessError):
                raise RemoteQueryError(
                    f"BigQuery access unavailable: {exc.message}",
                    error_type="bq_error",
                    details={"kind": exc.kind, **exc.details},
                ) from exc
            raise
