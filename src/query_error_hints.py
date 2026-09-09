"""Map a raw DuckDB binder/catalog error string onto the concrete next
step that resolves it, without touching the original text.

Production finding (2026-09-09): `query` is the most-used and most
error-prone tool on a live instance -- nearly all of a day's 27 failures
were an agent guessing a column name. DuckDB's own message is either
misleading (names an ALIAS, e.g. `Values list "s" does not have a
column named "X"`, never the underlying table) or silent about which
relation to check at all (`Referenced column "X" not found in FROM
clause!`). Neither tells the caller to run `schema <table>` first.

Every function here returns ONLY the hint to append -- never a copy or
replacement of the original DuckDB text. The caller (`app/api/query.py`'s
`execute_query`, and `cli/query_hints.py`'s re-export for any CLI-side
caller that hits the same DuckDB shapes against a local database) keeps
the original error and appends the hint, so an agent debugging its own
SQL still gets the full DuckDB diagnostic (including a `Candidate
bindings:` list, when DuckDB provides one) alongside the next step.

Deliberately stdlib-only (`re`). This module is reachable from the
server (`app/api/query.py`) AND from `cli/query_hints.py`, which ships
in the CLI-only wheel an analyst `uv tool install`s -- that install never
pulls in the `[server]` extra that carries heavier SQL-parsing libraries
(sqlglot), so the alias/table extraction below is a best-effort regex
scan, not a real parse. That's acceptable here: a wrong or missed alias
resolution degrades to a slightly less specific hint, never to a wrong
answer, because the original DuckDB error is always still there too.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# column not found on a relation
# ---------------------------------------------------------------------------

# `Table "alias" does not have a column named "col"` -- the common shape.
# DuckDB also phrases the same class of error `Values list "alias" does
# not have a column named "col"` for some query shapes (seen in production
# against a JOIN whose right side went through a subquery); both name the
# relation as a bare quoted noun immediately before the fixed phrase.
_RELATION_COLUMN_MISS_RE = re.compile(r'(?:Values list|Table) "([^"]+)" does not have a column named "([^"]+)"')

# DuckDB gives no relation at all for this shape -- every table referenced
# by the statement is a candidate.
_REFERENCED_COLUMN_RE = re.compile(r'Referenced column "([^"]+)" not found in FROM clause')

# One FROM/JOIN clause: a table name (bare, dotted, or double-quoted) plus
# an optional alias. Best-effort only -- see module docstring.
_FROM_JOIN_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+(?P<table>"[^"]+"|[A-Za-z_][\w.$-]*)'
    r'(?:\s+(?:AS\s+)?(?P<alias>"[^"]+"|[A-Za-z_]\w*))?',
    re.IGNORECASE,
)

# Words that can legally follow a table name in a FROM/JOIN clause without
# being an alias -- otherwise `FROM a JOIN b` would read "JOIN" as `a`'s alias.
_NOT_AN_ALIAS = frozenset(
    {
        "on",
        "using",
        "join",
        "left",
        "right",
        "inner",
        "outer",
        "cross",
        "full",
        "natural",
        "where",
        "group",
        "order",
        "limit",
        "having",
        "union",
        "select",
        "qualify",
        "window",
    }
)


def _table_aliases(sql: str) -> dict[str, str]:
    """Best-effort ``{lowercased alias-or-table-name: original-case table
    name}`` map built from every FROM/JOIN clause in ``sql``."""
    aliases: dict[str, str] = {}
    for m in _FROM_JOIN_RE.finditer(sql or ""):
        table = m.group("table").strip('"')
        aliases.setdefault(table.lower(), table)
        alias = m.group("alias")
        if alias:
            alias_bare = alias.strip('"')
            if alias_bare.lower() not in _NOT_AN_ALIAS:
                aliases[alias_bare.lower()] = table
    return aliases


def _referenced_tables(sql: str) -> list[str]:
    """Distinct table names referenced by ``sql``'s FROM/JOIN clauses, in
    first-seen order."""
    seen: list[str] = []
    for m in _FROM_JOIN_RE.finditer(sql or ""):
        table = m.group("table").strip('"')
        if table not in seen:
            seen.append(table)
    return seen


def _unresolved_column_hint(column: str, tables: list[str]) -> str:
    if len(tables) == 1:
        return (
            f"Hint: `{tables[0]}` may not have a column named {column!r}. Run "
            f"`schema {tables[0]}` (CLI) or the `schema` MCP tool to see its "
            "real columns before retrying."
        )
    if tables:
        listed = ", ".join(f"`{t}`" for t in tables)
        return (
            f"Hint: no table in this query has a column named {column!r}. Run "
            f"`schema` on each of {listed} (or the `schema` MCP tool) before "
            "retrying."
        )
    return (
        f"Hint: no table in this query has a column named {column!r}. Run "
        "`schema <table>` (or the `schema` MCP tool) to see its real "
        "columns before retrying."
    )


def column_not_found_hint(error_text: str, sql: str) -> str | None:
    """Return a hint pointing at ``schema <table>`` for the relation a
    DuckDB "column not found" error refers to, or ``None`` if
    ``error_text`` doesn't match either known shape.

    Handles two DuckDB message shapes:

    - ``Table``/``Values list`` ``"alias" does not have a column named
      "col"`` -- the alias is resolved to its underlying table by
      scanning ``sql``'s FROM/JOIN clauses, so the hint names the real
      table Agnes's `schema` command understands, not the bare alias
      DuckDB gave.
    - ``Referenced column "col" not found in FROM clause!`` -- DuckDB
      names no relation at all; every table `sql` references becomes a
      candidate (named directly when there's only one).
    """
    m = _RELATION_COLUMN_MISS_RE.search(error_text)
    if m:
        alias, column = m.group(1), m.group(2)
        table = _table_aliases(sql).get(alias.lower())
        if table:
            subject = f"`{table}`" if table.lower() == alias.lower() else f"`{table}` (aliased `{alias}`)"
            return (
                f"Hint: {subject} has no column named {column!r}. Run "
                f"`schema {table}` (CLI) or the `schema` MCP tool to see its "
                "real columns before retrying."
            )
        # Alias didn't resolve (best-effort scan missed it) -- fall back to
        # listing every table rather than suggesting `schema <alias>`,
        # which would itself fail (the alias is not a real table id).
        return _unresolved_column_hint(column, _referenced_tables(sql))
    m = _REFERENCED_COLUMN_RE.search(error_text)
    if m:
        return _unresolved_column_hint(m.group(1), _referenced_tables(sql))
    return None


# ---------------------------------------------------------------------------
# table not found at all
# ---------------------------------------------------------------------------

_TABLE_NOT_EXIST_RE = re.compile(r"Table with name (.+?) does not exist")

# Keboola storage paths look like `in.c-<bucket>.<table>` / `out.c-<bucket>.
# <table>` -- the shape an agent copies straight from the semantic layer's
# source-system binding instead of the table's registered id.
_KEBOOLA_BUCKET_PATH_RE = re.compile(r"^(?:in|out)\.c[-_]", re.IGNORECASE)


def _looks_like_source_system_path(name: str) -> bool:
    """True when ``name`` looks like a source-system path (a Keboola
    bucket.table id, a BigQuery dataset.table, ...) rather than a
    registered Agnes catalog id -- a dotted name matching the Keboola
    bucket-path shape, or one with two or more dots (registered ids are
    plain slugs)."""
    return "." in name and (_KEBOOLA_BUCKET_PATH_RE.match(name) is not None or name.count(".") >= 2)


def unregistered_table_hint(error_text: str) -> str | None:
    """Return a hint pointing at ``catalog`` for a DuckDB "table does not
    exist" Catalog Error, or ``None`` if ``error_text`` doesn't match.

    When the unresolvable name looks like a source-system path rather
    than a registered id (dots, a bucket-style prefix), the hint says so
    explicitly -- DuckDB's own "Did you mean" suggestion in this case is
    an unrelated table name and doesn't help.
    """
    m = _TABLE_NOT_EXIST_RE.search(error_text)
    if not m:
        return None
    name = m.group(1).strip()
    if _looks_like_source_system_path(name):
        return (
            f"Hint: `{name}` looks like a source-system path (e.g. a Keboola "
            "bucket.table id or a BigQuery dataset.table), not a registered "
            "Agnes table id. Run `catalog` to find the table's registered "
            "id, then query that instead."
        )
    return (
        f"Hint: `{name}` is not a registered table. Run `catalog` to see "
        "what's available (or `catalog --json` for machine-readable output)."
    )
