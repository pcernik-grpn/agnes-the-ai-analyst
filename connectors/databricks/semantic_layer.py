"""Databricks connection settings + semantic-layer identity.

Unity Catalog *metric views* (Databricks's semantic layer) are read by
``connectors/databricks/semantic_ossie.py::DatabricksSemanticAdapter``, which
composes one Apache Ossie document per view; this module resolves the
credentials that adapter (and every other Databricks code path) uses, and
owns the one-time cutover away from the retired direct writer that used to
live here.

Before this cutover, ``sync_semantic_layer()`` fetched Unity Catalog metric
views and upserted ``metric_definitions`` rows directly, stamped
``source='databricks_semantic_layer'`` + ``source_ref=<workspace host>``.
That function, its YAML/measure parsing (``build_metric_rows``) and its
scoped prune are gone: the workspace's metric views now flow through the
standard semantic-source pipeline (``src/semantic/transports.py`` ->
``src/semantic/importer.py`` -> ``src/semantic/projection.py``), the same one
every other source (git, upload, Snowflake, Keboola) uses, stamped
``source='ossie_connection'`` + ``source_ref=<semantic source id>`` — see
:func:`ensure_semantic_source`. :func:`purge_legacy_metric_rows` is the
one-time reconciliation of the pre-cutover rows that provenance change
orphans.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# The fixed id of the `connection`-kind semantic source that backs the
# instance's Databricks workspace. A single, deterministic id — not one keyed
# on a `source_connections` row — because `resolve_databricks_settings()`
# itself resolves at most ONE workspace at a time (the default `databricks`
# connection, or the legacy `data_source.databricks.*` instance-config
# fallback); there is no per-connection identity to key a second row on yet,
# mirroring the single-workspace assumption every other Databricks code path
# already makes.
DATABRICKS_SEMANTIC_SOURCE_ID = "databricks_default"
DATABRICKS_SEMANTIC_SOURCE_NAME = "Databricks metric views"

# The pre-cutover writer's `metric_definitions.source` value. Retired but not
# forgotten: `purge_legacy_metric_rows()` reconciles rows still carrying it.
LEGACY_METRIC_SOURCE = "databricks_semantic_layer"


def _resolve_row_token(connection: dict[str, Any], token_env: str) -> str:
    """Vault-first (this connection's own vault slot), then the named env
    var / remote-attach vault fallback — same order the legacy
    instance-config path used, just with the connection's own vault slot
    checked first.

    SECURITY: only the env/vault-by-name fallback is allowlist-checked
    (RBAC review Finding 3, 2026-08-26) — mirroring the guard Snowflake's
    ``connectors.snowflake.extract_init.init_extract`` applies BEFORE its own
    ATTACH, and the write-time guard
    ``app.api.admin_source_connections._reject_disallowed_config_token_envs``
    already applies to a connection's ``config.token_env``. Without this, an
    admin-set (or yaml-seeded, ``app.connections_seed``) ``token_env`` naming
    an unrelated secret (``ANTHROPIC_API_KEY``, ``JWT_SECRET_KEY``, ...) would
    resolve here and ship out as the Databricks credential on the very next
    ATTACH/query — this connector had no such check at all. The connection's
    OWN vault slot is unaffected: it is not selected by name, so there is
    nothing for an attacker-controlled ``token_env`` to redirect.
    """
    try:
        from src.repositories import connection_secrets_repo

        vault_value = connection_secrets_repo().get(connection["id"])
    except Exception:
        vault_value = None
    if vault_value:
        return vault_value

    if not token_env:
        return ""

    from src.orchestrator_security import is_config_secret_env_allowed

    if not is_config_secret_env_allowed(token_env):
        logger.warning(
            "databricks connection %s: token_env %r is not on the config-secret "
            "allowlist; refusing to read it (add it to "
            "AGNES_CONFIG_SECRET_ENVS or use a vault secret)",
            connection.get("id"),
            token_env,
        )
        return ""

    token = os.environ.get(token_env, "")
    if token:
        return token
    try:
        from src.orchestrator_security import resolve_remote_attach_token

        return resolve_remote_attach_token(token_env) or ""
    except Exception:  # pragma: no cover - vault optional in dev contexts  # noqa: BLE001
        return ""


def _resolve_databricks_from_row(connection: dict[str, Any]) -> dict[str, Any] | None:
    """Settings from a ``source_connections`` row (``source_type='databricks'``).

    Non-secret coordinates come from ``config``; the token's env-var name is
    read from ``config`` first (a fresh wizard save), falling back to the
    row's top-level ``token_env`` column (the shape ``app.connections_seed``
    writes) and then the module default.
    """
    config = connection.get("config") or {}
    host = str(config.get("host") or "").strip()
    warehouse_id = str(config.get("warehouse_id") or "").strip()
    catalog = str(config.get("catalog") or "").strip()
    token_env = (
        str(config.get("token_env") or "").strip()
        or str(connection.get("token_env") or "").strip()
        or "DATABRICKS_TOKEN"
    )
    token = _resolve_row_token(connection, token_env)
    if not (host and warehouse_id and token):
        return None
    catalogs = config.get("semantic_layer_catalogs")
    if isinstance(catalogs, str):
        catalogs = [c.strip() for c in catalogs.split(",") if c.strip()]
    if not catalogs:
        catalogs = [catalog] if catalog else []
    return {
        "host": host,
        "warehouse_id": warehouse_id,
        "catalog": catalog,
        "catalogs": catalogs,
        "token": token,
    }


def _resolve_databricks_from_instance_config() -> dict[str, Any] | None:
    """Legacy path: ``data_source.databricks.*`` (instance.yaml / /admin/server-config).

    Kept byte-for-byte so an un-migrated instance (no databricks row yet)
    keeps resolving exactly as before D2.2 — EXCEPT for the ``token_env``
    allowlist check added by RBAC review Finding 3 (2026-08-26), for parity
    with :func:`_resolve_row_token`: a ``data_source.databricks.token_env``
    naming a secret outside the remote-attach allowlist must not resolve
    here either, same as the row path.
    """
    from app.instance_config import get_value

    host = get_value("data_source", "databricks", "host", default="") or ""
    warehouse_id = get_value("data_source", "databricks", "warehouse_id", default="") or ""
    catalog = get_value("data_source", "databricks", "catalog", default="") or ""
    token_env = get_value("data_source", "databricks", "token_env", default="DATABRICKS_TOKEN") or "DATABRICKS_TOKEN"

    from src.orchestrator_security import is_config_secret_env_allowed

    if not is_config_secret_env_allowed(token_env):
        logger.warning(
            "databricks: token_env %r is not on the config-secret allowlist; "
            "refusing to read it (add it to AGNES_CONFIG_SECRET_ENVS or "
            "use a vault secret)",
            token_env,
        )
        token = ""
    else:
        token = os.environ.get(token_env, "")
        if not token:
            try:
                from src.orchestrator_security import resolve_remote_attach_token

                token = resolve_remote_attach_token(token_env) or ""
            except Exception:  # pragma: no cover - vault optional in dev contexts  # noqa: BLE001
                token = ""
    if not (host and warehouse_id and token):
        return None
    catalogs = get_value("data_source", "databricks", "semantic_layer_catalogs", default=None)
    if isinstance(catalogs, str):
        catalogs = [c.strip() for c in catalogs.split(",") if c.strip()]
    if not catalogs:
        catalogs = [catalog] if catalog else []
    return {
        "host": host,
        "warehouse_id": warehouse_id,
        "catalog": catalog,
        "catalogs": catalogs,
        "token": token,
    }


def resolve_databricks_settings(connection: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Read the instance's Databricks settings; ``None`` when unconfigured.

    Row-first: with no explicit ``connection``, looks up the type's default
    ``source_connections`` row (``resolve_source_connection("databricks")``)
    and resolves from it when one exists — ``config.{host, warehouse_id,
    catalog}``, token vault-first via :func:`_resolve_row_token`. Falls back
    to the legacy ``data_source.databricks.*`` instance-config path —
    byte-compatible — when no row is registered yet, which is also what
    every existing zero-arg call and monkeypatch-based test exercises on an
    instance that predates the connection registry.
    """
    if connection is None:
        from src.connection_resolver import resolve_source_connection

        connection = resolve_source_connection("databricks")
    if connection is not None:
        return _resolve_databricks_from_row(connection)
    return _resolve_databricks_from_instance_config()


def ensure_semantic_source() -> str:
    """Idempotently register the Databricks connection as a ``connection``-kind
    semantic source (``adapter='databricks_semantic'``), returning its id.

    Called from the refresh endpoint before every sync — cheap (a single
    keyed lookup) and self-healing: an admin who deletes the row gets it back
    on the next scheduled run rather than a permanently broken cadence. The id
    is fixed (:data:`DATABRICKS_SEMANTIC_SOURCE_ID`) for the same reason it is
    fixed everywhere else in this module — one workspace, one row.
    """
    from src.repositories import semantic_source_repo

    repo = semantic_source_repo()
    existing = repo.get(DATABRICKS_SEMANTIC_SOURCE_ID)
    if existing is not None:
        return DATABRICKS_SEMANTIC_SOURCE_ID
    repo.create(
        id=DATABRICKS_SEMANTIC_SOURCE_ID,
        kind="connection",
        name=DATABRICKS_SEMANTIC_SOURCE_NAME,
        adapter="databricks_semantic",
        config={},
    )
    return DATABRICKS_SEMANTIC_SOURCE_ID


def purge_legacy_metric_rows() -> int:
    """One-time reconciliation of rows the retired direct writer left behind.

    The old sync stamped ``source='databricks_semantic_layer'`` +
    ``source_ref=<workspace host>``; the semantic-source pipeline that
    replaces it stamps ``source='ossie_connection'`` +
    ``source_ref='databricks_default'`` (:func:`ensure_semantic_source`) — a
    different scope, so the generic importer's own prune can never reach the
    old rows and they would otherwise linger as permanent duplicates.

    Unconditional and idempotent: every legacy-source row is deleted every
    call, so the first call after upgrade clears them and every call after
    that finds none. Metric identity is unaffected — a metric's new id is
    ``ossie_connection/databricks_default/<fqn>/<name>``
    (:func:`src.semantic.projection._scoped_id`), never colliding with the
    retired ``databricks/<fqn>/<name>`` shape, so this never removes a row
    the new sync just wrote.
    """
    from src.repositories import metric_repo

    repo = metric_repo()
    purged = 0
    for m in repo.list():
        if (m.get("source") or "") == LEGACY_METRIC_SOURCE:
            repo.delete(m["id"])
            purged += 1
    return purged
