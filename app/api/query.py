"""Query endpoint — execute SQL against server DuckDB."""

import contextlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
import duckdb

from app.auth.access import is_user_admin
from app.auth.dependencies import get_current_user, _get_db
from app.auth.session_principal import SessionPrincipal
from app.instance_config import get_value
from connectors.internal.access import (
    InternalAccessError,
    execute_internal_query,
    find_internal_refs,
    is_internal_table,
)

from src.repositories import (
    audit_repo,
    table_registry_repo,
)
from src.audit_helpers import client_kind_from_user
from src.db import get_analytics_db_readonly
from src.rbac import get_accessible_tables
from src.remote_query import _strip_leading_sql_comments

# Imported at module level so tests can monkeypatch via
# `app.api.query._bq_dry_run_bytes` without resolving lazy imports inside
# the handler (reaches the patched attribute on each call). Same for
# get_bq_access — sibling module, dep direction doesn't matter (both are
# leaves under app.api).
from app.api.v2_quota import _build_quota_tracker, QuotaExceededError
from app.api.v2_scan import _bq_dry_run_bytes
from connectors.bigquery.access import get_bq_access, BqAccessError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/query", tags=["query"])

# ---------------------------------------------------------------------------
# Per-session BQ scan budget (Phase 12.3)
# ---------------------------------------------------------------------------

# In-memory counter: session_id → cumulative bytes scanned this session.
# Keyed by chat session JWT claim ``chat_session_id`` (set by Task 13.1).
# Resets on server restart (intentional — sessions are short-lived).
_per_session_bq_bytes: dict[str, int] = {}

_DEFAULT_PER_SESSION_BQ_BYTES = 20 * 1024**3  # 20 GiB


def _maybe_charge_chat_session_bq_budget(request, scan_bytes: int) -> None:
    """If the request was authenticated under a chat-scope JWT (claim stashed
    on ``request.state.chat_session_id`` by ``app.auth.dependencies``), charge
    ``scan_bytes`` against that chat session's per-session BigQuery budget.

    Regular (non-chat) /api/query callers leave ``request.state.chat_session_id``
    unset and silently skip this — they're already capped by the per-user
    daily/concurrent BQ guards in v2_quota.
    """
    session_id = getattr(getattr(request, "state", None), "chat_session_id", None)
    if not session_id:
        return
    cfg = None
    try:
        cfg = request.app.state.chat_config
    except Exception:
        cfg = None
    limit_bytes = (
        cfg.per_session_bq_scan_bytes if cfg is not None else _DEFAULT_PER_SESSION_BQ_BYTES
    )
    accumulate_session_bq_bytes(session_id, scan_bytes, limit_bytes=limit_bytes)


def accumulate_session_bq_bytes(
    session_id: str,
    scan_bytes: int,
    *,
    limit_bytes: int = _DEFAULT_PER_SESSION_BQ_BYTES,
) -> None:
    """Accumulate ``scan_bytes`` for ``session_id`` and raise HTTPException
    (400 / ``bq_budget_exhausted``) if the cumulative total exceeds
    ``limit_bytes``.

    Called from the BQ scan guard after the dry-run resolves ``total_bytes``.
    Integration with the request auth path is deferred to Task 13.1 — that
    task wires ``chat_session_id`` from the JWT into ``request.state`` so the
    execute_query handler can pass it through here.
    """
    current = _per_session_bq_bytes.get(session_id, 0)
    new_total = current + scan_bytes
    _per_session_bq_bytes[session_id] = new_total
    if limit_bytes > 0 and new_total > limit_bytes:
        raise HTTPException(status_code=400, detail={
            "reason": "bq_budget_exhausted",
            "session_id": session_id,
            "scan_bytes_cumulative": new_total,
            "limit_bytes": limit_bytes,
            "suggestion": (
                "Per-session BigQuery scan budget exhausted. "
                "Start a new chat session to reset the quota."
            ),
        })


# Heuristic: did the BQ-side execution of a `bigquery_query()`-rewritten
# query reject the inner SQL because of a **DuckDB-vs-BQ dialect mismatch**
# specifically? We want to fall back ONLY on cases where the same SQL
# would have worked under the legacy DuckDB ATTACH-catalog path —
# DuckDB-only syntax (``::INT`` casts, ``STRPTIME``, COALESCE arity quirks)
# that BQ's parser rejects.
#
# We DO NOT want to fall back on user-data errors that BQ would reject in
# either path (unknown column name, wrong function signature, invalid cast
# of literal user input). For those, the legacy ATTACH path would issue
# the same query and fail the same way — just 50-100× slower. Triggering
# fallback there is a 2× latency tax on every typo (devil's-advocate R1
# finding #2).
#
# Conservative pattern set: only the BQ-emitted ``Syntax error: <detail>``
# (with trailing colon) covers genuine parse-level dialect mismatch.
# ``Unrecognized name`` etc. surface for both bad-user-column AND
# DuckDB-only-name cases — the safe assumption is that user-column-typo
# is the more common case, so we don't fall back. If a deployment
# surfaces a real DuckDB-only-name regression, it's better caught as
# a BinderException with the original SQL in the logs than amplified
# via slow-path retry.
#
# The trailing colon (devil's-advocate R2 finding #3) anchors the match
# against BQ's verbatim error format and avoids false positives where
# the literal substring `Syntax error` appears in a user's SQL string
# literal that DuckDB then echoes back in an unrelated error message
# (e.g. `WHERE log_msg = 'Syntax error in foo'` failing on quota).
_BQ_REWRITE_PARSE_ERROR_PATTERNS = (
    "Syntax error: ",
    "syntax error: ",
)


def _looks_like_bq_rewrite_parse_error(exc: BaseException) -> bool:
    """Return True when ``exc`` is the BQ-rejected-inner-SQL flavour we
    want to fall back from. Conservative: matches against the exception
    message text only, no isinstance checks, so it works whether the
    DuckDB BQ extension wrapped the error as BinderException, IOException,
    or a plain Python Exception."""
    msg = str(exc)
    return any(pat in msg for pat in _BQ_REWRITE_PARSE_ERROR_PATTERNS)


def _hint_for_bq_bad_request(message: str) -> str:
    """Pick the most useful one-line hint for a BigQuery `bad_request`
    error message. The default "column doesn't exist" hint is correct
    for ~half of BQ rejections (`Unrecognized name: foo`,
    `Field foo not found in record`) but actively misleading when BQ
    actually rejected on syntax (`Syntax error: Unexpected keyword
    ROWS at [1:20]` — reserved-keyword alias without quoting,
    extremely common because `rows` / `range` / `groups` / `window`
    are all reserved). Branch on the BQ message to pick the right hint
    rather than always blaming columns."""
    msg = message.lower()
    if "unexpected keyword" in msg or "syntax error" in msg:
        # Plain text — this string is surfaced as JSON `hint:` and printed
        # verbatim by the CLI. No markdown rendering, so avoid backtick
        # quoting around BQ-style backtick identifiers (`\\\`` escape in
        # a Python source literal renders the backslashes literally to
        # the analyst — exactly the misleading shape this hint tries to
        # fix).
        return (
            "BigQuery rejected this on SQL syntax. Most often this is a "
            "reserved-keyword identifier used unquoted — e.g. "
            "SELECT COUNT(*) AS rows fails because 'rows' is reserved. "
            "Either rename the alias to a non-reserved word (AS row_count) "
            "or backtick-quote it BQ-style (AS `rows` with literal "
            "backticks around the identifier). For other syntax errors, "
            "see the 'underlying' field below — it carries BigQuery's own "
            "diagnostic with the error position."
        )
    if "unrecognized name" in msg or "not found inside" in msg or "field name" in msg:
        return (
            "BigQuery rejected this because a column referenced in "
            "WHERE/SELECT/etc doesn't exist on the table. Verify with "
            "`agnes schema <id>`."
        )
    if "table not found" in msg or "not found:" in msg:
        return (
            "BigQuery rejected this because the table reference doesn't "
            "exist. Use a registered table id from `agnes catalog`, or "
            "write a full backtick path like `` `<project>.<dataset>.<table>` ``."
        )
    return (
        "BigQuery rejected this query during cost estimation. See the "
        "`underlying` field for BigQuery's own diagnostic; common causes "
        "are missing columns (verify with `agnes schema <id>`), "
        "reserved-keyword aliases, or unregistered table paths."
    )

# Issue #160 §4.3.1 — direct `bq.<dataset>.<source_table>` references in user
# SQL. Catalog token accepts both `bq` (the unquoted DuckDB-style name) and
# `"bq"` (quoted identifier). DuckDB resolves both to the same ATTACHed
# catalog, so the security-boundary regex must accept both — Phase 3 review
# caught the quoted variant as an RBAC + cost-cap bypass.
# Lookahead `(?=\W|$)` works where `\b` doesn't (after a closing quote).
# Negative lookbehind `(?<![\w.])` rejects `other_bq.x.y`, `my_bq.ds.tbl`,
# and `x.bq.y.z` so the regex doesn't fire on column qualifiers or
# look-alike-prefixed identifiers.
BQ_PATH = re.compile(
    r'(?<![\w.])(?:"bq"|bq)\s*\.\s*("[^"]+"|\w+)\s*\.\s*("[^"]+"|\w+)(?=\W|$)',
    re.IGNORECASE,
)


# Issue #201 — full backtick BQ path `<project>.<dataset>.<table>` in user
# SQL. Used by the registry-gating pass and (via `_mask_backticks`) to keep
# bare-name regexes from firing inside backtick-quoted segments.
_BACKTICK_SEGMENT = re.compile(r'`[^`]*`')
_BACKTICK_FULL_PATH = re.compile(r'`([^.`]+)\.([^.`]+)\.([^.`]+)`')


def _mask_backticks(sql: str) -> str:
    """Replace each `…`-quoted segment with spaces of equal length so
    word-boundary regexes find positions outside backticks but ignore
    everything inside. Preserves all character offsets so ``re.search``
    on the masked string returns matches at the same positions as on the
    original.

    Issue #201: `\\b` matches inside backtick segments because both `.`
    and `` ` `` are non-word characters. A registered bare-name like
    ``unit_economics`` would otherwise match inside a user-supplied full
    backtick path ``\\`<project>.<dataset>.unit_economics\\``` and get
    falsely rewritten — corrupting the user's intended SQL.
    """
    return _BACKTICK_SEGMENT.sub(lambda m: ' ' * len(m.group(0)), sql)


def _default_remote_query_cap_bytes() -> int:
    """5 GiB default cap on /api/query BQ-touching scans. Configurable via
    `data_source.bigquery.bq_max_scan_bytes` in /admin/server-config —
    sits next to `max_bytes_per_materialize` for visual symmetry.
    """
    raw = get_value("data_source", "bigquery", "bq_max_scan_bytes", default=5_368_709_120)
    try:
        return int(raw) if raw is not None else 5_368_709_120
    except (TypeError, ValueError):
        return 5_368_709_120


class QueryRequest(BaseModel):
    sql: str
    limit: int = 1000


class QueryResponse(BaseModel):
    columns: list
    rows: list
    row_count: int
    truncated: bool = False
    # BigQuery dry-run scan estimate (bytes) for `query_mode='remote'`
    # queries; ``None`` for local DuckDB queries (no BQ tables involved).
    bytes_scanned: Optional[int] = None


def _run_internal_query(
    request: "QueryRequest",
    user: dict,
    conn: duckdb.DuckDBPyConnection,
    t0: float,
    internal_refs: list[str],
) -> "QueryResponse":
    """Execute a SELECT against system.duckdb under per-request RBAC views.

    Builds a fresh read-only connection, materialises one TEMP VIEW per
    referenced internal table with the appropriate row filter, runs the
    user SQL, and writes an audit row. Errors are converted to 400 with
    a hint that points at ``agnes catalog`` for the registered ids.
    """
    from src.db import _get_state_dir
    system_db_path = str(_get_state_dir() / "system.duckdb")
    # is_user_admin takes (user_id, conn) — passing the dict raises
    # TypeError, which is exactly the regression review #278/1 caught.
    # A SessionPrincipal has no .get("id") — treat co-session as non-admin
    # for internal row-level filter. build_filter_clause expects a dict
    # so pass a shim when user is a principal (mirrors v2_sample.py:131-137).
    # The shim id "session.none" passes the safe-identifier regex but never
    # matches a real user_id; the email local-part "session.none" similarly
    # passes the username regex but matches no real session. Together the
    # filter yields zero rows for every internal table — correct behaviour
    # (co-sessions should not see any single user's internal rows).
    if isinstance(user, SessionPrincipal):
        is_admin = False
        user = {"id": "session.none", "email": "session.none@internal"}
    else:
        is_admin = is_user_admin(user.get("id"), conn) if user.get("id") else False
    try:
        columns, rows, truncated = execute_internal_query(
            system_db_path=system_db_path,
            user=user,
            is_admin=is_admin,
            sql=request.sql,
            limit=request.limit,
        )
    except InternalAccessError as exc:
        raise HTTPException(status_code=400, detail=f"Internal query rejected: {exc}")
    except duckdb.Error as exc:
        raise HTTPException(status_code=400, detail=f"DuckDB error: {exc}")

    serializable = [
        [str(v) if v is not None and not isinstance(v, (int, float, bool, str)) else v
         for v in row]
        for row in rows
    ]
    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="query.internal",
            resource=("table:" + ",".join(internal_refs))[:256],
            params={
                "sql_preview": (request.sql or "")[:200],
                "internal_tables": internal_refs,
                "is_admin": is_admin,
                "rows_returned": len(serializable),
                "duration_ms": int((time.monotonic() - t0) * 1000),
            },
            result="success",
            client_kind=client_kind_from_user(user),
        )
    except Exception:
        logger.exception("audit_log write failed for query.internal; continuing")
    return QueryResponse(
        columns=columns,
        rows=serializable,
        row_count=len(serializable),
        truncated=truncated,
    )


def _first_table_from_sql(sql: str) -> Optional[str]:
    """Extract the first identifier after FROM or JOIN for audit resource tagging.

    Regex-based; best-effort. Returns None when no table reference is found.
    Does not need to be accurate — it's only for audit diagnostics.
    """
    m = re.search(r'\b(?:from|join)\s+(["\`]?[\w.]+["\`]?)', sql, re.IGNORECASE)
    if m:
        return m.group(1).strip('"\'`')[:200]
    return None


# SQL keywords / functions rejected on every user-submitted query path. Shared
# by `execute_query` (the /api/query handler) and `run_remote_select_to_arrow`
# (the snapshot `from_query` materialize path) so the two surfaces can never
# drift on what counts as a safe single-SELECT.
_BLOCKED_SQL_TOKENS = [
    "drop ", "delete ", "insert ", "update ", "alter ", "create ",
    "copy ", "attach ", "detach ", "load ", "install ",
    "export ", "import ", "pragma ", "call ",
    # File access functions
    "read_csv", "read_json", "read_parquet", "read_text",
    "write_csv", "write_parquet", "read_blob", "read_ndjson",
    "parquet_scan", "parquet_metadata", "parquet_schema",
    "json_scan", "csv_scan",
    "query_table", "iceberg_scan", "delta_scan",
    # #160: bigquery_query() bypasses the registry / RBAC entirely
    # (it runs an arbitrary BQ jobs API call against any reachable
    # dataset). Wrap views created by the BQ extractor use it inside
    # CREATE VIEW bodies, but those run via DuckDB's view resolution at
    # query time — user-submitted SQL never contains the function name.
    "bigquery_query",
    "glob(", "list_files",
    "'/", '"/', 'http://', 'https://', 's3://', 'gcs://',
    # DuckDB metadata (leaks schema info regardless of RBAC)
    "information_schema", "duckdb_tables", "duckdb_columns",
    "duckdb_databases", "duckdb_settings", "duckdb_functions",
    "duckdb_views", "duckdb_indexes", "duckdb_schemas",
    "pragma_table_info", "pragma_storage_info",
    # Relative path traversal
    "'../", '"../',
    # Multiple statements
    ";",
]


def _assert_select_only(sql_lower: str) -> None:
    """Raise HTTPException(400) unless ``sql_lower`` is a single SELECT/WITH
    query free of the blocked keywords/functions. ``sql_lower`` MUST already
    be ``.strip().lower()``-ed by the caller."""
    if any(keyword in sql_lower for keyword in _BLOCKED_SQL_TOKENS):
        raise HTTPException(status_code=400, detail="Only single SELECT queries are allowed")
    # Accept any whitespace (newline, tab, space) after the keyword so
    # multi-line SQL doesn't 400 on `SELECT\n  col, ...`. Strip leading `--`
    # / `/* */` comments first so a query whose stored SQL opens with a header
    # comment isn't rejected — DuckDB and the local `agnes query` path tolerate
    # them. The blocklist above still scans the full SQL, so a comment can't
    # smuggle a blocked keyword through.
    if not re.match(r"^(select|with)\s", _strip_leading_sql_comments(sql_lower)):
        raise HTTPException(status_code=400, detail="Query must start with SELECT or WITH")


@router.post("", response_model=QueryResponse)
def execute_query(
    request: QueryRequest,
    http_request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Execute SQL against the server analytics DuckDB.

    Plain ``def`` (not ``async def``) so FastAPI auto-offloads the call
    to the anyio thread pool. The body invokes ``analytics.execute(sql)``
    synchronously, which blocks for the full BQ jobs.query wait when a
    referenced view resolves through the BQ extension. Under ``async def``
    that block holds the single uvicorn event loop, freezing every other
    request (UI, /api/health, auth) until the query returns. Plain ``def``
    runs each invocation on its own thread, so heavy queries no longer
    starve unrelated endpoints. See PR #188's CHANGELOG entry for the
    Tier 1 event-loop unblocking rollout.
    """
    _t0 = time.monotonic()
    sql_lower = request.sql.strip().lower()

    _assert_select_only(sql_lower)

    # ----- Internal-source short-circuit ----------------------------------
    # SQL referencing one of the seeded internal tables (agnes_sessions,
    # agnes_usage, agnes_audit) is executed against system.duckdb with a
    # per-request RBAC view, not against analytics.duckdb. Mixing internal
    # + BQ/local refs in one SQL is rejected — the two backends live in
    # different DuckDB instances, joining across requires materialising
    # one side and isn't worth the complexity in v1. The BQ guardrail
    # logic doesn't apply (no BQ traffic), and the view-name RBAC check
    # below doesn't either (internal tables don't live in analytics.duckdb
    # at all).
    internal_refs = find_internal_refs(request.sql)
    if internal_refs:
        if BQ_PATH.search(_mask_backticks(request.sql)) or _BACKTICK_FULL_PATH.search(request.sql):
            raise HTTPException(
                status_code=400,
                detail="Internal tables can't be combined with `bq.*` paths in a "
                       "single SELECT (v1 limitation).",
            )
        # Reject if user SQL also mentions any non-internal registry id —
        # that would be a mixed query against analytics.duckdb views.
        registry_rows = table_registry_repo().list_all()
        for r in registry_rows:
            rid = r.get("id") or ""
            if not rid or is_internal_table(rid):
                continue
            if re.search(rf"\b{re.escape(rid)}\b", sql_lower):
                raise HTTPException(
                    status_code=400,
                    detail=f"Internal tables can't be joined with registered "
                           f"table {rid!r} in a single SELECT (v1 limitation).",
                )
        return _run_internal_query(request, user, conn, _t0, internal_refs)

    # Get allowed tables for this user
    allowed = get_accessible_tables(user, conn)

    analytics = get_analytics_db_readonly()
    # Track whether this query touched BQ-remote tables (set below in _bq_guardrail_inputs).
    # Used for audit action selection (query.remote vs query.local) and bytes_scanned.
    _dry_run_set: list = []
    try:
        if allowed is not None:  # None = admin, sees all
            # Get all views in analytics DB
            all_views = {row[0] for row in analytics.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
            ).fetchall()}

            # `allowed` carries registry IDs (resource_grants.resource_id);
            # DuckDB master views are named by registry display `name`.
            # Build a name->id map so the forbidden check compares apples to
            # apples — when id != name, the prior `all_views - set(allowed)`
            # over-denied authorized users (Devin Review iter #5 on PR #168;
            # pre-existing class of name/id mismatch flagged across this
            # PR's BQ guardrail too).
            allowed_ids = set(allowed)
            registry_rows = table_registry_repo().list_all()
            allowed_view_names = {
                r["name"] for r in registry_rows
                if r.get("name") and r.get("id") in allowed_ids
            }

            # Check if query references any forbidden tables (word-boundary
            # match). Issue #201: mask backtick segments so `\b` doesn't
            # falsely fire inside a user-supplied full backtick path like
            # `<project>.<dataset>.<table>` whose final segment happens to
            # collide with a forbidden master view name. The full-path
            # registry-gate downstream is the proper authorization check
            # for those.
            sql_lower_masked = _mask_backticks(sql_lower)
            forbidden = all_views - allowed_view_names
            for table in forbidden:
                pattern = r'\b' + re.escape(table.lower()) + r'\b'
                if re.search(pattern, sql_lower_masked):
                    from src.rbac import table_not_in_stack_message
                    raise HTTPException(
                        status_code=403,
                        detail=table_not_in_stack_message(table),
                    )

        # ---- #160 BQ remote-row guardrail + RBAC patch -------------------
        dry_run_set, name_lookups, blocked_bq_path = _bq_guardrail_inputs(
            request.sql, sql_lower, conn, user, allowed,
        )
        _dry_run_set = dry_run_set  # expose to outer scope for audit
        if blocked_bq_path is not None:
            raise HTTPException(status_code=403, detail=blocked_bq_path)

        # Issue #160 §4.3.3 — concurrent-slot guard MUST wrap the actual
        # `analytics.execute(request.sql)` call (which is what triggers the
        # BQ scan when DuckDB resolves the master view), not just the
        # dry-run. Devin Review on PR #168 caught this — earlier
        # implementation released the slot before execute. Use a context
        # manager so dry-run + cap check + execute + record_bytes all run
        # inside the slot.
        # Match /api/v2/scan's user_id key shape (`email or "anon"`) so the
        # shared QuotaTracker singleton sees the SAME key for both endpoints.
        # Earlier `id or email` ordering keyed BQ bytes on UUID for /api/query
        # vs email for /api/v2/scan — the per-user daily cap was effectively
        # doubled because the two paths tracked under different keys.
        # Devin Review #2 caught this on PR #168.
        user_id = user.get("email") or user.get("id") or "anon"
        guard = (
            _bq_quota_and_cap_guard(
                user_id=user_id,
                user=user,
                dry_run_set=dry_run_set,
                name_lookups=name_lookups,
                sql=request.sql,
            )
            if dry_run_set
            else contextlib.nullcontext()
        )
        with guard:
            # Performance fix: rewrite user SQL referencing BQ-remote tables
            # to a single ``bigquery_query()`` call so WHERE / projection /
            # LIMIT push into BQ via jobs.query (1-2 s) instead of falling
            # through DuckDB's ATTACH-catalog Storage Read API session over
            # the full table (often 70-150 s, fails with "Response too
            # large to return" on >100M-row sources). Helper returns the
            # original SQL unchanged when rewriting would be unsafe
            # (cross-source JOIN, no BQ tables referenced, double-wrap).
            execution_sql, did_rewrite = _rewrite_user_sql_for_bigquery_query(
                request.sql, conn,
            )
            if did_rewrite:
                # Memory-safety: ``bigquery_query()`` materialises the entire
                # BQ result into DuckDB before fetchmany sees it (vs the
                # ATTACH-catalog Storage Read API path, which streams rows
                # lazily). Wrap the rewritten SQL in an outer ``LIMIT N+1``
                # so a `SELECT *` against a billion-row remote table doesn't
                # buffer the full table into the worker process — the cap
                # is pushed into the BQ job itself. Aliased subquery so the
                # outer LIMIT applies to the final rewritten result.
                execution_sql = (
                    f"SELECT * FROM ({execution_sql}) AS _bqq_outer "
                    f"LIMIT {request.limit + 1}"
                )
                logger.info(
                    "query_rewrite_to_bigquery_query: user_id=%s — wrapped "
                    "SQL in bigquery_query() with outer LIMIT for BQ "
                    "predicate pushdown",
                    user_id,
                )
            else:
                logger.debug(
                    "query_rewrite_skipped: user_id=%s — running original "
                    "SQL via ATTACH-catalog path",
                    user_id,
                )

            # Open in read-only mode for extra safety. If the rewritten
            # path errors (e.g. user SQL contained DuckDB-only syntax —
            # ``::INT`` casts, ``STRPTIME``, COALESCE arity differences —
            # that survives identifier rewrite but BQ refuses), fall back
            # to the original SQL via the legacy ATTACH-catalog path so
            # the request still succeeds (slower, but correct). Same
            # safety contract as the dry-run fallback in
            # ``_bq_quota_and_cap_guard``.
            try:
                result = analytics.execute(execution_sql).fetchmany(request.limit + 1)
            except Exception as exc:
                if did_rewrite and _looks_like_bq_rewrite_parse_error(exc):
                    logger.warning(
                        "query_rewrite_fallback: user_id=%s — bigquery_query() "
                        "rewrite rejected by BQ (%s); retrying via "
                        "ATTACH-catalog path",
                        user_id, type(exc).__name__,
                    )
                    result = analytics.execute(request.sql).fetchmany(request.limit + 1)
                else:
                    raise
            columns = [desc[0] for desc in analytics.description] if analytics.description else []
            truncated = len(result) > request.limit
            rows = result[:request.limit]

            # Post-flight: bill the dry-run estimate against the user's daily
            # quota. Do this AFTER execute so a downstream failure (e.g. BQ
            # outage) doesn't strand the user with charged-but-unrun bytes.
            # Stays inside the `with quota.acquire(...)` block so the slot
            # release happens after record_bytes completes.
            if dry_run_set:
                total_bq_bytes = sum(b for _, _, b in dry_run_set)
                try:
                    _build_quota_tracker().record_bytes(
                        user_id, total_bq_bytes,
                    )
                except Exception:
                    # record_bytes is documented as never-raising; defensive guard.
                    logger.warning("quota record_bytes failed for user=%s", user_id)
                # Charge the chat-session-scoped BQ scan budget when the
                # request carries a chat JWT (request.state.chat_session_id
                # set by app/auth/dependencies). Non-chat callers no-op.
                # Raises HTTPException(400, bq_budget_exhausted) when the
                # cumulative session bytes exceed ChatConfig.per_session_bq_scan_bytes.
                _maybe_charge_chat_session_bq_budget(http_request, total_bq_bytes)

        # Convert to serializable types
        serializable_rows = []
        for row in rows:
            serializable_rows.append([
                str(v) if v is not None and not isinstance(v, (int, float, bool, str)) else v
                for v in row
            ])
        # bytes_scanned from _dry_run_set (pinned to entry 0 after _bq_quota_and_cap_guard).
        # Computed before building the response so it can be surfaced to
        # REST/CLI/MCP consumers; ``None`` for local queries (no BQ tables).
        _bytes_scanned = sum(b for _, _, b in _dry_run_set) if _dry_run_set else None
        response = QueryResponse(
            columns=columns,
            rows=serializable_rows,
            row_count=len(serializable_rows),
            truncated=truncated,
            bytes_scanned=_bytes_scanned,
        )
        # Determine action: remote when BQ tables were involved (_dry_run_set non-empty),
        # local otherwise.
        _action = "query.remote" if _dry_run_set else "query.local"
        _first_table = _first_table_from_sql(request.sql)
        _resource = (f"table:{_first_table}" if _first_table else "adhoc")[:256]
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action=_action,
                resource=_resource,
                params={
                    "sql_preview": (request.sql or "")[:200],
                    # bytes_scanned / bytes_billed / bq_job_id: only available for
                    # BQ-remote path. bytes_billed and bq_job_id are not yet surfaced
                    # by the DuckDB BQ extension execute() path — deferred TODO.
                    # bytes_scanned comes from the dry-run estimate (close approximation).
                    "bytes_scanned": _bytes_scanned,
                    "bytes_billed": None,   # deferred — BQ extension doesn't expose per-execute billing
                    "bq_job_id": None,      # deferred — bigquery_query() path doesn't return a job id
                    "rows_returned": len(serializable_rows),
                    "duration_ms": int((time.monotonic() - _t0) * 1000),
                },
                result="success",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for %s; continuing", _action)
        return response
    except HTTPException as exc:
        _first_table = _first_table_from_sql(request.sql)
        _resource = (f"table:{_first_table}" if _first_table else "adhoc")[:256]
        _action_err = "query.remote" if _dry_run_set else "query.local"
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action=_action_err,
                resource=_resource,
                params={"sql_preview": (request.sql or "")[:200],
                        "error": str(exc.detail)[:200],
                        "duration_ms": int((time.monotonic() - _t0) * 1000)},
                result=f"error.{exc.status_code}",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for query (error path); continuing")
        raise
    except Exception as e:
        # If DuckDB raised "Table … does not exist" for a referenced name,
        # check whether that name belongs to a registry row in
        # `query_mode='materialized'` that hasn't yet been materialized in
        # this instance's analytics.duckdb. Materialized rows produce a
        # parquet at `${DATA_DIR}/extracts/<source>/data/<id>.parquet` but
        # the orchestrator is `_meta`-driven and only creates master views
        # for connectors that emit `_meta` rows — so on a fresh instance
        # (or before the first scheduler tick) the master view doesn't
        # exist yet and the operator gets a confusing "table does not
        # exist" with no path forward. Surface a materialize-aware hint
        # instead of DuckDB's bare error.
        msg = str(e)
        helpful = _materialized_hint_for_query_error(conn, request.sql, msg)
        _first_table = _first_table_from_sql(request.sql)
        _resource = (f"table:{_first_table}" if _first_table else "adhoc")[:256]
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="query.local",
                resource=_resource,
                params={"sql_preview": (request.sql or "")[:200],
                        "error": msg[:200],
                        "duration_ms": int((time.monotonic() - _t0) * 1000)},
                result="error.400",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for query (exception path); continuing")
        if helpful:
            raise HTTPException(status_code=400, detail=helpful)
        raise HTTPException(status_code=400, detail=f"Query error: {msg}")
    finally:
        analytics.close()


def _materialized_hint_for_query_error(
    conn: duckdb.DuckDBPyConnection, sql: str, error_msg: str,
) -> Optional[str]:
    """Return a materialize-aware error message if the failed query
    references a registry row whose `query_mode='materialized'` and which
    has no master view in analytics.duckdb yet, OR ``None`` to fall back
    to DuckDB's raw error.

    The detection scans each materialized row's id/name against the SQL
    text; a hit means the operator picked a name that exists in the
    registry but isn't queryable in this instance. The hint is the same
    in both arms of the OR — it tells them what the table needs and what
    they can do today (`agnes pull` or query `bq."dataset"."table"`
    directly using the bucket/source_table from the registry row).
    """
    # Cheap fast-path — only inspect the registry when DuckDB's error
    # actually mentions a missing table. Avoids registry round-trip on
    # every parse/cast/permission failure.
    el = error_msg.lower()
    if "does not exist" not in el and "table with name" not in el:
        return None
    try:
        repo = table_registry_repo()
        rows = repo.list_all()
    except Exception:
        # Registry read failed for whatever reason — don't compound the
        # error response by hiding the original DuckDB message.
        return None
    sql_l = sql.lower()
    for r in rows:
        if (r.get("query_mode") or "") != "materialized":
            continue
        # Match by id or by name; either could appear in the SQL.
        candidates = {r.get("id"), r.get("name")}
        for cand in candidates:
            if not cand:
                continue
            cand_l = str(cand).lower()
            # Word-boundary-ish check — `\b` doesn't match `.` so
            # `bq.dataset.cand` would still hit, which is fine for the
            # hint path (the operator is referring to the same table).
            if re.search(r"\b" + re.escape(cand_l) + r"\b", sql_l):
                return _build_materialized_hint(r)
    return None


def _build_materialized_hint(row: dict) -> str:
    """Format the user-facing hint for a materialized row that's not yet
    queryable. Includes the table id, the bucket/source_table when the
    row carries them, and concrete operator next steps."""
    tid = row.get("id") or row.get("name") or "<unknown>"
    bucket = row.get("bucket")
    source_table = row.get("source_table")
    direct_hint = ""
    if bucket and source_table:
        # BigQuery: `bq."dataset"."table"`; Keboola: `kbc."bucket"."table"`.
        # Pick the alias by source_type so the hint is copy-pasteable.
        alias = "bq" if (row.get("source_type") or "") == "bigquery" else "kbc"
        direct_hint = (
            f' or query the source directly via {alias}."{bucket}".'
            f'"{source_table}"'
        )
    return (
        f"Table {tid!r} is registered as query_mode='materialized' but is "
        f"not yet materialized in this instance's analytics views. Run "
        f"`agnes pull` (or wait for the scheduler tick / hit POST "
        f"/api/sync/trigger) to materialize the parquet"
        f"{direct_hint}."
    )


def _bq_guardrail_inputs(
    sql: str,
    sql_lower: str,
    sys_conn: duckdb.DuckDBPyConnection,
    user: dict,
    allowed: Optional[list],
):
    """Two-pass scan over user SQL for the upcoming BQ guardrail + RBAC patch.

    Returns a tuple `(dry_run_set, name_lookups, blocked_bq_path)`:

    - `dry_run_set` is a list of `(bucket, source_table, est_bytes)` triples
      identifying every BigQuery row the request will scan. The caller dry-runs
      the rewritten user SQL once and distributes the total here for quota
      bookkeeping.

    - `name_lookups` is a list of `(registered_name, bucket, source_table)`
      triples — only the bare-name matches from pass 1, NOT the direct
      `bq."<ds>"."<tbl>"` matches. Issue #171 fix: the cap-guard rewrites
      these name → ``\\`<project>.<bucket>.<source_table>\\``` when building
      the BQ-native SQL for dry-run, so partition pruning + column projection
      + predicate pushdown all engage.

    - `blocked_bq_path` is a structured-detail dict for the caller to raise
      HTTPException(403) with, when user SQL contains a direct
      `bq."<ds>"."<tbl>"` reference that either points at an unregistered
      path (`bq_path_not_registered`) or registered but the caller has no
      grant on the registered name (`bq_path_access_denied`). None when the
      RBAC check passes.
    """
    repo = table_registry_repo()

    # 1. Bare-name pass: look up registered remote-BQ names that appear in
    # the user SQL as word-boundary tokens. Reuses the same regex shape as
    # the existing forbidden-table loop above.
    #
    # `accessible_set` comes from `get_accessible_tables()` which returns
    # `resource_grants.resource_id` values — i.e. table registry IDs, NOT
    # display names. Devin Review iter #3 caught the mismatch: when
    # `id != name` (e.g. id="bq.finance.ue", name="ue"), legitimate
    # accessible rows were skipped, under-counting dry-run bytes for the
    # cost cap. The user SQL still references the display `name` (that's
    # what shows in `agnes catalog`), so the regex match below uses `name`,
    # but the access gate uses `id`.
    dry_run: list = []
    name_lookups: list = []
    seen_paths: set = set()
    accessible_set = set(allowed) if allowed is not None else None
    # Issue #201: mask backtick segments so a registered bare name like
    # `unit_economics` doesn't false-positive on a user-supplied full
    # backtick path `<project>.<dataset>.unit_economics`. The full-path
    # pass below registry-gates those properly.
    sql_lower_masked = _mask_backticks(sql_lower)
    for r in repo.list_by_source("bigquery"):
        if (r.get("query_mode") or "") != "remote":
            continue
        bucket = r.get("bucket")
        source_table = r.get("source_table")
        name = r.get("name")
        row_id = r.get("id")
        if not (bucket and source_table and name and row_id):
            continue
        if accessible_set is not None and row_id not in accessible_set:
            # Forbidden-table loop above will have rejected the request
            # before we get here. Defensive skip.
            continue
        pattern = r'\b' + re.escape(str(name).lower()) + r'\b'
        if re.search(pattern, sql_lower_masked):
            key = (bucket.lower(), source_table.lower())
            if key not in seen_paths:
                seen_paths.add(key)
                dry_run.append((bucket, source_table, 0))  # bytes filled at dry-run
            # Record the (name, bucket, source_table) mapping separately so the
            # cap-guard's SQL rewriter can find every occurrence — even if the
            # user references the same physical table under two registered
            # names (rare but possible: aliased catalog rows).
            name_lookups.append((str(name), bucket, source_table))

    # 2. Direct bq.<ds>.<tbl> pass: every match must point at a registered
    # row. Run BEFORE adding to dry_run so unregistered paths fail-fast.
    is_admin = is_user_admin(user.get("id") or user.get("email") or "", sys_conn)
    for m in BQ_PATH.finditer(sql):
        bucket_raw = m.group(1).strip('"')
        source_table_raw = m.group(2).strip('"')
        row = repo.find_by_bq_path(bucket_raw, source_table_raw)
        if row is None:
            return [], [], {
                "reason": "bq_path_not_registered",
                "path": f'bq."{bucket_raw}"."{source_table_raw}"',
                "hint": (
                    "Direct bq.* references must point to a registered table. "
                    "Register via `agnes admin register-table` or use the "
                    "registered name from `agnes catalog`."
                ),
            }
        # Row exists. Per-id grant check (non-admin only).
        # `accessible_set` is keyed by registry id (resource_grants
        # resource_id), so use `row["id"]` here, not display name.
        # Devin Review iter #3.
        if not is_admin:
            if accessible_set is None or row["id"] not in accessible_set:
                return [], [], {
                    "reason": "bq_path_access_denied",
                    "path": f'bq."{bucket_raw}"."{source_table_raw}"',
                    "registered_as": row["name"],
                }
        # Add to dry-run set if not already covered by bare-name pass.
        bucket = row["bucket"]
        source_table = row["source_table"]
        if bucket and source_table:
            key = (bucket.lower(), source_table.lower())
            if key not in seen_paths:
                seen_paths.add(key)
                dry_run.append((bucket, source_table, 0))

    # 3. Full backtick path `<project>.<dataset>.<table>` pass (issue #201).
    # Pre-#201 these bypassed Agnes RBAC entirely — only the configured
    # service account scope limited which tables a user could reach. Gate
    # them identically to the `bq.<ds>.<tbl>` pass: must match the
    # configured data project, must point at a registered row, and the
    # caller must hold a grant on that row's id (admin bypasses the grant
    # check but still requires registration + project match).
    #
    # Lazy `get_bq_access()` import via the module-level alias so tests
    # can monkeypatch a fake. When BQ isn't configured (no data project),
    # fall through silently — full backtick paths can't possibly resolve
    # against this instance, so leave them to BQ to reject if a query
    # somehow makes it through.
    try:
        bq = get_bq_access()
        data_project = (bq.projects.data or "").strip()
    except Exception:
        data_project = ""

    if data_project:
        for m in _BACKTICK_FULL_PATH.finditer(sql):
            proj, ds, tbl = m.group(1), m.group(2), m.group(3)
            if proj.lower() != data_project.lower():
                return [], [], {
                    "reason": "bq_path_cross_project",
                    "path": f"`{proj}.{ds}.{tbl}`",
                    "expected_project": data_project,
                    "hint": (
                        "--remote queries can only reference tables in the "
                        "configured BigQuery data project. Register "
                        "cross-project tables via `agnes admin "
                        "register-table` if needed."
                    ),
                }
            row = repo.find_by_bq_path(ds, tbl)
            if row is None:
                return [], [], {
                    "reason": "bq_path_not_registered",
                    "path": f"`{proj}.{ds}.{tbl}`",
                    "hint": (
                        "Direct BigQuery paths must point to a registered "
                        "table. Register via `agnes admin register-table` "
                        "or use the registered name from `agnes catalog`."
                    ),
                }
            if not is_admin:
                if accessible_set is None or row["id"] not in accessible_set:
                    return [], [], {
                        "reason": "bq_path_access_denied",
                        "path": f"`{proj}.{ds}.{tbl}`",
                        "registered_as": row["name"],
                    }
            bucket = row["bucket"]
            source_table = row["source_table"]
            if bucket and source_table:
                key = (bucket.lower(), source_table.lower())
                if key not in seen_paths:
                    seen_paths.add(key)
                    dry_run.append((bucket, source_table, 0))

    return dry_run, name_lookups, None


def _rewrite_bq_table_refs_to_native(
    sql: str, name_lookups: list, project: str,
) -> str:
    """Core identifier rewrite: DuckDB-flavor table references → BQ-native
    backtick form. Shared between dry-run and execution-path rewriters.

    Two transformations:

    1. Each registered remote-BQ name (word-boundary, case-insensitive)
       → ``\\`<project>.<bucket>.<source_table>\\````. A SINGLE re.sub call
       with an alternation regex sorted longest-first replaces every
       occurrence in one pass — important to avoid cross-contamination
       (Devin Review on query.py:464). The previous iterative approach
       (one re.sub per name, longest-first) corrupted output when the
       project ID contained a registered table name as a hyphen-delimited
       word: Pass 1 iter N's `\\bname\\b` regex would match INSIDE the
       backticked replacement text from a prior iter. Concrete repro:
       project = `my-ue-project`, registered names `orders` + `ue`, SQL
       `FROM orders JOIN ue` → after iter 1 (orders): the backticked path
       contains `my-ue-project`, then iter 2 (ue) matches the `ue` inside
       it. Single-pass alternation processes each source position exactly
       once, so the freshly-inserted backticked text isn't re-scanned.

    2. ``bq."<ds>"."<tbl>"`` (and the unquoted variant) → ``\\`<project>.<ds>.<tbl>\\````.
       Distinct pattern from Pass 1, no overlap, separate re.sub.

    The rewrite is regex-only (no SQL parser): a registered name appearing
    inside a string literal (e.g. an `IN (...)` value or a `LIKE` pattern)
    will also be rewritten. This is acceptable because (a) it's vanishingly
    rare to have a string literal exactly matching a registered table name,
    and (b) when it does happen the caller's error path covers the case
    (dry-run falls back to per-table SELECT * estimate; execution falls
    through to the ATTACH-catalog path).

    CTE shadowing: a `WITH unit_economics AS (...)` followed by `FROM
    unit_economics` would also rewrite the `FROM` reference. BQ then treats
    the CTE as unreferenced (legal) and the rewriter's caller deals with
    the consequence — over-estimation for dry-run, fall-through-to-ATTACH
    via BQ parse error for execution.
    """
    out = sql

    # Pass 1: bare-name rewrite. Build a single alternation regex sorted
    # longest-first, with a function-replacement that looks the matched
    # name up in a case-insensitive dict. Single-pass means freshly
    # inserted backticked text isn't re-scanned, fixing the
    # project-ID-contains-name corruption (Devin Review on query.py:464).
    #
    # Issue #201: split the SQL on `…` segments and rewrite ONLY in the
    # outside-backtick chunks. Without this, a user-supplied full backtick
    # path like ``\\`<project>.<dataset>.unit_economics\\``` whose final
    # segment matches a registered bare name would have the bare-name
    # regex fire INSIDE the backticks (since `\\b` treats both `.` and
    # `` ` `` as non-word boundaries), producing malformed nested
    # backticks. Splitting confines the rewrite to user identifier
    # positions where bare-name resolution is the intended behaviour.
    if name_lookups:
        # Map name (lower-cased) → backticked target. Names are
        # case-insensitive on the input side per the existing helper
        # contract (see test_rewrite_helper_is_case_insensitive_on_bare_names).
        name_to_target: dict[str, str] = {}
        for name, bucket, source_table in name_lookups:
            name_to_target[name.lower()] = f"`{project}.{bucket}.{source_table}`"

        # Alternation pattern, longest-first. Longer match wins at any
        # given position because Python's re tries alternatives
        # left-to-right and stops at the first match — pinning longest
        # entries to the front preserves the prefix-collision invariant
        # exercised by test_rewrite_helper_longer_name_wins_over_prefix.
        sorted_names = sorted(name_to_target.keys(), key=len, reverse=True)
        pattern = r"\b(" + "|".join(re.escape(n) for n in sorted_names) + r")\b"

        def _name_repl(m: re.Match) -> str:
            return name_to_target[m.group(1).lower()]

        # `re.split` with a captured group returns: [outside, backtick,
        # outside, backtick, …]. Even indices are outside-backtick chunks
        # eligible for bare-name rewrite; odd indices are full backtick
        # segments preserved verbatim.
        parts = re.split(r'(`[^`]*`)', out)
        for i, part in enumerate(parts):
            if i % 2 == 0:
                parts[i] = re.sub(pattern, _name_repl, part, flags=re.IGNORECASE)
        out = "".join(parts)

    # Pass 2: bq."ds"."tbl" / bq.ds.tbl → `<project>.<ds>.<tbl>`.
    def _bq_path_repl(m: re.Match) -> str:
        ds = m.group(1).strip('"')
        tbl = m.group(2).strip('"')
        return f"`{project}.{ds}.{tbl}`"

    out = BQ_PATH.sub(_bq_path_repl, out)
    return out


def _rewrite_user_sql_for_bq_dry_run(
    sql: str, name_lookups: list, project: str,
) -> str:
    """Rewrite user SQL from DuckDB-flavor to BQ-native so a single
    `_bq_dry_run_bytes` call can estimate scan size for the EXACT query
    the user submitted (issue #171). Thin wrapper around the shared
    core; kept as a stable name for callers in /api/query's cap-guard.
    """
    return _rewrite_bq_table_refs_to_native(sql, name_lookups, project)


def _rewrite_user_sql_for_bigquery_query(
    user_sql: str, conn: duckdb.DuckDBPyConnection,
) -> tuple[str, bool]:
    """Rewrite user SQL so the entire query ships to BQ as a single
    ``bigquery_query(<project>, <inner-sql>)`` call.

    Returns ``(rewritten_sql, did_rewrite)``. When ``did_rewrite`` is
    ``False``, the caller MUST execute the original ``user_sql`` via the
    ATTACH-catalog path (slow but correct); the rewriter is conservative
    on purpose — wrapping cross-source queries in ``bigquery_query()``
    would silently lose the local-side data.

    Why this matters
    ----------------
    The orchestrator's master view (``CREATE VIEW name AS SELECT * FROM
    bigquery.<bucket>.<source_table>``) does not push WHERE / projections
    into BQ when DuckDB resolves the query — the BQ extension opens a
    Storage Read API session over the entire table, which on multi-100M-row
    tables is 50-100× slower than letting BQ run the query server-side.
    Wrapping the user's SQL in ``bigquery_query('<project>', '<inner>')``
    makes the BQ extension issue a ``jobs.query`` instead, with full
    predicate pushdown.

    Skip rules (returns ``(user_sql, False)``)
    ------------------------------------------
    1. No registered ``query_mode='remote'`` BQ row referenced in the SQL.
       Nothing to rewrite — original SQL passes through unchanged.
    2. User SQL already contains ``bigquery_query(`` — never double-wrap.
       (The /api/query keyword denylist also blocks this in production;
       defensive guard for callers in other contexts.)
    3. SQL also references a non-BQ master view (Keboola/Jira local-mode
       table). Wrapping would lose those references — fall through to
       ATTACH-catalog so the cross-source query still runs.
    4. ``get_bq_access()`` returns the unconfigured sentinel
       (``data == ''``). No project to fill into ``bigquery_query()``.

    Edge cases preserved by design
    ------------------------------
    - CTEs / sub-queries referencing BQ tables: the table-name rewrite
      happens at every match position, then the whole SQL is wrapped in
      one ``bigquery_query()``. BQ supports CTEs, so this works.
    - Multiple BQ tables, same project: combined into ONE wrap (single
      jobs.query). DuckDB's BQ extension doesn't support multi-project
      JOINs in a single ``bigquery_query()`` call today; if/when the
      registry grows per-table source_project, this helper would need to
      gate on cross-project mixing.
    - ``bq."ds"."tbl"`` direct paths: rewritten to BQ-native backticks
      via the same shared core as dry-run.
    """
    # Skip 2: don't double-wrap. Cheap pre-check before any registry I/O.
    if "bigquery_query(" in user_sql.lower():
        return user_sql, False

    # Find all referenced BQ remote-mode rows (bare-name + direct bq.path).
    # Mirrors the non-RBAC parts of `_bq_guardrail_inputs`. Issue #201:
    # bare-name regex must run against a backtick-masked copy so a
    # registered name like ``orders`` doesn't false-positive when it
    # appears as the table segment of a user-supplied full backtick path
    # like ``\\`<project>.<dataset>.orders\\```. Without masking, the
    # cross-source check below would falsely conclude the SQL touches
    # both BQ-remote and local sources, dropping every backtick-path
    # query into the 50-100× slower ATTACH-catalog fallback. Devin
    # Review on PR #208.
    sql_lower = user_sql.lower()
    sql_lower_masked = _mask_backticks(sql_lower)
    name_lookups: list = []
    seen_paths: set = set()

    try:
        repo = table_registry_repo()
        bq_rows = repo.list_by_source("bigquery")
        all_rows = repo.list_all()
    except Exception:
        # Registry read failure — let the original SQL run through the
        # ATTACH-catalog path. The handler's generic error path will
        # surface anything user-visible.
        return user_sql, False

    # Multi-project guard (devil's-advocate R1 finding #5): the rewriter
    # assumes every BQ-remote table resolves under the single
    # `bq.projects.data` project. The current registry schema doesn't
    # store `source_project` per row, so `bucket` is the only place a
    # cross-project leak could hide. A bucket containing `.` (e.g.
    # `other_prj.dataset`) suggests the operator encoded a project
    # prefix into the bucket name — wrapping that under our single
    # project would silently target the wrong project. Conservative
    # skip: any BQ row whose bucket contains `.` aborts the rewrite,
    # falling through to the legacy ATTACH-catalog path which uses
    # whatever resolution the operator's _remote_attach configured.
    for r in bq_rows:
        if (r.get("query_mode") or "") != "remote":
            continue
        bucket = r.get("bucket")
        source_table = r.get("source_table")
        name = r.get("name")
        if not (bucket and source_table and name):
            continue
        if "." in str(bucket):
            # Project-qualified bucket — can't safely wrap under our
            # single-project assumption. Bail out completely so we don't
            # mix rewritten and non-rewritten BQ paths in one query.
            return user_sql, False
        pattern = r'\b' + re.escape(str(name).lower()) + r'\b'
        if re.search(pattern, sql_lower_masked):
            key = (bucket.lower(), source_table.lower())
            if key not in seen_paths:
                seen_paths.add(key)
            name_lookups.append((str(name), bucket, source_table))

    # Direct bq."ds"."tbl" references — pull the registered (bucket,
    # source_table) pair so the inner SQL receives a backticked BQ-native
    # path. Mismatched / unregistered paths are caught upstream by the
    # guardrail; here we just collect the mappings the rewriter needs.
    direct_paths: set[tuple[str, str]] = set()
    for m in BQ_PATH.finditer(user_sql):
        bucket_raw = m.group(1).strip('"')
        source_table_raw = m.group(2).strip('"')
        direct_paths.add((bucket_raw, source_table_raw))

    # Issue #363: full backtick BQ paths (`<project>.<dataset>.<table>`) are
    # already BQ-native syntax — DuckDB can't parse backtick quoting locally.
    # When the user SQL uses only backtick paths (no bare names, no bq.ds.tbl),
    # both name_lookups and direct_paths stay empty and Skip 1 fires, sending
    # the backtick SQL to analytics.execute() → "syntax error at or near `"`.
    # Detect them here so the SQL still gets wrapped in bigquery_query().
    # _rewrite_bq_table_refs_to_native already preserves backtick segments
    # verbatim (backtick-split pass 1), so no additional rewrite is needed.
    has_backtick_paths = bool(_BACKTICK_FULL_PATH.search(user_sql))

    if not name_lookups and not direct_paths and not has_backtick_paths:
        # Skip 1: no BQ tables referenced.
        return user_sql, False

    # Skip 3: cross-source query (BQ + local-mode). If user SQL also
    # references a non-BQ master view, we can't push the whole thing to
    # BQ — DuckDB needs to do the join.
    bq_names_lc = {n.lower() for n, _, _ in name_lookups}
    for r in all_rows:
        st = (r.get("source_type") or "").lower()
        qm = (r.get("query_mode") or "").lower()
        if st == "bigquery" and qm == "remote":
            continue  # already handled
        name = r.get("name")
        if not name:
            continue
        name_lc = str(name).lower()
        if name_lc in bq_names_lc:
            # Same name registered both BQ-remote and local? Pathological;
            # skip as a safety measure.
            return user_sql, False
        if re.search(r'\b' + re.escape(name_lc) + r'\b', sql_lower_masked):
            logger.info(
                "rewrite_skip_cross_source: user SQL references both "
                "BQ-remote and local-mode tables; falling back to "
                "ATTACH-catalog path",
            )
            return user_sql, False

    # Skip 4: BQ project not configured.
    try:
        bq = get_bq_access()
        data_project = bq.projects.data
        # The first arg to `bigquery_query()` is the **execution / billing**
        # project — the project under which the BQ job runs and is billed.
        # In cross-project deployments the SA may only have
        # `serviceusage.services.use` on the billing project, so passing
        # the data project there returns 403 USER_PROJECT_DENIED. Match
        # the convention used everywhere else in the codebase (v2_scan /
        # v2_sample / v2_schema / extractor): backtick paths use the
        # **data** project, `bigquery_query()` first-arg uses the
        # **billing** project. For single-project deploys the two are
        # identical so the fix is a no-op there.
        billing_project = bq.projects.billing or data_project
    except Exception:
        return user_sql, False
    if not data_project:
        return user_sql, False

    # Rewrite identifiers using the DATA project — backtick paths
    # `<data-project>.<dataset>.<table>` resolve to the same logical
    # source no matter which project bills the query.
    inner_sql = _rewrite_bq_table_refs_to_native(user_sql, name_lookups, data_project)

    # Embed the inner SQL using DuckDB's dollar-quoted string literal form
    # (`$tag$ ... $tag$`). Naive `replace("'", "''")` doubling misses
    # backslash-escape sequences DuckDB's lexer recognises (`\\`, `\n`,
    # `\t`, …) — a predicate like `WHERE name = 'O\'Brien'` is unsafe
    # under doubling. Dollar-quoting takes the inner SQL verbatim with no
    # escape sequences whatsoever, so the user's exact bytes reach BQ.
    # Tag is a fixed conventional value; the absurdly unlikely collision
    # (user SQL containing the literal `$bqq_inner$`) falls back to the
    # legacy doubling path so the rewrite still proceeds — over-doubled
    # quotes are at worst a parse error caught by the handler's fallback
    # at the call site, not a silent bad result.
    DOLLAR_TAG = "$bqq_inner$"
    if DOLLAR_TAG in inner_sql:
        escaped_inner = inner_sql.replace("'", "''")
        rewritten = (
            f"SELECT * FROM bigquery_query('{billing_project}', '{escaped_inner}')"
        )
    else:
        rewritten = (
            f"SELECT * FROM bigquery_query('{billing_project}', "
            f"{DOLLAR_TAG}{inner_sql}{DOLLAR_TAG})"
        )
    return rewritten, True


def _view_targets_in(dry_run_set: list) -> list[str]:
    """Return registry IDs from ``dry_run_set`` whose ``bq_metadata_cache``
    row classifies them as ``VIEW`` or ``MATERIALIZED VIEW``.

    Used to enrich the ``remote_scan_too_large`` error message: when the
    target is a view, BigQuery does NOT push ``LIMIT`` into the view body,
    so a `SELECT * FROM <view> LIMIT 1` still scans the full underlying
    tables. Telling the analyst that explicitly saves them from retrying
    with the same query expecting different results.

    Best-effort: any lookup failure returns ``[]`` so the original error
    message still ships. The catalog is the source of truth for entity_type;
    if the bq_metadata_cache hasn't been refreshed yet for a table, that
    table is silently skipped (we just won't add the VIEW hint for it).
    """
    if not dry_run_set:
        return []
    try:
        # Route through the repo factory (backend-agnostic): a raw JOIN on the
        # always-DuckDB system connection comes back empty on a Postgres
        # instance, so bq_metadata_cache / table_registry would be invisible and
        # the VIEW hint would silently never fire.
        from src.repositories import bq_metadata_cache_repo, table_registry_repo

        wanted = {(b, t) for b, t, _ in dry_run_set}
        target_ids = {
            r["id"]
            for r in table_registry_repo().list_all()
            if (r.get("bucket"), r.get("source_table")) in wanted
        }
        if not target_ids:
            return []
        view_types = {"VIEW", "MATERIALIZED VIEW"}
        return [
            r["table_id"]
            for r in bq_metadata_cache_repo().list_all()
            if r.get("table_id") in target_ids and r.get("entity_type") in view_types
        ]
    except Exception:
        return []


@contextlib.contextmanager
def _bq_quota_and_cap_guard(
    *,
    user_id: str,
    user: dict | None = None,
    dry_run_set: list,
    name_lookups: list,
    sql: str,
):
    """Pre-flight check + dry-run + cap enforcement for /api/query BQ paths.

    Context-manager shape (Devin Review #5 on PR #168). Earlier implementation
    ran the dry-run + cap check inside `with quota.acquire(user_id):`, then
    returned — releasing the concurrent slot BEFORE the actual BQ-touching
    `analytics.execute(...)` ran. Spec §4.3.3 wants execute to be inside the
    slot so the per-user concurrent cap actually limits BQ scans, not just
    dry-runs.

    Now: the helper is a context manager that yields after the cap check.
    The caller's `with` block holds the slot through both dry-run AND the
    subsequent `analytics.execute(...)` until the body exits.

    Issue #171 fix: dry-run runs ONCE on the user's actual SQL (translated
    to BQ-native via `_rewrite_user_sql_for_bq_dry_run`). Pre-fix the
    pre-check did N dry-runs of synthetic ``SELECT * FROM <table>`` per
    referenced table — which ignored WHERE filters, column projection, and
    partition pruning, over-estimating scan size up to ~30,000× on
    partitioned/clustered tables and rejecting narrow queries that BQ
    itself would dry-run as a few MB.

    Issue #201 fix: when BQ rejects the rewritten SQL with a parse-level
    ``bq_bad_request`` (e.g. DuckDB-only syntax like ``::INT`` casts, or
    a rewriter bug that broke valid BQ-native input), retry with the
    user's ORIGINAL SQL — BQ-native input dry-runs cleanly. If the
    original ALSO fails, return a structured `remote_estimate_failed`
    HTTP 400 instead of the pre-#201 synthetic ``SELECT *`` per-table
    over-estimate. The synthetic fallback threw away user filters and
    routinely ballooned to "full table size", blocking legitimate narrow
    queries via `remote_scan_too_large`. Forbidden / upstream errors
    still propagate as HTTP 502.

    On retry-failure the surfaced `underlying` is the FIRST exception's
    message (the rewritten-SQL diagnostic) — not the second's. For the
    common case where the user references a catalog id (no qualifying
    dataset in their SQL), the second attempt is guaranteed to fail
    with the unhelpful ``Table "<id>" must be qualified with a dataset``,
    masking the actually-useful ``Unrecognized name: <column>`` /
    ``Syntax error`` diagnostic from the rewritten attempt. The
    second-attempt message is preserved as `underlying_original` for
    operator visibility.

    `user` (the full dict, distinct from `user_id`) is threaded through only
    to label the dry-run BQ jobs via `job_labels_for(user, "query")` — the
    quota/cap logic itself keys exclusively on `user_id`.

    Flow:
    1. `check_daily_budget` — over-cap users get 429 BEFORE any BQ work.
    2. `quota.acquire(user_id)` opened — concurrent-slot held throughout.
    3. Single dry-run of rewritten user SQL → `total_bytes`.
       On parse error, retry with the user's original SQL.
       On second parse error, raise 400 `remote_estimate_failed`.
    4. If total > cap → 400 `remote_scan_too_large`.
    5. Yield. Caller runs `analytics.execute(...)` + `record_bytes(...)`.
    6. On exit, slot released.

    Mutates `dry_run_set` in place: the third tuple element (bytes) is
    populated so the caller can sum and record bytes against the user's
    quota post-flight. Pin `total_bytes` on entry 0 and zero on the rest
    — BQ doesn't expose per-table bytes for a composite query — so
    `sum(b for _, _, b in dry_run_set)` still equals `total_bytes`.
    """
    quota = _build_quota_tracker()
    try:
        quota.check_daily_budget(user_id)
    except QuotaExceededError as exc:
        raise HTTPException(status_code=429, detail={
            "reason": "daily_byte_cap_exceeded",
            "kind": exc.kind,
            "current": exc.current,
            "limit": exc.limit,
            "retry_after_seconds": exc.retry_after_seconds,
        })

    try:
        bq = get_bq_access()
    except BqAccessError as exc:
        raise HTTPException(status_code=502, detail={
            "kind": exc.kind,
            "message": exc.message,
            **(exc.details or {}),
        })

    cap_bytes = _default_remote_query_cap_bytes()

    # `quota.acquire(user_id)` raises QuotaExceededError(KIND_CONCURRENT)
    # via __enter__ when the per-user concurrent-scan slot is at cap.
    # Catch around the `with` and map to HTTP 429 with the typed detail
    # shape — same shape as the daily-budget rejection above. Without
    # this, the exception propagates through @contextlib.contextmanager
    # and is caught by execute_query's generic `except Exception` →
    # returns HTTP 400 with a flattened "Query error: concurrent_scans:
    # N/M" string, dropping the typed retry_after_seconds field.
    # Devin Review #2 on PR #168.
    try:
        with quota.acquire(user_id):
            project = bq.projects.data
            rewritten_sql = _rewrite_user_sql_for_bq_dry_run(
                sql, name_lookups, project,
            )

            # Try the single-dry-run path first (issue #171). On BQ parse
            # errors (`bq_bad_request` — typically DuckDB-only syntax the
            # rewriter couldn't translate, OR — pre-#201 fix — a
            # rewriter-corrupted backtick path) retry the user's ORIGINAL
            # SQL: when the user submitted BQ-native SQL, the rewriter is
            # the only thing standing between them and a clean dry-run.
            # If the original ALSO fails, this is true DuckDB-only syntax
            # that BQ cannot estimate — fail fast with a structured
            # `remote_estimate_failed` instead of the pre-#201 synthetic
            # `SELECT *` over-estimate (which threw away user filters and
            # often ballooned to "full table size", blocking legitimate
            # narrow queries via `remote_scan_too_large`).
            #
            # All other BQ errors (forbidden, upstream) propagate as 502.
            total_bytes = 0
            try:
                total_bytes = _bq_dry_run_bytes(bq, rewritten_sql, user=user, agent_name="query")
            except BqAccessError as exc:
                if exc.kind != "bq_bad_request":
                    raise HTTPException(status_code=502, detail={
                        "kind": exc.kind,
                        "message": exc.message,
                        **(exc.details or {}),
                    })
                logger.warning(
                    "BQ dry-run rejected the rewritten SQL "
                    "(kind=%s, message=%s). Retrying with the user's "
                    "original SQL.",
                    exc.kind, exc.message,
                )
                try:
                    total_bytes = _bq_dry_run_bytes(bq, sql, user=user, agent_name="query")
                except BqAccessError as exc2:
                    if exc2.kind != "bq_bad_request":
                        raise HTTPException(status_code=502, detail={
                            "kind": exc2.kind,
                            "message": exc2.message,
                            **(exc2.details or {}),
                        })
                    raise HTTPException(status_code=400, detail={
                        "kind": "remote_estimate_failed",
                        "message": (
                            "BigQuery rejected this query during cost "
                            "estimation."
                        ),
                        # Branch the hint on the actual BQ error class —
                        # syntax errors (e.g. reserved-keyword aliases like
                        # `AS rows`) deserve a different pointer than
                        # column-not-found, which deserves a different one
                        # than table-not-found. Pre-#NNN this was a single
                        # hardcoded "column referenced doesn't exist" hint
                        # that misled analysts whenever BQ actually rejected
                        # on syntax. The first attempt's diagnostic
                        # (rewritten SQL — has the real BQ position info)
                        # is the more informative one to dispatch on.
                        "hint": _hint_for_bq_bad_request(exc.message),
                        # Surface the FIRST attempt's diagnostic (rewritten
                        # SQL — has the real "Unrecognized name" / syntax
                        # info). Second attempt for catalog-id-only SQL
                        # always fails with the unhelpful "must be
                        # qualified" message, so we keep it as
                        # `underlying_original` for operator context but
                        # don't lead with it.
                        "underlying": exc.message,
                        "underlying_original": exc2.message,
                    })

            # Distribute the total to dry_run_set so the caller's
            # `record_bytes(sum(...))` stays correct. Per-table breakdown
            # is unavailable from a composite dry-run; pin total to entry
            # 0, zero the rest. (Same accounting symmetry whether the
            # bytes came from the rewritten SQL or the original-SQL
            # retry.)
            if dry_run_set:
                b0, t0, _ = dry_run_set[0]
                dry_run_set[0] = (b0, t0, total_bytes)
                for i in range(1, len(dry_run_set)):
                    bi, ti, _ = dry_run_set[i]
                    dry_run_set[i] = (bi, ti, 0)

            if cap_bytes > 0 and total_bytes > cap_bytes:
                tables = [f"{b}.{t}" for b, t, _ in dry_run_set]
                view_targets = _view_targets_in(dry_run_set)
                if view_targets:
                    suggestion = (
                        f"Target(s) {', '.join(view_targets)} are VIEW or "
                        "MATERIALIZED VIEW. BigQuery does not push `LIMIT` "
                        "into the view body — `SELECT * FROM <view> LIMIT 1` "
                        "still runs the full underlying scan. Use "
                        "`agnes snapshot create <id> --select <cols> --where "
                        "<predicate>` to bound the scan, then query the "
                        "snapshot locally."
                    )
                else:
                    suggestion = (
                        "Use `agnes snapshot create <id> --select <cols> "
                        "--where <predicate> --estimate` to materialize a "
                        "filtered subset, then query the snapshot locally."
                    )
                raise HTTPException(status_code=400, detail={
                    "reason": "remote_scan_too_large",
                    "scan_bytes": total_bytes,
                    "limit_bytes": cap_bytes,
                    "tables": tables,
                    "view_targets": view_targets,
                    "suggestion": suggestion,
                })

            # Yield control to the handler — slot stays acquired while the
            # caller runs analytics.execute() + record_bytes().
            yield total_bytes
    except QuotaExceededError as exc:
        # Only KIND_CONCURRENT can land here (daily-budget already mapped
        # above; record_bytes never raises). Map to 429 with structured
        # detail consistent with the daily-budget shape.
        raise HTTPException(status_code=429, detail={
            "reason": "concurrent_slot_exceeded",
            "kind": exc.kind,
            "current": exc.current,
            "limit": exc.limit,
            "retry_after_seconds": exc.retry_after_seconds,
        })


def run_remote_select_to_arrow(conn, user, sql, bq, quota):
    """Materialize a raw SELECT against BigQuery into an Arrow table (#616).

    Backs the snapshot ``from_query`` mode used by ``agnes query --remote
    --auto-snapshot``: the analyst has explicitly opted into materializing a
    snapshot, so this path reuses the SAME validation as ``/api/query`` —
    SELECT-only guard, per-user view RBAC, BQ registry-gating, and the
    ``bigquery_query()`` rewrite for predicate pushdown — but deliberately
    does NOT apply the ``remote_scan_too_large`` cap (the cap is exactly what
    the analyst is bypassing). Daily-byte budget and concurrent-slot quotas
    still apply via ``quota``.

    Returns a ``pyarrow.Table`` of the FULL result. Raises:
        HTTPException — on RBAC / registry / SELECT-only rejection (same
            shapes as /api/query), surfaced by the v2_scan endpoint.
        QuotaExceededError — daily-budget / concurrent-slot exhaustion.
    """
    sql_lower = (sql or "").strip().lower()
    _assert_select_only(sql_lower)

    # Internal-source SQL (agnes_sessions/usage/audit) isn't a BQ snapshot
    # source — refuse rather than silently mis-route.
    if find_internal_refs(sql):
        raise HTTPException(
            status_code=400,
            detail="Internal tables cannot be snapshotted via --from-query.",
        )

    allowed = get_accessible_tables(user, conn)
    analytics = get_analytics_db_readonly()
    try:
        if allowed is not None:  # None = admin, sees all
            all_views = {row[0] for row in analytics.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
            ).fetchall()}
            allowed_ids = set(allowed)
            registry_rows = table_registry_repo().list_all()
            allowed_view_names = {
                r["name"] for r in registry_rows
                if r.get("name") and r.get("id") in allowed_ids
            }
            sql_lower_masked = _mask_backticks(sql_lower)
            for table in all_views - allowed_view_names:
                pattern = r'\b' + re.escape(table.lower()) + r'\b'
                if re.search(pattern, sql_lower_masked):
                    from src.rbac import table_not_in_stack_message
                    raise HTTPException(
                        status_code=403, detail=table_not_in_stack_message(table),
                    )

        dry_run_set, name_lookups, blocked_bq_path = _bq_guardrail_inputs(
            sql, sql_lower, conn, user, allowed,
        )
        if blocked_bq_path is not None:
            raise HTTPException(status_code=403, detail=blocked_bq_path)

        user_id = user.get("email") or user.get("id") or "anon"
        quota.check_daily_budget(user=user_id)
        with quota.acquire(user=user_id):
            # Dry-run the rewritten SQL purely to bill the user's daily byte
            # quota — NO cap enforcement here (that's the whole point of the
            # opt-in). Fail closed: without an estimate we cannot bill the
            # daily byte budget, and proceeding at 0 cost would let an analyst
            # bypass the budget via --auto-snapshot during a BQ dry-run outage.
            # Mirror /api/query's `remote_estimate_failed` behavior.
            total_bq_bytes = 0
            if dry_run_set:
                try:
                    project = bq.projects.data
                    rewritten = _rewrite_user_sql_for_bq_dry_run(
                        sql, name_lookups, project,
                    )
                    total_bq_bytes = _bq_dry_run_bytes(bq, rewritten, user=user, agent_name="query")
                except HTTPException:
                    raise
                except Exception as exc:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "reason": "remote_estimate_failed",
                            "message": (
                                "BigQuery dry-run estimate failed; the scan "
                                "cannot be billed against your daily budget, "
                                "so the materialize is refused. Retry shortly."
                            ),
                        },
                    ) from exc

            execution_sql, did_rewrite = _rewrite_user_sql_for_bigquery_query(
                sql, conn,
            )
            try:
                try:
                    table = analytics.execute(execution_sql).arrow()
                except Exception as exc:
                    if did_rewrite and _looks_like_bq_rewrite_parse_error(exc):
                        table = analytics.execute(sql).arrow()
                    else:
                        raise
            except HTTPException:
                # Don't re-wrap structured rejections raised below (RBAC,
                # SELECT-only, registry) — let them propagate.
                raise
            except Exception as exc:
                # Map DuckDB execution errors (syntax error, missing table,
                # type mismatch) to a structured 400 mirroring the normal
                # /api/query path (`app/api/query.py` ~line 714) so the
                # scan_endpoint caller surfaces a user-friendly error
                # instead of a raw 500. Devin Review ANALYSIS_0003 on #620.
                raise HTTPException(
                    status_code=400,
                    detail={
                        "reason": "duckdb_execution_error",
                        "message": str(exc),
                    },
                ) from exc

            if dry_run_set and total_bq_bytes:
                try:
                    quota.record_bytes(user=user_id, n=total_bq_bytes)
                except Exception:
                    logger.warning("quota record_bytes failed for user=%s", user_id)
        return table
    finally:
        analytics.close()
