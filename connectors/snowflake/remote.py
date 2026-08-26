"""Read a ``query_mode='remote'`` Snowflake row's columns from Snowflake.

A remote row has no parquet on disk, so every surface that introspects a table
by describing its local file comes up empty on one — and answers "not found"
for a table that is registered and queryable. `agnes schema` is the surface
that mattered: the agent rails tell an analyst (and every agent) to run it
*before* writing a query, which is exactly when a wrong column name is most
expensive.

The columns come from Snowflake's own ``information_schema.columns`` through
the same ATTACH the query path uses, so what `agnes schema` reports and what a
query can bind cannot drift apart. Mirrors
``connectors/databricks/remote.py::fetch_schema``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from connectors.snowflake.attach import (
    SF_ALIAS,
    SF_EXTENSION,
    attach_snowflake,
    build_remote_attach_url,
    install_snowflake_adbc_driver,
)

logger = logging.getLogger(__name__)


def _open_attached_conn(settings: Dict[str, Any], database: str):
    """Open an in-memory DuckDB with the Snowflake catalog attached as ``sf``."""
    from src.duckdb_conn import _open_duckdb

    url = build_remote_attach_url(
        str(settings.get("account") or ""),
        database,
        str(settings.get("warehouse") or ""),
        str(settings.get("user") or ""),
        str(settings.get("role") or ""),
    )
    token = settings.get("password") or settings.get("private_key") or ""
    passphrase = settings.get("private_key_passphrase") or None

    conn = _open_duckdb(":memory:")
    try:
        install_snowflake_adbc_driver()
        conn.execute(f"INSTALL {SF_EXTENSION} FROM community")
        conn.execute(f"LOAD {SF_EXTENSION}")
        # `attach_snowflake` enforces the host allowlist before the credential
        # goes anywhere — do not reimplement the ATTACH here to skip it.
        attach_snowflake(conn, alias=SF_ALIAS, url=url, token=token, passphrase=passphrase)
    except Exception:
        conn.close()
        raise
    return conn


def fetch_schema(
    row: Dict[str, Any],
    *,
    settings: Dict[str, Any],
    conn: Optional[Any] = None,
    allow_empty: bool = True,
) -> List[Dict[str, Any]]:
    """Column list for one remote Snowflake row.

    ``conn`` is an injection seam for tests; in production this opens (and
    closes) its own attached session. ``allow_empty=False`` turns "the upstream
    reports no columns" into an error rather than a schema that would cache as
    a valid empty answer.
    """
    from connectors.snowflake.extractor import split_bucket

    database, schema = split_bucket(str(row.get("bucket") or ""), str(settings.get("database") or ""))
    table = str(row.get("source_table") or row.get("id") or "").strip()
    if not schema or not table:
        raise ValueError(f"snowflake row {row.get('id')!r} has no schema/table to describe")

    owns_conn = conn is None
    if owns_conn:
        conn = _open_attached_conn(settings, database)
    try:
        # Schema and table are bound as parameters, not interpolated: they are
        # admin-controlled, but a registry row is still hand-editable.
        #
        # Matched VERBATIM, not upper-cased. Snowflake stores unquoted
        # identifiers folded to upper case, so a row registered in lower case
        # finds nothing here — but that same row's query also finds nothing,
        # because `full_table_sql` quotes the identifiers exactly as stored
        # (`sf."gold"."x"`). Folding the case here would make `agnes schema`
        # answer for a table no query can reach, which is the drift this whole
        # function exists to prevent. With allow_empty=False at the endpoint the
        # mismatch surfaces as an error naming the schema and table, which is
        # what an operator needs to spot the wrong case.
        #
        # This reads Snowflake's own INFORMATION_SCHEMA through the ATTACH
        # rather than the extension's `snowflake_query(...)` pass-through the
        # semantic-layer adapter uses. Verified against a live Snowflake
        # 2026-08-19: the `comment` column is present, and `data_type` comes
        # back as Snowflake's own type names (`TEXT`, `DATE`, `NUMBER`) — this
        # is not a DuckDB catalog projection with DuckDB's mapped names. If a
        # future extension release changes either, this query fails loudly
        # (missing column) or reports the wrong type names; the pass-through is
        # the fallback.
        rows = conn.execute(
            f"SELECT column_name, data_type, is_nullable, comment "
            f"FROM {SF_ALIAS}.information_schema.columns "
            f"WHERE table_schema = ? AND table_name = ? "
            f"ORDER BY ordinal_position",
            [schema, table],
        ).fetchall()
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                logger.debug("snowflake schema probe: connection close failed", exc_info=True)

    columns = [
        {
            "name": str(r[0]),
            "type": str(r[1]),
            # Snowflake reports is_nullable as 'YES'/'NO'; be tolerant of a
            # driver handing back a real boolean.
            "nullable": r[2] is True or str(r[2]).upper() == "YES",
            "description": str(r[3] or ""),
        }
        for r in rows
    ]
    if not columns and not allow_empty:
        raise ValueError(
            f"snowflake reported no columns for {database}.{schema}.{table} — the table may have "
            "been dropped, the connection repointed, or the registered bucket/source_table may "
            "differ in case from how Snowflake stores it"
        )
    return columns
