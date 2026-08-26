"""Resolve Snowflake connection settings from a `source_connections` row,
falling back to instance.yaml / env / vault for un-migrated instances."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SF_TOKEN_ENV = "SNOWFLAKE_PASSWORD"
SF_PRIVATE_KEY_ENV = "SNOWFLAKE_PRIVATE_KEY"
SF_PRIVATE_KEY_PASSPHRASE_ENV = "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"


def _resolve_secret(name: str) -> str:
    """Resolve a named credential: env var first, then the vault."""
    from app.datasource_secrets import datasource_secret

    value = os.environ.get(name, "")
    if not value:
        try:
            value = datasource_secret(name) or ""
        except Exception:
            value = ""
    return value


def _resolve_row_secret(connection: Dict[str, Any], env_name: str) -> str:
    """Vault-first (this connection's own vault slot), then the named env
    var / generic-name vault fallback.

    Mirrors ``src.connection_resolver.resolve_token``'s vault-first
    contract, but the env-var NAME comes from the row's own ``config``
    (or the row's top-level ``token_env`` column for a row seeded before
    this shape existed) rather than a single fixed column — Snowflake can
    carry up to three independent secret-ref names (``token_env`` OR
    ``private_key_env``, plus an optional passphrase), more than the
    generic single-column resolver supports.
    """
    try:
        from src.repositories import connection_secrets_repo

        vault_value = connection_secrets_repo().get(connection["id"])
    except Exception:
        vault_value = None
    if vault_value:
        return vault_value
    return _resolve_secret(env_name)


def _resolve_from_connection_row(connection: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Settings from a ``source_connections`` row (``source_type='snowflake'``).

    Non-secret coordinates come from ``config``; the credential is
    vault-first via :func:`_resolve_row_secret`. The secret-ref NAME
    (``token_env``/``private_key_env``/``private_key_passphrase_env``) is
    read from ``config`` first (the shape a fresh wizard save writes),
    falling back to the row's top-level ``token_env`` column (the shape
    ``app.connections_seed`` writes when migrating an existing
    ``data_source.snowflake.*`` yaml block) and then the module default —
    so a row from either writer resolves identically.
    """
    config = connection.get("config") or {}
    account = str(config.get("account") or "").strip()
    user = str(config.get("user") or "").strip()
    database = str(config.get("database") or "").strip()
    warehouse = str(config.get("warehouse") or "").strip()
    role = str(config.get("role") or "").strip()
    auth_type = str(config.get("auth_type") or "password").strip() or "password"
    row_token_env = str(connection.get("token_env") or "").strip()

    if auth_type == "key_pair":
        private_key_env = str(config.get("private_key_env") or "").strip() or row_token_env or SF_PRIVATE_KEY_ENV
        private_key_passphrase_env = (
            str(config.get("private_key_passphrase_env") or "").strip() or SF_PRIVATE_KEY_PASSPHRASE_ENV
        )
        private_key = _resolve_row_secret(connection, private_key_env)
        private_key_passphrase = _resolve_secret(private_key_passphrase_env)
        if not (account and user and private_key and database and warehouse):
            return None
        return {
            "account": account,
            "user": user,
            "database": database,
            "warehouse": warehouse,
            "role": role,
            "auth_type": auth_type,
            "private_key": private_key,
            "private_key_passphrase": private_key_passphrase,
            "private_key_env": private_key_env,
            "private_key_passphrase_env": private_key_passphrase_env,
        }

    token_env = str(config.get("token_env") or "").strip() or row_token_env or SF_TOKEN_ENV
    password = _resolve_row_secret(connection, token_env)
    if not (account and user and password and database and warehouse):
        return None

    return {
        "account": account,
        "user": user,
        "password": password,
        "database": database,
        "warehouse": warehouse,
        "role": role,
        "auth_type": auth_type,
        "token_env": token_env,
    }


def _resolve_from_instance_config() -> Optional[Dict[str, Any]]:
    """Legacy path: ``data_source.snowflake.*`` (instance.yaml / /admin/server-config).

    Kept byte-for-byte so an un-migrated instance (no snowflake row yet)
    keeps resolving exactly as before D2.2.
    """
    from app.instance_config import get_value

    account = get_value("data_source", "snowflake", "account", default="") or ""
    user = get_value("data_source", "snowflake", "user", default="") or ""
    database = get_value("data_source", "snowflake", "database", default="") or ""
    warehouse = get_value("data_source", "snowflake", "warehouse", default="") or ""
    role = get_value("data_source", "snowflake", "role", default="") or ""
    auth_type = get_value("data_source", "snowflake", "auth_type", default="password") or "password"

    if auth_type == "key_pair":
        private_key_env = (
            get_value("data_source", "snowflake", "private_key_env", default=SF_PRIVATE_KEY_ENV) or SF_PRIVATE_KEY_ENV
        )
        private_key_passphrase_env = (
            get_value("data_source", "snowflake", "private_key_passphrase_env", default=SF_PRIVATE_KEY_PASSPHRASE_ENV)
            or SF_PRIVATE_KEY_PASSPHRASE_ENV
        )
        private_key = _resolve_secret(private_key_env)
        private_key_passphrase = _resolve_secret(private_key_passphrase_env)
        if not (account and user and private_key and database and warehouse):
            return None
        return {
            "account": account,
            "user": user,
            "database": database,
            "warehouse": warehouse,
            "role": role,
            "auth_type": auth_type,
            "private_key": private_key,
            "private_key_passphrase": private_key_passphrase,
            "private_key_env": private_key_env,
            "private_key_passphrase_env": private_key_passphrase_env,
        }

    token_env = get_value("data_source", "snowflake", "token_env", default=SF_TOKEN_ENV) or SF_TOKEN_ENV
    password = _resolve_secret(token_env)
    if not (account and user and password and database and warehouse):
        return None

    return {
        "account": account,
        "user": user,
        "password": password,
        "database": database,
        "warehouse": warehouse,
        "role": role,
        "auth_type": auth_type,
        "token_env": token_env,
    }


def resolve_snowflake_settings(connection: Optional[Dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Return Snowflake settings, or ``None`` if the instance is not configured.

    Row-first: with no explicit ``connection``, looks up the type's default
    ``source_connections`` row (``resolve_source_connection("snowflake")``)
    and resolves from it when one exists. Falls back to the legacy
    ``data_source.snowflake.*`` instance-config path — byte-compatible —
    when no row is registered yet, which is also what every existing
    zero-arg call and monkeypatch-based test exercises on an instance that
    predates the connection registry.
    """
    if connection is None:
        from src.connection_resolver import resolve_source_connection

        connection = resolve_source_connection("snowflake")
    if connection is not None:
        return _resolve_from_connection_row(connection)
    return _resolve_from_instance_config()


def resolve_snowflake_passphrase_for_token(token_env: str) -> Optional[str]:
    """Return the passphrase that unlocks the private key named by ``token_env``.

    Honors operator-configured ``private_key_env`` / ``private_key_passphrase_env``
    (row-first, same as :func:`resolve_snowflake_settings`). Falls back to the
    default ``SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`` env/vault lookup when the
    settings cannot be resolved or the token env does not match the
    configured private key env.
    """
    if not token_env:
        return None
    settings = resolve_snowflake_settings()
    if settings and settings.get("auth_type") == "key_pair":
        if token_env == settings.get("private_key_env", SF_PRIVATE_KEY_ENV):
            passphrase_env = settings.get("private_key_passphrase_env", SF_PRIVATE_KEY_PASSPHRASE_ENV)
            return _resolve_secret(passphrase_env) or None
    if token_env == SF_PRIVATE_KEY_ENV:
        return _resolve_secret(SF_PRIVATE_KEY_PASSPHRASE_ENV) or None
    return None
