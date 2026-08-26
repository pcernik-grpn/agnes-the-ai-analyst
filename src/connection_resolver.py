"""Resolve table_registry rows to a named connection + credentials.

Resolution (spec 2026-06-12 §3.2):
  connection_id -> that connection; NULL -> default for source_type;
  nothing registered -> None (caller falls back to the legacy env path
  during the deprecation window).
Token chain: vault -> token_env -> None.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def resolve_connection(source_type: str, connection_id: Optional[str]) -> Optional[Dict[str, Any]]:
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    if connection_id:
        return repo.get(connection_id)
    return repo.get_default(source_type)


def resolve_source_connection(source_type: str) -> Optional[Dict[str, Any]]:
    """The single entry a per-source settings resolver calls with no
    explicit connection: the type's default row, or ``None`` when no row
    is registered yet (the caller falls back to the legacy instance-config
    path). Thin wrapper over :func:`resolve_connection` with
    ``connection_id=None`` — named separately because "give me the default
    connection for this type" is the shape every zero-arg settings resolver
    (Snowflake, Databricks, BigQuery) actually wants; a bare positional
    ``None`` at every call site reads like a mistake.
    """
    return resolve_connection(source_type, None)


def resolve_token(connection: Dict[str, Any]) -> Optional[str]:
    from src.repositories import connection_secrets_repo

    secret = connection_secrets_repo().get(connection["id"])
    if secret:
        return secret
    token_env = connection.get("token_env")
    if token_env:
        return os.environ.get(token_env) or None
    return None
