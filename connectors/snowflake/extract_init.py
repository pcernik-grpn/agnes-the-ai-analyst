"""Build ``extracts/snowflake/extract.duckdb`` for ``query_mode='remote'`` rows."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

import duckdb

from connectors.snowflake.attach import (
    SF_ALIAS,
    SF_EXTENSION,
    SF_TOKEN_ENV,
    attach_snowflake,
    build_remote_attach_url,
    install_snowflake_adbc_driver,
)
from src.duckdb_conn import _open_duckdb
from src.orchestrator_security import is_attach_host_allowed, is_token_env_allowed
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

_AttachFn = Callable[[duckdb.DuckDBPyConnection, str, str, Optional[str]], None]


def _ensure_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS _meta (
            table_name   VARCHAR NOT NULL,
            description  VARCHAR,
            rows         BIGINT,
            size_bytes   BIGINT,
            extracted_at TIMESTAMP,
            query_mode   VARCHAR DEFAULT 'local'
        )"""
    )


def write_remote_attach(
    conn: duckdb.DuckDBPyConnection,
    account: str,
    database: str,
    warehouse: str,
    user: str,
    role: str,
    token_env: str = SF_TOKEN_ENV,
) -> None:
    """Write the single ``_remote_attach`` row the orchestrator will replay."""
    conn.execute("DROP TABLE IF EXISTS _remote_attach")
    conn.execute(
        """CREATE TABLE _remote_attach (
            alias     VARCHAR,
            extension VARCHAR,
            url       VARCHAR,
            token_env VARCHAR
        )"""
    )
    conn.execute(
        "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
        [
            SF_ALIAS,
            SF_EXTENSION,
            build_remote_attach_url(account, database, warehouse, user, role),
            token_env,
        ],
    )


def _remote_view_sql(name: str, schema: str, table: str) -> str:
    return (
        f"CREATE OR REPLACE VIEW {quote_ident(name)} AS "
        f"SELECT * FROM {quote_ident(SF_ALIAS)}.{quote_ident(schema)}.{quote_ident(table)}"
    )


def _default_attach_fn(
    conn: duckdb.DuckDBPyConnection,
    *,
    url: str,
    token: str,
    passphrase: str | None = None,
) -> None:
    install_snowflake_adbc_driver()
    conn.execute(f"INSTALL {SF_EXTENSION} FROM community")
    conn.execute(f"LOAD {SF_EXTENSION}")
    attach_snowflake(conn, alias=SF_ALIAS, url=url, token=token, passphrase=passphrase)


def init_extract(
    output_dir: str,
    account: str,
    database: str,
    warehouse: str,
    user: str,
    role: str,
    table_configs: list[dict[str, Any]],
    *,
    token: str = "",
    token_env: str = SF_TOKEN_ENV,
    passphrase: str | None = None,
    attach_fn: Optional[_AttachFn] = None,
) -> dict[str, Any]:
    """Create ``extract.duckdb`` containing ``_meta``, ``_remote_attach`` and one view per remote table."""
    from connectors.snowflake.extractor import split_bucket

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    stats: dict[str, Any] = {"tables_registered": 0, "errors": []}

    conn = _open_duckdb(str(out / "extract.duckdb"), read_only=False)
    try:
        _ensure_meta_table(conn)

        attach = attach_fn or _default_attach_fn
        url = build_remote_attach_url(account, database, warehouse, user, role)
        if not is_attach_host_allowed(url):
            raise ValueError(
                f"Snowflake host {url!r} is not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST; "
                "refusing to send credential during extract build"
            )

        # SECURITY: checked BEFORE the attach, not after. A `token_env` not on
        # the orchestrator's allowlist means the connector does not get to pick
        # which secret gets shipped — but `token` here was already resolved
        # from that env var (or the connection's vault) by the caller, so a
        # check that ran only AFTER `attach()` had already sent it over the
        # network to `url` (defense-in-depth: writable `source_connections`
        # rows since D2.2 make an admin-set `config.token_env` an untrusted
        # input, not just an operator typo — see
        # `app.api.admin_source_connections._reject_disallowed_config_token_envs`
        # for the write-time half of this same guard) would have been a
        # warning after the leak, not a refusal before it. `resolve_remote_
        # attach_token`/replay-time ATTACH (src/orchestrator.py, src/db.py)
        # apply the identical allowlist, so a refusal here is consistent with
        # what a rebuild would do anyway — no working configuration is broken
        # by moving the check earlier.
        if token_env and not is_token_env_allowed(token_env):
            message = (
                f"snowflake extract: token_env {token_env!r} is not in the remote-attach "
                "token-env allowlist; refusing to attach. Add it to "
                "AGNES_REMOTE_ATTACH_TOKEN_ENVS (the override REPLACES the defaults), "
                "or store the credential in the connection's vault instead."
            )
            logger.error(message)
            for tc in table_configs:
                stats["errors"].append({"table": tc.get("name"), "error": message})
            return stats

        try:
            attach(conn, url=url, token=token, passphrase=passphrase)
        except Exception as exc:
            logger.error("snowflake extract: ATTACH failed: %s", exc)
            for tc in table_configs:
                stats["errors"].append({"table": tc.get("name"), "error": f"Snowflake ATTACH failed: {exc}"})
            return stats

        write_remote_attach(conn, account, database, warehouse, user, role, token_env=token_env)

        # Remove remote rows from a previous rebuild; the orchestrator will
        # recreate master views from this fresh extract.
        conn.execute("DELETE FROM _meta WHERE query_mode = 'remote'")

        for tc in table_configs:
            name = str(tc.get("name") or "").strip()
            try:
                row_database, schema = split_bucket(str(tc.get("bucket") or ""), database)
                if row_database.lower() != database.lower():
                    raise ValueError(
                        f"row targets database {row_database!r} but the ATTACH serves {database!r}; "
                        "one ATTACH serves one Snowflake database"
                    )
                sql = _remote_view_sql(name, schema, str(tc.get("source_table") or "").strip())
            except ValueError as exc:
                stats["errors"].append({"table": name, "error": str(exc)})
                continue

            try:
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, 'remote')",
                    [name, tc.get("description") or "", 0, 0],
                )
                stats["tables_registered"] += 1
            except Exception as exc:
                stats["errors"].append({"table": name, "error": str(exc)})
    finally:
        conn.close()

    return stats


def rebuild_from_registry(output_dir: str | None = None) -> dict[str, Any]:
    """Rebuild the Snowflake remote extract from all ``query_mode='remote'`` registry rows."""
    from connectors.snowflake.settings import resolve_snowflake_settings
    from src.repositories import table_registry_repo

    settings = resolve_snowflake_settings()
    if settings is None:
        return {"skipped": True, "reason": "not_configured", "tables_registered": 0, "errors": []}

    rows = [r for r in table_registry_repo().list_by_source("snowflake") if (r.get("query_mode") or "") == "remote"]
    if not rows:
        return {"skipped": True, "reason": "no_remote_rows", "tables_registered": 0, "errors": []}

    if output_dir is None:
        output_dir = str(Path(os.environ.get("DATA_DIR", "./data")) / "extracts" / "snowflake")

    token = settings.get("password") or settings.get("private_key") or ""
    token_env = (
        settings.get("token_env")
        or (settings.get("private_key_env") if settings.get("auth_type") == "key_pair" else SF_TOKEN_ENV)
        or SF_TOKEN_ENV
    )

    result = init_extract(
        output_dir,
        settings["account"],
        settings["database"],
        settings["warehouse"],
        settings["user"],
        settings.get("role") or "",
        rows,
        token=token,
        token_env=token_env,
        passphrase=settings.get("private_key_passphrase") or None,
    )
    result["skipped"] = False
    return result
