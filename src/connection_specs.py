"""Per-type validation for source_connections.config (spec 2026-06-12 §3.1).

Mirrors the ResourceTypeSpec pattern in app/resource_types.py: adding a
source type registers a spec here — no DB migration. Validation runs at
registration time (admin API / seeding), so consumers downstream never
see a denormalized config (e.g. a trailing-slash stack URL).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict


@dataclass(frozen=True)
class ConnectionSpec:
    source_type: str
    validate: Callable[[Dict[str, Any]], Dict[str, Any]]  # returns normalized config


def _validate_keboola(config: Dict[str, Any]) -> Dict[str, Any]:
    url = str(config.get("stack_url") or "").strip().rstrip("/")
    if not url:
        raise ValueError("keboola connection requires config.stack_url")
    if not url.startswith("https://"):
        raise ValueError(f"stack_url must be https://, got: {url!r}")
    return {**config, "stack_url": url}


def _validate_bigquery(config: Dict[str, Any]) -> Dict[str, Any]:
    project = str(config.get("project") or "").strip()
    if not project:
        raise ValueError("bigquery connection requires config.project")
    out = {**config, "project": project}
    out.setdefault("location", "us")
    return out


def _validate_databricks(config: Dict[str, Any]) -> Dict[str, Any]:
    host = str(config.get("host") or "").strip().rstrip("/")
    if not host:
        raise ValueError("databricks connection requires config.host")
    if not host.startswith("https://"):
        raise ValueError(f"host must be https://, got: {host!r}")
    warehouse_id = str(config.get("warehouse_id") or "").strip()
    if not warehouse_id:
        raise ValueError("databricks connection requires config.warehouse_id")
    return {**config, "host": host, "warehouse_id": warehouse_id}


def _validate_snowflake(config: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors ``connectors.snowflake.settings.resolve_snowflake_settings``'s
    read set exactly: ``account``, ``user``, ``database``, ``warehouse`` are
    the coordinates required to open a session; ``role``/``auth_type`` get
    the resolver's own defaults when omitted. The secret refs
    (``token_env``/``private_key_env``/``private_key_passphrase_env``) are
    already optional there (each falls back to a well-known default env var
    name), so they pass through unvalidated here too.
    """
    account = str(config.get("account") or "").strip()
    if not account:
        raise ValueError("snowflake connection requires config.account")
    user = str(config.get("user") or "").strip()
    if not user:
        raise ValueError("snowflake connection requires config.user")
    database = str(config.get("database") or "").strip()
    if not database:
        raise ValueError("snowflake connection requires config.database")
    warehouse = str(config.get("warehouse") or "").strip()
    if not warehouse:
        raise ValueError("snowflake connection requires config.warehouse")
    auth_type = str(config.get("auth_type") or "password").strip() or "password"
    if auth_type not in ("password", "key_pair"):
        raise ValueError(f"auth_type must be 'password' or 'key_pair', got: {auth_type!r}")
    out = {**config, "account": account, "user": user, "database": database, "warehouse": warehouse}
    out.setdefault("role", "")
    out["auth_type"] = auth_type
    return out


def _validate_sharepoint(config: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors ``connectors.sharepoint.settings.resolve_sharepoint_settings``'s
    read set: ``tenant_id``/``client_id`` are plain identity fields typed in
    the connect wizard's step 1 (never travel through a conversation). The
    certificate itself is never here — it lives in the connection's vault
    slot (``PUT .../secret``) or the deployment's ``cert_private_key_env``
    (an admin-writable secret-ref NAME, not a value, so it passes through
    unvalidated here — the shared allowlist guard runs at the admin-API layer,
    same as Snowflake/Databricks's token-env fields).

    ``scopes`` (the wizard's step-2/3 output — selected site/library rows,
    each ``{source_scope_id, display_path, anonymize, collection_id}``) is
    admin/server-written via the dedicated sharepoint scopes endpoints, not
    typed by hand, so it also passes through unvalidated here rather than
    duplicating that shape's ownership.
    """
    tenant_id = str(config.get("tenant_id") or "").strip()
    if not tenant_id:
        raise ValueError("sharepoint connection requires config.tenant_id")
    client_id = str(config.get("client_id") or "").strip()
    if not client_id:
        raise ValueError("sharepoint connection requires config.client_id")
    # Mirrors Snowflake's auth_type normalization: default the method, reject
    # the unknown, so a typo fails at save time rather than at first resolve.
    auth_method = str(config.get("auth_method") or "certificate").strip() or "certificate"
    if auth_method not in ("certificate", "client_secret"):
        raise ValueError(f"auth_method must be 'certificate' or 'client_secret', got: {auth_method!r}")
    return {**config, "tenant_id": tenant_id, "client_id": client_id, "auth_method": auth_method}


_SPECS: Dict[str, ConnectionSpec] = {
    "keboola": ConnectionSpec("keboola", _validate_keboola),
    "bigquery": ConnectionSpec("bigquery", _validate_bigquery),
    "databricks": ConnectionSpec("databricks", _validate_databricks),
    "snowflake": ConnectionSpec("snowflake", _validate_snowflake),
    "sharepoint": ConnectionSpec("sharepoint", _validate_sharepoint),
}


def validate_connection_config(source_type: str, config: Dict[str, Any]) -> Dict[str, Any]:
    spec = _SPECS.get(source_type)
    if spec is None:
        raise ValueError(f"unknown source_type: {source_type!r}")
    return spec.validate(config)
