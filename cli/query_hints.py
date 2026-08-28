"""Shared hints for "not found" outcomes across CLI surfaces.

Used by `agnes query` (CLI) and the stdio MCP `query_local` tool so both
surfaces explain query_mode='remote' / server_only tables the same way, and
by `agnes facts neighbors|claims` for the fact-graph query surface (spec
`docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md`).
"""

from __future__ import annotations

import re

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
