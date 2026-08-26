"""Browse the configured Snowflake account's catalog.

The register half of "add a Snowflake table" has existed since the connector
landed; the browse half has not. Keboola enumerates buckets + tables into a
checkbox picker (``GET /api/admin/source-connections/{id}/tables``) and
BigQuery lists datasets, but the Snowflake wizard step was two free-text
inputs: nothing validated the schema/table against the account, and the
registry id was composed as ``schema + "_" + table``, so pasting a name that
already carried its schema prefix silently produced a doubled id. The
resulting row cannot heal itself either — only a re-save re-runs the remote
extract build — so a typo is a permanent registry entry pointing at nothing.

This module is deliberately read-only and stateless: it attaches, reads
``information_schema.tables``, and hands back a schema-grouped listing. It
writes no extract, touches no registry, and stores no credential — the write
side stays in ``extract_init``/the admin API.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from connectors.snowflake.attach import (
    SF_ALIAS,
    SF_EXTENSION,
    attach_snowflake,
    build_remote_attach_url,
    install_snowflake_adbc_driver,
)
from connectors.snowflake.settings import resolve_snowflake_settings
from src.duckdb_conn import _open_duckdb
from src.orchestrator_security import is_attach_host_allowed

logger = logging.getLogger(__name__)

# The account's own metadata schema. Excluded because it is noise in a picker
# and because registering one of its views would produce a table nobody asked
# for. Matched case-insensitively upstream (Snowflake upper-cases identifiers).
_EXCLUDED_SCHEMAS = ("INFORMATION_SCHEMA",)


def list_tables(
    schema: Optional[str] = None,
    *,
    attach_fn: Optional[Callable[..., None]] = None,
) -> Optional[dict[str, Any]]:
    """Return ``{"database", "schemas": [{"name", "tables": [{"name", "table_type"}]}]}``.

    ``None`` when Snowflake is not configured on this instance — a "nothing to
    browse" answer the caller turns into a setup hint, not an error.

    ``schema`` narrows the listing to one schema; omitted, every schema the
    configured user can see is returned. ``attach_fn`` is a test seam mirroring
    ``extract_init.init_extract``'s.

    Raises ``ValueError`` when the resolved host is outside
    ``AGNES_REMOTE_ATTACH_HOST_ALLOWLIST`` — the same egress gate the extract
    build applies, since this path ships the same credential. Any driver /
    catalog failure propagates to the caller (which maps it to a 502): a
    swallowed failure here would render as an empty account, and "your account
    has no tables" is a worse lie than "listing failed".
    """
    settings = resolve_snowflake_settings()
    if settings is None:
        return None

    database = settings["database"]
    url = build_remote_attach_url(
        settings["account"],
        database,
        settings["warehouse"],
        settings["user"],
        settings.get("role") or "",
    )
    if not is_attach_host_allowed(url):
        raise ValueError(
            f"Snowflake host {url!r} is not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST; "
            "refusing to send credential while listing tables"
        )

    attach = attach_fn or _default_attach_fn
    # In-memory: nothing about a catalog listing belongs on disk, and it keeps
    # this path off every extract.duckdb the orchestrator may be holding.
    conn = _open_duckdb(":memory:", read_only=False)
    try:
        attach(
            conn,
            url=url,
            token=settings.get("password") or settings.get("private_key") or "",
            passphrase=settings.get("private_key_passphrase") or None,
        )

        placeholders = ", ".join("?" for _ in _EXCLUDED_SCHEMAS)
        params: list[Any] = list(_EXCLUDED_SCHEMAS)
        sql = (
            f"SELECT table_schema, table_name, table_type "
            f"FROM {SF_ALIAS}.information_schema.tables "
            f"WHERE upper(table_schema) NOT IN ({placeholders})"
        )
        if schema:
            sql += " AND upper(table_schema) = ?"
            params.append(schema.upper())
        sql += " ORDER BY table_schema, table_name"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[dict[str, str]]] = {}
    for table_schema, table_name, table_type in rows:
        grouped.setdefault(str(table_schema), []).append(
            {"name": str(table_name), "table_type": str(table_type)}
        )

    return {
        "database": database,
        "schemas": [{"name": name, "tables": tables} for name, tables in sorted(grouped.items())],
    }


def _default_attach_fn(conn, *, url: str, token: str, passphrase: Optional[str] = None) -> None:
    """INSTALL + LOAD + ATTACH, same sequence the extract build uses.

    INSTALL is kept here (rather than LOAD-only, as the read-only query paths in
    ``src/db.py`` do) because a community extension lives in a container-local
    directory that a recreate wipes: an operator opening the wizard right after
    a deploy would otherwise get "listing failed" until something else installed
    it.
    """
    install_snowflake_adbc_driver()
    conn.execute(f"INSTALL {SF_EXTENSION} FROM community")
    conn.execute(f"LOAD {SF_EXTENSION}")
    attach_snowflake(conn, alias=SF_ALIAS, url=url, token=token, passphrase=passphrase)
