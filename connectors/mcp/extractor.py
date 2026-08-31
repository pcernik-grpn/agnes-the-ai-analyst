"""Universal MCP extractor — produces ``extract.duckdb + data/*.parquet`` for materialize-mode tools.

Per the extract.duckdb contract (see ``.claude/skills/agnes-connectors.md``):
every connector writes a ``_meta`` table with one row per produced table. The
``SyncOrchestrator`` scans ``/data/extracts/*/extract.duckdb``, ATTACHes each
into ``analytics.duckdb``, and creates master views automatically.

For Universal MCP, one ``mcp_sources`` row maps to one ``extract.duckdb``.
Each materialize-mode tool registered under that source contributes one
table (= one parquet file + one ``_meta`` row + one view inside
``extract.duckdb``).

Passthrough-mode tools are NOT materialized — they live in ``tool_registry``
and are invoked live at query time by the outbound MCP server's passthrough
handler (see RFC #461 §7).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd

from src.duckdb_conn import _open_duckdb
from src.identifier_validation import validate_quoted_identifier
from src.parquet_publish import atomic_publish
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import MATERIALIZE, ToolRegistryRepository
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)


# ── backend-aware repo accessors ────────────────────────────────────────────
#
# ``extract_source`` / ``extract_source_async`` take a caller-supplied
# ``system_conn`` (a DuckDB connection) for reading ``mcp_sources`` /
# ``tool_registry``. On a Postgres-backed instance those tables live in
# Postgres — a direct ``MCPSourceRepository(system_conn)`` would silently
# read an empty DuckDB shard regardless of what the caller passed in (the
# admin materialize endpoint hands over a ``Depends(_get_db)`` connection,
# which is always DuckDB). Mirror the escape-hatch pattern used by
# ``app.services.stack_resolver`` / ``app.auth.access``: honor the caller's
# connection only when the active backend actually is DuckDB, otherwise
# route through the factory.


def _sources_repo(system_conn: duckdb.DuckDBPyConnection) -> Any:
    from src.repositories import mcp_sources_repo, use_pg

    if use_pg():
        return mcp_sources_repo()
    return MCPSourceRepository(system_conn)


def _tools_repo(system_conn: duckdb.DuckDBPyConnection) -> Any:
    from src.repositories import tool_registry_repo, use_pg

    if use_pg():
        return tool_registry_repo()
    return ToolRegistryRepository(system_conn)


# ── result parsing ──────────────────────────────────────────────────────────


class EmptyUpstreamError(ValueError):
    """The tool answered successfully with an empty collection, and there is no
    previous snapshot whose schema a zero-row parquet could be written from.

    Distinct from a plain ``ValueError`` (upstream failure / not table-shaped)
    because the two want opposite handling: a failure keeps the last-known-good
    table, an empty upstream must not.
    """


def _find_data_array(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """Heuristic — find the first list-of-dicts inside an MCP tool's JSON payload.

    MCP tools commonly wrap data in a top-level dict like
    ``{"accounts": [...], "total": N}`` or ``{"items": [...]}``. We scan keys
    in insertion order; the first value that is a non-empty list of dicts
    becomes the materialized table.

    If the top-level itself is a list of dicts, use that directly.
    """
    if isinstance(payload, list):
        if payload and all(isinstance(x, dict) for x in payload):
            return payload
        return None
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                return v
    return None


# Keys whose truthy value says the request did not fully succeed. An upstream
# that answers HTTP-200-with-a-soft-error (``{"error": "quota exceeded",
# "accounts": []}``, ``{"status": "degraded", "details": []}``) must never be
# read as "the table is empty" — that would overwrite the last-known-good
# parquet on a rate limit. Deliberately narrow: keys that routinely ride along
# with a successful response (``message``, ``detail``, ``warning``) are NOT
# here, because treating those as failures would re-pin genuinely empty tables
# to stale data — the bug this whole path exists to fix.
_FAILURE_SIGNAL_KEYS = frozenset({"error", "errors", "err", "exception", "failure", "fault", "status", "state"})
_SUCCESS_VALUES = frozenset(
    {"ok", "okay", "success", "successful", "succeeded", "healthy", "complete", "completed", "done"}
)
# The other half of the convention: a *negated* success flag. `{"success":
# false, "data": []}` is as much a failure as `{"error": "..."}`.
_SUCCESS_FLAG_KEYS = frozenset({"success", "successful", "succeeded", "ok", "okay", "isok", "is_ok"})


def _looks_like_soft_failure(payload: Dict[str, Any]) -> bool:
    """True when a 200 response carries an in-band failure signal.

    Covers the three ways upstreams encode "it didn't work" inside a 200 body:
    a truthy failure key (``{"error": "quota exceeded"}``), a non-success status
    — string *or* numeric (``{"status": 429}``) — and a falsified success flag
    (``{"success": false}``).
    """
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        name = key.lower()
        if name in _SUCCESS_FLAG_KEYS and value is False:
            return True
        if name not in _FAILURE_SIGNAL_KEYS:
            continue
        if value is True:  # {"error": true}
            return True
        if isinstance(value, dict) and value:  # {"error": {"code": 429}}
            return True
        if isinstance(value, str) and value.strip() and value.strip().lower() not in _SUCCESS_VALUES:
            return True
        # Numeric status: HTTP-shaped, so 2xx passes and 429/500 do not. 0 is
        # the "no error" convention in several RPC dialects.
        if isinstance(value, int) and not isinstance(value, bool) and not (value == 0 or 200 <= value < 300):
            return True
    return False


def _is_keyed_record_map(value: Any) -> bool:
    """True when a dict value could itself be the table — an id → record map.

    Distinguishes ``{"accounts": {"a1": {...}, "a2": {...}}}`` (plausibly the
    real payload, keyed by id) from a sibling metadata object like
    ``{"pagination": {"page": 1, "total": 0}}`` or ``{"meta": {"took_ms": 4}}``,
    which say nothing about whether the table is empty. Getting this boundary
    wrong is costly in both directions: too wide and every paginated empty
    response goes back to being pinned to stale data, too narrow and a real
    payload gets overwritten with zero rows.
    """
    return isinstance(value, dict) and bool(value) and all(isinstance(v, dict) for v in value.values())


# Sibling counters that contradict "the table is empty" when positive. Compared
# on a normalized key (lowercased, underscores stripped), so total_count /
# totalCount / TotalCount all match. `size`/`limit`/`page` are excluded on
# purpose — they describe the request, not the result.
_COUNT_KEYS = frozenset(
    {"total", "count", "totalcount", "totalresults", "numresults", "recordcount", "rowcount", "numrows", "totalrows"}
)
# Pagination continuation markers. An empty page that still advertises more
# pages is the same self-contradiction as a positive total: materialize always
# calls the tool with no arguments, so this is the FIRST page — nothing legitimate
# says "no rows here, but keep going". Only `next*`-style keys and explicit
# more-flags; a bare `cursor`/`page_token` may be the request echoed back.
_CONTINUATION_KEYS = frozenset(
    {
        "next",
        "nextcursor",
        "nextpage",
        "nextpagetoken",
        "nextpageurl",
        "nextlink",
        "nexttoken",
        "nexturl",
        "nextoffset",
        "continuationtoken",
        "hasmore",
        "hasnext",
        "hasnextpage",
        "morepages",
        "more",
    }
)
# Traversal budget for the nested scan. A payload big or deep enough to exhaust
# it is not a clean empty response, so exhaustion reads as "do not reset".
# Iterative (explicit stack) so adversarially deep JSON cannot blow the stack.
_SCAN_NODE_BUDGET = 10_000


def _contradicts_empty(payload: Dict[str, Any]) -> bool:
    """True when something in the payload argues against "the table is empty".

    Runs *after* every top-level list has been found empty, and looks for the
    three ways a response can still be carrying data:

    * a non-empty list at any depth — ``{"errors": [], "result": {"rows":
      [{...}]}}`` has its rows one level below where ``_find_data_array`` looks,
      and resetting there would wipe a table whose upstream *did* return data;
    * an id → record map (see ``_is_keyed_record_map``);
    * a positive count sibling — ``{"accounts": [], "total": 2}`` is a paginated
      or cursor glitch, not an empty table. Materialize always calls the tool
      with no arguments, so an empty first page next to a positive total is a
      contradiction rather than a legitimate past-the-end page;
    * a live continuation marker — ``{"items": [], "next_cursor": "abc"}`` or
      ``{"items": [], "has_more": true}`` says "no rows here, but keep going",
      which nothing legitimate says about a first page. ``next_cursor: null``
      is the honest end-of-results and still resets.

    Both the nested scan and the count check came from Devin Review on this
    change. Plain metadata objects still pass: ``{"pagination": {"page": 1,
    "total": 0}}`` contains no list, no record map and no positive count.
    """
    stack: List[Any] = [payload]
    budget = _SCAN_NODE_BUDGET
    while stack:
        if budget <= 0:
            return True  # too big to reason about — stay conservative
        budget -= 1
        node = stack.pop()
        if isinstance(node, list):
            if node:
                return True
            continue
        if not isinstance(node, dict):
            continue
        if node is not payload and _is_keyed_record_map(node):
            return True
        for key, value in node.items():
            name = key.lower().replace("_", "") if isinstance(key, str) else ""
            if name in _COUNT_KEYS and isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return True
            if name in _CONTINUATION_KEYS and _is_truthy_marker(value):
                return True
            if isinstance(value, (list, dict)):
                stack.append(value)
    return False


def _is_truthy_marker(value: Any) -> bool:
    """True for a continuation marker that actually points somewhere.

    ``next_cursor: null`` / ``""`` / ``false`` / ``0`` are all "no more pages";
    a non-empty string, ``true``, or a positive number means there is more.
    A dict/list value counts when non-empty (``next: {"href": "..."}``).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, int):
        return value > 0
    if isinstance(value, (dict, list)):
        return bool(value)
    return False


def _upstream_is_empty(payload: Any) -> bool:
    """True when a SUCCESSFUL response carries a legitimately empty collection.

    ``_find_data_array`` returns None for two situations that need opposite
    handling: the upstream has no rows right now (``[]``,
    ``{"accounts": [], "total": 0}``) — a real, materializable state — and the
    response does not describe an empty table at all (``{"status": "degraded"}``,
    ``{"names": ["a"]}``) — a classification or upstream problem where the
    safest move is to keep the last-known-good table.

    A dict qualifies only when it has at least one list value, *every* list
    value is empty, nothing in it contradicts "empty" (``_contradicts_empty``),
    and no value carries an in-band failure signal
    (``_looks_like_soft_failure``). Those two exclusions matter because this
    function decides whether to overwrite a parquet we still depend on:

    * ``{"error": "quota exceeded", "accounts": []}`` — a soft-failed 200 would
      otherwise wipe the table on a rate limit;
    * ``{"accounts": {"a1": {...}}, "warnings": []}`` and ``{"errors": [],
      "result": {"rows": [{...}]}}`` — the real content sits in a container we
      failed to interpret, so "empty" is not a safe conclusion (same reasoning
      as a mixed ``{"items": [], "rows": ["a"]}``).

    Plain metadata objects do not disqualify a payload:
    ``{"accounts": [], "pagination": {"page": 1, "total": 0}}`` is an ordinary
    empty page and resets the table. Every one of these boundaries came from
    Devin Review on this change.
    """
    if isinstance(payload, list):
        return not payload
    if isinstance(payload, dict):
        lists = [v for v in payload.values() if isinstance(v, list)]
        if not lists or any(v for v in lists):
            return False
        if _contradicts_empty(payload):
            return False
        return not _looks_like_soft_failure(payload)
    return False


def _write_zero_row_parquet_like(parquet_path: Path) -> Optional[int]:
    """Rewrite ``parquet_path`` as a zero-row file with its own schema.

    An empty JSON list carries no column information, so the snapshot being
    replaced is the only schema source available (the registry stores each
    tool's *input* schema; MCP ``outputSchema`` is optional and almost never
    populated by the servers we ingest). Returns the new size in bytes, or
    None when there is no readable previous file to take a schema from.

    The rows being dropped are kept as a single ``.parquet.prev`` sibling. This
    is the one destructive step in the extractor — after it, carry-forward can
    no longer recover the rows on a later failed run — and no in-band-failure
    heuristic is perfect (``{"accounts": [], "message": "internal error,
    retry"}`` reads as legitimately empty on purpose, because treating
    ``message`` as a failure would re-pin genuinely empty tables to stale data).
    One retained copy makes a misclassification recoverable by hand instead of
    permanent. Raised by Devin Review on this change.
    """
    if not parquet_path.exists():
        return None
    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        prev_meta = pq.read_metadata(parquet_path)  # footer-only read, never data
        schema = prev_meta.schema.to_arrow_schema()
    except Exception:
        logger.warning(
            "empty upstream: previous parquet %s is unreadable; cannot write a zero-row table",
            parquet_path,
            exc_info=True,
        )
        return None
    # Write-then-publish (#1359: via `atomic_publish` — per-process temp,
    # chmod 0644, os.replace): this is the one path that overwrites a parquet
    # whose content we still depend on (its schema), and a torn write would
    # take the last-known-good snapshot with it — carry-forward would then
    # drop the table as unreadable on the next run. from_batches([]) rather
    # than Schema.empty_table(): the latter needs pyarrow >= 14, the floor is
    # 12.
    prev_path = parquet_path.with_suffix(parquet_path.suffix + ".prev")
    with atomic_publish(parquet_path) as tmp_path:
        pq.write_table(pa.Table.from_batches([], schema=schema), tmp_path)
        # Only when the file being replaced actually HOLDS rows. On a second
        # consecutive empty run the live parquet is already the zero-row file,
        # and copying it would overwrite the one recoverable snapshot with
        # nothing — destroying the backup in exactly the repeated-
        # misclassification case it exists for (Devin Review). So `.prev` means
        # "the last non-empty snapshot", not "the previous file".
        #
        # copy, not rename: the original must stay in place until the atomic
        # publish below, or a failure between the two steps would leave the
        # table with no parquet at all (carry-forward drops such a table).
        # Bounded to one copy per table — a later non-empty→empty cycle
        # overwrites it.
        if prev_meta.num_rows:
            try:
                shutil.copy2(parquet_path, prev_path)
            except Exception:
                logger.warning(
                    "empty upstream: could not retain %s; the dropped rows will not be recoverable",
                    prev_path,
                    exc_info=True,
                )
    logger.warning(
        "empty upstream: %s reset to zero rows (%d dropped); last non-empty snapshot %s",
        parquet_path.name,
        prev_meta.num_rows,
        f"retained at {prev_path}" if prev_path.exists() else "not retained",
    )
    return parquet_path.stat().st_size


# ── output paths ────────────────────────────────────────────────────────────


def _data_dir() -> Path:
    """Resolve the extracts root. Honors AGNES_DATA_DIR; defaults to ./data."""
    root = os.environ.get("AGNES_DATA_DIR") or os.environ.get("DATA_DIR") or "data"
    return Path(root) / "extracts"


def output_dir_for_source(source_name: str) -> Path:
    return _data_dir() / source_name


# ── _meta + extract.duckdb writers ──────────────────────────────────────────


def _create_meta(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the _meta table required by the extract.duckdb contract."""
    conn.execute("DROP TABLE IF EXISTS _meta")
    conn.execute(
        """CREATE TABLE _meta (
            table_name   VARCHAR NOT NULL,
            description  VARCHAR,
            rows         BIGINT,
            size_bytes   BIGINT,
            extracted_at TIMESTAMP,
            query_mode   VARCHAR DEFAULT 'local'
        )"""
    )


def _insert_meta(
    conn: duckdb.DuckDBPyConnection,
    *,
    table_name: str,
    description: Optional[str],
    rows: int,
    size_bytes: int,
    extracted_at: datetime,
) -> None:
    conn.execute(
        "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
        [table_name, description, rows, size_bytes, extracted_at],
    )


def _table_summary(table_name: str, rows: int, size_bytes: int) -> Dict[str, Any]:
    """Build one ``tables[]`` entry of the extractor's summary.

    ``rows == 0`` is reachable only through the empty-upstream reset in
    ``_materialize_one_tool_async`` (a normal write needs a non-empty
    list-of-dicts), so flag it: the table exists and is genuinely empty, which
    is a different story from "the tool did not run".
    """
    entry: Dict[str, Any] = {"table": table_name, "rows": rows, "size_bytes": size_bytes}
    if rows == 0:
        entry["empty_upstream"] = True
    return entry


def _error_entry(table_name: str, exc: Exception) -> Dict[str, Any]:
    """Build one ``errors[]`` entry, tagged so callers can branch on the kind.

    ``empty_upstream`` — the tool answered fine but has no rows and no previous
    snapshot to take a schema from (nothing was materialized, nothing is
    stale). ``materialize_failed`` — everything else; the table keeps its
    last-known-good snapshot via carry-forward.
    """
    code = "empty_upstream" if isinstance(exc, EmptyUpstreamError) else "materialize_failed"
    return {"tool": table_name, "error": str(exc), "code": code}


def _carry_forward_untouched(
    out_conn: duckdb.DuckDBPyConnection,
    *,
    prev_db_path: Path,
    output_root: Path,
    keep: set,
    exclude: set,
) -> List[str]:
    """Merge the previous extract's untouched tables into the fresh one.

    Every run rebuilds ``extract.duckdb`` from scratch; without this, the
    ``_meta`` rows + views of every table the run didn't (re-)write — the
    tools a targeted (``only_tool_id``) run skipped, or a tool whose
    upstream call failed — would vanish until the next successful
    full-source run (their parquets stay on disk but the orchestrator's
    rebuild loses the views). Re-inserts each previous ``_meta`` row whose
    table was not written in this run and recreates its view over the
    existing parquet. Returns the carried table names.

    ``keep`` is the set of exposed_names of the source's currently
    registered, enabled materialize-mode tools: previous tables outside it
    (tool renamed, disabled, or deleted since the last run) are pruned
    rather than carried, so stale tables can't survive indefinitely.
    ``exclude`` is what this run already wrote.
    """
    if not prev_db_path.exists():
        return []
    try:
        prev_conn = _open_duckdb(str(prev_db_path), read_only=True)
    except Exception:
        logger.warning(
            "carry-forward: cannot open previous extract %s; other tools' tables will need a full-source run",
            prev_db_path,
            exc_info=True,
        )
        return []
    try:
        prev_rows = prev_conn.execute(
            "SELECT table_name, description, rows, size_bytes, extracted_at, query_mode FROM _meta"
        ).fetchall()
    except Exception:
        logger.warning("carry-forward: previous extract %s has no readable _meta", prev_db_path, exc_info=True)
        prev_rows = []
    finally:
        prev_conn.close()

    carried: List[str] = []
    for table_name, description, rows, size_bytes, extracted_at, query_mode in prev_rows:
        if table_name in exclude or table_name not in keep:
            continue
        # Relaxed check on purpose: the write path accepts dashed/dotted
        # exposed_names (common for MCP tool names), so the carry-forward
        # gate must not be stricter than what a full run writes. Still
        # refuses quote-breakout, path separators, and control chars.
        if not validate_quoted_identifier(table_name, "carry-forward table_name"):
            continue
        parquet_path = output_root / "data" / f"{table_name}.parquet"
        if not parquet_path.exists():
            logger.warning("carry-forward: parquet missing for %s; dropping its stale _meta row", table_name)
            continue
        # Per-row guard: carry-forward is best-effort housekeeping for tables
        # this run didn't touch, so one unreadable leftover parquet (zero-byte
        # from an interrupted write, truncated, a directory) must not fail the
        # whole materialize request and throw away the data just fetched.
        # DuckDB binds read_parquet at CREATE VIEW time, so the raise lands
        # here, outside the per-tool try/except (Devin Review on #1119).
        # View first, _meta second: DuckDB binds read_parquet at CREATE VIEW
        # time, so an unreadable file raises there — doing the INSERT first
        # would leave a _meta row describing a table with no view behind it.
        try:
            _create_view(out_conn, table_name, parquet_path)
            out_conn.execute(
                "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, ?)",
                [table_name, description, rows, size_bytes, extracted_at, query_mode],
            )
        except Exception:
            logger.warning(
                "carry-forward: could not re-attach %s from %s; skipping it this run",
                table_name,
                parquet_path,
                exc_info=True,
            )
            continue
        carried.append(table_name)
    return carried


def _create_view(conn: duckdb.DuckDBPyConnection, table_name: str, parquet_path: Path) -> None:
    # DuckDB does not accept prepared parameters inside read_parquet(),
    # so we inline the path. parquet_path comes from output_dir_for_source()
    # which derives from mcp_sources.name (DB-enforced unique) + the
    # exposed_name we control — no user-supplied path components.
    safe_path = str(parquet_path).replace("'", "''")
    conn.execute(f"CREATE OR REPLACE VIEW {quote_ident(table_name)} AS SELECT * FROM read_parquet('{safe_path}')")


# ── extraction ──────────────────────────────────────────────────────────────


async def _materialize_one_tool_async(
    *,
    source: Dict[str, Any],
    tool: Dict[str, Any],
    output_path: Path,
) -> Tuple[int, int]:
    """Call the upstream tool (async-safe), write parquet, return (rows, size_bytes).

    Async-only because the parent extract path may run inside FastAPI's
    event loop (admin /materialize endpoint). The sync wrapper around it
    used ``asyncio.run`` which blows up in that case.

    A legitimately empty upstream collection writes a zero-row parquet (rows=0)
    rather than raising; see ``_upstream_is_empty``. Raises
    ``EmptyUpstreamError`` when there is no previous snapshot to take the schema
    from, and ``ValueError`` / ``RuntimeError`` for responses that are not
    table-shaped or that failed outright.
    """
    from connectors.mcp.client import call_tool_async

    original_name = tool["original_name"]
    logger.info("materialize: calling %s.%s", source["name"], original_name)
    result = await call_tool_async(source, original_name, arguments=None)
    if result.is_error:
        raise RuntimeError(f"upstream tool {original_name} returned error: {result.text[:300]}")
    if result.data is None:
        # Name what arrived, not just what was required: the operator's only
        # other route to the actual shape is reproducing the call by hand,
        # and the credential this job used lives write-only in the vault.
        blocks = ", ".join(result.block_types) if result.block_types else "none"
        sample = " ".join(result.text.split())[:200]
        raise ValueError(
            f"tool {original_name} did not return parseable JSON; "
            f"materialize mode requires a JSON response with a list-of-dicts. "
            f"Got: structuredContent={'yes' if result.structured_present else 'no'}, "
            f"content blocks=[{blocks}], text starts: {sample!r}" + ("" if sample else " (empty)")
        )
    parquet_path = output_path / "data" / f"{tool['exposed_name']}.parquet"
    rows = _find_data_array(result.data)
    if rows is None:
        if _upstream_is_empty(result.data):
            # The upstream legitimately has no rows. Writing a zero-row parquet
            # is the only way analytics stop serving the last non-empty
            # snapshot: the carry-forward merge would otherwise keep the
            # previous _meta row + view alive on every subsequent run, forever
            # (Devin Review on #1119), so all-rows-deleted-upstream would be
            # indistinguishable from a transient failure.
            size_bytes = _write_zero_row_parquet_like(parquet_path)
            if size_bytes is None:
                raise EmptyUpstreamError(
                    f"tool {original_name} returned an empty collection and has no previous snapshot "
                    f"to take a schema from; nothing was materialized. Re-run once the tool has rows, "
                    f"or reclassify it as passthrough."
                )
            logger.info(
                "materialize: %s.%s returned an empty collection; table reset to zero rows",
                source["name"],
                original_name,
            )
            return (0, size_bytes)
        raise ValueError(
            f"tool {original_name} response has no list-of-dicts; either reclassify as passthrough or wrap the response"
        )
    df = pd.DataFrame(rows)
    # Published atomically (#1359) — a direct `df.to_parquet(parquet_path)`
    # wrote straight onto the served path every reader (the orchestrator's
    # hasher, the master views, `agnes pull`) trusts is complete.
    with atomic_publish(parquet_path) as tmp_path:
        df.to_parquet(tmp_path, index=False)
    size_bytes = parquet_path.stat().st_size
    return (len(df), size_bytes)


def _materialize_one_tool(
    *,
    source: Dict[str, Any],
    tool: Dict[str, Any],
    output_path: Path,
) -> Tuple[int, int]:
    """Sync wrapper around ``_materialize_one_tool_async`` — only for the
    scheduler / CLI paths that run outside an event loop. FastAPI handlers
    MUST call the async variant directly."""
    return asyncio.run(
        _materialize_one_tool_async(
            source=source,
            tool=tool,
            output_path=output_path,
        )
    )


async def extract_source_async(
    *,
    system_conn: duckdb.DuckDBPyConnection,
    source_id: str,
    only_tool_id: Optional[str] = None,
    output_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Async variant of ``extract_source`` — call from FastAPI handlers.

    Same return shape as the sync version; the only difference is that
    each upstream call awaits ``_materialize_one_tool_async`` instead of
    going through ``asyncio.run`` (which is illegal inside a running
    event loop).
    """
    sources_repo = _sources_repo(system_conn)
    tools_repo = _tools_repo(system_conn)

    source = sources_repo.get(source_id)
    if source is None:
        raise ValueError(f"mcp_source not found: {source_id}")
    if not source.get("enabled"):
        raise ValueError(f"mcp_source disabled: {source_id}")

    all_tools = tools_repo.list_for_source(source_id)
    tools = [t for t in all_tools if t["mode"] == MATERIALIZE and t.get("enabled", True)]
    registered_names = {t["exposed_name"] for t in tools}
    if only_tool_id:
        tools = [t for t in tools if t["tool_id"] == only_tool_id]
    if not tools:
        return {
            "source_name": source["name"],
            "tables": [],
            "errors": [],
            "carried_forward": [],
            "note": "no materialize tools to run",
        }

    if output_root is None:
        output_root = output_dir_for_source(source["name"])
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "data").mkdir(exist_ok=True)

    db_path = output_root / "extract.duckdb"
    tmp_db_path = output_root / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()

    summary_tables: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    carried: List[str] = []

    out_conn = _open_duckdb(str(tmp_db_path))
    try:
        _create_meta(out_conn)
        for tool in tools:
            extracted_at = datetime.now(timezone.utc)
            try:
                rows, size_bytes = await _materialize_one_tool_async(source=source, tool=tool, output_path=output_root)
                _insert_meta(
                    out_conn,
                    table_name=tool["exposed_name"],
                    description=tool.get("description"),
                    rows=rows,
                    size_bytes=size_bytes,
                    extracted_at=extracted_at,
                )
                _create_view(out_conn, tool["exposed_name"], output_root / "data" / f"{tool['exposed_name']}.parquet")
                summary_tables.append(_table_summary(tool["exposed_name"], rows, size_bytes))
            except EmptyUpstreamError as exc:
                # Not a failure — the tool works, it just has nothing to show
                # yet and no schema to write. Reported with its own code so the
                # admin surface can tell it apart from a broken upstream.
                logger.warning("materialize: %s.%s %s", source["name"], tool["original_name"], exc)
                errors.append(_error_entry(tool["exposed_name"], exc))
            except Exception as exc:
                logger.exception("materialize failed for %s.%s", source["name"], tool["original_name"])
                errors.append(_error_entry(tool["exposed_name"], exc))
        # Merge semantics: keep every previously materialized table this run
        # didn't (re-)write — the tools a targeted run skipped, plus the
        # last-known-good snapshot of any tool whose upstream call failed
        # above. Tables whose tool left the registry (renamed / disabled /
        # deleted) are not in ``registered_names`` and drop out.
        carried = _carry_forward_untouched(
            out_conn,
            prev_db_path=db_path,
            output_root=output_root,
            keep=registered_names,
            exclude={t["table"] for t in summary_tables},
        )
    finally:
        out_conn.close()

    if db_path.exists():
        db_path.unlink()
    tmp_db_path.rename(db_path)

    return {
        "source_id": source_id,
        "source_name": source["name"],
        "extract_duckdb": str(db_path),
        "tables": summary_tables,
        "carried_forward": carried,
        "errors": errors,
    }


def extract_source(
    *,
    system_conn: duckdb.DuckDBPyConnection,
    source_id: str,
    only_tool_id: Optional[str] = None,
    output_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Materialize all (or one) materialize-mode tools for an MCP source.

    Writes ``extract.duckdb`` + ``data/*.parquet`` under
    ``<AGNES_DATA_DIR>/extracts/<source.name>/``. The orchestrator's next
    ``rebuild()`` will ATTACH it into ``analytics.duckdb`` automatically.

    Args:
        system_conn: open connection to ``system.duckdb`` (for repo reads).
        source_id:   ``mcp_sources.id`` to extract from.
        only_tool_id: if set, only materialize this one tool; the other
            tools' parquets, ``_meta`` rows and views are carried forward
            from the previous extract (merge, not replace).
        output_root:  override the extracts root (defaults to AGNES_DATA_DIR).

    Returns a summary dict: ``{"source_name": ..., "tables": [...], "errors": [...]}``.
    """
    sources_repo = _sources_repo(system_conn)
    tools_repo = _tools_repo(system_conn)

    source = sources_repo.get(source_id)
    if source is None:
        raise ValueError(f"mcp_source not found: {source_id}")
    if not source.get("enabled"):
        raise ValueError(f"mcp_source disabled: {source_id}")

    all_tools = tools_repo.list_for_source(source_id)
    tools = [t for t in all_tools if t["mode"] == MATERIALIZE and t.get("enabled", True)]
    registered_names = {t["exposed_name"] for t in tools}
    if only_tool_id:
        tools = [t for t in tools if t["tool_id"] == only_tool_id]
    if not tools:
        return {
            "source_name": source["name"],
            "tables": [],
            "errors": [],
            "carried_forward": [],
            "note": "no materialize tools to run",
        }

    if output_root is None:
        output_root = output_dir_for_source(source["name"])
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "data").mkdir(exist_ok=True)

    db_path = output_root / "extract.duckdb"
    tmp_db_path = output_root / "extract.duckdb.tmp"
    if tmp_db_path.exists():
        tmp_db_path.unlink()

    summary_tables: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    carried: List[str] = []

    out_conn = _open_duckdb(str(tmp_db_path))
    try:
        _create_meta(out_conn)
        for tool in tools:
            extracted_at = datetime.now(timezone.utc)
            try:
                rows, size_bytes = _materialize_one_tool(source=source, tool=tool, output_path=output_root)
                _insert_meta(
                    out_conn,
                    table_name=tool["exposed_name"],
                    description=tool.get("description"),
                    rows=rows,
                    size_bytes=size_bytes,
                    extracted_at=extracted_at,
                )
                _create_view(out_conn, tool["exposed_name"], output_root / "data" / f"{tool['exposed_name']}.parquet")
                summary_tables.append(_table_summary(tool["exposed_name"], rows, size_bytes))
            except EmptyUpstreamError as exc:
                # Not a failure — the tool works, it just has nothing to show
                # yet and no schema to write. Reported with its own code so the
                # admin surface can tell it apart from a broken upstream.
                logger.warning("materialize: %s.%s %s", source["name"], tool["original_name"], exc)
                errors.append(_error_entry(tool["exposed_name"], exc))
            except Exception as exc:
                logger.exception("materialize failed for %s.%s", source["name"], tool["original_name"])
                errors.append(_error_entry(tool["exposed_name"], exc))
        # Merge semantics (see extract_source_async).
        carried = _carry_forward_untouched(
            out_conn,
            prev_db_path=db_path,
            output_root=output_root,
            keep=registered_names,
            exclude={t["table"] for t in summary_tables},
        )
    finally:
        out_conn.close()

    if db_path.exists():
        db_path.unlink()
    tmp_db_path.rename(db_path)

    return {
        "source_id": source_id,
        "source_name": source["name"],
        "extract_duckdb": str(db_path),
        "tables": summary_tables,
        "carried_forward": carried,
        "errors": errors,
    }


# ── introspect (used at source registration time) ───────────────────────────


def introspect_source(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Connect to the source and return discovered tools (as plain dicts).

    Convenience wrapper around ``connectors.mcp.client.list_tools`` for the
    admin CLI introspection flow. Async callers (FastAPI handlers) MUST
    use ``introspect_source_async`` — calling this from an async loop
    blows up with ``asyncio.run() cannot be called from a running event
    loop`` because the underlying ``list_tools`` sync wrapper invokes
    ``asyncio.run`` internally.
    """
    from connectors.mcp.client import list_tools  # local import keeps duckdb-free

    return [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in list_tools(source)]


async def introspect_source_async(
    source: Dict[str, Any],
    *,
    caller_user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Async-safe variant of ``introspect_source`` — call from FastAPI
    handlers (and any code already inside a running event loop).

    ``caller_user_id`` is threaded to the secret lookup so a ``per_user``
    source can be probed under the caller's own credential."""
    from connectors.mcp.client import list_tools_async  # local import keeps duckdb-free

    tools = await list_tools_async(source, caller_user_id=caller_user_id)
    # `read_only` rides along: the builder maps it onto `tool_registry.mutating`,
    # and dropping it here is what made every tool the builder registers look
    # non-mutating — including the ones that delete things upstream.
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
            "read_only": t.read_only,
        }
        for t in tools
    ]
