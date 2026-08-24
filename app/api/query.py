"""Query endpoint — execute SQL against server DuckDB."""

import contextlib
import dataclasses
import functools
import json
import logging
import os
import re
import threading
import time
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

# Imported at module level so tests can monkeypatch via
# `app.api.query._bq_dry_run_bytes` without resolving lazy imports inside
# the handler (reaches the patched attribute on each call). Same for
# get_bq_access — sibling module, dep direction doesn't matter (both are
# leaves under app.api).
from app.api.v2_quota import QuotaExceededError, _build_quota_tracker
from app.api.v2_scan import _bq_dry_run_bytes
from app.auth.access import is_user_admin
from app.auth.dependencies import _get_db, get_current_user
from app.auth.session_principal import PRINCIPAL_TYPES
from app.instance_config import get_value
from connectors.bigquery.access import (
    BqAccessError,
    bq_query_parameters_from_policy_params,
    get_bq_access,
    run_bq_query_to_arrow,
)
from connectors.bigquery.labels import job_labels_for
from connectors.internal.access import (
    InternalAccessError,
    execute_internal_query,
    find_internal_refs,
    is_internal_table,
)
from src.access_policy import (
    PolicyError,
    PolicyIdentityUnresolvable,
    PolicyNameCollision,
    assert_policied_reads_unique,
    assert_unique_output_columns,
    policied_relation,
    rewrite_sql,
    row_scope_payload,
)
from src.audit_helpers import client_kind_from_user
from src.db import _open_duckdb, get_analytics_db_readonly
from src.rbac import get_accessible_tables
from src.remote_engines import (
    SQL_RESERVED_NAMES,
    TABLE_REF_PREFIX_RE,
    mask_backticks,
    name_reference_re,
    qualified_path_re,
    rewrite_bare_names,
    strip_one_trailing_semicolon,
)
from src.remote_query import _strip_leading_sql_comments
from src.repositories import (
    audit_repo,
    table_registry_repo,
)
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/query", tags=["query"])

# Orchestrator-internal base tables inside each source's ATTACHed
# extract.duckdb catalog. Never part of an analyst's query surface (analysts
# hit the RBAC-filtered master views); a non-admin referencing them leaks
# cross-source schemas + remote-source URLs / token_env names (audit M1).
_INTERNAL_EXTRACT_TABLES = ("_meta", "_remote_attach", "_remote_links")

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
    limit_bytes = cfg.per_session_bq_scan_bytes if cfg is not None else _DEFAULT_PER_SESSION_BQ_BYTES
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
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "bq_budget_exhausted",
                "session_id": session_id,
                "scan_bytes_cumulative": new_total,
                "limit_bytes": limit_bytes,
                "suggestion": (
                    "Per-session BigQuery scan budget exhausted. Start a new chat session to reset the quota."
                ),
            },
        )


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


# Cap on SQL text written to the application log. Matches the 200-char
# `sql_preview` the audit-log writer already uses, so a query body has one
# consistent exposure limit across both sinks.
_SQL_LOG_PREVIEW_CHARS = 200


def _bq_error_offset(message: str, sql: str) -> int | None:
    """Absolute offset into ``sql`` for a BigQuery ``at [line:col]`` suffix.

    BigQuery reports where it stopped parsing, and for the ROLLUP rejection this
    module logs it is well past any sane preview cap — the motivating message
    ends ``other grouping elements at [1:657]``. Returns ``None`` when the
    message carries no position or the position does not exist in ``sql``, so
    callers fall back to a head preview rather than to a wrong window.

    Both coordinates are 1-based.
    """
    m = re.search(r"\[(\d+):(\d+)\]", message or "")
    if not m:
        return None
    line, col = int(m.group(1)), int(m.group(2))
    raw = sql or ""
    if line < 1 or col < 1:
        return None
    # Walk the RAW string counting only `\n`, rather than `splitlines()` plus
    # one character per break. `splitlines()` gets this wrong twice: it drops
    # the `\r` of a CRLF, so the reconstructed offset under-counts by one per
    # preceding line and the window drifts off the clause BigQuery named; and
    # it also breaks on separators BigQuery does not treat as line ends
    # (`\v`, `\f`, `\x1c`, ` `, …), which would mis-split a query that
    # merely contains one. Real indices into the string the engine parsed are
    # the only thing `around` can be applied to (Devin Review on #1188).
    start = 0
    for _ in range(line - 1):
        nl = raw.find("\n", start)
        if nl == -1:
            return None
        start = nl + 1
    offset = start + (col - 1)
    return offset if offset <= len(raw) else None


def _sql_log_preview(sql: str, *, around: int | None = None) -> str:
    """Truncate SQL for logging. Query literals can carry sensitive values,
    so the log must never become the one place a full query body lands
    unbounded.

    ``around`` centers the window on a character offset the engine complained
    about instead of taking it from the head. Without it, logging a rejected
    query is self-defeating for exactly the case it was added for: BigQuery
    rejected at character 657 and the head-200 preview stops long before the
    clause the operator needs to see. The cap itself is unchanged, so the
    sensitive-value bound this function exists for still holds — the window
    moves, it does not grow.

    Whitespace is collapsed AFTER windowing, never before: ``around`` indexes
    the raw SQL the engine parsed, and collapsing first would shift every offset
    past the first run of whitespace.
    """
    raw = sql or ""
    text = " ".join(raw.split())
    if len(text) <= _SQL_LOG_PREVIEW_CHARS:
        return text
    if around is None:
        return text[:_SQL_LOG_PREVIEW_CHARS] + "... [truncated]"

    half = _SQL_LOG_PREVIEW_CHARS // 2
    start = max(0, min(around - half, max(0, len(raw) - _SQL_LOG_PREVIEW_CHARS)))
    window = " ".join(raw[start : start + _SQL_LOG_PREVIEW_CHARS].split())
    lead = "[truncated] ..." if start > 0 else ""
    tail = "... [truncated]" if start + _SQL_LOG_PREVIEW_CHARS < len(raw) else ""
    return f"{lead}{window}{tail}"


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
    if "only supports rollup" in msg:
        # DuckDB accepts `GROUP BY a, ROLLUP(b)`; BigQuery requires ROLLUP
        # to be the sole grouping element. Same dialect-divergence class as
        # `::INT` casts, but far more confusing: the query is well-formed
        # DuckDB and runs fine via the ATTACH-catalog path, so the analyst
        # has no reason to suspect their GROUP BY. Only the dry-run (which
        # must reach BQ to price the scan) rejects it.
        #
        # Deliberately does NOT suggest `GROUP BY ROLLUP(a, b)`: that is a
        # DIFFERENT query (it adds a grand-total row). GROUPING SETS is the
        # faithful translation.
        return (
            "BigQuery requires ROLLUP to be the only element in a GROUP BY, "
            "and this query combines it with other grouping elements. DuckDB "
            "allows that mix, so SQL that runs locally is rejected here. "
            "Rewrite the GROUP BY as an explicit GROUPING SETS list: "
            "GROUP BY country, ROLLUP(brand) becomes GROUP BY GROUPING SETS "
            "((country, brand), (country)). Note that GROUP BY "
            "ROLLUP(country, brand) is NOT the same query: it adds a "
            "grand-total row."
        )
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

# Snowflake direct path guard. Snowflake is a DuckDB community extension,
# so the query runs locally, but a qualified `sf."schema"."table"` bypasses
# the master-view RBAC layer and must be registry-gated like `bq.*`.
SF_PATH = qualified_path_re("sf")


# Issue #201 — full backtick BQ path `<project>.<dataset>.<table>` in user
# SQL. Used by the registry-gating pass and (via `_mask_backticks`) to keep
# bare-name regexes from firing inside backtick-quoted segments.
_BACKTICK_FULL_PATH = re.compile(r"`([^.`]+)\.([^.`]+)\.([^.`]+)`")

# `_mask_backticks` / `_name_reference_re` now live in `src/remote_engines.py`
# so every engine's registry gate asks the same question with the same regex.
# Aliased rather than renamed at ~40 call sites: the local names carry a lot of
# reviewed history in this module's comments.
_mask_backticks = mask_backticks


def _local_extract_catalogs(conn) -> set[str]:
    """Attached local extract catalogs (per-source ``extract.duckdb`` files).

    Each source is ATTACHed as its own catalog named after the source
    (``src/db.py``). These are file-backed ``duckdb`` attachments, so the
    default catalog (where the analyst-facing master views live) and the
    remote-extension catalogs (type ``bigquery``/``keboola``) fall outside this
    set.

    That exclusion is only safe for a prefix that has a gate of its own, and
    the two are not equal. ``bq`` does (``_bq_guardrail_inputs``), as do ``sf``
    (``_sf_guardrail_inputs``) and ``dbx``
    (``connectors.databricks.remote.guardrail_inputs``). **``kbc`` does not** —
    ``_bq_guardrail_inputs`` scans ``BQ_PATH`` only, Keboola is not registered
    in ``src.remote_engines._ENGINES``, and no ``_kbc_guardrail_inputs``
    exists. An earlier version of this docstring asserted the opposite. So on
    an instance whose Keboola extract wrote a ``_remote_attach`` row (every
    Keboola sync does — ``connectors/keboola/extractor.py``), a
    ``kbc."bucket"."table"`` path is gated by neither this catalog check nor a
    registry/grant/policy one. Pre-existing and tracked separately from the
    engine-path policy gates; recorded here so the next reader does not infer
    coverage from the exclusion.
    """
    try:
        default = conn.execute("SELECT current_database()").fetchone()[0]
        rows = conn.execute("SELECT database_name, type FROM duckdb_databases()").fetchall()
    except Exception:
        # If catalog enumeration fails we can't run the #868 catalog gate, so
        # the request rides on the view-name + internal-table denylists below.
        # Don't 500 over it.
        #
        # Those denylists are a weaker backstop than this comment used to
        # imply, and weaker again since the guards started matching parsed
        # table references: a qualified path whose last segment is itself
        # quoted and dotted (`src.main."bucket.orders"`) yields the whole
        # `bucket.orders` as the table name, so a denied view called `orders`
        # no longer matches it. The old text scan caught that by coincidence —
        # it matched the substring anywhere — not by design. Reaching a source
        # catalog by qualified path is this gate's job; when it cannot run,
        # accept that it is not covered rather than assume layer (b) stands in.
        logger.warning("RBAC catalog gate: could not enumerate attached catalogs", exc_info=True)
        return set()
    reserved = {default, "system", "temp", "memory"}
    return {name for (name, typ) in rows if typ == "duckdb" and name not in reserved}


def _assert_no_ungranted_catalog_ref(sql_lower_masked: str, conn) -> None:
    """403 if user SQL references a local extract catalog by qualified path.

    Non-admins reach their granted data through the *unqualified* master views
    in the default catalog; a catalog-qualified reference like
    ``<source>.main."<name>"`` bypasses the master-view-name denylist and reads
    an un-granted source's rows directly (audit #868). The legitimate analyst
    surface never needs to name a source catalog, so any such reference is
    denied.
    """
    for cat in _local_extract_catalogs(conn):
        # `src.` or `"src".` (optionally quoted) immediately followed by a `.`
        # qualifier. The `(?<![\w."])` lookbehind avoids matching mid-identifier
        # or inside a longer already-qualified path segment.
        if re.search(r'(?<![\w."])"?' + re.escape(cat.lower()) + r'"?\s*\.', sql_lower_masked):
            raise HTTPException(
                status_code=403,
                detail=(
                    "query references an un-granted source catalog directly; "
                    "use the granted table name (unqualified) instead"
                ),
            )


def _identity_for_audit(user) -> tuple:
    """``(user_id, email)`` for audit-log rows / BQ-quota-key bookkeeping
    only — NEVER for an authorization decision (see the admin-check note in
    ``_bq_guardrail_inputs``, which deliberately does not use this helper).

    A restricted principal (co-session / agent-session, V1d) is a frozen
    dataclass with no ``.get`` and no single caller identity of its own: an
    ``AgentPrincipal`` reports its owner (the request legitimately runs on
    the owner's behalf, just intersection-narrowed — same call
    ``app.marketplace_server.packager._diag_identity`` makes for
    ``/marketplace/info``); a ``SessionPrincipal`` reports neither, since a
    co-session has several live participants and naming one would
    misattribute the others' actions.
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        return getattr(user, "owner_user_id", None), getattr(user, "owner_email", None)
    return user.get("id"), user.get("email")


# Parse-only DuckDB connection (see _parse_connection).
_PARSE_CONN: "duckdb.DuckDBPyConnection | None" = None
_PARSE_CONN_LOCK = threading.Lock()


def _parse_connection():
    """Lazily-created in-memory DuckDB used ONLY to parse user SQL.

    Deliberately not the analytics or system connection: parsing needs no
    catalog (an unknown table serializes fine), so a connection with nothing
    attached — no data, no secrets, no locks to contend for — is the smallest
    thing that can answer the question. Callers take a ``cursor()`` per call,
    which is what makes this safe from the request threads (~4 us; the parse
    itself is ~70 us).
    """
    global _PARSE_CONN
    with _PARSE_CONN_LOCK:
        if _PARSE_CONN is None:
            # Through the shared helper rather than a bare duckdb.connect:
            # every connection in the codebase pins its session to UTC that
            # way (guarded by tests/test_duckdb_session_tz.py).
            _PARSE_CONN = _open_duckdb(":memory:")
    return _PARSE_CONN


_ORACLE_HEALTHY: bool | None = None
_ORACLE_PROBE_LOCK = threading.Lock()

# The "no answer" branch of the self-check deliberately re-probes rather than
# latching, so a permanent failure (a parse connection that can never open, a
# build without `json_serialize_sql`) would otherwise emit one warning per
# request, forever, and drown the log it is trying to appear in. Throttled to
# one line a minute: the first occurrence is always logged, and a condition
# this sticky needs saying once, not once per query. Callers are unaffected —
# every request still re-probes and still falls back safely.
_ORACLE_PROBE_WARN_INTERVAL_S = 60.0
_oracle_probe_warned_at: float | None = None


def _warn_probe_failure(message: str, **kwargs) -> None:
    """Emit a probe-failure warning at most once per interval.

    Callers hold ``_ORACLE_PROBE_LOCK``, so the timestamp needs no lock of
    its own.
    """
    global _oracle_probe_warned_at
    now = time.monotonic()
    if _oracle_probe_warned_at is not None and now - _oracle_probe_warned_at < _ORACLE_PROBE_WARN_INTERVAL_S:
        return
    _oracle_probe_warned_at = now
    logger.warning(message, **kwargs)


# Above this many characters the SQL goes to the text scan instead of the
# parser. Measured expansion for a wide statement: 16k chars -> 1.3 MB of
# serialized JSON -> 6 MB of Python objects, growing linearly (64k chars ->
# 24 MB). `sql` has no length limit and the handler runs on the shared anyio
# threadpool, so the ceiling is per-request memory times whatever is in
# flight. 16k leaves ~10x headroom over the largest statement seen here
# (1.6 KB) while bounding one request to single-digit MB.
#
# Characters, not bytes — non-ASCII SQL can be up to 4x this in UTF-8, still
# bounded. And falling back is conservative in PRECISION, not in cost: over a
# statement this size the text scan burns more CPU than the parse it replaces
# (~87 ms against 200 view names). Bounding `sql` at the request model and
# rejecting outright would beat both, but that is a user-visible API change,
# so it is filed rather than smuggled in here.
_MAX_ORACLE_SQL_CHARS = 16 * 1024


def _oracle_answers_as_expected() -> bool:
    """One-time probe that the installed DuckDB still answers the way the
    guards read its output.

    The tripwire test covers a changed *error* shape, but the dangerous
    direction is a changed *success* shape: rename ``BASE_TABLE`` or
    ``table_name`` and every statement suddenly references nothing, which
    reads as "denies nothing" — a silent, total bypass. CI cannot be the only
    net here, because ``duckdb`` is pinned open-ended (``>=1.5.2``) and a
    built image can resolve a newer wheel than the one the suite ran against.

    So the parse path asks this first, and a wrong answer turns the oracle off
    for good: callers get ``None`` and fall back to the conservative text scan.

    A *wrong answer* latches; *no answer* does not. A wrong answer is
    deterministic — this engine will keep answering wrongly, so re-probing
    every request would buy nothing. No answer may be a transient (an
    interrupted call, memory pressure, a parse connection that failed to
    open), and latching on one would degrade the process until restart with
    no way back.

    "No answer" is ``None``, not an exception: the helper below catches
    everything and returns ``None``, so in production no exception reaches
    here at all. Checking only for a raised exception — as an earlier revision
    did — therefore latched on every real transient. The ``except`` remains
    for a caller-supplied helper that does raise, but ``None`` is the branch
    that fires. Neither retry is rate-limited: a permanently broken engine
    costs one failed parse (~90 us) per request, which is a better trade than
    one blip disabling the precise matcher for the process lifetime. Its other
    per-request cost — a warning line each time — is throttled by
    ``_warn_probe_failure``, so a permanent failure says so once a minute
    rather than once a query.
    """
    global _ORACLE_HEALTHY
    # Its own lock, NOT _PARSE_CONN_LOCK: the probe opens the parse connection,
    # which takes that one, and a plain Lock is not reentrant.
    with _ORACLE_PROBE_LOCK:
        if _ORACLE_HEALTHY is None:
            try:
                probe = _sql_referenced_names_unguarded("SELECT 1 FROM __agnes_oracle_probe__")
            except Exception:
                _warn_probe_failure(
                    "DuckDB parse oracle self-check raised; retrying on the next query. "
                    "SQL name guards fall back to text scanning until it succeeds.",
                    exc_info=True,
                )
                return False
            if probe is None:
                _warn_probe_failure(
                    "DuckDB parse oracle self-check returned no answer; retrying on the next "
                    "query. SQL name guards fall back to text scanning until it succeeds."
                )
                return False
            _ORACLE_HEALTHY = probe == {"__agnes_oracle_probe__"}
            if not _ORACLE_HEALTHY:
                logger.error(
                    "DuckDB parse oracle answered its self-check wrongly (got %r) — SQL name "
                    "guards fall back to text scanning for the life of this process.",
                    probe,
                )
        return _ORACLE_HEALTHY


def _sql_referenced_names(sql: str) -> set[str] | None:
    """Lowercased names of every table ``sql`` references, according to DuckDB.

    Returns ``None`` — meaning "no answer, use the conservative text scan" —
    in three cases: the statement is longer than ``_MAX_ORACLE_SQL_CHARS``,
    the engine failed its self-check (see ``_oracle_answers_as_expected``), or
    DuckDB would not serialize the SQL (a ``PIVOT`` subquery, a backtick-quoted
    BQ path, anything malformed, or a non-SELECT statement anywhere in it).

    Otherwise every name reported is a real reference, and every reference
    DuckDB will resolve at bind time is reported — with two exceptions that
    ``_assert_select_only`` blocks upstream, so no caller can meet them: SQL
    smuggled through a string-taking table function (``query('…')``), and
    ``EXECUTE`` of a prepared statement. A table macro hides its body from
    this too, but equally from the text scan — neither can see a name the SQL
    does not contain.
    """
    if len(sql) > _MAX_ORACLE_SQL_CHARS or not _oracle_answers_as_expected():
        return None
    try:
        return _sql_referenced_names_unguarded(sql)
    except Exception:
        # The walk can raise on a serialized shape the probe's SELECT never
        # exercises (a future DuckDB handing back a non-string table_name,
        # say). An access question must degrade to the text scan, not surface
        # as a request error. The probe keeps calling the unguarded form, so
        # a breakage still gets diagnosed there.
        _warn_probe_failure(
            "DuckDB parse oracle raised while walking a query's parse tree; "
            "falling back to the text scan for this query.",
            exc_info=True,
        )
        return None


def _sql_referenced_names_unguarded(sql: str) -> set[str] | None:
    """The parse and walk itself, with none of the guards above it.

    Call ``_sql_referenced_names`` instead — this exists unguarded only
    because the self-check has to exercise the real parse path to be worth
    anything, and it cannot do that through a function that asks it first.

    ``json_serialize_sql`` hands back DuckDB's own parse tree, in which every
    table reference is a ``BASE_TABLE`` node. Using the engine that will run
    the query as the oracle is the point: a third-party SQL parser can
    disagree with DuckDB about what a construct means, and when it does, the
    disagreement is a security hole. sqlglot, for instance, reads DuckDB's
    ``(TABLE v)`` shorthand as a column named ``table`` and lexes ``values``
    as a keyword, so ``SELECT * FROM (TABLE values) t`` reads a view while
    naming nothing. DuckDB cannot disagree with itself, and a DuckDB upgrade
    moves both together.

    It parses; it does not bind or execute. An unknown table serializes
    happily, a non-SELECT statement is refused, and a serialized INSERT
    inserts nothing. The SQL is passed as a bound parameter — never
    interpolated. All statements of a multi-statement string are covered.
    """
    try:
        cursor = _parse_connection().cursor()
        try:
            row = cursor.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
        finally:
            cursor.close()
        document = json.loads(row[0]) if row and row[0] else None
    except Exception:
        return None
    if not isinstance(document, dict) or document.get("error"):
        return None
    names: set[str] = set()
    # Iterative walk: a deeply nested statement parses fine but would blow
    # Python's recursion limit, and that must not decide an access question.
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "BASE_TABLE":
                # The bare name only: every caller compares an unqualified
                # identifier (an information_schema view name, or a registry
                # id, which `[a-z_][a-z0-9_]*` validation keeps dot-free), and
                # a qualified `main.orders` still yields `orders` here. Refs
                # into un-granted catalogs are layer (a)'s job, not this one's.
                table = (node.get("table_name") or "").lower()
                if table:
                    names.add(table)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return names


def _sql_reference_test(sql_lower: str):
    """Return ``predicate(name) -> bool``: does the SQL reference that table?

    Takes an already-lowercased statement — both guards hold one, and the
    parameter says so, because what gets handed to the parser matters (a
    case-folded copy folds quoted identifiers too, which is safe only because
    DuckDB resolves names case-insensitively and this module lowercases every
    name it extracts).

    Parses once and answers every name from that result, falling back to the
    conservative text scan when the oracle declines. Both guards go through
    here so the "oracle, else text scan" decision cannot drift apart.
    """
    referenced = _sql_referenced_names(sql_lower)
    if referenced is not None:
        return referenced.__contains__
    masked = _mask_backticks(sql_lower)
    return lambda name: _sql_text_references_name(masked, name)


# Memoized in `src/remote_engines`; aliased here for this module's call sites.
_name_reference_re = name_reference_re


def _sql_text_references_name(sql_masked_lower: str, name: str) -> bool:
    """Conservative word-boundary fallback for when DuckDB won't parse the SQL.

    Over-matches by design — a column or alias spelled like a denied view
    trips it — because on this path nothing is known about the statement's
    structure and a missed reference in a deny check leaks data. Runs on a
    backtick-masked copy (issue #201).

    The one position it skips is a name immediately followed by ``BY``: the
    keyword half of a two-word clause (ORDER BY, GROUP BY, PARTITION BY — the
    only ones DuckDB has), which no reference can occupy because DuckDB
    rejects a bare ``by`` as a table alias. Without it, an ungranted view
    named ``order`` would deny every sorted query reaching this path.
    """
    return _name_reference_re(name).search(sql_masked_lower) is not None


def _enforce_non_admin_sql_rbac(analytics, sql_lower: str, allowed) -> None:
    """Non-admin SQL RBAC deny checks shared by ``/api/query`` (``execute_query``)
    and the snapshot ``run_remote_select_to_arrow`` path — previously duplicated
    verbatim in both.

    ``allowed`` is ``get_accessible_tables(user, conn)``; ``None`` means admin →
    no checks. Layers (b) and (c) match against the tables DuckDB says the SQL
    references (``_sql_referenced_names``), falling back to a word-boundary
    scan of a backtick-masked copy (``_mask_backticks``, issue #201) when
    DuckDB will not parse it. Three layers:

      (a) #868 — block catalog-qualified refs into un-granted local extract
          catalogs (``<source>.main.x``);
      (b) master-view-name denylist — default-catalog views not covered by a
          granted registry row;
      (c) M1 — internal extract metadata tables
          (``_meta``/``_remote_attach``/``_remote_links``); kept as
          defense-in-depth, now subsumed by (a) when the catalog is attached.

    The ``_bq_guardrail_inputs`` remote-row gate stays in each caller (it also
    computes the dry-run set used downstream).
    """
    if allowed is None:  # admin — sees all
        return
    from src.rbac import table_not_in_stack_message

    sql_lower_masked = _mask_backticks(sql_lower)
    references = _sql_reference_test(sql_lower)

    # (a) #868 catalog gate
    _assert_no_ungranted_catalog_ref(sql_lower_masked, analytics)

    # (b) master-view-name denylist. `allowed` carries registry IDs
    # (resource_grants.resource_id); DuckDB master views are named by registry
    # display `name`, so map name->id to compare apples to apples (avoids the
    # over-deny when id != name — Devin Review iter #5 on PR #168).
    all_views = {
        row[0]
        for row in analytics.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
        ).fetchall()
    }
    allowed_ids = set(allowed)
    registry_rows = table_registry_repo().list_all()
    allowed_view_names = {r["name"] for r in registry_rows if r.get("name") and r.get("id") in allowed_ids}
    for table in all_views - allowed_view_names:
        if references(table.lower()):
            raise HTTPException(status_code=403, detail=table_not_in_stack_message(table))

    # (c) internal extract metadata tables (audit M1) — see
    # _assert_no_ungranted_catalog_ref for why base tables slip the view denylist.
    for internal in _INTERNAL_EXTRACT_TABLES:
        if references(internal):
            raise HTTPException(
                status_code=403,
                detail="query references an internal extract metadata table (_meta/_remote_attach/_remote_links)",
            )


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


def _max_query_rows() -> int:
    """Hard ceiling on rows returned by POST /api/query. Operators who
    legitimately need more can raise it via AGNES_MAX_QUERY_ROWS."""
    try:
        return max(1, int(os.environ.get("AGNES_MAX_QUERY_ROWS", "1000000")))
    except (TypeError, ValueError):
        return 1_000_000


class QueryRequest(BaseModel):
    sql: str
    limit: int = 1000

    @field_validator("limit")
    @classmethod
    def _clamp_limit(cls, v: int) -> int:
        # DoS guard: `limit` was unbounded, so a single request could stream an
        # entire local table into a Python list (twice) and OOM the worker
        # (/api/query has no cost/row cap for local tables, unlike /api/v2/scan).
        # Clamp to the ceiling; non-positive falls back to the default. Silent
        # clamp (not a 422) keeps existing callers working — the response's
        # `truncated` flag still signals that more rows existed.
        if v <= 0:
            return 1000
        return min(v, _max_query_rows())


class QueryResponse(BaseModel):
    columns: list
    rows: list
    row_count: int
    truncated: bool = False
    # BigQuery dry-run scan estimate (bytes) for `query_mode='remote'`
    # queries; ``None`` for local DuckDB queries (no BQ tables involved).
    bytes_scanned: int | None = None
    # Task 11 (§10): disclosure envelope when this result read through one
    # or more access-policied tables -- ``{"policied_tables": [id, ...],
    # "note": str}``, built by ``row_scope_payload``. ``None`` when no table
    # this query touched carries a policy, or the caller is the admin
    # bypass (§12) -- either way the result is the raw table, so there is
    # nothing to disclose. "Silent partial scope is forbidden"
    # (command-ux.md) applies to row filtering as much as to source scope.
    row_scope: dict | None = None


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
    # A Principal has no .get("id") — treat co-session / agent-session as non-admin
    # for internal row-level filter. build_filter_clause expects a dict
    # so pass a shim when user is a principal (mirrors v2_sample.py:131-137).
    # The shim id "session.none" passes the safe-identifier regex but never
    # matches a real user_id; the email local-part "session.none" similarly
    # passes the username regex but matches no real session. Together the
    # filter yields zero rows for every internal table — correct behaviour
    # (co-sessions should not see any single user's internal rows).
    if isinstance(user, PRINCIPAL_TYPES):
        is_admin = False
        user = {"id": "session.none", "email": "session.none@internal"}
    else:
        uid = user.get("id")
        is_admin = is_user_admin(uid, conn) if uid else False
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
        [str(v) if v is not None and not isinstance(v, (int, float, bool, str)) else v for v in row] for row in rows
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


# Functions that take `FROM` as an *argument separator* rather than a clause
# keyword: `EXTRACT(YEAR FROM ts)`, `SUBSTRING(s FROM 2)`, `TRIM(… FROM s)`.
# The identifier following that FROM is a column, not a table.
_FROM_AS_ARGUMENT_FUNCS = frozenset(
    {
        "extract",
        "substring",
        "substr",
        "trim",
        "overlay",
        "position",
    }
)

# One dotted-path segment: bare, double-quoted, or backticked. A backticked
# segment may itself contain dots, since a whole quoted FQN is a single token.
_SQL_IDENT_SEGMENT = r'(?:"[^"]+"|`[^`]+`|[A-Za-z_][\w$-]*)'
_SQL_IDENT_PATH = re.compile(
    rf"\b(?:from|join)\s+({_SQL_IDENT_SEGMENT}(?:\s*\.\s*{_SQL_IDENT_SEGMENT})*)",
    re.IGNORECASE,
)


def _mask_sql_noise(sql: str) -> str:
    """Blank string literals and comments, preserving length and offsets.

    Paren counting and the FROM/JOIN scan must not see brackets or keywords
    that live inside quoted values or comments — `SELECT 'extract(' AS tag,
    x FROM orders` used to read that literal `(` as a function-argument list
    and drop the genuine `FROM orders` (Devin Review on #1121). Double-quoted
    and backticked spans are identifiers, not literals, so they are skipped
    over intact: their contents stay matchable as part of a table path, and a
    quote inside them can't open a phantom literal.
    """
    out = list(sql)
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ('"', "`"):
            # Quoted identifier — step over it without blanking.
            close = sql.find(ch, i + 1)
            i = n if close == -1 else close + 1
        elif ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":  # '' escape
                        j += 2
                        continue
                    break
                j += 1
            for k in range(i, min(j + 1, n)):
                out[k] = " "
            i = j + 1
        elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _enclosing_calls(masked: str, positions: list) -> dict:
    """Map each position to the name of the function call enclosing it.

    ONE left-to-right pass with a paren stack, instead of walking left from
    every match: the old per-match walk plus a tail slice made labelling cost
    grow with the square of the query length, so one oversized query could
    tie up a worker far beyond the query itself (Devin Review on #1121).
    """
    result: dict = {}
    stack: list = []
    pending = sorted(positions)
    p = 0
    for i, ch in enumerate(masked):
        while p < len(pending) and pending[p] <= i:
            result[pending[p]] = stack[-1] if stack else None
            p += 1
        if ch == "(":
            end = i
            while end > 0 and masked[end - 1].isspace():
                end -= 1
            start = end
            while start > 0 and (masked[start - 1].isalnum() or masked[start - 1] == "_"):
                start -= 1
            stack.append(masked[start:end].lower() or None)
        elif ch == ")":
            if stack:
                stack.pop()
    while p < len(pending):
        result[pending[p]] = stack[-1] if stack else None
        p += 1
    return result


def _next_nonspace(s: str, idx: int) -> str:
    """First non-space character at/after ``idx`` — without copying the tail."""
    n = len(s)
    while idx < n and s[idx].isspace():
        idx += 1
    return s[idx] if idx < n else ""


def _normalize_table_path(raw: str) -> str | None:
    """Reduce a matched identifier path to the table id used for tagging.

    Quotes are stripped per segment and the segments are re-joined with dots,
    so the recorded id keeps the WHOLE path the query named
    (`project.dataset.table`, `bq.dataset.table`, `schema.table`).

    Keeping only the tail segment was ambiguous in both directions, and the
    id is the group-by key of the top-tables ranking:

    * two physically different tables that share a name
      (`proj_a.ds1.orders`, `proj_b.ds2.orders`) aggregated into one row, and
      that row could read as `registered` merely because an unrelated
      registry id happened to be `orders`;
    * a registry-gated `bq."dataset"."table"` reduced to a bare `table`,
      which usually matches no registry id, so a legitimately gated query
      rendered as `unregistered`.

    Mapping a path onto the registry id it refers to is the aggregation's job
    (``_registry_identity_keys`` in ``src/repositories/usage.py``), which can
    do it *without* first discarding what the query actually referenced.
    """
    parts: list[str] = []
    for segment in re.findall(_SQL_IDENT_SEGMENT, raw):
        inner = segment.strip('`"')
        parts.extend(piece for piece in inner.split(".") if piece)
    if not parts:
        return None
    return ".".join(parts)


def _first_table_from_sql(sql: str) -> str | None:
    """Extract the first table reference after FROM or JOIN, for audit tagging.

    Regex-based and still best-effort, but the result is the group-by key of
    the query-telemetry top-tables ranking, so a non-table identifier does not
    merely pollute one audit row — it surfaces on a dashboard as usage of a
    table that does not exist. Two classes are therefore filtered out
    rather than tolerated:

    * `FROM` used as a function argument separator (`EXTRACT(… FROM col)`),
      which would otherwise tag the *column*;
    * table-valued functions (`FROM UNNEST([…])`), which are inline values.

    A qualified path is recorded in full (see `_normalize_table_path`); the
    aggregation resolves it to a registry id when one owns that path.

    Returns None when no table reference is found.
    """
    if not sql:
        return None
    # Scan the masked text so a FROM inside a literal or comment is not a
    # match at all, then read the identifier back out of the ORIGINAL sql
    # (masking preserves offsets, and identifier spans are never masked).
    masked = _mask_sql_noise(sql)
    matches = list(_SQL_IDENT_PATH.finditer(masked))
    if not matches:
        return None
    enclosing = _enclosing_calls(masked, [m.start() for m in matches])
    for match in matches:
        if enclosing.get(match.start()) in _FROM_AS_ARGUMENT_FUNCS:
            continue
        if _next_nonspace(masked, match.end()) == "(":
            # `FROM unnest(...)` / `FROM generate_series(...)`: a function
            # call producing inline rows, not a relation.
            continue
        table = _normalize_table_path(sql[match.start(1) : match.end(1)])
        if table:
            # Lowercase: registry ids are lowercased at registration, so
            # `from Orders` must not tag `table:Orders` and then read as
            # unregistered (and group apart) — Devin Review on #1121.
            return table.lower()[:200]
    return None


# SQL keywords / functions rejected on every user-submitted query path. Shared
# by `execute_query` (the /api/query handler) and `run_remote_select_to_arrow`
# (the snapshot `from_query` materialize path) so the two surfaces can never
# drift on what counts as a safe single-SELECT.
_BLOCKED_SQL_TOKENS = [
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
    # #160: bigquery_query() bypasses the registry / RBAC entirely
    # (it runs an arbitrary BQ jobs API call against any reachable
    # dataset). Wrap views created by the BQ extractor use it inside
    # CREATE VIEW bodies, but those run via DuckDB's view resolution at
    # query time — user-submitted SQL never contains the function name.
    "bigquery_query",
    "glob(",
    "list_files",
    "'/",
    '"/',
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
    # DuckDB's SQLite-compat catalog views. `SELECT sql FROM sqlite_master`
    # returns every view's full CREATE VIEW body — which for the orchestrator's
    # master views is `... AS SELECT * FROM read_parquet('/data/extracts/...')`,
    # disclosing absolute on-disk parquet paths. Same class as duckdb_views
    # above; the `duckdb_*` names just didn't cover the `sqlite_*` aliases
    # (sqlite_master / sqlite_schema / sqlite_temp_master / sqlite_temp_schema —
    # all four resolve in DuckDB).
    "sqlite_master",
    "sqlite_schema",
    "sqlite_temp_master",
    "sqlite_temp_schema",
    # Leaks cached external file paths (absolute parquet paths) directly.
    "duckdb_external_file_cache",
    "pragma_table_info",
    "pragma_storage_info",
    # Relative path traversal
    "'../",
    '"../',
    # Multiple statements
    ";",
]


# Security audit F8: DuckDB resolves a quoted string in table position as a
# file to scan (a "replacement scan"), so ``SELECT * FROM 'data/extracts/…'``
# reads a file with NO ``read_parquet()`` call and slips past the function
# denylist above. The existing ``'/`` / ``'../`` tokens only catch absolute or
# dot-dot paths — a bare relative path like ``'data/…parquet'`` has neither.
#
# We detect file table sources by inspecting TABLE SOURCES precisely via
# sqlglot: a real table/view is a SQL identifier (optionally schema-qualified)
# and never contains a path separator, glob metacharacter, or data-file
# extension, whereas a DuckDB replacement scan (``FROM 'file.parquet'`` — direct,
# comma-list, or glob) parses as a Table whose NAME carries exactly those.
# Inspecting names — not arbitrary literals — is what makes this precise: a
# legitimate value literal in SELECT/WHERE position (``WHERE f = 'report.csv'``),
# or a functional ``FROM`` such as ``TRIM(' ' FROM x)`` / ``EXTRACT(day FROM
# ts)``, is never a Table and so is not flagged. The external-access boundary on
# the analytics connection stays ON because local views need ``read_parquet`` —
# this parse-level guard is the file-access boundary.
_FILE_TABLE_EXTS = frozenset({"parquet", "parq", "csv", "tsv", "json", "ndjson", "arrow", "duckdb", "xlsx"})

# Fallback ONLY for when sqlglot cannot parse the SQL at all: a quoted string
# literal directly after ``FROM`` / ``JOIN``. This is deliberately not used on
# parseable SQL because it over-matches functional FROM clauses like
# ``TRIM(' ' FROM 'abc')`` — sqlglot models those correctly, so the primary
# path never sees the false positive; an unparseable query is almost certainly
# invalid anyway, so a conservative reject there is acceptable.
_FROM_STRING_LITERAL_RE = re.compile(r"\b(?:from|join)\s*\(*\s*'")


def _name_looks_like_file(name: str) -> bool:
    if not name:
        return False
    if any(c in name for c in "/\\*?"):
        return True
    return "." in name and name.rsplit(".", 1)[-1].lower() in _FILE_TABLE_EXTS


def _has_file_table_source(sql: str) -> bool:
    """True if any FROM/JOIN table source is a file path (a DuckDB replacement
    scan), inspected precisely via sqlglot. This is the PRIMARY and only F8
    check on parseable SQL — it covers the direct ``FROM 'file'``, comma-list
    (``FROM v, 'file'``), and glob forms uniformly, without the false positives
    a position regex has on functional FROM clauses (TRIM/EXTRACT/SUBSTRING).
    Falls back to the position-based regex only when the SQL can't be parsed as
    DuckDB, so the direct/comma ``FROM 'file'`` form is still caught there.

    Depends on sqlglot (pinned ``sqlglot>=30.0.0`` in pyproject) modeling a
    quoted FROM source as an ``exp.Table`` whose ``.name`` carries the path.
    That behavioral assumption has dedicated tripwire tests
    (``test_f8_sqlglot_models_file_table_source_as_table`` covers direct AND
    comma-list) so a future sqlglot upgrade that changes it fails loudly rather
    than silently regressing detection."""
    try:
        import sqlglot
        from sqlglot import exp

        statements = sqlglot.parse(sql, read="duckdb")
    except Exception:
        return bool(_FROM_STRING_LITERAL_RE.search(sql.lower()))
    for statement in statements:
        if statement is None:
            continue
        for table in statement.find_all(exp.Table):
            if _name_looks_like_file(table.name):
                return True
    return False


# Table functions whose target is SQL (or a table name) passed as a STRING.
# They are invisible to every name-matching gate in this module: the non-admin
# RBAC denylist below decides access by regex-matching view names in the SQL
# text, so `query('select * from ' || 'x')` names nothing it can match while
# still executing against the analytics catalog, where every view resolves.
# `query_table` and `bigquery_query` are also on `_BLOCKED_SQL_TOKENS`; that
# list cannot cover the concatenated form (nor bare `query(`, which it never
# had), so this parse-level guard is the real boundary and the tokens stay as
# defense-in-depth.
_SQL_STRING_TABLE_FUNCTIONS = frozenset(
    {
        "query",
        "query_table",
        "bigquery_query",
        "postgres_query",
        "sqlite_query",
        "mysql_query",
        # Same shape, opposite direction: the extensions that ship the `_query`
        # readers also ship these, which run an arbitrary statement string
        # against an attached database — writes included. They are not table
        # functions, so they appear in a projection rather than in FROM, and
        # none of them is on `_BLOCKED_SQL_TOKENS`. Whether they resolve
        # depends on which extensions the analytics connection has loaded;
        # naming them costs nothing and closes the case where one is.
        # (Devin Review on #1264.)
        "postgres_execute",
        "sqlite_execute",
        "mysql_execute",
    }
)

# Fallback ONLY for unparseable SQL, mirroring `_FROM_STRING_LITERAL_RE`: the
# function name followed by its opening paren. Over-matching is acceptable on
# SQL that does not parse — it is almost certainly invalid anyway. The optional
# quote characters are there because DuckDB accepts a quoted function name
# (`"query"('…')`) and the fallback must see the same call the parser does.
_SQL_STRING_FN_RE = re.compile(
    r"""["`\[]?\b(?:""" + "|".join(sorted(_SQL_STRING_TABLE_FUNCTIONS)) + r""")\b["`\]]?\s*\("""
)


def _anonymous_name(node) -> str:
    """The called name of an ``exp.Anonymous``, quoted or not.

    ``node.this`` is a plain ``str`` for ``query(…)`` but an ``exp.Identifier``
    for ``"query"(…)`` — a form DuckDB resolves identically. An ``isinstance
    (…, str)`` test therefore read the quoted call as "not one of ours", and
    because the statement PARSES, the text fallback never ran either: quoting
    the function name walked straight through the guard. (Devin Review on
    #1264 — filed as a question about node shape; it is a bypass.)
    """
    raw = getattr(node, "this", None)
    if isinstance(raw, str):
        return raw
    name = getattr(raw, "name", None)
    return name if isinstance(name, str) else ""


def _has_sql_string_table_function(sql: str) -> bool:
    """True if the SQL calls a table function that takes SQL/table-name as a
    string (see ``_SQL_STRING_TABLE_FUNCTIONS``).

    Detection is on the parsed tree and keys on the function NAME, which is
    what makes the evasion detectable: the argument may be computed
    (``'a' || 'b'``, ``chr(105)``) but the call itself is always an
    ``exp.Anonymous`` node carrying the literal name. Inspecting names — not
    literals — also keeps a string that merely contains the name (``WHERE note
    = 'query(1)'``) from tripping it, and leaves an identifier such as
    ``query_id`` or ``saved_queries`` alone.

    Falls back to a text scan when sqlglot cannot parse the statement, so an
    unparseable query cannot fail open. Tripwire:
    ``test_sqlglot_models_sql_string_table_function_as_anonymous``."""
    try:
        import sqlglot
        from sqlglot import exp

        statements = sqlglot.parse(sql, read="duckdb")
    except Exception:
        return bool(_SQL_STRING_FN_RE.search(sql.lower()))
    found_statement = False
    for statement in statements:
        if statement is None:
            continue
        found_statement = True
        for node in statement.find_all(exp.Anonymous):
            name = _anonymous_name(node)
            if name and name.lower() in _SQL_STRING_TABLE_FUNCTIONS:
                return True
    if not found_statement:
        # sqlglot returned nothing parseable (e.g. all-None statements) —
        # treat as unparseable rather than clean.
        return bool(_SQL_STRING_FN_RE.search(sql.lower()))
    return False


def _assert_select_only(sql_lower: str) -> None:
    """Raise HTTPException(400) unless ``sql_lower`` is a single SELECT/WITH
    query free of the blocked keywords/functions. ``sql_lower`` MUST already
    be ``.strip().lower()``-ed by the caller."""
    # Tolerate exactly one trailing semicolon — a routine SQL-formatting habit
    # (LLM-generated queries, most CLI/DB clients) rather than a second
    # statement. A ";" that remains after stripping it is a genuine
    # multi-statement attempt and still blocked below.
    body = strip_one_trailing_semicolon(sql_lower)
    if any(keyword in body for keyword in _BLOCKED_SQL_TOKENS):
        raise HTTPException(status_code=400, detail="Only single SELECT queries are allowed")
    # File-path table source anywhere in the FROM graph (direct / comma-list /
    # glob), detected precisely via sqlglot — the position regex is used only as
    # the parse-failure fallback inside _has_file_table_source, so functional
    # FROM clauses (TRIM/EXTRACT/SUBSTRING) don't false-positive.
    if _has_file_table_source(body):
        raise HTTPException(
            status_code=400,
            detail="File-path table sources are not allowed; query registered views by name",
        )
    # SQL-as-a-string table functions (query/query_table/…): their target never
    # appears as a matchable token, so the RBAC name denylist cannot see it.
    if _has_sql_string_table_function(body):
        raise HTTPException(
            status_code=400,
            detail=(
                "Table functions that take SQL as a string (query, query_table, "
                "bigquery_query, …) are not allowed; query registered views by name"
            ),
        )
    # Accept any whitespace (newline, tab, space) after the keyword so
    # multi-line SQL doesn't 400 on `SELECT\n  col, ...`. Strip leading `--`
    # / `/* */` comments first so a query whose stored SQL opens with a header
    # comment isn't rejected — DuckDB and the local `agnes query` path tolerate
    # them. The blocklist above still scans the full SQL, so a comment can't
    # smuggle a blocked keyword through.
    if not re.match(r"^(select|with)\s", _strip_leading_sql_comments(body)):
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

    # One trailing `;` is now accepted (it is a terminator, not a second
    # statement), so normalize it away HERE, once, instead of at each place
    # that embeds the statement. Two reasons this is the boundary and not a
    # sixth site-local strip:
    #
    #   - `_bq_quota_and_cap_guard` below passes `request.sql` through
    #     `_rewrite_user_sql_for_bq_dry_run` — a purely textual rewrite that
    #     preserves the terminator — into a BigQuery dry run. If BQ classifies
    #     a `;`-terminated text as a script, the dry run reports
    #     `totalBytesProcessed: 0` and the scan cap silently passes a query it
    #     was meant to measure. That is a cost guardrail reading zero, not an
    #     error message, and it is not something this repo's tests can
    #     observe. Normalizing removes the question rather than answering it.
    #   - The jobs-API execution wrap at the bottom of the BQ arm nests the
    #     statement inside a dollar-quoted payload, so DuckDB never sees the
    #     `;` — but BigQuery does, verbatim.
    #
    # Accepted/rejected outcomes do not change: the guard above has already
    # ruled on the statement, and `sql_lower` is recomputed from the
    # normalized text so every check below reads the same string that runs.
    request.sql = strip_one_trailing_semicolon(request.sql)
    sql_lower = request.sql.strip().lower()

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
        if (
            BQ_PATH.search(_mask_backticks(request.sql))
            or SF_PATH.search(_mask_backticks(request.sql))
            or _BACKTICK_FULL_PATH.search(request.sql)
        ):
            raise HTTPException(
                status_code=400,
                detail="Internal tables can't be combined with `bq.*` or `sf.*` paths in a single SELECT (v1 limitation).",
            )
        # Reject if user SQL also references any non-internal registry id —
        # that would be a mixed query against analytics.duckdb views. Matched
        # against the tables DuckDB says are referenced, so a registered table
        # named for a SQL keyword (`order`) no longer collides with every
        # ORDER BY; text-scan fallback when DuckDB won't parse the SQL.
        references = _sql_reference_test(sql_lower)
        registry_rows = table_registry_repo().list_all()
        for r in registry_rows:
            rid = r.get("id") or ""
            if not rid or is_internal_table(rid):
                continue
            if references(rid.lower()):
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
    # Databricks counterparts of `_dry_run_set`: the plan (None unless this
    # statement runs on a warehouse) and the result bytes it reported. Bound
    # here so the response/audit tail below sees them on every path, including
    # the ones that raise before planning.
    _dbx_plan: dict | None = None
    _dbx_bytes: int | None = None
    try:
        # Non-admin SQL RBAC: catalog gate (#868) + master-view-name denylist +
        # internal-extract-table denylist (M1). Shared with the snapshot path
        # via _enforce_non_admin_sql_rbac; no-op for admins (allowed is None).
        _enforce_non_admin_sql_rbac(analytics, sql_lower, allowed)

        # Table access policies (§5/§6): substitute every policied table
        # reference in the caller's SQL with its resolved, caller-scoped
        # relation. No special-casing for admins here — policied_relation
        # already returns the passthrough (unfiltered) relation for a
        # full-surface admin, so this call is a no-op for them too. Inert
        # (byte-identical SQL, empty params) unless a table this query
        # touches actually carries a policy.
        try:
            policy_rewritten_sql, policy_params, policied_table_ids = rewrite_sql(
                request.sql, user, dialect=_policy_parse_dialect(request.sql, sql_lower)
            )
        except PolicyNameCollision as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "reason": "policy_name_collision",
                    "table": exc.table_id,
                    "fix": "rename your CTE",
                },
            )
        except PolicyIdentityUnresolvable:
            raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
        except PolicyError as exc:
            # Never fall back to the unfiltered table (§17). The raw
            # engine/parse detail is deliberately not surfaced (§16) — a
            # failing policy's error can quote literal values from the
            # policy body.
            raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})

        # ---- Which remote engine, if any, runs this statement -------------
        # `None` for an all-local query and for BigQuery, which keeps its own
        # (unchanged) guardrail below; a plan dict when the statement resolves
        # to a Databricks warehouse. Referencing both engines at once is
        # refused inside the planner — there is no join layer between them.
        _dbx_plan = _databricks_remote_plan(request.sql, sql_lower, conn, user, allowed)
        if _dbx_plan is not None and policied_table_ids:
            # The statement runs on the warehouse, so the policy has to travel
            # with it: substituted into the SQL and its identity values bound
            # as request parameters. Any failure below denies (§17) — there is
            # no branch that forwards the unfiltered statement.
            try:
                _apply_databricks_policies(_dbx_plan, request.sql, user, expected_ids=policied_table_ids)
            except PolicyNameCollision as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"reason": "policy_name_collision", "table": exc.table_id, "fix": "rename your CTE"},
                )
            except PolicyIdentityUnresolvable:
                raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
            except PolicyError as exc:
                raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})

        # ---- #160 BQ remote-row guardrail + RBAC patch -------------------
        dry_run_set, name_lookups, blocked_bq_path = (
            ([], [], None)
            if _dbx_plan is not None
            else _bq_guardrail_inputs(
                request.sql,
                sql_lower,
                conn,
                user,
                allowed,
            )
        )
        _dry_run_set = dry_run_set  # expose to outer scope for audit
        if blocked_bq_path is not None:
            raise HTTPException(status_code=403, detail=blocked_bq_path)

        # Snowflake direct-path guard. The SF extension runs inside DuckDB,
        # but ``sf."schema"."table"`` bypasses master views, so it needs the
        # same registration + RBAC check as ``bq.*``.
        blocked_sf_path = _sf_guardrail_inputs(
            request.sql,
            sql_lower,
            conn,
            user,
            allowed,
        )
        if blocked_sf_path is not None:
            raise HTTPException(status_code=403, detail=blocked_sf_path)

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
        # A restricted principal (co-session / agent-session) has no
        # ".get" — resolve via _identity_for_audit (owner identity for an
        # AgentPrincipal, "anon" for a SessionPrincipal's shared bucket;
        # this is bookkeeping, not an authorization decision).
        _audit_uid, _audit_email = _identity_for_audit(user)
        user_id = _audit_email or _audit_uid or "anon"
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
            if _dbx_plan is not None:
                # Databricks: the statement was rewritten to warehouse-native
                # SQL by the planner and runs there whole — there is no local
                # fallback path to fall through to, because the parquet a
                # `query_mode='remote'` row would need does not exist on this
                # server. Bytes are reported for disclosure only; unlike
                # BigQuery they are result bytes, not scanned bytes, so they
                # are deliberately NOT billed against the BQ daily byte quota
                # (which prices a different thing on a different engine).
                columns, rows, truncated, _dbx_bytes = _execute_databricks_plan(_dbx_plan, request.limit, user_id)
            else:
                # Performance fix: rewrite user SQL referencing BQ-remote tables
                # to a single ``bigquery_query()`` call so WHERE / projection /
                # LIMIT push into BQ via jobs.query (1-2 s) instead of falling
                # through DuckDB's ATTACH-catalog Storage Read API session over
                # the full table (often 70-150 s, fails with "Response too
                # large to return" on >100M-row sources). Helper returns the
                # original SQL unchanged when rewriting would be unsafe
                # (cross-source JOIN, no BQ tables referenced, double-wrap).
                #
                # This plans the push-down against ``request.sql`` — the
                # caller's ORIGINAL, unfiltered SQL. Fine for a query that
                # touches no policied table (the common case). When
                # ``policied_table_ids`` is non-empty, the resulting
                # ``execution_sql`` below is NEVER used to execute anything —
                # see the branch immediately below, which routes those
                # queries around this push-down entirely (§7.1: a
                # dollar-quoted ``bigquery_query()`` payload has no bind
                # mechanism, so ``$user_groups`` would either blow up with a
                # DuckDB parameter-count mismatch, or — when the policy needs
                # no bind value at all — nothing would raise and the
                # unfiltered result would silently ship with a 200).
                # ``did_rewrite`` is still meaningful on its own: it is True
                # only when EVERY table this query touches is
                # ``query_mode='remote'`` (``_bq_remote_execution_plan``'s own
                # cross-source bail, Skip 3) — exactly the condition under
                # which the policied branch below can run the whole query
                # directly against BigQuery.
                execution_sql, did_rewrite = _rewrite_user_sql_for_bigquery_query(
                    request.sql,
                    conn,
                )

                if did_rewrite and policied_table_ids:
                    # §7.1-§7.4: this query touches a policied
                    # ``query_mode='remote'`` table and is otherwise
                    # push-down-eligible. Never use the ``bigquery_query()``
                    # push-down above for it — run the whole query directly
                    # against the BQ jobs API instead, with the policy
                    # transpiled to BigQuery dialect and bound as named
                    # parameters (§7.2). Any failure past collision detection —
                    # building the query parameters, or the BQ job itself —
                    # becomes a table-scoped ``policy_error``; it NEVER falls
                    # back to the push-down or to an unfiltered execution
                    # (§17 — every failure denies).
                    try:
                        bq = get_bq_access()
                        table = _execute_policied_remote_bq(
                            request.sql,
                            user,
                            bq,
                            name_lookups=name_lookups,
                            labels=job_labels_for(user, "query"),
                            outer_limit=request.limit + 1,
                            expected_ids=policied_table_ids,
                        )
                    except PolicyNameCollision as exc:
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "reason": "policy_name_collision",
                                "table": exc.table_id,
                                "fix": "rename your CTE",
                            },
                        )
                    except PolicyIdentityUnresolvable:
                        raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
                    except PolicyError as exc:
                        raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
                    columns, rows, truncated = _arrow_table_to_rows(table, request.limit)
                else:
                    if did_rewrite:
                        # Memory-safety: ``bigquery_query()`` materialises the entire
                        # BQ result into DuckDB before fetchmany sees it (vs the
                        # ATTACH-catalog Storage Read API path, which streams rows
                        # lazily). Wrap the rewritten SQL in an outer ``LIMIT N+1``
                        # so a `SELECT *` against a billion-row remote table doesn't
                        # buffer the full table into the worker process — the cap
                        # is pushed into the BQ job itself. Aliased subquery so the
                        # outer LIMIT applies to the final rewritten result.
                        execution_sql = f"SELECT * FROM ({execution_sql}) AS _bqq_outer LIMIT {request.limit + 1}"
                        logger.info(
                            "query_rewrite_to_bigquery_query: user_id=%s — wrapped "
                            "SQL in bigquery_query() with outer LIMIT for BQ "
                            "predicate pushdown",
                            user_id,
                        )
                    else:
                        # Non-push-down (plain ATTACH-catalog) path: run the
                        # access-policy-rewritten SQL instead of the raw analyst SQL.
                        # Byte-identical to request.sql — so this branch is a no-op
                        # change — unless a table this query touches is policied
                        # (rewrite_sql's inert-until-attached guarantee).
                        execution_sql = policy_rewritten_sql
                        logger.debug(
                            "query_rewrite_skipped: user_id=%s — running original SQL via ATTACH-catalog path",
                            user_id,
                        )

                    # Open in read-only mode for extra safety. If the rewritten
                    # path errors (e.g. user SQL contained DuckDB-only syntax —
                    # ``::INT`` casts, ``STRPTIME``, COALESCE arity differences —
                    # that survives identifier rewrite but BQ refuses), fall back
                    # to the original SQL via the legacy ATTACH-catalog path so
                    # the request still succeeds (slower, but correct). Same
                    # safety contract as the dry-run fallback in
                    # ``_bq_quota_and_cap_guard``. Bind ``policy_params`` (DuckDB
                    # named params) whenever the executed SQL carries a policy's
                    # ``$name`` markers — never string-interpolated (§6.2). This
                    # branch is reachable ONLY when NOT (did_rewrite and
                    # policied_table_ids) — i.e. either no table this query
                    # touches is policied, or the policied one isn't BQ-remote —
                    # so a retry on ``policy_rewritten_sql`` below never
                    # re-exposes the unfiltered original (§7.4).
                    if policied_table_ids:
                        # Read-path guard (§17): fail closed if a policy's output
                        # has duplicate column names (a masking policy that
                        # re-derives a column `*` still emits leaks the plaintext
                        # copy). Checks the policy body itself — the outer SELECT
                        # here would dedup the names and hide it.
                        assert_policied_reads_unique(analytics, policied_table_ids, user)
                    try:
                        if policy_params:
                            result = analytics.execute(execution_sql, policy_params).fetchmany(request.limit + 1)
                        else:
                            result = analytics.execute(execution_sql).fetchmany(request.limit + 1)
                    except Exception as exc:
                        if did_rewrite and _looks_like_bq_rewrite_parse_error(exc):
                            logger.warning(
                                "query_rewrite_fallback: user_id=%s — bigquery_query() "
                                "rewrite rejected by BQ (%s); retrying via "
                                "ATTACH-catalog path",
                                user_id,
                                type(exc).__name__,
                            )
                            # Retry on the policy-rewritten SQL, not the raw
                            # request.sql — this fallback re-enters the ATTACH-catalog
                            # path (same as the did_rewrite=False branch above), so
                            # it must stay policy-filtered too.
                            if policy_params:
                                result = analytics.execute(policy_rewritten_sql, policy_params).fetchmany(
                                    request.limit + 1
                                )
                            else:
                                result = analytics.execute(policy_rewritten_sql).fetchmany(request.limit + 1)
                        else:
                            raise
                    columns = [desc[0] for desc in analytics.description] if analytics.description else []
                    truncated = len(result) > request.limit
                    rows = result[: request.limit]

            # Post-flight: bill the dry-run estimate against the user's daily
            # quota. Do this AFTER execute so a downstream failure (e.g. BQ
            # outage) doesn't strand the user with charged-but-unrun bytes.
            # Stays inside the `with quota.acquire(...)` block so the slot
            # release happens after record_bytes completes.
            if dry_run_set:
                total_bq_bytes = sum(b for _, _, b in dry_run_set)
                try:
                    _build_quota_tracker().record_bytes(
                        user_id,
                        total_bq_bytes,
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
            serializable_rows.append(
                [str(v) if v is not None and not isinstance(v, (int, float, bool, str)) else v for v in row]
            )
        # bytes_scanned from _dry_run_set (pinned to entry 0 after _bq_quota_and_cap_guard).
        # Computed before building the response so it can be surfaced to
        # REST/CLI/MCP consumers; ``None`` for local queries (no BQ tables).
        #
        # For a Databricks statement the number means something different —
        # bytes the warehouse RETURNED, not bytes it scanned, because the
        # Statement Execution API exposes no scan accounting. Surfaced anyway
        # (an analyst asking "how much did that move?" is better served by the
        # honest available number than by `null`), and the field's per-engine
        # meaning is spelled out in docs/DATA_SOURCES.md.
        _bytes_scanned = sum(b for _, _, b in _dry_run_set) if _dry_run_set else _dbx_bytes
        # Task 11 (§10): policied_table_ids (from the rewrite_sql call above)
        # discloses here as response.row_scope -- None unless this query
        # actually touched a policied table (empty list otherwise, or the
        # admin bypass, per rewrite_sql's own contract).
        response = QueryResponse(
            columns=columns,
            rows=serializable_rows,
            row_count=len(serializable_rows),
            truncated=truncated,
            bytes_scanned=_bytes_scanned,
            row_scope=row_scope_payload(policied_table_ids),
        )
        # Determine action: remote when an external engine ran the statement
        # (BQ dry-run set non-empty, or a Databricks plan executed), local
        # otherwise. One audit action for both engines — the engine itself is
        # recoverable from the resource/table, and splitting it would break
        # every existing `query.remote` dashboard.
        _action = "query.remote" if (_dry_run_set or _dbx_plan is not None) else "query.local"
        _first_table = _first_table_from_sql(request.sql)
        _resource = (f"table:{_first_table}" if _first_table else "adhoc")[:256]
        try:
            audit_repo().log(
                user_id=_identity_for_audit(user)[0],
                action=_action,
                resource=_resource,
                params={
                    "sql_preview": (request.sql or "")[:200],
                    # bytes_scanned / bytes_billed / bq_job_id: only available for
                    # BQ-remote path. bytes_billed and bq_job_id are not yet surfaced
                    # by the DuckDB BQ extension execute() path — deferred TODO.
                    # bytes_scanned comes from the dry-run estimate (close approximation).
                    "bytes_scanned": _bytes_scanned,
                    "bytes_billed": None,  # deferred — BQ extension doesn't expose per-execute billing
                    "bq_job_id": None,  # deferred — bigquery_query() path doesn't return a job id
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
                user_id=_identity_for_audit(user)[0],
                action=_action_err,
                resource=_resource,
                params={
                    "sql_preview": (request.sql or "")[:200],
                    "error": str(exc.detail)[:200],
                    "duration_ms": int((time.monotonic() - _t0) * 1000),
                },
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
                user_id=_identity_for_audit(user)[0],
                action="query.local",
                resource=_resource,
                params={
                    "sql_preview": (request.sql or "")[:200],
                    "error": msg[:200],
                    "duration_ms": int((time.monotonic() - _t0) * 1000),
                },
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
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    error_msg: str,
) -> str | None:
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
        # Not executed — a copy-pasteable hint. Routed through quote_ident anyway
        # so the suggestion stays valid SQL when a name contains a quote, and
        # through normalize_source_table so a row registered by the pre-fix
        # wizard (full `<bucket>.<table>` in source_table) does not suggest
        # `kbc."in.c-main"."in.c-main.orders"` — a table that does not exist, so
        # the analyst who copies it gets an error instead of data
        # (Devin Review on #1189).
        from connectors.keboola.storage_api import normalize_source_table

        bare = normalize_source_table(bucket, source_table)
        direct_hint = f" or query the source directly via {alias}.{quote_ident(bucket)}.{quote_ident(bare)}"
    return (
        f"Table {tid!r} is registered as query_mode='materialized' but is "
        f"not yet materialized in this instance's analytics views. Run "
        f"`agnes pull` (or wait for the scheduler tick / hit POST "
        f"/api/sync/trigger) to materialize the parquet"
        f"{direct_hint}."
    )


def _bq_row_target(row: dict) -> tuple[str, str, str | None]:
    """Resolve a BQ registry row to ``(dataset, table, project_override)``.

    ``bq_fqn`` (v51, issue #343) pins a row's own ``project.dataset.table``
    and overrides all three legs of the legacy configured-project +
    ``bucket`` + ``source_table`` convention. ``bucket`` is a UX/RBAC label
    that need not equal the physical dataset. ``project_override`` is
    ``None`` for pre-v51 rows, meaning "use the configured data project".

    Malformed values degrade to the legacy triplet rather than raising:
    registration validates ``bq_fqn`` at the API boundary, so a bad value
    here means the row was written out-of-band, and one such row must not
    500 every query that merely mentions a sibling table.
    """
    from connectors.bigquery.extractor import parse_bq_fqn

    legacy = (row.get("bucket") or "", row.get("source_table") or "", None)
    raw = row.get("bq_fqn")
    if not raw:
        return legacy
    try:
        parsed = parse_bq_fqn(raw)
    except ValueError:
        logger.warning(
            "Ignoring malformed bq_fqn on registry row %r, falling back to "
            "the configured project. Re-register the row to fix.",
            row.get("id") or row.get("name"),
        )
        return legacy
    if parsed is None:
        return legacy
    project, dataset, table = parsed
    return dataset, table, project


def _bq_guardrail_inputs(
    sql: str,
    sql_lower: str,
    sys_conn: duckdb.DuckDBPyConnection,
    user: dict,
    allowed: list | None,
):
    """Two-pass scan over user SQL for the upcoming BQ guardrail + RBAC patch.

    Returns a tuple `(dry_run_set, name_lookups, blocked_bq_path)`:

    - `dry_run_set` is a list of `(bucket, source_table, est_bytes)` triples
      identifying every BigQuery row the request will scan. The caller dry-runs
      the rewritten user SQL once and distributes the total here for quota
      bookkeeping.

    - `name_lookups` is a list of
      `(registered_name, dataset, table, project_or_None)` tuples, only the
      bare-name matches from pass 1, NOT the direct `bq."<ds>"."<tbl>"`
      matches. Issue #171 fix: the cap-guard rewrites these name →
      ``\\`<project>.<dataset>.<table>\\``` when building the BQ-native SQL
      for dry-run, so partition pruning + column projection + predicate
      pushdown all engage. The dataset/table/project come from the row's
      `bq_fqn` when set (v51, issue #343) and from the configured project +
      `bucket` + `source_table` otherwise; a `None` project means "use the
      configured data project".

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
        # Issue #1322: a bare `\bname\b` match also fires on the keyword
        # half of ORDER BY / GROUP BY / PARTITION BY when a registered
        # table is named after one of those keywords (e.g. `order`), which
        # then corrupts the rewriter's substitution downstream. Reuse the
        # same compiled pattern the RBAC name guards use — it already
        # suppresses a name immediately followed by " by" via a negative
        # lookahead, since no real reference can occupy that position
        # (DuckDB/BQ both reject a bare `by` as an identifier).
        if _name_reference_re(str(name).lower()).search(sql_lower_masked):
            key = (bucket.lower(), source_table.lower())
            if key not in seen_paths:
                seen_paths.add(key)
                dry_run.append((bucket, source_table, 0))  # bytes filled at dry-run
            # Record the (name, dataset, table, project) mapping separately so
            # the cap-guard's SQL rewriter can find every occurrence, even if
            # the user references the same physical table under two registered
            # names (rare but possible: aliased catalog rows). The physical
            # target comes from ``bq_fqn`` when set, so a cross-project row is
            # dry-run against the table it actually reads. ``dry_run`` above
            # keeps the registry ``bucket``/``source_table``: those are
            # identity keys for the metadata cache, not a BQ path.
            ds, tbl, row_project = _bq_row_target(r)
            name_lookups.append((str(name), ds, tbl, row_project))

    # 2. Direct bq.<ds>.<tbl> pass: every match must point at a registered
    # row. Run BEFORE adding to dry_run so unregistered paths fail-fast.
    # A restricted principal (co-session / agent-session) is NEVER admin,
    # even when its owner is — resolving the owner's id here and calling
    # is_user_admin would reintroduce exactly the admin-inheritance bug the
    # AgentPrincipal design forbids (this is the "no admin short-circuit"
    # invariant, not an audit-identity question — do not route this through
    # _identity_for_audit). Mirrors the same PRINCIPAL_TYPES guard in
    # _run_internal_query / v2_sample.py.
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        is_admin = False
    else:
        # v106: the admin bypass below skips the per-id grant check, so it
        # must honor the credential's data-read surface exactly like
        # get_accessible_tables/can_access_table do — otherwise an admin on
        # a surface='stack' PAT could read out-of-stack tables via a direct
        # bq.* / full-backtick path while the bare-name pass (which uses
        # `accessible_set`) correctly stack-scopes them. A stack-surface
        # admin falls through to the grant check against `accessible_set`,
        # which for them is the concrete stack-derived id set.
        from src.rbac import _credential_surface

        is_admin = (
            is_user_admin(user.get("id") or user.get("email") or "", sys_conn) and _credential_surface(user) == "all"
        )
    for m in BQ_PATH.finditer(sql):
        bucket_raw = m.group(1).strip('"')
        source_table_raw = m.group(2).strip('"')
        row = repo.find_by_bq_path(bucket_raw, source_table_raw)
        if row is None:
            return (
                [],
                [],
                {
                    "reason": "bq_path_not_registered",
                    "path": f"bq.{quote_ident(bucket_raw)}.{quote_ident(source_table_raw)}",
                    "hint": (
                        "Direct bq.* references must point to a registered table. "
                        "Register via `agnes admin register-table` or use the "
                        "registered name from `agnes catalog`."
                    ),
                },
            )
        # Row exists. Per-id grant check (non-admin only).
        # `accessible_set` is keyed by registry id (resource_grants
        # resource_id), so use `row["id"]` here, not display name.
        # Devin Review iter #3.
        if not is_admin:
            if accessible_set is None or row["id"] not in accessible_set:
                return (
                    [],
                    [],
                    {
                        "reason": "bq_path_access_denied",
                        "path": f"bq.{quote_ident(bucket_raw)}.{quote_ident(source_table_raw)}",
                        "registered_as": row["name"],
                    },
                )
            policied = _policied_row_over_physical_source(
                repo,
                source_type="bigquery",
                bucket=bucket_raw,
                source_table=source_table_raw,
            )
            if policied is not None:
                return (
                    [],
                    [],
                    {
                        "reason": "bq_path_policied",
                        "path": f"bq.{quote_ident(bucket_raw)}.{quote_ident(source_table_raw)}",
                        "registered_as": policied["name"],
                        "hint": (
                            "This BigQuery table carries an access policy, which is "
                            f"enforced under its registered name. Query {policied['name']!r} "
                            "instead of the direct bq.* path."
                        ),
                    },
                )
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
                return (
                    [],
                    [],
                    {
                        "reason": "bq_path_cross_project",
                        "path": f"`{proj}.{ds}.{tbl}`",
                        "expected_project": data_project,
                        "hint": (
                            "--remote queries can only reference tables in the "
                            "configured BigQuery data project. Register "
                            "cross-project tables via `agnes admin "
                            "register-table` if needed."
                        ),
                    },
                )
            row = repo.find_by_bq_path(ds, tbl)
            if row is None:
                return (
                    [],
                    [],
                    {
                        "reason": "bq_path_not_registered",
                        "path": f"`{proj}.{ds}.{tbl}`",
                        "hint": (
                            "Direct BigQuery paths must point to a registered "
                            "table. Register via `agnes admin register-table` "
                            "or use the registered name from `agnes catalog`."
                        ),
                    },
                )
            if not is_admin:
                if accessible_set is None or row["id"] not in accessible_set:
                    return (
                        [],
                        [],
                        {
                            "reason": "bq_path_access_denied",
                            "path": f"`{proj}.{ds}.{tbl}`",
                            "registered_as": row["name"],
                        },
                    )
                policied = _policied_row_over_physical_source(
                    repo,
                    source_type="bigquery",
                    bucket=ds,
                    source_table=tbl,
                )
                if policied is not None:
                    return (
                        [],
                        [],
                        {
                            "reason": "bq_path_policied",
                            "path": f"`{proj}.{ds}.{tbl}`",
                            "registered_as": policied["name"],
                            "hint": (
                                "This BigQuery table carries an access policy, which is "
                                f"enforced under its registered name. Query {policied['name']!r} "
                                "instead of the direct path."
                            ),
                        },
                    )
            bucket = row["bucket"]
            source_table = row["source_table"]
            if bucket and source_table:
                key = (bucket.lower(), source_table.lower())
                if key not in seen_paths:
                    seen_paths.add(key)
                    dry_run.append((bucket, source_table, 0))

    return dry_run, name_lookups, None


# ---------------------------------------------------------------------------
# Databricks remote execution (phase 2)
# ---------------------------------------------------------------------------


def _databricks_remote_cap_bytes() -> int:
    """Cap on the RESULT bytes an interactive Databricks statement may return.

    Deliberately a different number — and a different meaning — from BigQuery's
    ``bq_max_scan_bytes``. BigQuery's cap is on bytes *scanned*, checked before
    the query runs, because a dry-run can price it. Databricks has no dry-run,
    so this caps what the warehouse is allowed to hand back (the API's
    ``byte_limit``) and the statement is refused if it hits the cap. 1 GiB of
    *returned* result is already far past what an interactive answer needs;
    bulk volume belongs in a materialized row.
    """
    raw = get_value("data_source", "databricks", "max_bytes_per_remote_query", default=1_073_741_824)
    try:
        return int(raw) if raw is not None else 1_073_741_824
    except (TypeError, ValueError):
        return 1_073_741_824


def _databricks_statement_timeout_s() -> float:
    """Client-side deadline for an interactive statement.

    Shorter than the materialize path's ``statement_timeout_seconds`` because a
    human is waiting on this one: past a couple of minutes the right answer is
    "register it as materialized", not a longer spinner.
    """
    raw = get_value("data_source", "databricks", "remote_query_timeout_seconds", default=120)
    try:
        t = float(raw) if raw is not None else 120.0
    except (TypeError, ValueError):
        return 120.0
    return t if t > 0 else 120.0


def _caller_is_unrestricted_admin(user, sys_conn) -> bool:
    """Admin *for the purposes of a direct-path bypass* — never inherited.

    A restricted principal (co-session / agent-session) is NEVER admin, even
    when its owner is: resolving the owner and asking ``is_user_admin`` would
    reintroduce exactly the admin-inheritance the AgentPrincipal design
    forbids. And a full admin on a ``surface='stack'`` PAT is not admin here
    either (v106) — otherwise they could reach an out-of-stack table through a
    direct path while the bare-name pass correctly stack-scopes them.

    Extracted from ``_bq_guardrail_inputs``'s inline logic so the Databricks
    gate cannot drift from the BigQuery one.
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        return False
    from src.rbac import _credential_surface

    return bool(
        is_user_admin(user.get("id") or user.get("email") or "", sys_conn) and _credential_surface(user) == "all"
    )


def _policied_row_over_physical_source(
    repo,
    *,
    source_type: str,
    bucket: str,
    source_table: str,
):
    """The registry row carrying an access policy over this physical
    source, if any — the reason an engine-qualified path must be refused.

    ``rewrite_sql`` substitutes policied tables by registry NAME (§5.2), and
    an ``sf."SCHEMA"."TABLE"`` / ``bq."ds"."tbl"`` reference names the
    PHYSICAL source instead, so the rewrite never fires for it and the
    policy simply does not apply. Each engine's gate below already proves
    the path is registered and that the caller holds a grant on the row it
    resolved to — neither of which says anything about a policy, and the
    row it resolves to need not even be the policied one when a source is
    registered twice. Fail closed and send the caller to the registered
    name, where enforcement lives.

    Scans ``list_by_source`` (both backends implement it) rather than
    adding a repository lookup, matching the existing ``sf.*`` gate's own
    scan; the registry is bounded by an instance's table count.

    Matches on ``(bucket, source_table)`` only, which is what both gates
    already resolve a path with. A BigQuery row registered with ONLY
    ``bq_fqn`` and no bucket/source_table is invisible here — but also to
    ``find_by_bq_path``, so such a path is refused one step earlier as
    unregistered. The uncovered shape is a policied ``bq_fqn``-only row
    beside an unpolicied bucket/source_table row for the same table; that
    pair already escapes ``_policy_physical_source_signals`` (the two
    signals never intersect), so closing it belongs there, not here.
    """
    bucket_l = (bucket or "").lower()
    table_l = (source_table or "").lower()
    if not bucket_l or not table_l:
        return None
    for row in repo.list_by_source(source_type):
        if not row.get("access_policy_sql"):
            continue
        if (row.get("bucket") or "").lower() == bucket_l and (row.get("source_table") or "").lower() == table_l:
            return row
    return None


def _sf_guardrail_inputs(sql: str, sql_lower: str, sys_conn, user, allowed) -> Optional[dict]:
    """Registry + RBAC gate for direct ``sf."schema"."table"`` paths.

    Snowflake is a DuckDB community extension, so ``sf.*`` resolves locally,
    but a qualified path bypasses the master-view RBAC layer. The same
    registration/admin/RBAC rules as BigQuery's ``bq.*`` guard apply here.
    Returns ``None`` when the statement contains no ``sf.*`` path or every path
    is registered and accessible.
    """
    from src.repositories import table_registry_repo

    repo = table_registry_repo()
    is_admin = _caller_is_unrestricted_admin(user, sys_conn)
    accessible_set = set(allowed) if allowed is not None else None

    for m in SF_PATH.finditer(sql):
        schema_raw = m.group(1).strip('"')
        table_raw = m.group(2).strip('"')
        row = None
        for r in repo.list_by_source("snowflake"):
            if (r.get("bucket") or "").lower() == schema_raw.lower() and (
                r.get("source_table") or ""
            ).lower() == table_raw.lower():
                row = r
                break
        if row is None:
            return {
                "reason": "sf_path_not_registered",
                "path": f"sf.{quote_ident(schema_raw)}.{quote_ident(table_raw)}",
                "hint": (
                    "Direct Snowflake paths must point to a registered table. "
                    "Register via `agnes admin register-table` or use the registered name from `agnes catalog`."
                ),
            }
        if not is_admin:
            if accessible_set is None or row["id"] not in accessible_set:
                return {
                    "reason": "sf_path_access_denied",
                    "path": f"sf.{quote_ident(schema_raw)}.{quote_ident(table_raw)}",
                    "registered_as": row["name"],
                }
            policied = _policied_row_over_physical_source(
                repo,
                source_type="snowflake",
                bucket=schema_raw,
                source_table=table_raw,
            )
            if policied is not None:
                return {
                    "reason": "sf_path_policied",
                    "path": f"sf.{quote_ident(schema_raw)}.{quote_ident(table_raw)}",
                    "registered_as": policied["name"],
                    "hint": (
                        "This Snowflake table carries an access policy, which is "
                        f"enforced under its registered name. Query {policied['name']!r} "
                        "instead of the direct sf.* path."
                    ),
                }
    return None


def _databricks_attach_views_available(sql_lower: str) -> bool:
    """True when DuckDB can resolve this statement's Databricks tables itself.

    That is only the case on an instance where the operator opted into the
    experimental Unity Catalog ATTACH: the orchestrator then holds a master
    view per ``query_mode='remote'`` Databricks row and the ordinary local
    execution path can join it against local parquets. Asking the catalog is
    the honest test — the config flag says what was *intended*, the view says
    what actually got built (the extension may have failed to install, the
    ATTACH may have been refused by the host allowlist).
    """
    try:
        from src.repositories import table_registry_repo

        rows = [
            r
            for r in table_registry_repo().list_by_source("databricks")
            if (r.get("query_mode") or "") == "remote" and r.get("name")
        ]
        masked = mask_backticks(sql_lower)
        referenced = [str(r["name"]) for r in rows if _name_reference_re(str(r["name"]).lower()).search(masked)]
        if not referenced:
            return False
        analytics = get_analytics_db_readonly()
        views = {
            row[0].lower()
            for row in analytics.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
            ).fetchall()
        }
        return all(n.lower() in views for n in referenced)
    except Exception:
        # No catalog, no ATTACH — the caller's refusal branch is the safe answer.
        logger.debug("databricks attach-view probe failed; treating as unavailable", exc_info=True)
        return False


def _databricks_remote_plan(sql: str, sql_lower: str, sys_conn, user, allowed):
    """Decide whether this statement runs on a Databricks warehouse, and how.

    Returns a plan dict (``{"sql": <warehouse-native SQL>, "settings": …}``) or
    ``None`` when no Databricks remote row is referenced — in which case the
    caller proceeds down the unchanged BigQuery / local path.

    Raises ``HTTPException`` for the three refusals an analyst can trigger:
    mixing two remote engines in one statement (400), naming an unregistered or
    un-granted Databricks table (403), and Databricks not being configured on
    this instance at all (503).
    """
    from src.remote_engines import (
        CrossEngineError,
        referenced_remote_rows,
        references_non_engine_tables,
        resolve_single_engine,
    )

    try:
        engine = resolve_single_engine(referenced_remote_rows(sql, sql_lower))
    except CrossEngineError as exc:
        raise HTTPException(status_code=400, detail=exc.detail())
    if engine != "databricks":
        return None

    # Cross-source bail. The whole statement ships to the warehouse, so a
    # reference to anything the warehouse cannot see — a local parquet, a
    # materialized one, a Jira table — would resolve against nothing there.
    # BigQuery answers this by falling back to DuckDB's ATTACH-catalog path;
    # Databricks has no such fallback unless the operator opted into the
    # experimental Unity Catalog ATTACH (see `docs/DATA_SOURCES.md`), in which
    # case DuckDB already holds a view for the row and returning None here
    # lets the local path do the join. Otherwise: refuse, and name the tables.
    foreign = references_non_engine_tables(sql_lower, "databricks")
    if foreign:
        if _databricks_attach_views_available(sql_lower):
            return None
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "remote_cross_source_unsupported",
                "engine": "databricks",
                "tables": foreign,
                "message": (
                    "This query joins a remote Databricks table with data that only exists "
                    f"on this server ({', '.join(foreign)}). The statement runs entirely on "
                    "the warehouse, which cannot see it."
                ),
                "hint": (
                    "Register the Databricks side as a query_mode='materialized' table so it "
                    "syncs to a parquet, then join locally with `agnes query`."
                ),
            },
        )

    from connectors.databricks.remote import DatabricksRemoteError, guardrail_inputs, rewrite_to_native
    from connectors.databricks.semantic_layer import resolve_databricks_settings

    settings = resolve_databricks_settings()
    if settings is None:
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "databricks_not_configured",
                "message": (
                    "This query references a Databricks table but the instance has no Databricks connection configured."
                ),
                "hint": (
                    "Set data_source.databricks.host + warehouse_id (instance.yaml or "
                    "/admin/server-config) and the DATABRICKS_TOKEN env var / vault secret."
                ),
            },
        )

    default_catalog = str(settings.get("catalog") or "")
    try:
        name_lookups, blocked = guardrail_inputs(
            sql,
            sql_lower,
            allowed=allowed,
            is_admin=_caller_is_unrestricted_admin(user, sys_conn),
            default_catalog=default_catalog,
        )
        if blocked is not None:
            # "I can't parse this" is a bad request; everything else the gate
            # returns is an authorization answer.
            status = 400 if blocked.get("reason") == "databricks_sql_unparseable" else 403
            raise HTTPException(status_code=status, detail=blocked)
        native_sql = rewrite_to_native(sql, name_lookups, default_catalog)
    except DatabricksRemoteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail())

    return {
        "sql": native_sql,
        "settings": settings,
        "tables": [n for n, _c, _s, _t in name_lookups],
        "name_lookups": name_lookups,
        "default_catalog": default_catalog,
        "parameters": [],
    }


class _PolicyResolutionFailed(Exception):
    """A policy resolver failed for a reason that is NOT "unregistered name".

    Deliberately not a ``PolicyError`` subclass. ``rewrite_sql``'s resolution
    loop swallows ``PolicyError`` on purpose — that exception doubles as the
    registry's "no such table" signal, and swallowing it is what keeps every
    query naming a CTE or an ``information_schema`` view working. But an
    engine-side failure (a transpile error, a pattern-metacharacter group name,
    a parameter-binding failure) raises the SAME type, and being swallowed
    there makes a policied table look unpolicied. This type is invisible to
    that ``except``, so a genuine resolution failure propagates instead.
    """

    def __init__(self, table_id: str) -> None:
        self.table_id = table_id
        super().__init__(f"policy resolution failed for {table_id!r}")


def _table_is_registered(name_or_id: str) -> bool:
    """Does the registry know this name? Distinguishes the two things
    ``policied_relation`` reports with the same ``PolicyError``: an unknown
    name (a CTE, an ``information_schema`` view — must stay swallowed) from a
    genuine resolution failure on a table that does exist."""
    repo = table_registry_repo()
    if repo.get(name_or_id):
        return True
    getter = getattr(repo, "get_by_name", None)
    return bool(getter(name_or_id)) if getter else False


def _assert_policy_substitution_complete(expected_ids, actual_ids) -> None:
    """Every table the first policy pass flagged must still be policied after
    the engine-specific second pass (§17 — every failure denies).

    Both remote arms run ``rewrite_sql`` twice: once with the default DuckDB
    resolver to decide whether a policy is in play at all, then again with an
    engine resolver that transpiles and binds. Only the second can fail on
    engine-specific work, and its failure mode is silent: ``rewrite_sql``
    swallows ``PolicyError`` from ``resolve``, so the table drops out of
    ``policied_table_ids``, the substitution never happens, and what executes
    is the caller's own unfiltered statement — returned with a 200.

    That is the worst shape a policy bug can take, so it gets a positive
    invariant rather than trust in the failure paths: compare the two passes
    and deny on any table the second one lost.
    """
    actual = set(actual_ids or [])
    missing = [t for t in (expected_ids or []) if t not in actual]
    if missing:
        raise PolicyError(missing[0])


def _assert_databricks_policy_columns_unique(plan: dict, columns) -> None:
    """Fail closed when a POLICIED Databricks read produced duplicate column names.

    §17's masking rule: a policy written ``SELECT * EXCEPT (national_id),
    md5(email) AS email`` leaves ``email`` out of the EXCEPT list, so the star
    still emits the plaintext column and the re-derived one appends a second
    with the same name. Whoever reads ``row["email"]`` may get the plaintext.

    Each engine closes this differently, and Databricks is the one that closes
    it nowhere by default. DuckDB reads get ``assert_policied_reads_unique``,
    which DESCRIBEs the policy body up front. BigQuery needs no guard at all —
    its jobs API rejects a result with duplicate column names, so a leaky
    policy simply fails. Spark/Databricks permits duplicates, and the save-time
    probe cannot cover a `query_mode='remote'` row either: with no local master
    view, `probe_policy` takes its "nothing to check yet" early return.

    So the check happens on the columns the warehouse actually returned. That
    is after execution — the rows are already in this process — but nothing
    reaches the caller, which is what fail-closed means here. Checking earlier
    would cost a `LIMIT 0` probe round-trip on every policied query to defend
    against a policy shape the save-time validator already rejects everywhere
    it can see the schema.
    """
    policied = plan.get("policied_table_ids") or []
    if not policied:
        return
    assert_unique_output_columns(list(columns or []), policied[0])


def _policy_parse_dialect(sql: str, sql_lower: str) -> str:
    """Which SQL dialect the policy machinery should parse this statement in.

    `rewrite_sql`'s FIRST call decides whether a policied table is touched at
    all, and it happens before any engine planning — so without this it parsed
    every statement as DuckDB. On a Databricks statement that is not a
    cosmetic mismatch: sqlglot's DuckDB parser rejects backtick-quoted
    identifiers outright, so `SELECT * FROM \\`main\\`.\\`sales\\`.\\`orders\\`` — the
    shape you get by copying a query out of the Databricks UI, which this
    connector explicitly supports — fails to parse, and
    `_scan_unparseable_for_policied_table` then denies the whole query with
    `policy_error` the moment it mentions a policied table. `MEASURE()` is the
    same story. That is exactly the deny this feature set out to remove.

    Engine detection is a registry + text scan (`referenced_remote_rows` masks
    backticks and matches names), never a parse, so it is safe to run this
    early. A cross-engine statement resolves to no single engine here; the
    planner reports that properly a few lines later, so this just falls back
    to the historical default rather than raising twice.

    Mentioning a Databricks table is NOT sufficient. With the experimental
    Unity Catalog ATTACH on, a statement that also references local data is
    planned as an ordinary DuckDB query (the planner declines, and DuckDB has a
    view for the remote row) — rendering the caller's SQL through sqlglot's
    Databricks generator and then executing it on DuckDB would change its
    meaning, and DuckDB-only syntax such as ``SELECT * EXCLUDE (col)`` would
    fail the Databricks parse and deny. So the answer is "databricks" only for
    a statement that will actually run on the warehouse: single-engine, and
    naming nothing the warehouse cannot see.
    """
    from src.remote_engines import (
        CrossEngineError,
        referenced_remote_rows,
        references_non_engine_tables,
        resolve_single_engine,
    )

    try:
        engine = resolve_single_engine(referenced_remote_rows(sql, sql_lower))
    except CrossEngineError:
        return "duckdb"
    if engine != "databricks":
        return "duckdb"
    if references_non_engine_tables(sql_lower, "databricks"):
        # Either the planner refuses this outright (ATTACH off) or it falls
        # through to local execution (ATTACH on). Neither runs on the
        # warehouse, so neither wants the warehouse's dialect.
        return "duckdb"
    return "databricks"


def _databricks_policy_resolver(*, name_lookups, default_catalog: str):
    """A ``rewrite_sql`` ``resolve`` callable that returns Databricks-dialect
    policy bodies, native-path-rewritten, with the array-valued variable
    already expanded into scalar markers.

    Doing the expansion HERE, per policy body, rather than over the finished
    statement is deliberate. ``bind_policy_parameters`` has to parse and
    re-render whatever it is handed; handing it the whole caller statement
    would round-trip the analyst's SQL through sqlglot a second time for no
    reason. A policy body is small, admin-authored and already validated, and
    the BigQuery arm likewise round-trips only the body (via its transpile).

    The returned relation's ``params`` are the *generated scalar* parameters,
    so ``rewrite_sql``'s own param merge produces a flat ``name -> value`` dict
    with no list left in it — which is precisely what the Statement Execution
    API can bind.
    """
    from connectors.databricks.policy_params import (
        DatabricksPolicyBindingError,
        bind_policy_parameters,
    )
    from connectors.databricks.remote import (
        DatabricksPolicyRewriteError,
        rewrite_policy_body_to_native,
        row_target,
    )

    def _body_lookups(policied_id: str):
        """Lookups for rewriting ONE policy body: the caller's, plus the
        policied row's own target resolved from the registry rather than from
        whatever the caller happened to type."""
        row = table_registry_repo().get(policied_id)
        if not row:
            return list(name_lookups)
        name = str(row.get("name") or "")
        if not name:
            return list(name_lookups)
        catalog, schema, table = row_target(row, default_catalog)
        others = [e for e in name_lookups if e[0].lower() != name.lower()]
        return [(name, catalog, schema, table), *others]

    def resolve(table_id: str, principal):
        try:
            relation = policied_relation(table_id, principal, dialect="databricks")
        except PolicyError as exc:
            # `policied_relation` raises PolicyError for BOTH "no such
            # registered table" (which must stay swallowed, or every CTE name
            # breaks) and genuine resolution failures — a Databricks transpile
            # error, a group name carrying a LIKE metacharacter. Only the
            # second kind can happen after the DuckDB pass already resolved
            # this name successfully, so re-raise as the non-swallowed type.
            if _table_is_registered(table_id):
                raise _PolicyResolutionFailed(exc.table_id) from exc
            raise
        if not relation.policied:
            return relation
        try:
            body_sql, parameters = bind_policy_parameters(relation.relation_sql, relation.params)
        except DatabricksPolicyBindingError as exc:
            # NOT PolicyError: `rewrite_sql` swallows that, which would drop
            # the policy and execute the caller's unfiltered statement.
            raise _PolicyResolutionFailed(relation.table_id) from exc
        try:
            # `_body_lookups` and not the caller-derived `name_lookups`: the
            # policied row's own path has to come from the ROW, because
            # `guardrail_inputs` records a lookup only for a registered name
            # the caller wrote BARE (it masks backticks first). A caller who
            # writes the fully-qualified ``main`.`sales`.`orders_raw`` — a
            # spelling this connector documents and tests — produces no entry
            # at all, so the policy body's own `FROM orders_raw` stayed bare
            # and shipped unqualified, to resolve against whatever the
            # warehouse's default context holds.
            #
            # The AST rewriter and not `rewrite_to_native`: the textual one
            # replaces every occurrence of the name, and in a policy body the
            # policied name is also what qualifies its own columns, so
            # `WHERE orders_raw.country = …` became a four-part column
            # reference. The outer pass avoids this by excluding the policied
            # name; the body cannot, because the body is where that table must
            # actually be rewritten.
            native_body = rewrite_policy_body_to_native(body_sql, _body_lookups(relation.table_id), default_catalog)
        except DatabricksPolicyRewriteError as exc:
            # Same reasoning as the binding failure above: anything other than
            # the non-swallowed type would execute the caller's SQL unfiltered.
            raise _PolicyResolutionFailed(relation.table_id) from exc
        return dataclasses.replace(
            relation,
            relation_sql=native_body,
            # The WHOLE API entry is the value, not just its ``value`` field.
            # `bind_policy_parameters` omits ``value`` entirely for a NULL bind
            # (that is how the Statement Execution API binds SQL NULL, and it
            # is the fail-closed choice), so flattening to ``{name: value}``
            # both raised ``KeyError`` on such an entry and, had it used
            # ``.get()``, would have re-emitted it as JSON ``null`` instead of
            # omitting the field — losing the NULL-vs-empty-string distinction
            # the binding module exists to preserve. `rewrite_sql` only
            # ``update()``s this dict, so the value type is its own business.
            params={p["name"]: p for p in parameters},
        )

    return resolve


def _apply_databricks_policies(plan: dict, sql: str, principal, *, expected_ids=None) -> list[str]:
    """Rewrite ``plan`` in place so a policied table is read through its
    policy, and return the policied registry ids.

    Like the BigQuery arm (§7.3) the policy substitution runs over the
    caller's own BARE table names — a prior name-to-native pass would leave the
    AST rewrite nothing to match, since it turns the caller's reference into a
    backtick path sqlglot no longer recognises as the same table.

    Where this diverges from BigQuery is *which* text gets the native rewrite,
    and it has to: the policy body is rewritten inside the resolver, before
    splicing, and the spliced statement is then rewritten with the policied
    table's own name excluded. See the comment below for why one wholesale
    pass over the result does not work here.

    The gate then runs a SECOND time, on the substituted statement. The first
    pass only saw the caller's SQL; the policy body can name tables the caller
    never wrote (a ``policy_mapping`` join, §15). Without this pass such a name
    would ship to the warehouse unchecked and resolve against whatever the
    default catalog holds. Re-gating makes an unregistered table inside a
    policy body deny, rather than silently read something.
    """
    from connectors.databricks.remote import DatabricksRemoteError, guardrail_inputs, rewrite_to_native

    default_catalog = plan["default_catalog"]
    name_lookups = plan["name_lookups"]

    # The policy body is rewritten to native paths BEFORE it is spliced, and
    # the spliced statement is then rewritten with the policied table's own
    # name excluded. Both halves matter:
    #
    #   `rewrite_sql` aliases the substituted subquery with the table's own
    #   name (its rule 2, so a caller's `orders_raw.country` qualifier still
    #   resolves), and the name rewriter replaces EVERY occurrence of a
    #   registered name outside backticks. Rewriting the spliced statement
    #   wholesale therefore rewrites the alias too, producing
    #   `... ) AS `main`.`sales`.`orders_raw`` — a three-part path in alias
    #   position, which is a syntax error, caught by
    #   TestAccessPolicyInterlock rather than by review.
    #
    # After the exclusion the policied name survives only where it should: as
    # the subquery's alias and as column qualifiers pointing at it.
    try:
        resolve = _databricks_policy_resolver(name_lookups=name_lookups, default_catalog=default_catalog)
        spliced_sql, policy_params, policied_table_ids = rewrite_sql(
            sql,
            principal,
            resolve=resolve,
            dialect="databricks",
        )
        # Deny if the engine pass lost a table the DuckDB pass had flagged —
        # the silent-drop shape `_assert_policy_substitution_complete` exists
        # for. Checked BEFORE the early return, because that return is exactly
        # what would leave `plan["sql"]` unfiltered.
        _assert_policy_substitution_complete(expected_ids, policied_table_ids)
        if not policied_table_ids:
            return []

        policied_names = set()
        for tid in policied_table_ids:
            name = str((table_registry_repo().get(tid) or {}).get("name") or "").strip()
            if not name:
                # The id came out of `rewrite_sql`, which resolved it against
                # this same registry, so an unresolvable name here means the
                # row moved underneath us. Deny: the alternative is an empty
                # exclusion set, which silently reintroduces the alias-rewrite
                # bug this exclusion exists to prevent.
                raise PolicyError(tid)
            policied_names.add(name.lower())

        # Gate the SPLICED statement, and derive the outer rewrite's lookups
        # from it rather than from the caller's SQL. A policy body may name a
        # table the caller never wrote — §15's `policy_mapping` join — and
        # `plan["name_lookups"]` was built by scanning the CALLER's statement,
        # so such a name would never be rewritten to a native path and would
        # ship bare, resolving against whatever the warehouse's default context
        # holds.
        #
        # `allowed=None` is what narrows THIS pass to the question that
        # actually applies to a policy body — is every table it names
        # REGISTERED, so Agnes can resolve it and knows it was intended? — and
        # `is_admin=True` alone would NOT have done it: the gate honours the
        # admin bypass only for a fully-qualified path, on the reasoning that a
        # full-surface admin arrives with `accessible is None` anyway. A bare
        # `region_map` inside a policy body would still have been grant-checked
        # against the caller and denied.
        #
        # Skipping the grant check here is not a hole. The caller's own
        # authorization was enforced by the planner's gate over the caller's
        # SQL, before any substitution. What is left is the tables only the
        # POLICY names — §15's `policy_mapping` idiom — and requiring the
        # caller to hold a grant on those would defeat the idiom's whole
        # purpose: the admin picks the mapping table precisely so the analyst
        # need not be able to read it directly.
        body_lookups, blocked = guardrail_inputs(
            spliced_sql,
            spliced_sql.lower(),
            allowed=None,
            is_admin=True,
            default_catalog=default_catalog,
        )
        if blocked is not None:
            # §17: a policied read that cannot be fully verified denies.
            # Reporting the gate's own reason to the CALLER would name tables
            # from inside the policy body (§16), so the response collapses to
            # the table-scoped error — but the admin who wrote the policy still
            # has to be able to find out why, and the two most likely causes
            # (an unregistered name, or a mapping table registered `local` /
            # `materialized`, which this gate cannot see because a warehouse
            # cannot read a local parquet) are indistinguishable from the
            # response alone. Log the real reason server-side.
            logger.warning(
                "databricks policy gate refused the substituted statement for %s: %s",
                policied_table_ids[0],
                blocked,
            )
            raise PolicyError(policied_table_ids[0])

        outer_lookups = [entry for entry in body_lookups if entry[0].lower() not in policied_names]
        native_sql = rewrite_to_native(spliced_sql, outer_lookups, default_catalog)
    except _PolicyResolutionFailed as exc:
        raise PolicyError(exc.table_id) from exc
    except DatabricksRemoteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail())

    plan["sql"] = native_sql
    # The entries are already in the API's shape (see `_databricks_policy_resolver`);
    # rebuilding them from name+value would drop the omitted-`value` NULL form.
    plan["parameters"] = list(policy_params.values())
    plan["policied_table_ids"] = list(policied_table_ids)
    return policied_table_ids


def _execute_databricks_plan(plan: dict, limit: int, user_id: str):
    """Run a planned Databricks statement; return ``(columns, rows, truncated, bytes)``.

    Holds the caller's concurrent-scan slot for the duration. That slot is the
    one piece of the quota machinery that transfers cleanly across engines —
    it limits how many expensive external queries one user can have in flight,
    which is exactly as true of a SQL warehouse as of BigQuery.

    The *daily byte budget* deliberately does NOT apply: it is denominated in
    BigQuery scanned bytes, and Databricks reports returned bytes on a
    different engine with different pricing. Charging one against the other
    would either block a Databricks query because of BigQuery spend or quietly
    inflate the BigQuery budget with numbers that do not mean the same thing.
    Databricks cost is bounded by ``max_bytes_per_remote_query`` +
    ``remote_query_timeout_seconds`` instead.

    When the statement touches a policied table, ``_apply_databricks_policies``
    has already rewritten ``plan["sql"]`` to read through the policy body and
    filled ``plan["parameters"]`` with the identity values it binds. Those
    travel as Statement Execution API request parameters, never as spliced SQL
    text — the same guarantee the DuckDB and BigQuery arms give (§6.2).
    """
    from connectors.databricks.remote import DatabricksRemoteError, execute_select

    quota = _build_quota_tracker()
    try:
        with quota.acquire(user=user_id):
            result = execute_select(
                plan["sql"],
                settings=plan["settings"],
                limit=limit,
                cap_bytes=_databricks_remote_cap_bytes(),
                timeout_s=_databricks_statement_timeout_s(),
                parameters=plan.get("parameters") or None,
            )
            _assert_databricks_policy_columns_unique(plan, result[0])
            return result
    except PolicyError as exc:
        # §16: a policy failure names the table and nothing else — never the
        # engine's own message, which can quote literals out of the policy body.
        raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "reason": "concurrent_scans_exceeded",
                "kind": exc.kind,
                "current": exc.current,
                "limit": exc.limit,
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )
    except DatabricksRemoteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail())


# `_assert_no_policied_remote_engine` lived here and refused any policied read
# bound for Databricks, because only BigQuery had a path that carried the
# policy across the engine boundary. Databricks has one now
# (`_apply_databricks_policies`), so both registered remote engines enforce
# rather than refuse and the helper had no caller left. A future engine does
# not get to inherit a generic refusal: it has to answer the same question
# these two did — how does the policy travel, and how do its values bind
# without being spliced into SQL text.


# The reserved-keyword set and the table-reference-position regex moved to
# `src/remote_engines.py` when Databricks gained a rewriter of its own — both
# engines must agree on which bare names are safe to substitute where, and a
# second copy would have drifted the first time one engine's keyword list grew.
_SQL_RESERVED_NAMES = SQL_RESERVED_NAMES
_TABLE_REF_PREFIX_RE = TABLE_REF_PREFIX_RE


def _rewrite_bq_table_refs_to_native(
    sql: str,
    name_lookups: list,
    project: str,
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

    TODO(durable fix): the residual cases above — string literals, CTE
    shadowing, and a non-keyword name that collides with a *column* name
    (`SELECT revenue FROM revenue` rewrites all three occurrences) — all
    come from the same root cause: this is a regex over unparsed SQL, so it
    cannot tell an identifier's role apart. `_sql_referenced_names` already
    asks DuckDB's own parser (`json_serialize_sql`) which names are
    `BASE_TABLE` references and is the right oracle here too, but it hands
    back a *set of names*, not source positions, so position-accurate
    rewriting needs more than a call swap: either a placeholder
    substitution round-tripped through `json_deserialize_sql` (which
    reformats the statement and drops comments) or a real tokenizer. It
    also declines on backtick-quoted BQ paths — first-class input on this
    path — so the regex must survive as the fallback either way. Left as a
    design task rather than smuggled into the keyword fix below.
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
        # Entries are ``(name, bucket, source_table)`` or, when the registry
        # row carries a ``bq_fqn`` (v51, issue #343), the 4-tuple
        # ``(name, dataset, table, project)`` whose project overrides the
        # single configured one for that row only. A ``None`` override and
        # the legacy 3-tuple both fall back to ``project``.
        name_to_target: dict[str, str] = {}
        for entry in name_lookups:
            name, bucket, source_table = entry[0], entry[1], entry[2]
            row_project = entry[3] if len(entry) > 3 else None
            target_project = row_project or project
            name_to_target[name.lower()] = f"`{target_project}.{bucket}.{source_table}`"

        # The substitution itself — single-pass alternation, outside-backtick
        # only, keyword-named entries anchored to a table-reference position —
        # is `rewrite_bare_names` in `src/remote_engines.py`. It moved there
        # when Databricks needed the identical rewrite against a different
        # target syntax; every hazard it guards against (and the issue that
        # found each one) is documented at the definition.
        out = rewrite_bare_names(out, name_to_target)

    # Pass 2: bq."ds"."tbl" / bq.ds.tbl → `<project>.<ds>.<tbl>`.
    def _bq_path_repl(m: re.Match) -> str:
        ds = m.group(1).strip('"')
        tbl = m.group(2).strip('"')
        return f"`{project}.{ds}.{tbl}`"

    out = BQ_PATH.sub(_bq_path_repl, out)
    return out


def _rewrite_user_sql_for_bq_dry_run(
    sql: str,
    name_lookups: list,
    project: str,
) -> str:
    """Rewrite user SQL from DuckDB-flavor to BQ-native so a single
    `_bq_dry_run_bytes` call can estimate scan size for the EXACT query
    the user submitted (issue #171). Thin wrapper around the shared
    core; kept as a stable name for callers in /api/query's cap-guard.
    """
    return _rewrite_bq_table_refs_to_native(sql, name_lookups, project)


def _rewrite_user_sql_for_bigquery_query(
    user_sql: str,
    conn: duckdb.DuckDBPyConnection,
) -> tuple[str, bool]:
    """Rewrite user SQL so the entire query ships to BQ as a single
    ``bigquery_query(<project>, <inner-sql>)`` call.

    Thin wrapper over :func:`_bq_remote_execution_plan` preserving the
    long-standing ``(rewritten_sql, did_rewrite)`` return shape used by
    ``execute_query`` and this module's tests. Callers that also need the
    labeled ``client.query`` path (cost attribution, #752) use
    ``_bq_remote_execution_plan`` directly to get the BQ-native inner SQL and
    billing project.
    """
    rewritten, did_rewrite, _billing_project, _inner_sql = _bq_remote_execution_plan(user_sql, conn)
    return rewritten, did_rewrite


def _bq_remote_execution_plan(
    user_sql: str,
    conn: duckdb.DuckDBPyConnection,
) -> tuple[str, bool, str | None, str | None]:
    """Plan how a remote (BQ) SELECT should execute.

    Returns ``(rewritten_sql, did_rewrite, billing_project, inner_sql)``:

    - ``rewritten_sql`` — the DuckDB ``SELECT * FROM bigquery_query(...)`` form
      for the extension path (equals ``user_sql`` when ``did_rewrite`` is False).
    - ``did_rewrite`` — whether the whole query could be pushed to BQ.
    - ``billing_project`` / ``inner_sql`` — the BQ execution/billing project and
      the BQ-native inner SQL, populated **only** when ``did_rewrite`` is True.
      They let a caller run the billable job via
      ``connectors.bigquery.access.run_bq_query_to_arrow`` (labeled
      ``client.query``, #752) instead of the unlabeled DuckDB
      ``bigquery_query()`` extension. ``None`` when not rewritten.

    When ``did_rewrite`` is
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
        return user_sql, False, None, None

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
        return user_sql, False, None, None

    # Multi-project guard (devil's-advocate R1 finding #5): rows resolve
    # under the single `bq.projects.data` project UNLESS they carry a
    # `bq_fqn` (v51, issue #343), which pins their own project explicitly
    # and is honored per-row via `_bq_row_target` below.
    #
    # `bucket` remains the one place an *implicit* cross-project leak could
    # hide: a bucket containing `.` (e.g. `other_prj.dataset`) suggests the
    # operator encoded a project prefix into the bucket name, and wrapping
    # that under our project would silently target the wrong one.
    # Conservative skip: any BQ row whose bucket contains `.` aborts the
    # rewrite, falling through to the legacy ATTACH-catalog path which uses
    # whatever resolution the operator's _remote_attach configured. Rows
    # that need a different project should set `bq_fqn` instead.
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
            return user_sql, False, None, None
        # Issue #1322: a bare `\bname\b` match also fires on the keyword
        # half of ORDER BY / GROUP BY / PARTITION BY when a registered
        # table is named after one of those keywords (e.g. `order`), which
        # then corrupts the rewriter's substitution downstream. Reuse the
        # same compiled pattern the RBAC name guards use — it already
        # suppresses a name immediately followed by " by" via a negative
        # lookahead, since no real reference can occupy that position
        # (DuckDB/BQ both reject a bare `by` as an identifier).
        if _name_reference_re(str(name).lower()).search(sql_lower_masked):
            key = (bucket.lower(), source_table.lower())
            if key not in seen_paths:
                seen_paths.add(key)
            ds, tbl, row_project = _bq_row_target(r)
            name_lookups.append((str(name), ds, tbl, row_project))

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
        return user_sql, False, None, None

    # Skip 3: cross-source query (BQ + local-mode). If user SQL also
    # references a non-BQ master view, we can't push the whole thing to
    # BQ — DuckDB needs to do the join.
    bq_names_lc = {str(entry[0]).lower() for entry in name_lookups}
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
            return user_sql, False, None, None
        # Issue #1322: same keyword-collision suppression as the bare-name
        # pass above — a local-mode table named e.g. `order` must not make
        # every BQ query containing an innocent `ORDER BY` fall back to the
        # slower ATTACH-catalog path.
        if _name_reference_re(name_lc).search(sql_lower_masked):
            logger.info(
                "rewrite_skip_cross_source: user SQL references both "
                "BQ-remote and local-mode tables; falling back to "
                "ATTACH-catalog path",
            )
            return user_sql, False, None, None

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
        return user_sql, False, None, None
    if not data_project:
        return user_sql, False, None, None

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
        rewritten = f"SELECT * FROM bigquery_query('{billing_project}', '{escaped_inner}')"
    else:
        rewritten = f"SELECT * FROM bigquery_query('{billing_project}', {DOLLAR_TAG}{inner_sql}{DOLLAR_TAG})"
    return rewritten, True, billing_project, inner_sql


# ---------------------------------------------------------------------------
# Task 10 -- BigQuery arm: transpile, named params, ordering, fail-closed
# (design §7). A query touching a policied `query_mode='remote'` table
# cannot use the `bigquery_query()` push-down above at all (§7.1: its
# dollar-quoted payload has no bind mechanism, so `$user_groups` would
# either blow up with a DuckDB parameter-count mismatch or -- worse, when
# the policy needs no bind value -- silently ship the UNFILTERED original
# query with a 200). Both `execute_query` and `run_remote_select_to_arrow`
# route a policied-and-otherwise-pushable query through
# `_execute_policied_remote_bq` instead, which never falls back to either
# the push-down or an unfiltered execution on failure (§7.4/§17).
# ---------------------------------------------------------------------------


def _bq_policied_execution_sql(
    sql: str,
    principal,
    bq,
    *,
    name_lookups: list,
) -> tuple[str, dict, list[str]]:
    """Build the final, executable BigQuery-native SQL for a query that
    touches a policied `query_mode='remote'` table (§7.1-§7.3).

    Ordering (§7.3): the policy substitution runs FIRST -- via `rewrite_sql`
    (Task 6) with its `resolve` callable bound to
    `policied_relation(..., dialect='bigquery')`, so the policied table's
    node is replaced by its policy body already transpiled to BigQuery
    dialect (§7.2) -- and the existing bare-name -> physical-path pass
    (`_rewrite_bq_table_refs_to_native`, the same one the non-policied
    push-down uses) runs SECOND, over the whole substituted result. That
    second pass is what resolves the spliced-in policy body's own
    `FROM <name>` (still a bare registry name after transpile) to its
    physical ``\\`project.dataset.table\\``` path -- along with any OTHER
    bare BQ table name elsewhere in the query. Reversing the order would
    leave the AST substitution nothing to match (a prior bare-name pass
    would have already turned the caller's own reference into an opaque
    backtick path sqlglot no longer recognises as the same table, §7.3).

    Safe to call `rewrite_sql` a second time (the first, `dialect='duckdb'`
    call already ran in the caller to decide whether to reach this
    function at all): collision detection and identity resolution are
    dialect-independent, so the only NEW failure mode here is the
    BigQuery transpile itself, surfaced as `PolicyError` exactly like
    every other resolution failure (§16).

    `name_lookups` is the SAME bare-name -> (dataset, table, project)
    mapping the non-policied push-down already computed from the
    caller's original SQL (`_bq_guardrail_inputs`) -- it covers every
    registered BQ name in the query, policied or not, so it is reused
    as-is rather than recomputed.

    Returns `(bq_sql, params, policied_table_ids)` -- `params` are the
    RAW identity values (§6.2), not yet BigQuery `QueryParameter` objects;
    convert via `bq_query_parameters_from_policy_params` before binding.
    Raises `PolicyNameCollision` / `PolicyIdentityUnresolvable` /
    `PolicyError` exactly like `rewrite_sql` -- callers map these the
    same way they already map the `dialect='duckdb'` call's exceptions.
    """
    spliced_sql, params, policied_table_ids = rewrite_sql(
        sql,
        principal,
        resolve=functools.partial(policied_relation, dialect="bigquery"),
    )
    data_project = bq.projects.data
    bq_sql = _rewrite_bq_table_refs_to_native(spliced_sql, name_lookups, data_project)
    return bq_sql, params, policied_table_ids


def _execute_policied_remote_bq(
    sql: str,
    principal,
    bq,
    *,
    name_lookups: list,
    labels: dict,
    outer_limit: int | None = None,
    expected_ids=None,
):
    """Execute a query that touches a policied `query_mode='remote'` table
    directly against the BigQuery jobs API (§7.1) -- never through the
    `bigquery_query()` DuckDB-extension push-down, and never falling back
    to an unfiltered execution when this fails (§7.4/§17): ANY exception
    past the `rewrite_sql`/collision-detection step -- building the query
    parameters or the BQ job itself -- becomes a table-scoped `PolicyError`
    rather than propagating the raw engine detail (§16's rule that a
    `policy_error` never quotes the underlying message, which for a BQ
    rejection can otherwise echo literal identifiers from the policy body).

    `outer_limit`, when given, wraps the final SQL in an outer
    `LIMIT <outer_limit>` -- mirroring the non-policied push-down's
    `_bqq_outer` wrap -- so a preview-sized caller (`/api/query`) doesn't
    materialise an entire remote table into the worker process. `None`
    for a caller that wants the full, uncapped result (the
    snapshot-materialize path, `run_remote_select_to_arrow`).

    Returns the full `pyarrow.Table` (labeled cost-attribution job, #752
    -- same shape `run_bq_query_to_arrow` always returns); callers needing
    a row/column/truncated triple convert via `_arrow_table_to_rows`.
    """
    bq_sql, policy_params, policied_table_ids = _bq_policied_execution_sql(
        sql, principal, bq, name_lookups=name_lookups
    )
    # The BigQuery transpile happens inside that second `rewrite_sql`, and its
    # PolicyError is swallowed by the resolution loop — so a policy that fails
    # to transpile drops out silently and `bq_sql` becomes the caller's own
    # unfiltered statement. Same invariant as the Databricks arm.
    _assert_policy_substitution_complete(expected_ids, policied_table_ids)
    if outer_limit is not None:
        bq_sql = f"SELECT * FROM ({bq_sql}) AS _bqq_policy_outer LIMIT {outer_limit}"

    query_parameters = bq_query_parameters_from_policy_params(policy_params)
    try:
        table, _job_info = run_bq_query_to_arrow(
            bq,
            bq_sql,
            query_parameters=query_parameters,
            labels=labels,
        )
    except Exception as exc:
        raise PolicyError(next(iter(policied_table_ids), "unknown")) from exc
    return table


def _materialize_databricks_select(
    sql: str,
    sql_lower: str,
    conn,
    user,
    allowed,
    *,
    quota,
    policied_table_ids,
    policy_info: dict | None,
):
    """Materialize a Databricks SELECT to a full ``pyarrow.Table`` for
    ``agnes snapshot create --from-query``.

    Shares the planner with the interactive path — same registry gate, same
    RBAC, same cross-source refusal, same policy substitution — and differs in
    exactly two ways, both because a snapshot is a materialize and not a
    preview: no ``LIMIT n + 1`` wrap (the caller wants every row the predicate
    selects), and the size bound is the scan endpoint's own
    ``api.scan.max_result_bytes`` rather than the interactive remote-query cap.
    """
    from app.api.v2_scan import _databricks_scan_timeout_s, _max_result_bytes
    from connectors.databricks.remote import DatabricksRemoteError, execute_scan_to_arrow

    plan = _databricks_remote_plan(sql, sql_lower, conn, user, allowed)
    if plan is None:
        # `resolve_single_engine` said databricks, so the only way the planner
        # declines is the ATTACH path having a local view for every row — in
        # which case this is an ordinary local query and the caller's own
        # DuckDB branch handles it.
        return None
    if policied_table_ids:
        try:
            _apply_databricks_policies(plan, sql, user, expected_ids=policied_table_ids)
        except PolicyNameCollision as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": "policy_name_collision", "table": exc.table_id, "fix": "rename your CTE"},
            )
        except PolicyIdentityUnresolvable:
            raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
        except PolicyError as exc:
            raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
        if policy_info is not None:
            policy_info["policied_table_ids"] = list(policied_table_ids)

    _audit_uid, _audit_email = _identity_for_audit(user)
    user_id = _audit_email or _audit_uid or "anon"
    try:
        with quota.acquire(user=user_id):
            table = execute_scan_to_arrow(
                plan["sql"],
                settings=plan["settings"],
                cap_bytes=_max_result_bytes(),
                timeout_s=_databricks_scan_timeout_s(),
                parameters=plan.get("parameters") or None,
            )
            # A snapshot is written to disk and queried later, so a masking
            # policy that leaked a plaintext duplicate here would outlive the
            # request that made it.
            _assert_databricks_policy_columns_unique(plan, table.column_names)
            return table
    except PolicyError as exc:
        raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "reason": "concurrent_scans_exceeded",
                "kind": exc.kind,
                "current": exc.current,
                "limit": exc.limit,
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )
    except DatabricksRemoteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail())


def _arrow_table_to_rows(table, limit: int) -> tuple[list, list, bool]:
    """Convert a fully-materialized ``pyarrow.Table`` into the same
    ``(columns, rows, truncated)`` shape a DuckDB cursor's
    ``.fetchmany(limit + 1)`` call + slice produces, so a caller's
    existing "convert to serializable types" step (`execute_query`) works
    unchanged regardless of which execution path produced the result.
    """
    columns = list(table.column_names)
    truncated = table.num_rows > limit
    if truncated:
        table = table.slice(0, limit)
    rows = [[row.get(c) for c in columns] for row in table.to_pylist()]
    return columns, rows, truncated


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
            r["id"] for r in table_registry_repo().list_all() if (r.get("bucket"), r.get("source_table")) in wanted
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
        raise HTTPException(
            status_code=429,
            detail={
                "reason": "daily_byte_cap_exceeded",
                "kind": exc.kind,
                "current": exc.current,
                "limit": exc.limit,
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )

    try:
        bq = get_bq_access()
    except BqAccessError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "kind": exc.kind,
                "message": exc.message,
                **(exc.details or {}),
            },
        )

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
                sql,
                name_lookups,
                project,
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
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "kind": exc.kind,
                            "message": exc.message,
                            **(exc.details or {}),
                        },
                    )
                # Log the rejected SQL itself. Dry-run BQ jobs are not
                # retained by BigQuery, so once the request is over this
                # WARNING is the ONLY surviving evidence of what BQ was
                # actually asked to parse. Without it, triage cannot
                # distinguish a rewriter bug from user-side dialect drift —
                # an ambiguity that has cost a full investigation before.
                # Truncated: see `_sql_log_preview`.
                logger.warning(
                    "BQ dry-run rejected the rewritten SQL "
                    "(kind=%s, message=%s). Retrying with the user's "
                    "original SQL. rewritten_sql_preview=%r",
                    exc.kind,
                    exc.message,
                    _sql_log_preview(rewritten_sql, around=_bq_error_offset(exc.message, rewritten_sql)),
                )
                try:
                    total_bytes = _bq_dry_run_bytes(bq, sql, user=user, agent_name="query")
                except BqAccessError as exc2:
                    if exc2.kind != "bq_bad_request":
                        raise HTTPException(
                            status_code=502,
                            detail={
                                "kind": exc2.kind,
                                "message": exc2.message,
                                **(exc2.details or {}),
                            },
                        )
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "kind": "remote_estimate_failed",
                            "message": ("BigQuery rejected this query during cost estimation."),
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
                        },
                    )

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
                raise HTTPException(
                    status_code=400,
                    detail={
                        "reason": "remote_scan_too_large",
                        "scan_bytes": total_bytes,
                        "limit_bytes": cap_bytes,
                        "tables": tables,
                        "view_targets": view_targets,
                        "suggestion": suggestion,
                    },
                )

            # Yield control to the handler — slot stays acquired while the
            # caller runs analytics.execute() + record_bytes().
            yield total_bytes
    except QuotaExceededError as exc:
        # Only KIND_CONCURRENT can land here (daily-budget already mapped
        # above; record_bytes never raises). Map to 429 with structured
        # detail consistent with the daily-budget shape.
        raise HTTPException(
            status_code=429,
            detail={
                "reason": "concurrent_slot_exceeded",
                "kind": exc.kind,
                "current": exc.current,
                "limit": exc.limit,
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )


def run_remote_select_to_arrow(conn, user, sql, bq, quota, *, policy_info: dict | None = None):
    """Materialize a raw SELECT against a remote engine into an Arrow table (#616).

    Databricks statements branch out early to ``_materialize_databricks_select``
    and never reach the BigQuery machinery below; everything from the SELECT-only
    guard and the policy rewrite upward is shared.

    Backs the snapshot ``from_query`` mode used by ``agnes query --remote
    --auto-snapshot``: the analyst has explicitly opted into materializing a
    snapshot, so this path reuses the SAME validation as ``/api/query`` —
    SELECT-only guard, per-user view RBAC, BQ registry-gating, and the
    ``bigquery_query()`` rewrite for predicate pushdown — but deliberately
    does NOT apply the ``remote_scan_too_large`` cap (the cap is exactly what
    the analyst is bypassing). Daily-byte budget and concurrent-slot quotas
    still apply via ``quota``.

    When the rewrite fully pushes the query to BQ, the billable job runs via
    ``connectors.bigquery.access.run_bq_query_to_arrow`` (labeled
    ``client.query``) rather than the unlabeled DuckDB ``bigquery_query()``
    extension, so the job carries per-user cost-attribution labels (#752). The
    result is fully materialized either way, so this is shape-equivalent. Queries
    that can't be pushed (cross-source joins, DuckDB-only syntax) fall back to
    the extension path unlabeled.

    ``policy_info`` (Task 11, §10): an optional caller-supplied dict, mutated
    in place with ``{"policied_table_ids": [...]}`` when this SELECT touched
    an access-policied table -- the disclosure counterpart of ``job_info`` in
    ``app/api/v2_scan.py``'s ``_run_bq_scan``. This function returns a bare
    ``pyarrow.Table`` with no envelope of its own to carry the field, so
    ``/api/v2/scan``'s ``from_query`` branch passes a dict here to build the
    ``X-Agnes-Row-Scope`` response header. Callers that don't care leave it
    ``None`` and see no behavior change.

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
        # Same non-admin SQL RBAC as /api/query (catalog gate #868 + view-name
        # denylist + internal-extract denylist M1), shared via the helper.
        _enforce_non_admin_sql_rbac(analytics, sql_lower, allowed)

        # Table access policies (§5/§6) — same substitution as /api/query's
        # execute_query, shared here so a snapshot materialize can't be used
        # to bypass a policy /api/query would have enforced. See that
        # handler's comment for the full rationale; error→HTTP mapping
        # mirrors it exactly.
        try:
            policy_rewritten_sql, policy_params, policied_table_ids = rewrite_sql(
                sql, user, dialect=_policy_parse_dialect(sql, sql_lower)
            )
        except PolicyNameCollision as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "reason": "policy_name_collision",
                    "table": exc.table_id,
                    "fix": "rename your CTE",
                },
            )
        except PolicyIdentityUnresolvable:
            raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
        except PolicyError as exc:
            raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})

        # Which engine materializes this snapshot. Both registered remote
        # engines can; anything else is refused up front with the command that
        # does work, rather than skipping the BQ-only registry gate below and
        # then failing deep inside DuckDB with "table does not exist" (a
        # `query_mode='remote'` row has no local view on any engine).
        from src.remote_engines import CrossEngineError, referenced_remote_rows, resolve_single_engine

        try:
            _engine = resolve_single_engine(referenced_remote_rows(sql, sql_lower))
        except CrossEngineError as exc:
            raise HTTPException(status_code=400, detail=exc.detail())
        if _engine == "databricks":
            _dbx_table = _materialize_databricks_select(
                sql,
                sql_lower,
                conn,
                user,
                allowed,
                quota=quota,
                policied_table_ids=policied_table_ids,
                policy_info=policy_info,
            )
            # `None` means the planner declined because the experimental Unity
            # Catalog ATTACH gave DuckDB a local view for every referenced row.
            # That is an ordinary local query — fall through, don't return an
            # empty result. Clearing `_engine` is what actually makes it fall
            # through: leaving it set to "databricks" walked straight into the
            # unsupported-engine refusal below, so the ATTACH path — the one
            # case this branch exists to let through — was rejected.
            if _dbx_table is not None:
                return _dbx_table
            _engine = None
        if _engine is not None and _engine != "bigquery":
            raise HTTPException(
                status_code=400,
                detail={
                    "reason": "snapshot_engine_unsupported",
                    "engine": _engine,
                    "message": (
                        f"Snapshots can only be materialized from BigQuery or Databricks; this query runs on {_engine}."
                    ),
                    "hint": (
                        "Run it with `agnes query --remote` for an interactive answer, or register "
                        "it as a query_mode='materialized' table so the scheduler syncs it."
                    ),
                },
            )

        dry_run_set, name_lookups, blocked_bq_path = _bq_guardrail_inputs(
            sql,
            sql_lower,
            conn,
            user,
            allowed,
        )
        if blocked_bq_path is not None:
            raise HTTPException(status_code=403, detail=blocked_bq_path)

        # Snowflake direct-path guard — the same gate `/api/query` applies, for
        # the same reason. `src/db.py` re-ATTACHes the `sf` catalog on the
        # read-only analytics connection this path executes against, and
        # `_local_extract_catalogs` deliberately excludes non-`duckdb` catalogs
        # from the #868 catalog gate, so without this an `sf."schema"."table"`
        # reference reaches Snowflake with no registration and no grant check.
        # `resolve_single_engine` above does not know Snowflake either, so it
        # does not refuse the statement on this path.
        blocked_sf_path = _sf_guardrail_inputs(sql, sql_lower, conn, user, allowed)
        if blocked_sf_path is not None:
            raise HTTPException(status_code=403, detail=blocked_sf_path)

        # See _identity_for_audit — a restricted principal has no ".get".
        _audit_uid, _audit_email = _identity_for_audit(user)
        user_id = _audit_email or _audit_uid or "anon"
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
                        sql,
                        name_lookups,
                        project,
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

            # Plans the push-down against the ORIGINAL `sql`, not
            # `policy_rewritten_sql`. Fine when no table this query
            # touches is policied (the common case) — when
            # `policied_table_ids` is non-empty, `inner_sql`/`execution_sql`
            # below are NEVER used to execute anything; see the branch
            # immediately below (mirrors execute_query's identically
            # structured branch, §7.1-§7.4).
            execution_sql, did_rewrite, billing_project, inner_sql = _bq_remote_execution_plan(sql, conn)

            if did_rewrite and policied_table_ids:
                # §7.1-§7.4: see execute_query's identically commented
                # branch — this query touches a policied
                # `query_mode='remote'` table and is otherwise
                # push-down-eligible, so it runs directly against the BQ
                # jobs API with the policy transpiled + bound as named
                # parameters, never through the unlabeled `bigquery_query()`
                # extension and never falling back to an unfiltered
                # execution on failure. No outer LIMIT here — a snapshot
                # materialize wants the full, uncapped result.
                try:
                    table = _execute_policied_remote_bq(
                        sql,
                        user,
                        bq,
                        name_lookups=name_lookups,
                        labels=job_labels_for(user, "query"),
                        expected_ids=policied_table_ids,
                    )
                except PolicyNameCollision as exc:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "reason": "policy_name_collision",
                            "table": exc.table_id,
                            "fix": "rename your CTE",
                        },
                    )
                except PolicyIdentityUnresolvable:
                    raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
                except PolicyError as exc:
                    raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
            else:
                if not (did_rewrite and inner_sql is not None):
                    # Non-push-down (ATTACH-catalog) path: run the
                    # access-policy-rewritten SQL instead of the raw analyst
                    # SQL. Byte-identical to `execution_sql` here (a no-op)
                    # unless a table this query touches is policied.
                    execution_sql = policy_rewritten_sql
                if policied_table_ids:
                    # Read-path guard (§17) — the same fail-closed check
                    # `execute_query`'s identically structured branch runs,
                    # and it matters MORE here: this result is written
                    # straight to the analyst's snapshot parquet, so a
                    # masking policy that re-derives a column while `*`
                    # still emits the original persists BOTH copies to
                    # disk, past every live enforcement point. Arrow, unlike
                    # the JSON row surfaces, happily carries two fields of
                    # the same name. `probe_policy` rejects such a policy at
                    # save time, but only once the base table has a
                    # resolvable schema — a policy attached before the table
                    # synced slips through, which is exactly the gap this
                    # closes. Not needed on the push-down branch above:
                    # BigQuery itself refuses a result with duplicate output
                    # column names.
                    try:
                        assert_policied_reads_unique(analytics, policied_table_ids, user)
                    except PolicyError as exc:
                        raise HTTPException(
                            status_code=500,
                            detail={"reason": "policy_error", "table": exc.table_id},
                        )
                try:
                    try:
                        if did_rewrite and inner_sql is not None:
                            # #752: this path fully materializes the result to Arrow
                            # anyway, so run the billable job through
                            # google-cloud-bigquery `client.query(labels=...)` (like
                            # /api/v2/scan) instead of the unlabeled DuckDB
                            # `bigquery_query()` extension. The job then carries
                            # cost-attribution labels for the requesting user. The
                            # BQ-native `inner_sql` is what the extension would have
                            # sent to `jobs.query`. The bq client bills under
                            # `bq.projects.billing` (quota_project_id), matching the
                            # billing_project the extension path passes as
                            # bigquery_query()'s first arg.
                            table, _job_info = run_bq_query_to_arrow(
                                bq,
                                inner_sql,
                                labels=job_labels_for(user, "query"),
                            )
                        elif policy_params:
                            table = analytics.execute(execution_sql, policy_params).arrow()
                        else:
                            table = analytics.execute(execution_sql).arrow()
                    except HTTPException:
                        raise
                    except Exception as exc:
                        # A rewritten query rejected by BQ (DuckDB-only syntax that
                        # survived identifier rewrite) falls back to the original SQL
                        # via the ATTACH-catalog extension path — slower but correct.
                        # This fallback job is unlabeled (extension-owned), matching
                        # the interactive /api/query fallback contract. Retries on
                        # the policy-rewritten SQL, not the raw `sql` — this
                        # fallback re-enters the ATTACH-catalog path, so it must
                        # stay policy-filtered too. Reachable ONLY when NOT
                        # (did_rewrite and policied_table_ids) — see the branch
                        # above — so this never re-exposes the unfiltered
                        # original (§7.4).
                        if did_rewrite and _looks_like_bq_rewrite_parse_error(exc):
                            if policy_params:
                                table = analytics.execute(policy_rewritten_sql, policy_params).arrow()
                            else:
                                table = analytics.execute(policy_rewritten_sql).arrow()
                        else:
                            raise
                except HTTPException:
                    # Don't re-wrap structured rejections raised below (RBAC,
                    # SELECT-only, registry) — let them propagate.
                    raise
                except BqAccessError:
                    # #752: the labeled client.query path raises BqAccessError with
                    # a kind (auth_failed / bq_forbidden / bq_bad_request / …). Let
                    # it propagate so scan_endpoint maps it to the right HTTP status
                    # (500 / 502 / 400) instead of flattening every BQ failure into
                    # a generic 400 duckdb_execution_error.
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
        # Task 11 (§10): this function's plain pyarrow.Table return has no
        # envelope of its own to carry policied_table_ids (from the
        # rewrite_sql call above) — report it through policy_info instead,
        # so /api/v2/scan's from_query branch can build the
        # X-Agnes-Row-Scope header.
        if policy_info is not None:
            policy_info["policied_table_ids"] = list(policied_table_ids)
        return table
    finally:
        analytics.close()
