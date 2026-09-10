"""Shared hints for "not found" outcomes across CLI surfaces.

Used by `agnes query` (CLI) and the stdio MCP `query_local` tool so both
surfaces explain query_mode='remote' / server_only tables the same way, and
by `agnes facts neighbors|claims` for the fact-graph query surface (spec
`docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md`).

``column_not_found_hint``/``unregistered_table_hint`` are re-exported here
from ``src.query_error_hints`` — that module is the actual definition (it
must also be importable from `app/api/query.py`'s server-side error
handler, which never imports from `cli/`), but this file stays the one
place CLI/stdio-MCP code reaches for a query "not found" hint, same as
every other function below.
"""

from __future__ import annotations

import json
import re

from src.query_error_hints import column_not_found_hint, unregistered_table_hint

__all__ = [
    "column_not_found_hint",
    "facts_not_found_hint",
    "missing_table",
    "remote_table_hint",
    "row_scope_note",
    "row_scope_note_from_header",
    "sharepoint_connection_not_found_hint",
    "unregistered_table_hint",
]

_TABLE_MISS_RE = re.compile(r"Table with name ([A-Za-z_][A-Za-z0-9_]*) does not exist")


def missing_table(error_text: str) -> str | None:
    """Extract the unresolvable table name from a DuckDB CatalogException
    message, or None if ``error_text`` doesn't match that shape (e.g. a
    plain syntax error)."""
    m = _TABLE_MISS_RE.search(error_text)
    return m.group(1) if m else None


def remote_table_hint(table: str, *, surface: str = "cli") -> str:
    """Human-readable hint explaining that ``table`` might be a
    `query_mode='remote'` or `server_only` table with no local view.

    ``surface`` picks the wording appropriate to the caller: "cli" points
    the user at `agnes query --remote`; "mcp" points the calling agent at
    the `query` MCP tool.
    """
    if surface == "mcp":
        return (
            f"`{table}` might be a `query_mode='remote'` or `server_only` table — "
            "neither has a local view. Use the `query` tool instead: it runs "
            "server-side and routes local/remote tables automatically."
        )
    return (
        f"Note: `{table}` might be a `query_mode='remote'` or "
        "`server_only` table. Local DuckDB only holds views for tables "
        "`agnes pull` downloads — `remote` ones live on BigQuery, and "
        "`server_only` ones are kept server-side and not distributed to "
        "the laptop. Both are queryable server-side:\n"
        "  - List all registered tables:    agnes catalog\n"
        "  - Inspect column schema:         agnes schema <name>\n"
        '  - Run it server-side:            agnes query --remote "<SQL>"'
    )


def issue_not_found_hint() -> str:
    """Hint for a `404 issue_not_found` from `agnes issue show|comment` (or the
    `get_issue`/`issue_comment` MCP tools). Deliberately one wording for both
    causes — the id is wrong, or it names someone else's report (the API
    returns 404, not 403, for the second case; see `app/api/issues.py`'s
    `_owned_or_admin`) — so a hint can never be used to probe for an id's
    existence."""
    return "Not found. List your reports: agnes issue list   ·   admins: agnes admin issue list"


def sharepoint_connection_not_found_hint(connection_id: str) -> str:
    """Hint for a `404 connection_not_found` from an `agnes admin sharepoint
    <connection_id> ...` subcommand — the id is wrong, or it names a
    connection that isn't a SharePoint one (see `_sharepoint_connection_or_
    404`, `app/api/admin_extraction.py`)."""
    return (
        f"No SharePoint connection '{connection_id}'. Check the id with "
        "`agnes admin connection list --source-type sharepoint`."
    )


def facts_not_found_hint(subject_id: str, *, surface: str = "cli") -> str:
    """Hint for a `404` from `agnes facts neighbors|claims` (or the
    `fact_neighbors`/`fact_claims` MCP tools).

    Deliberately covers THREE causes in one honest sentence rather than
    branching on which applies: the id is wrong, it exists but you have no
    readable claim for it (spec §5 rule 2 — these two must be
    indistinguishable, on purpose, so a caller can never probe for
    existence), or the `facts` feature is off on this instance entirely.
    Never special-case the response body to tell these apart — that would
    reopen the existence oracle the repository closes.
    """
    base = (
        f"No readable fact/edge '{subject_id}'. This means one of: the id "
        "is wrong, you don't have access to any of its evidence, or the "
        "`facts` feature isn't enabled on this instance."
    )
    if surface == "mcp":
        return base + " Use the `fact_search` tool to find a valid id, or ask an admin about the `facts` feature flag."
    return base + " Check the id with `agnes facts search <type>`, or ask an admin whether `facts` is enabled here."


def row_scope_note(row_scope: dict | None) -> str | None:
    """Render the `[scope]` disclosure line (table access policies §10) for
    an already-parsed ``row_scope`` envelope
    (``src/access_policy.py::row_scope_payload``), or ``None`` if absent.

    The single wording source every CLI surface that can read a policied
    table's rows shares -- `agnes query` (``row_scope`` in the JSON body),
    `agnes describe` (``sample.row_scope``), `agnes snapshot create`/`refresh`
    (parsed from the ``X-Agnes-Row-Scope`` header via
    :func:`row_scope_note_from_header`) -- so the note never drifts between
    them. Always render to stderr at the call site: "silent partial scope
    is forbidden" (command-ux.md) applies to row filtering exactly like it
    does to source scope, so json/csv stdout must stay clean.
    """
    if not isinstance(row_scope, dict):
        return None
    note = row_scope.get("note")
    if not note:
        return None
    return f"[scope] {note}"


def row_scope_note_from_header(header_value: str | None) -> str | None:
    """Same disclosure line as :func:`row_scope_note`, for a caller that only
    has the raw ``X-Agnes-Row-Scope`` response header.

    ``POST /api/v2/scan`` (what `agnes snapshot create`/`refresh` call) has
    no JSON body to carry ``row_scope`` in, so it ships the same envelope as
    a JSON-encoded header instead (see ``app/api/v2_scan.py``). A missing or
    malformed header returns ``None`` rather than raising -- a disclosure
    header must never crash a snapshot fetch.
    """
    if not header_value:
        return None
    try:
        row_scope = json.loads(header_value)
    except (ValueError, TypeError):
        return None
    return row_scope_note(row_scope)
